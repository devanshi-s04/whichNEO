"""Verification checks for the parsers and the astronomy.

Two of these guard against mistakes that would otherwise be invisible:

  test_analytic_matches_astropy  -- the 24h window scan uses a fast analytic
      alt/az path while instantaneous values use astropy's full transform.
      If they diverge, observing windows are silently wrong.

  test_ephemeris_azimuth_convention -- MPC reports azimuth from south, not
      north. Getting this wrong rotates every target 180 degrees through the
      dome mask, so blocked northern sky reads as open southern sky.

Run: python3 selftest.py
"""

import importlib
import os
import re
import sys
import time

import numpy as np
import astropy.units as u
from astropy.time import Time
from astropy.coordinates import SkyCoord, AltAz, TETE, get_body

import config
import db
import ephemeris
import neocp
import observability
import output
import pipeline
import ranking
import moonplot
import skymap
import update_neocp

FAILURES = []

# A real ephemeris row, taken verbatim from the MPC CGI.
SAMPLE_EPH = ("2026 09 10 2000   02 30 34.7 +36 47 19 118.3  16.4   391.9  "
              "047.2  240  +23   -26    0.00  115  -28   Map/Offsets   !!")


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def test_site():
    loc = observability.site()
    check("site latitude ~45.2909 N", abs(loc.lat.deg - 45.2909) < 0.001,
          f"got {loc.lat.deg:.4f}")
    check("site longitude ~13.7493 E", abs(loc.lon.deg - 13.74930) < 0.001,
          f"got {loc.lon.deg:.5f}")
    check("site height ~381 m", abs(loc.height.to(u.m).value - 381) < 15,
          f"got {loc.height.to(u.m).value:.0f}")


def test_horizon_mask():
    for az, expected in [(0, np.inf), (10, np.inf), (350, np.inf), (45, 20.0),
                         (90, 20.0), (180, 20.0), (225, 30.0), (270, 40.0),
                         (315, 40.0)]:
        got = float(observability.min_altitude_for_azimuth(az))
        ok = (np.isinf(got) and np.isinf(expected)) or got == expected
        check(f"mask az={az:3.0f} -> {expected}", ok, f"got {got}")


def test_analytic_matches_astropy():
    loc = observability.site()
    t = Time("2026-09-08 22:48:00")
    lst = t.sidereal_time("apparent", longitude=loc.lon).deg
    rng = np.random.default_rng(0)
    ras, decs = rng.uniform(0, 360, 40), rng.uniform(-30, 80, 40)

    coords = SkyCoord(ra=ras * u.deg, dec=decs * u.deg)
    aa = coords.transform_to(AltAz(obstime=t, location=loc))
    app = coords.transform_to(TETE(obstime=t))
    ha = ((lst - app.ra.deg + 180.0) % 360.0) - 180.0
    alt_a, az_a = observability._altaz_from_hour_angle(ha, app.dec.deg, loc.lat.deg)

    d_alt = np.abs(alt_a - aa.alt.deg).max()
    high = aa.alt.deg < 88
    d_az = np.abs(((az_a - aa.az.deg + 180) % 360) - 180)[high].max()
    check("analytic vs astropy altitude within 0.5 deg", d_alt < 0.5,
          f"max diff {d_alt:.3f}")
    check("analytic vs astropy azimuth within 0.5 deg", d_az < 0.5,
          f"max diff {d_az:.3f}")


def test_neocp_list_parse():
    line = ("TR0006  100 2026 09 04.9  21.8863 -14.6396 17.3 "
            "Added Sept. 8.89 UT              3   0.01 18.5  3.975")
    rows = neocp.parse_neocp(line)
    check("parses one NEOCP line", len(rows) == 1)
    if rows:
        r = rows[0]
        check("designation", r["desig"] == "TR0006", r["desig"])
        check("digest2 score", r["score"] == 100, r["score"])
        check("RA -> degrees", abs(r["ra_deg"] - 328.2945) < 1e-6, r["ra_deg"])
        check("V magnitude", r["vmag"] == 17.3, r["vmag"])
        check("not-seen days", r["not_seen_days"] == 3.975, r["not_seen_days"])


def test_neocp_info_column_collision():
    """e and a run together when a is large; both carry three decimals.

    The legacy planner splits on whitespace and requires 13 fields, so it
    silently drops exactly these rows -- the most extreme orbits on the page.
    """
    normal = "6J93321    23.5  19 -40 18.9 119  0.14  0.5  11.2 0.316   1.093  12/12  0.27"
    merged = "A11GrOJ    10.1   1 -32 19.7 142  0.03  1.7 126.0 0.9982411.744   4/4   0.37"

    a = neocp.parse_neocp_info(normal)
    check("normal row parses", "6J93321" in a)
    if a:
        v = a["6J93321"]
        check("  e", abs(v["e"] - 0.316) < 1e-9, v["e"])
        check("  a", abs(v["a"] - 1.093) < 1e-9, v["a"])
        check("  q = a(1-e)", abs(v["q"] - 1.093 * (1 - 0.316)) < 1e-9, v["q"])

    b = neocp.parse_neocp_info(merged)
    check("collided row still parses", "A11GrOJ" in b,
          "this is the row the legacy parser drops")
    if b:
        v = b["A11GrOJ"]
        check("  e recovered as 0.998", abs(v["e"] - 0.998) < 1e-9, v["e"])
        check("  a recovered as 2411.744", abs(v["a"] - 2411.744) < 1e-9, v["a"])

    check("naive whitespace split would have dropped it",
          len(merged.split()) != 13, f"{len(merged.split())} fields")


def test_ephemeris_row():
    r = ephemeris.Row(SAMPLE_EPH)
    check("altitude", r.alt == 23.0, r.alt)
    check("sun altitude", r.sun_alt == -26.0, r.sun_alt)
    check("motion", r.motion == 391.9, r.motion)
    check("V magnitude", r.vmag == 16.4, r.vmag)
    check("moon distance", r.moon_dist == 115.0, r.moon_dist)
    check("elongation", r.elong == 118.3, r.elong)
    check("MPC flag captured", r.flag == "!!", r.flag)
    check("RA -> degrees", abs(r.ra_deg - (2 + 30 / 60 + 34.7 / 3600) * 15) < 1e-6,
          r.ra_deg)
    check("Dec -> degrees", abs(r.dec_deg - (36 + 47 / 60 + 19 / 3600)) < 1e-6,
          r.dec_deg)


def test_ephemeris_azimuth_convention():
    """MPC measures azimuth from south; we store compass bearings."""
    r = ephemeris.Row(SAMPLE_EPH)
    check("raw MPC azimuth preserved", r.az_mpc == 240.0, r.az_mpc)
    check("converted to compass bearing", r.az == 60.0, r.az)
    check("plan file writes MPC's convention back out",
          " 240 " in output.ephemeris_line(r).replace("  ", " "),
          output.ephemeris_line(r))


def test_exposure_rule():
    """minutes = 10 + (V - 18) * 5, floored."""
    for vmag, expected in [(18.0, 10.0), (20.0, 20.0), (21.6, 28.0), (19.0, 15.0)]:
        r = ephemeris.Row(SAMPLE_EPH)
        r.vmag = vmag
        got = r.exposure_minutes()
        check(f"V={vmag} -> {expected} min", abs(got - expected) < 1e-9, got)
    r = ephemeris.Row(SAMPLE_EPH)
    r.vmag = 12.5  # would be -17.5 minutes unclamped
    check("bright target clamped to the floor, not negative",
          r.exposure_minutes() == config.EXPOSURE_FLOOR_MIN, r.exposure_minutes())


def test_night_bounds():
    """A night runs 11:00 UT to 11:00 UT, so evening and the small hours of
    the next morning belong to the same night."""
    evening = calendar_utc("2026-09-10 22:00")
    after_midnight = calendar_utc("2026-09-11 02:00")
    morning = calendar_utc("2026-09-11 12:00")

    check("evening and after-midnight share a night",
          pipeline.night_label(evening) == pipeline.night_label(after_midnight),
          f"{pipeline.night_label(evening)} vs {pipeline.night_label(after_midnight)}")
    check("night is labelled by its evening date",
          pipeline.night_label(evening) == "2026-09-10",
          pipeline.night_label(evening))
    check("after the 11:00 rollover a new night starts",
          pipeline.night_label(morning) == "2026-09-11",
          pipeline.night_label(morning))


def calendar_utc(s):
    import calendar as _c
    import datetime as _dt
    return _c.timegm(_dt.datetime.strptime(s, "%Y-%m-%d %H:%M").timetuple())


def test_chronological_sort():
    rows = [
        dict(desig="late", observable=True, max_alt_ts=300, score_total=9.0),
        dict(desig="early", observable=True, max_alt_ts=100, score_total=1.0),
        dict(desig="down", observable=False, max_alt_ts=50, score_total=9.9),
    ]
    order = [r["desig"] for r in ranking.sort_targets(rows, "chronological")]
    check("earliest peak first, unobservable last",
          order == ["early", "late", "down"], order)
    order = [r["desig"] for r in ranking.sort_targets(rows, "score")]
    check("score mode ranks by value instead",
          order == ["late", "early", "down"], order)


def test_upcoming_cards_look_forward():
    """The 'up next' cards must not advertise a peak that already happened.

    Sorting by peak time is right for the night's plan, but midway through
    the night the earliest-peaking targets have long since peaked.
    """
    import app
    now = time.time()
    rows = [
        dict(desig="peaked_early", observable=True, max_alt_ts=now - 4 * 3600),
        dict(desig="peaked_recently", observable=True, max_alt_ts=now - 600),
        dict(desig="soon", observable=True, max_alt_ts=now + 1800),
        dict(desig="later", observable=True, max_alt_ts=now + 7200),
        dict(desig="not_up", observable=False, max_alt_ts=now + 60),
    ]
    picked = [r["desig"] for r in app.pick_upcoming(rows, n=3)]
    check("still-to-peak targets come first",
          picked[:2] == ["soon", "later"], picked)
    check("unobservable target never appears in the cards",
          "not_up" not in picked, picked)
    check("past-peak targets are flagged, not silently shown as upcoming",
          all(r.get("past_peak") for r in rows if r["max_alt_ts"] < now
              and r["desig"] in picked) or "peaked_early" not in picked[:2],
          picked)

    # Late in the night nothing is still to peak; the cards should fall back
    # rather than go empty.
    late = [dict(desig="a", observable=True, max_alt_ts=now - 3600),
            dict(desig="b", observable=True, max_alt_ts=now - 1800)]
    got = app.pick_upcoming(late, n=3)
    check("falls back to past-peak targets when none remain ahead",
          len(got) == 2 and all(r.get("past_peak") for r in got),
          [r["desig"] for r in got])


def test_cache_signature_ignores_the_clock():
    """The ephemeris cache key must not move on its own.

    not_seen_days is the age of the last observation, so it advances with the
    wall clock. Including it invalidated every cached ephemeris on every
    cycle -- the cache silently did nothing and updates took 80 s instead of
    2, which looked fine from outside for two days.
    """
    base = dict(nobs=4, arc_days=0.04, not_seen_days=0.081)
    later = dict(nobs=4, arc_days=0.04, not_seen_days=0.084)   # only time passed
    check("signature is stable as not_seen_days ticks up",
          ephemeris.signature(base) == ephemeris.signature(later),
          f"{ephemeris.signature(base)} vs {ephemeris.signature(later)}")

    more_obs = dict(nobs=5, arc_days=0.04, not_seen_days=0.081)
    check("signature changes when new observations arrive",
          ephemeris.signature(base) != ephemeris.signature(more_obs))
    longer_arc = dict(nobs=4, arc_days=0.09, not_seen_days=0.081)
    check("signature changes when the arc extends",
          ephemeris.signature(base) != ephemeris.signature(longer_arc))


def test_plan_file_survives_dawn():
    """An empty plan must not overwrite a night's record.

    Nothing is observable after dawn, but the night label does not roll over
    until 11:00 UT, so every cycle in between rewrites that night's file.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        full = [dict(desig="T1", observable=True, score=90, nobs=5,
                     arc_days=0.5, not_seen_days=0.1, vmag=20.0,
                     exposure_min=20.0, q=0.9, e=0.6, sun_elong_deg=120.0,
                     scatteredness=(4, 5), map_url=None, max_alt_row=None,
                     nearest_row=None, interp_row=None)]
        p = output.write_plan(full, "2026-09-11", d)
        size_with_content = os.path.getsize(p)
        check("a plan with targets is written", size_with_content > len(output.HEADER))

        # Dawn: same night label, nothing observable any more.
        output.write_plan([dict(desig="T1", observable=False)], "2026-09-11", d)
        check("an empty plan does not wipe the night's record",
              os.path.getsize(p) == size_with_content,
              f"{os.path.getsize(p)} vs {size_with_content}")

        # A genuinely empty night should still produce a file.
        p2 = output.write_plan([dict(desig="X", observable=False)], "2026-09-12", d)
        check("a night with no targets still writes a file",
              os.path.exists(p2))


def test_mpc_markers():
    """! / !! are fast-motion markers from the ephemeris; S / B are warnings
    from the NEOCP Note column. Neither may change the running order."""
    flagged = ("A11GMT0  98 2026 09 13.2  22.9499 -12.2595 18.4 "
               "Updated Sept. 13.51 UT     S     8   0.30 28.1  0.164")
    plain = ("P12pZCB  91 2026 09 13.5   1.8368  -9.6186 20.7 "
             "Updated Sept. 13.64 UT           4   0.04 20.2  0.095")

    a = neocp.parse_neocp(flagged)[0]
    b = neocp.parse_neocp(plain)[0]
    check("S flag extracted from the note column", a["note_flag"] == "S", a["note_flag"])
    check("no flag when the note column is empty", b["note_flag"] is None, b["note_flag"])
    check("the flag does not corrupt nobs", (a["nobs"], b["nobs"]) == (8, 4),
          (a["nobs"], b["nobs"]))
    check("arc still parsed correctly alongside a flag",
          abs(a["arc_days"] - 0.30) < 1e-9, a["arc_days"])

    # Object-level badge is the strongest marker over observable rows.
    def row(flag):
        r = ephemeris.Row(SAMPLE_EPH)
        r.flag = flag
        return r
    for rows, expected in ([[row(""), row("")], None],
                           [[row(""), row("!")], "!"],
                           [[row("!"), row("!!")], "!!"]):
        got = ("!!" if any(x.flag == "!!" for x in rows)
               else "!" if any(x.flag == "!" for x in rows) else None)
        check(f"rows {[x.flag for x in rows]} -> badge {expected!r}", got == expected, got)

    # Explicitly pinned: markers are informational until Luka has seen them.
    now = time.time()
    rows = [dict(desig="flagged_late", observable=True, max_alt_ts=now + 7200,
                 mpc_flag="!!", score_total=1.0),
            dict(desig="plain_early", observable=True, max_alt_ts=now + 600,
                 mpc_flag=None, score_total=1.0)]
    order = [r["desig"] for r in ranking.sort_targets(rows, "chronological")]
    check("a !! marker does not jump the chronological queue",
          order == ["plain_early", "flagged_late"], order)


def test_observatory_identification():
    """Real observatory codes out of the astrometry, and readable names for
    them from the vendored tables. No Find_Orb execution involved."""
    import observatories as O

    def rec(code, discovery=False):
        line = list(" " * 80)
        line[5:12] = "P22pP3n"
        line[12] = "*" if discovery else " "
        line[77:80] = code
        return "".join(line)

    text = "\n".join([rec("F51", discovery=True), rec("F51"), rec("F51"),
                      rec("474"), rec("474"), "short line, ignored"])
    got = ephemeris.parse_observations(text, "L01")
    check("counts records per observatory",
          got["codes"] == {"F51": 3, "474": 2}, got["codes"])
    check("finds the discovery site from the column-13 asterisk",
          got["discovery_code"] == "F51", got["discovery_code"])
    check("our own site is correctly absent",
          got["observed_from_site"] is False)
    check("our own site is detected when present",
          ephemeris.parse_observations(text + "\n" + rec("L01"),
                                       "L01")["observed_from_site"] is True)
    check("no asterisk falls back to the earliest record",
          ephemeris.parse_observations(rec("703"), "L01")["discovery_code"] == "703")
    check("empty astrometry yields nothing rather than a blank record",
          ephemeris.parse_observations("", "L01") is None)

    sites, details = O.count()
    check("vendored details parsed", details > 400, details)

    # ObsCodes has two layouts: parallax fields run together on some lines and
    # are space-padded on others. A regex tuned to the first silently dropped
    # 1418 of 2361 lines, and went unnoticed because the codes being tested
    # happened to use that form. Assert almost every line parses.
    raw = sum(1 for line in open(
        os.path.join(os.path.dirname(os.path.abspath(O.__file__)),
                     "mpcdata", "ObsCodes.htm"), encoding="utf-8",
        errors="replace") if re.match(r"^[A-Z0-9]{3}[ \d]", line))
    check("nearly every ObsCodes line parses",
          sites >= raw * 0.98, f"{sites} parsed of {raw} code lines")

    check("compressed-layout code resolves (F51)",
          "Pan-STARRS" in (O.site_name("F51") or ""), O.site_name("F51"))
    check("space-padded-layout code resolves (L51)",
          "Nauchnyi" in (O.site_name("L51") or ""), O.site_name("L51"))
    check("L01 resolves to Visnjan",
          "Visnjan" in (O.site_name("L01") or ""), O.site_name("L01"))
    check("L01 observers include Korlevic",
          "Korlevic" in (O.lookup("L01")["observers"] or ""),
          O.lookup("L01")["observers"])
    check("an automated survey has a telescope but no observers",
          O.lookup("G96")["telescope"] and not O.lookup("G96")["observers"],
          O.lookup("G96"))
    check("an unknown code degrades to nulls, not an exception",
          O.lookup("ZZZ")["name"] is None)

    check("the designation-prefix survey guess is gone",
          "survey" not in neocp.parse_neocp(
              "TR0006  100 2026 09 04.9  21.8863 -14.6396 17.3 "
              "Added Sept. 8.89 UT              3   0.01 18.5  3.975")[0])


def test_uncertainty_plot():
    """Coverage maths, and the scale decision that keeps the plot readable."""
    import uncertainty as U

    check("offsets parse out of the MPC page format",
          [(int(a), int(b)) for a, b in ephemeris._OFFSET_RE.findall(
              "      +0      +0      Ephemeris #    1\n"
              "      -4      +3      Ephemeris #    2\n")] == [(0, 0), (-4, 3)])

    tight = [(3, -2), (0, 0), (-5, 4), (10, -9)]
    check("a cloud inside the field is fully covered",
          U.coverage(tight, 2600) == 1.0, U.coverage(tight, 2600))
    check("extent is the half-width in each axis",
          U.extent(tight) == (10, 9), U.extent(tight))

    # Half these points sit outside a 2600" field.
    wide = [(0, 0), (5000, 0), (-5000, 0), (100, 100)]
    check("a cloud larger than the field is partly covered",
          abs(U.coverage(wide, 2600) - 0.5) < 1e-9, U.coverage(wide, 2600))

    check("spread matches MPC's scatteredness definition",
          ephemeris.spread(wide) == (10000, 100), ephemeris.spread(wide))

    # The field is drawn only when it would be visible beside the cloud.
    check("field box omitted when it dwarfs the cloud",
          "field 2600" not in U.render_svg(tight, 2600))
    check("field box drawn when the cloud overflows it",
          "field 2600" in U.render_svg(wide, 2600))

    svg = U.render_svg(tight, 2600)
    check("one circle per distinct position, plus the nominal marker",
          svg.count("<circle") == len(set(tight)) + 1, svg.count("<circle"))
    check("svg is well formed", svg.startswith("<svg") and svg.endswith("</svg>"))

    # MPC rounds offsets to whole arcseconds, so a small cloud collapses onto
    # a few grid points. Drawing one dot per variant stacks them invisibly and
    # looks far sparser than MPC's own picture.
    stacked = [(0, 0)] * 600 + [(0, 1)] * 300 + [(1, 0)] * 100
    check("duplicate positions collapse to one dot each",
          U.distinct(stacked) == 3, U.distinct(stacked))
    s2 = U.render_svg(stacked, 2600)
    check("1000 stacked variants draw 3 dots, not 1000",
          s2.count("<circle") == 4, s2.count("<circle"))

    radii = sorted(float(m) for m in re.findall(r'<circle[^>]*r="([\d.]+)"', s2)
                   if float(m) < 6.5)
    check("the busiest position is drawn largest",
          radii[-1] > radii[0], radii)
    check("each dot carries its share as a tooltip",
          s2.count("<title>") == 3, s2.count("<title>"))

    check("no points yields no plot rather than an empty frame",
          U.render_svg([]) is None and U.coverage([]) is None)


def test_auth_protects_state_changes():
    """Reads stay open; anything that changes state must be protected once
    credentials are configured. Without this, anyone who can reach the URL
    can mark targets observed or reorder the queue."""
    import importlib

    import app as appmod

    client = appmod.app.test_client()
    for path in ("/", "/status", "/rows"):
        check(f"{path} readable without credentials",
              client.get(path).status_code == 200)

    import auth
    prev_env = os.environ.get("WHICHNEO_AUTH")
    os.environ["WHICHNEO_AUTH"] = "obs:secret"
    try:
        importlib.reload(auth)
        importlib.reload(appmod)
        c = appmod.app.test_client()
        check("state change rejected without credentials",
              c.post("/mark/XYZ", data={"action": "hide"}).status_code == 401)
        check("state change rejected with the wrong password",
              c.post("/mark/XYZ", data={"action": "hide"},
                     headers={"Authorization": "Basic b2JzOndyb25n"}
                     ).status_code == 401)
        import base64
        good = base64.b64encode(b"obs:secret").decode()
        check("state change accepted with correct credentials",
              c.post("/mark/XYZ", data={"action": "hide"},
                     headers={"Authorization": f"Basic {good}"}
                     ).status_code in (200, 302))
        check("reads remain open even with auth enabled",
              c.get("/status").status_code == 200)
    finally:
        if prev_env is None:
            os.environ.pop("WHICHNEO_AUTH", None)
        else:
            os.environ["WHICHNEO_AUTH"] = prev_env
        importlib.reload(auth)
        importlib.reload(appmod)

    check("auth is off when no credentials are configured", not auth.ENABLED)


def test_schema_migration_from_older_db():
    """An existing database with an older `targets` must upgrade cleanly.

    The schema indexes columns an old table lacks, so creating it before
    dropping the stale table fails outright and the migration never runs --
    which is exactly what happened on the first real upgrade.
    """
    import sqlite3

    import db

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE targets (desig TEXT PRIMARY KEY, score INTEGER, vmag REAL);
        CREATE TABLE observer_state (desig TEXT PRIMARY KEY, observed INTEGER,
            observed_at_utc TEXT, hidden INTEGER, priority_bump REAL, note TEXT);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO targets VALUES ('OLD001', 90, 20.0);
        INSERT INTO observer_state (desig, observed) VALUES ('KEEPME', 1);
    """)
    c.commit()

    db.init(c)

    cols = {r[1] for r in c.execute("PRAGMA table_info(targets)")}
    check("stale targets table is rebuilt to the current schema",
          cols == set(db._COLS), f"{len(cols)} cols")
    kept = c.execute(
        "SELECT observed FROM observer_state WHERE desig='KEEPME'").fetchone()
    check("observer state survives the migration",
          kept is not None and kept["observed"] == 1, kept)
    check("the index is rebuilt", bool(c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='idx_targets_seq'").fetchone()))

    db.init(c)  # must be idempotent
    check("init is idempotent on an already-current database",
          {r[1] for r in c.execute("PRAGMA table_info(targets)")} == set(db._COLS))


def test_ranking_bounds():
    rows = [dict(score=100, arc_days=0.0, vmag=config.MAG_BRIGHT),
            dict(score=0, arc_days=99.0, vmag=config.MAX_MAG)]
    ranking.rank(rows)
    check("best possible target scores the maximum",
          abs(rows[0]["score_total"] - ranking.max_possible_score()) < 1e-9,
          rows[0]["score_total"])
    check("worst possible target scores zero",
          abs(rows[1]["score_total"]) < 1e-9, rows[1]["score_total"])


def test_row_rejection_reasons():
    far_future = 2 ** 40
    r = ephemeris.Row(SAMPLE_EPH)          # sun at -26, alt 23, motion 391.9
    check("clean row is accepted", pipeline.row_rejections(r, far_future) == [],
          pipeline.row_rejections(r, far_future))

    r2 = ephemeris.Row(SAMPLE_EPH)
    r2.sun_alt = 5.0
    check("daylight row rejected", "nearSun" in pipeline.row_rejections(r2, far_future))

    r3 = ephemeris.Row(SAMPLE_EPH)
    r3.motion = 0.1
    check("slow mover rejected", "tooSlow" in pipeline.row_rejections(r3, far_future))

    r4 = ephemeris.Row(SAMPLE_EPH)
    r4.moon_dist = 5.0
    check("row near the moon rejected",
          "nearMoon" in pipeline.row_rejections(r4, far_future))

    # The horizon mask must NOT delete anything while its sectors are soft.
    # An impactor discovered toward Trieste is still an impactor; the light
    # dome makes it a poor target, not an invisible one.
    r5 = ephemeris.Row(SAMPLE_EPH)
    r5.az, r5.alt = 0.0, 80.0              # due north, high
    check("north target survives the soft mask",
          pipeline.row_rejections(r5, far_future) == [],
          pipeline.row_rejections(r5, far_future))
    check("north target is flagged instead",
          any(f.startswith("azBlocked:") for f in pipeline.row_mask_flags(r5)),
          pipeline.row_mask_flags(r5))
    check("flag names the sector",
          pipeline.row_mask_flags(r5) == ["azBlocked:N"],
          pipeline.row_mask_flags(r5))

    r6 = ephemeris.Row(SAMPLE_EPH)
    # South-west, not west. The keep-out wedge now starts at 247.5, which is
    # the W sector's own western boundary, so the ENTIRE W sector below 70 deg
    # is refused outright and its soft 40-degree limit can never fire again.
    # Same for NW and N. SW is the nearest sector the advisory mask still
    # governs, so it is what exercises this behaviour now.
    r6.az, r6.alt = 225.0, 25.0            # south-west, under its 30 deg preference
    check("low south-western row kept but flagged",
          pipeline.row_rejections(r6, far_future) == []
          and pipeline.row_mask_flags(r6) == ["belowMask:SW"],
          (pipeline.row_rejections(r6, far_future), pipeline.row_mask_flags(r6)))

    # ...but a sector marked hard still refuses, which is the whole point of
    # keeping the distinction configurable.
    saved = config.HORIZON_MASK[:]
    try:
        config.HORIZON_MASK[0] = (337.5, 22.5, None, "hard")
        importlib.reload(observability)
        importlib.reload(pipeline)
        check("a sector marked hard does reject",
              "azBlocked" in pipeline.row_rejections(r5, far_future),
              pipeline.row_rejections(r5, far_future))
    finally:
        config.HORIZON_MASK[:] = saved
        importlib.reload(observability)
        importlib.reload(pipeline)
    check("mask restored to soft after the test",
          pipeline.row_rejections(r5, far_future) == [])


def test_skymap_orientation():
    """North up, east right, south down, west left.

    The ground truth is geographic: Trieste lies at bearing 3.0 deg true from
    L01, 41 km away, so it must land essentially at the top of the disc. Any
    mirrored or rotated projection fails this.
    """
    size, cx, cy, radius = 400, 200.0, 200.0, 180.0
    horizon = [("N", 0, cx, cy - radius), ("E", 90, cx + radius, cy),
               ("S", 180, cx, cy + radius), ("W", 270, cx - radius, cy)]
    for name, az, ex, ey in horizon:
        x, y = skymap.project(az, 0.0, cx, cy, radius)
        check(f"{name} (az {az}) projects to the expected edge",
              abs(x - ex) < 1e-6 and abs(y - ey) < 1e-6, f"got {x:.2f},{y:.2f}")

    tx, ty = skymap.project(3.0, 0.0, cx, cy, radius)
    check("Trieste at bearing 3.0 deg lands at the top of the disc",
          abs(tx - cx) < radius * 0.06 and ty < cy - radius * 0.99,
          f"got {tx:.1f},{ty:.1f} vs centre {cx},{cy}")

    zx, zy = skymap.project(123.0, 90.0, cx, cy, radius)
    check("zenith lands on the centre at any azimuth",
          abs(zx - cx) < 1e-9 and abs(zy - cy) < 1e-9, f"got {zx},{zy}")

    # Radius linear in altitude: 45 deg must sit exactly half way out.
    hx, hy = skymap.project(90.0, 45.0, cx, cy, radius)
    check("altitude 45 sits half way to the rim",
          abs((hx - cx) - radius / 2) < 1e-6, f"got {hx - cx:.3f}")

    # East on the RIGHT is the map convention, the mirror of a planisphere.
    ex, _ = skymap.project(90.0, 0.0, cx, cy, radius)
    wx, _ = skymap.project(270.0, 0.0, cx, cy, radius)
    check("east is right of west (map convention, not planisphere)",
          ex > cx > wx, f"east {ex:.1f}, west {wx:.1f}")


def test_skymap_mask_wedges():
    """Every sector wedge must cover the sector it claims, and no other."""
    svg = skymap.render_svg([], None, size=400)
    for name in config.SECTOR_NAMES:
        check(f"{name} wedge present in the map",
              f">{name} &mdash;" in svg or f">{name}</text>" in svg)

    idx = {n: i for i, n in enumerate(config.SECTOR_NAMES)}
    for name, az in [("N", 0), ("NE", 45), ("E", 90), ("SE", 135),
                     ("S", 180), ("SW", 225), ("W", 270), ("NW", 315)]:
        got = int(observability.sector_index(az))
        check(f"azimuth {az:3d} falls in sector {name}", got == idx[name],
              f"got {config.SECTOR_NAMES[got]}")

    # Sector boundaries: 337.5 is the first degree of N, 22.5 the first of NE.
    check("337.5 is the start of the north sector",
          int(observability.sector_index(337.5)) == 0)
    check("22.5 is the start of the north-east sector",
          int(observability.sector_index(22.5)) == 1)


def test_moon_exclusion_locus():
    """The lunar ring must be a true constant-separation locus.

    A fixed angular radius is not a fixed radius on this projection, so
    drawing a plain circle would be wrong away from the zenith. Every sampled
    point has to sit at exactly MOON_SEP_MIN from the moon.
    """
    for alt, az in [(70.0, 10.0), (35.0, 200.0), (5.0, 300.0)]:
        bearings = [360.0 * k / 72 for k in range(72)]
        alts, azs = observability.offset_position(
            alt, az, config.MOON_SEP_MIN, bearings)
        seps = observability.angular_separation(alt, az, np.array(alts),
                                                np.array(azs))
        worst = float(np.abs(seps - config.MOON_SEP_MIN).max())
        check(f"locus around alt={alt:.0f} is exactly "
              f"{config.MOON_SEP_MIN:.0f} deg wide", worst < 1e-8,
              f"worst error {worst:.2e} deg")

    # Radius is linear in zenith distance, so distance from the CENTRE is the
    # one thing the projection preserves. A locus around the zenith therefore
    # really is a circle, and the distortion grows toward the horizon -- which
    # is exactly where the moon usually sits when it matters.
    cx = cy = 200.0
    radius = 180.0
    bearings = [360.0 * k / 72 for k in range(72)]

    def projected_spread(moon_alt):
        alts, azs = observability.offset_position(
            moon_alt, 0.0, config.MOON_SEP_MIN, bearings)
        mx, my = skymap.project(0.0, moon_alt, cx, cy, radius)
        d = []
        for a_l, a_z in zip(list(alts), list(azs)):
            x, y = skymap.project(a_z, a_l, cx, cy, radius)
            d.append(((x - mx) ** 2 + (y - my) ** 2) ** 0.5)
        return max(d) - min(d)

    check("locus around the zenith projects to a true circle",
          projected_spread(88.0) < 0.1, f"spread {projected_spread(88.0):.2f} px")
    check("locus near the horizon is emphatically not a circle",
          projected_spread(10.0) > 10.0,
          f"spread only {projected_spread(10.0):.2f} px")
    check("distortion grows as the moon drops",
          projected_spread(10.0) > projected_spread(30.0)
          > projected_spread(60.0) > projected_spread(88.0))


def test_skymap_marks():
    """A target is a dot only when the ephemeris covers this instant."""
    now = 1_760_000_000.0
    track = [(now - 3600, 100.0, 30.0), (now, 110.0, 40.0),
             (now + 3600, 120.0, 50.0)]
    rows = [{"desig": "TEST01", "observed": False, "mask_flags": [],
             "vmag": 19.0, "score": 80}]

    up = skymap.target_marks(rows, {"TEST01": track}, now)
    check("target inside its ephemeris span is drawn as a dot",
          len(up) == 1 and up[0]["up"] and abs(up[0]["alt"] - 40.0) < 1e-6,
          up)

    # Six hours before the first row: not up, so a rim tick, not a dot at the
    # first row's position. Plotting the nearest row regardless is how a map
    # ends up showing the night's targets in the afternoon.
    early = skymap.target_marks(rows, {"TEST01": track}, now - 6 * 3600)
    check("target outside its span becomes a rim tick",
          len(early) == 1 and not early[0]["up"]
          and early[0]["rise_ts"] == now - 3600, early)

    after = skymap.target_marks(rows, {"TEST01": track}, now + 6 * 3600)
    check("target whose night is over is dropped entirely", after == [], after)

    obs = skymap.target_marks(
        [dict(rows[0], observed=True)], {"TEST01": track}, now)
    check("observed target stays on the map", len(obs) == 1 and obs[0]["observed"])

    svg = skymap.render_svg(obs, None, size=400)
    check("observed marker is drawn green", "#55b37e" in svg)

    # The map's ring is judged where the object is NOW, not from the stored
    # peak flag -- the peak deliberately prefers clean sky, so a peak-derived
    # ring would stay dark even for a target sitting in the light dome.
    # South-west, not north: the whole northern sector below 70 deg is inside
    # the keep-out wedge now, where targets are removed rather than ringed.
    # The SW sector is soft-masked below 30 deg and clear of the wedge, so it
    # still exercises the ring.
    poor = [(now - 60, 225.0, 20.0), (now + 60, 225.0, 20.0)]
    got = skymap.target_marks(
        [dict(rows[0], mask_flags=[])], {"TEST01": poor}, now)
    check("target in poor sky is ringed on the map despite a clean peak flag",
          got and got[0]["mask"], got)

    south = [(now - 60, 180.0, 55.0), (now + 60, 180.0, 55.0)]
    clear = skymap.target_marks(
        [dict(rows[0], mask_flags=["azBlocked:N"])], {"TEST01": south}, now)
    check("target in clear sky is not ringed despite a stale peak flag",
          clear and not clear[0]["mask"], clear)


def test_interpolate_clamps_to_the_right_end():
    """Past the end of a track, clamp to the LAST sample, not the first.

    Falling back to track[0] put a target that had just run off the end of its
    ephemeris back where it was when the ephemeris began -- a day earlier and
    most of the sky away. On the live board A11GP9t read azimuth 112 while it
    was actually near 76.
    """
    t0 = 1_760_000_000.0
    track = [(t0, 100.0, 20.0), (t0 + 1800, 110.0, 30.0), (t0 + 3600, 120.0, 40.0)]

    az, alt = skymap._interpolate(track, t0 + 900)
    check("interpolates inside the track", abs(az - 105.0) < 1e-6
          and abs(alt - 25.0) < 1e-6, (az, alt))

    az, alt = skymap._interpolate(track, t0 + 7200)
    check("past the end clamps to the last sample",
          (az, alt) == (120.0, 40.0), (az, alt))

    az, alt = skymap._interpolate(track, t0 - 7200)
    check("before the start clamps to the first sample",
          (az, alt) == (100.0, 20.0), (az, alt))

    # Azimuth still takes the short way round the wrap.
    wrap = [(t0, 350.0, 30.0), (t0 + 1800, 10.0, 30.0)]
    az, _ = skymap._interpolate(wrap, t0 + 900)
    check("azimuth wraps the short way", abs(az - 0.0) < 1e-6 or abs(az - 360.0) < 1e-6,
          az)


def test_ephemeris_refetched_when_its_window_runs_out():
    """The cache must notice an ephemeris that no longer covers tonight.

    The signature tracks the orbit solution, not the span MPC computed. An
    object attracting no new astrometry was therefore never re-requested, and
    its cached ephemeris silently became last night's -- 15 of 38 observable
    targets on the live board.
    """
    now = 1_760_000_000.0
    target = {"desig": "TEST01", "nobs": 12, "arc_days": 0.5}
    sig = ephemeris.signature(target)
    full = {"offsets": [], "obs_codes": {}, "fetched_ts": now - 600,
            "last_row_ts": now + 6 * 3600}

    check("a covering ephemeris is left alone",
          not update_neocp.needs_refetch((sig, full), target, now))
    check("no cache entry means fetch",
          update_neocp.needs_refetch(None, target, now))
    check("a changed solution means fetch",
          update_neocp.needs_refetch(("99|9.9", full), target, now))

    # The case that was silently broken.
    ended = dict(full, last_row_ts=now - 14 * 3600, fetched_ts=now - 20 * 3600)
    check("an ephemeris whose window has ended is refetched",
          update_neocp.needs_refetch((sig, ended), target, now))

    # ...but not on every cycle, or an object that never rises again would be
    # requested forever: a fresh fetch would also end in the past.
    just_tried = dict(ended, fetched_ts=now - 60)
    check("but not again within the backoff",
          not update_neocp.needs_refetch((sig, just_tried), target, now))
    later = dict(ended, fetched_ts=now - config.EPHEMERIS_REFETCH_BACKOFF_S - 60)
    check("and is retried once the backoff expires",
          update_neocp.needs_refetch((sig, later), target, now))

    # Entries cached before these fields existed must backfill exactly once.
    old = {"offsets": [], "obs_codes": {}}
    check("entries predating the coverage fields are refetched once",
          update_neocp.needs_refetch((sig, old), target, now))

    # An object MPC returned nothing for must not spin.
    empty = dict(full, last_row_ts=None)
    check("an empty ephemeris does not spin the fetcher",
          not update_neocp.needs_refetch((sig, empty), target, now))


def _fake_eph(n=12, rising=True):
    """A synthetic ephemeris arc for the altitude plot."""
    base = 1_760_000_000.0
    out = []
    for i in range(n):
        r = ephemeris.Row(SAMPLE_EPH)
        r.ts = base + i * 1800
        frac = i / (n - 1)
        r.alt = 25.0 + 55.0 * (frac if rising else (1.0 - frac))
        r.moon_alt = -20.0 - 30.0 * frac
        r.sun_alt = -30.0
        r.moon_dist = 95.0
        out.append(r)
    return out


def test_keepout_wedge():
    """Sky the mount must not be pointed at. Hard, and never overridable.

    The arc runs clockwise, west -> north -> north-east. Read the other way
    round it covers the southern sky instead, which on a measured night cut
    87% of usable rows rather than 9% -- so the direction is pinned here.
    """
    far_future = 2 ** 40
    # Read the edges from config rather than hardcoding them. The boundary has
    # already moved once and will move again when the observatory gives us the
    # real mount limit; what must never change is the INVARIANT -- north is
    # inside, south is outside, and a low row in there is refused.
    start, end, min_alt, _reason = config.KEEPOUT_WEDGES[0]
    inside_az = (start + end) / 2.0 if start < end else ((start + 360.0 + end) / 2.0) % 360.0

    check("the arc runs clockwise and swallows the north",
          all(bool(observability.in_arc(a, start, end))
              for a in (start + 1.0, 315.0, 350.0, 0.0, 20.0, end - 1.0)),
          [a for a in (start + 1.0, 315.0, 350.0, 0.0, 20.0, end - 1.0)
           if not observability.in_arc(a, start, end)])
    check("and NOT the long way round through the south",
          not any(bool(observability.in_arc(a, start, end))
                  for a in (90.0, 135.0, 180.0, 200.0, start - 1.0)),
          [a for a in (90.0, 135.0, 180.0, 200.0, start - 1.0)
           if observability.in_arc(a, start, end)])

    check("just outside the western edge is clear",
          observability.keepout_violation(start - 1.0, 30.0) is None)
    check("just inside the western edge is not",
          observability.keepout_violation(start + 1.0, 30.0) is not None)
    check("just inside the north-eastern edge is not",
          observability.keepout_violation(end - 1.0, 30.0) is not None)
    check("just outside the north-eastern edge is clear",
          observability.keepout_violation(end + 1.0, 30.0) is None)
    check("high enough inside the wedge is allowed",
          observability.keepout_violation(inside_az, min_alt + 5.0) is None)
    check("but not a shade under the limit",
          observability.keepout_violation(inside_az, min_alt - 0.1) is not None)

    # The specific case that moved the edge: a target tracking down the
    # west-south-west used to slip under a due-west boundary for a whole
    # night. Its real position when it was reported is pinned here.
    check("a target at azimuth 259, altitude 33 is inside the wedge",
          observability.keepout_violation(259.1, 32.9) is not None,
          "P22pZo5 as reported; a due-west edge missed it entirely")

    # It rejects rows outright, and no view or flag can bring them back.
    r = ephemeris.Row(SAMPLE_EPH)
    r.az, r.alt = 300.0, 50.0
    check("a row in the wedge is rejected, not merely flagged",
          "keepOut" in pipeline.row_rejections(r, far_future),
          pipeline.row_rejections(r, far_future))
    check("and it carries no soft mask flag instead",
          "keepOut" in pipeline.row_rejections(r, far_future))

    r.alt = 75.0
    check("the same azimuth above the limit is usable",
          pipeline.row_rejections(r, far_future) == [],
          pipeline.row_rejections(r, far_future))

    # The southern sky, where the observatory actually works, is untouched.
    south = ephemeris.Row(SAMPLE_EPH)
    south.az, south.alt = 180.0, 30.0
    check("southern sky is unaffected",
          pipeline.row_rejections(south, far_future) == [],
          pipeline.row_rejections(south, far_future))

    # Clear early, blocked later: keep the early rows, lose the late ones.
    eph_rows = []
    for i, (az, alt) in enumerate([(200.0, 40.0), (240.0, 45.0),
                                   (280.0, 50.0), (300.0, 40.0)]):
        row = ephemeris.Row(SAMPLE_EPH)
        row.ts = 1_760_000_000.0 + i * 1800
        row.az, row.alt = az, alt
        eph_rows.append(row)
    usable, report = pipeline.usable_rows(
        ephemeris.ObjectEphemeris("T", eph_rows), far_future)
    check("a target clear early keeps its early rows", len(usable) == 2, len(usable))
    check("and loses the ones inside the wedge",
          report.get("keepOut") == 2, report)

    # The wedge fires even though the advisory mask stays soft.
    north = ephemeris.Row(SAMPLE_EPH)
    north.az, north.alt = 0.0, 40.0
    check("north below the limit is now rejected by the wedge",
          "keepOut" in pipeline.row_rejections(north, far_future),
          pipeline.row_rejections(north, far_future))
    north.alt = 80.0
    check("north high above it is allowed, mask still only advisory",
          pipeline.row_rejections(north, far_future) == [],
          pipeline.row_rejections(north, far_future))

    # Drawn on the map, and nothing is plotted inside it.
    svg = skymap.render_svg([], None, size=400)
    check("the wedge is drawn hatched", 'fill="url(#keepout)"' in svg)
    check("and labelled as a hard limit", "Keep out" in svg)

    now = 1_760_000_000.0
    inside = [(now - 60, 300.0, 40.0), (now + 60, 302.0, 40.0)]
    marks = skymap.target_marks(
        [{"desig": "T", "vmag": 19.0, "score": 80, "mask_flags": []}],
        {"T": inside}, now)
    check("a target inside the wedge is not drawn at all", marks == [], marks)


def test_plan_file_never_publishes_a_stale_pointing_line():
    """Exactly one uncommented line per target, and only when it means now.

    interpolate_at() clamps to the nearest usable row whenever now falls
    outside the usable span -- before the target rises, and again after its
    window shuts. The coordinates then describe a different moment, and the
    sky has turned since, so slewing to them points somewhere else. Measured
    live, 4 of 24 targets were publishing such a line.
    """
    row = ephemeris.Row(SAMPLE_EPH)
    base = dict(desig="T1", score=80, nobs=12, arc_days=0.5,
                not_seen_days=0.1, exposure_min=20.0, max_alt_row=row,
                nearest_row=row, interp_row=row)

    live = output.plan_entry(dict(base, live_row_is_now=True))
    body = [l for l in live.splitlines() if l.startswith("2026")]
    check("a genuinely current target gets one pointable line",
          len(body) == 1, body)

    stale = output.plan_entry(dict(base, live_row_is_now=False))
    body = [l for l in stale.splitlines() if l.startswith("2026")]
    check("a clamped one gets none", len(body) == 0, body)
    check("its coordinates are kept, commented, and marked",
          "do not slew" in stale and "// 2026" in stale, stale.splitlines()[-1])

    # The same flag drives the queue badge. A target whose window has closed,
    # or which has moved into a keep-out wedge, is still "observable tonight"
    # -- but badging it GO invites a slew at something unpointable.
    check("the flag that suppresses the line is stored for the board",
          "live_row_is_now" in db._COLS, db._COLS[-6:])

    check("the block is otherwise unchanged in shape",
          len(live.splitlines()) == len(stale.splitlines()),
          (len(live.splitlines()), len(stale.splitlines())))


def test_altitude_plot():
    """The staralt-style altitude plot, as brought over from #8."""
    rows = _fake_eph()
    svg = moonplot.render_svg(rows, rows[2].ts, rows[-3].ts,
                              localt=lambda ts: "L%d" % (ts % 100),
                              tzlabel="CEST")
    check("plot renders", svg and svg.startswith("<svg"), type(svg))

    # The board speaks observatory local time everywhere; UT belongs in the
    # hover text, not on the axis.
    check("axis is labelled with the local zone, not UT",
          ">time (CEST)<" in svg, re.search(r">time \([^)]*\)<", svg))
    check("axis ticks use the supplied local formatter",
          ">L" in svg and "UT<" not in svg)
    check("hover text still carries UT",
          "UT &#8212;" in svg or " UT" in svg)

    # MIN_ALT is dead -- oalt=20 means MPC never sends a row below 20, so a
    # line at 15 would imply a limit that can never apply.
    check("floor line draws MPC's real cutoff",
          f"MPC cutoff {config.MPC_SERVER_MIN_ALT:g}" in svg)
    check("and not the dead MIN_ALT threshold",
          f"min {config.MIN_ALT:g}&#176;" not in svg)

    check("the usable window is shaded", "observable window" in svg)
    check("too few rows renders nothing rather than a broken axis",
          moonplot.render_svg(rows[:1]) is None)

    # Rising and setting targets crowd opposite corners, so the legend has to
    # move. Pinning it left hid the first hours of a setting target entirely.
    setting = moonplot.render_svg(_fake_eph(rising=False), tzlabel="CEST")
    rising = moonplot.render_svg(_fake_eph(rising=True), tzlabel="CEST")

    def legend_x(s):
        return float(re.search(r'<rect x="([\d.]+)" y="[\d.]+" width="132"',
                               s).group(1))

    check("the legend moves to the clearer corner for a setting target",
          legend_x(setting) > legend_x(rising),
          (legend_x(setting), legend_x(rising)))
def test_done_targets_stay_in_the_list():
    """Marking a target done must not remove it from the board.

    It used to vanish the moment you clicked done, which hid three things at
    once: the green row, the green marker on the sky map, and the undo button
    -- so correcting a misclick meant hunting for the row in a separate view.
    """
    import app
    now = time.time()
    rows = [
        dict(desig="fresh", observable=True, observed=0, max_alt_ts=now + 1800),
        dict(desig="finished", observable=True, observed=1, max_alt_ts=now + 900),
        dict(desig="later", observable=True, observed=0, max_alt_ts=now + 7200),
    ]

    # The board always loads observed rows now; the ?observed flag is gone.
    with app.app.test_request_context("/"):
        v = app._view_args()
    check("the default view asks for observed rows", v["show_observed"] is True, v)
    with app.app.test_request_context("/?observed=0"):
        v = app._view_args()
    check("and no query string can turn them back off",
          v["show_observed"] is True, v)

    picked = [r["desig"] for r in app.pick_upcoming(rows, n=3)]
    check("a done target never headlines an up-next card",
          "finished" not in picked, picked)
    check("the cards still offer the outstanding targets in order",
          picked == ["fresh", "later"], picked)

    # ...but it keeps its lane on the night strip, tinted rather than dropped.
    for r in rows:
        r.update(window_start_ts=now - 3600, window_end_ts=now + 3600,
                 max_alt=40.0)
    strip = app.night_strip(rows)
    lanes = {l["desig"]: l for l in strip["lanes"]}
    check("a done target keeps its lane on the night strip",
          "finished" in lanes, sorted(lanes))
    check("and the lane is marked as observed so it can be tinted",
          lanes["finished"]["observed"] is True
          and lanes["fresh"]["observed"] is False,
          {k: v["observed"] for k, v in lanes.items()})


def test_done_target_is_green_on_the_sky_map():
    """A target done while it is still up shows a green marker.

    This path existed and was tested from the day the map was built, but was
    unreachable on the default view: the row was filtered out before the map
    ever saw it, so the marker could never be drawn.
    """
    now = 1_760_000_000.0
    track = [(now - 1800, 100.0, 30.0), (now, 110.0, 40.0),
             (now + 1800, 120.0, 50.0)]
    base = dict(desig="T1", vmag=19.0, score=80, mask_flags=[])

    done = skymap.target_marks([dict(base, observed=True)], {"T1": track}, now)
    check("a done, still-observable target is drawn",
          len(done) == 1 and done[0]["up"], done)
    check("and it is drawn green",
          "#55b37e" in skymap.render_svg(done, None, size=400))

    fresh = skymap.target_marks([dict(base, observed=False)], {"T1": track}, now)
    check("an outstanding target stays amber",
          "#f0a63c" in skymap.render_svg(fresh, None, size=400))


def test_moon_phase_geometry():
    """The drawn lune must enclose exactly the illuminated fraction.

    Caught a real inversion: the sweep flag was backwards, so a 13 percent
    crescent rendered as an 87 percent gibbous -- a plausible-looking moon
    that was simply the wrong one.
    """
    r = 8.0
    for illum in (0.0, 0.13, 0.25, 0.5, 0.75, 0.87, 1.0):
        rx, sweep = skymap.lune(illum, r)
        got = skymap.lit_fraction(rx, sweep, r)
        check(f"phase {illum:.2f} draws {illum * 100:.0f}% of the disc",
              abs(got - illum) < 1e-9, f"got {got:.4f}")

    check("new moon encloses nothing",
          abs(skymap.lit_fraction(*skymap.lune(0.0, r), r)) < 1e-9)
    check("full moon encloses the whole disc",
          abs(skymap.lit_fraction(*skymap.lune(1.0, r), r) - 1.0) < 1e-9)
    check("a crescent subtracts, a gibbous adds",
          skymap.lune(0.2, r)[1] == 0 and skymap.lune(0.8, r)[1] == 1)


def test_ephemeris_track_matches_row():
    """track() must agree with Row, including the south-to-north azimuth flip.

    They are two parsers of the same line. A second, drifting copy of the
    azimuth convention would rotate the entire map by 180 degrees without
    anything raising.
    """
    row = ephemeris.Row(SAMPLE_EPH)
    got = ephemeris.track([SAMPLE_EPH])
    check("track returns one entry for one row", len(got) == 1, got)
    ts, az, alt = got[0]
    check("track timestamp matches Row", ts == row.ts, (ts, row.ts))
    check("track azimuth matches Row (compass, not MPC south)",
          abs(az - row.az) < 1e-9, (az, row.az))
    check("track altitude matches Row", abs(alt - row.alt) < 1e-9, (alt, row.alt))
    check("track azimuth really is the flipped one",
          abs(az - (row.az_mpc + 180.0) % 360.0) < 1e-9, (az, row.az_mpc))


def main():
    for fn in (test_site, test_horizon_mask, test_analytic_matches_astropy,
               test_neocp_list_parse, test_neocp_info_column_collision,
               test_ephemeris_row, test_ephemeris_azimuth_convention,
               test_exposure_rule, test_night_bounds, test_chronological_sort,
               test_upcoming_cards_look_forward, test_cache_signature_ignores_the_clock,
               test_plan_file_survives_dawn, test_mpc_markers,
               test_observatory_identification, test_uncertainty_plot,
               test_auth_protects_state_changes,
               test_schema_migration_from_older_db,
               test_ranking_bounds, test_row_rejection_reasons,
               test_skymap_orientation, test_skymap_mask_wedges,
               test_moon_exclusion_locus, test_skymap_marks,
               test_keepout_wedge,
               test_plan_file_never_publishes_a_stale_pointing_line,
               test_altitude_plot,
               test_moon_phase_geometry, test_ephemeris_track_matches_row,
               test_done_targets_stay_in_the_list,
               test_done_target_is_green_on_the_sky_map,
               test_interpolate_clamps_to_the_right_end,
               test_ephemeris_refetched_when_its_window_runs_out):
        print(f"\n{fn.__name__}:")
        fn()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
