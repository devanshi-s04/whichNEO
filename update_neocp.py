"""NEOCP update cycle. Run every 5 minutes; the website only reads the DB.

All astronomy, filtering and ordering happens here precisely so that page
loads do no computation and stay fast.

Network cost is kept down two ways: objects rejected by the cheap NEOCP-level
filters never trigger a per-object request at all, and ephemerides are cached
against a signature of the object's NEOCP row so they are re-requested only
when new observations change the solution.
"""

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import config
import db
import ephemeris
import neocp
import output
import pipeline
import ranking


def setup_logging(verbose=False):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.FileHandler(config.LOG_PATH),
                  logging.StreamHandler(sys.stdout)])


def cheap_reject(t):
    """Filters needing no network. Ordered cheapest first."""
    out = []
    if t["score"] < config.MIN_SCORE:
        out.append("LOW_SCORE")
    if t["arc_days"] < config.MIN_ARC_DAYS:
        out.append("SHORT_ARC")
    if t["not_seen_days"] > config.MAX_NOT_SEEN_DAYS:
        out.append("NOT_SEEN")
    if t["desig"] in config.BLACKLIST:
        out.append("BLACKLISTED")
    if config.NEO_ONLY and t.get("q") is not None and t.get("e") is not None:
        if not (t["q"] < config.NEO_Q_MAX or t["e"] > config.NEO_E_MIN):
            out.append("NOT_NEO")
    return out


def needs_refetch(entry, target, now):
    """Whether this object's cached ephemeris has to be requested again.

    `entry` is (signature, payload) from the cache, or None.

    Two independent reasons, and missing either one produces a board that is
    confidently wrong:

    1. **The solution changed.** New astrometry moves the object, so the
       signature no longer matches. This is the usual case.

    2. **The window ran out.** MPC computes an ephemeris over a fixed span
       beginning when we ask, roughly a day. The signature says nothing about
       that span, so an object attracting no new observations is never
       re-requested and its cached ephemeris eventually stops covering
       tonight. usable_rows() then returns LAST night's window without
       complaint -- peak time, azimuth, exposure, the whole plan, a day stale.
       Measured on the live board: 15 of 38 observable targets, one of them 14
       hours past the end of its ephemeris, showing a position most of the sky
       away from the truth.

    Reason 2 is the shadow of an earlier fix. `not_seen_days` was removed from
    the signature because it advances with the wall clock and so invalidated
    every entry on every cycle -- correct, but it was also the only
    time-varying term, and taking it out left nothing watching the calendar.
    The answer is not to put it back but to ask the precise question: does this
    ephemeris still say anything about the rest of tonight?

    The backoff keeps an object that genuinely never rises again from being
    re-requested every cycle, since a fresh fetch would end in the past too.
    """
    if entry is None:
        return True
    signature, payload = entry
    if signature != ephemeris.signature(target):
        return True
    # Backfill entries cached before a field existed. A key being absent means
    # never fetched; present-but-null means MPC had nothing, which must not
    # trigger a refetch every cycle.
    if any(k not in payload for k in
           ("offsets", "obs_codes", "last_row_ts", "fetched_ts")):
        return True

    last = payload.get("last_row_ts")
    if last is None or last >= now:
        return False
    return now - (payload.get("fetched_ts") or 0) > \
        config.EPHEMERIS_REFETCH_BACKOFF_S


def run_update(conn, source=None):
    timings = {}
    t0 = time.perf_counter()

    def mark(stage):
        nonlocal t0
        timings[stage] = round((time.perf_counter() - t0) * 1000, 1)
        t0 = time.perf_counter()

    raw = open(source).read() if source else neocp.fetch_neocp()
    targets = neocp.parse_neocp(raw)
    if not targets:
        raise RuntimeError("NEOCP returned no parseable targets")
    mark("fetch_list")

    try:
        orbits = neocp.parse_neocp_info(neocp.fetch_neocp_info())
    except Exception as e:
        logging.warning("neocp_info unavailable (%s); q/e filters inactive", e)
        orbits = {}
    mark("fetch_orbits")

    for t in targets:
        o = orbits.get(t["desig"]) or {}
        t["q"], t["e"], t["incl"] = o.get("q"), o.get("e"), o.get("incl")
        t["cheap_reject"] = cheap_reject(t)

    now = time.time()
    cache = db.load_cache(conn)
    candidates = [t for t in targets if not t["cheap_reject"]]

    stale = [t for t in candidates if needs_refetch(cache.get(t["desig"]), t, now)]
    fresh = ephemeris.fetch_many(t["desig"] for t in stale)
    mark("fetch_ephemerides")

    # Auxiliary pages, only for objects we just refetched. Two more requests
    # each, so they run through the same bounded pool -- done sequentially a
    # cold start spends over a minute here.
    def _aux(t):
        e = fresh.get(t["desig"])
        if e is None:
            return None
        # One fetch serves both: the spread feeds the filter cascade, the
        # points let the detail page draw the uncertainty map itself.
        pts = ephemeris.offsets(e.offsets_url)
        # One fetch of the astrometry serves both the already-observed check
        # and the discovering observatory.
        obs = ephemeris.observations(e.observations_url) or {}
        return t["desig"], (ephemeris.signature(t), {
            "lines": [r.line for r in e.rows],
            # When this was fetched, and how far forward it reaches. Together
            # these are what let is_stale() notice an ephemeris that has run
            # out of night without waiting for new astrometry to arrive.
            "fetched_ts": now,
            "last_row_ts": max((r.ts for r in e.rows), default=None),
            "offsets_url": e.offsets_url,
            "map_url": e.map_url,
            "observations_url": e.observations_url,
            "offsets": pts,
            "scatteredness": ephemeris.spread(pts),
            "observed_from_site": obs.get("observed_from_site"),
            "discovery_code": obs.get("discovery_code"),
            "obs_codes": obs.get("codes"),
            "error": e.error,
        })

    new_cache = {}
    if stale:
        with ThreadPoolExecutor(max_workers=config.EPHEMERIS_WORKERS) as pool:
            for res in pool.map(_aux, stale):
                if res:
                    new_cache[res[0]] = res[1]
    mark("fetch_aux")

    if new_cache:
        db.save_cache(conn, new_cache)
    cache.update(new_cache)

    for t in targets:
        if t["cheap_reject"]:
            t.update(discard_reasons=t["cheap_reject"], observable=False,
                     max_alt=None, max_alt_ts=None, max_alt_az=None,
                     mask_flags=[], exposure_min=None,
                     window_minutes=0.0, eph_rows_total=0, eph_rows_usable=0,
                     eph_error=None, eph_report={}, map_url=None,
                     offsets_url=None, scatteredness=None,
                     observed_from_site=None, max_alt_row=None,
                     nearest_row=None, interp_row=None, mpc_flag=None,
                     discovery_code=None, obs_codes=None)
            continue

        entry = cache.get(t["desig"])
        payload = entry[1] if entry else {}
        eph = ephemeris.from_lines(
            t["desig"], payload.get("lines", []), payload.get("offsets_url"),
            payload.get("map_url"), payload.get("observations_url"))
        eph.error = payload.get("error")
        sc = payload.get("scatteredness")
        t["scatteredness"] = tuple(sc) if sc else None
        t["observed_from_site"] = payload.get("observed_from_site")
        t["discovery_code"] = payload.get("discovery_code")
        t["obs_codes"] = payload.get("obs_codes")
        pipeline.analyze(t, eph, orbits.get(t["desig"]), now=now)
    mark("analyze")

    pipeline.crosscheck_all(targets, now + config.INTERPOLATE_AHEAD_S)
    mark("crosscheck")

    ranking.rank(targets)
    ordered = ranking.sort_targets(targets)
    for t in ordered:
        t["max_alt_utc"] = (time.strftime("%Y-%m-%d %H:%M",
                                          time.gmtime(t["max_alt_ts"]))
                            if t.get("max_alt_ts") else None)
        row = t.get("interp_row") or t.get("nearest_row")
        t["cur_alt"] = row.alt if row else None
        t["cur_az"] = row.az if row else None
        t["cur_motion"] = row.motion if row else None
        t["cur_moon_dist"] = row.moon_dist if row else None
        t["cur_sun_alt"] = row.sun_alt if row else None
        t["cur_vmag"] = row.vmag if row else None
        t["cur_ts"] = row.ts if row else None
        t["plan_block"] = output.plan_entry(t) if t.get("observable") else None
    mark("rank")

    night = pipeline.night_label(now)
    plan_path = None
    if config.WRITE_NIGHTLY_PLAN:
        plan_path = output.write_plan(ordered, night)
    mark("plan")

    n = db.replace_targets(conn, ordered)
    db.prune_cache(conn, [t["desig"] for t in targets])
    mark("database")

    n_obs = sum(1 for t in ordered if t["observable"])
    mismatches = sum(1 for t in ordered
                     if t.get("crosscheck") and not t["crosscheck"].get("ok"))
    timings["total"] = round(sum(timings.values()), 1)

    db.set_meta(conn, "last_update_utc", db.utcnow())
    db.set_meta(conn, "last_update_count", n)
    db.set_meta(conn, "last_update_observable", n_obs)
    db.set_meta(conn, "last_update_timings", json.dumps(timings))
    db.set_meta(conn, "last_update_ok", "1")
    db.set_meta(conn, "night", night)
    db.set_meta(conn, "crosscheck_mismatches", mismatches)
    if plan_path:
        db.set_meta(conn, "plan_path", plan_path)

    return timings, n, n_obs, len(stale), mismatches


def main():
    ap = argparse.ArgumentParser(description="Update the NEOCP target database")
    ap.add_argument("--source", help="read NEOCP text from a local file")
    ap.add_argument("--loop", action="store_true",
                    help=f"run forever, every {config.UPDATE_INTERVAL_S}s")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    conn = db.connect()
    db.init(conn)

    while True:
        try:
            timings, n, n_obs, n_fetched, mism = run_update(conn, args.source)
            logging.info(
                "%d targets, %d observable, %d ephemerides fetched, "
                "%d crosscheck mismatches, %.1f s "
                "(list %.0f, orbits %.0f, eph %.0f, aux %.0f, analyze %.0f, "
                "xcheck %.0f, rank %.0f, plan %.0f, db %.0f ms)",
                n, n_obs, n_fetched, mism, timings["total"] / 1000,
                timings["fetch_list"], timings["fetch_orbits"],
                timings["fetch_ephemerides"], timings["fetch_aux"],
                timings["analyze"], timings["crosscheck"], timings["rank"],
                timings["plan"], timings["database"])
        except Exception as e:
            logging.exception("update failed: %s", e)
            db.set_meta(conn, "last_update_ok", "0")
            db.set_meta(conn, "last_error", str(e))
            if not args.loop:
                return 1

        if not args.loop:
            return 0
        time.sleep(config.UPDATE_INTERVAL_S)


if __name__ == "__main__":
    sys.exit(main())
