"""How far away a candidate is, and what colour that makes it.

MPC colours its uncertainty maps by the object's distance from Earth, and
says so in NEOCPNotes.html:

    green, for objects that are more then 0.05 AU from the earth at the time
    used for the uncertainty map; Further classifications are made as
    follows: dark blue, for orbits that are "main-belt"; magenta, for orbits
    that are Jupiter Trojans [this is not currently implemented, at the
    present time such orbits are colored dark blue]; orange (supposedly) for
    objects between 0.05 and 0.01 AU from the earth; red, for objects within
    0.01 AU.

MPC does not publish that distance. It is not in the confirmation ephemeris
(we already ask for "Full output" and there is no distance column, nor an
option for one), not on the offsets page, and not per variant orbit. It is
computed server-side and only ever rendered into the map image.

So it is derived here, from three numbers MPC does give us:

    r^2 = D^2 + R^2 - 2*R*D*cos(elongation)      R = Earth-Sun distance
    V   = H + 5*log10(r*D) + Phi(phase angle)    the HG phase relation

Two equations, two unknowns, solved by bisection on D.

**Validated against JPL Horizons**, for Ceres, Eros, Apophis and 2024 YR4
over 50 epochs spanning 0.002 to 3.5 AU, observed from L01:

    with each object's true G     50/50 buckets agreed, median error < 0.25%
    with G assumed to be 0.15     49/50 agreed; Eros, whose real G is 0.46,
                                  reads about 19% near
    deepest case                  2024 YR4 at a true 0.0022 AU, recovered
                                  to within 1.6%

Two limits are worth stating plainly, because a colour implies a confidence
this number does not have.

**Phase angle.** Below about 30 degrees elongation the object is a back-lit
crescent, the HG relation stops describing it, and the error runs to
thousands of percent. That regime is refused rather than guessed at -- see
MIN_ELONG_DEG. It is also unreachable in practice: over 2126 cached
ephemeris rows on one night, the lowest elongation of any row the board would
plot was 41 degrees. You cannot observe something 15 degrees from the Sun in
a dark sky.

**H.** This is the real error, and it is an input error rather than a flaw in
the arithmetic. MPC's own two estimates of H -- the value in neocp.txt and
the median over variant orbits -- differ by 0.46 magnitudes on average, which
is +-24% in distance. Measured against one night's board, 6 of 110 objects
would change colour if H were wrong by that much, all of them straddling the
0.05 AU line and none near 0.01. Prefer the variant-orbit median where it is
available: it is the better estimate and it is refreshed hourly.
"""

import math

# MPC's thresholds, from the passage quoted above.
NEAR_AU = 0.01
CLOSE_AU = 0.05

# Sun-Earth distance. A circular orbit is a tenth of a percent wrong at the
# extremes, which is far inside the uncertainty H contributes.
EARTH_SUN_AU = 1.0

# NEOCP objects have no measured slope parameter, so the conventional default
# is assumed. Validation says this is worth up to ~19% for an object whose
# real G is far from it, which is the same order as the H uncertainty.
DEFAULT_G = 0.15

# Below this the phase function stops describing the object. Set well under
# the lowest elongation a plottable row can have, so it refuses only the
# genuinely undefined and never a target an observer could point at.
MIN_ELONG_DEG = 30.0

# The HG relation is fitted over roughly 0-120 degrees of phase and is
# extrapolation beyond it, so phase_term() clamps there. That clamp is not
# free: it under-counts the dimming, the model then predicts a brighter
# object than reality, and solve() compensates by pushing the distance OUT.
# A close object is the case that triggers it -- when D is much less than
# 1 AU the phase angle is about 180 minus the elongation, so anything nearer
# than a few hundredths of an AU at 43 degrees elongation lands at 137.
# Refusing is right where extrapolating would quietly report a red object as
# orange, which is the one error this feature exists to avoid.
MAX_PHASE_DEG = 120.0

# Colour names, kept as the board's own vocabulary rather than hex, so the
# templates decide what "near" looks like and this module never does.
NEAR = "near"          # red,        < 0.01 AU
CLOSE = "close"        # orange,     0.01 - 0.05 AU
MAIN_BELT = "mainbelt"  # dark blue, >= 0.05 AU and a main-belt orbit
FAR = "far"            # green,      >= 0.05 AU otherwise
UNKNOWN = "unknown"    # no usable H, or geometry we refuse to guess at


def phase_term(alpha_deg, g=DEFAULT_G):
    """Magnitudes the HG phase relation adds at this phase angle."""
    a = math.radians(max(0.0, min(alpha_deg, 120.0)))
    t = math.tan(a / 2.0)
    phi1 = math.exp(-3.33 * t ** 0.63)
    phi2 = math.exp(-1.87 * t ** 1.22)
    value = (1.0 - g) * phi1 + g * phi2
    return -2.5 * math.log10(value) if value > 0 else 0.0


def geometry(delta_au, elong_deg):
    """(heliocentric distance, phase angle) for a candidate geocentric one."""
    e = math.radians(elong_deg)
    r = math.sqrt(max(delta_au ** 2 + EARTH_SUN_AU ** 2
                      - 2.0 * EARTH_SUN_AU * delta_au * math.cos(e), 1e-12))
    denom = 2.0 * r * delta_au
    cos_alpha = ((r ** 2 + delta_au ** 2 - EARTH_SUN_AU ** 2) / denom
                 if denom > 0 else 1.0)
    return r, math.degrees(math.acos(max(-1.0, min(1.0, cos_alpha))))


def apparent_v(delta_au, elong_deg, h, g=DEFAULT_G):
    r, alpha = geometry(delta_au, elong_deg)
    return h + 5.0 * math.log10(r * delta_au) + phase_term(alpha, g)


def solve(v, h, elong_deg, g=DEFAULT_G):
    """Geocentric distance in AU, or None when it cannot be said.

    Bisects on D. V rises monotonically with D over the range that matters,
    so the bracket is checked rather than assumed -- a V brighter than the
    object could be at any distance means H is wrong, not that D is tiny.
    """
    if v is None or h is None or elong_deg is None:
        return None
    if elong_deg < MIN_ELONG_DEG:
        return None
    lo, hi = 1e-5, 60.0
    if apparent_v(lo, elong_deg, h, g) > v:
        return None
    if apparent_v(hi, elong_deg, h, g) < v:
        return None
    # Geometric bisection: D spans six orders of magnitude and the answer is
    # wanted to a fixed fraction, not a fixed number of AU.
    for _ in range(120):
        mid = math.sqrt(lo * hi)
        if apparent_v(mid, elong_deg, h, g) < v:
            lo = mid
        else:
            hi = mid
    answer = math.sqrt(lo * hi)
    # Checked at the answer, not at the input: the phase angle depends on the
    # distance being solved for, so whether the model was extrapolated is
    # only knowable once there is a distance. See MAX_PHASE_DEG.
    _r, alpha = geometry(answer, elong_deg)
    if alpha > MAX_PHASE_DEG:
        return None
    return answer


def is_main_belt(mb_score, tro_score=None):
    """MPC's own call, not ours.

    Its uncertainty-map text says "main-belt" without defining it, and the
    conventional a/e cut would be our definition standing in for theirs. The
    hourly variant-orbit table at
    minorplanetcenter.net/mpcops/neocp/neocp_plots/info/ publishes a per
    object main-belt population score instead, so that is what is used.

    It is a population score from variant orbits, which is not stated to be
    the same rule the map applies when it picks dark blue -- closer to
    authoritative than inventing a cut, and not provably identical to it.
    """
    if mb_score is None:
        return False
    try:
        return float(mb_score) >= 50.0
    except (TypeError, ValueError):
        return False


def colour(delta_au, mb_score=None):
    """Which of MPC's colours this distance earns."""
    if delta_au is None:
        return UNKNOWN
    if delta_au < NEAR_AU:
        return NEAR
    if delta_au < CLOSE_AU:
        return CLOSE
    return MAIN_BELT if is_main_belt(mb_score) else FAR
