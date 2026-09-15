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
# shipped as sort=mag_asc/mag_desc keep working). Each entry names the row
# field to read and the two captions the flip button shows for that field.
SORTABLE_COLUMNS = {
    "mag":          {"field": "vmag",           "label": "Magnitude",
                      "low": "brightest first",    "high": "faintest first"},
    "digest2":      {"field": "score",           "label": "Digest2",
                      "low": "lowest first",       "high": "highest first"},
    "exposure_min": {"field": "exposure_min",     "label": "Exposure",
                      "low": "shortest first",     "high": "longest first"},
    "motion":       {"field": "cur_motion",       "label": "Motion",
                      "low": "slowest first",      "high": "fastest first"},
    "alt":          {"field": "cur_alt",          "label": "Altitude",
                      "low": "lowest first",       "high": "highest first"},
    "az":           {"field": "cur_az",           "label": "Azimuth",
                      "low": "lowest first",       "high": "highest first"},
    "moon":         {"field": "cur_moon_dist",    "label": "Moon distance",
                      "low": "closest first",      "high": "farthest first"},
    "unseen":       {"field": "not_seen_days",    "label": "Not seen",
                      "low": "most recent first",  "high": "longest first"},
    "q":            {"field": "q",                "label": "Perihelion (q)",
                      "low": "smallest first",     "high": "largest first"},
}


def parse_mode_columns(mode):
    """{column_key: ascending}, in priority order, for a mode string like
    "mag_desc,alt_asc" -- the compound-sort combination the filter dropdown
    built. Used both to actually sort and to prefill the dropdown's checkboxes
    and flip buttons, so the two can never drift apart. Empty for the two
    curated modes, or for anything that names no real column.
    """
    if not mode or mode in ("chronological", "score"):
        return {}
    active = {}
    for token in mode.split(","):
        for suffix, ascending in (("_asc", True), ("_desc", False)):
            if token.endswith(suffix):
                key = token[: -len(suffix)]
                if key in SORTABLE_COLUMNS:
                    active[key] = ascending
                break
    return active


def sort_key_compound(columns):
    """Sort by several columns at once, in the order given: the first breaks
    the most ties, each later one only matters among rows still tied on
    everything before it. Missing values sink within their own column's
    contribution, same as a single-column sort would."""
    def key(row):
        parts = [0 if row.get("observable") else 1]
        for field, ascending in columns:
            v = row.get(field)
            parts.append(v is None)
            parts.append(0.0 if v is None else (v if ascending else -v))
        parts.append(row["desig"])
        return tuple(parts)
    return key


def sort_targets(rows, mode=None):
    mode = mode or config.DEFAULT_SORT
    if mode == "chronological":
        key = sort_key_chronological
    elif mode == "score":
        key = sort_key_score
    else:
        active = parse_mode_columns(mode)
        columns = [(SORTABLE_COLUMNS[k]["field"], asc) for k, asc in active.items()]
        key = sort_key_compound(columns) if columns else sort_key_chronological
    return sorted(rows, key=key)
