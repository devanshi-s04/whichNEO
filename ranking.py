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


def _site(site):
    """The site to work on. None means the deployment's default.

    The weights are per site: a 0.4-m telescope weighs magnitude differently
    from a 1-m, and the magnitude component is normalised against the site's
    own limiting magnitude.
    """
    return config.DEFAULT_SITE if site is None else site


def _clip01(x):
    return max(0.0, min(1.0, x))


def score_components(row, site=None):
    """Each component normalised to 0..1, higher meaning better target."""
    s = _site(site)
    digest2 = _clip01(row["score"] / 100.0)

    # A shorter arc means a less constrained orbit, so follow-up is worth more.
    arc = _clip01(1.0 - row["arc_days"] / s.arc_saturate_days)

    span = s.max_mag - s.mag_bright
    magnitude = _clip01((s.max_mag - row["vmag"]) / span) if span > 0 else 0.0

    return {"digest2": digest2, "arc": arc, "magnitude": magnitude}


def rank(rows, site=None):
    """Attach score components and a weighted total to every row."""
    s = _site(site)
    for r in rows:
        comps = score_components(r, s)
        r["score_digest2"] = comps["digest2"]
        r["score_arc"] = comps["arc"]
        r["score_magnitude"] = comps["magnitude"]
        r["score_total"] = sum(
            s.rank_weights[k] * v for k, v in comps.items())
    return rows


def max_possible_score(site=None):
    return sum(_site(site).rank_weights.values())


# Targets with no observable window sort last; among those, keep a stable
# order so the table does not shuffle between refreshes.
_FAR_FUTURE = float("inf")


def sort_key_chronological(row):
    ts = row.get("max_alt_ts")
    return (0 if row.get("observable") else 1,
            ts if ts is not None else _FAR_FUTURE,
            row["desig"])


def sort_key_score(row):
    # No manual adjustment term. The up/down arrows that fed priority_bump
    # never worked in the default chronological view -- that key ignores the
    # bump entirely -- so a click stored a number and moved nothing. Rather
    # than leave a control that works in one view and silently fails in the
    # other, the whole mechanism is gone: an invisible per-target offset on a
    # score is hard to notice and harder to undo.
    return (0 if row.get("observable") else 1,
            -(row.get("score_total") or 0.0),
            row["desig"])


# Every column the board can sort or range-filter by beyond the two curated
# sort modes above, keyed by the URL sort-mode prefix ("mag", not "vmag", so
# the URLs already shipped as sort=mag_asc/mag_desc keep working). Sorting is
# driven by clicking the column's own header, so this only needs the row
# field; the range-filter panel also shows `label`, since there's no header
# text to borrow there.
SORTABLE_COLUMNS = {
    "mag":          {"field": "vmag",          "label": "Magnitude"},
    "digest2":      {"field": "score",         "label": "Score"},
    "exposure_min": {"field": "exposure_min",  "label": "Exposure"},
    "motion":       {"field": "cur_motion",    "label": "Motion"},
    "alt":          {"field": "cur_alt",       "label": "Altitude"},
    "az":           {"field": "cur_az",        "label": "Azimuth"},
    "moon":         {"field": "cur_moon_dist", "label": "Moon distance"},
    "unseen":       {"field": "not_seen_days", "label": "Not seen"},
    "q":            {"field": "q",             "label": "Perihelion (q)"},
    # ds42's posterior. Sortable like the rest, and worth sorting: the point
    # of showing it is to find where it disagrees with digest2, which is
    # hard to see by eye in a list of a hundred. Objects with no score sink,
    # the same as any other missing value.
    "ds42":         {"field": "p_neo",         "label": "ds42 p(NEO)"},
}


def parse_mode_columns(mode):
    """{column_key: ascending}, in priority order (the first entry breaks
    the most ties, each later one only matters among rows still tied on
    everything before it), from a mode string like "mag_desc,alt_asc".
    Empty for the two curated modes, or for anything that names no real
    column. Used both to sort and by the template, so several headers can
    be active together and the table's actual order can never drift from
    what they show."""
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


def mode_after_click(mode, key):
    """The mode string after clicking column `key`'s header once: a
    three-state cycle, off -> ascending -> descending -> off again. Every
    other active column keeps both its direction and its place in the
    priority order -- clicking one header never disturbs another, which is
    what lets several of them combine into one compound sort."""
    active = parse_mode_columns(mode)
    if key not in active:
        active[key] = True
    elif active[key]:
        active[key] = False
    else:
        del active[key]
    if not active:
        return "chronological"
    return ",".join(f"{k}_{'asc' if asc else 'desc'}" for k, asc in active.items())


def parse_range_filters(raw):
    """{column_key: (min_or_None, max_or_None)} from a URL value like
    "mag:18:21,alt:15:" -- each token is col:min:max, either bound left
    blank for an open-ended range (there's no lower bound, rather than a
    lower bound of zero). Malformed tokens, unknown columns and non-numeric
    bounds are dropped rather than raising, since this comes straight from
    the URL and a stray edit shouldn't 500 the page."""
    if not raw:
        return {}
    filters = {}
    for token in raw.split(","):
        parts = token.split(":")
        if len(parts) != 3:
            continue
        key, lo_s, hi_s = parts
        if key not in SORTABLE_COLUMNS:
            continue
        try:
            lo = float(lo_s) if lo_s else None
            hi = float(hi_s) if hi_s else None
        except ValueError:
            continue
        if lo is None and hi is None:
            continue
        filters[key] = (lo, hi)
    return filters


def apply_range_filters(rows, filters):
    """Keep only rows whose value for every filtered column falls inside
    that column's [min, max] (either end optional, both inclusive). A row
    missing the field entirely fails any range set on it -- there's no
    sensible "unknown falls inside 18-21" reading, so it's excluded rather
    than kept or guessed at."""
    if not filters:
        return rows
    bounds = [(SORTABLE_COLUMNS[key]["field"], lo, hi)
              for key, (lo, hi) in filters.items()]

    def keep(row):
        for field, lo, hi in bounds:
            v = row.get(field)
            if v is None:
                return False
            if lo is not None and v < lo:
                return False
            if hi is not None and v > hi:
                return False
        return True
    return [r for r in rows if keep(r)]


def sort_key_compound(columns):
    """Sort by several columns at once, in the order given: the first
    breaks the most ties, each later one only matters among rows still
    tied on everything before it. Missing values sink within their own
    column's contribution, same as a single-column sort would."""
    def key(row):
        parts = [0 if row.get("observable") else 1]
        for field, ascending in columns:
            v = row.get(field)
            parts.append(v is None)
            parts.append(0.0 if v is None else (v if ascending else -v))
        parts.append(row["desig"])
        return tuple(parts)
    return key


def sort_targets(rows, mode=None, site=None):
    mode = mode or _site(site).default_sort
    if mode == "chronological":
        key = sort_key_chronological
    elif mode == "score":
        key = sort_key_score
    else:
        active = parse_mode_columns(mode)
        columns = [(SORTABLE_COLUMNS[k]["field"], asc) for k, asc in active.items()]
        key = sort_key_compound(columns) if columns else sort_key_chronological
    return sorted(rows, key=key)
