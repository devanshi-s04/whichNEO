"""Per-organization confirmation rate, from neocp_history.py's own database.

"Organization" here is the first three characters of an object's NEOCP
submission id (its trksub, e.g. P12, A11, ZTF) -- a naming convention some
surveys follow, not the certified MPC observatory code (that only comes
from an object's 80-column astrometry, which isn't retained once it's off
the live board -- see moonplot.py's own gap-fill fix for why). The same
heuristic historical_group_success.py used for its one-shot archive scrape;
this is the continuously-updated version, fed by whatever neocp_history.py
has actually polled itself rather than everything MPC's archive page has
ever listed.

Written on every poll cycle -- see neocp_history.py's poll_once, the only
caller of write_csv below -- not scheduled separately.
"""

import csv
import os

import config

CSV_PATH = os.path.join(config.DATA_DIR, "organization_confirmation_rate.csv")


def group_of(desig):
    return desig[:3].upper()


def compute(conn):
    """{group: {reported, confirmed, ...}}, counting only objects that have
    actually resolved. A still-pending object's eventual outcome isn't
    known yet, so including it would understate or overstate a rate purely
    based on when you happen to look."""
    stats = {}
    # Plain tuples: this reads through neocp_history.py's own connection,
    # which (unlike history.py's) never sets row_factory -- desig, status
    # match the SELECT order below positionally, not by name.
    for desig, status in conn.execute(
            "SELECT desig, status FROM objects WHERE status != 'pending'"):
        g = stats.setdefault(group_of(desig), {
            "reported": 0, "confirmed": 0, "not_confirmed": 0,
            "not_minor_planet": 0, "does_not_exist": 0,
            "suspected_artificial": 0,
        })
        g["reported"] += 1
        g[status] = g.get(status, 0) + 1
    return stats


def write_csv(conn):
    stats = compute(conn)
    with open(CSV_PATH, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["group_prefix", "reported", "confirmed", "not_confirmed",
                    "not_minor_planet", "does_not_exist",
                    "suspected_artificial", "confirmation_rate"])
        for g, s in sorted(stats.items(), key=lambda kv: -kv[1]["reported"]):
            rate = s["confirmed"] / s["reported"] if s["reported"] else 0.0
            w.writerow([g, s["reported"], s["confirmed"], s["not_confirmed"],
                        s["not_minor_planet"], s["does_not_exist"],
                        s["suspected_artificial"], f"{rate:.3f}"])
    return CSV_PATH
