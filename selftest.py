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
import json
import os
import re
import sqlite3
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

    # The records themselves, kept for ds42 -- see ds42.md. The parse already
    # walks every line to count codes, so this costs nothing to return, and
    # it is the only copy that will exist: MPC stops serving an object's
    # astrometry once it leaves NEOCP.
    check("the records come back with the summary",
          len(got["records"]) == 5, got.get("records"))
    check("the short line is excluded, as it is from the counts",
          all(len(r) >= 80 for r in got["records"]))
    check("records keep their fixed-width columns",
          all(r[77:80].strip() in ("F51", "474") for r in got["records"]),
          [r[77:80] for r in got["records"]])
    check("the discovery asterisk survives in column 13",
          sum(1 for r in got["records"] if r[12] == "*") == 1)
    check("records are in the order MPC gave them",
          [r[77:80].strip() for r in got["records"]]
          == ["F51", "F51", "F51", "474", "474"])
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
    """Reads stay open; anything that changes state must be protected.

    The shared basic-auth credential is transitional -- accounts replace it --
    but it has to keep working while Luka's team moves over, so it is tested
    as a first-class path rather than as a leftover."""
    import importlib
    import tempfile

    import app as appmod
    import auth

    # Redirected FIRST, before the app is touched at all. Every route goes
    # through get_conn(), which calls db.init() -- so merely *reading* a page
    # runs the schema migration against whatever database config points at,
    # and on epyc that is the observatory's. An earlier version of this fix
    # redirected further down, after the three reads below, and the next
    # schema change caught it: running the suite on epyc created the new
    # night_archive table in the live database.
    prev_db = config.DB_PATH
    prev_env = os.environ.get("WHICHNEO_AUTH")
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    importlib.reload(appmod)

    client = appmod.app.test_client()
    for path in ("/", "/status", "/rows"):
        check(f"{path} readable without credentials",
              client.get(path).status_code == 200)

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
        config.DB_PATH = prev_db
        if prev_env is None:
            os.environ.pop("WHICHNEO_AUTH", None)
        else:
            os.environ["WHICHNEO_AUTH"] = prev_env
        importlib.reload(auth)
        importlib.reload(appmod)

    # "No credentials configured" has two sources -- the environment and
    # data/auth -- and this has to neutralise both. Clearing only the
    # environment passes on a laptop and fails on epyc, which is the one
    # machine whose answer matters: it has had a data/auth since the board
    # went public, so the check asserted something that was simply untrue
    # there.
    prev_dir = config.DATA_DIR
    prev_db = config.DB_PATH
    scratch = tempfile.mkdtemp()
    config.DATA_DIR = scratch
    config.DB_PATH = os.path.join(scratch, "targets.db")
    try:
        importlib.reload(auth)
        importlib.reload(appmod)
        check("shared credential is off when none is configured",
              not auth.BASIC_ENABLED)
        # This is the line that changed meaning when accounts arrived. It used
        # to read `not auth.ENABLED` -- "no password file, so writes are
        # open". A board on the public internet must not have that state at
        # all: with no credential file and no account, a write is still
        # refused.
        check("writes stay protected with no shared credential at all",
              auth.ENABLED)
        check("anonymous write refused with no credential configured",
              appmod.app.test_client().post(
                  "/mark/XYZ", data={"action": "hide"}).status_code == 401)
    finally:
        config.DATA_DIR = prev_dir
        config.DB_PATH = prev_db
        importlib.reload(auth)
        importlib.reload(appmod)


def test_accounts():
    """Sign-up, sign-in, and the CSRF token on every write.

    Each check here is something that, wrong, is invisible from the board:
    a password stored in the clear looks identical to one that is hashed, and
    a missing CSRF check looks identical to a working one until somebody else's
    page starts marking targets done on an observer's behalf.
    """
    import importlib
    import tempfile

    import app as appmod
    import auth

    prev_db = config.DB_PATH
    prev_env = os.environ.pop("WHICHNEO_AUTH", None)
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    try:
        importlib.reload(auth)
        importlib.reload(appmod)
        c = appmod.app.test_client()

        def session_token(client):
            with client.session_transaction() as s:
                return s.get("csrf")

        good = {"username": "obs1", "password": "correct-horse-1",
                "confirm": "correct-horse-1"}

        r = c.post("/register", data=dict(good, password="short", confirm="short"))
        check("short password refused", r.status_code == 400
              and b"least" in r.data)
        r = c.post("/register", data=dict(good, username="has space"))
        check("malformed username refused", r.status_code == 400)
        r = c.post("/register", data=dict(good, confirm="mistyped-horse-1"))
        check("mismatched confirmation refused", r.status_code == 400
              and b"do not match" in r.data)

        r = c.post("/register", data=good)
        check("registration succeeds", r.status_code == 302)

        fresh = appmod.app.test_client()
        r = fresh.post("/register", data=dict(good, username="OBS1"))
        check("username uniqueness is case-insensitive",
              r.status_code == 400 and b"taken" in r.data)

        conn = db.connect(config.DB_PATH)
        try:
            user = db.user_by_name(conn, "obs1")
            stored = user["password_hash"]
            check("password stored as argon2id",
                  stored.startswith("$argon2id$"))
            # The plaintext must not appear anywhere in the row. A hash that
            # happens to embed the password would pass the prefix check above.
            check("plaintext password nowhere in the user row",
                  "correct-horse-1" not in " ".join(
                      str(v) for v in dict(user).values()))
            ok, _ = auth.verify_password(stored, "correct-horse-1")
            bad, _ = auth.verify_password(stored, "correct-horse-2")
            check("hash verifies the right password", ok)
            check("hash rejects the wrong password", not bad)
        finally:
            conn.close()

        tok = session_token(c)
        check("session carries a CSRF token", bool(tok))
        check("write without a token refused",
              c.post("/mark/AAA", data={"action": "hide"}).status_code == 400)
        check("write with the wrong token refused",
              c.post("/mark/AAA", data={"action": "hide", "csrf": "no"}
                     ).status_code == 400)
        check("write with the session's token accepted",
              c.post("/mark/AAA", data={"action": "hide", "csrf": tok}
                     ).status_code == 302)

        conn = db.connect(config.DB_PATH)
        try:
            row = conn.execute("SELECT hidden FROM observer_state "
                               "WHERE desig='AAA'").fetchone()
            check("the accepted write actually landed",
                  row is not None and row[0] == 1)
        finally:
            conn.close()

        page = c.get("/").data.decode()
        check("signed-in observer named in the header", "obs1" in page)
        c.post("/logout")
        check("signing out clears the session", session_token(c) != tok)
        check("write refused again after signing out",
              c.post("/mark/AAA", data={"action": "restore"}
                     ).status_code in (400, 401))

        # One message for a wrong password and for a name that does not exist.
        # Two different messages would enumerate the observatory's accounts.
        r1 = c.post("/login", data={"username": "obs1", "password": "nope"})
        r2 = c.post("/login", data={"username": "ghost", "password": "nope"})
        check("wrong password and unknown user answer identically",
              r1.status_code == r2.status_code == 401
              and b"do not match" in r1.data and b"do not match" in r2.data)

        r = c.post("/login", data={"username": "obs1",
                                   "password": "correct-horse-1"})
        check("correct credentials sign in", r.status_code == 302)

        c.post("/logout")
        r = c.post("/login", data={"username": "obs1",
                                   "password": "correct-horse-1",
                                   "next": "https://evil.example/"})
        check("next= cannot leave the site",
              dict(r.headers).get("Location") == "/")
        c.post("/logout")
        r = c.post("/login", data={"username": "obs1",
                                   "password": "correct-horse-1",
                                   "next": "//evil.example/"})
        check("protocol-relative next= cannot leave the site",
              dict(r.headers).get("Location") == "/")

        for path in ("/", "/status", "/rows", "/login", "/register"):
            check(f"{path} readable signed out",
                  appmod.app.test_client().get(path).status_code == 200)
    finally:
        config.DB_PATH = prev_db
        if prev_env is not None:
            os.environ["WHICHNEO_AUTH"] = prev_env
        importlib.reload(auth)
        importlib.reload(appmod)


def test_observer_state_is_per_account():
    """Two observers, one board.

    What one marks done is their own record of their own night. The risk this
    creates -- both integrating the same object for twenty minutes, neither
    seeing the other -- is why every row also carries who else has marked it.
    """
    import sqlite3
    import tempfile

    path = os.path.join(tempfile.mkdtemp(), "targets.db")
    conn = db.connect(path)
    db.init(conn)

    a = db.create_user(conn, "ana", "x")
    b = db.create_user(conn, "boris", "y")
    conn.execute("INSERT INTO targets (desig) VALUES ('P11aaaa'),('P11bbbb')")
    conn.commit()

    db.set_state(conn, "P11aaaa", a, observed=1, observed_at_utc=db.utcnow())

    rows_a = {r["desig"]: r for r in db.load_targets(conn, include_observed=True,
                                                     user_id=a)}
    rows_b = {r["desig"]: r for r in db.load_targets(conn, include_observed=True,
                                                     user_id=b)}
    check("the observer who marked it sees it done",
          rows_a["P11aaaa"]["observed"] == 1)
    check("the other observer does not", rows_b["P11aaaa"]["observed"] == 0)
    check("but is told somebody else did",
          rows_b["P11aaaa"]["others_observed"] == 1
          and rows_b["P11aaaa"]["others_names"] == "ana")
    check("and is not told about themselves",
          rows_a["P11aaaa"]["others_observed"] == 0)
    check("an untouched target is clean for both",
          rows_a["P11bbbb"]["others_observed"] == 0
          and rows_b["P11bbbb"]["observed"] == 0)

    db.set_state(conn, "P11aaaa", b, observed=1, observed_at_utc=db.utcnow())
    rows_a = {r["desig"]: r for r in db.load_targets(conn, include_observed=True,
                                                     user_id=a)}
    check("two marks on one target coexist",
          conn.execute("SELECT count(*) FROM observer_state "
                       "WHERE desig='P11aaaa'").fetchone()[0] == 2)
    check("now each sees the other", rows_a["P11aaaa"]["others_names"] == "boris")

    # Hiding is personal too: one observer clearing their list must not take
    # the target off anybody else's.
    db.set_state(conn, "P11bbbb", a, hidden=1)
    vis_b = [r["desig"] for r in db.load_targets(conn, user_id=b)]
    check("hiding is personal", "P11bbbb" in vis_b)

    anon = {r["desig"]: r for r in db.load_targets(conn, include_observed=True,
                                                   user_id=None)}
    check("a signed-out reader owns no state",
          anon["P11aaaa"]["observed"] == 0 and anon["P11bbbb"]["hidden"] == 0)
    check("but still sees what has been done",
          anon["P11aaaa"]["others_observed"] == 2)

    db.set_state(conn, "P11bbbb", 0, observed=1)
    # include_hidden, because ana hid P11bbbb three lines up -- and that is
    # the point: her hiding it does not stop it being on the board.
    seen_by_a = {r["desig"]: r for r in db.load_targets(
        conn, include_observed=True, include_hidden=True, user_id=a)}
    check("a write with the shared credential is labelled, not attributed",
          seen_by_a["P11bbbb"]["others_names"] == "shared")
    conn.close()


def test_observer_state_migration_keeps_every_mark():
    """observer_state is the one table nothing can regenerate.

    `targets` is rebuilt from MPC every five minutes, so dropping it costs
    nothing. There is nowhere to re-fetch the fact that L01 observed something
    last Tuesday, so the user_id migration has to rebuild and copy -- and the
    first account has to inherit the marks, or a season of work shows up as
    "already done by someone else" to everybody forever.
    """
    import tempfile

    path = os.path.join(tempfile.mkdtemp(), "targets.db")
    conn = db.connect(path)
    # The pre-accounts shape, exactly as the live database has it.
    conn.executescript("""
        CREATE TABLE observer_state (
            desig           TEXT PRIMARY KEY,
            observed        INTEGER DEFAULT 0,
            observed_at_utc TEXT,
            hidden          INTEGER DEFAULT 0,
            priority_bump   REAL DEFAULT 0,
            note            TEXT
        );
        INSERT INTO observer_state (desig, observed, observed_at_utc, hidden, note)
        VALUES ('C46JQC1', 1, '2026-09-01 22:10:00', 0, 'clouds'),
               ('A11GP9t', 0, NULL, 1, NULL);
    """)
    conn.commit()

    db.init(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(observer_state)")}
    check("migrated table has user_id", "user_id" in cols)
    check("every row survived the migration",
          conn.execute("SELECT count(*) FROM observer_state").fetchone()[0] == 2)
    check("the old table is kept as a fallback",
          conn.execute("SELECT count(*) FROM sqlite_master WHERE name="
                       "'observer_state_pre_accounts'").fetchone()[0] == 1)
    row = conn.execute("SELECT * FROM observer_state WHERE desig='C46JQC1'"
                       ).fetchone()
    check("fields came across intact",
          row["observed"] == 1 and row["note"] == "clouds"
          and row["observed_at_utc"] == "2026-09-01 22:10:00")
    check("migrated rows land unattributed", row["user_id"] == 0)

    uid = db.create_user(conn, "luka", "hash")
    check("the first account inherits the pre-accounts marks",
          conn.execute("SELECT count(*) FROM observer_state WHERE user_id = ?",
                       (uid,)).fetchone()[0] == 2)
    second = db.create_user(conn, "ana", "hash")
    check("a later account inherits nothing",
          conn.execute("SELECT count(*) FROM observer_state WHERE user_id = ?",
                       (second,)).fetchone()[0] == 0)

    db.init(conn)
    check("running the migration twice is a no-op",
          conn.execute("SELECT count(*) FROM observer_state").fetchone()[0] == 2)
    conn.close()


def test_password_reset_by_email():
    """The reset flow, with the relay replaced by a list.

    Nothing here touches the network. What it does check is the two things
    that are invisible if they are wrong: that a used link stops working, and
    that the form answers identically for an account that exists and one that
    does not.
    """
    import importlib
    import tempfile

    import app as appmod
    import auth
    import mailer

    prev_db = config.DB_PATH
    prev_env = os.environ.pop("WHICHNEO_AUTH", None)
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    sent = []
    real_send, real_available = mailer.send, mailer.available
    try:
        importlib.reload(auth)
        importlib.reload(appmod)
        # Replace the relay, not the routes: everything above smtplib is the
        # code we actually ship.
        appmod.mailer.send = lambda to, subj, body: sent.append((to, subj, body))
        appmod.mailer.available = lambda: True

        conn = db.connect(config.DB_PATH)
        db.init(conn)
        db.create_user(conn, "ana", auth.hash_password("first-password-1"),
                       "ana@example.org")
        db.create_user(conn, "boris", auth.hash_password("first-password-2"))
        conn.close()

        c = appmod.app.test_client()

        r = c.post("/forgot", data={"who": "ana"})
        check("asking by username sends one message",
              r.status_code == 200 and len(sent) == 1)
        check("it goes to the address on file", sent[0][0] == "ana@example.org")

        sent.clear()
        r2 = c.post("/forgot", data={"who": "ana@example.org"})
        check("asking by email works too", len(sent) == 1)

        # The three cases a stranger could use to enumerate accounts.
        before = c.post("/forgot", data={"who": "ana"})
        sent.clear()
        nobody = c.post("/forgot", data={"who": "nosuchperson"})
        no_email = c.post("/forgot", data={"who": "boris"})
        check("an unknown name answers like a known one",
              nobody.status_code == before.status_code
              and nobody.data == before.data)
        check("an account with no email answers the same way",
              no_email.status_code == before.status_code
              and no_email.data == before.data)
        check("and neither actually sends anything", sent == [])

        sent.clear()
        c.post("/forgot", data={"who": "ana"})
        body = sent[0][2]
        token = re.search(r"/reset/(\S+)", body).group(1)
        check("the link is absolute so it works from an inbox",
              config.SITE_URL in body)
        check("the account name is in the message", "ana" in body)

        r = c.get(f"/reset/{token}")
        check("a good link opens the form",
              r.status_code == 200 and b"ana" in r.data)
        check("a mangled link does not",
              c.get(f"/reset/{token[:-4]}xxxx").status_code == 400)

        r = c.post(f"/reset/{token}", data={"password": "short",
                                            "confirm": "short"})
        check("the new password still has to be long enough",
              r.status_code == 400)
        r = c.post(f"/reset/{token}", data={"password": "second-password-1",
                                            "confirm": "mistyped"})
        check("and still has to be typed twice", r.status_code == 400)

        r = c.post(f"/reset/{token}", data={"password": "second-password-1",
                                            "confirm": "second-password-1"})
        check("a good reset signs you straight in", r.status_code == 302)

        conn = db.connect(config.DB_PATH)
        try:
            stored = db.user_by_name(conn, "ana")["password_hash"]
            ok, _ = auth.verify_password(stored, "second-password-1")
            old, _ = auth.verify_password(stored, "first-password-1")
            check("the new password works", ok)
            check("the old one does not", not old)
            # This is the single-use property, and it is free: the token
            # carries a fingerprint of the password hash, which just changed.
            check("the link cannot be used twice",
                  auth.reset_token_user(conn, token) is None)

            user = db.user_by_name(conn, "ana")
            check("an expired link is refused",
                  auth.reset_token_user(conn, auth.reset_token(user),
                                        max_age=-1) is None)
            check("a token signed with another key is refused",
                  auth.reset_token_user(conn, "abc.def.ghi") is None)
        finally:
            conn.close()

        check("used link is refused by the route too",
              c.get(f"/reset/{token}").status_code == 400)

        appmod.mailer.available = lambda: False
        r = c.get("/forgot")
        check("with no relay the page says so rather than lying",
              r.status_code == 503 and b"cannot send email" in r.data)
    finally:
        mailer.send, mailer.available = real_send, real_available
        config.DB_PATH = prev_db
        if prev_env is not None:
            os.environ["WHICHNEO_AUTH"] = prev_env
        importlib.reload(auth)
        importlib.reload(appmod)


def test_invite_creates_an_account_nobody_can_sign_into():
    """`manage.py invite` is the onboarding path that needs no terminal.

    The property that matters: between creating the account and its owner
    opening the emailed link, there must be no password that works. Not a
    blank one, not a default, not one printed to a terminal -- nothing.
    """
    import argparse
    import tempfile

    import auth
    import mailer

    prev_db = config.DB_PATH
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    sent = []
    real_send_now, real_available = mailer.send_now, mailer.available
    try:
        import manage

        mailer.send_now = lambda to, subj, body: (
            sent.append((to, subj, body)) or "<test@local>")
        mailer.available = lambda: True

        conn = db.connect(config.DB_PATH)
        db.init(conn)

        args = argparse.Namespace(username="luka", email="luka@example.org",
                                  admin=True, resend=False)
        check("invite succeeds", manage.cmd_invite(conn, args) == 0)

        user = db.user_by_name(conn, "luka")
        check("the account exists", user is not None)
        check("and is an admin", user["is_admin"] == 1)
        check("one message went to the right address",
              len(sent) == 1 and sent[0][0] == "luka@example.org")

        # The whole point. Try the obvious candidates for a password that
        # someone might have left working.
        guesses = ["", " ", "luka", "password", "luka@example.org",
                   "changeme", "whichneo"]
        works = [g for g in guesses
                 if auth.verify_password(user["password_hash"], g)[0]]
        check("no guessable password signs into it", works == [], str(works))
        check("the password is hashed, not a placeholder",
              user["password_hash"].startswith("$argon2id$"))

        body = sent[0][2]
        token = re.search(r"/reset/(\S+)", body).group(1)
        check("the link is in the message and absolute", config.SITE_URL in body)
        check("the link names the account",
              auth.reset_token_user(conn, token) is not None
              and auth.reset_token_user(conn, token)["username"] == "luka")
        check("the username is stated so they know what to sign in as",
              "luka" in body)

        # Setting a password through the link must kill it, exactly as a
        # normal reset does -- an invitation is not a standing key.
        db.update_password(conn, user["id"], auth.hash_password("chosen-by-luka"))
        check("the invitation stops working once used",
              auth.reset_token_user(conn, token) is None)

        args2 = argparse.Namespace(username="luka", email="luka@example.org",
                                   admin=False, resend=False)
        check("inviting an existing account twice is refused",
              manage.cmd_invite(conn, args2) == 1)
        sent.clear()
        args3 = argparse.Namespace(username="luka", email="luka@example.org",
                                   admin=False, resend=True)
        check("--resend sends a fresh link instead",
              manage.cmd_invite(conn, args3) == 0 and len(sent) == 1)

        db.create_user(conn, "noemail", auth.hash_password("x" * 12))
        args4 = argparse.Namespace(username="noemail", email=None,
                                   admin=False, resend=True)
        check("an account with no address cannot be invited",
              manage.cmd_invite(conn, args4) == 1)

        mailer.available = lambda: False
        args5 = argparse.Namespace(username="ana", email="ana@example.org",
                                   admin=False, resend=False)
        check("with no relay, no half-made account is left behind",
              manage.cmd_invite(conn, args5) == 1
              and db.user_by_name(conn, "ana") is None)
        conn.close()
    finally:
        mailer.send_now, mailer.available = real_send_now, real_available
        config.DB_PATH = prev_db


def test_invitations_outlive_a_reset_link():
    """An invitation must survive being read the next morning.

    A reset is asked for by someone sitting at the page waiting. An invitation
    is pushed at someone who was not expecting it, and the clock starts when
    it is minted, not when it is read -- so on one shared hour, anyone invited
    while they were asleep opens a dead link, with nothing to distinguish that
    from a broken board.
    """
    import tempfile
    import time as _time

    import auth

    prev_db = config.DB_PATH
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    try:
        conn = db.connect(config.DB_PATH)
        db.init(conn)
        db.create_user(conn, "ana", auth.hash_password("first-password-1"),
                       "ana@example.org")
        user = db.user_by_name(conn, "ana")

        check("an invitation lasts longer than a reset",
              auth.token_lifetime("invite") > auth.token_lifetime("reset"))
        check("an invitation lasts at least a night",
              auth.token_lifetime("invite") >= 12 * 3600)

        invite = auth.reset_token(user, kind="invite")
        reset = auth.reset_token(user)
        check("both kinds work when fresh",
              auth.reset_token_user(conn, invite) is not None
              and auth.reset_token_user(conn, reset) is not None)

        # The case that bit: read two hours after it was sent.
        real = _time.time
        _time.time = lambda: real() + 2 * 3600
        try:
            check("a reset link is dead after two hours",
                  auth.reset_token_user(conn, reset) is None)
            check("an invitation is still good",
                  auth.reset_token_user(conn, invite) is not None)
        finally:
            _time.time = real

        _time.time = lambda: real() + auth.token_lifetime("invite") + 60
        try:
            check("but an invitation does expire eventually",
                  auth.reset_token_user(conn, invite) is None)
        finally:
            _time.time = real

        # The kind decides the lifetime, so it must not be editable by whoever
        # holds the link -- otherwise an hour becomes a day for the asking.
        forged = auth.reset_token(user, kind="invite")
        tampered = reset.split(".")[0] + "." + ".".join(forged.split(".")[1:])
        check("a token cannot be re-labelled as an invitation",
              auth.reset_token_user(conn, tampered) is None)

        # Tokens minted before invitations existed carry no kind at all.
        from itsdangerous import URLSafeTimedSerializer
        legacy = URLSafeTimedSerializer(
            auth.secret_key(), salt=auth.RESET_SALT).dumps(
                {"uid": user["id"],
                 "fp": auth._hash_fingerprint(user["password_hash"])})
        check("a token with no kind still works",
              auth.reset_token_user(conn, legacy) is not None)
        _time.time = lambda: real() + 2 * 3600
        try:
            check("and is treated as the shorter reset, not the longer invite",
                  auth.reset_token_user(conn, legacy) is None)
        finally:
            _time.time = real

        check("the wording matches the clock",
              auth.lifetime_phrase("reset") == "1 hour"
              and auth.lifetime_phrase("invite") == "1 day")
        conn.close()
    finally:
        config.DB_PATH = prev_db


def test_mailer_builds_a_sane_message():
    """The parts of a message that decide whether it is delivered or filed as
    spam, checked without sending anything."""
    import mailer

    msg = mailer.build("someone@example.org", "WhichNEO password reset",
                       "line one\nline two\n")
    check("from is the relay identity", msg["From"] == config.SMTP_FROM)
    check("recipient is set", msg["To"] == "someone@example.org")
    check("subject survives", msg["Subject"] == "WhichNEO password reset")
    check("has a Date", bool(msg["Date"]))
    check("has a Message-ID on our own domain",
          "@" in (msg["Message-ID"] or "")
          and config.SMTP_FROM.split("@")[-1] in msg["Message-ID"])
    check("marked auto-generated so vacation responders stay quiet",
          msg["Auto-Submitted"] == "auto-generated")
    check("body is intact", "line two" in msg.get_content())
    check("STARTTLS is on for the configured relay", config.SMTP_STARTTLS)
    check("port is the submission port", config.SMTP_PORT == 587)


def test_login_throttle():
    """A public login form with no throttle is an invitation to guess, and
    argon2's deliberate cost makes each guess expensive for *us* -- enough
    parallel attempts and the board stops rendering."""
    import importlib
    import tempfile

    import app as appmod
    import auth

    # /login opens a connection like every other route, so this needs a
    # scratch database for the same reason the others do.
    prev_db = config.DB_PATH
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    try:
        importlib.reload(auth)
        importlib.reload(appmod)
        c = appmod.app.test_client()

        codes = [c.post("/login", data={"username": "ghost", "password": "x"}
                        ).status_code for _ in range(auth.FAIL_LIMIT + 2)]
        check("every bad attempt is refused", set(codes) == {401})
        last = c.post("/login", data={"username": "ghost", "password": "x"})
        check("attempts are capped after the limit",
              b"Too many failed attempts" in last.data)
    finally:
        # Leave no residue: the next test's client comes from the same process
        # and would arrive already locked out.
        auth._fails.clear()
        config.DB_PATH = prev_db
        importlib.reload(auth)
        importlib.reload(appmod)


def test_secret_key_is_stable_and_private():
    """Regenerating the signing key on restart logs the whole observatory out
    mid-night, so it has to be persisted -- and persisted 0600, since anyone
    who can read it can forge a session for any account."""
    import stat

    import auth

    prev = os.environ.pop("WHICHNEO_SECRET_KEY", None)
    try:
        first = auth.secret_key()
        second = auth.secret_key()
        check("key survives a second call", first == second)
        check("key is long enough to sign with", len(first) >= 32)
        mode = stat.S_IMODE(os.stat(auth.SECRET_PATH).st_mode)
        check("key file is not readable by anyone else", mode == 0o600,
              f"mode {oct(mode)}")
    finally:
        if prev is not None:
            os.environ["WHICHNEO_SECRET_KEY"] = prev


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


def test_replay_ts_never_takes_the_map_down():
    """?ts= must never turn /skymap.svg into a 500.

    float() accepting a value does not mean datetime can represent it:
    ?ts=99999999999999 parsed fine and then raised out of localt() building
    the response header -- "year 3170843 is out of range" -- on a public,
    unauthenticated endpoint the page polls every 20 seconds. nan and inf
    slipped through the same way.
    """
    import app
    now = time.time()

    check("a live request has no timestamp", app._replay_ts(None) is None)
    check("an empty value is live", app._replay_ts("") is None)

    ok = now - 3600
    got = app._replay_ts(str(ok))
    check("an instant inside the window is honoured",
          got is not None and abs(got - ok) < 1e-6, got)

    for bad, why in (("99999999999999", "the original crash"),
                     ("-99999999999999", "far past"),
                     ("nan", "not a number"),
                     ("inf", "infinite"),
                     ("-inf", "negative infinite"),
                     ("1e308", "enormous but finite"),
                     ("banana", "not a number at all"),
                     ("", "empty")):
        check("%-16s -> live, not a crash (%s)" % (bad, why),
              app._replay_ts(bad) is None, app._replay_ts(bad))

    # Anything accepted must survive the calls that previously blew up.
    for offset in (0, -3600, 3600, -86400, 86400):
        v = app._replay_ts(str(now + offset))
        if v is None:
            continue
        try:
            app.localt(v), app._tzabbr(v)
            fine = True
        except Exception as e:
            fine = "%s: %s" % (type(e).__name__, e)
        check("accepted ts %+7ds renders a header" % offset, fine is True, fine)

    check("beyond the window falls back to live",
          app._replay_ts(str(now + 2 * app._REPLAY_WINDOW_S)) is None)

    # An archived night carries its own bounds. Without them a night older
    # than _REPLAY_WINDOW_S would have every ?ts= rejected and the slider
    # would silently snap to the end of the night with nothing to explain it.
    old = now - 3 * app._REPLAY_WINDOW_S            # three years ago
    window = (old, old + 8 * 3600)
    mid = old + 4 * 3600
    check("a year-old night is still scrubbable with its own window",
          app._replay_ts(str(mid), window=window) is not None)
    check("the same instant is refused without a window",
          app._replay_ts(str(mid)) is None)
    check("outside the archived night falls back",
          app._replay_ts(str(old - 86400), window=window) is None)
    check("the window does not weaken the crash guards",
          all(app._replay_ts(b, window=window) is None
              for b in ("nan", "inf", "banana", "99999999999999")))


def test_night_archive_survives_per_account_state():
    """The archive must hold one row per target, not one per observer.

    observer_state is keyed by (desig, user_id) since accounts landed. The
    plain join this replaced fanned out to a row per target per observer --
    the same target archived twice, carrying opposite `observed` values, and
    drawn twice on the replayed map, once green and once amber. Silent: no
    error, just a wrong picture of a night nobody can re-observe.
    """
    import tempfile

    import auth

    prev_db = config.DB_PATH
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    try:
        conn = db.connect(config.DB_PATH)
        db.init(conn)
        ana = db.create_user(conn, "ana", auth.hash_password("x" * 12))
        boris = db.create_user(conn, "boris", auth.hash_password("x" * 12))

        conn.execute(
            "INSERT INTO targets (desig, score, vmag, observable, "
            "window_start_ts, window_end_ts) VALUES "
            "('P12aaaa', 90, 20.1, 1, 1000, 2000),"     # ana only
            "('P12bbbb', 80, 21.0, 1, 1100, 2100),"     # both
            "('P12cccc', 70, 22.0, 1, 1200, 2200),"     # nobody
            "('P12dddd', 60, 22.5, 0, 1300, 2300)")     # not observable
        conn.commit()
        db.set_state(conn, "P12aaaa", ana, observed=1)
        db.set_state(conn, "P12aaaa", boris, observed=0)
        db.set_state(conn, "P12bbbb", ana, observed=1)
        db.set_state(conn, "P12bbbb", boris, observed=1)
        db.set_state(conn, "P12dddd", ana, observed=1)

        check("archiving reports it wrote", db.archive_night(conn, "2026-09-15"))
        got = db.load_archived_night(conn, "2026-09-15")
        desigs = [t["desig"] for t in got["targets"]]

        check("one row per target, not per observer",
              len(desigs) == len(set(desigs)) == 3, desigs)
        check("a target two people marked appears once",
              desigs.count("P12bbbb") == 1)
        check("a non-observable target is not archived",
              "P12dddd" not in desigs)

        by = {t["desig"]: t for t in got["targets"]}
        check("observed means somebody shot it",
              by["P12aaaa"]["observed"] and by["P12bbbb"]["observed"])
        check("a target nobody marked is not observed",
              not by["P12cccc"]["observed"])
        check("who observed it is kept",
              sorted(by["P12bbbb"]["observers"]) == ["ana", "boris"]
              and by["P12aaaa"]["observers"] == ["ana"], by)
        check("boris marking it unobserved does not make him an observer",
              "boris" not in by["P12aaaa"]["observers"])
        check("nobody is recorded for an untouched target",
              by["P12cccc"]["observers"] == [])

        check("the night's bounds span its targets",
              got["start_ts"] == 1000 and got["end_ts"] == 2200)
        check("archiving twice is a no-op",
              db.archive_night(conn, "2026-09-15") is False)
        check("it is listed",
              [n["night"] for n in db.list_archived_nights(conn)]
              == ["2026-09-15"])
        check("an unknown night is None",
              db.load_archived_night(conn, "1999-01-01") is None)
        conn.close()
    finally:
        config.DB_PATH = prev_db


def test_archived_replay_endpoint():
    """/skymap.svg?night= must serve the archive without disturbing live."""
    import importlib
    import tempfile

    import app as appmod
    import auth

    prev_db = config.DB_PATH
    prev_env = os.environ.pop("WHICHNEO_AUTH", None)
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    try:
        importlib.reload(auth)
        importlib.reload(appmod)

        conn = db.connect(config.DB_PATH)
        db.init(conn)
        ana = db.create_user(conn, "ana", auth.hash_password("x" * 12))
        db.create_user(conn, "boris", auth.hash_password("x" * 12))

        # Built through archive_night itself rather than hand-inserted, so
        # this exercises the path the updater will actually take at rollover
        # -- the one step LemonSneeze's test plan left unchecked.
        eph_ts = ephemeris.Row(SAMPLE_EPH).ts
        start, end = eph_ts - 3600, eph_ts + 3600
        conn.execute(
            "INSERT INTO targets (desig, score, vmag, observable, "
            "window_start_ts, window_end_ts) VALUES (?,?,?,?,?,?)",
            ("P12aaaa", 90, 20.1, 1, start, end))
        conn.execute(
            "INSERT INTO ephemeris_cache (desig, signature, fetched_utc, "
            "payload) VALUES (?,?,?,?)",
            ("P12aaaa", "sig", db.utcnow(),
             json.dumps({"lines": [SAMPLE_EPH]})))
        conn.commit()
        db.set_state(conn, "P12aaaa", ana, observed=1)
        check("the real archive path writes a night",
              db.archive_night(conn, "2026-09-14"))
        conn.close()

        c = appmod.app.test_client()
        r = c.get("/skymap.svg?night=2026-09-14")
        check("an archived night renders",
              r.status_code == 200 and b"<svg" in r.data, r.status_code)
        check("it is served as SVG",
              "image/svg" in r.headers.get("Content-Type", ""))
        check("an unknown night is a clean 404, not a crash",
              c.get("/skymap.svg?night=1999-01-01").status_code == 404)

        # The guard that the conflict resolution had to preserve.
        for bad in ("99999999999999", "nan", "inf", "banana"):
            check("live ?ts=%-16s still not a 500" % bad,
                  c.get("/skymap.svg?ts=" + bad).status_code == 200)
            check("archived ?ts=%-16s still not a 500" % bad,
                  c.get("/skymap.svg?night=2026-09-14&ts=" + bad
                        ).status_code == 200)

        mid = str((start + end) / 2)
        check("scrubbing inside the archived night works",
              c.get("/skymap.svg?night=2026-09-14&ts=" + mid).status_code == 200)

        # Green means "you" when there is a you, "anyone" when there is not.
        anon = appmod.app.test_client().get("/skymap.svg?night=2026-09-14")
        with c.session_transaction() as s:
            s["uid"] = ana
        as_ana = c.get("/skymap.svg?night=2026-09-14")
        cb = appmod.app.test_client()
        with cb.session_transaction() as s:
            conn = db.connect(config.DB_PATH)
            s["uid"] = db.user_by_name(conn, "boris")["id"]
            conn.close()
        as_boris = cb.get("/skymap.svg?night=2026-09-14")

        def done_colour(resp):
            # The same green the live map uses for a done target; see
            # test_done_target_is_green_on_the_sky_map.
            return "#55b37e" in resp.data.decode()

        check("ana, who observed it, sees it green", done_colour(as_ana))
        check("boris, who did not, does not", not done_colour(as_boris))
        check("a signed-out visitor sees what the observatory did",
              done_colour(anon))
    finally:
        config.DB_PATH = prev_db
        if prev_env is not None:
            os.environ["WHICHNEO_AUTH"] = prev_env
        importlib.reload(auth)
        importlib.reload(appmod)


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
            "last_row_ts": now + 6 * 3600,
            "cache_schema": ephemeris.CACHE_SCHEMA}

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


def test_priority_bump_is_fully_gone():
    """The manual up/down adjustment is removed everywhere, not just the UI.

    The arrows never worked in the default chronological view -- that sort key
    ignores the bump -- so a click stored a number and moved nothing. Removing
    only the buttons would have left ten stored values still skewing the
    by-value ordering, unreachable and invisible.
    """
    rows = [
        dict(desig="A", observable=True, score_total=2.0, priority_bump=0.0),
        dict(desig="B", observable=True, score_total=1.0, priority_bump=99.0),
    ]
    order = [r["desig"] for r in ranking.sort_targets(rows, "score")]
    check("a stored bump cannot reorder the by-value view",
          order == ["A", "B"], order)

    check("the sort key ignores the bump entirely",
          ranking.sort_key_score(rows[1]) == ranking.sort_key_score(
              dict(rows[1], priority_bump=0.0)))

    src = open(os.path.join(os.path.dirname(__file__), "app.py")).read()
    check("the /mark endpoint no longer writes a bump",
          "priority_bump=" not in src, "app.py still sets priority_bump")
    check("and no longer handles up/down",
          '"up", "down"' not in src and "'up', 'down'" not in src)

    tpl = open(os.path.join(os.path.dirname(__file__),
                            "templates", "_rows.html")).read()
    check("the arrows are gone from the table",
          'value="up"' not in tpl and 'value="down"' not in tpl)
    check("the value column shows the bare score",
          "priority_bump" not in tpl, "template still reads priority_bump")

    # The column itself stays: observer_state also holds observed and hidden.
    check("observer_state keeps the column, so nothing else is disturbed",
          "priority_bump" in db.SCHEMA)


def test_offsets_parse_with_the_fast_motion_flag():
    """MPC appends ! or !! after the ephemeris number on fast movers.

    The pattern used to anchor `$` straight after the digits, so every line of
    a fast mover's offsets page failed and the object was recorded as having
    no uncertainty data. On a live board that was 40 of 103 objects, median
    motion 17.6 "/min against 2.4 for those that parsed -- the uncertainty was
    being stripped from exactly the objects whose uncertainty matters.

    Lines below are copied verbatim from ZTF10G9's page.
    """
    page = "\n".join([
        "      +0      +0      Ephemeris #    1 !!",
        "   +6581   +4822      Ephemeris #    2 !!",
        "   -2327   -1517      Ephemeris #    3 !!",
        "  +11175   +7757      Ephemeris #    4 !!",
    ])
    got = [(int(a), int(b)) for a, b in ephemeris._OFFSET_RE.findall(page)]
    check("all four flagged lines parse", len(got) == 4, got)
    check("values are read correctly",
          got[:2] == [(0, 0), (6581, 4822)], got[:2])

    # The unflagged and single-! forms must keep working.
    for line, want in (
            ("   +6581   +4822      Ephemeris #    2", (6581, 4822)),
            ("   +6581   +4822      Ephemeris #    2 !", (6581, 4822)),
            ("   -2327   -1517      Ephemeris #    3 !! ", (-2327, -1517))):
        m = ephemeris._OFFSET_RE.findall(line)
        check("parses %r" % line[-12:].strip(),
              m and (int(m[0][0]), int(m[0][1])) == want, m)

    # Header and decoration must still be ignored.
    check("a line without an ephemeris number is ignored",
          ephemeris._OFFSET_RE.findall("   +1   +2   some other text") == [])

    spread = ephemeris.spread(got)
    check("spread over the real ZTF10G9 sample is enormous",
          spread == (13502, 9274), spread)
    check("and a cloud that size dwarfs the telescope's field",
          spread[0] > config.FOV_ARCSEC * 4,
          "%d\" vs %d\" field" % (spread[0], config.FOV_ARCSEC))


def test_cache_schema_forces_one_refetch():
    """Bumping the payload schema must invalidate every cached entry once.

    Without it a parser fix never reaches objects already cached: their
    signature does not change until new astrometry arrives, so they keep
    serving the wrong result indefinitely.
    """
    now = 1_760_000_000.0
    target = {"desig": "T1", "nobs": 12, "arc_days": 0.5}
    sig = ephemeris.signature(target)
    current = {"offsets": [], "obs_codes": {}, "fetched_ts": now - 600,
               "last_row_ts": now + 3600,
               "cache_schema": ephemeris.CACHE_SCHEMA}
    check("an entry at the current schema is left alone",
          not update_neocp.needs_refetch((sig, current), target, now))

    old = dict(current, cache_schema=ephemeris.CACHE_SCHEMA - 1)
    check("an entry at an older schema is refetched",
          update_neocp.needs_refetch((sig, old), target, now))

    missing = {k: v for k, v in current.items() if k != "cache_schema"}
    check("an entry predating the field entirely is refetched",
          update_neocp.needs_refetch((sig, missing), target, now))

    # Adding a payload key without bumping the schema is the failure this
    # guards against, and it is silent: an entry cached at the old schema
    # passes every other test, is never refetched, and simply lacks the new
    # data forever. Measured live when gap_fill_lines was introduced -- 106 of
    # 114 objects kept a gapped altitude plot with nothing to indicate why.
    at_2 = dict(current, cache_schema=2)
    check("an entry cached before gap_fill_lines existed is refetched",
          update_neocp.needs_refetch((sig, at_2), target, now),
          "schema is %d; a payload at 2 has no gap_fill_lines"
          % ephemeris.CACHE_SCHEMA)
    check("the schema is past 2, so gap-fill data actually reaches objects",
          ephemeris.CACHE_SCHEMA > 2, ephemeris.CACHE_SCHEMA)

    # Every key _aux writes should be one the staleness test knows about,
    # either by name or by the schema having moved since it was added.
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "update_neocp.py")).read()
    aux = src.split("def _aux")[1].split("new_cache = {}")[0]
    written = set(re.findall(r'^\s+"(\w+)":', aux, re.M))
    expected = {"lines", "gap_fill_lines", "fetched_ts", "last_row_ts",
                "cache_schema", "offsets_url", "map_url", "observations_url",
                "offsets", "scatteredness", "observed_from_site",
                "discovery_code", "obs_codes", "obs_records", "error"}
    check("the payload's keys are the ones this test knows about",
          written == expected, sorted(written ^ expected))

    # Same guard again for obs_records, and it matters more here than it did
    # for gap_fill_lines: a gapped altitude plot can be repaired by refetching
    # whenever anyone notices, but astrometry cannot. MPC stops serving an
    # object's records once it leaves NEOCP, and 17 of 77 objects left
    # overnight between the two nights measured in ds42.md. An entry that
    # never refetches is a night of astrometry lost permanently.
    at_3 = dict(current, cache_schema=3)
    check("an entry cached before obs_records existed is refetched",
          update_neocp.needs_refetch((sig, at_3), target, now),
          "schema is %d; a payload at 3 has no obs_records"
          % ephemeris.CACHE_SCHEMA)
    check("the schema is past 3, so astrometry actually reaches objects",
          ephemeris.CACHE_SCHEMA > 3, ephemeris.CACHE_SCHEMA)


PREVDES_FIXTURE = """
<html><body><a name="prev"></a>
<ul>
<li>2026 RR<sub>39</sub> = 6JD1C21 (Sept. 16.48 UT) [see <a
    href="/mpec/K26/K26S09.html">MPEC 2026-S09</a>]
<li>2026 RS<sub>39</sub> = P22pRQ8 (Sept. 16.49 UT)
<li>ZTF10GC = SK000cT (Sept. 16.62 UT)
<li>A11GXkI was not a minor planet (Sept. 16.30 UT)
<li>S06001 was not confirmed (Sept. 15.90 UT)
<li>P99xxxx does not exist (Sept. 15.10 UT)
<li>Q88yyyy was suspected artificial (Sept. 14.80 UT)
</ul></body></html>
"""


def test_prevdes_parsing_keeps_what_ground_truth_needs():
    """MPC's archive, parsed for more than "it resolved".

    "confirmed" alone cannot answer the question ds42 is asked: a main-belt
    asteroid that lands on NEOCP is confirmed too. The permanent designation
    is what lets an orbit be looked up later and the object classified, so
    losing it means the archive records that something resolved without
    recording what it was.

    Note which side is which. An entry reads "2026 RR39 = 6JD1C21" -- the
    permanent designation on the LEFT, the NEOCP tracklet on the right -- and
    an entry can also link two tracklets with no permanent designation at all.
    """
    import neocp_history as H

    got = H.parse_prevdes(PREVDES_FIXTURE)

    check("a designated object keeps its designation",
          got["6JD1C21"]["linked_desig"] == "2026 RR39", got.get("6JD1C21"))
    check("and its MPEC", got["6JD1C21"]["mpec"] == "MPEC 2026-S09")
    check("the designation is found however the entry is ordered",
          got["P22pRQ8"]["linked_desig"] == "2026 RS39")
    check("an entry with no MPEC still resolves",
          got["P22pRQ8"]["status"] == "confirmed"
          and got["P22pRQ8"]["mpec"] is None)
    check("the permanent designation is itself lookupable",
          got["2026 RR39"]["status"] == "confirmed")

    # Two tracklets of the same object. Confirmed, but nothing is designated,
    # so there is no orbit to look up and no NEO verdict to reach.
    check("a tracklet merge confirms both sides",
          got["ZTF10GC"]["status"] == "confirmed"
          and got["SK000cT"]["status"] == "confirmed")
    check("but claims no designation for either",
          got["ZTF10GC"]["linked_desig"] is None
          and got["SK000cT"]["linked_desig"] is None)

    for d, st in (("A11GXkI", "not_minor_planet"), ("S06001", "not_confirmed"),
                  ("P99xxxx", "does_not_exist"),
                  ("Q88yyyy", "suspected_artificial")):
        check("%s -> %s" % (d, st), got[d]["status"] == st, got.get(d))
    check("a negative outcome designates nothing",
          all(got[d]["linked_desig"] is None
              for d in ("A11GXkI", "S06001", "P99xxxx", "Q88yyyy")))

    check("a page with no archive list yields nothing, rather than raising",
          H.parse_prevdes("<html><body>nothing here</body></html>") == {})


def test_archived_run_import():
    """Reconstructing past polls from the ds42 run archive.

    The history poller only started when it was deployed, and MPC keeps no
    archive of past NEOCP listings -- so those nights exist only where
    something else happened to record them. The ds42 runs did.

    Two daily samples are not five-minute polling, which is why every row is
    flagged: anything reasoning about cadence, or reading a column the
    archive never held, has to be able to exclude them.
    """
    import tempfile

    import neocp_history as H

    prev = H.DB_PATH
    H.DB_PATH = os.path.join(tempfile.mkdtemp(), "neocp_history.db")
    try:
        con = H.ensure_db()
        cols = {r[1] for r in con.execute("PRAGMA table_info(snapshots)")}
        check("snapshots can be flagged retrospective",
              "retrospective" in cols)

        meta = {"P12aaaa": {"score": 100, "vmag": 20.1, "nobs": 3, "arc": 0.02},
                "P12bbbb": {"score": 77, "vmag": 21.4, "nobs": 6, "arc": 0.05}}
        ts = "2026-09-15T01:19:47+00:00"
        rows, tracked = H.import_archived_run(con, "2026-09-15", meta, ts)
        check("one snapshot per object", rows == 2, rows)
        check("and each is tracked as an object", tracked == 2, tracked)

        got = con.execute("SELECT desig, score, vmag, nobs, arc_days, "
                          "retrospective, ra_deg FROM snapshots "
                          "ORDER BY desig").fetchall()
        check("what the archive held comes across",
              got[0][1] == 100 and abs(got[0][2] - 20.1) < 1e-9
              and got[0][3] == 3)
        check("every row is flagged retrospective",
              all(r[5] == 1 for r in got))
        check("a column the archive never held stays NULL",
              all(r[6] is None for r in got))

        # Re-running must not double the history.
        again = H.import_archived_run(con, "2026-09-15", meta, ts)
        check("importing the same night twice is a no-op", again == (0, 0),
              again)
        check("still one poll", con.execute(
            "SELECT count(DISTINCT snapshot_ts) FROM snapshots"
        ).fetchone()[0] == 1)

        # A later night must extend last_seen, not rewrite first_seen.
        later = "2026-09-16T14:34:00+00:00"
        H.import_archived_run(con, "2026-09-16", {"P12aaaa": meta["P12aaaa"]},
                              later)
        row = con.execute("SELECT first_seen_ts, last_seen_ts FROM objects "
                          "WHERE desig='P12aaaa'").fetchone()
        check("first seen stays at the earliest night", row[0] == ts, row)
        check("last seen moves to the latest", row[1] == later, row)
        check("two polls now", con.execute(
            "SELECT count(DISTINCT snapshot_ts) FROM snapshots"
        ).fetchone()[0] == 2)

        # Out of order: an older night imported afterwards must still win
        # first_seen, or an object looks younger than it is.
        older = "2026-09-14T00:00:00+00:00"
        H.import_archived_run(con, "2026-09-14", {"P12aaaa": meta["P12aaaa"]},
                              older)
        row = con.execute("SELECT first_seen_ts, last_seen_ts FROM objects "
                          "WHERE desig='P12aaaa'").fetchone()
        check("an older night imported later still wins first_seen",
              row[0] == older and row[1] == later, row)
        con.close()
    finally:
        H.DB_PATH = prev


def test_history_page_survives_missing_columns():
    """A NULL in any polled column must not take /history down.

    NEOCP omits columns for an object it knows little about, and a snapshot
    reconstructed from an archive carries only the fields that archive kept --
    a retrospective row has no not_seen_days at all. Jinja's format filter
    raises TypeError on None, so one missing number became a 500 for the whole
    page. It did, in production, the moment the archived runs were imported.
    """
    import importlib
    import tempfile

    import app as appmod
    import neocp_history as H

    prev_db, prev_hist = config.DB_PATH, H.DB_PATH
    d = tempfile.mkdtemp()
    config.DB_PATH = os.path.join(d, "targets.db")
    H.DB_PATH = os.path.join(d, "neocp_history.db")
    try:
        importlib.reload(appmod)
        con = H.ensure_db()
        now = H._now()
        # One fully-populated object, and one with every optional column NULL
        # -- which is exactly the shape import_archived_run writes.
        con.execute("INSERT INTO objects (desig, first_seen_ts, last_seen_ts) "
                    "VALUES ('FULL', ?, ?)", (now, now))
        con.execute("INSERT INTO objects (desig, first_seen_ts, last_seen_ts, "
                    "retrospective) VALUES ('SPARSE', ?, ?, 1)", (now, now))
        con.execute("INSERT INTO snapshots (desig, snapshot_ts, score, vmag, "
                    "nobs, arc_days, not_seen_days) "
                    "VALUES ('FULL', ?, 100, 20.1, 5, 0.03, 0.4)", (now,))
        con.execute("INSERT INTO snapshots (desig, snapshot_ts, score, vmag, "
                    "nobs, arc_days, retrospective) "
                    "VALUES ('SPARSE', ?, 77, 21.4, 6, 0.05, 1)", (now,))
        con.commit()
        con.close()

        c = appmod.app.test_client()
        r = c.get("/history")
        check("/history renders with a NULL in a formatted column",
              r.status_code == 200, r.status_code)
        check("/history/rows too", c.get("/history/rows").status_code == 200)
        body = r.data.decode()
        check("the sparse row is shown, not skipped", "SPARSE" in body)
        check("and its missing value reads as a dash", "&mdash;" in body
              or "\u2014" in body)
        check("the complete row still formats normally", "0.40" in body)
    finally:
        config.DB_PATH, H.DB_PATH = prev_db, prev_hist
        importlib.reload(appmod)


def test_history_backfill_and_resolution():
    """Backfill recovers labels that already exist, and never overwrites.

    ds42 scored objects before this history existed, and MPC's archive still
    lists their outcomes -- so those labels are recoverable rather than lost.
    But a resolved_at means "when we learned this", so a later pass must not
    rewrite it.
    """
    import tempfile

    import neocp_history as H

    prev = H.DB_PATH
    H.DB_PATH = os.path.join(tempfile.mkdtemp(), "neocp_history.db")
    try:
        con = H.ensure_db()
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
        check("the history database uses WAL, like targets.db",
              mode.lower() == "wal", mode)

        cols = {r[1] for r in con.execute("PRAGMA table_info(objects)")}
        for c in ("linked_desig", "mpec", "is_neo", "perihelion_au",
                  "retrospective"):
            check("objects has %s" % c, c in cols)

        scored = ["6JD1C21", "P22pRQ8", "A11GXkI", "P12unknown"]
        entries = H.parse_prevdes(PREVDES_FIXTURE)
        inserted = 0
        now = H._now()
        known = {r[0] for r in con.execute("SELECT desig FROM objects")}
        for d in scored:
            if d not in known:
                con.execute("INSERT INTO objects (desig, first_seen_ts, "
                            "last_seen_ts, status, retrospective) "
                            "VALUES (?,?,?,'pending',1)", (d, now, now))
                inserted += 1
        con.commit()
        check("every scored object is tracked", inserted == 4)

        n = H.apply_resolutions(con, entries, now)
        check("the three the archive knows are resolved", n == 3, n)

        rows = {r[0]: r for r in con.execute(
            "SELECT desig, status, linked_desig, mpec, resolved_at, "
            "retrospective FROM objects")}
        check("a designated object records what it became",
              rows["6JD1C21"][2] == "2026 RR39" and rows["6JD1C21"][3]
              == "MPEC 2026-S09")
        check("a negative outcome is recorded as such",
              rows["A11GXkI"][1] == "not_minor_planet")
        check("an object the archive has never heard of stays pending",
              rows["P12unknown"][1] == "pending"
              and rows["P12unknown"][4] is None)
        check("backfilled rows are flagged retrospective",
              all(rows[d][5] == 1 for d in scored))

        # Re-running must be inert: resolved_at is when we learned, not when
        # we last looked.
        first_seen_at = rows["6JD1C21"][4]
        again = H.apply_resolutions(con, entries, "2099-01-01T00:00:00+00:00")
        after = con.execute("SELECT resolved_at FROM objects WHERE desig="
                            "'6JD1C21'").fetchone()[0]
        # Zero, not three: the default pass only looks at objects still
        # pending, so already-resolved ones are not even considered.
        check("a second pass resolves nothing new", again == 0, again)
        check("and does not rewrite when we learned it",
              after == first_seen_at, (first_seen_at, after))
        con.close()
    finally:
        H.DB_PATH = prev


def test_history_bulk_download_needs_an_account():
    """The dashboard and CSV stay open; the whole database does not.

    Reads are open on this board and should stay open, but one row per object
    per five minutes grows to gigabytes, and an anonymous endpoint handing out
    the entire file is a bandwidth commitment rather than a read. Nothing in
    it is secret -- it is all public MPC data -- so this is about cost.
    """
    import importlib
    import tempfile

    import app as appmod
    import auth

    prev_db, prev_env = config.DB_PATH, os.environ.pop("WHICHNEO_AUTH", None)
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    try:
        importlib.reload(auth)
        importlib.reload(appmod)
        c = appmod.app.test_client()
        check("the dashboard is open to anyone",
              c.get("/history").status_code == 200)
        check("so is the CSV",
              c.get("/history/download.csv").status_code == 200)
        check("the whole database is not",
              c.get("/history/download.db").status_code in (302, 401),
              c.get("/history/download.db").status_code)

        conn = db.connect(config.DB_PATH)
        db.init(conn)
        uid = db.create_user(conn, "obs", auth.hash_password("x" * 12))
        conn.close()
        with c.session_transaction() as s:
            s["uid"] = uid
        check("but it is once you are signed in",
              c.get("/history/download.db").status_code == 200)
    finally:
        config.DB_PATH = prev_db
        if prev_env is not None:
            os.environ["WHICHNEO_AUTH"] = prev_env
        importlib.reload(auth)
        importlib.reload(appmod)


def test_ds42_column():
    """The column, and the three states it has to keep apart.

    A posterior; a scored object with no posterior; and one never scored.
    Collapsing the last two would hide whether ds42 has an opinion or has
    simply not looked -- and roughly a third of the board is never looked at,
    because objects rejected before their astrometry is fetched never reach
    ds42 at all.

    Also checks the tooltip, which is not decoration here: p_neo is scored
    from the discovery tracklet and does not move as follow-up arrives, so a
    frozen number beside a moving digest2 score reads as a bug unless the
    column says what it is.
    """
    import importlib
    import tempfile

    import app as appmod

    prev_db = config.DB_PATH
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    try:
        conn = db.connect(config.DB_PATH)
        db.init(conn)
        conn.execute(
            "INSERT INTO targets (desig, score, vmag, observable, not_seen_days,"
            " score_total, discard_reasons) VALUES "
            "('SCORED', 77, 21.4, 1, 0.5, 3.1, '[]'),"
            "('UNDEF',  100, 21.0, 1, 0.5, 3.0, '[]'),"
            "('NEVER',  40, 20.0, 1, 0.5, 2.0, '[]')")
        conn.commit()
        db.save_ds42_scores(conn, {
            "SCORED": {"p_neo": 0.0642, "log_lr": -2.0, "status": "ok",
                       "n_obs": 3, "arc_h": 0.23, "obscode": "F51",
                       "vmag": 21.4},
            "UNDEF": {"p_neo": None, "log_lr": None, "status": "empty_region",
                      "n_obs": 4, "arc_h": 0.14, "obscode": "I41",
                      "vmag": 21.0},
        }, {"ds42_rev": "a4bc848", "ds42_dirty": False,
            "model_sha256": "2b9d9d2a", "config": {}})
        conn.close()

        rows = {r["desig"]: r for r in db.load_targets(
            db.connect(config.DB_PATH), include_observed=True)}
        check("a posterior reaches the row", rows["SCORED"]["p_neo"] == 0.0642)
        check("so does the status that qualifies it",
              rows["SCORED"]["ds42_status"] == "ok"
              and rows["SCORED"]["ds42_n_obs"] == 3)
        check("an undefined posterior is None with a status",
              rows["UNDEF"]["p_neo"] is None
              and rows["UNDEF"]["ds42_status"] == "empty_region")
        check("an unscored object has neither",
              rows["NEVER"]["p_neo"] is None
              and rows["NEVER"]["ds42_status"] is None)

        importlib.reload(appmod)
        page = appmod.app.test_client().get("/").data.decode()

        check("the header is there", ">ds42<" in page)
        check("the header explains it is the discovery tracklet",
              "DISCOVERY TRACKLET" in page)
        check("and that it does not move with follow-up",
              "does not change as follow-up" in page)
        check("the posterior renders to three decimals", ">0.064<" in page)
        check("its tooltip carries the full value and the count",
              "0.0642" in page and "3 observation(s)" in page)
        check("an undefined posterior is a dash, not a number",
              "could not compute a posterior: empty_region" in page)
        check("and says what empty_region means",
              "no bound orbit is consistent" in page)
        check("an unscored object is distinguishable from an undefined one",
              "Not scored by ds42" in page)

        # Sortable, using the same machinery as every other column.
        check("ds42 is a sortable column", "ds42" in ranking.SORTABLE_COLUMNS)
        check("it sorts on p_neo",
              ranking.SORTABLE_COLUMNS["ds42"]["field"] == "p_neo")
        ordered = ranking.sort_targets(list(rows.values()), "ds42_asc")
        vals = [r["p_neo"] for r in ordered if r["p_neo"] is not None]
        check("ascending really is ascending", vals == sorted(vals), vals)
        check("unscored objects sink rather than sorting as zero",
              ordered[-1]["p_neo"] is None)
        check("/?sort=ds42_desc renders",
              appmod.app.test_client().get(
                  "/?sort=ds42_desc").status_code == 200)

        # The table must not go ragged when a column is added.
        body = page.split('<tbody id="rows">')[1].split("</tbody>")[0]
        first = body.split("<tr")[1]
        head = page.split("<thead>")[1].split("</thead>")[0]
        check("header and body cell counts agree",
              head.count("<th") == first.count("<td"),
              (head.count("<th"), first.count("<td")))
    finally:
        config.DB_PATH = prev_db
        importlib.reload(appmod)


def test_ds42_score_parsing():
    """ds42's TSV, including the rows that are not a posterior.

    p_neo comes back as `nan` for an undefined result, and a nan must not
    reach storage: it survives into SQLite and JSON, and compares false
    against itself, so a stored nan is a value no query can ever match again.
    """
    import ds42score

    tsv = ("object_id\tn_obs\tarc_h\tepoch\tobscode\tV\tp_neo\tlog_lr\tstatus\n"
           "P12aaaa\t3\t0.2993\t2026-09-13T00:06:47Z\tW94\t19.94\t1\tinf\tok\n"
           "P12bbbb\t4\t0.4400\t2026-09-13T01:00:00Z\tF51\t20.10\t0.3058\t-1.2\tok\n"
           "ZTF10G2\t4\t0.1400\t2026-09-13T02:00:00Z\tI41\t21.00\tnan\tnan\tempty_region\n")
    got = ds42score._parse_scores(tsv)

    check("every row parses", sorted(got) == ["P12aaaa", "P12bbbb", "ZTF10G2"],
          sorted(got))
    check("a posterior survives intact",
          abs(got["P12bbbb"]["p_neo"] - 0.3058) < 1e-9)
    check("integer-looking p_neo becomes a float",
          got["P12aaaa"]["p_neo"] == 1.0)
    check("an undefined posterior stores None, never nan",
          got["ZTF10G2"]["p_neo"] is None and got["ZTF10G2"]["log_lr"] is None)
    check("its status says why",
          got["ZTF10G2"]["status"] == "empty_region")
    check("the observations ds42 actually used are recorded",
          got["P12bbbb"]["n_obs"] == 4 and abs(got["P12bbbb"]["arc_h"] - 0.44)
          < 1e-9)
    check("the observatory comes across", got["P12aaaa"]["obscode"] == "W94")

    check("a truncated row is skipped, not guessed at",
          ds42score._parse_scores(
              "object_id\tn_obs\tp_neo\nP12cccc\t3\n") == {})
    check("empty input is empty output", ds42score._parse_scores("") == {})


def test_ds42_never_breaks_an_update_cycle():
    """Everything about ds42 is best effort, and this proves it.

    The board's job is telling an observer where to point a telescope. A
    research score is not worth risking that, which is why ds42 runs as a
    subprocess rather than an import -- and why every failure below has to
    come back as an empty result rather than an exception.
    """
    import tempfile

    import ds42score

    prev = (config.DS42_ENABLED, config.DS42_BIN, config.DS42_MODEL,
            config.DS42_TIMEOUT_S)
    recs = {"P12aaaa": ["x" * 80]}
    try:
        config.DS42_ENABLED = False
        check("disabled means unavailable", not ds42score.available())
        check("and scoring is a no-op, not an error",
              ds42score.score_records(recs) == {})

        config.DS42_ENABLED = True
        config.DS42_BIN = "/nonexistent/ds42"
        check("a missing binary is unavailable", not ds42score.available())
        check("and still returns nothing rather than raising",
              ds42score.score_records(recs) == {})

        config.DS42_BIN = prev[1]
        config.DS42_MODEL = "/nonexistent/model.csv"
        check("a missing model is unavailable", not ds42score.available())

        config.DS42_MODEL = prev[2]
        # A binary that exists and fails, which is the case no amount of
        # existence-checking catches.
        broken = os.path.join(tempfile.mkdtemp(), "ds42")
        with open(broken, "w") as f:
            f.write("#!/bin/sh\necho 'model parse failed' >&2\nexit 3\n")
        os.chmod(broken, 0o755)
        config.DS42_BIN = broken
        check("a non-zero exit is handled, not raised",
              ds42score.score_records(recs) == {})

        # And one that hangs.
        hangs = os.path.join(tempfile.mkdtemp(), "ds42")
        with open(hangs, "w") as f:
            f.write("#!/bin/sh\nsleep 30\n")
        os.chmod(hangs, 0o755)
        config.DS42_BIN = hangs
        config.DS42_TIMEOUT_S = 2
        check("a hang is bounded by the timeout",
              ds42score.score_records(recs) == {})

        check("no records means no subprocess at all",
              ds42score.score_records({}) == {}
              and ds42score.score_records({"P12aaaa": []}) == {})
    finally:
        (config.DS42_ENABLED, config.DS42_BIN, config.DS42_MODEL,
         config.DS42_TIMEOUT_S) = prev


def test_ds42_scores_are_stored_once_and_survive_pruning():
    """The table has to outlive the things that get rewritten around it.

    targets is rebuilt every cycle and prune_cache drops an object the moment
    it leaves NEOCP -- which is exactly when its score becomes interesting,
    because that is when we can finally ask whether ds42 was right. A score
    kept in either place would be deleted at that moment.
    """
    import tempfile

    prev_db = config.DB_PATH
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    try:
        conn = db.connect(config.DB_PATH)
        db.init(conn)

        prov = {"ds42_rev": "a4bc848", "ds42_dirty": False,
                "model_sha256": "2b9d9d2a", "config": {"variant": "All"}}
        scores = {
            "P12aaaa": {"p_neo": 0.75, "log_lr": 1.1, "status": "ok",
                        "n_obs": 3, "arc_h": 0.3, "obscode": "W94",
                        "vmag": 20.1},
            "ZTF10G2": {"p_neo": None, "log_lr": None,
                        "status": "empty_region", "n_obs": 4, "arc_h": 0.14,
                        "obscode": "I41", "vmag": 21.0},
        }
        check("both scores are written", db.save_ds42_scores(conn, scores, prov) == 2)
        check("counted", db.count_ds42_scores(conn) == 2)

        got = db.load_ds42_scores(conn)
        check("the posterior round-trips", got["P12aaaa"]["p_neo"] == 0.75)
        check("an undefined one stays NULL, not nan",
              got["ZTF10G2"]["p_neo"] is None)
        check("provenance is stored with the score",
              got["P12aaaa"]["ds42_rev"] == "a4bc848"
              and got["P12aaaa"]["model_sha256"] == "2b9d9d2a"
              and got["P12aaaa"]["ds42_dirty"] == 0)
        check("the configuration is stored too",
              json.loads(got["P12aaaa"]["config_json"]) == {"variant": "All"})

        # Scoring is one-shot. Re-running must not spend the model load, and
        # must not let a different revision overwrite a recorded one.
        check("already-scored objects are filtered out",
              db.unscored_desigs(conn, ["P12aaaa", "ZTF10G2", "P12new"])
              == ["P12new"])
        newer = {"P12aaaa": {"p_neo": 0.01, "status": "ok", "n_obs": 9,
                             "log_lr": 0, "arc_h": 1, "obscode": "F51",
                             "vmag": 20.0}}
        db.save_ds42_scores(conn, newer, dict(prov, ds42_rev="deadbee"))
        after = db.load_ds42_scores(conn)
        check("a second write never overwrites the first",
              after["P12aaaa"]["p_neo"] == 0.75
              and after["P12aaaa"]["ds42_rev"] == "a4bc848")

        # The point of the separate table.
        conn.execute("INSERT INTO targets (desig) VALUES ('P12aaaa')")
        conn.commit()
        db.replace_targets(conn, [])
        db.prune_cache(conn, [])
        check("scores survive targets being rewritten and the cache pruned",
              db.count_ds42_scores(conn) == 2)

        check("an empty batch writes nothing",
              db.save_ds42_scores(conn, {}, prov) == 0)
        check("asking about nothing returns nothing",
              db.unscored_desigs(conn, []) == []
              and db.load_ds42_scores(conn, []) == {})
        conn.close()
    finally:
        config.DB_PATH = prev_db


def test_frame_speed_table():
    """The observatory's own speed table, as given by Luka.

      0-5 "/min -> 30s, 5-25 -> 15s, 25-50 -> 10s,
      50-100    ->  5s, 100-200 -> 2s, 200+  ->  1s

    Upper bound inclusive. These are someone else's numbers for protecting
    someone else's detector, so they are pinned literally rather than derived.
    """
    def secs(motion):
        r = ephemeris.Row(SAMPLE_EPH)
        r.motion = motion
        return r.frame_seconds()

    for motion, expected in [
            (0.0, 30), (1.7, 30), (4.99, 30),
            (5.0, 30),                      # boundary: upper bound INCLUSIVE
            (5.01, 15), (14.6, 15), (25.0, 15),
            (25.01, 10), (43.4, 10), (50.0, 10),
            (50.01, 5), (64.5, 5), (100.0, 5),
            (100.01, 2), (179.1, 2), (200.0, 2),
            (200.01, 1), (231.0, 1), (5000.0, 1)]:
        check(f"{motion:>8.2f} \"/min -> {expected:2d} s", secs(motion) == expected,
              f"got {secs(motion)}")

    check("every band boundary takes the SHORTER-speed band",
          secs(5.0) == 30 and secs(25.0) == 15 and secs(200.0) == 2)

    r = ephemeris.Row(SAMPLE_EPH)
    r.motion = 8.5
    check("frame count is fixed for every target",
          r.frame_plan() == (config.EXPOSURE_FRAMES, 15), r.frame_plan())

    # Real values from the night this was specified.
    for desig, motion, expected in [("P22pZo5", 5.03, 15), ("A11GP9t", 64.45, 5),
                                    ("ZTF10G9", 179.1, 2), ("P12pRQk", 1.0, 30)]:
        check(f"{desig} at {motion} \"/min -> {expected} s",
              secs(motion) == expected, f"got {secs(motion)}")


def test_plan_line_carries_the_frame_instruction():
    """Luka's format, literally: `* DESIG 36 x 02 sec score=...`"""
    row = ephemeris.Row(SAMPLE_EPH)
    t = dict(desig="ZTF10G9", score=100, nobs=4, arc_days=0.02,
             not_seen_days=0.667, exposure_min=9.0, frames=36, frame_sec=2,
             max_alt_row=row, nearest_row=row, interp_row=row,
             live_row_is_now=True)
    first = output.plan_entry(t).splitlines()[0]

    check("frames and seconds sit between the designation and score",
          first.startswith("* ZTF10G9 36 x 02 sec score=100,"), first)
    check("seconds are zero-padded to two digits",
          " x 02 sec " in first, first)
    check("obsExposure is still present and untouched",
          "obsExposure=9.0min" in first, first)

    t1 = dict(t, frame_sec=30)
    check("a two-digit value is not padded further",
          output.plan_entry(t1).splitlines()[0].startswith("* ZTF10G9 36 x 30 sec"),
          output.plan_entry(t1).splitlines()[0])

    # Without a frame plan the old layout is kept rather than emitting a
    # half-written instruction.
    t2 = dict(t, frames=None, frame_sec=None)
    check("no frame plan falls back to the original line",
          output.plan_entry(t2).splitlines()[0].startswith("* ZTF10G9    "),
          output.plan_entry(t2).splitlines()[0])

    check("the frame fields are stored for the board",
          all(c in db._COLS for c in ("frames", "frame_sec", "frame_motion")))


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
def test_plan_sync_button_and_the_toolbar_it_merged_past():
    """The plan-sync control renders, and nothing it merged past regressed.

    This branch was cut 21 commits back and its version of the toolbar still
    had the one-way `hidden=1` chip that #25 fixed, plus the plan-file link
    that #25 moved into the header. Resolving that hunk by taking its side
    would have quietly undone both -- a chip that can be clicked on and never
    off, and two plan-file links. Neither raises; both just look like someone
    else's styling bug. Hence checking for them here rather than trusting the
    merge.
    """
    import importlib
    import tempfile

    import app as appmod

    prev_db = config.DB_PATH
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    try:
        importlib.reload(appmod)
        c = appmod.app.test_client()
        page = c.get("/").data.decode()

        check("the sync button renders", 'id="planSyncBtn"' in page)
        check("so does its status label", 'id="planSyncStatus"' in page)
        check("it sits with the plan file it syncs",
              page.index('class="plan-btn"') < page.index('id="planSyncBtn"')
              < page.index('class="live"'))

        # Four call sites, all wanted: on connect, on resume, on load, and
        # once per poll. A fifth would mean the refresh hook got duplicated
        # by the merge, which would double every write.
        check("syncPlanNow is called from exactly four places",
              page.count("await syncPlanNow();") == 4,
              page.count("await syncPlanNow();"))

        # The regressions this merge could have reintroduced.
        check("exactly one plan-file link on the page",
              len(re.findall(r'<a[^>]+href="/plan"', page)) == 1,
              re.findall(r'<a[^>]+href="/plan"', page))
        # The round trip, not the markup. A page with the chip OFF correctly
        # contains hidden=1 -- that is the link that turns it on -- so
        # grepping for that string proves nothing. What the old bug got wrong
        # is the other direction: once on, it still pointed at hidden=1 and
        # there was no way back.
        def chip_link(html):
            m = re.search(r'<a class="chip[^"]*"[^>]*href="([^"]+)"[^>]*>hidden<',
                          html)
            return m.group(1) if m else None

        off_link = chip_link(page)
        on_link = chip_link(c.get("/?hidden=1").data.decode())
        check("off, the chip offers to turn hidden on",
              off_link is not None and "hidden=1" in off_link, off_link)
        check("on, the chip offers to turn it back off",
              on_link is not None and "hidden=0" in on_link, on_link)

        check("the plan route it syncs from still exists",
              c.get("/plan").status_code in (200, 404))
    finally:
        config.DB_PATH = prev_db
        importlib.reload(appmod)


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


def _live_db_fingerprint():
    """What the deployment's database looks like, for before/after comparison.

    Returns None when there is no database yet -- a fresh clone, which is the
    normal case off the observatory host.
    """
    import hashlib

    path = config.DB_PATH
    if not os.path.exists(path):
        return None
    conn = db.connect(path)
    try:
        counts = {}
        for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "ORDER BY name"):
            counts[name] = conn.execute(f"SELECT count(*) FROM '{name}'"
                                        ).fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM observer_state ORDER BY desig, user_id").fetchall()
        digest = hashlib.sha256(
            repr([tuple(r) for r in rows]).encode()).hexdigest()[:16]
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return counts, digest


def main():
    # The suite runs on epyc, where config.DB_PATH is the observatory's live
    # database. For years one test marked /mark/XYZ straight into it. Nothing
    # caught that, because a stray row in observer_state is invisible: the
    # board joins from `targets`, so a designation that was never on the
    # NEOCP simply never renders. This is what catches it.
    live_before = _live_db_fingerprint()

    for fn in (test_site, test_horizon_mask, test_analytic_matches_astropy,
               test_neocp_list_parse, test_neocp_info_column_collision,
               test_ephemeris_row, test_ephemeris_azimuth_convention,
               test_exposure_rule, test_night_bounds, test_chronological_sort,
               test_upcoming_cards_look_forward, test_cache_signature_ignores_the_clock,
               test_plan_file_survives_dawn, test_mpc_markers,
               test_observatory_identification, test_uncertainty_plot,
               test_auth_protects_state_changes,
               test_accounts, test_observer_state_is_per_account,
               test_observer_state_migration_keeps_every_mark,
               test_password_reset_by_email,
               test_invite_creates_an_account_nobody_can_sign_into,
               test_invitations_outlive_a_reset_link,
               test_mailer_builds_a_sane_message,
               test_login_throttle,
               test_secret_key_is_stable_and_private,
               test_schema_migration_from_older_db,
               test_ranking_bounds, test_row_rejection_reasons,
               test_replay_ts_never_takes_the_map_down,
               test_night_archive_survives_per_account_state,
               test_archived_replay_endpoint,
               test_skymap_orientation, test_skymap_mask_wedges,
               test_moon_exclusion_locus, test_skymap_marks,
               test_priority_bump_is_fully_gone,
               test_offsets_parse_with_the_fast_motion_flag,
               test_cache_schema_forces_one_refetch,
               test_prevdes_parsing_keeps_what_ground_truth_needs,
               test_archived_run_import,
               test_history_page_survives_missing_columns,
               test_history_backfill_and_resolution,
               test_history_bulk_download_needs_an_account,
               test_ds42_column,
               test_ds42_score_parsing,
               test_ds42_never_breaks_an_update_cycle,
               test_ds42_scores_are_stored_once_and_survive_pruning,
               test_frame_speed_table,
               test_plan_line_carries_the_frame_instruction,
               test_keepout_wedge,
               test_plan_file_never_publishes_a_stale_pointing_line,
               test_altitude_plot,
               test_moon_phase_geometry, test_ephemeris_track_matches_row,
               test_plan_sync_button_and_the_toolbar_it_merged_past,
               test_done_targets_stay_in_the_list,
               test_done_target_is_green_on_the_sky_map,
               test_interpolate_clamps_to_the_right_end,
               test_ephemeris_refetched_when_its_window_runs_out):
        print(f"\n{fn.__name__}:")
        fn()

    print("\nno_test_touched_the_live_database:")
    if live_before is None:
        check("no deployment database here to protect", True,
              "config.DB_PATH does not exist")
    else:
        after = _live_db_fingerprint()
        check("the deployment's tables are unchanged",
              after is not None and after[0] == live_before[0],
              f"{live_before[0]} -> {after[0] if after else None}")
        check("no observer's marks were added, changed or removed",
              after is not None and after[1] == live_before[1])

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
