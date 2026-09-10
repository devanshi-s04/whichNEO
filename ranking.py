"""First-generation intrinsic ranking.

Deliberately excludes altitude and airmass. The board runs 24/7, so a
time-varying score would collapse to near-zero for every target during
Visnjan daylight. Instead this scores intrinsic follow-up value only, and
observability sinks unobservable targets at sort time.

Every component is kept separate so weights can be retuned from config, and
so the website can show observers exactly why a target ranks where it does.
"""

import config


def _clip01(x):
    return max(0.0, min(1.0, x))


def score_components(row):
    """Each component is normalised to 0..1, higher meaning better target."""
    digest2 = _clip01(row["digest2"] / 100.0)

    # Shorter arc means a less constrained orbit, so follow-up is worth more.
    arc = _clip01(1.0 - row["arc_days"] / config.ARC_SATURATE_DAYS)

    span = config.MAX_MAG - config.MAG_BRIGHT
    magnitude = _clip01((config.MAX_MAG - row["vmag"]) / span) if span > 0 else 0.0

    return {"digest2": digest2, "arc": arc, "magnitude": magnitude}


def rank(rows):
    """Attach score components and a weighted total to every row."""
    for r in rows:
        comps = score_components(r)
        r["score_digest2"] = comps["digest2"]
        r["score_arc"] = comps["arc"]
        r["score_magnitude"] = comps["magnitude"]
        r["score_total"] = sum(
            config.RANK_WEIGHTS[k] * v for k, v in comps.items()
        )
    return rows


def sort_key(row):
    """Observable targets first, then by adjusted score.

    priority_bump is the observer's manual override and is applied here rather
    than baked into score_total, so the computed score stays interpretable.
    """
    bump = row.get("priority_bump") or 0.0
    return (0 if row.get("observable") else 1, -(row["score_total"] + bump))


def max_possible_score():
    return sum(config.RANK_WEIGHTS.values())
