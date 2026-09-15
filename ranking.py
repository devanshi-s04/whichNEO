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


# Every column the board can sort by beyond the two curated modes above,
# keyed by the URL sort-mode prefix ("mag", not "vmag", so the URLs already
# shipped as sort=mag_asc/mag_desc keep working). Clicking the column's own
# header activates/reverses it -- there's no separate control -- so all this
# needs is which row field backs it.
SORTABLE_COLUMNS = {
    "mag":          {"field": "vmag"},
    "digest2":      {"field": "score"},
    "exposure_min": {"field": "exposure_min"},
    "motion":       {"field": "cur_motion"},
    "alt":          {"field": "cur_alt"},
    "az":           {"field": "cur_az"},
    "moon":         {"field": "cur_moon_dist"},
    "unseen":       {"field": "not_seen_days"},
    "q":            {"field": "q"},
}


def parse_column_mode(mode):
    """(column_key, ascending) for a mode string like "mag_desc", or None if
    mode names no real column -- the two curated modes, or anything a stray
    URL edit made up. Used both to sort and by the template, so the active
    column header and the actual order can never drift apart."""
    if not mode or mode in ("chronological", "score"):
        return None
    for suffix, ascending in (("_asc", True), ("_desc", False)):
        if mode.endswith(suffix):
            key = mode[: -len(suffix)]
            if key in SORTABLE_COLUMNS:
                return key, ascending
    return None


def sort_key_column(field, ascending=True):
    """Sort by any single numeric field, observable targets first (as with
    the two curated modes above). Missing values sink to the end of their
    group regardless of direction, and ties break by designation so the
    table never shuffles targets that are equal."""
    def key(row):
        v = row.get(field)
        ordered = 0.0 if v is None else (v if ascending else -v)
        return (0 if row.get("observable") else 1, v is None, ordered, row["desig"])
    return key


def sort_targets(rows, mode=None):
    mode = mode or config.DEFAULT_SORT
    if mode == "chronological":
        key = sort_key_chronological
    elif mode == "score":
        key = sort_key_score
    else:
        parsed = parse_column_mode(mode)
        key = (sort_key_column(SORTABLE_COLUMNS[parsed[0]]["field"], parsed[1])
               if parsed else sort_key_chronological)
    return sorted(rows, key=key)
