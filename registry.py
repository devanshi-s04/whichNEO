"""Every observatory this deployment serves.

Two sources, one answer. A site is either a TOML file in `sites/` or a row
written when somebody signed up, and **nothing downstream is told which kind
it got**. That is the whole point: the observatory this board was built for
runs the same code path as the newest one, so a bug in it is found rather
than hidden behind a special case.

Ids are unique across both sources, and a signed-up site takes the next id
after everything already known -- including inactive ones, so a deactivated
site's id is never handed to a different observatory and its rows never
reappear under a new name.
"""

import json
import logging

import config
import db
import siteconf
import sites as sitesmod


def base_sites(conn):
    """Every active site, before any edited settings are applied.

    A stored definition that no longer parses is logged and skipped rather
    than raised: one observatory's bad row must not take every other
    observatory's board down with it. It is loud in the log precisely
    because the site silently vanishing from the switcher is the symptom.
    """
    out = dict(config.SITES)
    for row in db.load_db_sites(conn):
        try:
            site = sitesmod.from_dict(
                json.loads(row["definition_json"]),
                source=f"site {row['obscode']} (id {row['id']})")
        except Exception:
            logging.exception(
                "site %s (id %s) has an unreadable definition and is being "
                "skipped; its board will not update",
                row["obscode"], row["id"])
            continue
        out[site.id] = site
    return {k: out[k] for k in sorted(out)}


def all_sites(conn):
    """Every active site as actually configured, edits included."""
    return {i: siteconf.effective(conn, s)
            for i, s in base_sites(conn).items()}


def base_site(conn, site_id):
    """One site as its file or its row defines it, without edits."""
    return base_sites(conn).get(site_id)


def by_obscode(conn, code):
    """Resolve an observatory code to a configured site, or None."""
    code = (code or "").strip().upper()
    if not code:
        return None
    for site in all_sites(conn).values():
        if site.obscode.upper() == code:
            return site
    return None


def next_id(conn):
    """The id a new observatory should take.

    Counts inactive sites too. Reusing the id of a site that was switched
    off would hand a new observatory the old one's targets, cached
    ephemerides and observer marks.
    """
    known = set(config.SITES)
    known.update(r["id"] for r in db.load_db_sites(conn, include_inactive=True))
    return (max(known) + 1) if known else 1


def active_count(conn):
    """How many observatories are being served right now.

    Both kinds count. The cap exists to bound what this deployment asks of
    MPC, and an ephemeris fetch costs the same whether the site came from a
    file or from a form.
    """
    return len(base_sites(conn))


def at_capacity(conn):
    return active_count(conn) >= config.ACTIVE_SITE_CAP
