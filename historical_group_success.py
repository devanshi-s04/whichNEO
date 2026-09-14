"""One-shot historical success rate per reporting group, from the MPC
"previous NEOCP objects" archive alone -- no live polling, no per-object
astrometry, no waiting for outcomes to resolve over time.

This trades accuracy for immediacy. The real MPC observatory code (the
3-character obscode in ObsCodes.htm, e.g. F51, G96) is only readable from an
object's 80-column astrometry, and that astrometry is only fetchable while
an object is still on the live NEOCP page -- see discovery_stats.py's module
docstring for why, and why that script instead has to watch objects live and
capture their observatory the moment they first appear. This script does not
do that: "group" here is just the first three characters of each object's
NEOCP submission id (its "trksub", e.g. P12, A11, ZTF, C46), which loosely
tracks which survey or station submitted it by convention, but is not the
certified MPC observatory code and is not guaranteed consistent -- the same
physical telescope can submit under more than one prefix, and the mapping
from prefix to site isn't published or guaranteed stable. Good enough for a
quick read of "who reports the most and how often it holds up"; not a
replacement for discovery_stats.py's per-observatory numbers.
"""

import csv
import os
import re

import discovery_stats as ds

_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(_DIR, "historical_group_success.csv")

_IAU_DESIG_RE = re.compile(r"^\d{4}\s")   # "2026 RZ34" -- an outcome label,
                                          # not a submission id; skip it.


def group_of(desig):
    return desig[:3].upper()


def build_stats():
    entries = ds.fetch_prevdes_entries()

    stats = {}
    for desig, outcome in entries.items():
        if _IAU_DESIG_RE.match(desig):
            continue
        g = stats.setdefault(group_of(desig), {
            "reported": 0, "confirmed": 0, "not_confirmed": 0,
            "not_minor_planet": 0, "does_not_exist": 0,
            "suspected_artificial": 0,
        })
        g["reported"] += 1
        g[outcome] = g.get(outcome, 0) + 1
    return stats


def write_csv(stats):
    with open(CSV_PATH, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["group_prefix", "reported", "confirmed", "not_confirmed",
                    "not_minor_planet", "does_not_exist",
                    "suspected_artificial", "success_rate"])
        for g, s in sorted(stats.items(), key=lambda kv: -kv[1]["reported"]):
            rate = s["confirmed"] / s["reported"] if s["reported"] else 0.0
            w.writerow([g, s["reported"], s["confirmed"], s["not_confirmed"],
                        s["not_minor_planet"], s["does_not_exist"],
                        s["suspected_artificial"], f"{rate:.3f}"])


if __name__ == "__main__":
    write_csv(build_stats())
    print(f"wrote {CSV_PATH}")
