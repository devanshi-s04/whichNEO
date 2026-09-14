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

import os
import re
import sys
import time

import numpy as np
import astropy.units as u
from astropy.time import Time
from astropy.coordinates import SkyCoord, AltAz, TETE, get_body

import config
import ephemeris
import neocp
import observability
import output
import pipeline
import ranking

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


def test_exposure_plan():
    """Frame length from motion, frame count from the legacy total.

    The count must come out near the ~48 frames observers describe for a
    typical target, without that number being hardcoded anywhere.
    """
    def plan(motion, vmag):
        r = ephemeris.Row(SAMPLE_EPH)
        r.motion, r.vmag = motion, vmag
        return r.exposure_plan()

    b = config.TRAIL_BUDGET_ARCSEC
    t, n, capped = plan(6.0, 20.0)
    check("frame length is the trailing limit",
          abs(t - 60 * b / 6.0) < 0.05, t)
    check("faster motion gives shorter frames", plan(60.0, 20.0)[0] < t)
    check("slower motion gives longer frames", plan(1.0, 20.0)[0] > t)

    # A typical target: median motion and a faint magnitude.
    t_typ, n_typ, cap_typ = plan(3.4, 21.2)
    check("a typical target lands near the ~48 frames observers quote",
          35 <= n_typ <= 60, f"{n_typ} frames of {t_typ}s")
    check("typical target is not capped", not cap_typ)

    # Fast mover: short frames, so the count would explode without a cap.
    t_fast, n_fast, cap_fast = plan(116.8, 18.1)
    check("a fast mover is capped rather than demanding hundreds of frames",
          cap_fast and n_fast == config.MAX_FRAMES, f"{n_fast}, capped={cap_fast}")

    # Slow mover: long frames, so few of them.
    t_slow, n_slow, cap_slow = plan(1.0, 21.3)
    check("a slow mover needs few frames", n_slow < 25, n_slow)
    check("slow mover is not capped", not cap_slow)

    check("frame length respects the floor",
          plan(100000.0, 20.0)[0] == config.MIN_EXPOSURE_S, plan(100000.0, 20.0)[0])
    check("frame length respects the ceiling",
          plan(0.001, 20.0)[0] == config.MAX_EXPOSURE_S, plan(0.001, 20.0)[0])
    check("a motionless target does not divide by zero",
          plan(0.0, 20.0)[0] == config.MAX_EXPOSURE_S)
    check("frame count is never zero", plan(6.0, 12.0)[1] >= 1, plan(6.0, 12.0))

    check("the legacy total is left untouched",
          abs(ephemeris.Row(SAMPLE_EPH).exposure_minutes() -
              max(10 + (16.4 - 18) * 5, config.EXPOSURE_FLOOR_MIN)) < 1e-9)


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

    r5 = ephemeris.Row(SAMPLE_EPH)
    r5.az, r5.alt = 0.0, 80.0              # due north, high
    check("north sector blocked by the dome mask",
          "azBlocked" in pipeline.row_rejections(r5, far_future),
          pipeline.row_rejections(r5, far_future))


def main():
    for fn in (test_site, test_horizon_mask, test_analytic_matches_astropy,
               test_neocp_list_parse, test_neocp_info_column_collision,
               test_ephemeris_row, test_ephemeris_azimuth_convention,
               test_exposure_rule, test_exposure_plan, test_night_bounds,
               test_chronological_sort,
               test_upcoming_cards_look_forward, test_cache_signature_ignores_the_clock,
               test_plan_file_survives_dawn, test_mpc_markers,
               test_observatory_identification, test_uncertainty_plot,
               test_auth_protects_state_changes,
               test_schema_migration_from_older_db,
               test_ranking_bounds, test_row_rejection_reasons):
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
