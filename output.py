"""Telescope-ready observing block, matching the legacy MPCS output format.

MPCS emits, per target:

    * NAME   20.5    36 x 05 sec
    2022 08 08 2145   00 38 52.8 -07 22 05   100.9  20.5    0.31  007.1

Line 1 is name, magnitude, exposure plan. Line 2 is UT date/time, J2000 RA and
Dec, then a context tail of elongation, magnitude, sky motion and position
angle. Motion and position angle come only from the per-object MPC ephemeris,
so they are blank until an observer opens that target.
"""

import config

# MPCS writes CRLF explicitly so the text pastes correctly on Windows.
NL = "\r\n"


def _ra_hms(ra_deg):
    hours = (ra_deg % 360.0) / 15.0
    h = int(hours)
    minutes = (hours - h) * 60.0
    m = int(minutes)
    s = (minutes - m) * 60.0
    return h, m, s


def _dec_dms(dec_deg):
    sign = -1 if dec_deg < 0 else 1
    a = abs(dec_deg)
    d = int(a)
    minutes = (a - d) * 60.0
    m = int(minutes)
    s = (minutes - m) * 60.0
    return sign * d, m, s, sign


def observing_block(row, when_utc, count=None, exposure=None, motion=None, pa=None):
    """Render one target as an MPCS-style two-line block.

    `when_utc` is an ISO-ish 'YYYY-MM-DD HH:MM' string.
    """
    count = count if count is not None else config.EXPOSURE_COUNT
    exposure = exposure if exposure is not None else config.EXPOSURE_SECONDS

    head = f"* {row['desig']}   {row['vmag']:5.1f}    {count:02d} x {exposure:02d} sec"

    date, _, clock = when_utc.partition(" ")
    year, month, day = date.split("-")
    hh, mm = clock.split(":")[:2]

    rh, rm, rs = _ra_hms(row["ra_deg"])
    dd, dm, ds, _ = _dec_dms(row["dec_deg"])

    tail = f"{row.get('sun_elong_deg', 0.0):5.1f} {row['vmag']:5.1f}"
    if motion is not None and pa is not None:
        tail += f" {motion:7.2f}  {pa:05.1f}"

    body = (
        f"{year} {month} {day} {hh}{mm}   "
        f"{rh:02d} {rm:02d} {rs:04.1f} "
        f"{dd:+03d} {dm:02d} {ds:02.0f}   "
        f"{tail}"
    )
    return head + NL + body


def one_line_command(row, count=None, exposure=None):
    """Short form from the observer notes: `NAME 06 x 30 sec`."""
    count = count if count is not None else config.EXPOSURE_COUNT
    exposure = exposure if exposure is not None else config.EXPOSURE_SECONDS
    return f"{row['desig']} {count:02d} x {exposure:02d} sec"
