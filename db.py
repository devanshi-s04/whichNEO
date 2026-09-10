"""SQLite storage.

Two tables by design: `targets` is owned by the updater and rewritten every
cycle, while `observer_state` is owned by the website and never touched by the
updater. That separation is what makes "mark observed" survive a NEOCP refresh.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    desig                   TEXT PRIMARY KEY,
    digest2                 INTEGER,
    ra_deg                  REAL,
    dec_deg                 REAL,
    vmag                    REAL,
    hmag                    REAL,
    nobs                    INTEGER,
    arc_days                REAL,
    not_seen_days           REAL,
    update_note             TEXT,
    is_new                  INTEGER,
    survey                  TEXT,

    alt_deg                 REAL,
    az_deg                  REAL,
    airmass                 REAL,
    hour_angle_deg          REAL,
    pre_meridian            INTEGER,
    rising                  INTEGER,
    transit_utc             TEXT,
    minutes_to_transit      REAL,
    moon_sep_deg            REAL,
    moon_alt_deg            REAL,
    moon_illum              REAL,
    sun_alt_deg             REAL,
    sun_elong_deg           REAL,
    window_remaining_min    REAL,
    minutes_until_observable REAL,

    score_total             REAL,
    score_digest2           REAL,
    score_arc               REAL,
    score_magnitude         REAL,
    observable              INTEGER,
    flags                   TEXT,

    first_seen_utc          TEXT,
    last_updated_utc        TEXT
);

CREATE TABLE IF NOT EXISTS observer_state (
    desig           TEXT PRIMARY KEY,
    observed        INTEGER DEFAULT 0,
    observed_at_utc TEXT,
    hidden          INTEGER DEFAULT 0,
    priority_bump   REAL DEFAULT 0,
    note            TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_targets_rank ON targets(observable DESC, score_total DESC);
"""

_TARGET_COLS = [
    "desig", "digest2", "ra_deg", "dec_deg", "vmag", "hmag", "nobs",
    "arc_days", "not_seen_days", "update_note", "is_new", "survey",
    "alt_deg", "az_deg", "airmass", "hour_angle_deg", "pre_meridian",
    "rising", "transit_utc", "minutes_to_transit", "moon_sep_deg",
    "moon_alt_deg", "moon_illum", "sun_alt_deg", "sun_elong_deg",
    "window_remaining_min", "minutes_until_observable",
    "score_total", "score_digest2", "score_arc", "score_magnitude",
    "observable", "flags", "first_seen_utc", "last_updated_utc",
]


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path=None):
    path = path or config.DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    # WAL lets the website read while the updater writes.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def existing_first_seen(conn):
    return {r["desig"]: r["first_seen_utc"]
            for r in conn.execute("SELECT desig, first_seen_utc FROM targets")}


def replace_targets(conn, rows):
    """Rewrite the target table. observer_state is intentionally untouched."""
    now = utcnow()
    first_seen = existing_first_seen(conn)

    payload = []
    for r in rows:
        r = dict(r)
        r["flags"] = json.dumps(r.get("flags") or [])
        r["observable"] = int(bool(r.get("observable")))
        r["pre_meridian"] = int(bool(r.get("pre_meridian")))
        r["rising"] = int(bool(r.get("rising")))
        r["is_new"] = int(bool(r.get("is_new")))
        r["first_seen_utc"] = first_seen.get(r["desig"], now)
        r["last_updated_utc"] = now
        payload.append(tuple(r.get(c) for c in _TARGET_COLS))

    placeholders = ",".join("?" * len(_TARGET_COLS))
    with conn:
        conn.execute("DELETE FROM targets")
        conn.executemany(
            f"INSERT INTO targets ({','.join(_TARGET_COLS)}) VALUES ({placeholders})",
            payload,
        )
    return len(payload)


def load_targets(conn, include_hidden=False, include_observed=False):
    """Targets joined with observer state, ready for display."""
    sql = """
        SELECT t.*,
               COALESCE(s.observed, 0)      AS observed,
               s.observed_at_utc            AS observed_at_utc,
               COALESCE(s.hidden, 0)        AS hidden,
               COALESCE(s.priority_bump, 0) AS priority_bump,
               s.note                       AS note
        FROM targets t
        LEFT JOIN observer_state s ON s.desig = t.desig
    """
    rows = [dict(r) for r in conn.execute(sql)]
    for r in rows:
        r["flags"] = json.loads(r["flags"] or "[]")
    if not include_hidden:
        rows = [r for r in rows if not r["hidden"]]
    if not include_observed:
        rows = [r for r in rows if not r["observed"]]
    return rows


def set_state(conn, desig, **fields):
    """Upsert one observer-state field set."""
    allowed = {"observed", "observed_at_utc", "hidden", "priority_bump", "note"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    cols = ",".join(fields)
    placeholders = ",".join("?" * len(fields))
    updates = ",".join(f"{k}=excluded.{k}" for k in fields)
    with conn:
        conn.execute(
            f"INSERT INTO observer_state (desig,{cols}) VALUES (?,{placeholders}) "
            f"ON CONFLICT(desig) DO UPDATE SET {updates}",
            (desig, *fields.values()),
        )


def set_meta(conn, key, value):
    with conn:
        conn.execute(
            "INSERT INTO meta (key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default
