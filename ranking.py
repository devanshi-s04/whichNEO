"""Ordering and the intrinsic score.

The default order is chronological by the time each target reaches its best
observable altitude, matching the observatory's own planner: the list is a
working sequence for the night, not a league table. Work down it and each
target is near its peak when you reach it.

The intrinsic score is kept as a separate, sortable quantity. It answers a
different question -- which target is most worth having at all -- and stays
stable through the day because it deliberately excludes altitude.
"""

import config


def _clip01(x):
    return max(0.0, min(1.0, x))


def score_components(row):
    """Each component normalised to 0..1, higher meaning better target."""
    digest2 = _clip01(row["score"] / 100.0)

    # A shorter arc means a less constrained orbit, so follow-up is worth more.
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
            config.RANK_WEIGHTS[k] * v for k, v in comps.items())
    return rows


def max_possible_score():
    return sum(config.RANK_WEIGHTS.values())


# Targets with no observable window sort last; among those, keep a stable
# order so the table does not shuffle between refreshes.
_FAR_FUTURE = float("inf")


def sort_key_chronological(row):
    ts = row.get("max_alt_ts")
    return (0 if row.get("observable") else 1,
            ts if ts is not None else _FAR_FUTURE,
            row["desig"])


def sort_key_score(row):
    bump = row.get("priority_bump") or 0.0
    return (0 if row.get("observable") else 1,
            -((row.get("score_total") or 0.0) + bump),
            row["desig"])


# Every other numeric column shown in the table, sortable directly: URL sort
# key -> the row field it reads. "score" is already taken by sort_key_score
# (the composite Value column), so the raw digest2 column is keyed
# "digest2" here to avoid colliding with it.
SORTABLE_COLUMNS = {
    "digest2": "score",
    "vmag": "vmag",
    "exposure_min": "exposure_min",
    "motion": "cur_motion",
    "alt": "cur_alt",
    "az": "cur_az",
    "moon": "cur_moon_dist",
    "unseen": "not_seen_days",
    "q": "q",
}


def sort_key_column(field, ascending=True):
    """Sort by any single numeric field, observable targets first (as with
    the two curated modes above), missing values sent to the end of their
    group regardless of direction, ties broken by designation."""
    def key(row):
        v = row.get(field)
        ordered = 0.0 if v is None else (v if ascending else -v)
        return (0 if row.get("observable") else 1, v is None, ordered, row["desig"])
    return key


def sort_targets(rows, mode=None, ascending=True):
    mode = mode or config.DEFAULT_SORT
    if mode == "score":
        key = sort_key_score
    elif mode in SORTABLE_COLUMNS:
        key = sort_key_column(SORTABLE_COLUMNS[mode], ascending)
    else:
        key = sort_key_chronological
    return sorted(rows, key=key)
