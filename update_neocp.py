"""NEOCP update cycle. Run every 5 minutes; the website only reads the DB.

All astronomy, filtering and ordering happens here precisely so that page
loads do no computation and stay fast.

Network cost is kept down two ways: objects rejected by the cheap NEOCP-level
filters never trigger a per-object request at all, and ephemerides are cached
against a signature of the object's NEOCP row so they are re-requested only
when new observations change the solution.

Each cycle's freshly fetched, freshly parsed NEOCP list is also handed
straight to neocp_history.record_cycle() to build the longitudinal history
database (see that module) -- rather than neocp_history.py fetching an
identical copy of the same list again on its own separate schedule, this is
now the only place that list gets requested. Run neocp_history.py's own
--loop only as a standalone fallback, never alongside this one.
"""

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import config
import db
import ds42score
import ephemeris
import neocp
import neocp_history
import output
import pipeline
import ranking
import siteconf


def setup_logging(verbose=False):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.FileHandler(config.LOG_PATH),
                  logging.StreamHandler(sys.stdout)])


def cheap_reject(t, site=None):
    """Filters needing no network. Ordered cheapest first."""
    s = config.DEFAULT_SITE if site is None else site
    out = []
    if t["score"] < s.min_score:
        out.append("LOW_SCORE")
    if t["arc_days"] < s.min_arc_days:
        out.append("SHORT_ARC")
    if t["not_seen_days"] > s.max_not_seen_days:
        out.append("NOT_SEEN")
    if t["desig"] in s.blacklist:
        out.append("BLACKLISTED")
    if s.neo_only and t.get("q") is not None and t.get("e") is not None:
        if not (t["q"] < s.neo_q_max or t["e"] > s.neo_e_min):
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
    # A parser fix changes what the same page yields, and the signature cannot
    # see that -- it tracks the object's astrometry, not our bugs. The schema
    # version does, so a bump refetches everything once.
    if payload.get("cache_schema") != ephemeris.CACHE_SCHEMA:
        return True

    last = payload.get("last_row_ts")
    if last is None or last >= now:
        return False
    return now - (payload.get("fetched_ts") or 0) > \
        config.EPHEMERIS_REFETCH_BACKOFF_S


def _score_new_objects(conn, cache):
    """Score, with ds42, every cached object that has astrometry and no score.

    One subprocess for the whole batch, never one per object: the model load
    is 1.5 s of the ~4.8 s it takes to score a full night.

    Entirely best effort. Wrapped so that nothing here -- a missing install, a
    broken model, a timeout -- can fail an update cycle whose actual job is
    telling an observer where to point a telescope.
    """
    if not ds42score.available():
        return
    try:
        with_records = {d: (payload.get("obs_records") or [])
                        for d, (_sig, payload) in cache.items()
                        if payload.get("obs_records")}
        todo = db.unscored_desigs(conn, list(with_records))
        if not todo:
            return
        batch = {d: with_records[d] for d in todo}
        scores = ds42score.score_records(batch)
        if not scores:
            return
        written = db.save_ds42_scores(conn, scores, ds42score.provenance())
        ok = sum(1 for s in scores.values() if s.get("status") == "ok")
        logging.info("ds42 scored %d new object(s), %d ok, %d stored",
                     len(scores), ok, written)
    except Exception:
        logging.exception("ds42 scoring step failed; continuing")


def _backfill_history_once(conn, hist_conn):
    """Bring objects ds42 has already scored into the history, once.

    ds42 began scoring before this history existed, and MPC's archive still
    lists outcomes going back weeks -- so those labels can be recovered
    instead of waited for. Guarded by a meta flag rather than a check for
    emptiness: the history legitimately becomes non-empty on the first normal
    cycle, and re-running the backfill every cycle would mean re-fetching the
    archive forever.
    """
    if hist_conn is None or db.get_meta(conn, "ds42_history_backfilled"):
        return
    try:
        scored = [r[0] for r in conn.execute("SELECT desig FROM ds42_scores")]
        if not scored:
            return
        inserted, resolved = neocp_history.backfill_from_scores(hist_conn,
                                                                scored)
        db.set_meta(conn, "ds42_history_backfilled", db.utcnow())
        logging.info("history backfill: %d object(s) added, %d resolved",
                     inserted, resolved)
    except Exception:
        # Left unflagged so the next cycle retries -- this is a one-off whose
        # only cost is a single archive fetch.
        logging.exception("history backfill failed; will retry next cycle")


def run_update(conn, hist_conn=None, source=None, sites=None):
    """One cycle: the work that is about the objects once, then each site's.

    The NEOCP list, its orbital parameters, the ds42 scores and the
    longitudinal history are all properties of the objects themselves, so
    they are fetched, parsed and recorded ONCE however many observatories are
    being served. Only the ephemeris is genuinely per-observatory -- MPC
    computes it for one observatory code -- along with the analysis that
    follows from it.

    Getting that division wrong would not merely be wasteful. Recording the
    history inside the per-site loop would write N identical snapshots per
    poll and make the research record a function of how many observatories
    happened to sign up; see multisite.md.
    """
    sites = list(config.SITES.values()) if sites is None else list(sites)
    # Settings edited through the web are overrides layered over each site's
    # file, so they have to be applied here too. Without this the board would
    # show a changed limit while the cycle that decides what reaches the board
    # kept using the old one -- the worst of both, and invisible.
    sites = [siteconf.effective(conn, s) for s in sites]
    shared = {}
    t0 = time.perf_counter()

    def mark(stage):
        nonlocal t0
        shared[stage] = round((time.perf_counter() - t0) * 1000, 1)
        t0 = time.perf_counter()

    raw = open(source).read() if source else neocp.fetch_neocp()
    base = neocp.parse_neocp(raw)
    if not base:
        raise RuntimeError("NEOCP returned no parseable targets")
    mark("fetch_list")

    # Same parsed list every site below uses -- not a `source` test fixture,
    # and only when the caller actually wants history recorded (see main()'s
    # --no-history) -- fed straight to neocp_history's own database instead
    # of it fetching an identical copy of this list itself.
    if hist_conn is not None and source is None:
        neocp_history.record_cycle(hist_conn, base)
    mark("history")

    try:
        orbits = neocp.parse_neocp_info(neocp.fetch_neocp_info())
    except Exception as e:
        logging.warning("neocp_info unavailable (%s); q/e filters inactive", e)
        orbits = {}
    mark("fetch_orbits")

    for t in base:
        o = orbits.get(t["desig"]) or {}
        t["q"], t["e"], t["incl"] = o.get("q"), o.get("e"), o.get("incl")

    # One instant for the whole cycle, so two observatories analysing the
    # same object are answering the same question rather than questions a
    # few seconds apart.
    now = time.time()

    results, everything_cached = [], {}
    for site in sites:
        # One observatory's failure must not take the others down with it.
        # Without this, a partner site whose ephemeris fetch times out ends
        # the cycle, and every other board -- including the one this
        # deployment was built for -- silently stops updating behind it.
        try:
            result, cache = _run_site(conn, site, base, orbits, now)
        except Exception as e:
            logging.exception("site %s failed; other sites continue",
                              site.obscode)
            db.set_meta(conn, "last_update_ok", "0", site=site)
            db.set_meta(conn, "last_error", str(e), site=site)
            continue
        results.append(result)
        # The 80-column astrometry is the object's own and identical whoever
        # fetched it -- but an object cheap-rejected at one site is only in
        # another's cache, so scoring reads the union rather than any one.
        for desig, entry in cache.items():
            everything_cached.setdefault(desig, entry)

    t0 = time.perf_counter()
    _score_new_objects(conn, everything_cached)
    # After scoring, so the first run already has this cycle's new scores to
    # backfill rather than leaving them for the next one.
    _backfill_history_once(conn, hist_conn)
    mark("ds42")

    shared["total"] = round(
        sum(shared.values()) + sum(r["timings"]["total"] for r in results), 1)
    return shared, results


def _run_site(conn, site, base, orbits, now):
    """One observatory's half of a cycle. Returns (result, its cache).

    Each site works on its OWN copy of the target dicts: analyze() annotates
    them in place with altitudes, windows, exposures and discard reasons,
    every one of which is a statement about this site's sky rather than about
    the object. Sharing one list between sites would have the last
    observatory in the loop overwrite every earlier one's board.
    """
    timings = {}
    t0 = time.perf_counter()

    def mark(stage):
        nonlocal t0
        timings[stage] = round((time.perf_counter() - t0) * 1000, 1)
        t0 = time.perf_counter()

    targets = [dict(t) for t in base]

    # Archive the outgoing night the moment this site's label rolls over,
    # before anything below overwrites it. targets/observer_state/
    # ephemeris_cache still hold last cycle's (i.e. the night that just
    # ended) state at this point -- this cycle's replace_targets/prune_cache
    # haven't run yet, and after they do there is no other record of where a
    # resolved-and-removed object actually was.
    #
    # Per site because the label is: a site four timezones away rolls over at
    # a different moment, and reading a shared "night" would archive one
    # observatory's evening into another's morning.
    outgoing_night = db.get_meta(conn, "night", site=site)
    incoming_night = pipeline.night_label(time.time(), site)
    if outgoing_night and outgoing_night != incoming_night:
        try:
            db.archive_night(conn, outgoing_night, site)
        except Exception:
            # Logged and swallowed: a failed archive costs a night of replay,
            # and taking the updater down over it would cost the board. But it
            # is silent by the same token, so the first real rollover after
            # this ships is worth watching in the log rather than assuming.
            logging.exception("failed to archive night %s for %s",
                              outgoing_night, site.obscode)
    # Billed as its own stage rather than folded into fetch_list: archiving
    # serialises every observable target's whole track, and hiding that inside
    # a network timing is how a slow one becomes impossible to spot.
    mark("archive")

    for t in targets:
        t["cheap_reject"] = cheap_reject(t, site)

    cache = db.load_cache(conn, site)
    candidates = [t for t in targets if not t["cheap_reject"]]

    stale = [t for t in candidates if needs_refetch(cache.get(t["desig"]), t, now)]
    fresh = ephemeris.fetch_many((t["desig"] for t in stale), site=site)
    mark("fetch_ephemerides")

    # Auxiliary pages, only for objects we just refetched. Three more
    # requests each, so they run through the same bounded pool -- done
    # sequentially a cold start spends over a minute here.
    def _aux(t):
        e = fresh.get(t["desig"])
        if e is None:
            return None
        # One fetch serves both: the spread feeds the filter cascade, the
        # points let the detail page draw the uncertainty map itself.
        pts = ephemeris.offsets(e.offsets_url)
        # One fetch of the astrometry serves the already-observed check, the
        # discovering observatory, and -- since the records were being parsed
        # and thrown away anyway -- the records themselves, which are what
        # ds42 scores and the only copy we will ever have of them.
        obs = ephemeris.observations(e.observations_url, site=site) or {}
        # No altitude floor, scoped only to filling holes in the altitude
        # plot -- see fetch_gap_fill's own docstring for why this is safe
        # to loosen here without touching the normal fetch above.
        gap_fill = ephemeris.fetch_gap_fill(t["desig"], site=site)
        return t["desig"], (ephemeris.signature(t), {
            "lines": [r.line for r in e.rows],
            "gap_fill_lines": [r.line for r in gap_fill.rows],
            # When this was fetched, and how far forward it reaches. Together
            # these are what let is_stale() notice an ephemeris that has run
            # out of night without waiting for new astrometry to arrive.
            "fetched_ts": now,
            "last_row_ts": max((r.ts for r in e.rows), default=None),
            "cache_schema": ephemeris.CACHE_SCHEMA,
            "offsets_url": e.offsets_url,
            "map_url": e.map_url,
            "observations_url": e.observations_url,
            "offsets": pts,
            "scatteredness": ephemeris.spread(pts),
            "observed_from_site": obs.get("observed_from_site"),
            "discovery_code": obs.get("discovery_code"),
            "obs_codes": obs.get("codes"),
            # The raw 80-column astrometry. Kept because it is unrecoverable
            # once MPC drops the object from NEOCP, and because it is ds42's
            # input. ~0.8 KB per object, ~62 KB a night. See ds42.md.
            "obs_records": obs.get("records"),
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
        db.save_cache(conn, new_cache, site)
    cache.update(new_cache)

    for t in targets:
        if t["cheap_reject"]:
            t.update(discard_reasons=t["cheap_reject"], observable=False,
                     max_alt=None, max_alt_ts=None, max_alt_az=None,
                     mask_flags=[], exposure_min=None, live_row_is_now=False,
                     frames=None, frame_sec=None, frame_motion=None,
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
        pipeline.analyze(t, eph, orbits.get(t["desig"]), now=now, site=site)
    mark("analyze")

    pipeline.crosscheck_all(targets, now + site.interpolate_ahead_s, site)
    mark("crosscheck")

    ranking.rank(targets, site)
    ordered = ranking.sort_targets(targets, site=site)
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

    night = pipeline.night_label(now, site)
    plan_path = None
    if config.WRITE_NIGHTLY_PLAN:
        plan_path = output.write_plan(ordered, night, site=site)
    mark("plan")

    n = db.replace_targets(conn, ordered, site)
    db.prune_cache(conn, [t["desig"] for t in targets], site)
    mark("database")

    n_obs = sum(1 for t in ordered if t["observable"])
    mismatches = sum(1 for t in ordered
                     if t.get("crosscheck") and not t["crosscheck"].get("ok"))
    timings["total"] = round(sum(timings.values()), 1)

    db.set_meta(conn, "last_update_utc", db.utcnow(), site=site)
    db.set_meta(conn, "last_update_count", n, site=site)
    db.set_meta(conn, "last_update_observable", n_obs, site=site)
    db.set_meta(conn, "last_update_timings", json.dumps(timings), site=site)
    db.set_meta(conn, "last_update_ok", "1", site=site)
    db.set_meta(conn, "night", night, site=site)
    db.set_meta(conn, "crosscheck_mismatches", mismatches, site=site)
    if plan_path:
        db.set_meta(conn, "plan_path", plan_path, site=site)

    return ({"site": site, "timings": timings, "n": n, "n_obs": n_obs,
             "n_fetched": len(stale), "mismatches": mismatches}, cache)


def main():
    ap = argparse.ArgumentParser(description="Update the NEOCP target database")
    ap.add_argument("--source", help="read NEOCP text from a local file")
    ap.add_argument("--loop", action="store_true",
                    help=f"run forever, every {config.UPDATE_INTERVAL_S}s")
    ap.add_argument("--no-history", action="store_true",
                    help="don't also record this cycle's list into "
                         "neocp_history.py's database")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    conn = db.connect()
    db.init(conn)
    hist_conn = None if args.no_history else neocp_history.ensure_db()

    while True:
        try:
            shared, results = run_update(conn, hist_conn, args.source)
            logging.info(
                "shared %.1f s over %d site(s) "
                "(list %.0f, history %.0f, orbits %.0f, ds42 %.0f ms)",
                shared["total"] / 1000, len(results),
                shared["fetch_list"], shared["history"],
                shared["fetch_orbits"], shared["ds42"])
            for r in results:
                t = r["timings"]
                logging.info(
                    "%s: %d targets, %d observable, %d ephemerides fetched, "
                    "%d crosscheck mismatches, %.1f s "
                    "(archive %.0f, eph %.0f, aux %.0f, analyze %.0f, "
                    "xcheck %.0f, rank %.0f, plan %.0f, db %.0f ms)",
                    r["site"].obscode, r["n"], r["n_obs"], r["n_fetched"],
                    r["mismatches"], t["total"] / 1000,
                    t["archive"], t["fetch_ephemerides"], t["fetch_aux"],
                    t["analyze"], t["crosscheck"], t["rank"], t["plan"],
                    t["database"])
        except Exception as e:
            # A failure out here is shared -- NEOCP itself, or the parse --
            # so it is every site's failure, not one site's. A single site's
            # own failure is caught inside run_update and recorded against
            # that site alone.
            logging.exception("update failed: %s", e)
            for site in config.SITES.values():
                db.set_meta(conn, "last_update_ok", "0", site=site)
                db.set_meta(conn, "last_error", str(e), site=site)
            if not args.loop:
                return 1

        if not args.loop:
            return 0
        time.sleep(config.UPDATE_INTERVAL_S)


if __name__ == "__main__":
    sys.exit(main())
