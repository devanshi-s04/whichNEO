"""Editing a site's settings, and layering those edits over its file.

A site is defined by `sites/<code>.toml`. That file is also where the
*reasoning* lives -- why the keep-out wedge starts at 247.5, why the
south-west figure is interpolated -- and rewriting it from a web form would
destroy exactly that. So the file stays the written record and the seed, and
an edit is stored in the database as an override of one field, applied on top
when the site is used.

Three consequences worth stating, because they are the point rather than side
effects:

  * reverting is deleting a row, and the file's value comes back untouched
  * the previous value is kept beside the new one, so a change can be undone
    without knowing what it replaced
  * a field nobody has edited reads from the file, so a site that is never
    touched through the web behaves exactly as it did before this existed

What is NOT editable is as deliberate as what is. The parallax constants and
the observatory code are MPC's facts about where the telescope physically is,
not preferences -- editing them would not move the dome, it would only make
every ephemeris wrong. The mask's azimuth boundaries are fixed 45-degree
sectors because the sector lookup is arithmetic rather than a search.
"""

import json
from dataclasses import dataclass, replace

# --- what may be edited ------------------------------------------------------


@dataclass(frozen=True)
class Spec:
    """One editable scalar: how to show it, and what counts as a sane value."""
    name: str
    label: str
    kind: str                      # float | int | bool | choice | tz
    group: str
    lo: float = None
    hi: float = None
    choices: tuple = ()
    unit: str = ""
    note: str = ""


SCALARS = (
    # --- what reaches the board ---
    Spec("max_mag", "Faintest magnitude", "float", "policy", 5, 30, unit="V"),
    Spec("sun_alt_max", "Sun below", "float", "policy", -90, 0, unit="°"),
    Spec("moon_sep_min", "Moon at least", "float", "policy", 0, 180, unit="°"),
    Spec("min_score", "digest2 score at least", "int", "policy", 0, 100),
    Spec("min_arc_days", "Arc at least", "float", "policy", 0, 365, unit="d"),
    Spec("max_not_seen_days", "Not seen at most", "float", "policy", 0, 365,
         unit="d"),
    Spec("min_motion", "Moving at least", "float", "policy", 0, 10000,
         unit="″/min"),
    Spec("neo_only", "NEO-like orbits only", "bool", "policy"),
    Spec("neo_q_max", "…meaning q below", "float", "policy", 0, 100,
         unit="AU"),
    Spec("neo_e_min", "…or e above", "float", "policy", 0, 10),
    Spec("skip_already_observed", "Skip what this site already shot", "bool",
         "policy"),

    # --- altitude limits ---
    Spec("min_alt", "Flat altitude floor", "float", "altitude", 0, 90,
         unit="°"),
    Spec("mpc_server_min_alt", "Floor asked of MPC", "float", "altitude",
         0, 90, unit="°",
         note="Changing this clears this site's cached ephemerides, because "
              "MPC never sent the rows below the old floor."),

    # --- telescope ---
    Spec("fov_arcsec", "Field of view", "float", "telescope", 1, 100000,
         unit="″"),
    Spec("interpolate_ahead_s", "Interpolate ahead", "int", "telescope",
         0, 7200, unit="s", note="Allows for slewing and settling."),

    # --- exposures ---
    Spec("exposure_base_min", "Base integration", "float", "exposure", 0, 600,
         unit="min"),
    Spec("exposure_ref_mag", "…at magnitude", "float", "exposure", 0, 30,
         unit="V"),
    Spec("exposure_min_per_mag", "…plus per magnitude", "float", "exposure",
         0, 100, unit="min"),
    Spec("exposure_floor_min", "Never less than", "float", "exposure", 0, 600,
         unit="min"),
    Spec("exposure_frames", "Frames per target", "int", "exposure", 1, 10000),

    # --- ordering ---
    Spec("default_sort", "Default order", "choice", "ranking",
         choices=("chronological", "score")),
    Spec("arc_saturate_days", "Arc saturates at", "float", "ranking",
         0.001, 365, unit="d"),
    Spec("mag_bright", "Counted as bright at", "float", "ranking", 0, 30,
         unit="V"),

    # --- the night ---
    Spec("night_rollover_hour_ut", "Night rolls over at", "int", "night",
         0, 23, unit=":00 UT"),
    Spec("display_tz", "Times shown in", "tz", "night"),
)

SCALARS_BY_NAME = {s.name: s for s in SCALARS}

# The zenith blind spot is a scalar that is allowed to be absent, which no
# range can express, so it is handled beside the others rather than among
# them.
MAX_ALTITUDE = "max_altitude"

RANK_WEIGHT_KEYS = ("digest2", "arc", "magnitude")

# Every field this module will ever write. Anything arriving from a form that
# is not in here is ignored rather than trusted -- the whitelist is the
# boundary, not the form's own field list.
EDITABLE = (tuple(s.name for s in SCALARS)
            + (MAX_ALTITUDE, "rank_weights", "horizon_mask", "keepout_wedges"))

HARDNESS = ("soft", "hard")
MAX_REASON = 120


class SettingsError(Exception):
    """A submitted value that must not be stored."""


# --- applying overrides ------------------------------------------------------

_CACHE = {}          # site_id -> (stamp, Site)


def apply_overrides(base, overrides):
    """The site as configured, with any edited fields replacing the file's."""
    if not overrides:
        return base
    clean = {k: v for k, v in overrides.items() if k in EDITABLE}
    return replace(base, **clean) if clean else base


def effective(conn, base):
    """`base` with this site's stored edits applied, cached between calls.

    The cache is keyed on the site's own change stamp, so an edit made in one
    process is picked up by the others on their next read rather than at some
    restart -- and a site nobody has edited costs one cheap query.
    """
    import db
    stamp = db.site_settings_stamp(conn, base.id)
    hit = _CACHE.get(base.id)
    if hit is not None and hit[0] == stamp and hit[1].obscode == base.obscode:
        return hit[1]
    site = apply_overrides(base, db.load_site_overrides(conn, base.id))
    _CACHE[base.id] = (stamp, site)
    return site


def forget(site_id=None):
    """Drop the cache, for tests and for a process that just wrote."""
    if site_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(site_id, None)


# --- parsing what a form sent ------------------------------------------------

def _number(spec, raw):
    text = (raw or "").strip()
    if not text:
        raise SettingsError(f"{spec.label}: give a value")
    try:
        value = float(text)
    except ValueError:
        raise SettingsError(f"{spec.label}: “{text}” is not a number")
    if spec.kind == "int":
        if value != int(value):
            raise SettingsError(f"{spec.label}: must be a whole number")
        value = int(value)
    if spec.lo is not None and value < spec.lo:
        raise SettingsError(f"{spec.label}: must be at least {spec.lo}")
    if spec.hi is not None and value > spec.hi:
        raise SettingsError(f"{spec.label}: must be at most {spec.hi}")
    return value


def _timezone(spec, raw):
    name = (raw or "").strip()
    if not name:
        raise SettingsError(f"{spec.label}: give a timezone")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
    except Exception:
        raise SettingsError(
            f"{spec.label}: “{name}” is not a timezone this host knows. "
            "Use a name like Europe/Zagreb.")
    return name


def _reason(raw, what):
    text = " ".join((raw or "").split())
    if not text:
        raise SettingsError(f"{what}: say why this limit exists")
    if len(text) > MAX_REASON:
        raise SettingsError(
            f"{what}: keep the reason under {MAX_REASON} characters")
    return text


def scalar_from_form(spec, form):
    """One scalar's new value, or raise. Absent means unchanged."""
    if spec.kind == "bool":
        return spec.name in form
    raw = form.get(spec.name)
    if raw is None:
        return None
    if spec.kind == "choice":
        if raw not in spec.choices:
            raise SettingsError(f"{spec.label}: not one of "
                                f"{', '.join(spec.choices)}")
        return raw
    if spec.kind == "tz":
        return _timezone(spec, raw)
    return _number(spec, raw)


def max_altitude_from_form(form):
    """The zenith blind spot, which is allowed to be absent."""
    raw = (form.get(MAX_ALTITUDE) or "").strip()
    if not raw or raw.lower() in ("none", "-", "off"):
        return None
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f"Zenith blind spot: “{raw}” is not a number")
    if not 0 <= value <= 90:
        raise SettingsError("Zenith blind spot: must be between 0 and 90")
    return value


def rank_weights_from_form(form):
    out = {}
    for key in RANK_WEIGHT_KEYS:
        raw = (form.get(f"weight_{key}") or "").strip()
        if not raw:
            raise SettingsError(f"Weight — {key}: give a value")
        try:
            value = float(raw)
        except ValueError:
            raise SettingsError(f"Weight — {key}: “{raw}” is not a number")
        if value < 0:
            raise SettingsError(f"Weight — {key}: cannot be negative")
        out[key] = value
    if sum(out.values()) <= 0:
        raise SettingsError(
            "At least one ranking weight has to be above zero, or every "
            "target scores the same")
    return out


def horizon_mask_from_form(form, base_mask):
    """The mask, sector by sector.

    The azimuth boundaries are not editable: the sector lookup is arithmetic
    on fixed 45-degree spans rather than a search, so a mask with different
    edges would silently be read through the old ones. What a site can change
    is how high it wants to be in each sector, whether that is a preference
    or an obstruction, and why.
    """
    out = []
    for i, entry in enumerate(base_mask):
        start, end = entry[0], entry[1]
        raw = (form.get(f"mask_alt_{i}") or "").strip()
        if raw.lower() in ("", "any", "none", "-"):
            min_alt = None
        else:
            try:
                min_alt = float(raw)
            except ValueError:
                raise SettingsError(
                    f"Sector {i + 1}: “{raw}” is not an altitude")
            if not 0 <= min_alt <= 90:
                raise SettingsError(
                    f"Sector {i + 1}: altitude must be between 0 and 90")
        hardness = form.get(f"mask_hard_{i}", entry[3])
        if hardness not in HARDNESS:
            raise SettingsError(f"Sector {i + 1}: effect must be soft or hard")
        why = _reason(form.get(f"mask_why_{i}"), f"Sector {i + 1}")
        out.append((start, end, min_alt, hardness, why))
    return tuple(out)


def keepout_wedges_from_form(form):
    """The wedges, which remove sky outright.

    Rows arrive as parallel lists so a wedge can be added or dropped. A row
    with every field blank is a deletion; anything half-filled is a mistake
    and is refused rather than guessed at.
    """
    froms = form.getlist("wedge_from")
    tos = form.getlist("wedge_to")
    alts = form.getlist("wedge_alt")
    whys = form.getlist("wedge_why")
    if not (len(froms) == len(tos) == len(alts) == len(whys)):
        raise SettingsError("The keep-out wedge form was submitted "
                            "incomplete; reload the page and try again")

    out = []
    for i, (a, b, alt, why) in enumerate(zip(froms, tos, alts, whys), start=1):
        parts = [(a or "").strip(), (b or "").strip(), (alt or "").strip(),
                 (why or "").strip()]
        if not any(parts):
            continue
        if not all(parts):
            raise SettingsError(
                f"Wedge {i}: fill in every field, or clear the whole row to "
                "remove it")
        try:
            az_from, az_to, min_alt = float(parts[0]), float(parts[1]), \
                float(parts[2])
        except ValueError:
            raise SettingsError(f"Wedge {i}: azimuths and altitude must be "
                                "numbers")
        for name, value in (("azimuth from", az_from), ("azimuth to", az_to)):
            if not 0 <= value <= 360:
                raise SettingsError(
                    f"Wedge {i}: {name} must be between 0 and 360")
        if not 0 <= min_alt <= 90:
            raise SettingsError(
                f"Wedge {i}: altitude must be between 0 and 90")
        if az_from == az_to:
            raise SettingsError(
                f"Wedge {i}: an arc from an azimuth to itself covers nothing. "
                "To refuse the whole sky at a height, use 0 to 360.")
        out.append((az_from, az_to, min_alt,
                    _reason(parts[3], f"Wedge {i}")))
    return tuple(out)


# --- serialising -------------------------------------------------------------

def same(a, b):
    """Whether a submitted value is the one already in force.

    Compared by value rather than by serialised form. A field's TOML integer
    and the float a form round-trips it back as -- 2600 and 2600.0 -- are the
    same setting, and storing an override for that difference would mark the
    field "edited" forever, against a value nobody changed. Sequences compare
    element-wise for the same reason: JSON has no tuples.
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    return a == b


def dumps(value):
    return json.dumps(value, sort_keys=True)


def loads(text):
    value = json.loads(text)
    # JSON has no tuples, and the Site's own normalisation expects sequences
    # it can freeze; lists are fine going in, so nothing to undo here.
    return value
