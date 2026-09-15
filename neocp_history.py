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
    is_new         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_snapshots_desig ON snapshots(desig, snapshot_ts);

CREATE TABLE IF NOT EXISTS objects (
    desig          TEXT PRIMARY KEY,
    first_seen_ts  TEXT NOT NULL,
    last_seen_ts   TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    resolved_at    TEXT
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


def ensure_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    con.commit()
    return con


def fetch_prevdes_entries():
    """Parse the previous-NEOCP-objects archive into {desig: outcome}.

    outcome is "confirmed" (linked to any other identifier) or one of the
    negative status keys above. The archive is newest-first and MPC
    sometimes revises an earlier verdict, so the first (i.e. most recent)
    entry for a given desig wins.
    """
    r = requests.get(PREVDES_URL, timeout=config.NEOCP_TIMEOUT_S, headers=_UA)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    anchor = soup.find("a", attrs={"name": "prev"})
    ul = anchor.find_next("ul") if anchor else None
    if ul is None:
        return {}

    import re
    out = {}
    for li in ul.find_all("li", recursive=False):
        # No separator: a <sub> tag (e.g. "RZ<sub>34</sub>") must concatenate
        # directly onto "RZ34" with no inserted space.
        text = re.sub(r"\s+", " ", li.get_text()).strip()
        if "(" not in text:
            continue
        body = text.split("(", 1)[0].strip()
        if " = " in body:
            left, right = (t.strip() for t in body.split(" = ", 1))
            out.setdefault(left, "confirmed")
            out.setdefault(right, "confirmed")
        else:
            parts = body.split(None, 1)
            if len(parts) != 2:
                continue
            desig, phrase = parts
            outcome = _STATUS_MAP.get(phrase.strip())
            if outcome:
                out.setdefault(desig, outcome)
    return out


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
    resolutions = fetch_prevdes_entries()
    n_resolved = 0
    for d in pending:
        outcome = resolutions.get(d)
        if outcome:
            con.execute(
                "UPDATE objects SET status=?, resolved_at=? WHERE desig=?",
                (outcome, now, d))
            n_resolved += 1
    con.commit()
    return n_resolved


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
