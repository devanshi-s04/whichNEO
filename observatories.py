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

_sites = None
_details = None


def _load_sites():
    out = {}
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
    except OSError:
        pass
    return out


def _load_details():
    """Blocks of COD/OBS/MEA/TEL lines, one per observatory."""
    out = {}
    path = os.path.join(_DIR, "details.txt")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read().replace("\r", "")
    except OSError:
        return out

    code = None
    for line in text.splitlines():
        if line.startswith("COD "):
            code = line[4:].strip()
            out.setdefault(code, {})
        elif code and len(line) > 4 and line[:3] in ("OBS", "MEA", "TEL"):
            out[code].setdefault(line[:3], []).append(line[4:].strip())
    return out


def _ensure():
    global _sites, _details
    if _sites is None:
        _sites = _load_sites()
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
