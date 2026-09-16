"""SQLite storage.

Four concerns, deliberately separated:

  targets          rewritten wholesale by the updater every cycle
  observer_state   owned by the website, never touched by the updater --
                   this is what makes "mark observed" survive a refresh.
                   Keyed by (desig, user_id): one row per observer per target
  users            accounts. Reads are open; writes need one of these
  ephemeris_cache  raw MPC responses keyed by a signature of the object's
                   NEOCP row, so an ephemeris is re-requested only when new
                   observations actually change the solution

The nightly plan file is deliberately built from `targets` alone and never
consults observer_state. The plan is the observatory's, not one observer's --
one person marking a target done must not silently drop it out of the file
the telescope is driven from.
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

-- Observer state is per account: what one observer has marked done is their
-- own record of their own night, not a fact about the target. user_id 0 is
-- the pre-accounts bucket -- rows migrated from the single-observer table,
-- and writes made with the shared basic-auth credential, which names nobody.
--
-- The cost of this choice is real and is handled in load_targets(): two
-- observers can each spend twenty minutes integrating the same object without
-- either seeing the other do it. Every row therefore also carries a count of
-- how many *other* accounts have marked it, so the duplication is visible
-- even though the state is not shared.
CREATE TABLE IF NOT EXISTS observer_state (
    desig           TEXT NOT NULL,
    user_id         INTEGER NOT NULL DEFAULT 0,
    observed        INTEGER DEFAULT 0,
    observed_at_utc TEXT,
    hidden          INTEGER DEFAULT 0,
    priority_bump   REAL DEFAULT 0,
    note            TEXT,
    PRIMARY KEY (desig, user_id)
);

-- Accounts. Lives here rather than in its own file for the same reason
-- observer_state does: db.init() only ever drops `targets`, so anything else
-- in this database is safe across a schema migration, and one file stays one
-- backup. Passwords are argon2id hashes; nothing here is reversible.
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY,
    username       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email          TEXT UNIQUE COLLATE NOCASE,
    password_hash  TEXT NOT NULL,
    created_utc    TEXT NOT NULL,
    last_login_utc TEXT,
    is_admin       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ephemeris_cache (
    desig       TEXT PRIMARY KEY,
    signature   TEXT,
    fetched_utc TEXT,
    payload     TEXT
);

-- ds42 scores. Deliberately its OWN table rather than a column on targets or
-- a key in the ephemeris cache, because both of those are destroyed on a
-- schedule: targets is rewritten every cycle, and prune_cache drops an
-- object the moment it leaves NEOCP. A score stored in either would vanish
-- exactly when it became interesting -- which is after the object resolves
-- and we can finally ask whether ds42 was right.
--
-- Written once per object and never updated. p_neo is a function of the
-- discovery tracklet, and ds42 truncates to the first two hours of the first
-- night, so it does not move as follow-up accumulates: across two banked
-- nights, all 60 objects present on both scored identically. Re-scoring
-- would spend the model load to arrive at the same number. See ds42.md.
--
-- The provenance columns are not decoration. A p_neo means nothing without
-- the code revision, the model hash and the configuration that produced it,
-- and deploy/DS42.md explains why the revision has to be resolved at scoring
-- time rather than read from ds42.__version__.
CREATE TABLE IF NOT EXISTS ds42_scores (
    desig           TEXT PRIMARY KEY,
    scored_utc      TEXT NOT NULL,
    p_neo           REAL,
    log_lr          REAL,
    status          TEXT,
    n_obs           INTEGER,
    arc_h           REAL,
    obscode         TEXT,
    vmag            REAL,
    ds42_rev        TEXT,
    ds42_dirty      INTEGER,
    model_sha256    TEXT,
    config_json     TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per night, written once at rollover (see archive_night). Ephemeris
-- tracks are the only thing that would otherwise be lost: prune_cache drops
-- an object's cache entry once it rolls off NEOCP, and unlike targets and
-- observer_state there is no other record of where it actually was.
CREATE TABLE IF NOT EXISTS night_archive (
    night        TEXT PRIMARY KEY,
    archived_utc TEXT,
    start_ts     REAL,
    end_ts       REAL,
    payload      TEXT
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
    _migrate_observer_state(conn)
    conn.executescript(SCHEMA)
    conn.commit()


def _migrate_observer_state(conn):
    """Give the single-observer table a user_id, keeping every row.

    `targets` can be dropped and rebuilt because the updater regenerates it.
    observer_state cannot: it is the only record that a target was observed,
    and there is nowhere to fetch it back from. So this rebuilds rather than
    drops, and it runs before the schema, because CREATE TABLE IF NOT EXISTS
    is a no-op against the old table and would leave it in place forever.

    Migrated rows land on user_id 0 rather than on an account, because at
    migration time there may not be one yet. create_user() adopts them when
    the first account is created.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(observer_state)")}
    if not cols or "user_id" in cols:
        return
    keep = [c for c in ("desig", "observed", "observed_at_utc", "hidden",
                        "priority_bump", "note") if c in cols]
    names = ",".join(keep)
    conn.executescript("""
        ALTER TABLE observer_state RENAME TO observer_state_pre_accounts;
        CREATE TABLE observer_state (
            desig           TEXT NOT NULL,
            user_id         INTEGER NOT NULL DEFAULT 0,
            observed        INTEGER DEFAULT 0,
            observed_at_utc TEXT,
            hidden          INTEGER DEFAULT 0,
            priority_bump   REAL DEFAULT 0,
            note            TEXT,
            PRIMARY KEY (desig, user_id)
        );
    """)
    conn.execute(f"INSERT INTO observer_state ({names}, user_id) "
                 f"SELECT {names}, 0 FROM observer_state_pre_accounts")
    # The old table is kept, not dropped. It is a few dozen rows, it is the
    # only copy of state nothing can regenerate, and if this migration turns
    # out to be wrong the rows are still there to re-read.
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


def load_gap_fill_lines(conn, desig):
    """The same object's ephemeris with no altitude floor, cached
    specifically to patch holes the normal (oalt=20) fetch leaves in the
    altitude plot. See ephemeris.fetch_gap_fill / update_neocp.py's _aux.
    None if this object was never stale since that fetch was added, or the
    fetch itself failed."""
    row = conn.execute("SELECT payload FROM ephemeris_cache WHERE desig=?",
                       (desig,)).fetchone()
    if not row:
        return None
    return json.loads(row["payload"]).get("gap_fill_lines") or None


def archive_night(conn, night):
    """Snapshot a just-ended night into night_archive, once.

    Called from the update loop at the moment it notices the night label has
    rolled over -- at that instant targets, observer_state and
    ephemeris_cache still hold the OUTGOING night's last-known state, because
    this cycle's replace_targets/prune_cache have not run yet. One cycle
    later that state is gone: targets is rewritten wholesale, and any object
    that has since resolved and left NEOCP is pruned from the cache with no
    other record of where it actually was. This is the only chance to keep it.

    Returns False (nothing to archive, or already archived) or True.
    """
    # GROUP BY, because observer_state is keyed by (desig, user_id) now. A
    # plain join fans out to one row per target per observer who touched it,
    # which put the same target in the archive twice -- carrying opposite
    # `observed` values -- and drew it twice on the replayed map, once green
    # and once amber. See test_archive_survives_per_account_state.
    #
    # Two fields rather than one, because the question has two answers: the
    # night as the observatory saw it (did anyone shoot this) and the night as
    # a given observer saw it (did *I*). Deciding that at render time keeps
    # both; deciding it here would throw one away permanently.
    rows = conn.execute("""
        SELECT t.desig, t.score, t.vmag, t.window_start_ts, t.window_end_ts,
               COALESCE(MAX(s.observed), 0) AS observed_any,
               group_concat(CASE WHEN s.observed = 1
                                 THEN COALESCE(u.username, 'shared') END)
                   AS observers
        FROM targets t
        LEFT JOIN observer_state s ON s.desig = t.desig
        LEFT JOIN users u ON u.id = s.user_id
        WHERE t.observable = 1
          AND t.window_start_ts IS NOT NULL AND t.window_end_ts IS NOT NULL
        GROUP BY t.desig
    """).fetchall()
    if not rows:
        return False

    tracks = load_tracks(conn, [r["desig"] for r in rows])
    start = min(r["window_start_ts"] for r in rows)
    end = max(r["window_end_ts"] for r in rows)

    payload = {
        "targets": [
            {"desig": r["desig"], "score": r["score"], "vmag": r["vmag"],
             "observed": bool(r["observed_any"]),
             "observers": (r["observers"] or "").split(",") if r["observers"]
                          else [],
             "lines": tracks.get(r["desig"], [])}
            for r in rows
        ],
    }

    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO night_archive "
            "(night, archived_utc, start_ts, end_ts, payload) "
            "VALUES (?,?,?,?,?)",
            (night, utcnow(), start, end, json.dumps(payload)))
    return cur.rowcount > 0


def load_archived_night(conn, night):
    """A previously archived night's targets/tracks/bounds, or None."""
    row = conn.execute(
        "SELECT start_ts, end_ts, payload FROM night_archive WHERE night=?",
        (night,)).fetchone()
    if not row:
        return None
    d = json.loads(row["payload"])
    d["start_ts"] = row["start_ts"]
    d["end_ts"] = row["end_ts"]
    return d


def list_archived_nights(conn):
    """Archived nights, most recent first, as [{night, start_ts, end_ts}, ...]."""
    return [dict(r) for r in conn.execute(
        "SELECT night, start_ts, end_ts FROM night_archive ORDER BY night DESC")]


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
                 include_discarded=True, user_id=None):
    """Targets with the viewing account's own observer state attached.

    `user_id` None is a signed-out visitor: they have no state of their own,
    so every mark on the board counts as somebody else's. That is deliberate --
    a reader who cannot change the board should still be able to see what has
    already been done tonight.

    `others_observed` and `others_names` are the escape hatch for per-account
    state. Without them the first observer to mark a target done makes it
    disappear only from their own list, and the second observer re-shoots it.
    """
    sql = """
        SELECT t.*,
               COALESCE(s.observed, 0)      AS observed,
               s.observed_at_utc            AS observed_at_utc,
               COALESCE(s.hidden, 0)        AS hidden,
               COALESCE(s.priority_bump, 0) AS priority_bump,
               s.note                       AS note,
               (SELECT count(*) FROM observer_state o
                 WHERE o.desig = t.desig AND o.observed = 1
                   AND o.user_id IS NOT ?)  AS others_observed,
               (SELECT group_concat(COALESCE(u.username, 'shared'))
                  FROM observer_state o
                  LEFT JOIN users u ON u.id = o.user_id
                 WHERE o.desig = t.desig AND o.observed = 1
                   AND o.user_id IS NOT ?)  AS others_names
        FROM targets t
        LEFT JOIN observer_state s
               ON s.desig = t.desig AND s.user_id IS ?
    """
    rows = []
    for r in conn.execute(sql, (user_id, user_id, user_id)):
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


def set_state(conn, desig, user_id=0, **fields):
    """Record one account's view of one target.

    user_id 0 is the shared basic-auth credential, which names nobody. It is
    the default so that a caller who forgets to pass an account writes to the
    anonymous bucket rather than silently onto some real observer's record.
    """
    allowed = {"observed", "observed_at_utc", "hidden", "priority_bump", "note"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    cols = ",".join(fields)
    placeholders = ",".join("?" * len(fields))
    updates = ",".join(f"{k}=excluded.{k}" for k in fields)
    with conn:
        conn.execute(
            f"INSERT INTO observer_state (desig,user_id,{cols}) "
            f"VALUES (?,?,{placeholders}) "
            f"ON CONFLICT(desig,user_id) DO UPDATE SET {updates}",
            (desig, user_id, *fields.values()))


# --- ds42 scores ------------------------------------------------------------

def unscored_desigs(conn, desigs):
    """Which of these have no ds42 score yet.

    The filter that makes scoring one-shot. Everything already in the table
    is skipped, so a cycle only ever pays for objects it has not seen.
    """
    want = [d for d in desigs if d]
    if not want:
        return []
    have = set()
    # Chunked: SQLite's variable limit is 999 by default and NEOCP has run to
    # several hundred objects.
    for i in range(0, len(want), 500):
        chunk = want[i:i + 500]
        marks = ",".join("?" * len(chunk))
        have.update(r[0] for r in conn.execute(
            f"SELECT desig FROM ds42_scores WHERE desig IN ({marks})", chunk))
    return [d for d in want if d not in have]


def save_ds42_scores(conn, scores, prov):
    """Insert scores that are not already there. Never overwrites.

    INSERT OR IGNORE rather than REPLACE: a score already stored was produced
    by a known revision and model, and silently replacing it with one from a
    different revision would make the table's provenance columns a lie.
    """
    if not scores:
        return 0
    cfg = json.dumps(prov.get("config") or {}, sort_keys=True)
    rows = [(d, utcnow(), s.get("p_neo"), s.get("log_lr"), s.get("status"),
             s.get("n_obs"), s.get("arc_h"), s.get("obscode"), s.get("vmag"),
             prov.get("ds42_rev"), 1 if prov.get("ds42_dirty") else 0,
             prov.get("model_sha256"), cfg)
            for d, s in sorted(scores.items())]
    with conn:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO ds42_scores (desig, scored_utc, p_neo, "
            "log_lr, status, n_obs, arc_h, obscode, vmag, ds42_rev, "
            "ds42_dirty, model_sha256, config_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return cur.rowcount


def load_ds42_scores(conn, desigs=None):
    """{desig: row dict}, for the whole table or a subset."""
    if desigs is None:
        rows = conn.execute("SELECT * FROM ds42_scores").fetchall()
    else:
        want = [d for d in desigs if d]
        if not want:
            return {}
        rows = []
        for i in range(0, len(want), 500):
            chunk = want[i:i + 500]
            marks = ",".join("?" * len(chunk))
            rows.extend(conn.execute(
                f"SELECT * FROM ds42_scores WHERE desig IN ({marks})", chunk))
    return {r["desig"]: dict(r) for r in rows}


def count_ds42_scores(conn):
    return conn.execute("SELECT count(*) FROM ds42_scores").fetchone()[0]


# --- accounts ---------------------------------------------------------------

def create_user(conn, username, password_hash, email=None, is_admin=0):
    """Insert an account. Raises sqlite3.IntegrityError if the name is taken.

    The uniqueness check is the UNIQUE COLLATE NOCASE constraint, not a
    prior SELECT: two registrations racing between the check and the insert
    would both pass a lookup and one would still have to fail here, so this
    is the only place that can decide it.

    `email` is stored as NULL when blank rather than as "", because SQLite's
    UNIQUE lets any number of NULLs coexist but only one empty string -- the
    second account without an email would otherwise collide with the first.
    """
    first = count_users(conn) == 0
    with conn:
        cur = conn.execute(
            "INSERT INTO users (username, email, password_hash, created_utc, "
            "is_admin) VALUES (?,?,?,?,?)",
            (username, email or None, password_hash, utcnow(),
             1 if is_admin else 0))
        uid = cur.lastrowid
        if first:
            # The board ran for a season before it had accounts, and those
            # marks belong to whoever was making them -- which is whoever
            # registers first. Left on user_id 0 they would show up as
            # "already done by someone else" to every account forever, which
            # is true but useless.
            conn.execute("UPDATE observer_state SET user_id = ? "
                         "WHERE user_id = 0", (uid,))
    return uid


def user_by_name(conn, username):
    row = conn.execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
        (username,)).fetchone()
    return dict(row) if row else None


def user_by_name_or_email(conn, needle):
    """Resolve whatever someone typed into the reset form.

    Observers will type either, and making them guess which one the box wants
    is a way to turn a forgotten password into two forgotten things.
    """
    needle = (needle or "").strip()
    if not needle:
        return None
    return (user_by_name(conn, needle)
            or (user_by_email(conn, needle) if "@" in needle else None))


def user_by_email(conn, email):
    row = conn.execute(
        "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,)
    ).fetchone()
    return dict(row) if row else None


def user_by_id(conn, uid):
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return dict(row) if row else None


def touch_login(conn, uid):
    with conn:
        conn.execute("UPDATE users SET last_login_utc = ? WHERE id = ?",
                     (utcnow(), uid))


def update_password(conn, uid, password_hash):
    with conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (password_hash, uid))


def count_users(conn):
    return conn.execute("SELECT count(*) FROM users").fetchone()[0]


def set_meta(conn, key, value):
    with conn:
        conn.execute(
            "INSERT INTO meta (key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default
