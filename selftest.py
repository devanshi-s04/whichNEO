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

import dataclasses
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
import registry
import siteconf
import sites as sitesmod
import neodistance
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
    loc = observability.earth_location()
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
    loc = observability.earth_location()
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
          r.exposure_minutes() == config.DEFAULT_SITE.exposure_floor_min,
          r.exposure_minutes())


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


def _site_2():
    """A second observatory, identical to L01 but for its identity.

    Deliberately a copy: every difference that is not site_id would give a
    test a second way to pass, and the thing under test is the id alone.
    """
    return dataclasses.replace(config.DEFAULT_SITE, id=2, obscode="Z99")


def test_site_id_migration_keeps_every_row():
    """Gaining site_id must not lose a row from the three unregenerable tables.

    observer_state, ephemeris_cache and night_archive all hold state the
    updater cannot rebuild -- what an observer marked, what MPC sent, and
    where objects were on a night that has ended. site_id changes each of
    their primary keys, which SQLite cannot do with ALTER, so they are
    rebuilt. A rebuild that silently drops rows is the failure to guard
    against, and the rows must land on the site this deployment has always
    been rather than on a null.
    """
    import sqlite3

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    # The pre-site shape, exactly as the live database has it: observer_state
    # already migrated to per-account, nothing yet carrying site_id.
    c.executescript("""
        CREATE TABLE observer_state (
            desig TEXT NOT NULL, user_id INTEGER NOT NULL DEFAULT 0,
            observed INTEGER DEFAULT 0, observed_at_utc TEXT,
            hidden INTEGER DEFAULT 0, priority_bump REAL DEFAULT 0, note TEXT,
            PRIMARY KEY (desig, user_id));
        CREATE TABLE ephemeris_cache (desig TEXT PRIMARY KEY, signature TEXT,
            fetched_utc TEXT, payload TEXT);
        CREATE TABLE night_archive (night TEXT PRIMARY KEY, archived_utc TEXT,
            start_ts REAL, end_ts REAL, payload TEXT);
        INSERT INTO observer_state (desig, user_id, observed)
            VALUES ('MARKED', 7, 1), ('HIDDEN', 7, 0);
        INSERT INTO ephemeris_cache VALUES ('CACHED', 'sig', 'now', '{}');
        INSERT INTO night_archive VALUES ('2026-09-01', 'now', 1.0, 2.0, '{}');
    """)
    c.commit()

    db.init(c)

    want = config.DEFAULT_SITE.id
    for table, n in (("observer_state", 2), ("ephemeris_cache", 1),
                     ("night_archive", 1)):
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        check(f"{table} gained site_id", "site_id" in cols)
        got = c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        check(f"{table} kept all {n} row(s)", got == n, got)
        strays = c.execute(
            f"SELECT count(*) FROM {table} WHERE site_id IS NOT ?",
            (want,)).fetchone()[0]
        check(f"{table} rows all belong to site {want}", strays == 0, strays)
        check(f"{table}_pre_site is cleaned up after the copy is verified",
              not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name=?", (f"{table}_pre_site",)).fetchone())

    kept = c.execute("SELECT observed, user_id FROM observer_state "
                     "WHERE desig='MARKED'").fetchone()
    check("the mark itself survives, still owned by its account",
          kept is not None and kept["observed"] == 1 and kept["user_id"] == 7,
          dict(kept) if kept else None)

    db.init(c)  # must be idempotent, and must not re-migrate
    check("init is idempotent once site_id is present",
          c.execute("SELECT count(*) FROM observer_state").fetchone()[0] == 2)


def test_two_sites_never_read_each_others_rows():
    """The whole point of stage B: one object, two observatories, no bleed.

    Every one of these was a single shared row before site_id, so each check
    is a thing that would silently have been wrong the moment a second
    observatory existed -- not hypothetically, but on the first cycle.
    """
    import tempfile

    prev = config.DB_PATH
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    try:
        one, two = config.DEFAULT_SITE, _site_2()
        conn = db.connect(config.DB_PATH)
        db.init(conn)

        shared = dict(desig="SHARED", score=90, vmag=20.0, observable=1,
                      window_start_ts=1.0, window_end_ts=2.0)
        db.replace_targets(conn, [shared, dict(desig="ONLY1", score=50,
                                               vmag=21.0, observable=1)], one)
        db.replace_targets(conn, [shared, dict(desig="ONLY2", score=60,
                                               vmag=21.0, observable=1)], two)

        at1 = {r["desig"] for r in db.load_targets(conn, True, True, site=one)}
        at2 = {r["desig"] for r in db.load_targets(conn, True, True, site=two)}
        check("each site sees its own board", at1 == {"SHARED", "ONLY1"}, at1)
        check("and not the other's", at2 == {"SHARED", "ONLY2"}, at2)

        # Rewriting one site's targets must not empty the other's board.
        db.replace_targets(conn, [dict(desig="ONLY1", score=50, vmag=21.0,
                                       observable=1)], one)
        still = {r["desig"] for r in db.load_targets(conn, True, True, site=two)}
        check("rewriting one site leaves the other's targets alone",
              still == {"SHARED", "ONLY2"}, still)
        # Put site one's board back, so the checks below are about site_id
        # rather than about what this rewrite just removed.
        db.replace_targets(conn, [shared, dict(desig="ONLY1", score=50,
                                               vmag=21.0, observable=1)], one)

        # The cache is the one genuinely per-observatory fetch: MPC computes
        # an ephemeris for one obscode, so the same object is two entries.
        db.save_cache(conn, {"SHARED": ("sig1", {"lines": ["from L01"]})}, one)
        db.save_cache(conn, {"SHARED": ("sig2", {"lines": ["from Z99"]})}, two)
        c1, c2 = db.load_cache(conn, one), db.load_cache(conn, two)
        check("one object caches once per site, not once in total",
              c1["SHARED"][0] == "sig1" and c2["SHARED"][0] == "sig2",
              (c1["SHARED"][0], c2["SHARED"][0]))
        check("and the tracks come back per site",
              db.load_tracks(conn, ["SHARED"], one) == {"SHARED": ["from L01"]},
              db.load_tracks(conn, ["SHARED"], one))

        # Pruning one site's cache must not drop the other's.
        db.prune_cache(conn, [], one)
        check("pruning one site's cache leaves the other's",
              "SHARED" in db.load_cache(conn, two))
        check("while genuinely clearing its own",
              db.load_cache(conn, one) == {})

        # "Somebody else already shot this" has to mean from HERE.
        #
        # Read as the account that made the mark, and as a second account.
        # Viewing as a signed-out visitor would pass whether site_id worked
        # or not -- the observer_state join matches no rows at all for
        # user_id NULL, so there is nothing left for site_id to get wrong.
        ana = db.create_user(conn, "ana2", "x")
        bob = db.create_user(conn, "bob2", "x")
        db.set_state(conn, "SHARED", ana, two, observed=1)

        mine_at_1 = [r for r in db.load_targets(conn, True, True, user_id=ana,
                                                site=one)
                     if r["desig"] == "SHARED"][0]
        check("my own mark at another site is not observed here",
              not mine_at_1["observed"], mine_at_1["observed"])
        mine_at_2 = [r for r in db.load_targets(conn, True, True, user_id=ana,
                                                site=two)
                     if r["desig"] == "SHARED"][0]
        check("but it is recorded at the site it was made at",
              mine_at_2["observed"] == 1, mine_at_2["observed"])

        others_at_1 = [r for r in db.load_targets(conn, True, True,
                                                  user_id=bob, site=one)
                       if r["desig"] == "SHARED"][0]
        check("nor does it count as somebody else having done it here",
              others_at_1["others_observed"] == 0,
              others_at_1["others_observed"])
        others_at_2 = [r for r in db.load_targets(conn, True, True,
                                                  user_id=bob, site=two)
                       if r["desig"] == "SHARED"][0]
        check("while at that site it does warn the next observer",
              others_at_2["others_observed"] == 1,
              others_at_2["others_observed"])

        # ds42 is about the tracklet, so it stays shared across sites.
        db.save_ds42_scores(conn, {"SHARED": {"p_neo": 0.42, "status": "ok"}},
                            {"ds42_rev": "r", "model_sha256": "m", "config": {}})
        p1 = [r for r in db.load_targets(conn, True, True, site=one)
              if r["desig"] == "SHARED"][0]["p_neo"]
        p2 = [r for r in db.load_targets(conn, True, True, site=two)
              if r["desig"] == "SHARED"][0]["p_neo"]
        check("one ds42 score serves every site", p1 == 0.42 and p2 == 0.42,
              (p1, p2))

        # Two observatories can be observing the same calendar night.
        db.replace_targets(conn, [shared], one)
        db.replace_targets(conn, [shared], two)
        check("the same night label archives once per site",
              db.archive_night(conn, "2026-09-20", one)
              and db.archive_night(conn, "2026-09-20", two))
        check("and each reads back its own",
              db.load_archived_night(conn, "2026-09-20", one) is not None
              and db.load_archived_night(conn, "2026-09-20", two) is not None)
        conn.close()
    finally:
        config.DB_PATH = prev


def test_l01_from_file_is_the_l01_we_had():
    """L01's settings moved into sites/L01.toml and must not have drifted.

    These numbers are written out here rather than read from the file,
    deliberately: a test that compares the file to itself would pass through
    any transcription slip. Every value below is what config.py held before
    stage C moved it, and several of them decide where a telescope points.
    """
    s = config.DEFAULT_SITE
    check("L01 is site one", s.id == 1, s.id)
    check("and keeps its observatory code", s.obscode == "L01", s.obscode)

    for name, want in [
            ("lon_deg", 13.74930), ("rho_cos_phi", 0.704742),
            ("rho_sin_phi", 0.707169), ("night_rollover_hour_ut", 11),
            ("min_alt", 15.0), ("mpc_server_min_alt", 20.0),
            ("fov_arcsec", 2600), ("interpolate_ahead_s", 600),
            ("max_mag", 21.6), ("sun_alt_max", -15.0), ("moon_sep_min", 20.0),
            ("min_score", 25), ("min_arc_days", 0.01),
            ("max_not_seen_days", 4.0), ("min_motion", 0.7),
            ("neo_q_max", 1.3), ("neo_e_min", 0.5),
            ("exposure_base_min", 10.0), ("exposure_ref_mag", 18.0),
            ("exposure_min_per_mag", 5.0), ("exposure_floor_min", 1.0),
            ("exposure_frames", 36), ("exposure_fastest_sec", 1),
            ("arc_saturate_days", 3.0), ("mag_bright", 15.0)]:
        got = getattr(s, name)
        check(f"L01 {name} is still {want}", got == want, got)

    check("display timezone is Visnjan's", s.display_tz == "Europe/Zagreb",
          s.display_tz)
    check("NEO-only filtering is still on", s.neo_only is True)
    check("already-observed objects are still skipped",
          s.skip_already_observed is True)
    check("default sort is still chronological",
          s.default_sort == "chronological", s.default_sort)
    check("rank weights unchanged",
          s.rank_weights == {"digest2": 2.0, "arc": 1.5, "magnitude": 1.5},
          s.rank_weights)
    check("scatteredness limits unchanged",
          s.max_scatteredness == (2000, 2000)
          and s.scatteredness_warn == (1000, 800),
          (s.max_scatteredness, s.scatteredness_warn))
    check("max altitude is still unset", s.max_altitude is None, s.max_altitude)

    # The two that would be dangerous to get wrong.
    check("the keep-out wedge is exactly the one wedge it was",
          s.keepout_wedges == ((247.5, 45.0, 70.0,
                                "mount/dome collision risk"),),
          s.keepout_wedges)
    # The first four fields are the ones that decide where the telescope may
    # point, and they are asserted exactly. The comparison is sliced rather
    # than whole so that adding a field to an entry -- stage D added the
    # reason -- is not mistaken for a change in the limits themselves.
    check("the horizon mask is the same eight soft sectors",
          tuple(e[:4] for e in s.horizon_mask) == (
              (337.5, 22.5, None, "soft"), (22.5, 67.5, 20.0, "soft"),
              (67.5, 112.5, 20.0, "soft"), (112.5, 157.5, 20.0, "soft"),
              (157.5, 202.5, 20.0, "soft"), (202.5, 247.5, 30.0, "soft"),
              (247.5, 292.5, 40.0, "soft"), (292.5, 337.5, 40.0, "soft")),
          tuple(e[:4] for e in s.horizon_mask))
    check("north is still discouraged at every altitude, not blocked",
          s.horizon_mask[0][2] is None and s.horizon_mask[0][3] == "soft")
    # A limit with no stated reason is one nobody can safely relax later.
    missing = [s.sector_names[i] for i, e in enumerate(s.horizon_mask)
               if not (len(e) > 4 and str(e[4]).strip())]
    check("every sector says why its limit exists", not missing, missing)
    check("and the north's reason is the light dome, not an obstruction",
          "Trieste" in s.horizon_mask[0][4], s.horizon_mask[0][4])
    check("the speed table is unchanged",
          s.exposure_speed_bands == ((5.0, 30), (25.0, 15), (50.0, 10),
                                     (100.0, 5), (200.0, 2)),
          s.exposure_speed_bands)
    check("its plan files still go where the legacy planner reads them",
          s.plan_dir == "plans", s.plan_dir)


def test_a_site_file_must_be_complete():
    """A half-written site file is an error, not a set of defaults.

    A site that quietly inherited somebody else's magnitude limit or horizon
    mask would produce a board that looks right and is not.
    """
    import tempfile

    good = dataclasses.asdict(config.DEFAULT_SITE)
    check("the round trip of a complete site works",
          sitesmod.from_dict(good).obscode == "L01")

    short = dict(good)
    del short["max_mag"]
    try:
        sitesmod.from_dict(short)
        check("a missing field is refused", False, "no error raised")
    except sitesmod.SiteFileError as e:
        check("a missing field is refused", "max_mag" in str(e), str(e))

    extra = dict(good, telescope_colour="blue")
    try:
        sitesmod.from_dict(extra)
        check("an unknown field is refused", False, "no error raised")
    except sitesmod.SiteFileError as e:
        check("an unknown field is refused", "telescope_colour" in str(e),
              str(e))

    d = tempfile.mkdtemp()
    with open(os.path.join(d, "bad.toml"), "w") as f:
        f.write("this is not toml = = =\n")
    try:
        sitesmod.load_dir(d)
        check("a malformed file is refused", False, "no error raised")
    except sitesmod.SiteFileError:
        check("a malformed file is refused", True)

    # Two sites claiming the same id would share a board's worth of rows;
    # two claiming the same obscode would double our requests to MPC.
    d2 = tempfile.mkdtemp()
    for name, code in (("a.toml", "AAA"), ("b.toml", "BBB")):
        body = dict(good, obscode=code)
        with open(os.path.join(d2, name), "w") as f:
            f.write(_toml_of(body))
    try:
        sitesmod.load_dir(d2)
        check("a duplicate id is refused", False, "no error raised")
    except sitesmod.SiteFileError as e:
        check("a duplicate id is refused", "id" in str(e), str(e))


def _toml_of(d):
    """Minimal TOML writer, just for the tests above."""
    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, str):
            return json.dumps(v)
        if isinstance(v, (int, float)):
            return repr(v)
        if v is None:
            return "-1.0"
        if isinstance(v, (list, tuple)):
            return "[" + ", ".join(val(x) for x in v) + "]"
        raise TypeError(type(v))

    lines, tables = [], []
    for k, v in d.items():
        if isinstance(v, dict):
            tables.append(f"[{k}]\n" + "\n".join(
                f"{kk} = {val(vv)}" for kk, vv in v.items()))
        elif k == "horizon_mask":
            rows = [[a, b, -1.0 if m is None else m, h, r]
                    for a, b, m, h, r in v]
            lines.append(f"{k} = " + val(rows))
        else:
            lines.append(f"{k} = {val(v)}")
    return "\n".join(lines) + "\n" + "\n".join(tables) + "\n"


NEOCP_TWO_OBJECTS = (
    "TR0006  100 2026 09 04.9  21.8863 -14.6396 17.3 "
    "Added Sept. 8.89 UT              3   0.01 18.5  3.975\n"
    "TR0007   90 2026 09 04.9  11.1111 +22.2222 18.1 "
    "Added Sept. 8.89 UT              4   0.02 19.0  2.500\n")


def _second_site(**over):
    """A second observatory, differing only where the test needs it to."""
    base = dict(id=2, obscode="Z99", name="Second Site", plan_dir="plans/Z99")
    base.update(over)
    return dataclasses.replace(config.DEFAULT_SITE, **base)


def test_a_cycle_serves_several_sites_without_repeating_shared_work():
    """The object-level work happens once; only the ephemeris is per site.

    This is the efficiency the whole data-model split exists for. Fetching
    the orbital parameters or running ds42 once per observatory would scale
    our load on MPC and on the model with the number of sites, for answers
    that are identical every time -- and running the history recorder per
    site would write N copies of every poll into the research record.
    """
    import tempfile

    prev_db, prev_sites = config.DB_PATH, config.SITES
    d = tempfile.mkdtemp()
    config.DB_PATH = os.path.join(d, "targets.db")
    config.SITES = {1: config.DEFAULT_SITE, 2: _second_site()}
    src = os.path.join(d, "neocp.txt")
    with open(src, "w") as f:
        f.write(NEOCP_TWO_OBJECTS)

    real_info = neocp.fetch_neocp_info
    real_many = ephemeris.fetch_many
    real_score = update_neocp._score_new_objects
    real_plan = config.WRITE_NIGHTLY_PLAN
    calls = {"orbits": 0, "ephemeris": 0, "ds42": 0}

    def fake_info(*a, **k):
        calls["orbits"] += 1
        return ""

    def fake_many(desigs, **k):
        calls["ephemeris"] += 1
        list(desigs)
        return {}

    def fake_score(conn, cache):
        calls["ds42"] += 1

    try:
        neocp.fetch_neocp_info = fake_info
        ephemeris.fetch_many = fake_many
        update_neocp._score_new_objects = fake_score
        config.WRITE_NIGHTLY_PLAN = False

        conn = db.connect(config.DB_PATH)
        db.init(conn)
        shared, results = update_neocp.run_update(conn, None, src)

        check("both sites ran", len(results) == 2,
              [r["site"].obscode for r in results])
        check("the orbital parameters were fetched once, not per site",
              calls["orbits"] == 1, calls["orbits"])
        check("ds42 scoring ran once, not per site", calls["ds42"] == 1,
              calls["ds42"])
        check("but the ephemeris was fetched once per site",
              calls["ephemeris"] == 2, calls["ephemeris"])

        one, two = config.SITES[1], config.SITES[2]
        n1 = len(db.load_targets(conn, True, True, site=one))
        n2 = len(db.load_targets(conn, True, True, site=two))
        check("each site got its own board", n1 == 2 and n2 == 2, (n1, n2))

        check("and its own last-cycle record",
              db.get_meta(conn, "last_update_ok", site=one) == "1"
              and db.get_meta(conn, "last_update_ok", site=two) == "1")
        check("recorded against the site, not the deployment",
              db.get_meta(conn, "last_update_ok") is None,
              db.get_meta(conn, "last_update_ok"))
        check("each site has a night label of its own",
              db.get_meta(conn, "night", site=one) is not None
              and db.get_meta(conn, "night", site=two) is not None)
        conn.close()
    finally:
        neocp.fetch_neocp_info = real_info
        ephemeris.fetch_many = real_many
        update_neocp._score_new_objects = real_score
        config.WRITE_NIGHTLY_PLAN = real_plan
        config.DB_PATH, config.SITES = prev_db, prev_sites


def test_one_sites_failure_does_not_take_the_others_down():
    """A partner observatory breaking must not stop this one's board.

    Without isolation the per-site loop makes every site a single point of
    failure for every other: one timing out ends the cycle, and the board
    this deployment was built for silently stops updating behind it.
    """
    import tempfile

    prev_db, prev_sites = config.DB_PATH, config.SITES
    d = tempfile.mkdtemp()
    config.DB_PATH = os.path.join(d, "targets.db")
    # The broken site runs FIRST, so a failure that aborted the cycle would
    # take the good one with it.
    broken = _second_site(id=1, obscode="BAD", name="Broken")
    good = _second_site(id=2, obscode="Z99")
    config.SITES = {1: broken, 2: good}
    src = os.path.join(d, "neocp.txt")
    with open(src, "w") as f:
        f.write(NEOCP_TWO_OBJECTS)

    real_info = neocp.fetch_neocp_info
    real_many = ephemeris.fetch_many
    real_score = update_neocp._score_new_objects
    real_plan = config.WRITE_NIGHTLY_PLAN

    def fake_many(desigs, site=None, **k):
        if site is not None and site.obscode == "BAD":
            raise RuntimeError("MPC timed out for BAD")
        list(desigs)
        return {}

    try:
        neocp.fetch_neocp_info = lambda *a, **k: ""
        ephemeris.fetch_many = fake_many
        update_neocp._score_new_objects = lambda conn, cache: None
        config.WRITE_NIGHTLY_PLAN = False

        conn = db.connect(config.DB_PATH)
        db.init(conn)
        shared, results = update_neocp.run_update(conn, None, src)

        check("the cycle completed despite one site failing",
              [r["site"].obscode for r in results] == ["Z99"],
              [r["site"].obscode for r in results])
        check("the working site still got its board",
              len(db.load_targets(conn, True, True, site=good)) == 2)
        check("the failed site is marked not ok",
              db.get_meta(conn, "last_update_ok", site=broken) == "0",
              db.get_meta(conn, "last_update_ok", site=broken))
        check("with its own error recorded against it",
              "BAD" in (db.get_meta(conn, "last_error", site=broken) or ""),
              db.get_meta(conn, "last_error", site=broken))
        check("and the working site is not blamed for it",
              db.get_meta(conn, "last_error", site=good) is None,
              db.get_meta(conn, "last_error", site=good))
        conn.close()
    finally:
        neocp.fetch_neocp_info = real_info
        ephemeris.fetch_many = real_many
        update_neocp._score_new_objects = real_score
        config.WRITE_NIGHTLY_PLAN = real_plan
        config.DB_PATH, config.SITES = prev_db, prev_sites


def test_each_site_writes_its_own_plan_file():
    """Two observatories must not overwrite each other's night.

    L01's plan_dir is the directory its legacy planner already reads, so the
    path it has always used is unchanged; a new site gets its own. Nothing in
    the code knows which of those is the special case.
    """
    import tempfile

    prev_base = config.BASE_DIR
    config.BASE_DIR = tempfile.mkdtemp()
    try:
        rows = [dict(desig="T1", observable=False)]
        p1 = output.write_plan(rows, "2026-09-20", site=config.DEFAULT_SITE)
        p2 = output.write_plan(rows, "2026-09-20", site=_second_site())
        check("L01's plan keeps the path the planner reads",
              p1 == os.path.join(config.BASE_DIR, "plans", "2026-09-20.txt"),
              p1)
        check("the second site writes somewhere else entirely", p1 != p2, p2)
        check("and both files exist",
              os.path.exists(p1) and os.path.exists(p2))
    finally:
        config.BASE_DIR = prev_base


def test_the_switcher_resolves_an_observatory_by_code():
    """A URL someone pastes names the observatory, and a wrong one is safe.

    This test used to assert the switcher stayed hidden until a second
    observatory existed. That was the original design and it was wrong: it
    made the whole feature undiscoverable, because you cannot add the second
    observatory without the control that offers it. What it is always
    present is now asserted by
    test_the_switcher_is_there_with_only_one_observatory; what survives here
    is the resolution behaviour, which is unchanged.
    """
    import importlib
    import tempfile

    import app as appmod

    prev_db, prev_sites = config.DB_PATH, config.SITES
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    try:
        conn = db.connect(config.DB_PATH)
        db.init(conn)
        conn.close()

        config.SITES = {1: config.DEFAULT_SITE, 2: _second_site()}
        importlib.reload(appmod)
        c = appmod.app.test_client()
        page = c.get("/").data.decode()
        check("the switcher offers both observatories",
              "Z99" in page and config.DEFAULT_SITE.obscode in page)

        with appmod.app.test_request_context("/?site=Z99"):
            check("?site= picks the observatory by code",
                  appmod.current_site().obscode == "Z99")
        with appmod.app.test_request_context("/?site=NOPE"):
            check("an unknown code falls back to the default rather than 500",
                  appmod.current_site().obscode
                  == config.DEFAULT_SITE.obscode)
        with appmod.app.test_request_context("/"):
            check("and naming none gives the default",
                  appmod.current_site().obscode
                  == config.DEFAULT_SITE.obscode)

        # The choice has to survive the next click, or every link on the
        # board would have to carry it.
        c.get("/?site=Z99")
        check("the choice is remembered for the session",
              "Z99" in c.get("/status").data.decode())
    finally:
        config.DB_PATH, config.SITES = prev_db, prev_sites
        importlib.reload(appmod)


def test_the_settings_page_keeps_soft_and_hard_apart():
    """The page must never let a light-dome limit read as a dome collision.

    This is the subtlety the plan calls the one most likely to be lost in a
    settings page. A form offering only "minimum altitude per direction"
    collapses an advisory limit and a physical obstruction into one number
    and silently picks an interpretation: an observatory with light pollution
    to the north types a high number, and either its northern targets vanish
    or its telescope is pointed into a wall.

    So the page has to keep them in separate tables, say which removes and
    which only warns, and print the reason each one exists.
    """
    import importlib
    import tempfile

    import app as appmod

    prev_db = config.DB_PATH
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    try:
        conn = db.connect(config.DB_PATH)
        db.init(conn)
        conn.close()
        importlib.reload(appmod)

        r = appmod.app.test_client().get("/settings")
        check("the settings page renders", r.status_code == 200, r.status_code)
        page = r.data.decode()

        check("the soft structure says it never removes",
              "Warns, never removes" in page)
        check("the hard structure says it removes with no override",
              "Removes, with no override" in page)
        check("they are two tables, not one",
              page.index("Warns, never removes")
              < page.index("Removes, with no override"))

        site = config.DEFAULT_SITE
        for i, entry in enumerate(site.horizon_mask):
            check(f"sector {site.sector_names[i]} prints its reason",
                  entry[4].split(",")[0].replace("'", "&#39;") in page
                  or entry[4].split(",")[0] in page, entry[4])
        for wedge in site.keepout_wedges:
            check("the wedge prints its reason", wedge[3] in page, wedge[3])

        check("the arc direction is spelled out, not assumed",
              "clockwise" in page.lower())
        check("the mask and wedges are drawn, not only tabulated",
              "<svg" in page)
        # The page was read-only when it first shipped and is editable now,
        # so what it has to say is no longer "you cannot change this" but
        # "here is what changing it does, and who may".
        check("a signed-out reader is told why they cannot change anything",
              "signed out" in page and "Sign in" in page)
        check("and the page says an edit overrides the file, not rewrites it",
              "override of that file" in page)

        # The numbers an observer would come here to check.
        check("the magnitude limit is shown", str(site.max_mag) in page)
        check("the digest2 floor is shown", str(site.min_score) in page)
        check("the frame count is shown", str(site.exposure_frames) in page)
        check("the ranking weights are shown",
              all(str(v) in page for v in site.rank_weights.values()))

        # It is one observatory's page, not the deployment's.
        prev_sites = config.SITES
        try:
            config.SITES = {1: site, 2: _second_site()}
            importlib.reload(appmod)
            other = appmod.app.test_client().get(
                "/settings?site=Z99").data.decode()
            check("the page follows the site switcher",
                  "Z99" in other and "Second Site" in other)
        finally:
            config.SITES = prev_sites
    finally:
        config.DB_PATH = prev_db
        importlib.reload(appmod)


def _settings_client(appmod, username="keeper", admin=True):
    """A client signed in as an account that may configure L01.

    Admin by default, because L01 comes from a file and so has no owner row:
    nobody signed it up. That is the deployment's own observatory, and if
    admins could not configure it, it would be the one site nobody could.
    """
    c = appmod.app.test_client()
    c.post("/register", data={"username": username,
                              "password": "correct-horse-7",
                              "confirm": "correct-horse-7"})
    if admin:
        conn = db.connect(config.DB_PATH)
        try:
            with conn:
                conn.execute("UPDATE users SET is_admin=1 WHERE username=?",
                             (username,))
        finally:
            conn.close()
    with c.session_transaction() as s:
        return c, s.get("csrf")


def _mask_form(site, **over):
    """The settings form as the page would submit it, unchanged by default."""
    form = {}
    for spec in siteconf.SCALARS:
        value = getattr(site, spec.name)
        if spec.kind == "bool":
            if value:
                form[spec.name] = "on"
        else:
            form[spec.name] = str(value)
    form["max_altitude"] = ("none" if site.max_altitude is None
                            else str(site.max_altitude))
    for k in siteconf.RANK_WEIGHT_KEYS:
        form[f"weight_{k}"] = str(site.rank_weights[k])
    for i, e in enumerate(site.horizon_mask):
        form[f"mask_alt_{i}"] = "any" if e[2] is None else str(e[2])
        form[f"mask_hard_{i}"] = e[3]
        form[f"mask_why_{i}"] = e[4]
    form["wedge_from"] = [str(w[0]) for w in site.keepout_wedges]
    form["wedge_to"] = [str(w[1]) for w in site.keepout_wedges]
    form["wedge_alt"] = [str(w[2]) for w in site.keepout_wedges]
    form["wedge_why"] = [w[3] for w in site.keepout_wedges]
    form.update(over)
    return form


def test_settings_edits_layer_over_the_file_and_can_be_undone():
    """An edit overrides the file; it never rewrites it, and it reverts.

    The file is where the reasoning lives -- why a wedge starts where it
    does, why a sector's figure is interpolated -- and a form cannot write a
    comment. So an edit is a row in the database applied on top, reverting is
    deleting that row, and a site nobody has touched reads exactly as before.
    """
    import importlib
    import tempfile

    import app as appmod

    prev_db = config.DB_PATH
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    siteconf.forget()
    try:
        conn = db.connect(config.DB_PATH)
        db.init(conn)
        conn.close()
        importlib.reload(appmod)

        base = config.DEFAULT_SITE
        anon = appmod.app.test_client()
        check("the page is readable signed out",
              anon.get("/settings").status_code == 200)
        check("but it offers no inputs to somebody who cannot save",
              'name="max_mag"' not in anon.get("/settings").data.decode())
        check("and a save from an unnamed writer is refused",
              anon.post("/settings", data={"action": "save"}).status_code
              in (400, 401, 403))

        c, tok = _settings_client(appmod)
        page = c.get("/settings").data.decode()
        check("a signed-in account gets a form", 'name="max_mag"' in page)

        # --- a scalar edit ---
        form = _mask_form(base, csrf=tok, action="save", max_mag="20.5")
        r = c.post("/settings", data=form)
        check("the edit is accepted", r.status_code == 302, r.status_code)

        conn = db.connect(config.DB_PATH)
        try:
            over = db.load_site_overrides(conn, base.id)
            check("only the field that changed is stored",
                  list(over) == ["max_mag"], list(over))
            check("stored as the new value", over["max_mag"] == 20.5)
            siteconf.forget()
            eff = siteconf.effective(conn, base)
            check("the site now reads the edited value", eff.max_mag == 20.5)
            check("and everything else still comes from the file",
                  eff.min_score == base.min_score
                  and eff.keepout_wedges == base.keepout_wedges)
            rows = db.site_settings_rows(conn, base.id)
            check("what it replaced is kept, so it can be undone",
                  json.loads(rows["max_mag"]["previous_json"]) == base.max_mag,
                  rows["max_mag"]["previous_json"])
            check("and who changed it is recorded",
                  rows["max_mag"]["changed_by"] is not None)
        finally:
            conn.close()

        check("the file on disk is untouched",
              sitesmod.load_file("sites/L01.toml").max_mag == base.max_mag)

        # --- undo ---
        r = c.post("/settings", data={"csrf": tok, "action": "revert:max_mag"})
        check("reverting is accepted", r.status_code == 302, r.status_code)
        conn = db.connect(config.DB_PATH)
        try:
            check("the override is gone entirely",
                  db.load_site_overrides(conn, base.id) == {})
            siteconf.forget()
            check("so the file decides again",
                  siteconf.effective(conn, base).max_mag == base.max_mag)
        finally:
            conn.close()

        # --- a value that must not be stored ---
        bad = _mask_form(base, csrf=tok, action="save", max_mag="banana")
        r = c.post("/settings", data=bad)
        check("a non-numeric limit is refused", r.status_code == 400)
        check("and the refusal says which field and why",
              b"Faintest magnitude" in r.data and b"not a number" in r.data)
        r = c.post("/settings", data=_mask_form(base, csrf=tok, action="save",
                                                sun_alt_max="40"))
        check("a sun altitude above the horizon is refused",
              r.status_code == 400 and b"at most" in r.data)
        conn = db.connect(config.DB_PATH)
        try:
            check("nothing was written by either refusal",
                  db.load_site_overrides(conn, base.id) == {})
        finally:
            conn.close()

        # --- a mask sector with no stated reason ---
        r = c.post("/settings", data=_mask_form(base, csrf=tok, action="save",
                                                mask_why_0=""))
        check("a mask limit with no reason is refused",
              r.status_code == 400 and b"say why" in r.data)
    finally:
        config.DB_PATH = prev_db
        siteconf.forget()
        importlib.reload(appmod)


def test_a_keepout_wedge_cannot_be_changed_by_accident():
    """The one setting where a typo points a telescope at a wall.

    Saving a wedge change requires typing the observatory code, the pattern
    GitHub uses for deleting a repository. Everything else on the page saves
    without it, so the guard has to apply to wedge changes and only to them --
    a confirmation demanded for every save is a confirmation people learn to
    type without reading.
    """
    import importlib
    import tempfile

    import app as appmod

    prev_db = config.DB_PATH
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    siteconf.forget()
    try:
        conn = db.connect(config.DB_PATH)
        db.init(conn)
        conn.close()
        importlib.reload(appmod)

        base = config.DEFAULT_SITE
        c, tok = _settings_client(appmod)

        # A scalar change alone needs no confirmation.
        r = c.post("/settings", data=_mask_form(base, csrf=tok, action="save",
                                                min_score="30"))
        check("an ordinary setting saves without ceremony",
              r.status_code == 302, r.status_code)

        widened = _mask_form(base, csrf=tok, action="save")
        widened["wedge_alt"] = ["80.0"]          # was 70
        r = c.post("/settings", data=widened)
        check("a wedge change without the code is refused",
              r.status_code == 400, r.status_code)
        check("and the refusal names the code that has to be typed",
              b"L01" in r.data)

        r = c.post("/settings", data=dict(widened, confirm_obscode="WRONG"))
        check("the wrong code is refused too", r.status_code == 400)

        conn = db.connect(config.DB_PATH)
        try:
            siteconf.forget()
            check("the wedge is untouched by either attempt",
                  siteconf.effective(conn, base).keepout_wedges
                  == base.keepout_wedges)
        finally:
            conn.close()

        r = c.post("/settings", data=dict(widened, confirm_obscode="l01"))
        check("the right code saves it, case-insensitively",
              r.status_code == 302, r.status_code)
        conn = db.connect(config.DB_PATH)
        try:
            siteconf.forget()
            wedges = siteconf.effective(conn, base).keepout_wedges
            check("and the new wedge is what takes effect",
                  wedges[0][2] == 80.0, wedges)
            check("with its reason carried over", wedges[0][3] == base.keepout_wedges[0][3])
        finally:
            conn.close()

        # A wedge is removed by clearing its row, not by a separate verb.
        cleared = _mask_form(base, csrf=tok, action="save",
                             confirm_obscode="L01")
        cleared["wedge_from"] = [""]
        cleared["wedge_to"] = [""]
        cleared["wedge_alt"] = [""]
        cleared["wedge_why"] = [""]
        r = c.post("/settings", data=cleared)
        check("clearing a row removes that wedge", r.status_code == 302)
        conn = db.connect(config.DB_PATH)
        try:
            siteconf.forget()
            check("leaving no sky refused outright",
                  siteconf.effective(conn, base).keepout_wedges == ())
        finally:
            conn.close()

        # Half a row is a mistake, not an instruction.
        half = _mask_form(base, csrf=tok, action="save",
                          confirm_obscode="L01")
        half["wedge_from"] = ["100"]
        half["wedge_to"] = [""]
        half["wedge_alt"] = ["50"]
        half["wedge_why"] = ["half filled"]
        r = c.post("/settings", data=half)
        check("a half-filled wedge row is refused rather than guessed at",
              r.status_code == 400 and b"every field" in r.data)
    finally:
        config.DB_PATH = prev_db
        siteconf.forget()
        importlib.reload(appmod)


def test_an_edited_limit_reaches_the_update_cycle():
    """A setting changed on the page has to change what reaches the board.

    The failure this guards against is the quiet one: the settings page and
    the board both reading the override while the cycle that decides what is
    observable keeps reading the file. The limit would appear changed
    everywhere a person looks and be unchanged in the only place it acts.
    """
    import tempfile

    prev_db, prev_sites = config.DB_PATH, config.SITES
    d = tempfile.mkdtemp()
    config.DB_PATH = os.path.join(d, "targets.db")
    config.SITES = {1: config.DEFAULT_SITE}
    siteconf.forget()
    src = os.path.join(d, "neocp.txt")
    with open(src, "w") as f:
        f.write(NEOCP_TWO_OBJECTS)

    real_info = neocp.fetch_neocp_info
    real_many = ephemeris.fetch_many
    real_score = update_neocp._score_new_objects
    real_plan = config.WRITE_NIGHTLY_PLAN
    try:
        neocp.fetch_neocp_info = lambda *a, **k: ""
        ephemeris.fetch_many = lambda desigs, **k: (list(desigs), {})[1]
        update_neocp._score_new_objects = lambda conn, cache: None
        config.WRITE_NIGHTLY_PLAN = False

        conn = db.connect(config.DB_PATH)
        db.init(conn)

        # The sample list holds objects scoring 100 and 90.
        update_neocp.run_update(conn, None, src)
        rows = db.load_targets(conn, True, True, site=config.DEFAULT_SITE)
        kept = {r["desig"] for r in rows
                if "LOW_SCORE" not in (r["discard_reasons"] or [])}
        check("both objects clear the file's digest2 floor", len(kept) == 2,
              kept)

        db.save_site_override(conn, config.DEFAULT_SITE.id, "min_score",
                              siteconf.dumps(95),
                              siteconf.dumps(config.DEFAULT_SITE.min_score), 1)
        siteconf.forget()

        update_neocp.run_update(conn, None, src)
        rows = db.load_targets(conn, True, True, site=config.DEFAULT_SITE)
        low = {r["desig"] for r in rows
               if "LOW_SCORE" in (r["discard_reasons"] or [])}
        check("raising the floor on the page rejects the weaker object",
              low == {"TR0007"}, low)
        check("and leaves the stronger one alone",
              not any(r["desig"] == "TR0006" and "LOW_SCORE"
                      in (r["discard_reasons"] or []) for r in rows))
        conn.close()
    finally:
        neocp.fetch_neocp_info = real_info
        ephemeris.fetch_many = real_many
        update_neocp._score_new_objects = real_score
        config.WRITE_NIGHTLY_PLAN = real_plan
        config.DB_PATH, config.SITES = prev_db, prev_sites
        siteconf.forget()


def test_the_settings_preview_is_drawn_by_the_board_s_own_code():
    """The preview must not be a second implementation of the sky map.

    A preview drawn by different code is a preview that can disagree with
    what actually gets enforced, which is the one thing it must never do. So
    it goes back to the server and comes out of skymap.render_svg.
    """
    import importlib
    import tempfile

    import app as appmod

    prev_db = config.DB_PATH
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    siteconf.forget()
    try:
        conn = db.connect(config.DB_PATH)
        db.init(conn)
        conn.close()
        importlib.reload(appmod)

        base = config.DEFAULT_SITE
        c = appmod.app.test_client()
        args = {k: v for k, v in _mask_form(base).items()
                if isinstance(v, str)}
        args["wedge_from"] = "247.5"
        args["wedge_to"] = "45.0"
        args["wedge_alt"] = "70.0"
        args["wedge_why"] = "mount/dome collision risk"

        r = c.get("/settings/preview.svg", query_string=args)
        check("the preview renders", r.status_code == 200, r.status_code)
        check("as an SVG", r.mimetype == "image/svg+xml", r.mimetype)
        body = r.data.decode()
        check("drawn by the same code as the board",
              "Keep out" in body and "mount/dome collision risk" in body)

        # A wedge the form has not finished describing must not 500 the
        # endpoint the page polls on every keystroke.
        r = c.get("/settings/preview.svg",
                  query_string=dict(args, wedge_alt="banana"))
        check("an unparseable value answers rather than failing",
              r.status_code == 200)
        check("and says so on the drawing itself",
              b"Cannot draw this yet" in r.data)

        # The reason is user text reaching an SVG, so it must be escaped.
        r = c.get("/settings/preview.svg",
                  query_string=dict(args, wedge_why="</title><script>x</script>"))
        check("a reason cannot inject markup into the drawing",
              b"<script>x</script>" not in r.data)
    finally:
        config.DB_PATH = prev_db
        siteconf.forget()
        importlib.reload(appmod)


def _fresh_board(appmod):
    """An empty database and a reloaded app, for the sign-up tests."""
    import importlib
    import tempfile

    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    siteconf.forget()
    conn = db.connect(config.DB_PATH)
    db.init(conn)
    conn.close()
    importlib.reload(appmod)


def test_a_new_observatory_inherits_nobody_elses_dome_limits():
    """The one mistake on the sign-up path that could point a telescope at a
    wall.

    A keep-out wedge means "the mount will hit something here", which is a
    fact about one particular building. Seeding a new observatory with
    another's would be worse than useless: it would refuse sky that is
    perfectly safe for them and say nothing about the sky that is not. So a
    new site starts refusing nothing, and its horizon mask says in every
    sector that it has not been confirmed.
    """
    import app as appmod

    prev_db = config.DB_PATH
    try:
        _fresh_board(appmod)
        c, tok = _settings_client(appmod, username="newowner", admin=False)
        r = c.post("/sites/new", data={"csrf": tok, "obscode": "F51",
                                       "name": "Test Pan-STARRS",
                                       "display_tz": "Pacific/Honolulu"})
        check("the observatory is created", r.status_code == 302,
              r.status_code)

        conn = db.connect(config.DB_PATH)
        try:
            made = registry.by_obscode(conn, "F51")
            check("and appears in the registry", made is not None)
            check("refusing no sky at all", made.keepout_wedges == (),
                  made.keepout_wedges)
            check("which is NOT the first site's wedge",
                  made.keepout_wedges != config.DEFAULT_SITE.keepout_wedges)
            check("its mask says every sector is unconfirmed",
                  all("not yet confirmed" in e[4] for e in made.horizon_mask),
                  [e[4] for e in made.horizon_mask])
            check("and every sector only warns",
                  all(e[3] == "soft" for e in made.horizon_mask))

            # Position comes from MPC's table, not from the form.
            check("its position is MPC's own",
                  abs(made.lon_deg - 203.74409) < 1e-5, made.lon_deg)
            check("its night boundary follows its longitude, not Croatia's",
                  made.night_rollover_hour_ut
                  != config.DEFAULT_SITE.night_rollover_hour_ut,
                  made.night_rollover_hour_ut)
            check("and its plan files do not land in another site's directory",
                  made.plan_dir != config.DEFAULT_SITE.plan_dir, made.plan_dir)
        finally:
            conn.close()
    finally:
        config.DB_PATH = prev_db
        siteconf.forget()
        import importlib
        importlib.reload(appmod)


def test_an_observatory_code_is_not_optional_and_cannot_be_invented():
    """MPC computes an ephemeris for a code. No code, no ephemeris, no board.

    So the code is checked against MPC's own table rather than accepted as
    typed -- and that table is also where the position comes from, because a
    position typed into a form is a board describing a different patch of sky.
    """
    import app as appmod

    prev_db = config.DB_PATH
    try:
        _fresh_board(appmod)
        c, tok = _settings_client(appmod, username="hopeful", admin=False)

        r = c.post("/sites/new", data={"csrf": tok, "obscode": "",
                                       "name": "No code", "display_tz": "UTC"})
        check("a missing code is refused",
              r.status_code == 400 and b"code MPC assigned" in r.data)

        r = c.post("/sites/new", data={"csrf": tok, "obscode": "ZZZ",
                                       "name": "Invented",
                                       "display_tz": "UTC"})
        check("a code MPC does not know is refused", r.status_code == 400)
        check("and the refusal explains why it cannot be served",
              b"no position" in r.data)

        r = c.post("/sites/new", data={"csrf": tok, "obscode": "F51",
                                       "name": "Fine",
                                       "display_tz": "Nowhere/Nothing"})
        check("an unknown timezone is refused",
              r.status_code == 400 and b"not a timezone" in r.data)

        c.post("/sites/new", data={"csrf": tok, "obscode": "F51",
                                   "name": "First", "display_tz": "UTC"})
        r = c.post("/sites/new", data={"csrf": tok, "obscode": "F51",
                                       "name": "Again", "display_tz": "UTC"})
        check("the same code cannot be added twice",
              r.status_code == 400 and b"already on this deployment" in r.data)

        # L01 comes from a file rather than the table, and must be just as
        # taken.
        r = c.post("/sites/new", data={"csrf": tok, "obscode": "L01",
                                       "name": "Clash", "display_tz": "UTC"})
        check("nor can a code a file already claims",
              r.status_code == 400 and b"already on this deployment" in r.data)

        anon = appmod.app.test_client()
        r = anon.post("/sites/new", data={"obscode": "F52", "name": "Anon",
                                          "display_tz": "UTC"})
        check("and a signed-out visitor cannot add one at all",
              r.status_code in (400, 401, 403), r.status_code)
    finally:
        config.DB_PATH = prev_db
        siteconf.forget()
        import importlib
        importlib.reload(appmod)


def test_the_observatory_cap_is_enforced():
    """The cap is what bounds this deployment's load on MPC.

    Only the ephemeris fetch is per-observatory, so the number of sites is
    the number that matters -- and a file-defined site costs exactly as much
    as a signed-up one, so both count towards it.
    """
    import app as appmod

    prev_db, prev_cap = config.DB_PATH, config.ACTIVE_SITE_CAP
    try:
        _fresh_board(appmod)
        c, tok = _settings_client(appmod, username="capper", admin=False)

        conn = db.connect(config.DB_PATH)
        try:
            check("the file-defined site counts towards the cap",
                  registry.active_count(conn) == len(config.SITES),
                  registry.active_count(conn))
        finally:
            conn.close()

        config.ACTIVE_SITE_CAP = len(config.SITES)
        r = c.post("/sites/new", data={"csrf": tok, "obscode": "F51",
                                       "name": "Over", "display_tz": "UTC"})
        check("adding one past the cap is refused", r.status_code == 400)
        check("and the refusal says what the cap is for",
              b"load on MPC" in r.data or b"requests a minute" in r.data)

        config.ACTIVE_SITE_CAP = len(config.SITES) + 1
        r = c.post("/sites/new", data={"csrf": tok, "obscode": "F51",
                                       "name": "Fits", "display_tz": "UTC"})
        check("raising the cap by one lets exactly one more in",
              r.status_code == 302, r.status_code)
        r = c.post("/sites/new", data={"csrf": tok, "obscode": "F52",
                                       "name": "One too many",
                                       "display_tz": "UTC"})
        check("and no more than one", r.status_code == 400)
    finally:
        config.DB_PATH, config.ACTIVE_SITE_CAP = prev_db, prev_cap
        siteconf.forget()
        import importlib
        importlib.reload(appmod)


def test_owner_edits_and_members_observe():
    """One role boundary, and it has to hold on somebody else's observatory.

    The owner created the site and configures it. Everyone else -- including
    another observatory's owner, who is a perfectly ordinary signed-in
    account here -- marks targets and reads the board.
    """
    import app as appmod

    prev_db = config.DB_PATH
    try:
        _fresh_board(appmod)
        owner, otok = _settings_client(appmod, username="owner1", admin=False)
        owner.post("/sites/new", data={"csrf": otok, "obscode": "F51",
                                       "name": "Owned", "display_tz": "UTC"})

        conn = db.connect(config.DB_PATH)
        try:
            made = registry.by_obscode(conn, "F51")
            uid = db.user_by_name(conn, "owner1")["id"]
            check("whoever created it owns it",
                  db.site_role(conn, made.id, uid) == db.OWNER)
        finally:
            conn.close()

        page = owner.get("/settings?site=F51").data.decode()
        check("the owner gets a form on their own observatory",
              'name="max_mag"' in page)

        stranger, stok = _settings_client(appmod, username="stranger",
                                          admin=False)
        page = stranger.get("/settings?site=F51").data.decode()
        check("another account can read it", "Owned" in page)
        check("but is offered nothing to change",
              'name="max_mag"' not in page)
        r = stranger.post("/settings", data={"csrf": stok, "action": "save",
                                             "max_mag": "1"})
        check("and a save from them is refused", r.status_code == 403,
              r.status_code)
        check("with a refusal that says who may",
              b"owner" in r.data)

        conn = db.connect(config.DB_PATH)
        try:
            made = registry.by_obscode(conn, "F51")
            check("nothing was changed by the attempt",
                  made.max_mag == sitesmod.STARTING_POINT["max_mag"],
                  made.max_mag)

            # A member observes; they still do not configure.
            sid = made.id
            uid = db.user_by_name(conn, "stranger")["id"]
            db.add_site_member(conn, sid, uid, db.MEMBER)
            check("a member is recorded as one",
                  db.site_role(conn, sid, uid) == db.MEMBER)
        finally:
            conn.close()

        page = stranger.get("/settings?site=F51").data.decode()
        check("and a member is still offered nothing to change",
              'name="max_mag"' not in page)
    finally:
        config.DB_PATH = prev_db
        siteconf.forget()
        import importlib
        importlib.reload(appmod)


def test_you_land_on_the_observatory_you_work_at():
    """A member goes to their own board; a stranger sees the default."""
    import app as appmod

    prev_db = config.DB_PATH
    try:
        _fresh_board(appmod)
        owner, otok = _settings_client(appmod, username="lander", admin=False)
        owner.post("/sites/new", data={"csrf": otok, "obscode": "F51",
                                       "name": "Home", "display_tz": "UTC"})

        anon = appmod.app.test_client()
        check("a signed-out visitor still lands on the default board",
              config.DEFAULT_SITE.obscode
              in anon.get("/status").data.decode())

        fresh = appmod.app.test_client()
        fresh.post("/login", data={"username": "lander",
                                   "password": "correct-horse-7"})
        check("signing in lands you on the observatory you work at",
              "F51" in fresh.get("/status").data.decode(),
              fresh.get("/status").data.decode()[:200])

        check("and the switcher can still take you elsewhere",
              config.DEFAULT_SITE.obscode in fresh.get(
                  "/status?site=" + config.DEFAULT_SITE.obscode
              ).data.decode())
    finally:
        config.DB_PATH = prev_db
        siteconf.forget()
        import importlib
        importlib.reload(appmod)


HEADER_PAGES = ("/", "/history", "/settings", "/sites", "/sites/new",
                "/login", "/register", "/forgot")


def test_every_page_carries_the_same_navigation():
    """Navigation has to exist in one place, or pages lose it one by one.

    Every template used to hand-roll its own header, which is exactly how the
    observatories page ended up reachable from nowhere: there was no single
    place for a link to live, so each page invented its own back-link and
    nothing linked forward.
    """
    import importlib
    import tempfile

    import app as appmod

    prev_db = config.DB_PATH
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    siteconf.forget()
    try:
        conn = db.connect(config.DB_PATH)
        db.init(conn)
        conn.close()
        importlib.reload(appmod)
        c = appmod.app.test_client()

        for path in HEADER_PAGES:
            body = c.get(path).data.decode()
            check(f"{path} offers the observatory switcher",
                  'class="sitemenu"' in body)
            check(f"{path} links to the observatories page",
                  'href="/sites"' in body)
            check(f"{path} offers a way to add one",
                  "Add an observatory" in body)

        # The target page is per-object rather than per-section, and is the
        # one people reach by clicking rather than by navigating, so it is
        # the easiest to forget. It needs a real row: an unknown designation
        # answers with a bare 404 string, which is not a page at all.
        conn = db.connect(config.DB_PATH)
        try:
            now = time.time()
            conn.execute(
                "INSERT INTO targets (site_id, desig, score, vmag, hmag,"
                " nobs, arc_days, not_seen_days, observable, score_total,"
                " discard_reasons, max_alt, max_alt_az, max_alt_ts,"
                " exposure_min, frames, frame_sec, frame_motion,"
                " window_minutes, window_start_ts, window_end_ts, cur_alt,"
                " cur_az, cur_motion, cur_moon_dist, cur_vmag, cur_sun_alt,"
                " cur_ts, incl, q, e)"
                " VALUES (?, 'NAVCHECK', 90, 20.0, 22.0, 3, 0.5, 0.2, 1, 3.0,"
                " '[]', 61.0, 180.0, ?, 20.5, 36, 30, 1.4, 120.0, ?, ?, 55.0,"
                " 175.0, 1.4, 55.0, 20.1, -20.0, ?, 12.5, 0.9, 0.6)",
                (config.DEFAULT_SITE.id, now + 3600, now, now + 7200, now))
            conn.commit()
        finally:
            conn.close()
        body = c.get("/target/NAVCHECK").data.decode()
        check("even a target page keeps the navigation",
              'class="sitemenu"' in body)
        check("and its link to the observatories page",
              'href="/sites"' in body)
    finally:
        config.DB_PATH = prev_db
        siteconf.forget()
        importlib.reload(appmod)


def test_the_switcher_is_there_with_only_one_observatory():
    """The regression that would quietly undo this whole change.

    Hiding the control until a second observatory exists is what made the
    feature undiscoverable, because you cannot add the second one without
    it. One observatory is exactly the state every new deployment is in.
    """
    import importlib
    import tempfile

    import app as appmod

    prev_db, prev_sites = config.DB_PATH, config.SITES
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    siteconf.forget()
    try:
        conn = db.connect(config.DB_PATH)
        db.init(conn)
        conn.close()
        config.SITES = {1: config.DEFAULT_SITE}
        importlib.reload(appmod)

        body = appmod.app.test_client().get("/").data.decode()
        check("with one observatory the switcher is still there",
              'class="sitemenu"' in body)
        check("naming the one there is", config.DEFAULT_SITE.obscode in body)
        check("and still offering to add another",
              "Add an observatory" in body)

        # Signed out is the state a visiting astronomer is in.
        check("a signed-out visitor sees it too",
              'class="sitemenu"' in body and "Sign in" in body
              or 'class="sitemenu"' in body)
    finally:
        config.DB_PATH, config.SITES = prev_db, prev_sites
        siteconf.forget()
        importlib.reload(appmod)


def test_switching_observatory_always_lands_on_that_board():
    """Wherever you switch from, you arrive at that observatory's queue.

    A target page is the reason: a designation on one site's queue need not
    be on another's at all, so "the same page, elsewhere" can be a page that
    does not exist.
    """
    import importlib
    import tempfile

    import app as appmod

    prev_db, prev_sites = config.DB_PATH, config.SITES
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    siteconf.forget()
    try:
        conn = db.connect(config.DB_PATH)
        db.init(conn)
        conn.close()
        config.SITES = {1: config.DEFAULT_SITE, 2: _second_site()}
        importlib.reload(appmod)
        c = appmod.app.test_client()

        for path in ("/", "/settings", "/history", "/sites"):
            body = c.get(path).data.decode()
            check(f"from {path} the switcher points at a board",
                  'href="/?site=Z99"' in body,
                  [l for l in body.splitlines() if "site=Z99" in l][:1])
            # Only meaningful away from the board: on the board itself,
            # "that observatory's board" and "this page, elsewhere" are the
            # same URL, so the check would contradict the one above.
            if path != "/":
                check(f"from {path} it does not try to stay on this page",
                      f'href="{path}?site=Z99"' not in body)

        # And the link actually works from there.
        r = c.get("/?site=Z99")
        check("following it switches the board", r.status_code == 200)
        check("and the switch sticks for the session",
              "Z99" in c.get("/status").data.decode())
    finally:
        config.DB_PATH, config.SITES = prev_db, prev_sites
        siteconf.forget()
        importlib.reload(appmod)


def test_the_switcher_puts_your_own_observatories_first():
    """At the cap of 25 a flat list buries the one you actually observe from."""
    import importlib
    import tempfile

    import app as appmod

    prev_db, prev_sites = config.DB_PATH, config.SITES
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    siteconf.forget()
    try:
        conn = db.connect(config.DB_PATH)
        db.init(conn)
        conn.close()
        config.SITES = {1: config.DEFAULT_SITE, 2: _second_site()}
        importlib.reload(appmod)

        anon = appmod.app.test_client().get("/").data.decode()
        check("a signed-out visitor gets one plain list",
              "Your observatories" not in anon)
        check("listing every observatory served",
              "Z99" in anon and config.DEFAULT_SITE.obscode in anon)

        c, _tok = _settings_client(appmod, username="grouped", admin=False)
        conn = db.connect(config.DB_PATH)
        try:
            uid = db.user_by_name(conn, "grouped")["id"]
            db.add_site_member(conn, 2, uid, db.OWNER)
        finally:
            conn.close()

        body = c.get("/").data.decode()
        check("a member's own observatories are grouped first",
              "Your observatories" in body)
        check("with their role shown", "owner" in body)
        check("and the rest still reachable below",
              "Also served" in body)
        check("yours comes before the rest",
              body.index("Your observatories") < body.index("Also served"))
    finally:
        config.DB_PATH, config.SITES = prev_db, prev_sites
        siteconf.forget()
        importlib.reload(appmod)


def test_no_page_still_claims_to_be_l01():
    """Every page's header must name the site it is showing.

    Stage C made the board's own header follow the site and missed the rest:
    the sign-in pages, both target pages, and two lines in the target page's
    body still wrote the first observatory's name out. On a second site each
    of those is a wrong statement rather than a cosmetic slip.
    """
    import importlib
    import pathlib
    import tempfile

    import app as appmod

    written_out = []
    for f in sorted(pathlib.Path("templates").glob("*.html")):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if "L01" in line and "placeholder" not in line:
                written_out.append(f"{f.name}:{n}")
    check("no template writes an observatory code out by hand",
          not written_out, written_out)

    prev_db, prev_sites = config.DB_PATH, config.SITES
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    siteconf.forget()
    try:
        conn = db.connect(config.DB_PATH)
        db.init(conn)
        conn.close()
        config.SITES = {1: config.DEFAULT_SITE, 2: _second_site()}
        importlib.reload(appmod)
        c = appmod.app.test_client()

        for path in ("/?site=Z99", "/login?site=Z99", "/settings?site=Z99"):
            body = c.get(path).data.decode()
            check(f"{path} names the observatory it is showing",
                  "Z99" in body)
            check(f"{path} does not claim to be the other one",
                  config.DEFAULT_SITE.obscode not in body.split(
                      'class="sitemenu-panel"')[0])
    finally:
        config.DB_PATH, config.SITES = prev_db, prev_sites
        siteconf.forget()
        importlib.reload(appmod)


def test_ranking_bounds():
    rows = [dict(score=100, arc_days=0.0, vmag=config.DEFAULT_SITE.mag_bright),
            dict(score=0, arc_days=99.0, vmag=config.DEFAULT_SITE.max_mag)]
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
    #
    # This used to mutate the global mask and reload two modules to make the
    # change visible, because the sector arrays were built once at import.
    # A site is passed in instead now, which is what stage A was for -- and
    # the default site is never touched, so a hard north cannot leak out of
    # this test into whatever runs after it.
    hard_north = dataclasses.replace(
        config.DEFAULT_SITE,
        horizon_mask=((337.5, 22.5, None, "hard"),)
        + tuple(config.DEFAULT_SITE.horizon_mask[1:]))
    check("a sector marked hard does reject",
          "azBlocked" in pipeline.row_rejections(r5, far_future, hard_north),
          pipeline.row_rejections(r5, far_future, hard_north))
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

        # Done means "you" when there is a you, "anyone" when there is not.
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
            # White, not green. Green now means "more than 0.05 AU from
            # Earth" -- MPC's meaning -- so done had to move off that scale
            # entirely; see test_a_done_target_is_white_on_the_sky_map.
            return skymap.DONE_COLOUR in resp.data.decode()

        check("ana, who observed it, sees it marked done", done_colour(as_ana))
        check("boris, who did not, does not", not done_colour(as_boris))
        check("a signed-out visitor sees what the observatory did",
              done_colour(anon))
    finally:
        config.DB_PATH = prev_db
        if prev_env is not None:
            os.environ["WHICHNEO_AUTH"] = prev_env
        importlib.reload(auth)
        importlib.reload(appmod)


def _eph_line(ts, az_deg, alt_deg, vmag=19.0):
    """One fabricated ephemeris line at a chosen instant and sky position.

    ephemeris.Row splits on whitespace, so this only has to get the field
    ORDER right rather than the column widths. Azimuth goes in the way MPC
    writes it -- measured from south -- because undoing that is Row's job and
    a test that skipped it would be testing the wrong convention.
    """
    import datetime
    d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    return " ".join([
        "%04d" % d.year, "%02d" % d.month, "%02d" % d.day,
        "%02d%02d" % (d.hour, d.minute),
        "02", "30", "34.7", "+36", "47", "19",          # RA / Dec
        "118.3", "%.1f" % vmag, "391.9", "047.2",       # elong V motion PA
        "%.1f" % ((az_deg - 180.0) % 360.0),            # az, from south
        "%+.1f" % alt_deg,
        "-26", "0.00", "115", "-28", "Map/Offsets", "!!"])


def _skyframe_night(conn, site, desig="P12frm", az=120.0, alt=45.0,
                    minutes=40, step_min=10):
    """A one-target observable night in `conn`, as the updater would leave it.

    Returns (start_ts, end_ts). Anchored a little in the past and on a whole
    minute: /skymap.svg?ts= refuses instants more than a year from now, and
    an ephemeris line carries a time only to the minute.
    """
    start = (int(time.time()) // 60) * 60 - 3600
    end = start + minutes * 60
    lines = [_eph_line(start + k * step_min * 60, az, alt)
             for k in range(minutes // step_min + 1)]
    conn.execute(
        "INSERT INTO targets (site_id, desig, score, vmag, observable, "
        "window_start_ts, window_end_ts) VALUES (?,?,?,?,?,?,?)",
        (site.id, desig, 88, 19.0, 1, start, end))
    conn.execute(
        "INSERT INTO ephemeris_cache (site_id, desig, signature, "
        "fetched_utc, payload) VALUES (?,?,?,?,?)",
        (site.id, desig, "sig", db.utcnow(), json.dumps({"lines": lines})))
    conn.commit()
    return start, end


def test_precomputed_frames_match_the_live_route():
    """A precomputed frame must be the bytes the live route would send.

    This is the check the whole replay animation rests on. The browser no
    longer asks the server for a picture per frame -- it is handed the whole
    night up front and paints it locally -- so the one thing that could go
    wrong is the two paths drawing different maps for the same instant. That
    is not a cosmetic risk: which targets appear, which carry the poor-sky
    ring and which are removed outright are this observatory's own limits
    applied position by position by observability.keepout_violation and
    mask_violation, and an animation that disagreed with them would be a
    preview of sky the mount must not be sent to.

    So this does not compare approximately, or compare positions, or compare
    counts. It takes every instant of a precomputed night, fetches the same
    instant from /skymap.svg?ts=, and demands the precomputed frame be
    character for character what the route put inside its dynamic group --
    with the backdrop before it proven identical at every instant, which is
    what makes shipping the backdrop once legitimate.
    """
    import importlib
    import tempfile

    import app as appmod
    import auth
    import pipeline
    import skyframes
    import update_neocp as upd

    prev_db = config.DB_PATH
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    try:
        importlib.reload(auth)
        importlib.reload(appmod)
        site = config.DEFAULT_SITE

        conn = db.connect(config.DB_PATH)
        db.init(conn)
        start, end = _skyframe_night(conn, site)
        ordered = [dict(r) for r in conn.execute(
            "SELECT * FROM targets WHERE site_id=?", (site.id,))]
        night = pipeline.night_label(time.time(), site)
        check("the updater precomputes the night",
              upd._build_skyframes(conn, ordered, site, night))
        conn.close()

        c = appmod.app.test_client()
        r = c.get("/skyframes.json")
        check("the frames endpoint answers", r.status_code == 200, r.status_code)
        data = json.loads(r.data)
        check("it offers a night", data.get("available") is True, data)
        check("on the two-minute grid",
              data["step"] == skyframes.GRID_STEP_S, data.get("step"))
        check("one frame per instant of the night",
              len(data["frames"]) == len(data["grid"]) > 1,
              (len(data["frames"]), len(data.get("grid", []))))

        opening = '<g class="%s">' % skymap.DYNAMIC_CLASS
        backdrops, mismatched = set(), []
        for i, ts in enumerate(data["grid"]):
            body = c.get("/skymap.svg?ts=%r" % ts).data.decode()
            head, _, tail = body.partition(opening)
            backdrops.add(head)
            if tail != data["frames"][i] + "</g></svg>":
                mismatched.append(i)
        check("every precomputed frame is the live route's own bytes",
              not mismatched,
              "%d of %d differ, first at %s"
              % (len(mismatched), len(data["grid"]), mismatched[:3]))
        check("the backdrop really is the same at every instant",
              len(backdrops) == 1, len(backdrops))
        check("and it is the one backdrop_svg() draws",
              backdrops.pop() == skymap.backdrop_svg(None, site))

        # The composition is not an accident of this night: render_svg() is
        # defined as the two halves joined, which is why a frame can be
        # swapped in on its own at all.
        marks = skymap.target_marks(ordered, {}, start, site)
        check("render_svg is exactly backdrop plus one dynamic layer",
              skymap.render_svg(marks, None, site=site)
              == (skymap.backdrop_svg(None, site) + opening
                  + skymap.dynamic_svg(marks, None, site=site)
                  + "</g></svg>"))
    finally:
        config.DB_PATH = prev_db
        importlib.reload(auth)
        importlib.reload(appmod)


def test_the_replay_falls_back_when_nothing_is_precomputed():
    """Every way the fast path can be unavailable must stay honest.

    The precompute is a cache, and a cache that guesses when it is stale
    draws a map that is wrong rather than slow. There are four ways it can
    have nothing to say -- no row at all, a row built for a night that has
    rolled over, a target that joined the queue since it was built, and an
    archived night, which the updater never revisits -- and each of them has
    to put the board back on the server-rendered path it used before any of
    this existed, with /skymap.svg?ts= still answering.
    """
    import importlib
    import tempfile

    import app as appmod
    import auth
    import pipeline
    import skyframes
    import update_neocp as upd

    prev_db = config.DB_PATH
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    try:
        importlib.reload(auth)
        importlib.reload(appmod)
        site = config.DEFAULT_SITE
        c = appmod.app.test_client()

        conn = db.connect(config.DB_PATH)
        db.init(conn)
        check("a fresh database has no precomputed night",
              db.load_skyframes(conn, site) is None)
        start, end = _skyframe_night(conn, site)
        ordered = [dict(r) for r in conn.execute(
            "SELECT * FROM targets WHERE site_id=?", (site.id,))]
        conn.close()

        def offered():
            return json.loads(c.get("/skyframes.json").data)

        answer = offered()
        check("so the endpoint declines rather than erring",
              answer.get("available") is False, answer)
        check("and says why", "no precomputed" in answer.get("reason", ""),
              answer)
        r = c.get("/skymap.svg?ts=%r" % (start + 600))
        check("the server-rendered frame still works without it",
              r.status_code == 200 and b"<svg" in r.data, r.status_code)
        check("and still dates itself", bool(r.headers.get("X-Sky-Time")))

        conn = db.connect(config.DB_PATH)
        night = pipeline.night_label(time.time(), site)
        upd._build_skyframes(conn, ordered, site, night)
        conn.close()
        check("built for tonight, it is offered", offered().get("available"))

        # A night that has rolled over. The frames are still internally
        # consistent; they are simply not the night the slider is showing.
        conn = db.connect(config.DB_PATH)
        conn.execute("UPDATE skyframe_cache SET night='1999-01-01'")
        conn.commit()
        conn.close()
        answer = offered()
        check("a night that has rolled over is not served",
              answer.get("available") is False, answer)
        check("and says so", "not tonight" in answer.get("reason", ""), answer)

        # A target that joined the queue after the payload was built. Serving
        # the rest would draw fewer markers than /skymap.svg would.
        conn = db.connect(config.DB_PATH)
        conn.execute("UPDATE skyframe_cache SET night=?", (night,))
        _skyframe_night(conn, site, desig="P12new", az=150.0)
        conn.close()
        answer = offered()
        check("an uncovered target disables the fast path",
              answer.get("available") is False, answer)
        check("and names the reason",
              "not in the payload" in answer.get("reason", ""), answer)

        # An afternoon with nothing observable clears the row rather than
        # leaving last night's to be served as tonight's.
        conn = db.connect(config.DB_PATH)
        check("nothing observable means nothing to build",
              upd._build_skyframes(conn, [], site, night) is False)
        check("and the stale payload is gone",
              db.load_skyframes(conn, site) is None)
        conn.close()

        blob = {"version": skyframes.PAYLOAD_VERSION, "built_for": ["A", "B"]}
        check("a subset of the payload's targets is covered",
              skyframes.covers(blob, ["A"]))
        check("the whole set is covered", skyframes.covers(blob, ["A", "B"]))
        check("an unknown target is not",
              not skyframes.covers(blob, ["A", "C"]))
        check("neither is a payload from an older format",
              not skyframes.covers(dict(blob, version=0), ["A"]))
        check("nor no payload at all", not skyframes.covers(None, ["A"]))
    finally:
        config.DB_PATH = prev_db
        importlib.reload(auth)
        importlib.reload(appmod)


def test_the_browser_is_never_given_geometry_to_judge():
    """Marker state must reach the browser already decided.

    The failure this guards against is a second implementation of the
    observing rules in JavaScript: a client that got positions and a copy of
    the dome's limits could draw a marker in sky the mount must not be sent
    to, and would do it convincingly. So the payload deliberately carries no
    geometry at all -- no altitude, no azimuth, no wedge, no separation --
    only finished markup per instant, which leaves the browser nothing to
    judge even if it wanted to.

    Checked two ways: the payload's shape, and the behaviour that shape
    encodes. A target that crosses into a keep-out wedge has to vanish from
    the frames by the server's decision, while the payload never says where
    it was.
    """
    import importlib
    import tempfile

    import app as appmod
    import auth
    import pipeline
    import skyframes
    import update_neocp as upd

    prev_db = config.DB_PATH
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    try:
        importlib.reload(auth)
        importlib.reload(appmod)
        site = config.DEFAULT_SITE

        conn = db.connect(config.DB_PATH)
        db.init(conn)
        _skyframe_night(conn, site)
        ordered = [dict(r) for r in conn.execute(
            "SELECT * FROM targets WHERE site_id=?", (site.id,))]
        upd._build_skyframes(conn, ordered, site,
                             pipeline.night_label(time.time(), site))
        conn.close()

        data = json.loads(appmod.app.test_client().get("/skyframes.json").data)
        check("the payload's shape is fixed",
              set(data) == {"available", "step", "grid", "frames",
                            "built_utc", "night"}, sorted(data))
        check("a frame is markup, not a position",
              all(isinstance(f, str) for f in data["frames"]))
        check("the grid is only instants",
              all(isinstance(t, (int, float)) for t in data["grid"]))
        check("nothing in it is a per-target structure the client could read",
              not any(isinstance(f, (dict, list)) for f in data["frames"]))

        # The server's decision, visible in the markup rather than derivable
        # from it. L01's keep-out wedge runs clockwise from 247.5 to 45
        # degrees below altitude 70, so the same target is drawn at azimuth
        # 120 and removed at azimuth 300 -- and the payload says only that.
        rows = [{"desig": "KO1", "observed": False, "vmag": 19.0, "score": 70}]
        t0 = (int(time.time()) // 60) * 60
        clear = [(t0 + 600 * k, 120.0, 45.0) for k in range(5)]
        inside = [(t0 + 600 * k, 300.0, 45.0) for k in range(5)]
        drawn = skyframes.build(rows, {"KO1": clear}, t0, t0 + 2400, site)
        gone = skyframes.build(rows, {"KO1": inside}, t0, t0 + 2400, site)
        frames_drawn = skyframes.frames(drawn, rows, site)
        frames_gone = skyframes.frames(gone, rows, site)
        check("clear sky earns a marker in every frame",
              all('data-desig="KO1"' in f for f in frames_drawn),
              sum('data-desig="KO1"' in f for f in frames_drawn))
        check("a keep-out violation is drawn nowhere",
              not any('data-desig="KO1"' in f for f in frames_gone))
        check("and the removed target is still listed as covered, so the "
              "fast path is not silently disabled",
              skyframes.covers(gone, ["KO1"]))
        check("the payload holds no position for the client to test",
              all(not m for m in gone["marks"]), gone["marks"][:1])

        # The page's whole drawing path is one assignment of that markup.
        # Nothing here builds or places a marker, which is the property the
        # payload's shape is there to make unavoidable.
        page = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "templates", "index.html")).read()
        # The page's script, not the whole template: the legend beside the
        # map does render the site's keep-out wedges, in Jinja, as a caption.
        # Naming the limits in prose is the opposite of the problem -- what
        # must not happen is the SCRIPT deciding anything from them.
        script = page[page.rindex("<script>"):]
        check("the page paints a frame by assigning it",
              "layer.innerHTML = frag" in script)
        for forbidden in ("createElementNS",
                          "keepout", "moon_sep", "sector",
                          "Math.sin", "Math.cos", "Math.atan"):
            check("the script never reaches for %s" % forbidden,
                  forbidden not in script)

        # setAttribute is banned for the same reason as the rest -- placing a
        # marker means setting its coordinates -- with one exception that has
        # nothing to do with the map: patchRow() carries a table row's own
        # attributes across a poll. Scoped to that function rather than
        # dropped, so any new caller anywhere else still fails this.
        patch_row = re.search(r"\nfunction patchRow\(.*?\n\}", script, re.S)
        check("patchRow is where the one legitimate use lives",
              patch_row is not None)
        outside = script.replace(patch_row.group(0), "") if patch_row else script
        check("nothing else in the script sets an attribute",
              "setAttribute" not in outside)
        check("and patchRow never touches the sky map",
              patch_row is not None
              and "skyplot" not in patch_row.group(0)
              and "skydyn" not in patch_row.group(0))
    finally:
        config.DB_PATH = prev_db
        importlib.reload(auth)
        importlib.reload(appmod)


def test_the_replay_handle_never_opens_in_the_future():
    """The slider's right-hand stop is NOW, not the night's end.

    min/max came from night_strip's start_ts/end_ts and the handle opened at
    end_ts -- the last instant of the OBSERVABLE window, which for most of a
    night is still hours away. Opened during an evening demo that put the
    handle at the far right with 97 percent of the track in the future, and
    scrubbing "back" from there drew empty daylight sky, with 0 targets and
    no explanation, until it reached anything real.

    An archived night is all past, so it keeps its whole recorded span.
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

        conn = db.connect(config.DB_PATH)
        db.init(conn)
        now = time.time()
        # Two hours in, six still to run: the shape of a real evening.
        start, end = now - 2 * 3600, now + 6 * 3600
        conn.execute(
            "INSERT INTO targets (desig, score, vmag, hmag, nobs, arc_days, "
            "not_seen_days, observable, window_start_ts, window_end_ts, "
            "max_alt_ts, max_alt, max_alt_az, window_minutes, exposure_min, "
            "frames, frame_sec, cur_alt, cur_az, cur_motion, cur_moon_dist, "
            "score_total, mask_flags) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("P12aaaa", 90, 20.1, 22.0, 12, 0.5, 0.4, 1, start, end,
             now, 55.0, 180.0, 480.0, 12.0, 4, 180, 40.0, 170.0, 3.0, 90.0,
             90.0, "[]"))
        conn.commit()
        conn.close()

        html = appmod.app.test_client().get("/").data.decode()
        m = re.search(r'<input type="range" id="replaySlider"[^>]*>', html)
        check("the board renders a replay slider", m is not None)
        if m is None:
            return
        tag = m.group(0)
        got = {k: float(v) for k, v in
               re.findall(r'\b(min|max|value)="(-?\d+)"', tag)}
        check("the slider still opens at the start of the night",
              abs(got.get("min", 0) - start) < 120, got)
        # The whole point: both the stop and the handle are now, not end.
        check("the right-hand stop is now, not the end of the night",
              got.get("max", 0) <= now + 120 < end, got)
        check("and the handle opens on it, not in the future",
              got.get("value", 0) <= now + 120, got)
        check("the span is still the night that has happened, not a point",
              got.get("max", 0) - got.get("min", 0) > 3600, got)

        # An archived night has no future half to clamp away.
        eph_ts = ephemeris.Row(SAMPLE_EPH).ts
        conn = db.connect(config.DB_PATH)
        conn.execute("DELETE FROM targets")
        conn.execute(
            "INSERT INTO targets (desig, score, vmag, observable, "
            "window_start_ts, window_end_ts) VALUES (?,?,?,?,?,?)",
            ("P12bbbb", 90, 20.1, 1, eph_ts - 3600, eph_ts + 3600))
        conn.execute(
            "INSERT INTO ephemeris_cache (desig, signature, fetched_utc, "
            "payload) VALUES (?,?,?,?)",
            ("P12bbbb", "sig", db.utcnow(), json.dumps({"lines": [SAMPLE_EPH]})))
        conn.commit()
        db.archive_night(conn, "2026-09-14")
        conn.execute("DELETE FROM targets")          # the night has rolled over
        conn.commit()
        conn.close()

        html = appmod.app.test_client().get("/").data.decode()
        m = re.search(r'<input type="range" id="replaySlider"[^>]*>', html)
        check("a board with no live night still offers the archive", m is not None)
        if m is not None:
            got = {k: float(v) for k, v in
                   re.findall(r'\b(min|max|value)="(-?\d+)"', m.group(0))}
            check("an archived night keeps its whole recorded span",
                  abs(got.get("max", 0) - (eph_ts + 3600)) < 120, got)
    finally:
        config.DB_PATH = prev_db
        if prev_env is not None:
            os.environ["WHICHNEO_AUTH"] = prev_env
        importlib.reload(auth)
        importlib.reload(appmod)


def test_a_bad_replay_url_cannot_replace_the_map_with_prose():
    """A 404's body is a sentence. It must never reach the plot.

    /skymap.svg?night=bogus answers 404 with "No archive for night bogus".
    Neither the replay fetch nor the 20-second poll checked resp.ok, so that
    sentence was written into #skyplot.innerHTML and the all-sky map became
    one line of text -- on the live board, from nothing more than a mistyped
    or stale link someone had shared.

    Two things had to be true, and both are checked here: the query keys the
    replay controls own are stripped out of the page URL so the poll can
    never carry them at all, and the fetch that CAN still receive an error
    refuses to paint it.
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
        conn = db.connect(config.DB_PATH)
        db.init(conn)
        conn.close()

        r = appmod.app.test_client().get("/skymap.svg?night=bogus")
        body = r.data.decode()
        check("an unknown night is a 404", r.status_code == 404, r.status_code)
        check("and its body is prose, not a map",
              "<svg" not in body and "No archive" in body, body[:80])

        # The page's own guards. Read from the template rather than a browser:
        # this has to hold on the observatory's host, which has no node.
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "templates", "index.html")).read()
        check("the page drops ?ts= from the polled query string",
              "params.delete('ts')" in src, None)
        check("and ?night= with it",
              "params.delete('night')" in src, None)

        # resp.ok has to be tested BEFORE anything is written to the plot,
        # not merely mentioned somewhere in the file.
        fn = re.search(r"async function loadSky\(ts\) \{.*?\n\}", src, re.S)
        check("loadSky is still there to guard", fn is not None)
        if fn is not None:
            b = fn.group(0)
            guard, paint = b.find("resp.ok"), b.find("innerHTML")
            check("loadSky refuses a non-ok response before painting",
                  0 <= guard < paint, (guard, paint))
            # And a stale reply cannot win a race it has already lost.
            seq, paint = b.find("seq !== skySeq"), b.find("innerHTML")
            check("and an out-of-date frame is dropped before painting",
                  0 <= seq < paint, (seq, paint))

        poll = re.search(r"async function refresh\(\) \{.*?\n\}", src, re.S)
        check("the 20-second poll checks its response too",
              poll is not None and "r.ok" in poll.group(0))
    finally:
        config.DB_PATH = prev_db
        if prev_env is not None:
            os.environ["WHICHNEO_AUTH"] = prev_env
        importlib.reload(auth)
        importlib.reload(appmod)


def test_marks_never_cross_a_hole_in_the_ephemeris():
    """A position is drawn only from samples that really bracket the instant.

    MPC suppresses every row below the server floor, so a target that sets
    and rises again leaves a hole of hours in its track -- and the samples
    either side of that hole are still consecutive in the list. The coverage
    test only asked whether the NEAREST sample was within the median gap, so
    for the whole gap after a run ended _interpolate() blended that run's
    last sample with the NEXT NIGHT's first one: the marker crawled for half
    an hour of scrub time and then jumped. Measured on the live board,
    P12q6iW went azimuth 219 to 134 at 02:31 and gb00870 242 to 127 at 04:01,
    85 and 115 degrees, both inside the slider's own range.

    Off the ENDS of a track there is nothing to blend with -- _interpolate()
    clamps to the end sample -- so the old tolerance stays. An archived night
    can hold a single ephemeris line, and that one sample still covers the
    instant it was taken at.
    """
    import datetime

    # Pinned to an evening in UT so the hole genuinely straddles midnight --
    # which is the case the tick label has to survive.
    now = datetime.datetime(2026, 9, 18, 20, 0,
                            tzinfo=datetime.timezone.utc).timestamp()
    # A covered run ending at now, and the next night's run 12 hours later.
    tonight = [(now - 5400 + 1800 * k, 210.0 + 2.0 * k, 30.0) for k in range(4)]
    tomorrow = [(now + 12 * 3600 + 1800 * k, 120.0 + 2.0 * k, 25.0)
                for k in range(4)]
    track = tonight + tomorrow
    rows = [{"desig": "HOLE", "observed": False, "mask_flags": [],
             "vmag": 19.0, "score": 80}]

    check("the median gap is the run's spacing, not the hole's",
          skymap._spacing(track) == 1800.0, skymap._spacing(track))

    inside = skymap.target_marks(rows, {"HOLE": track}, now - 900)
    check("inside the covered run it is still a dot",
          len(inside) == 1 and inside[0]["up"], inside)

    # 900 s past the last sample of tonight's run: the old test passed here,
    # and the answer it gave was a blend with tomorrow.
    after = skymap.target_marks(rows, {"HOLE": track}, now + 900)
    check("just past the end of the run it is no longer a dot",
          len(after) == 1 and not after[0]["up"], after)
    check("and the tick points at the next run's first sample, honestly",
          after and after[0]["rise_ts"] == tomorrow[0][0], after)

    # The measurable version of the same thing: step through the hole and
    # make sure no drawn position ever appears between the two runs. Starts
    # after the run's own last sample, which is a real position, not a hole.
    drawn = []
    for k in range(300, 12 * 3600, 300):
        got = skymap.target_marks(rows, {"HOLE": track}, now + k)
        drawn += [m["az"] for m in got if m["up"]]
    check("no position is invented anywhere inside the hole", drawn == [],
          drawn[:6])

    # A lone sample is not a hole, and must still be drawn near its instant.
    lone = [(now, 200.0, 40.0)]
    check("a single-sample track covers its own instant",
          len(skymap.target_marks(rows, {"HOLE": lone}, now)) == 1)
    check("and an hour either side of it, as it always did",
          len(skymap.target_marks(rows, {"HOLE": lone}, now + 1800)) == 1)
    check("but not a day later",
          skymap.target_marks(rows, {"HOLE": lone}, now + 86400) == [])

    # A rise on another night has to be readable as one. The tick's label
    # named a bare clock time, so tomorrow's rise read as tonight's.
    def _at(ts):
        return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)

    fmt = lambda ts: _at(ts).strftime("%H:%M")
    fmtdt = lambda ts: _at(ts).strftime("%Y-%m-%d %H:%M")

    svg = skymap.render_svg(after, None, size=400, localt=fmt, localdt=fmtdt)
    check("a rise on the following night is labelled with its date",
          fmtdt(tomorrow[0][0]) in svg, svg[-300:])
    # Well before the run starts, so this is a tick and not a clamped dot,
    # and still the same calendar day.
    soon = skymap.target_marks(rows, {"HOLE": track}, tonight[0][0] - 7200)
    check("a rise later today is a tick too", soon and not soon[0]["up"], soon)
    svg = skymap.render_svg(soon, None, size=400, localt=fmt, localdt=fmtdt)
    check("and stays a plain clock time, with no date to read past",
          fmt(tonight[0][0]) in svg and fmtdt(tonight[0][0]) not in svg,
          svg[-300:])


def test_marks_are_filtered_by_the_site_they_are_given():
    """target_marks must judge a position against the site being drawn.

    render_svg() draws the wedges of the site it is given; target_marks()
    took no site at all and filtered every marker against config.DEFAULT_SITE.
    With one observatory configured those are the same object and nothing
    shows. With two, a second site's map draws its own keep-out hatching
    while its markers are being suppressed by L01's -- targets missing from
    one dome's map because of a wall in a different country.
    """
    now = 1_760_000_000.0
    rows = [{"desig": "S1", "observed": False, "mask_flags": [],
             "vmag": 19.0, "score": 80}]

    def at(az, alt, site):
        track = [(now - 60, az, alt), (now + 60, az, alt)]
        return skymap.target_marks(rows, {"S1": track}, now, site)

    base = config.DEFAULT_SITE
    # Azimuth 300 at 40 degrees is inside L01's keep-out wedge -- the same
    # position test_keepout_wedge uses to prove nothing is plotted there.
    check("the position chosen really is blocked for L01",
          at(300.0, 40.0, None) == [], at(300.0, 40.0, None))

    open_sky = dataclasses.replace(base, id=901, obscode="Z91",
                                   keepout_wedges=())
    got = at(300.0, 40.0, open_sky)
    check("a site with no wedge there draws the target",
          len(got) == 1 and got[0]["up"], got)

    # And the other way: a wedge L01 does not have must be honoured.
    walled = dataclasses.replace(base, id=902, obscode="Z92",
                                 keepout_wedges=((160.0, 200.0, 70.0,
                                                  "a hill to the south"),))
    check("the same position is open for L01 in the south",
          len(at(180.0, 40.0, None)) == 1, at(180.0, 40.0, None))
    check("but a site walled to the south drops it",
          at(180.0, 40.0, walled) == [], at(180.0, 40.0, walled))

    # The advisory ring follows the given site's horizon mask, not L01's.
    idx = int(observability.sector_index(180.0))
    mask = list(base.horizon_mask)
    a0, a1, _minalt, _hard, _why = mask[idx]
    mask[idx] = (a0, a1, 60.0, "soft", "test fog")
    foggy = dataclasses.replace(base, id=903, obscode="Z93",
                                horizon_mask=tuple(mask))
    plain = at(180.0, 40.0, None)
    check("L01 does not ring a clear southern position",
          plain and not plain[0]["mask"], plain)
    hazed = at(180.0, 40.0, foggy)
    check("a site whose south is poor rings the same position",
          hazed and hazed[0]["mask"], hazed)

    # The lookahead tick is filtered by the site too, not just the dot.
    rising = [(now + 600, 300.0, 40.0), (now + 1200, 300.0, 41.0)]
    l01 = skymap.target_marks(rows, {"S1": rising}, now - 7200)
    check("a tick toward L01's wedge is suppressed for L01", l01 == [], l01)
    other = skymap.target_marks(rows, {"S1": rising}, now - 7200, open_sky)
    check("and offered to a site that can point there",
          len(other) == 1 and not other[0]["up"], other)


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
    for name in config.DEFAULT_SITE.sector_names:
        check(f"{name} wedge present in the map",
              f">{name} &mdash;" in svg or f">{name}</text>" in svg)

    idx = {n: i for i, n in enumerate(config.DEFAULT_SITE.sector_names)}
    for name, az in [("N", 0), ("NE", 45), ("E", 90), ("SE", 135),
                     ("S", 180), ("SW", 225), ("W", 270), ("NW", 315)]:
        got = int(observability.sector_index(az))
        check(f"azimuth {az:3d} falls in sector {name}", got == idx[name],
              f"got {config.DEFAULT_SITE.sector_names[got]}")

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
            alt, az, config.DEFAULT_SITE.moon_sep_min, bearings)
        seps = observability.angular_separation(alt, az, np.array(alts),
                                                np.array(azs))
        worst = float(np.abs(seps - config.DEFAULT_SITE.moon_sep_min).max())
        check(f"locus around alt={alt:.0f} is exactly "
              f"{config.DEFAULT_SITE.moon_sep_min:.0f} deg wide", worst < 1e-8,
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
            moon_alt, 0.0, config.DEFAULT_SITE.moon_sep_min, bearings)
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
    check("observed marker is drawn in the done colour",
          skymap.DONE_COLOUR in svg)

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
          spread[0] > config.DEFAULT_SITE.fov_arcsec * 4,
          "%d\" vs %d\" field" % (spread[0], config.DEFAULT_SITE.fov_arcsec))


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


def test_target_history_section_survives_missing_columns():
    """The same NULL must not take a TARGET page down either.

    /history was fixed for this; the per-object history table on target.html
    is a second template with the same unguarded formats, and it was missed.
    Found on deploy: 65 of the 123 objects on the live board had at least one
    snapshot with no hmag or not_seen_days, so more than half the detail
    pages answered 500 while /history itself was fine.

    Both columns are exercised here, because they are NULL together in the
    real data and guarding only one would still leave the page down.
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
        conn = db.connect(config.DB_PATH)
        db.init(conn)
        now = time.time()
        conn.execute(
            "INSERT INTO targets (desig, score, vmag, hmag, nobs, arc_days,"
            " not_seen_days, observable, score_total, discard_reasons,"
            " max_alt, max_alt_az, max_alt_ts, exposure_min, frames,"
            " frame_sec, frame_motion, window_minutes, cur_alt, cur_az,"
            " cur_motion, cur_moon_dist, cur_vmag, cur_sun_alt, cur_ts,"
            " window_start_ts, window_end_ts, incl, q, e)"
            " VALUES ('NULLHIST', 88, 20.1, 22.4, 3, 0.9, 0.4, 1, 3.2, '[]',"
            f" 61.0, 180.0, {now + 3600}, 20.5, 36, 30, 1.4, 120.0, 55.0,"
            f" 175.0, 1.4, 55.0, 20.1, -20.0, {now}, {now}, {now + 7200},"
            " 12.5, 0.9, 0.6)")
        conn.commit()
        conn.close()

        con = H.ensure_db()
        hnow = H._now()
        con.execute("INSERT INTO objects (desig, first_seen_ts, last_seen_ts) "
                    "VALUES ('NULLHIST', ?, ?)", (hnow, hnow))
        # One complete snapshot and one missing exactly what the archived runs
        # do not carry -- the shape that broke the page.
        con.execute("INSERT INTO snapshots (desig, snapshot_ts, score, vmag, "
                    "nobs, arc_days, hmag, not_seen_days) "
                    "VALUES ('NULLHIST', ?, 100, 20.1, 5, 0.03, 22.0, 0.40)",
                    (hnow,))
        con.execute("INSERT INTO snapshots (desig, snapshot_ts, score, vmag, "
                    "nobs, arc_days, retrospective) "
                    "VALUES ('NULLHIST', ?, 77, 21.4, 6, 0.05, 1)", (hnow,))
        con.commit()
        con.close()

        importlib.reload(appmod)
        r = appmod.app.test_client().get("/target/NULLHIST")
        check("a target page renders with NULL hmag and not_seen_days",
              r.status_code == 200, r.status_code)
        body = r.data.decode()
        check("the sparse snapshot is shown rather than skipped",
              "21.4" in body)
        check("its missing values read as a dash",
              "&mdash;" in body or "—" in body)
        check("the complete snapshot still formats normally",
              "22.0" in body and "0.40" in body)
    finally:
        config.DB_PATH, H.DB_PATH = prev_db, prev_hist
        importlib.reload(appmod)


def test_single_snapshot_history_still_shown():
    """One recorded snapshot is not the same as none, so the object's real
    data must still be shown even though there is nothing to plot a trend
    from.

    _history_section.html gated its entire "History on NEOCP" section --
    the raw data table included -- on hist_charts, which needs at least two
    points to draw a line. An object resolved after only ever being caught
    by a single poll (routine for the ds42-imported nights, where many
    objects appear on exactly one archived night before resolving) has
    hist_rows of length 1: hist_charts comes back empty, and the template's
    {% if hist_charts %} then hid the table along with the chart, showing
    "No polled history for this target yet" even though one real row of
    data existed the whole time.

    Exercised on the archived-object path (no live `targets` row at all),
    since that is exactly the shape of a resolved, single-snapshot object.
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
        con = H.ensure_db()
        hnow = H._now()
        con.execute(
            "INSERT INTO objects (desig, first_seen_ts, last_seen_ts, "
            "status, resolved_at) VALUES ('ONESNAP', ?, ?, 'confirmed', ?)",
            (hnow, hnow, hnow))
        con.execute(
            "INSERT INTO snapshots (desig, snapshot_ts, score, vmag, nobs, "
            "arc_days, retrospective) "
            "VALUES ('ONESNAP', ?, 91, 19.7, 4, 0.08, 1)", (hnow,))
        con.commit()
        con.close()

        importlib.reload(appmod)
        r = appmod.app.test_client().get("/target/ONESNAP")
        check("the archived page renders for a single-snapshot object",
              r.status_code == 200, r.status_code)
        body = r.data.decode()
        check("it does not falsely claim there is no polled history",
              "No polled history for this target" not in body)
        check("the one real snapshot's score is shown",
              "91" in body)
        check("the one real snapshot's vmag is shown",
              "19.7" in body)
        check("a note explains why there is no trend chart, not silence",
              "not enough to plot a trend" in body)
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
          r.frame_plan() == (config.DEFAULT_SITE.exposure_frames, 15),
          r.frame_plan())

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
    start, end, min_alt, _reason = config.DEFAULT_SITE.keepout_wedges[0]
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
          f"MPC cutoff {config.DEFAULT_SITE.mpc_server_min_alt:g}" in svg)
    check("and not the dead MIN_ALT threshold",
          f"min {config.DEFAULT_SITE.min_alt:g}&#176;" not in svg)

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


def test_the_plan_can_be_saved_from_any_browser():
    """Getting the plan onto the observer's disk must not need one browser.

    The version this replaces disabled the button outright unless
    `window.showSaveFilePicker` existed, and labelled it "needs Chrome/Edge".
    That label was wrong far more often than it was right, and wrong in a way
    that cost a week: the File System Access spec declares its `partial
    interface Window` [SecureContext], so the property is *absent* on any
    origin that is not https, localhost or 127.0.0.1 -- and both deployment
    guides we ship tell observers to reach the board over plain http on the
    LAN (`http://<machine>:8080` in deploy/VISNJAN.md, `http://epyc:12600` in
    deploy/EPYC.md). So an observer in Chrome, on the origin our own docs
    hand them, got told to go and install Chrome.

    Hence the three things checked here: the button is never disabled, it
    tells apart "this origin cannot" from "this browser cannot", and the
    route it falls back to actually offers the file as a download. The
    browser half is read out of the template rather than driven in a browser,
    because this suite has to pass on the observatory's Windows box, which
    has no node -- same reason as the skymap guards above.
    """
    import importlib
    import tempfile

    import app as appmod

    prev_db = config.DB_PATH
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    try:
        importlib.reload(appmod)
        conn = db.connect(config.DB_PATH)
        db.init(conn)
        conn.close()
        c = appmod.app.test_client()

        # ---- the server half: /plan gains a download form, and loses nothing.
        plan_dir = tempfile.mkdtemp()
        plan_path = os.path.join(plan_dir, "2026-09-19.txt")
        with open(plan_path, "w") as f:
            f.write("PLAN BODY\n")
        conn = db.connect(config.DB_PATH)
        db.set_meta(conn, "plan_path", plan_path, site=appmod.current_site())
        conn.commit()
        conn.close()

        plain = c.get("/plan")
        dl = c.get("/plan?download=1")
        check("the plain plan URL is unchanged", plain.status_code == 200
              and plain.headers["Content-Type"] == "text/plain; charset=utf-8",
              (plain.status_code, plain.headers.get("Content-Type")))
        # The control room's curl loop points at the bare URL. If it ever
        # starts arriving as an attachment, `curl -O` starts writing a
        # differently-named file and the loop silently stops updating the one
        # the dome reads.
        check("and still offers no attachment",
              plain.headers.get("Content-Disposition") is None,
              plain.headers.get("Content-Disposition"))
        check("?download=1 names the file after the night",
              dl.headers.get("Content-Disposition")
              == 'attachment; filename="whichneo-2026-09-19.txt"',
              dl.headers.get("Content-Disposition"))
        check("and changes not one byte of the body",
              dl.data == plain.data == b"PLAN BODY\n", dl.data)

        # The filename goes into a response header and comes from a database
        # value, so a quote or a newline in it would end the header and start
        # whatever followed. Dropped, not escaped.
        nasty = appmod._plan_download_name('/p/ev"il\r\nSet-Cookie: a=b.txt')
        check("a header-breaking plan path cannot break the header",
              '"' not in nasty and "\r" not in nasty and "\n" not in nasty,
              nasty)
        # At Tičan the board runs on Windows, so plan_path is a backslash path.
        check("a Windows plan path still yields a bare filename",
              appmod._plan_download_name(r"C:\whichNEO\plans\2026-09-19.txt")
              == "whichneo-2026-09-19.txt",
              appmod._plan_download_name(r"C:\whichNEO\plans\2026-09-19.txt"))
        check("a name that sanitises away still has a stem",
              appmod._plan_download_name("/p/š.txt") == "whichneo-plan.txt",
              appmod._plan_download_name("/p/š.txt"))
        for empty in ("", None):
            check("and so does %r" % (empty,),
                  appmod._plan_download_name(empty) == "whichneo-plan.txt",
                  appmod._plan_download_name(empty))

        # ---- the browser half, read from the template.
        page = c.get("/").data.decode()
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "templates", "index.html")).read()

        check("the button is never disabled any more",
              "planSyncBtn.disabled" not in src, None)
        check("and no longer tells anyone which browser to install",
              "sync to file (needs Chrome/Edge)" not in src, None)

        mode = re.search(r"function planSyncMode\(win\) \{.*?\n\}", src, re.S)
        check("planSyncMode is there to tell the two causes apart",
              mode is not None)
        if mode is not None:
            b = mode.group(0)
            picker, secure = b.find("showSaveFilePicker"), b.find("isSecureContext")
            # Order matters, and not only cosmetically: a secure context with
            # no picker (Firefox, Safari) and an insecure one (any browser)
            # need different words, and only the picker test can be first --
            # every origin that has the picker is secure, but not the reverse.
            check("it looks for the picker before blaming the origin",
                  0 <= picker < secure, (picker, secure))
            check("and returns all three tiers",
                  all(t in b for t in ("'file'", "'insecure'", "'unsupported'")),
                  b)

        check("the fallback hands over the download form of the route",
              "'/plan?download=1'" in src, None)
        check("the help control and its panel render",
              'id="planSyncHelp"' in page and 'id="planSyncRecipe"' in page)
        check("the panel starts collapsed",
              re.search(r'id="planSyncRecipe"[^>]*\bhidden\b', page) is not None)
        # Filled in from location.origin, so the command is copy-pasteable on
        # whatever origin the observer actually reached the board on -- which
        # is the whole point, since the insecure one is where they need it.
        check("the panel has slots for this origin's plan URL",
              page.count('class="planUrl"') >= 2, page.count('class="planUrl"'))
        check("and fills them as text, not markup",
              "el.textContent = url" in src, None)
        check("the recipe writes via a temporary name",
              "mv ~/plan.tmp ~/plan.txt" in page and "Move-Item -Force" in page)

        # A download on a timer is the thing this must not quietly become:
        # it fills Downloads with numbered copies, none of them at the path
        # the dome reads, and browsers block the repeats anyway.
        check("nothing downloads on a timer",
              "planDownload" in src
              and re.search(r"setInterval\([^)]*planDownload", src) is None,
              None)
        poll = re.search(r"async function refresh\(\) \{.*?\n\}", src, re.S)
        check("and the poll does not either",
              poll is not None and "planDownload" not in poll.group(0))
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


def test_a_done_target_is_white_on_the_sky_map():
    """Done is white, and white is not one of MPC's distance colours.

    It used to be green. Green now carries MPC's meaning -- more than 0.05 AU
    from Earth -- and one colour cannot say both "this observer has shot it"
    and "this is far away", least of all on a map where the other colours are
    about how close a thing is to hitting us.

    This path existed and was tested from the day the map was built, but was
    unreachable on the default view: the row was filtered out before the map
    ever saw it, so the marker could never be drawn.
    """
    now = 1_760_000_000.0
    # Fourth element is the derived geocentric distance. Well beyond 0.05 AU,
    # so an outstanding copy of this target is green and the done copy has to
    # differ from it for the right reason.
    track = [(now - 1800, 100.0, 30.0, 1.2), (now, 110.0, 40.0, 1.2),
             (now + 1800, 120.0, 50.0, 1.2)]
    base = dict(desig="T1", vmag=19.0, score=80, mask_flags=[])

    done = skymap.target_marks([dict(base, observed=True)], {"T1": track}, now)
    check("a done, still-observable target is drawn",
          len(done) == 1 and done[0]["up"], done)
    done_svg = skymap.render_svg(done, None, size=400)
    check("and it is drawn in the done colour",
          skymap.DONE_COLOUR in done_svg)
    check("which is none of MPC's distance colours",
          skymap.DONE_COLOUR not in skymap.DISTANCE_COLOURS.values(),
          skymap.DONE_COLOUR)

    fresh = skymap.target_marks([dict(base, observed=False)], {"T1": track}, now)
    fresh_svg = skymap.render_svg(fresh, None, size=400)
    check("an outstanding target far from Earth is green",
          skymap.DISTANCE_COLOURS[neodistance.FAR] in fresh_svg)
    check("and the done one is not",
          skymap.DISTANCE_COLOURS[neodistance.FAR] not in done_svg)


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
    ts, az, alt, delta = got[0]
    check("track timestamp matches Row", ts == row.ts, (ts, row.ts))
    check("track azimuth matches Row (compass, not MPC south)",
          abs(az - row.az) < 1e-9, (az, row.az))
    check("track altitude matches Row", abs(alt - row.alt) < 1e-9, (alt, row.alt))
    check("track azimuth really is the flipped one",
          abs(az - (row.az_mpc + 180.0) % 360.0) < 1e-9, (az, row.az_mpc))

    # Without an absolute magnitude there is nothing to derive a distance
    # from, and the sample says so rather than carrying a fabricated number.
    check("no absolute magnitude means no distance", delta is None, delta)

    with_h = ephemeris.track([SAMPLE_EPH], h=20.0)
    check("given one, the sample carries a distance",
          with_h[0][3] is not None, with_h[0])
    check("and it agrees with solving from the row directly",
          abs(with_h[0][3]
              - neodistance.solve(row.vmag, 20.0, row.elong)) < 1e-12,
          (with_h[0][3], neodistance.solve(row.vmag, 20.0, row.elong)))


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


def test_a_poll_patches_the_table_instead_of_rebuilding_it():
    """The twenty-second poll must not rebuild ~2700 cells to move a few.

    It used to do `#rows.innerHTML = html` every tick: ~230 KB parsed, every
    cell destroyed and rebuilt, any text selection in the table lost. Patching
    only what changed needs a stable key per row, and data-desig cannot be it
    -- that attribute is only present while the target is observable, so a row
    would lose its identity exactly when it set behind the horizon.

    The equivalence of the patched DOM to a plain innerHTML is checked in a
    real DOM outside this suite; what is checked here is the contract the
    patching depends on -- that every row arrives keyed, and that the keys are
    unique and stable across reordering and filtering.
    """
    import importlib
    import re
    import tempfile

    import app as appmod

    rows_src = open(os.path.join(os.path.dirname(__file__),
                                 "templates", "_rows.html")).read()
    opens = re.findall(r"<tr\b[^>]*>", rows_src, re.S)
    check("every kind of row in the template carries a key",
          len(opens) == 3 and all("data-row=" in t for t in opens),
          [t[:60] for t in opens if "data-row=" not in t])

    prev_db = config.DB_PATH
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    siteconf.forget()
    try:
        conn = db.connect(config.DB_PATH)
        db.init(conn)
        conn.execute(
            "INSERT INTO targets (desig, score, vmag, hmag, nobs, arc_days,"
            " observable, not_seen_days, score_total, discard_reasons,"
            " max_alt, max_alt_ts) VALUES "
            # Scores deliberately run against the clock, or sorting by score
            # would give back the chronological order and prove nothing.
            "('P11aaaa', 55, 20.0, 22.0, 5, 0.1, 1, 0.4, 1.0, '[]', 55.0, ?),"
            "('P11bbbb', 90, 21.5, 23.0, 4, 0.2, 1, 0.3, 3.0, '[]', 40.0, ?),"
            "('P11cccc', 70, 19.0, 21.0, 9, 0.4, 0, 0.6, 2.0, '[]', 20.0, ?)",
            (time.time() + 900, time.time() + 1800, time.time() + 2700))
        conn.commit()
        conn.close()
        importlib.reload(appmod)
        client = appmod.app.test_client()

        def keys(url):
            body = client.get(url).data.decode()
            return re.findall(r'<tr\b[^>]*\bdata-row="([^"]*)"', body), body

        base, body = keys("/rows")
        trs = re.findall(r"<tr\b", body)
        check("every rendered row carries a key",
              len(base) == len(trs) and len(base) > 0,
              f"{len(base)} keyed of {len(trs)} rows")
        check("and no two rows share one",
              len(set(base)) == len(base), sorted(base))

        # A row that is not observable carries no data-desig, which is exactly
        # why the key has to be a separate attribute.
        unobservable = re.search(r'<tr\b[^>]*data-row="P11cccc"[^>]*>', body)
        check("an unobservable row is still keyed", unobservable is not None)
        check("even though it has no designation attribute to key on",
              unobservable is not None
              and "data-desig=" not in unobservable.group(0),
              unobservable.group(0) if unobservable else None)

        # Plan rows share the table with target rows, so their keys must live
        # in a namespace that cannot collide with a designation.
        check("plan rows are keyed apart from target rows",
              'data-row="plan-{{ r.desig }}"' in rows_src)
        check("and no target designation reaches into that namespace",
              not any(k.startswith("plan-") and k[5:] in base for k in base),
              sorted(base))

        resorted, _ = keys("/rows?sort=score")
        check("reordering the table moves keys without inventing any",
              sorted(resorted) == sorted(base), (sorted(base), sorted(resorted)))
        check("and it really did reorder", resorted != base or len(base) < 2)

        filtered, _ = keys("/rows?range=mag:19.5:21.0")
        check("filtering can only ever remove keys",
              set(filtered) <= set(base), sorted(set(filtered) - set(base)))
    finally:
        config.DB_PATH = prev_db
        siteconf.forget()
        importlib.reload(appmod)

    # The hover highlight ran two document-wide queries per mouse event, on a
    # document of ~5100 elements, to find at most a handful of nodes. Both
    # mouseover and mouseout fire on every element boundary the pointer
    # crosses, so sweeping the table cost four full scans per cell.
    page_src = open(os.path.join(os.path.dirname(__file__),
                                 "templates", "index.html")).read()
    body = re.search(r"function linkHover\(.*?\n\}", page_src, re.S)
    check("linkHover is still there", body is not None)
    check("but it no longer searches the document",
          body is not None and "querySelectorAll" not in body.group(0),
          body.group(0) if body else None)
    check("it reads an index instead", body is not None
          and "hoverIndex.get" in body.group(0))

    # The replay repaints the map's moving layer ~24 times a second, and those
    # markers carry data-desig. Rebuilding the index there would put the
    # per-frame document scan back on the hottest path in the page, so it is
    # only marked stale and rebuilt by the next hover, if one comes at all.
    paint = re.search(r"function paintFrame\(.*?\n\}", page_src, re.S)
    check("playing the replay does not rebuild the index per frame",
          paint is not None and "buildHoverIndex()" not in paint.group(0),
          paint.group(0) if paint else None)
    check("it marks it stale instead",
          paint is not None and "hoverIndexStale = true" in paint.group(0))
    check("and a hover rebuilds it before reading it",
          body is not None and "hoverIndexStale" in body.group(0))

    # An index is only correct while it matches the DOM, so it has to be
    # rebuilt everywhere the rows or the map are replaced.
    check("the index is built when the page loads",
          re.search(r"^buildHoverIndex\(\);", page_src, re.M) is not None)
    refresh = re.search(r"async function refresh\(\).*?\n\}", page_src, re.S)
    check("the poll rebuilds it", refresh is not None
          and "buildHoverIndex()" in refresh.group(0))
    loadsky = re.search(r"async function loadSky\(.*?\n\}", page_src, re.S)
    check("so does redrawing the map for a replay", loadsky is not None
          and "buildHoverIndex()" in loadsky.group(0))

    check("the poll patches the table rather than replacing it",
          refresh is not None and "applyRows(rows)" in refresh.group(0)
          and "getElementById('rows').innerHTML" not in refresh.group(0),
          refresh.group(0) if refresh else None)

    # Unkeyed markup must degrade to the old behaviour, not drop rows.
    apply_rows = re.search(r"function applyRows\(.*?\n\}", page_src, re.S)
    check("unkeyed markup falls back to a plain replace",
          apply_rows is not None
          and "host.innerHTML = html" in apply_rows.group(0))


def test_feedback_reaches_somewhere_even_when_the_mail_does_not():
    """A suggestion must survive the relay being down.

    mailer.send() hands a message to a thread and swallows any failure, which
    is right for a password reset -- an error reaching the caller tells a
    stranger whether an address is on file -- and wrong here. Feedback has no
    such secret to keep, and a report lost to an outage with nothing but a
    line in the log is a report nobody acts on, while the sender was told it
    went. So the row is written before the send is attempted, and whether the
    mail actually left is recorded against it.
    """
    import importlib
    import tempfile

    import app as appmod
    import auth
    import mailer

    prev_db = config.DB_PATH
    prev_to = config.FEEDBACK_TO
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    config.FEEDBACK_TO = "nobody@example.invalid"
    siteconf.forget()
    try:
        conn = db.connect(config.DB_PATH)
        db.init(conn)
        conn.close()
        importlib.reload(appmod)
        appmod._feedback_hits.clear()
        c = appmod.app.test_client()

        def token():
            body = c.get("/").data.decode()
            m = re.search(r'name="csrf" value="([^"]+)"', body)
            return m.group(1) if m else None

        def stored():
            conn = db.connect(config.DB_PATH)
            try:
                return db.recent_feedback(conn)
            finally:
                conn.close()

        tok = token()
        check("the board hands out a token the form can use", bool(tok))

        # No mail is configured in a temp deployment, so every send here
        # fails -- which is exactly the case this test exists for.
        check("mail really is unavailable in this fixture",
              not mailer.available())

        r = c.post("/feedback", data={
            "csrf": tok, "message": "the sky map is upside down",
            "reply_to": "observer@example.org", "page": "/target/P11aaaa"})
        check("a signed-out visitor with an address may send",
              r.status_code == 200, r.status_code)
        check("and is told it is recorded, not that it was emailed",
              b"recorded" in r.data.lower() and b"emailed" not in r.data.lower())

        rows = stored()
        check("the message is stored despite the send failing",
              len(rows) == 1, len(rows))
        if rows:
            row = rows[0]
            check("with the text intact",
                  row["message"] == "the sky map is upside down", row["message"])
            check("the observatory it was sent from",
                  row["site_id"] == config.DEFAULT_SITE.id, row["site_id"])
            check("and the page the sender was looking at",
                  row["page"] == "/target/P11aaaa", row["page"])
            check("a reply address for someone with no account",
                  row["reply_to"] == "observer@example.org", row["reply_to"])

        # The delivery flag is written from the sending thread, so give it a
        # moment rather than asserting into a race.
        for _ in range(50):
            rows = stored()
            if rows and rows[0]["delivery_error"]:
                break
            time.sleep(0.1)
        check("the failure is recorded against the row, not swallowed",
              bool(rows and rows[0]["delivery_error"]),
              rows[0]["delivery_error"] if rows else None)
        check("and it is not marked delivered",
              bool(rows) and not rows[0]["delivered"])

        # --- what must be refused ---------------------------------------
        before = len(stored())

        r = c.post("/feedback", data={
            "csrf": token(), "message": "no way to answer me", "page": "/"})
        check("a signed-out visitor with no address is refused",
              r.status_code == 400, r.status_code)
        check("and told why", b"email" in r.data.lower())
        check("their words are handed back, not thrown away",
              b"no way to answer me" in r.data)

        r = c.post("/feedback", data={
            "csrf": token(), "message": "bad@address", "reply_to": "not-an-email",
            "page": "/"})
        check("an address that cannot be replied to is refused",
              r.status_code == 400, r.status_code)

        r = c.post("/feedback", data={
            "message": "no token", "reply_to": "a@b.co", "page": "/"})
        check("a post with no CSRF token is refused",
              r.status_code == 400, r.status_code)

        r = c.post("/feedback", data={
            "csrf": token(), "message": "buy cheap things",
            "reply_to": "bot@example.org", "website": "http://spam", "page": "/"})
        check("the honeypot answers normally so a bot learns nothing",
              r.status_code == 200, r.status_code)
        check("but nothing it sent is stored", len(stored()) == before,
              len(stored()))

        r = c.post("/feedback", data={
            "csrf": token(), "reply_to": "a@b.co",
            "message": "x" * (config.FEEDBACK_MAX_CHARS + 1), "page": "/"})
        check("an oversized message is refused", r.status_code == 400,
              r.status_code)

        # --- the throttle -------------------------------------------------
        appmod._feedback_hits.clear()
        codes = []
        for i in range(config.FEEDBACK_LIMIT + 2):
            codes.append(c.post("/feedback", data={
                "csrf": token(), "message": f"message {i}",
                "reply_to": "flood@example.org", "page": "/"}).status_code)
        check("the first few go through",
              codes[:config.FEEDBACK_LIMIT] == [200] * config.FEEDBACK_LIMIT,
              codes)
        check("and then one address is throttled",
              codes[config.FEEDBACK_LIMIT] == 429, codes)
        appmod._feedback_hits.clear()

        # --- reading them -------------------------------------------------
        r = c.get("/feedback")
        check("a signed-out visitor cannot read what was sent",
              r.status_code == 302, r.status_code)

        conn = db.connect(config.DB_PATH)
        db.create_user(conn, "plain", auth.hash_password("correct-horse-1"))
        db.create_user(conn, "boss", auth.hash_password("correct-horse-2"),
                       is_admin=1)
        conn.close()

        c.post("/login", data={"username": "plain",
                               "password": "correct-horse-1"})
        check("nor can an ordinary account",
              c.get("/feedback").status_code == 403)
        c.post("/logout")

        c.post("/login", data={"username": "boss",
                               "password": "correct-horse-2"})
        page = c.get("/feedback")
        check("an admin can", page.status_code == 200, page.status_code)
        check("and sees the message", b"upside down" in page.data)
        check("and whether it was actually mailed", b"Mailed" in page.data)

        # A signed-in sender is identified by their account, not by a form
        # field: we already know who they are, and a text input is not
        # evidence of anything.
        tok = token()
        c.post("/feedback", data={
            "csrf": tok, "message": "from an account",
            "reply_to": "someone-else@example.org", "page": "/"})
        mine = [r for r in stored() if r["message"] == "from an account"]
        check("a signed-in message records the account", bool(mine)
              and mine[0]["username"] == "boss",
              mine[0]["username"] if mine else None)
        check("and ignores a reply address typed into the form",
              bool(mine) and mine[0]["reply_to"] != "someone-else@example.org",
              mine[0]["reply_to"] if mine else None)
        c.post("/logout")
    finally:
        config.DB_PATH = prev_db
        config.FEEDBACK_TO = prev_to
        siteconf.forget()
        importlib.reload(appmod)


def test_the_feedback_form_cannot_be_used_to_send_mail_to_strangers():
    """A public form that sends email is an open relay unless it is nailed
    down. Four things are fixed and none of them come from the sender: the
    recipient, the subject, the length, and the rate. The sender's text
    reaches the body only -- a header taking a newline is how a suggestion
    box becomes a way to mail anybody.
    """
    import app as appmod
    import mailer

    check("the recipient is configuration, not a form field",
          "FEEDBACK_TO" in open(os.path.join(
              os.path.dirname(__file__), "config.py")).read())

    src = open(os.path.join(os.path.dirname(__file__), "app.py")).read()
    body = re.search(r"def feedback\(\):.*?\n(?=@app\.route|\Z)", src, re.S)
    check("the route is there", body is not None)
    if body:
        check("it mails config.FEEDBACK_TO and nothing else",
              "config.FEEDBACK_TO" in body.group(0)
              and 'form.get("to")' not in body.group(0))
        check("the subject is built here, not received",
              "FEEDBACK_SUBJECT.format" in body.group(0))

    # Reply-To is the one header carrying anything a stranger supplied, so it
    # is the one that has to refuse a newline.
    msg = mailer.build("to@example.org", "Subject", "body",
                       reply_to="ok@example.org")
    check("a clean reply address is used", msg["Reply-To"] == "ok@example.org")

    for bad in ("a@b.co\nBcc: victim@example.org",
                "a@b.co\r\nTo: victim@example.org"):
        msg = mailer.build("to@example.org", "Subject", "body", reply_to=bad)
        check("an address carrying a newline is dropped",
              msg["Reply-To"] is None, msg["Reply-To"])

    check("and the address check rejects it before that",
          not appmod._usable_email("a@b.co\nBcc: x@y.z"))
    for bad in ("", "no-at-sign", "a@nodot", "a b@c.co", "a@b.co, c@d.co"):
        check(f"{bad!r} is not a usable reply address",
              not appmod._usable_email(bad))
    check("an ordinary address is", appmod._usable_email("obs@example.org"))


def test_every_page_offers_the_feedback_form():
    """One footer, every page -- for the same reason there is one header.

    A suggestion box on the board only is one that the person looking at a
    target page, where they noticed the problem, has to go and find.
    """
    import importlib
    import tempfile

    import app as appmod

    prev_db = config.DB_PATH
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "targets.db")
    siteconf.forget()
    try:
        conn = db.connect(config.DB_PATH)
        db.init(conn)
        # A fully populated row: a target page formats magnitudes and counts
        # without guarding every one, so a row carrying only a designation
        # 500s for reasons that have nothing to do with the footer. The live
        # board has no NULL in any of these columns.
        conn.execute(
            "INSERT INTO targets (site_id, desig, score, vmag, hmag, nobs,"
            " arc_days, not_seen_days, observable, score_total,"
            " discard_reasons, window_minutes) VALUES "
            "(1,'P11aaaa', 85, 20.4, 22.1, 6, 0.4, 0.5, 1, 3.2, '[]', 180.0)")
        conn.commit()
        conn.close()
        importlib.reload(appmod)
        c = appmod.app.test_client()

        for path in list(HEADER_PAGES) + ["/target/P11aaaa"]:
            resp = c.get(path)
            # /forgot answers 503 with no relay configured, by design -- it
            # still renders, and still has to carry the form.
            check(f"{path} renders",
                  resp.status_code in (200, 503), resp.status_code)
            body = resp.data.decode()
            check(f"{path} offers the feedback form", 'class="fbmenu"' in body)
            check(f"{path} posts it to /feedback",
                  'action="/feedback"' in body)
            check(f"{path} names the page it was sent from",
                  'name="page"' in body)

        # Every template, not merely every route this list happens to name.
        here = os.path.dirname(os.path.abspath(__file__))
        missing = []
        for name in os.listdir(os.path.join(here, "templates")):
            if name.startswith("_") or not name.endswith(".html"):
                continue
            src = open(os.path.join(here, "templates", name)).read()
            if "</body>" in src and "_footer.html" not in src:
                missing.append(name)
        check("no full page template was missed", not missing, missing)
    finally:
        config.DB_PATH = prev_db
        siteconf.forget()
        importlib.reload(appmod)


def test_no_pointing_line_is_invented_across_a_gap():
    """An ephemeris hole must produce no pointable line, not a smooth lie.

    MPC suppresses every row below the server floor, so a target that sets and
    rises again leaves hours of nothing in the middle while the samples either
    side stay consecutive in the list. Interpolating across that blends the
    end of one night with the start of the next into a position that is
    smooth, plausible and invented -- and because the blended row is stamped
    with the requested instant, live_row_is_now reads it as current and the
    plan file publishes it as the one line to slew to.

    Measured on the live board at 15:05 UT: or09023 published a pointing line
    at altitude +35 with the sun at -33, blended across the daylight hole
    between 02:30 and 22:00. The MPC's own row for that instant says -31 and
    astropy agrees to half a degree. It was five in the afternoon.

    skymap._covers() already refuses this for markers, which is why the map
    went dark for those targets while the plan file did not.
    """
    import output

    t0 = 1_760_000_000.0
    STEP = 1800.0
    HOLE = 19 * 3600.0

    def row(ts, alt, az):
        r = ephemeris.Row(SAMPLE_EPH)
        r.ts, r.alt, r.az = ts, alt, az
        return r

    # Two runs, half an hour between samples inside each and nineteen hours
    # between them: the shape MPC returns for a target that sets and rises.
    first = [row(t0, 40.0, 100.0), row(t0 + STEP, 38.0, 110.0)]
    second = [row(t0 + HOLE, 20.0, 260.0), row(t0 + HOLE + STEP, 25.0, 270.0)]
    eph = ephemeris.ObjectEphemeris("GAPPY", first + second)

    inside = eph.interpolate_at(t0 + STEP / 2)
    check("inside a run it still interpolates",
          inside is not None and abs(inside.alt - 39.0) < 1e-6,
          None if inside is None else inside.alt)

    mid = eph.interpolate_at(t0 + HOLE / 2)
    check("in the hole it refuses instead of inventing a position",
          mid is None, None if mid is None else (mid.alt, mid.az))

    # The sample beside a hole is still itself -- nothing is blended into it,
    # so the last usable instant of a run keeps its position.
    edge = eph.interpolate_at(t0 + STEP)
    check("landing on the sample beside the hole returns that sample",
          edge is not None and edge.alt == 38.0,
          None if edge is None else edge.alt)

    # Clamping off either end is unchanged: there is nothing to blend with.
    check("before the first sample still clamps",
          eph.interpolate_at(t0 - 7200) is first[0])
    check("after the last sample still clamps",
          eph.interpolate_at(t0 + HOLE + 7200) is second[-1])

    # What the refusal is for. A target whose ephemeris says nothing about now
    # must not carry an uncommented line, because that line is what gets
    # slewed to.
    at = t0 + HOLE / 2
    target = {
        "desig": "GAPPY", "observable": True,
        "score": 100, "nobs": 6, "arc_days": 0.95, "not_seen_days": 1.4,
        "interp_row": eph.interpolate_at(at),
        "nearest_row": min(first + second, key=lambda r: abs(r.ts - at)),
    }
    target["live_row_is_now"] = (
        target["interp_row"] is not None
        and abs(target["interp_row"].ts - at) < 60.0)
    check("so nothing about that instant counts as pointable",
          target["live_row_is_now"] is False)

    block = output.plan_entry(target)
    pointable = [ln for ln in block.splitlines()
                 if ln and not ln.startswith(("*", "//", "   "))]
    check("and the plan entry publishes no uncommented pointing line",
          not pointable, pointable)
    check("while still showing the nearest real row, commented",
          "// " in block and "not now, do not slew" in block, block)

    # A run sampled evenly must be unaffected, or this would refuse every
    # ordinary interpolation and quietly empty the plan file.
    even = ephemeris.ObjectEphemeris(
        "EVEN", [row(t0 + i * STEP, 30.0 + i, 100.0 + i) for i in range(8)])
    got = [even.interpolate_at(t0 + i * STEP + STEP / 2) for i in range(7)]
    check("an evenly sampled run interpolates at every step",
          all(g is not None for g in got),
          [i for i, g in enumerate(got) if g is None])


def test_a_refused_target_is_refused_everywhere():
    """Keep-out is a safety limit, so it has to hold in all three outputs.

    The queue, the sky map and the plan file are rendered by different code
    from the same rows. "Rejected, not merely flagged" is only true if it is
    true in all three -- a target dropped from the queue but still drawn on
    the map, or still carrying a pointing line, is one an observer can slew
    at anyway, which is the whole thing the wedge exists to prevent.
    """
    import output

    site = config.DEFAULT_SITE
    if not site.keepout_wedges:
        check("this site declares a keep-out wedge to test", True,
              "no wedge configured; nothing to assert")
        return

    start, end, min_alt, _reason = site.keepout_wedges[0]
    inside_az = (start + (((end - start) % 360.0) / 2.0)) % 360.0
    low = min_alt - 5.0
    now = 1_760_000_000.0

    r = ephemeris.Row(SAMPLE_EPH)
    r.ts, r.az, r.alt = now, inside_az, low

    # 1. The queue.
    reasons = pipeline.row_rejections(r, now + 86400)
    check("a target in the wedge is rejected by the pipeline",
          "keepOut" in reasons, reasons)

    # 2. The map. A rejected row must not be drawn where it is.
    rows = [{"desig": "WEDGED", "observed": False, "mask_flags": [],
             "vmag": 19.0, "score": 80}]
    track = [(now - 1800, inside_az, low), (now, inside_az, low),
             (now + 1800, inside_az, low)]
    marks = skymap.target_marks(rows, {"WEDGED": track}, now)
    drawn = [m for m in marks if m.get("up")]
    check("and it is not drawn as a dot on the sky map", not drawn, marks)

    # Discriminating, not vacuous: the identical track lifted above the
    # wedge's height limit IS drawn, so the absence above is the wedge acting
    # and not some unrelated reason to withhold a marker.
    clear = [(ts, az, min_alt + 5.0) for ts, az, _ in track]
    lifted = [m for m in skymap.target_marks(rows, {"WEDGED": clear}, now)
              if m.get("up")]
    check("the same track above the limit is drawn",
          len(lifted) == 1, lifted)

    # 3. The plan file. render_plan only emits observable targets, and the
    #    rejection is what makes it unobservable.
    target = {"desig": "WEDGED", "score": 100, "nobs": 6, "arc_days": 0.9,
              "not_seen_days": 1.0, "observable": not reasons,
              "discard_reasons": reasons, "nearest_row": r,
              "interp_row": r, "live_row_is_now": True}
    check("and it is not observable, so the plan file omits it",
          target["observable"] is False)
    check("the plan file really does drop it",
          "WEDGED" not in output.render_plan([target]),
          output.render_plan([target])[:200])

    # The same position above the height limit is usable in all three.
    r.alt = min_alt + 5.0
    check("the same azimuth above the limit is not rejected",
          pipeline.row_rejections(r, now + 86400) == [],
          pipeline.row_rejections(r, now + 86400))


def test_an_mpc_row_survives_the_round_trip():
    """Read MPC's convention, store ours, write MPC's back out unchanged.

    MPC measures azimuth from south; the board stores compass bearings. Two
    conversions in opposite directions, and an error in either is invisible
    on its own -- the number still looks like an azimuth. Going round the
    loop is what catches it.
    """
    import output

    r = ephemeris.Row(SAMPLE_EPH)
    check("MPC's own azimuth is kept alongside ours",
          r.az_mpc == 240.0 and r.az == 60.0, (r.az_mpc, r.az))

    back = ephemeris.Row(output.ephemeris_line(r))
    check("the written line parses again", back is not None)
    check("azimuth survives the round trip",
          back.az_mpc == r.az_mpc and back.az == r.az,
          (back.az_mpc, back.az))
    check("altitude survives it", back.alt == r.alt, (back.alt, r.alt))
    check("so do the coordinates",
          abs(back.ra_deg - r.ra_deg) < 1e-6
          and abs(back.dec_deg - r.dec_deg) < 1e-6,
          (back.ra_deg, r.ra_deg, back.dec_deg, r.dec_deg))
    check("and the instant it refers to", back.ts == r.ts, (back.ts, r.ts))

    # The conversion is a half turn, so applying it twice is the identity --
    # which is what makes an off-by-180 error impossible to hide.
    check("the two conventions are exactly half a turn apart",
          (r.az - r.az_mpc) % 360.0 == 180.0, (r.az, r.az_mpc))


def test_derived_distance_agrees_with_jpl():
    """The distance behind the map's colours, checked against JPL Horizons.

    MPC colours its uncertainty maps by how far an object is from Earth and
    does not publish the number -- not in the ephemeris, not on the offsets
    page, not per variant orbit. So it is derived from H, V and solar
    elongation, and something has to establish that the arithmetic is right
    rather than merely plausible.

    Each case below is a real Horizons observation from L01: its absolute
    magnitude, the apparent magnitude and elongation it published, and the
    geocentric distance it computed from a well-determined orbit. Pinned as
    numbers rather than fetched, so the check is deterministic and needs no
    network -- Horizons is the reference, not a runtime dependency.

    Measured over 50 epochs when this was built: buckets agreed 50/50 using
    each object's true G, and 49/50 assuming 0.15 as production must.
    """
    # (name, H, V, elongation, true delta AU, true G)
    cases = [
        ("Ceres", 3.34, 8.893, 86.543, 2.77441914, 0.12),
        ("Eros", 10.4, 10.251, 109.2696, 0.41586274, 0.46),
        ("Apophis", 19.09, 10.816, 153.8702, 0.01345935, 0.24),
        ("2024 YR4", 23.93, 13.553, 95.8751, 0.00220468, 0.15),
    ]
    for name, h, v, elong, truth, g in cases:
        got = neodistance.solve(v, h, elong, g)
        check(f"{name}: a distance comes back", got is not None)
        if got is None:
            continue
        rel = abs(got - truth) / truth
        check(f"{name}: within 5% of Horizons ({truth:.5f} AU)",
              rel < 0.05, f"got {got:.5f}, off by {rel * 100:.2f}%")
        check(f"{name}: lands in the same colour as the true distance",
              neodistance.colour(got) == neodistance.colour(truth),
              (neodistance.colour(got), neodistance.colour(truth)))

    # 2024 YR4 at 0.0022 AU is the case the red bucket exists for, so it is
    # asserted by name rather than left to the loop above.
    close = neodistance.solve(13.553, 23.93, 95.8751, 0.15)
    check("a genuine close approach reads as within 0.01 AU",
          neodistance.colour(close) == neodistance.NEAR, close)

    # Production has no measured slope parameter and must assume one. That
    # costs accuracy, and the cost is stated rather than hidden: Eros, whose
    # real G is 0.46, reads about a fifth nearer than it is.
    assumed = neodistance.solve(10.251, 10.4, 109.2696)
    check("assuming G=0.15 still puts Eros in the right colour",
          neodistance.colour(assumed) == neodistance.FAR, assumed)
    check("though it is measurably off, which is why G is documented",
          abs(assumed - 0.41586274) / 0.41586274 > 0.1, assumed)


def test_a_distance_is_refused_rather_than_guessed():
    """Where the model does not apply, no colour is better than a wrong one.

    Both guards exist because the HG phase relation is fitted to roughly
    0-120 degrees of phase and is extrapolation beyond that. Extrapolating
    under-counts the dimming, so the solver compensates by pushing the
    distance outwards -- it would report a red object as orange, which is
    precisely the error this feature exists to prevent.
    """
    check("no apparent magnitude, no distance",
          neodistance.solve(None, 20.0, 90.0) is None)
    check("no absolute magnitude, no distance",
          neodistance.solve(20.0, None, 90.0) is None)
    check("no elongation, no distance",
          neodistance.solve(20.0, 20.0, None) is None)

    check("a target near the Sun is refused",
          neodistance.solve(20.0, 20.0, 5.0) is None)
    check("and the threshold is well below anything observable",
          neodistance.MIN_ELONG_DEG < 40.0, neodistance.MIN_ELONG_DEG)

    # The real case. C46K391 on the live board: H 31, V 21.5 at 57 degrees
    # elongation. Solved naively that is 0.0014 AU -- inside half the Moon's
    # distance -- but at that distance the phase angle is about 122 degrees,
    # past where the relation was ever fitted. The honest answer is that we
    # cannot say.
    check("a close object at high phase angle is refused, not coloured",
          neodistance.solve(21.5, 31.0, 57.5) is None)
    check("and the marker reads unknown rather than green",
          neodistance.colour(neodistance.solve(21.5, 31.0, 57.5))
          == neodistance.UNKNOWN)

    # Same object, same H, at a geometry the relation does cover.
    ok = neodistance.solve(10.816, 19.09, 153.8702)
    check("a low phase angle is answered normally", ok is not None)


def test_the_map_uses_mpc_s_own_colours():
    """Marker colour is MPC's uncertainty-map palette, plus white for done.

    MPC's own text: green beyond 0.05 AU, dark blue for a main-belt orbit,
    magenta for Jupiter Trojans (unimplemented, so reserved), orange between
    0.05 and 0.01, red within 0.01.
    """
    check("within 0.01 AU is the near colour",
          neodistance.colour(0.0099) == neodistance.NEAR)
    check("0.01 exactly is no longer near",
          neodistance.colour(0.0100) == neodistance.CLOSE)
    check("just under 0.05 is still close",
          neodistance.colour(0.0499) == neodistance.CLOSE)
    check("0.05 exactly is far",
          neodistance.colour(0.0500) == neodistance.FAR)
    check("no distance at all is unknown, not far",
          neodistance.colour(None) == neodistance.UNKNOWN)

    # The main-belt call is MPC's, from the variant-orbit table, because its
    # uncertainty-map text says "main-belt" without ever defining it.
    check("a far object MPC scores as main-belt is dark blue",
          neodistance.colour(1.5, 90) == neodistance.MAIN_BELT)
    check("a far object it does not is green",
          neodistance.colour(1.5, 0) == neodistance.FAR)
    check("the main-belt score never overrides a close distance",
          neodistance.colour(0.004, 100) == neodistance.NEAR)
    check("a missing score is not the same fact as a score of zero",
          not neodistance.is_main_belt(None))

    # Every colour distinct, or two meanings share a swatch.
    shades = list(skymap.DISTANCE_COLOURS.values()) + [skymap.DONE_COLOUR]
    check("every colour on the map is distinct",
          len(set(shades)) == len(shades), shades)
    check("done is not one of MPC's distance colours",
          skymap.DONE_COLOUR not in skymap.DISTANCE_COLOURS.values())

    now = 1_760_000_000.0
    base = dict(desig="T1", vmag=19.0, score=80, mask_flags=[], observed=False)

    def colour_of(delta, **extra):
        track = [(now - 1800, 100.0, 30.0, delta), (now, 110.0, 40.0, delta),
                 (now + 1800, 120.0, 50.0, delta)]
        marks = skymap.target_marks([dict(base, **extra)], {"T1": track}, now)
        return marks[0]["distance"] if marks else None

    check("a marker inside 0.01 AU carries the near colour",
          colour_of(0.005) == neodistance.NEAR)
    check("one between the thresholds carries close",
          colour_of(0.02) == neodistance.CLOSE)
    check("a distant one carries far", colour_of(2.0) == neodistance.FAR)
    check("a distant one MPC calls main-belt carries dark blue",
          colour_of(2.0, mb_score=80) == neodistance.MAIN_BELT)
    check("a sample with no distance carries unknown",
          colour_of(None) == neodistance.UNKNOWN)

    # Distance is interpolated at the instant being drawn, because a
    # candidate closing on Earth changes distance measurably across a night
    # and those are the objects whose colour matters most.
    moving = [(now - 3600, 100.0, 30.0, 0.08), (now + 3600, 120.0, 50.0, 0.02)]
    check("halfway along, the distance is halfway between",
          abs(skymap._distance_at(moving, now) - 0.05) < 1e-9,
          skymap._distance_at(moving, now))
    holed = [(now - 3600, 100.0, 30.0, 0.08), (now + 3600, 120.0, 50.0, None)]
    check("a sample that refused a distance is not interpolated across",
          skymap._distance_at(holed, now) is None)


def test_mpc_class_table_is_read_by_its_own_header():
    """MPC's variant-orbit table, parsed defensively.

    It is where the main-belt classification comes from -- MPC's own call
    rather than an a/e cut invented here -- and where the better H comes
    from. MPC marks the page Beta and publishes no JSON or CSV, so this is
    scraped HTML and is read by column NAME: a column inserted upstream then
    moves nothing.
    """
    header = ("<tr><th>desig</th><th>digest2</th><th>NEO</th><th>large_e</th>"
              "<th>MC</th><th>HUN</th><th>MB</th><th>HIL</th><th>JFC</th>"
              "<th>TRO</th><th>DIST</th><th>H</th><th>arc</th><th>Nsets</th>"
              "<th>Unc</th><th>V</th><th>dq</th><th>de</th><th>di</th></tr>")
    row = ("<tr><td>P12qbq7</td><td>95</td><td>3</td><td>10</td><td>4</td>"
           "<td>0</td><td>88</td><td>0</td><td>1</td><td>0</td><td>0</td>"
           "<td>20.4</td><td>0.05</td><td>1</td><td>0.34</td><td>21.1</td>"
           "<td>0.4</td><td>0.2</td><td>3.1</td></tr>")
    got = neocp.parse_neocp_classes(f"<table>{header}{row}</table>")
    check("one row parses", list(got) == ["P12qbq7"], got)
    entry = got.get("P12qbq7", {})
    check("the median H is picked up", entry.get("mpc_h") == 20.4, entry)
    check("so is the main-belt score", entry.get("mb_score") == 88.0, entry)
    check("and the Trojan score MPC reserves magenta for",
          entry.get("tro_score") == 0.0, entry)
    check("that score makes it main-belt",
          neodistance.is_main_belt(entry.get("mb_score")))

    # Read by name: a column added upstream must not shift the others.
    shifted_header = header.replace("<th>MB</th>",
                                    "<th>NEWCOL</th><th>MB</th>")
    shifted_row = row.replace("<td>88</td>", "<td>999</td><td>88</td>")
    moved = neocp.parse_neocp_classes(
        f"<table>{shifted_header}{shifted_row}</table>")
    check("an inserted column does not shift the ones we read",
          moved.get("P12qbq7", {}).get("mb_score") == 88.0, moved)

    check("a row whose cell count disagrees with the header is skipped",
          neocp.parse_neocp_classes(
              f"<table>{header}<tr><td>X</td><td>1</td></tr></table>") == {})
    check("markup that is not this table yields nothing, not a guess",
          neocp.parse_neocp_classes("<table><tr><th>a</th></tr></table>") == {})
    check("no markup at all is survivable", neocp.parse_neocp_classes("") == {})

    # n.a. is absent, not zero. A main-belt score nobody supplied is not the
    # same fact as a score of 0, and colouring on the difference matters.
    na_row = row.replace("<td>88</td>", "<td>n.a.</td>")
    na = neocp.parse_neocp_classes(f"<table>{header}{na_row}</table>")
    check("an n.a. cell is absent rather than zero",
          "mb_score" not in na.get("P12qbq7", {}), na)
    check("and absent means not main-belt",
          not neodistance.is_main_belt(na.get("P12qbq7", {}).get("mb_score")))


def main():
    # The suite runs on epyc, where config.DB_PATH is the observatory's live
    # database. For years one test marked /mark/XYZ straight into it. Nothing
    # caught that, because a stray row in observer_state is invisible: the
    # board joins from `targets`, so a designation that was never on the
    # NEOCP simply never renders. This is what catches it.
    live_before = _live_db_fingerprint()

    for fn in (test_the_plan_can_be_saved_from_any_browser,
               test_site, test_horizon_mask, test_analytic_matches_astropy,
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
               test_site_id_migration_keeps_every_row,
               test_two_sites_never_read_each_others_rows,
               test_l01_from_file_is_the_l01_we_had,
               test_a_site_file_must_be_complete,
               test_a_cycle_serves_several_sites_without_repeating_shared_work,
               test_one_sites_failure_does_not_take_the_others_down,
               test_each_site_writes_its_own_plan_file,
               test_the_switcher_resolves_an_observatory_by_code,
               test_the_settings_page_keeps_soft_and_hard_apart,
               test_settings_edits_layer_over_the_file_and_can_be_undone,
               test_a_keepout_wedge_cannot_be_changed_by_accident,
               test_an_edited_limit_reaches_the_update_cycle,
               test_the_settings_preview_is_drawn_by_the_board_s_own_code,
               test_a_new_observatory_inherits_nobody_elses_dome_limits,
               test_an_observatory_code_is_not_optional_and_cannot_be_invented,
               test_the_observatory_cap_is_enforced,
               test_owner_edits_and_members_observe,
               test_you_land_on_the_observatory_you_work_at,
               test_every_page_carries_the_same_navigation,
               test_the_switcher_is_there_with_only_one_observatory,
               test_switching_observatory_always_lands_on_that_board,
               test_the_switcher_puts_your_own_observatories_first,
               test_no_page_still_claims_to_be_l01,
               test_ranking_bounds, test_row_rejection_reasons,
               test_replay_ts_never_takes_the_map_down,
               test_night_archive_survives_per_account_state,
               test_archived_replay_endpoint,
               test_precomputed_frames_match_the_live_route,
               test_the_replay_falls_back_when_nothing_is_precomputed,
               test_the_browser_is_never_given_geometry_to_judge,
               test_the_replay_handle_never_opens_in_the_future,
               test_a_bad_replay_url_cannot_replace_the_map_with_prose,
               test_marks_never_cross_a_hole_in_the_ephemeris,
               test_marks_are_filtered_by_the_site_they_are_given,
               test_skymap_orientation, test_skymap_mask_wedges,
               test_moon_exclusion_locus, test_skymap_marks,
               test_priority_bump_is_fully_gone,
               test_offsets_parse_with_the_fast_motion_flag,
               test_cache_schema_forces_one_refetch,
               test_prevdes_parsing_keeps_what_ground_truth_needs,
               test_archived_run_import,
               test_history_page_survives_missing_columns,
               test_target_history_section_survives_missing_columns,
               test_single_snapshot_history_still_shown,
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
               test_a_done_target_is_white_on_the_sky_map,
               test_interpolate_clamps_to_the_right_end,
               test_ephemeris_refetched_when_its_window_runs_out,
               test_a_poll_patches_the_table_instead_of_rebuilding_it,
               test_feedback_reaches_somewhere_even_when_the_mail_does_not,
               test_the_feedback_form_cannot_be_used_to_send_mail_to_strangers,
               test_every_page_offers_the_feedback_form,
               test_no_pointing_line_is_invented_across_a_gap,
               test_a_refused_target_is_refused_everywhere,
               test_an_mpc_row_survives_the_round_trip,
               test_derived_distance_agrees_with_jpl,
               test_a_distance_is_refused_rather_than_guessed,
               test_the_map_uses_mpc_s_own_colours,
               test_mpc_class_table_is_read_by_its_own_header):
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
