"""SQLite storage.

Three concerns, deliberately separated:

  targets          rewritten wholesale by the updater every cycle
  observer_state   owned by the website, never touched by the updater --
                   this is what makes "mark observed" survive a refresh
  ephemeris_cache  raw MPC responses keyed by a signature of the object's
                   NEOCP row, so an ephemeris is re-requested only when new
                   observations actually change the solution
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    desig                TEXT PRIMARY KEY,
    score                INTEGER,
    ra_deg               REAL,
    dec_deg              REAL,
    vmag                 REAL,
    hmag                 REAL,
    nobs                 INTEGER,
    arc_days             REAL,
    not_seen_days        REAL,
    update_note          TEXT,
    is_new               INTEGER,
    note_flag            TEXT,
    mpc_flag             TEXT,
    discovery_code       TEXT,
    obs_codes            TEXT,

    q                    REAL,
    e                    REAL,
    incl                 REAL,

    max_alt              REAL,
    max_alt_ts           REAL,
    max_alt_utc          TEXT,
    max_alt_az           REAL,
    mask_flags           TEXT,
    exposure_min         REAL,
    frames               INTEGER,
    frame_sec            INTEGER,
    frame_motion         REAL,
    window_minutes       REAL,
    window_start_ts      REAL,
    window_end_ts        REAL,

    cur_alt              REAL,
    cur_az               REAL,
    cur_motion           REAL,
    cur_moon_dist        REAL,
    cur_sun_alt          REAL,
    cur_vmag             REAL,
    cur_ts               REAL,
    live_row_is_now      INTEGER,

    scat_ra              INTEGER,
    scat_dec             INTEGER,
    scattered_warn       INTEGER,
    observed_from_site   INTEGER,

    eph_rows_total       INTEGER,
    eph_rows_usable      INTEGER,
    eph_error            TEXT,
    eph_report           TEXT,
    map_url              TEXT,
    offsets_url          TEXT,

    score_total          REAL,
    score_digest2        REAL,
    score_arc            REAL,
    score_magnitude      REAL,

    observable           INTEGER,
    discard_reasons      TEXT,
    crosscheck           TEXT,
    plan_block           TEXT,

    first_seen_utc       TEXT,
    last_updated_utc     TEXT
);

CREATE TABLE IF NOT EXISTS observer_state (
    desig           TEXT PRIMARY KEY,
    observed        INTEGER DEFAULT 0,
    observed_at_utc TEXT,
    hidden          INTEGER DEFAULT 0,
    priority_bump   REAL DEFAULT 0,
    note            TEXT
);

CREATE TABLE IF NOT EXISTS ephemeris_cache (
    desig       TEXT PRIMARY KEY,
    signature   TEXT,
    fetched_utc TEXT,
    payload     TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_targets_seq
    ON targets(observable DESC, max_alt_ts ASC);
"""

_COLS = [
    "desig", "score", "ra_deg", "dec_deg", "vmag", "hmag", "nobs", "arc_days",
    "not_seen_days", "update_note", "is_new", "note_flag", "mpc_flag",
    "discovery_code", "obs_codes",
    "q", "e", "incl",
    "max_alt", "max_alt_ts", "max_alt_utc", "max_alt_az", "mask_flags",
    "exposure_min", "frames", "frame_sec", "frame_motion", "window_minutes",
    "window_start_ts", "window_end_ts",
    "cur_alt", "cur_az", "cur_motion", "cur_moon_dist", "cur_sun_alt",
    "cur_vmag", "cur_ts", "live_row_is_now",
    "scat_ra", "scat_dec", "scattered_warn", "observed_from_site",
    "eph_rows_total", "eph_rows_usable", "eph_error", "eph_report",
    "map_url", "offsets_url",
    "score_total", "score_digest2", "score_arc", "score_magnitude",
    "observable", "discard_reasons", "crosscheck", "plan_block",
    "first_seen_utc", "last_updated_utc",
]


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path=None):
    path = path or config.DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # website reads while updater writes
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init(conn):
    # Drop a stale `targets` BEFORE running the schema. The schema indexes
    # columns an older table does not have, so creating it first fails
    # outright and the migration never gets a chance to run. `targets` is
    # rewritten every cycle so dropping it costs nothing; observer_state and
    # ephemeris_cache are never dropped -- they hold state the updater cannot
    # regenerate.
    have = {r[1] for r in conn.execute("PRAGMA table_info(targets)")}
    if have and have != set(_COLS):
        conn.execute("DROP INDEX IF EXISTS idx_targets_seq")
        conn.execute("DROP TABLE targets")
    conn.executescript(SCHEMA)
    conn.commit()


# --- ephemeris cache -------------------------------------------------------

def load_cache(conn):
    return {r["desig"]: (r["signature"], json.loads(r["payload"]))
            for r in conn.execute(
                "SELECT desig, signature, payload FROM ephemeris_cache")}


def load_offsets(conn, desig):
    """Uncertainty-map points for one object, or None if not cached."""
    row = conn.execute("SELECT payload FROM ephemeris_cache WHERE desig=?",
                       (desig,)).fetchone()
    if not row:
        return None
    pts = json.loads(row["payload"]).get("offsets")
    return [tuple(p) for p in pts] if pts else None


def load_tracks(conn, desigs):
    """Cached ephemeris lines for several objects, as {desig: [line, ...]}.

    Feeds the sky map, which is redrawn on each page load so its positions
    are current rather than up to a cycle stale. Reading raw lines back out of
    the cache costs no network and no astronomy.
    """
    want = list(desigs)
    if not want:
        return {}
    out = {}
    marks = ",".join("?" * len(want))
    for r in conn.execute(
            f"SELECT desig, payload FROM ephemeris_cache WHERE desig IN ({marks})",
            want):
        lines = json.loads(r["payload"]).get("lines")
        if lines:
            out[r["desig"]] = lines
    return out


def save_cache(conn, entries):
    """entries: {desig: (signature, payload dict)}"""
    now = utcnow()
    with conn:
        conn.executemany(
            "INSERT INTO ephemeris_cache (desig,signature,fetched_utc,payload) "
            "VALUES (?,?,?,?) ON CONFLICT(desig) DO UPDATE SET "
            "signature=excluded.signature, fetched_utc=excluded.fetched_utc, "
            "payload=excluded.payload",
            [(d, sig, now, json.dumps(p)) for d, (sig, p) in entries.items()])


def prune_cache(conn, keep_desigs):
    """Drop cached ephemerides for objects no longer on NEOCP."""
    keep = set(keep_desigs)
    have = [r["desig"] for r in conn.execute("SELECT desig FROM ephemeris_cache")]
    gone = [(d,) for d in have if d not in keep]
    if gone:
        with conn:
            conn.executemany("DELETE FROM ephemeris_cache WHERE desig=?", gone)
    return len(gone)


# --- targets ---------------------------------------------------------------

def replace_targets(conn, rows):
    """Rewrite the target table. observer_state is intentionally untouched."""
    now = utcnow()
    first_seen = {r["desig"]: r["first_seen_utc"]
                  for r in conn.execute("SELECT desig, first_seen_utc FROM targets")}

    payload = []
    for r in rows:
        d = dict(r)
        sc = d.get("scatteredness")
        d["scat_ra"] = sc[0] if sc else None
        d["scat_dec"] = sc[1] if sc else None
        d["scattered_warn"] = int(bool(d.get("scattered_warn")))
        obs_site = d.get("observed_from_site")
        d["observed_from_site"] = None if obs_site is None else int(bool(obs_site))
        d["discard_reasons"] = json.dumps(d.get("discard_reasons") or [])
        d["mask_flags"] = json.dumps(d.get("mask_flags") or [])
        d["eph_report"] = json.dumps(d.get("eph_report") or {})
        d["obs_codes"] = json.dumps(d["obs_codes"]) if d.get("obs_codes") else None
        d["crosscheck"] = json.dumps(d.get("crosscheck")) if d.get("crosscheck") else None
        d["observable"] = int(bool(d.get("observable")))
        d["live_row_is_now"] = int(bool(d.get("live_row_is_now")))
        d["is_new"] = int(bool(d.get("is_new")))
        d["first_seen_utc"] = first_seen.get(d["desig"], now)
        d["last_updated_utc"] = now
        payload.append(tuple(d.get(c) for c in _COLS))

    placeholders = ",".join("?" * len(_COLS))
    with conn:
        conn.execute("DELETE FROM targets")
        conn.executemany(
            f"INSERT INTO targets ({','.join(_COLS)}) VALUES ({placeholders})",
            payload)
    return len(payload)


def load_targets(conn, include_hidden=False, include_observed=False,
                 include_discarded=True):
    sql = """
        SELECT t.*,
               COALESCE(s.observed, 0)      AS observed,
               s.observed_at_utc            AS observed_at_utc,
               COALESCE(s.hidden, 0)        AS hidden,
               COALESCE(s.priority_bump, 0) AS priority_bump,
               s.note                       AS note
        FROM targets t
        LEFT JOIN observer_state s ON s.desig = t.desig
    """
    rows = []
    for r in conn.execute(sql):
        d = dict(r)
        d["discard_reasons"] = json.loads(d["discard_reasons"] or "[]")
        d["mask_flags"] = json.loads(d["mask_flags"] or "[]")
        d["eph_report"] = json.loads(d["eph_report"] or "{}")
        d["obs_codes"] = json.loads(d["obs_codes"]) if d["obs_codes"] else None
        d["crosscheck"] = json.loads(d["crosscheck"]) if d["crosscheck"] else None
        d["scatteredness"] = ((d["scat_ra"], d["scat_dec"])
                              if d["scat_ra"] is not None else None)
        rows.append(d)

    if not include_hidden:
        rows = [r for r in rows if not r["hidden"]]
    if not include_observed:
        rows = [r for r in rows if not r["observed"]]
    if not include_discarded:
        rows = [r for r in rows if r["observable"]]
    return rows


def set_state(conn, desig, **fields):
    allowed = {"observed", "observed_at_utc", "hidden", "priority_bump", "note"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    cols = ",".join(fields)
    placeholders = ",".join("?" * len(fields))
    updates = ",".join(f"{k}=excluded.{k}" for k in fields)
    with conn:
        conn.execute(
            f"INSERT INTO observer_state (desig,{cols}) VALUES (?,{placeholders}) "
            f"ON CONFLICT(desig) DO UPDATE SET {updates}",
            (desig, *fields.values()))


def set_meta(conn, key, value):
    with conn:
        conn.execute(
            "INSERT INTO meta (key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default
