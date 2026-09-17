"""Longitudinal history of NEOCP objects: every raw column, every poll.

The NEO Confirmation Page (toconfirm_tabular.html / neocp.txt) is a live
snapshot -- MPC keeps no record of how an object's digest2 score, V
magnitude, arc, or anything else changed night to night while it sat there,
nor whether it was eventually confirmed. This fills that gap by polling the
same list.py already parses (see neocp.parse_neocp) on a schedule and
appending a new row per object per poll, rather than overwriting -- the
whole point is the trajectory, not just the latest value.

Two tables:

  snapshots   one row per (object, poll time), every raw NEOCP column.
              Never updated once written -- an object's full time series
              on the board is just its snapshots in snapshot_ts order.
  objects     one row per object: when first/last seen, and its eventual
              outcome, filled in once the "previous NEOCP objects" archive
              (ToConfirm_PrevDes.html) reports one, with resolved_at marking
              exactly when this poller noticed. 'pending' until then.

Also rewrites organization_stats.CSV_PATH every cycle: confirmation rate per
submitting group, derived from the objects table above -- see that module
for what "group" means and why it's a heuristic.

update_neocp.py's own update cycle already fetches the NEOCP list every
five minutes to build the live board; record_cycle() below lets it hand
that same parsed list straight to this database instead of this module
fetching an identical copy of it again on its own schedule. That merged
cycle is the normal way this data gets recorded now -- see run_update() in
update_neocp.py. --loop (see main() below) still exists for running this
module standalone (this host has no working cron daemon to call poll_once()
on a schedule some other way), but don't run it at the same time as
update_neocp.py --loop, or the list is back to being fetched twice.
"""

import argparse
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

import config
import neocp
import organization_stats

_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(config.DATA_DIR, "neocp_history.db")
PREVDES_URL = "https://www.minorplanetcenter.net/iau/NEO/ToConfirm_PrevDes.html"
_UA = {"User-Agent": "visnjan_whichneo/0.2 "
                     "(Visnjan Observatory L01 follow-up planning)"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    desig          TEXT NOT NULL,
    snapshot_ts    TEXT NOT NULL,
    score          INTEGER,
    ra_deg         REAL,
    dec_deg        REAL,
    vmag           REAL,
    nobs           INTEGER,
    arc_days       REAL,
    hmag           REAL,
    not_seen_days  REAL,
    update_note    TEXT,
    note_flag      TEXT,
    is_new         INTEGER,
    -- 1 for a row reconstructed from an archived run rather than observed by
    -- this poller. Those are daily samples, not five-minute polls, and they
    -- carry only the fields the archive kept -- so anything reasoning about
    -- cadence, or about a column they do not have, must exclude them.
    retrospective  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snapshots_desig ON snapshots(desig, snapshot_ts);

CREATE TABLE IF NOT EXISTS objects (
    desig          TEXT PRIMARY KEY,
    first_seen_ts  TEXT NOT NULL,
    last_seen_ts   TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    resolved_at    TEXT,
    -- What the object turned out to BE, not merely that it turned out to be
    -- something. "confirmed" alone cannot answer the question ds42 is asked:
    -- a main-belt asteroid that lands on NEOCP is confirmed too. The
    -- designation is what lets us look up an orbit later and decide; the
    -- MPEC is the announcement, which is a related but different fact.
    --
    -- is_neo stays NULL until an orbit source is settled -- see ds42.md.
    -- NULL means "not determined", not "no".
    linked_desig   TEXT,
    mpec           TEXT,
    is_neo         INTEGER,
    perihelion_au  REAL,
    -- 1 for a row created by backfill rather than observed on the board.
    -- Its first_seen/last_seen say when we learned of the object, not when
    -- it was actually on NEOCP, so any analysis of residence time has to
    -- exclude these.
    retrospective  INTEGER NOT NULL DEFAULT 0
);
"""

# The archive's only four negative-outcome phrases (verified by scanning the
# full current page). Anything after "=" is a real object -- new designation,
# a merge into another tracklet, or a match to something already known --
# and is treated as "confirmed" regardless of which of those it is.
_STATUS_MAP = {
    "was not confirmed": "not_confirmed",
    "was not a minor planet": "not_minor_planet",
    "does not exist": "does_not_exist",
    "was suspected artificial": "suspected_artificial",
}


def _now():
    return datetime.now(timezone.utc).isoformat()


_ADDED_SNAPSHOT_COLUMNS = [
    ("retrospective", "INTEGER NOT NULL DEFAULT 0"),
]

_ADDED_COLUMNS = [
    ("linked_desig", "TEXT"),
    ("mpec", "TEXT"),
    ("is_neo", "INTEGER"),
    ("perihelion_au", "REAL"),
    ("retrospective", "INTEGER NOT NULL DEFAULT 0"),
]


def ensure_db():
    con = sqlite3.connect(DB_PATH)
    # WAL for the same reason targets.db uses it: the updater writes a row per
    # object every five minutes while the dashboard is being read, and the
    # default rollback journal makes those two block each other. It also
    # matters for /history/download.db, which reads the file while a write
    # may be in flight.
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    # CREATE TABLE IF NOT EXISTS does nothing to a table that already exists,
    # so a database written before these columns existed would silently never
    # gain them.
    have = {r[1] for r in con.execute("PRAGMA table_info(objects)")}
    for name, decl in _ADDED_COLUMNS:
        if name not in have:
            con.execute(f"ALTER TABLE objects ADD COLUMN {name} {decl}")
    have = {r[1] for r in con.execute("PRAGMA table_info(snapshots)")}
    for name, decl in _ADDED_SNAPSHOT_COLUMNS:
        if name not in have:
            con.execute(f"ALTER TABLE snapshots ADD COLUMN {name} {decl}")
    con.commit()
    return con


# A permanent minor-planet designation: four-digit year, space, two letters,
# optional order number -- "2026 RR39", "2026 LH3". NEOCP tracklet ids never
# look like this: they are unspaced alphanumerics such as P22pRQ8, ZTF10GC,
# 6JD1C21. The distinction matters because an archive entry can be either
# "2026 RR39 = 6JD1C21" (a tracklet that became a designated object) or
# "ZTF10GC = SK000cT" (two tracklets of the same object merged), and only the
# first tells us what the thing actually is.
_PERMANENT_RE = re.compile(r"^\d{4} [A-Z]{2}\d*$")


def parse_prevdes(html):
    """{desig: {status, linked_desig, mpec}} from the archive page.

    Split from the fetch so it can be tested against a saved page without the
    network.

    status is "confirmed" (linked to any other identifier) or one of the
    negative keys above. linked_desig is the permanent designation where the
    entry gives one -- the field that makes this archive usable as ground
    truth rather than merely as a record that something resolved, since
    "confirmed" is equally true of a main-belt asteroid. mpec is the MPEC
    reference where MPC announced one.

    The archive is newest-first and MPC sometimes revises an earlier verdict,
    so the first entry for a given desig wins.
    """
    soup = BeautifulSoup(html, "lxml")
    anchor = soup.find("a", attrs={"name": "prev"})
    ul = anchor.find_next("ul") if anchor else None
    if ul is None:
        return {}

    out = {}

    def record(desig, status, linked=None, mpec=None):
        if desig and desig not in out:
            out[desig] = {"status": status, "linked_desig": linked,
                          "mpec": mpec}

    for li in ul.find_all("li", recursive=False):
        # No separator: a <sub> tag (e.g. "RZ<sub>34</sub>") must concatenate
        # directly onto "RZ34" with no inserted space.
        text = re.sub(r"\s+", " ", li.get_text()).strip()
        if "(" not in text:
            continue
        body = text.split("(", 1)[0].strip()
        link = li.find("a", href=re.compile(r"/mpec/"))
        mpec = re.sub(r"\s+", " ", link.get_text()).strip() if link else None

        if " = " in body:
            left, right = (t.strip() for t in body.split(" = ", 1))
            # Whichever side is a permanent designation identifies the object;
            # the other side is the tracklet we knew it by. Either may be the
            # permanent one, and in a tracklet merge neither is.
            perm = next((s for s in (left, right) if _PERMANENT_RE.match(s)),
                        None)
            for side in (left, right):
                if side != perm:
                    record(side, "confirmed", perm, mpec)
            if perm:
                record(perm, "confirmed", perm, mpec)
        else:
            parts = body.split(None, 1)
            if len(parts) != 2:
                continue
            desig, phrase = parts
            outcome = _STATUS_MAP.get(phrase.strip())
            if outcome:
                record(desig, outcome)
    return out


def fetch_prevdes_entries():
    """parse_prevdes() applied to a freshly fetched archive page."""
    r = requests.get(PREVDES_URL, timeout=config.NEOCP_TIMEOUT_S, headers=_UA)
    r.raise_for_status()
    return parse_prevdes(r.text)


def record_snapshot(con, rows, now):
    """Log one already-fetched, already-parsed NEOCP listing -- one row per
    object into `snapshots`, plus an upsert into `objects` for first/last
    seen. Split out from poll_once() so update_neocp.py's own update cycle
    can feed this the same parsed rows it already fetched for the live
    board, instead of neocp_history.py fetching the identical list over
    again on its own schedule."""
    for r in rows:
        con.execute(
            "INSERT INTO snapshots (desig, snapshot_ts, score, ra_deg, dec_deg, "
            "vmag, nobs, arc_days, hmag, not_seen_days, update_note, note_flag, "
            "is_new) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["desig"], now, r["score"], r["ra_deg"], r["dec_deg"], r["vmag"],
             r["nobs"], r["arc_days"], r["hmag"], r["not_seen_days"],
             r["update_note"], r["note_flag"], int(r["is_new"])))
        con.execute(
            "INSERT INTO objects (desig, first_seen_ts, last_seen_ts, status) "
            "VALUES (?, ?, ?, 'pending') "
            "ON CONFLICT(desig) DO UPDATE SET last_seen_ts=excluded.last_seen_ts",
            (r["desig"], now, now))
    con.commit()


def resolve_pending(con, now):
    """Check MPC's "previous NEOCP objects" archive for any object still
    marked pending here, and record its outcome. A request of its own --
    to a different page than the NEOCP list itself -- made only when there
    is at least one pending object to check, so an otherwise-quiet cycle
    costs nothing extra."""
    pending = [row[0] for row in
               con.execute("SELECT desig FROM objects WHERE status='pending'")]
    if not pending:
        return 0
    return apply_resolutions(con, fetch_prevdes_entries(), now, pending)


def apply_resolutions(con, resolutions, now, desigs=None):
    """Write parsed archive outcomes onto `objects`. Split from the fetch so
    a backfill can replay the same archive over objects recorded before this
    ran, and so it can be tested without the network.

    Only ever fills in: an object already resolved keeps the verdict and the
    timestamp it was first given, because resolved_at means "when we learned
    this", and rewriting it on every subsequent pass would destroy that.
    """
    if desigs is None:
        desigs = [row[0] for row in
                  con.execute("SELECT desig FROM objects WHERE status='pending'")]
    n = 0
    for d in desigs:
        entry = resolutions.get(d)
        if not entry:
            continue
        con.execute(
            "UPDATE objects SET status=?, resolved_at=?, linked_desig=?, "
            "mpec=? WHERE desig=? AND status='pending'",
            (entry["status"], now, entry.get("linked_desig"),
             entry.get("mpec"), d))
        n += 1
    con.commit()
    return n


def record_cycle(con, rows, now=None):
    """Everything one poll used to do, given rows someone else already
    fetched and parsed: log the snapshot, resolve anything that has since
    left the board, and refresh the confirmation-rate CSV. This is what
    update_neocp.py's own update cycle calls directly -- see poll_once()
    below for the standalone equivalent that fetches its own copy of the
    list, which the two should never both be doing at once."""
    now = now or _now()
    record_snapshot(con, rows, now)
    n_resolved = resolve_pending(con, now)
    # Cheap relative to the requests above -- pure SQL over what's already
    # on disk -- so just redone every cycle rather than tracked separately
    # for whether anything actually changed.
    organization_stats.write_csv(con)
    return len(rows), n_resolved


def backfill_from_scores(con, score_desigs, now=None):
    """Record objects we have already scored but never polled, then resolve
    every pending object against the archive in one fetch.

    ds42 has been scoring objects since before this history existed, and the
    archive still lists outcomes going back weeks -- so those labels can be
    recovered rather than waited for. Objects that have already left NEOCP
    are inserted with first_seen = last_seen = now, which is honestly wrong
    as a timestamp and is why `retrospective` marks them: they say when we
    learned about the object, not when it was on the board.

    One fetch, however many objects. Returns (inserted, resolved).
    """
    now = now or _now()
    known = {r[0] for r in con.execute("SELECT desig FROM objects")}
    fresh = [d for d in score_desigs if d and d not in known]
    for d in fresh:
        con.execute(
            "INSERT INTO objects (desig, first_seen_ts, last_seen_ts, status, "
            "retrospective) VALUES (?,?,?,'pending',1)", (d, now, now))
    con.commit()
    resolved = apply_resolutions(con, fetch_prevdes_entries(), now)
    return len(fresh), resolved


def import_archived_run(con, night, meta, snapshot_ts):
    """Reconstruct one poll from a banked ds42 run's view of the board.

    The runs under ds42/runs/<night>/ carry meta.json: the board's own score,
    V, nobs, arc and observability for every object it considered that night.
    That is a genuine observation of NEOCP state at a known time, and it is
    the only record of those nights that exists -- this poller was not running
    then, and MPC keeps no archive of past listings.

    Marked retrospective, because it is one daily sample rather than a
    five-minute poll and carries only the fields the run kept. Anything
    reasoning about cadence has to exclude these, and anything reading
    ra_deg or note_flag will find them NULL.

    Idempotent: a night already imported is skipped, so this can be re-run.
    """
    already = con.execute(
        "SELECT count(*) FROM snapshots WHERE snapshot_ts = ?",
        (snapshot_ts,)).fetchone()[0]
    if already:
        return 0, 0

    inserted = tracked = 0
    for desig, m in sorted(meta.items()):
        con.execute(
            "INSERT INTO snapshots (desig, snapshot_ts, score, vmag, nobs, "
            "arc_days, retrospective) VALUES (?,?,?,?,?,?,1)",
            (desig, snapshot_ts, m.get("score"), m.get("vmag"),
             m.get("nobs"), m.get("arc")))
        inserted += 1
        # first_seen only moves earlier; last_seen only later. A night
        # imported out of order must not make an object look younger than it
        # is, and the live poller has been writing these since today.
        cur = con.execute(
            "INSERT INTO objects (desig, first_seen_ts, last_seen_ts, status, "
            "retrospective) VALUES (?,?,?,'pending',1) "
            "ON CONFLICT(desig) DO UPDATE SET "
            "  first_seen_ts = min(first_seen_ts, excluded.first_seen_ts),"
            "  last_seen_ts  = max(last_seen_ts,  excluded.last_seen_ts)",
            (desig, snapshot_ts, snapshot_ts))
        tracked += cur.rowcount
    con.commit()
    return inserted, tracked


def poll_once():
    """Standalone poll: fetches the NEOCP list itself and records it. Only
    for running this module on its own (e.g. as a fallback, or for
    testing) -- when update_neocp.py's --loop is running, it already calls
    record_cycle() with the list it fetches for the live board, so running
    this loop too would mean two independent requests for the same page."""
    con = ensure_db()
    raw = neocp.fetch_neocp()
    rows = neocp.parse_neocp(raw)
    result = record_cycle(con, rows)
    con.close()
    return result


DEFAULT_INTERVAL_S = 300   # 5 min, matching update_neocp.py's own cadence
LOG_PATH = os.path.join(config.DATA_DIR, "neocp_history.log")


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)])


def main():
    ap = argparse.ArgumentParser(description="Poll NEOCP into a history database")
    ap.add_argument("--loop", action="store_true",
                    help=f"run forever, every {DEFAULT_INTERVAL_S}s, fetching "
                         "the NEOCP list itself -- standalone fallback only; "
                         "update_neocp.py --loop normally does this as part "
                         "of its own cycle instead, so don't run both at once")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_S)
    args = ap.parse_args()

    setup_logging()
    while True:
        try:
            n_snap, n_res = poll_once()
            logging.info("logged %d snapshots, resolved %d objects -> %s",
                        n_snap, n_res, DB_PATH)
        except Exception as e:
            logging.exception("poll failed: %s", e)
            if not args.loop:
                return 1
        if not args.loop:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
