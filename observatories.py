"""Turn MPC observatory codes into readable sites and people.

Two vendored lookup tables (see mpcdata/README.md):

  ObsCodes.htm  code -> observatory name, from MPC
  details.txt   code -> observers, measurers, telescope, from Project Pluto

Find_Orb is not run. These are its data files; nothing here executes anything.

The names in details.txt are the people who *typically* observe at a site,
harvested from MPEC headers. The 80-column astrometry format carries only the
observatory code and never the individual, so these must never be shown as the
observer of a particular observation.
"""

import os
import re

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mpcdata")

# ObsCodes is fixed-width, and the parallax fields may or may not be padded:
#
#   F51 203.744090.936241+0.351543Pan-STARRS 1, Haleakala
#   L51  34.0164 0.71169 +0.70028 MARGO, Nauchnij
#
# A regex anchored on the fields running together matches only the first form
# and silently drops the rest -- which was 1418 of 2361 lines. Slicing by
# column handles both. Name starts at column 30.
_CODE_RE = re.compile(r"^[A-Z0-9]{3}[ \d]")
_NAME_COL = 30

# The same fixed columns, for the three numbers that say where the telescope
# is. MPC packs them without separators -- "L01  13.749300.704742+0.707169" is
# longitude 13.74930, rho_cos_phi 0.704742, rho_sin_phi +0.707169 -- so they
# can only be taken by position. Slicing is also what makes the padded and
# unpadded forms both work; see the note above.
_LON_COLS = (3, 13)
_RHO_COS_COLS = (13, 21)
_RHO_SIN_COLS = (21, 30)

_sites = None
_details = None
_geometry = None


def _load_sites():
    out, geom = {}, {}
    path = os.path.join(_DIR, "ObsCodes.htm")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.replace("\r", "").rstrip()
                if len(line) <= _NAME_COL or not _CODE_RE.match(line):
                    continue
                name = line[_NAME_COL:].strip()
                if name:
                    out[line[:3]] = name
                where = _parse_geometry(line)
                if where:
                    geom[line[:3]] = where
    except OSError:
        pass
    return out, geom


def _parse_geometry(line):
    """Longitude and parallax constants from one ObsCodes line, or None.

    A space-probe or roving "observatory" has no fixed position and leaves
    these blank, which is a real answer rather than a parse failure -- such a
    code simply cannot be a site here.
    """
    try:
        lon = float(line[slice(*_LON_COLS)])
        rho_cos = float(line[slice(*_RHO_COS_COLS)])
        rho_sin = float(line[slice(*_RHO_SIN_COLS)])
    except ValueError:
        return None
    if rho_cos == 0.0 and rho_sin == 0.0:
        return None
    return {"lon_deg": lon, "rho_cos_phi": rho_cos, "rho_sin_phi": rho_sin}


def geometry(code):
    """Where MPC says this observatory is, or None if it does not say."""
    _ensure()
    return _geometry.get((code or "").strip().upper())


def _load_details():
    """Blocks of COD/OBS/MEA/TEL lines, one per observatory."""
    out = {}
    path = os.path.join(_DIR, "details.txt")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read().replace("\r", "")
    except OSError:
        return out

    # Current format, three wrinkles:
    #   COD Multi F51  F52     one block shared by several codes
    #   COD 249 A              a code with a sub-designation or site name
    #   COM Valid: ... - ...   several dated rosters stacked under one code,
    #                          newest first
    # Only the first roster in a block is kept, so staff from different eras
    # are never merged into one list. Selecting the roster matching an
    # observation's date is tracked separately; see TBD.md.
    codes, roster_index = [], 0
    for line in text.splitlines():
        if line.startswith("COD "):
            tok = line[4:].split()
            if not tok:
                continue
            codes = tok[1:] if tok[0] == "Multi" else tok[:1]
            roster_index = 0
            for c in codes:
                out.setdefault(c, {})
        elif line.startswith("COM Valid:"):
            roster_index += 1
        elif (codes and roster_index <= 1 and len(line) > 4
              and line[:3] in ("OBS", "MEA", "TEL")):
            for c in codes:
                out[c].setdefault(line[:3], []).append(line[4:].strip())
    return out


def _ensure():
    global _sites, _details, _geometry
    if _sites is None:
        _sites, _geometry = _load_sites()
    if _details is None:
        _details = _load_details()


def site_name(code):
    _ensure()
    return _sites.get(code)


def lookup(code):
    """Everything known about one observatory code."""
    if not code:
        return None
    _ensure()
    d = _details.get(code, {})

    def join(key):
        vals = d.get(key) or []
        # Entries are sometimes split across lines with trailing separators.
        text = " ".join(vals).replace(";", ",").strip().strip(",")
        return re.sub(r"\s*,\s*", ", ", text) or None

    return {
        "code": code,
        "name": _sites.get(code),
        "observers": join("OBS"),
        "measurers": join("MEA"),
        "telescope": join("TEL"),
    }


def describe(code):
    """Short one-line form: 'F51 — Pan-STARRS 1, Haleakala'."""
    name = site_name(code)
    return f"{code} — {name}" if name else code


def count():
    _ensure()
    return len(_sites), len(_details)
