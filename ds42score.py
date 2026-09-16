"""Score NEOCP tracklets with ds42, from the astrometry we already cache.

ds42 is run as a **subprocess**, not imported. That is the whole design, and
it is deliberate:

  - ds42 lives outside this repository, in its own virtualenv, and is a
    separate fast-moving project. A subprocess boundary means its
    dependencies stay its own and ours stay ours.
  - It cannot take the board down. An import-time failure, a segfault in the
    Rust kernel, a model file that has gone missing -- all of it is a
    non-zero exit code to handle, not a dead updater. The board's job is to
    tell an observer where to point a telescope; nothing about a research
    score is worth risking that.
  - The CLI's defaults *are* the operational configuration (variant=All,
    h_extend_to=30, the 1200x50 grid, the D-19 triage policy). Using it
    means not re-deriving that pinning in our own code, where it could
    silently drift from what ds42 itself considers correct.

The cost is reloading the 3.9 MB model per invocation, about 1.5 s. That is
why scoring is batched: one subprocess per cycle for every object that needs
a score, not one per object. Measured, 73 objects score in ~4.8 s total.

Scores are computed **once per object**. p_neo is a function of the discovery
tracklet, and ds42's defaults truncate to the first two hours of the first
night, so it does not move as follow-up accumulates -- measured across two
nights, all 60 objects present on both scored identically. See ds42.md.
"""

import json
import logging
import os
import subprocess
import tempfile

import config

log = logging.getLogger(__name__)


def available():
    """Whether ds42 can be run at all. The board must work without it."""
    return bool(config.DS42_ENABLED
                and os.path.exists(config.DS42_BIN)
                and os.path.exists(config.DS42_MODEL))


def provenance():
    """What identifies these scores, resolved now rather than trusted from a
    version string.

    deploy/DS42.md is emphatic about this: an editable install freezes
    ds42.__version__ at install time, so it names whatever commit was checked
    out when pip ran, not the one on disk. Git HEAD at scoring time plus a
    dirty flag plus the model's own hash is what actually identifies a score.
    """
    import hashlib

    def sh(*cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=10).stdout.strip()
        except Exception:
            return ""

    src = config.DS42_SRC
    try:
        with open(config.DS42_MODEL, "rb") as f:
            model_sha = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        model_sha = ""
    return {
        "ds42_rev": sh("git", "-C", src, "rev-parse", "HEAD"),
        "ds42_dirty": bool(sh("git", "-C", src, "status", "--porcelain")),
        "model_sha256": model_sha,
        "config": {"variant": "All", "h_extend_to": 30,
                   "h_interpolation": "none", "grid": "1200x50",
                   "max_arc_hours": 2, "night_gap_hours": 3,
                   "note": "ds42 CLI defaults = D-18/D-19 operational config"},
    }


def score_records(records_by_desig):
    """{desig: [80-col record, ...]} -> {desig: {p_neo, status, ...}}.

    Returns {} on any failure, having logged it. Never raises: the caller is
    an update cycle whose real job is the observing plan.
    """
    if not available() or not records_by_desig:
        return {}

    lines = []
    for desig in sorted(records_by_desig):
        lines.extend(records_by_desig[desig] or [])
    if not lines:
        return {}

    tmp = tempfile.mkdtemp(prefix="ds42-")
    obs_path = os.path.join(tmp, "tracklets.obs")
    out_path = os.path.join(tmp, "scores.tsv")
    try:
        with open(obs_path, "w") as f:
            f.write("\n".join(lines) + "\n")

        cmd = [config.DS42_BIN, "score",
               "--model", config.DS42_MODEL,
               "--obscodes", config.DS42_OBSCODES,
               "-o", out_path, obs_path]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=config.DS42_TIMEOUT_S)
        if r.returncode != 0:
            log.error("ds42 exited %d: %s", r.returncode,
                      (r.stderr or "").strip()[-400:])
            return {}
        return _parse_scores(open(out_path).read())
    except subprocess.TimeoutExpired:
        log.error("ds42 timed out after %ss on %d objects",
                  config.DS42_TIMEOUT_S, len(records_by_desig))
        return {}
    except Exception:
        log.exception("ds42 scoring failed")
        return {}
    finally:
        for p in (obs_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass


def _parse_scores(text):
    """ds42's TSV: object_id, n_obs, arc_h, epoch, obscode, V, p_neo, log_lr,
    status. Split out so it can be tested without running anything."""
    out = {}
    rows = text.splitlines()
    if not rows:
        return out
    header = rows[0].split("\t")
    for line in rows[1:]:
        parts = line.split("\t")
        if len(parts) != len(header):
            continue
        d = dict(zip(header, parts))
        desig = d.get("object_id")
        if not desig:
            continue
        out[desig] = {
            "p_neo": _num(d.get("p_neo")),
            "log_lr": _num(d.get("log_lr")),
            "status": d.get("status") or "",
            "n_obs": int(_num(d.get("n_obs")) or 0),
            "arc_h": _num(d.get("arc_h")),
            "obscode": d.get("obscode") or "",
            "vmag": _num(d.get("V")),
        }
    return out


def _num(s):
    """Float, or None for the blanks and nans ds42 writes for an undefined
    posterior. nan must not survive: it reaches SQLite and JSON, and compares
    false against itself, so a stored nan is a value nothing can ever match."""
    if s is None or s == "":
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return None if v != v else v
