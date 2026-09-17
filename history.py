"""Read-only access to neocp_history.py's database.

MPC's own NEOCP feed is a live snapshot with no memory of its own -- see
neocp_history.py for why and how this fills that gap by polling it onto
disk. Nothing here writes; the background loop launched separately owns
that. This module just answers the two questions the website needs: an
overview of what has accumulated, and one object's full trajectory.
"""

import re
import sqlite3
from datetime import datetime

from neocp_history import DB_PATH, ensure_db


def connect():
    """A read connection, with the schema guaranteed to exist.

    sqlite3.connect() happily creates an empty file, so before this called
    ensure_db() the dashboard raised "no such table: objects" on any host
    where the updater had not yet run -- which is every host on the day this
    is deployed, and exactly when someone clicks the new History link to see
    what it does. Creating the tables is idempotent and costs a handful of
    no-op statements; a 500 on first look costs the feature's credibility.
    """
    conn = ensure_db()
    conn.row_factory = sqlite3.Row
    return conn


def summary(conn):
    """Overview counts for the dashboard: how much has been tracked, and
    how it has resolved so far."""
    total_objects = conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
    total_snapshots = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    by_status = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM objects GROUP BY status")}
    earliest = conn.execute(
        "SELECT MIN(snapshot_ts) FROM snapshots").fetchone()[0]
    return {
        "total_objects": total_objects,
        "total_snapshots": total_snapshots,
        "by_status": by_status,
        "tracking_since": earliest,
    }


# URL sort key -> the actual SQL expression to order by. A whitelist, not a
# straight interpolation of the query param, since `sort` comes from the
# request.
SORTABLE_COLUMNS = {
    "desig": "o.desig",
    "first_seen": "o.first_seen_ts",
    "last_seen": "o.last_seen_ts",
    "status": "o.status",
    "decided": "o.resolved_at",
    "score": "s.score",
    "vmag": "s.vmag",
    "nobs": "s.nobs",
    "arc": "s.arc_days",
    "unseen": "s.not_seen_days",
}


# Metadata for the dashboard's filter-builder UI: what fields can be
# filtered on, how to label them, and what kind of control/operators each
# one needs (a number needs relational operators; an enum like status is
# best picked from a dropdown of its real values rather than typed and
# risking a typo that silently matches nothing). Keys match SORTABLE_COLUMNS
# exactly -- the UI only ever sends back field names this module already
# whitelists for sorting.
FILTER_FIELDS = [
    {"key": "desig", "label": "Designation", "kind": "text"},
    {"key": "status", "label": "Status", "kind": "enum", "options": [
        "pending", "confirmed", "not_confirmed", "not_minor_planet",
        "does_not_exist", "suspected_artificial"]},
    {"key": "score", "label": "Score", "kind": "number"},
    {"key": "vmag", "label": "V mag", "kind": "number"},
    {"key": "nobs", "label": "NObs", "kind": "number"},
    {"key": "arc", "label": "Arc (days)", "kind": "number"},
    {"key": "unseen", "label": "Unseen (days)", "kind": "number"},
    {"key": "first_seen", "label": "First seen", "kind": "text"},
    {"key": "last_seen", "label": "Last seen", "kind": "text"},
    {"key": "decided", "label": "Decided", "kind": "text"},
]

# <field><op><value> tokens, e.g. "score>=90" or "status=confirmed" --
# whitespace-separated tokens in one query string AND together. No spaces
# around the operator, and no quoting: none of the values these fields hold
# (numbers, or status/desig strings) ever contain a space. The dashboard's
# filter-builder UI is what actually produces these now (see FILTER_FIELDS
# above); this string form only still exists so the current filter set can
# round-trip through the URL for bookmarking/sharing.
_FILTER_TOKEN_RE = re.compile(r"^([A-Za-z_]+)(!=|>=|<=|=|>|<|~)(.+)$")
_FILTER_SQL_OPS = {"!=": "!=", ">=": ">=", "<=": "<=", "=": "=",
                   ">": ">", "<": "<", "~": "LIKE"}


def split_filter_tokens(q):
    """Parse a filter query string into [(field, op, value), ...], without
    building any SQL yet -- used both to build list_objects's WHERE clause
    (see parse_filters) and to tell the dashboard's filter-builder UI which
    chips to show pre-filled when a URL with ?q=... is opened directly.

    Raises ValueError with a message meant to be shown back to the user if
    a token doesn't parse or names a field that isn't one of
    SORTABLE_COLUMNS.
    """
    q = (q or "").strip()
    if not q:
        return []
    tokens = []
    for token in q.split():
        m = _FILTER_TOKEN_RE.match(token)
        if not m:
            raise ValueError(f"can't parse {token!r} -- expected field<op>value, "
                             "e.g. score>=90")
        field, op, value = m.groups()
        if field not in SORTABLE_COLUMNS:
            raise ValueError(f"unknown field {field!r} -- try one of: "
                             + ", ".join(sorted(SORTABLE_COLUMNS)))
        tokens.append((field, op, value))
    return tokens


def parse_filters(q):
    """Parse a small SQL-like filter expression into (where_sql, params)
    for list_objects, e.g. "score>=90 status=confirmed vmag<18" filters to
    objects scoring at least 90, confirmed, and brighter than V=18.

    Every value is bound as a query parameter, never spliced into the SQL
    text, so this stays safe against injection despite reading like a raw
    query. Raises ValueError -- see split_filter_tokens -- if the string
    doesn't parse.
    """
    clauses, params = [], []
    for field, op, value in split_filter_tokens(q):
        col = SORTABLE_COLUMNS[field]
        sql_op = _FILTER_SQL_OPS[op]
        if sql_op == "LIKE":
            clauses.append(f"{col} LIKE ?")
            params.append(f"%{value}%")
            continue
        cast_value = value
        for caster in (int, float):
            try:
                cast_value = caster(value)
                break
            except ValueError:
                continue
        clauses.append(f"{col} {sql_op} ?")
        params.append(cast_value)
    return " AND ".join(clauses), params


def list_objects(conn, sort=None, ascending=True, where_sql="", params=None):
    """One row per tracked object: its latest polled values plus status.

    Defaults to most-recently-active first when sort is None -- the
    dashboard's own baseline view, not treated as any column being
    "actively" sorted (the template only highlights a header once a URL
    sort key has actually been chosen). where_sql/params come from
    parse_filters -- already-safe SQL text with its values bound
    separately, not raw user input.
    """
    order = f"{SORTABLE_COLUMNS[sort]} {'ASC' if ascending else 'DESC'}" \
        if sort in SORTABLE_COLUMNS else "o.last_seen_ts DESC"
    where_clause = f"WHERE {where_sql}" if where_sql else ""
    return conn.execute(f"""
        SELECT o.desig, o.first_seen_ts, o.last_seen_ts, o.status, o.resolved_at,
               s.score, s.vmag, s.arc_days, s.not_seen_days, s.nobs
        FROM objects o
        JOIN snapshots s ON s.id = (
            SELECT id FROM snapshots WHERE desig = o.desig
            ORDER BY snapshot_ts DESC LIMIT 1
        )
        {where_clause}
        ORDER BY {order}
    """, params or []).fetchall()


def get_object(conn, desig):
    """One object's own row from `objects` -- first/last seen and outcome --
    independent of whether it's still on the live board. Used for the
    "archived" object page: once an object resolves, update_neocp.py's next
    cycle rewrites `targets` to just the current live list and drops it, and
    prunes its ephemeris_cache entry too, but this table is append-only and
    never loses it."""
    return conn.execute(
        "SELECT * FROM objects WHERE desig=?", (desig,)).fetchone()


def object_history(conn, desig):
    """Full time series for one object, oldest first -- every raw column
    from every poll while it was on the board."""
    return conn.execute(
        "SELECT * FROM snapshots WHERE desig=? ORDER BY snapshot_ts", (desig,)
    ).fetchall()


def to_unix(iso_ts):
    return datetime.fromisoformat(iso_ts).timestamp()
