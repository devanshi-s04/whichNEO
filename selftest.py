"""Verification checks for the astronomy and parsing.

The important one is test_analytic_matches_astropy: the 24h window scan uses a
fast analytic alt/az path while instantaneous values use astropy's full
transform. If those two ever disagree, observing windows are wrong in a way
that would not otherwise be visible.

Run: python3 selftest.py
"""

import sys

import numpy as np
import astropy.units as u
from astropy.time import Time
from astropy.coordinates import SkyCoord, AltAz, TETE, get_body

import config
import neocp
import observability
import output
import ranking

FAILURES = []


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


def test_coordinate_formatting():
    h, m, s = output._ra_hms(328.2945)
    check("RA 328.2945 deg -> 21h 53m 10.7s",
          (h, m) == (21, 53) and abs(s - 10.68) < 0.01, f"got {h} {m} {s:.2f}")
    d, dm, ds, _ = output._dec_dms(-14.6396)
    check("Dec -14.6396 -> -14 38 22",
          (d, dm) == (-14, 38) and abs(ds - 22.56) < 0.01, f"got {d} {dm} {ds:.2f}")


def test_horizon_mask():
    cases = [(0, np.inf), (10, np.inf), (350, np.inf),
             (45, 20.0), (90, 20.0), (180, 20.0),
             (225, 30.0), (270, 40.0), (315, 40.0)]
    for az, expected in cases:
        got = float(observability.min_altitude_for_azimuth(az))
        ok = (np.isinf(got) and np.isinf(expected)) or got == expected
        check(f"mask az={az:3.0f} -> {expected}", ok, f"got {got}")


def test_analytic_matches_astropy():
    """Cross-validate the fast grid path against the full astropy transform."""
    loc = observability.site()
    t = Time("2026-09-08 22:48:00")
    lat = loc.lat.deg
    lst = t.sidereal_time("apparent", longitude=loc.lon).deg

    rng = np.random.default_rng(0)
    ras = rng.uniform(0, 360, 40)
    decs = rng.uniform(-30, 80, 40)

    coords = SkyCoord(ra=ras * u.deg, dec=decs * u.deg)
    aa = coords.transform_to(AltAz(obstime=t, location=loc))

    # Same apparent-place conversion the scan does; without it, precession
    # since J2000 leaves ~0.7 deg of azimuth error.
    app = coords.transform_to(TETE(obstime=t))
    ha = ((lst - app.ra.deg + 180.0) % 360.0) - 180.0
    alt_a, az_a = observability._altaz_from_hour_angle(ha, app.dec.deg, lat)

    d_alt = np.abs(alt_a - aa.alt.deg).max()
    # Compare azimuth only well away from the zenith, where it is ill-defined.
    high = aa.alt.deg < 88
    d_az = np.abs(((az_a - aa.az.deg + 180) % 360) - 180)[high].max()

    check("analytic vs astropy altitude within 0.5 deg", d_alt < 0.5,
          f"max diff {d_alt:.3f} deg")
    check("analytic vs astropy azimuth within 0.5 deg", d_az < 0.5,
          f"max diff {d_az:.3f} deg")


def test_sun_lower_culmination():
    """Sun altitude at anti-transit must equal arcsin(-cos(dec+lat))."""
    loc = observability.site()
    lat = loc.lat.deg
    t = Time("2026-09-08 22:48:32")
    sun = get_body("sun", t, loc)
    alt = sun.transform_to(AltAz(obstime=t, location=loc)).alt.deg

    lst = t.sidereal_time("apparent", longitude=loc.lon).deg
    ha = ((lst - sun.ra.deg + 180) % 360) - 180
    expected, _ = observability._altaz_from_hour_angle(ha, sun.dec.deg, lat)
    check("sun altitude matches spherical trig", abs(alt - expected) < 0.5,
          f"astropy {alt:.2f} vs analytic {expected:.2f}")
    check("sun is below horizon at 22:48 UT on 2026-09-08", alt < -30,
          f"got {alt:.2f}")


def test_parse():
    line = ("TR0006  100 2026 09 04.9  21.8863 -14.6396 17.3 "
            "Added Sept. 8.89 UT              3   0.01 18.5  3.975")
    rows = neocp.parse_neocp(line)
    check("parses one NEOCP line", len(rows) == 1)
    if rows:
        r = rows[0]
        check("designation", r["desig"] == "TR0006", r["desig"])
        check("digest2", r["digest2"] == 100, r["digest2"])
        check("RA converted to degrees", abs(r["ra_deg"] - 328.2945) < 1e-6, r["ra_deg"])
        check("Dec", abs(r["dec_deg"] + 14.6396) < 1e-6, r["dec_deg"])
        check("V magnitude", r["vmag"] == 17.3, r["vmag"])
        check("not seen days", r["not_seen_days"] == 3.975, r["not_seen_days"])
        check("flagged as newly added", r["is_new"] is True)


def test_ranking_bounds():
    rows = [
        dict(digest2=100, arc_days=0.0, vmag=config.MAG_BRIGHT),
        dict(digest2=0, arc_days=99.0, vmag=config.MAX_MAG),
    ]
    ranking.rank(rows)
    check("best possible target scores the maximum",
          abs(rows[0]["score_total"] - ranking.max_possible_score()) < 1e-9,
          rows[0]["score_total"])
    check("worst possible target scores zero", abs(rows[1]["score_total"]) < 1e-9,
          rows[1]["score_total"])
    check("components stay within 0..1",
          all(0 <= rows[i][k] <= 1 for i in (0, 1)
              for k in ("score_digest2", "score_arc", "score_magnitude")))


def test_sort_sinks_unobservable():
    rows = [
        dict(desig="low_but_up", score_total=1.0, observable=True),
        dict(desig="high_but_down", score_total=9.0, observable=False),
    ]
    rows.sort(key=ranking.sort_key)
    check("observable target sorts above a higher-scoring unobservable one",
          rows[0]["desig"] == "low_but_up", rows[0]["desig"])


def main():
    for fn in (test_site, test_coordinate_formatting, test_horizon_mask,
               test_analytic_matches_astropy, test_sun_lower_culmination,
               test_parse, test_ranking_bounds, test_sort_sinks_unobservable):
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
