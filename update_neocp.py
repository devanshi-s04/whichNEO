"""NEOCP update cycle. Run this every 5 minutes; the website only reads the DB.

All observability and ranking is precomputed here precisely so that page loads
do no astronomy and stay fast.
"""

import argparse
import json
import logging
import sys
import time

import config
import db
import neocp
import observability
import ranking


def setup_logging(verbose=False):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.FileHandler(config.LOG_PATH), logging.StreamHandler(sys.stdout)],
    )


def run_update(conn, source=None):
    """One full cycle. Returns per-stage timings in milliseconds."""
    timings = {}
    t = time.perf_counter()

    def mark(stage):
        nonlocal t
        timings[stage] = round((time.perf_counter() - t) * 1000, 1)
        t = time.perf_counter()

    if source:
        with open(source) as f:
            raw = f.read()
    else:
        raw = neocp.fetch_neocp()
    mark("fetch")

    rows = neocp.parse_neocp(raw)
    mark("parse")
    if not rows:
        raise RuntimeError("NEOCP returned no parseable targets")

    observability.compute(rows)
    mark("observability")

    ranking.rank(rows)
    mark("ranking")

    n = db.replace_targets(conn, rows)
    mark("database")

    timings["total"] = round(sum(timings.values()), 1)
    db.set_meta(conn, "last_update_utc", db.utcnow())
    db.set_meta(conn, "last_update_count", n)
    db.set_meta(conn, "last_update_timings", json.dumps(timings))
    db.set_meta(conn, "last_update_ok", "1")
    return timings, n


def main():
    ap = argparse.ArgumentParser(description="Update the NEOCP target database")
    ap.add_argument("--source", help="read NEOCP text from a local file instead of the network")
    ap.add_argument("--loop", action="store_true",
                    help=f"run forever, every {config.UPDATE_INTERVAL_S}s")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    conn = db.connect()
    db.init(conn)

    while True:
        try:
            timings, n = run_update(conn, args.source)
            logging.info(
                "updated %d targets in %.1f ms (fetch %.1f, parse %.1f, "
                "observability %.1f, ranking %.1f, db %.1f)",
                n, timings["total"], timings["fetch"], timings["parse"],
                timings["observability"], timings["ranking"], timings["database"],
            )
        except Exception as e:
            logging.error("update failed: %s", e)
            db.set_meta(conn, "last_update_ok", "0")
            db.set_meta(conn, "last_error", str(e))
            if not args.loop:
                return 1

        if not args.loop:
            return 0
        time.sleep(config.UPDATE_INTERVAL_S)


if __name__ == "__main__":
    sys.exit(main())
