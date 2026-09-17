"""SQLite storage.

Four concerns, deliberately separated:

  targets          rewritten wholesale by the updater every cycle
  observer_state   owned by the website, never touched by the updater --
                   this is what makes "mark observed" survive a refresh.
                   Keyed by (site_id, desig, user_id): one row per observer
                   per target per observatory
  users            accounts. Reads are open; writes need one of these
  ephemeris_cache  raw MPC responses keyed by a signature of the object's
                   NEOCP row, so an ephemeris is re-requested only when new
                   observations actually change the solution

The nightly plan file is deliberately built from `targets` alone and never
consults observer_state. The plan is the observatory's, not one observer's --
one person marking a target done must not silently drop it out of the file
the telescope is driven from.

Some data is about the object and some is about the object *as seen from a
site*, and the schema now says which is which:

  per site, carrying site_id   targets, ephemeris_cache, observer_state,
                               night_archive
  shared by every site         ds42_scores, users, meta, and the whole of
                               neocp_history

That split is the efficiency win rather than an accident of layout: an object
is scored once and its NEOCP history recorded once, however many observatories
are watching it. Only the ephemeris is genuinely per-observatory, because MPC
computes it for one observatory code.

Every function that reads or writes a per-site table takes the site it is
working on; passing none means the deployment's default. See sites.py and
multisite.md.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    -- The same object is a different row for each observatory watching it:
    -- its altitude, window, exposure and discard reasons are all statements
    -- about this site's sky, not about the object.
    site_id              INTEGER NOT NULL DEFAULT 1,
    desig                TEXT NOT NULL,
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
    last_updated_utc     TEXT,

    PRIMARY KEY (site_id, desig)
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
-- Also per site: an account can observe at more than one observatory, and
-- having shot an object from one of them says nothing about whether it still
-- needs shooting from another.
CREATE TABLE IF NOT EXISTS observer_state (
    site_id         INTEGER NOT NULL DEFAULT 1,
    desig           TEXT NOT NULL,
    user_id         INTEGER NOT NULL DEFAULT 0,
    observed        INTEGER DEFAULT 0,
    observed_at_utc TEXT,
    hidden          INTEGER DEFAULT 0,
    priority_bump   REAL DEFAULT 0,
    note            TEXT,
    PRIMARY KEY (site_id, desig, user_id)
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

-- Per site, and the one genuinely unavoidable per-observatory cost: MPC
-- generates an ephemeris for one observatory code, so the same object has to
-- be fetched once per site. Measured at ~1.1 fetches per cycle per site.
CREATE TABLE IF NOT EXISTS ephemeris_cache (
    site_id     INTEGER NOT NULL DEFAULT 1,
    desig       TEXT NOT NULL,
    signature   TEXT,
    fetched_utc TEXT,
    payload     TEXT,
    PRIMARY KEY (site_id, desig)
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

-- Mostly per site, and the exception is the point of the site_id column.
-- "Which night is it", "did the last cycle work", "where is the plan file"
-- are all answers about one observatory -- and once sites span longitudes,
-- "tonight" stops being one thing at all. A handful of keys really are about
-- the deployment (whether the one-off ds42 history backfill has run), and
-- those sit on site_id 0.
CREATE TABLE IF NOT EXISTS meta (
    site_id INTEGER NOT NULL DEFAULT 0,
    key     TEXT NOT NULL,
    value   TEXT,
    PRIMARY KEY (site_id, key)
);

-- One row per night, written once at rollover (see archive_night). Ephemeris
-- tracks are the only thing that would otherwise be lost: prune_cache drops
-- an object's cache entry once it rolls off NEOCP, and unlike targets and
-- observer_state there is no other record of where it actually was.
-- Per site, and per site the night label itself differs: once observatories
-- span longitudes, "tonight" stops being one global thing.
CREATE TABLE IF NOT EXISTS night_archive (
    site_id      INTEGER NOT NULL DEFAULT 1,
    night        TEXT NOT NULL,
    archived_utc TEXT,
    start_ts     REAL,
    end_ts       REAL,
    payload      TEXT,
    PRIMARY KEY (site_id, night)
);

-- Settings edited through the web, one row per changed field, layered over
-- the site's own TOML file when the site is used.
--
-- The file stays the written record rather than being rewritten, because the
-- file is where the *reasoning* lives -- why a wedge starts at 247.5, why a
-- sector's figure is interpolated -- and a form cannot write a comment. So
-- reverting a setting is deleting a row here, and the file's value comes back
-- untouched. A field nobody has edited is simply absent, which is why a site
-- never touched through the web behaves exactly as it did before this table
-- existed.
--
-- previous_json is the value this row replaced, kept so a change can be undone
-- without anyone having to remember what it was.
CREATE TABLE IF NOT EXISTS site_settings (
    site_id       INTEGER NOT NULL,
    field         TEXT NOT NULL,
    value_json    TEXT NOT NULL,
    previous_json TEXT,
    changed_utc   TEXT NOT NULL,
    changed_by    INTEGER,
    PRIMARY KEY (site_id, field)
);

-- Observatories created through sign-up. A site that comes from a file in
-- sites/ is not in here: the registry serves both kinds and nothing
-- downstream knows which kind it got, which is what keeps the first
-- observatory on the same code path as the newest one.
--
-- The definition is stored as JSON rather than as forty columns because that
-- is what it is -- one Site, whole -- and because a settings edit is already
-- a JSON override layered on top of it.
CREATE TABLE IF NOT EXISTS sites (
    id              INTEGER PRIMARY KEY,
    obscode         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_utc     TEXT NOT NULL,
    created_by      INTEGER,
    active          INTEGER NOT NULL DEFAULT 1,
    definition_json TEXT NOT NULL
);

-- Who works at which observatory. One role boundary, deliberately: the
-- owner created the site and may change what it does; everyone else marks
-- targets and reads the board.
CREATE TABLE IF NOT EXISTS site_members (
    site_id   INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    role      TEXT NOT NULL DEFAULT 'member',
    added_utc TEXT NOT NULL,
    PRIMARY KEY (site_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_targets_seq
    ON targets(site_id, observable DESC, max_alt_ts ASC);
"""

_COLS = [
    "site_id",
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


# The per-site tables that hold state nothing can regenerate, so they are
# rebuilt to gain site_id rather than dropped. `targets` is absent on purpose:
# the updater rewrites it every cycle, so it is dropped and rebuilt instead.
_PER_SITE_TABLES = ("observer_state", "ephemeris_cache", "night_archive",
                    "meta")

# meta keys that describe one observatory's last cycle rather than the
# deployment. Anything not listed here stays global, on site_id 0 -- which is
# the right answer for the one-off flags, and the safe answer for a key added
# later and forgotten about, since a global key read per-site would simply
# come back empty rather than come back wrong.
_SITE_META_KEYS = (
    "night", "last_update_utc", "last_update_count", "last_update_observable",
    "last_update_timings", "last_update_ok", "last_error",
    "crosscheck_mismatches", "plan_path",
)


def _site(site):
    """The site to work on. None means the deployment's default."""
    return config.DEFAULT_SITE if site is None else site


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

    # Gaining site_id changes each of these tables' PRIMARY KEY, which SQLite
    # cannot do with ALTER, so they are renamed aside, recreated from SCHEMA
    # below, and copied back. Done here rather than after executescript
    # because CREATE TABLE IF NOT EXISTS is a no-op against the old table and
    # would leave it, unchanged and unnoticed, forever.
    pending = _tables_predating_site_id(conn)
    for table in pending:
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_pre_site")

    conn.executescript(SCHEMA)
    _adopt_pre_site_rows(conn, pending)
    conn.commit()


def _tables_predating_site_id(conn):
    """Per-site tables that exist but were written before site_id did."""
    out = []
    for table in _PER_SITE_TABLES:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and "site_id" not in cols:
            out.append(table)
    return out


def _adopt_pre_site_rows(conn, pending):
    """Copy the renamed tables' rows into the new ones, as the default site.

    Every row that existed before site_id belongs to the observatory this
    deployment has always served, so they all take its id -- L01 becomes site
    one by migration rather than by privilege.

    Unlike the user_id migration this cannot lose anything: it is a pure
    column addition, one old row to exactly one new row. The count is checked
    rather than assumed, and only then is the old table dropped -- the whole
    point of these three tables is that nothing can fetch them back.
    """
    site_id = config.DEFAULT_SITE.id
    for table in pending:
        old = f"{table}_pre_site"
        cols = sorted(r[1] for r in conn.execute(f"PRAGMA table_info({old})"))
        names = ",".join(cols)
        if table == "meta":
            # meta is the one table whose rows do not all belong to a site:
            # the per-cycle keys are this deployment's single observatory's,
            # everything else describes the deployment itself.
            marks = ",".join("?" * len(_SITE_META_KEYS))
            conn.execute(
                f"INSERT INTO meta ({names}, site_id) SELECT {names}, "
                f"CASE WHEN key IN ({marks}) THEN ? ELSE 0 END FROM {old}",
                (*_SITE_META_KEYS, site_id))
        else:
            conn.execute(f"INSERT INTO {table} ({names}, site_id) "
                         f"SELECT {names}, ? FROM {old}", (site_id,))
        before = conn.execute(f"SELECT count(*) FROM {old}").fetchone()[0]
        after = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        if before != after:
            raise RuntimeError(
                f"site_id migration of {table} would lose rows: "
                f"{before} before, {after} after -- refusing to drop {old}")
        conn.execute(f"DROP TABLE {old}")


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

def load_cache(conn, site=None):
    return {r["desig"]: (r["signature"], json.loads(r["payload"]))
            for r in conn.execute(
                "SELECT desig, signature, payload FROM ephemeris_cache "
                "WHERE site_id=?", (_site(site).id,))}


def load_offsets(conn, desig, site=None):
    """Uncertainty-map points for one object, or None if not cached."""
    row = conn.execute(
        "SELECT payload FROM ephemeris_cache WHERE site_id=? AND desig=?",
        (_site(site).id, desig)).fetchone()
    if not row:
        return None
    pts = json.loads(row["payload"]).get("offsets")
    return [tuple(p) for p in pts] if pts else None


def load_tracks(conn, desigs, site=None):
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
            f"SELECT desig, payload FROM ephemeris_cache "
            f"WHERE site_id=? AND desig IN ({marks})",
            (_site(site).id, *want)):
        lines = json.loads(r["payload"]).get("lines")
        if lines:
            out[r["desig"]] = lines
    return out


def load_gap_fill_lines(conn, desig, site=None):
    """The same object's ephemeris with no altitude floor, cached
    specifically to patch holes the normal (oalt=20) fetch leaves in the
    altitude plot. See ephemeris.fetch_gap_fill / update_neocp.py's _aux.
    None if this object was never stale since that fetch was added, or the
    fetch itself failed."""
    row = conn.execute(
        "SELECT payload FROM ephemeris_cache WHERE site_id=? AND desig=?",
        (_site(site).id, desig)).fetchone()
    if not row:
        return None
    return json.loads(row["payload"]).get("gap_fill_lines") or None


def archive_night(conn, night, site=None):
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
    # The observer_state join carries site_id as well as desig: the same
    # account marking the same object at another observatory is a different
    # night's work, and joining on desig alone would import it into this
    # site's archive.
    s = _site(site)
    rows = conn.execute("""
        SELECT t.desig, t.score, t.vmag, t.window_start_ts, t.window_end_ts,
               COALESCE(MAX(s.observed), 0) AS observed_any,
               group_concat(CASE WHEN s.observed = 1
                                 THEN COALESCE(u.username, 'shared') END)
                   AS observers
        FROM targets t
        LEFT JOIN observer_state s
               ON s.desig = t.desig AND s.site_id = t.site_id
        LEFT JOIN users u ON u.id = s.user_id
        WHERE t.site_id = ?
          AND t.observable = 1
          AND t.window_start_ts IS NOT NULL AND t.window_end_ts IS NOT NULL
        GROUP BY t.desig
    """, (s.id,)).fetchall()
    if not rows:
        return False

    tracks = load_tracks(conn, [r["desig"] for r in rows], s)
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
            "(site_id, night, archived_utc, start_ts, end_ts, payload) "
            "VALUES (?,?,?,?,?,?)",
            (s.id, night, utcnow(), start, end, json.dumps(payload)))
    return cur.rowcount > 0


def load_archived_night(conn, night, site=None):
    """A previously archived night's targets/tracks/bounds, or None."""
    row = conn.execute(
        "SELECT start_ts, end_ts, payload FROM night_archive "
        "WHERE site_id=? AND night=?",
        (_site(site).id, night)).fetchone()
    if not row:
        return None
    d = json.loads(row["payload"])
    d["start_ts"] = row["start_ts"]
    d["end_ts"] = row["end_ts"]
    return d


def list_archived_nights(conn, site=None):
    """Archived nights, most recent first, as [{night, start_ts, end_ts}, ...]."""
    return [dict(r) for r in conn.execute(
        "SELECT night, start_ts, end_ts FROM night_archive "
        "WHERE site_id=? ORDER BY night DESC", (_site(site).id,))]


def save_cache(conn, entries, site=None):
    """entries: {desig: (signature, payload dict)}"""
    now = utcnow()
    site_id = _site(site).id
    with conn:
        conn.executemany(
            "INSERT INTO ephemeris_cache "
            "(site_id,desig,signature,fetched_utc,payload) "
            "VALUES (?,?,?,?,?) ON CONFLICT(site_id,desig) DO UPDATE SET "
            "signature=excluded.signature, fetched_utc=excluded.fetched_utc, "
            "payload=excluded.payload",
            [(site_id, d, sig, now, json.dumps(p))
             for d, (sig, p) in entries.items()])


def prune_cache(conn, keep_desigs, site=None):
    """Drop cached ephemerides for objects no longer on NEOCP.

    Scoped to this site: another observatory's cache of the same object is
    its own business, and the object may still be on its board.
    """
    site_id = _site(site).id
    keep = set(keep_desigs)
    have = [r["desig"] for r in conn.execute(
        "SELECT desig FROM ephemeris_cache WHERE site_id=?", (site_id,))]
    gone = [(site_id, d) for d in have if d not in keep]
    if gone:
        with conn:
            conn.executemany(
                "DELETE FROM ephemeris_cache WHERE site_id=? AND desig=?", gone)
    return len(gone)


# --- targets ---------------------------------------------------------------

def replace_targets(conn, rows, site=None):
    """Rewrite one site's targets. observer_state is intentionally untouched.

    Only this site's rows are replaced. Another observatory's board is not
    this cycle's business, and deleting the whole table would empty it.
    """
    now = utcnow()
    site_id = _site(site).id
    first_seen = {r["desig"]: r["first_seen_utc"]
                  for r in conn.execute(
                      "SELECT desig, first_seen_utc FROM targets "
                      "WHERE site_id=?", (site_id,))}

    payload = []
    for r in rows:
        d = dict(r)
        d["site_id"] = site_id
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
        conn.execute("DELETE FROM targets WHERE site_id=?", (site_id,))
        conn.executemany(
            f"INSERT INTO targets ({','.join(_COLS)}) VALUES ({placeholders})",
            payload)
    return len(payload)


def load_targets(conn, include_hidden=False, include_observed=False,
                 include_discarded=True, user_id=None, site=None):
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
               -- Scoped to this site as well as this object: "somebody else
               -- has already shot this" must mean from HERE. Another
               -- observatory having it done is not a reason to skip it.
               (SELECT count(*) FROM observer_state o
                 WHERE o.desig = t.desig AND o.site_id = t.site_id
                   AND o.observed = 1
                   AND o.user_id IS NOT ?)  AS others_observed,
               (SELECT group_concat(COALESCE(u.username, 'shared'))
                  FROM observer_state o
                  LEFT JOIN users u ON u.id = o.user_id
                 WHERE o.desig = t.desig AND o.site_id = t.site_id
                   AND o.observed = 1
                   AND o.user_id IS NOT ?)  AS others_names,
               -- ds42's posterior, and the status that says what kind of
               -- number it is. Both, always: p_neo alone cannot distinguish
               -- a computed 1.0 from a policy-assigned one, and NULL from
               -- "not scored yet" from "scored, undefined".
               d.p_neo                      AS p_neo,
               d.status                     AS ds42_status,
               d.n_obs                      AS ds42_n_obs
        FROM targets t
        LEFT JOIN observer_state s
               ON s.desig = t.desig AND s.site_id = t.site_id
              AND s.user_id IS ?
        -- ds42 joins on desig alone, deliberately: a score is a property of
        -- the discovery tracklet, so it is the same number at every site.
        LEFT JOIN ds42_scores d ON d.desig = t.desig
        WHERE t.site_id = ?
    """
    rows = []
    for r in conn.execute(sql, (user_id, user_id, user_id, _site(site).id)):
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


def set_state(conn, desig, user_id=0, site=None, **fields):
    """Record one account's view of one target at one site.

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
            f"INSERT INTO observer_state (site_id,desig,user_id,{cols}) "
            f"VALUES (?,?,?,{placeholders}) "
            f"ON CONFLICT(site_id,desig,user_id) DO UPDATE SET {updates}",
            (_site(site).id, desig, user_id, *fields.values()))


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
            #
            # Deliberately not scoped to a site: this only ever runs for the
            # very first account on the deployment, which can only happen in
            # the era before a second observatory existed, so every row it
            # adopts is already site one's.
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


# --- observatories created through sign-up -----------------------------------

OWNER, MEMBER = "owner", "member"


def create_site(conn, site_id, obscode, definition_json, user_id):
    """Register a new observatory, owned by whoever created it.

    The owner row is written in the same transaction as the site. A site with
    no owner is one nobody can configure, which on a self-service deployment
    is a site nobody can fix.
    """
    with conn:
        conn.execute(
            "INSERT INTO sites (id, obscode, created_utc, created_by, active,"
            " definition_json) VALUES (?,?,?,?,1,?)",
            (site_id, obscode.upper(), utcnow(), user_id, definition_json))
        conn.execute(
            "INSERT INTO site_members (site_id, user_id, role, added_utc) "
            "VALUES (?,?,?,?)", (site_id, user_id, OWNER, utcnow()))
    return site_id


def load_db_sites(conn, include_inactive=False):
    """Every signed-up observatory, as stored rows."""
    sql = "SELECT * FROM sites"
    if not include_inactive:
        sql += " WHERE active=1"
    return [dict(r) for r in conn.execute(sql + " ORDER BY id")]


def db_sites_stamp(conn):
    """Cheap cache key: changes when a site is added or deactivated."""
    row = conn.execute(
        "SELECT count(*) AS n, max(created_utc) AS t, sum(active) AS a "
        "FROM sites").fetchone()
    return (row["n"], row["t"], row["a"]) if row else (0, None, None)


def site_by_obscode(conn, obscode):
    row = conn.execute(
        "SELECT * FROM sites WHERE obscode = ? COLLATE NOCASE",
        ((obscode or "").strip().upper(),)).fetchone()
    return dict(row) if row else None


def set_site_active(conn, site_id, active):
    with conn:
        conn.execute("UPDATE sites SET active=? WHERE id=?",
                     (1 if active else 0, site_id))


def add_site_member(conn, site_id, user_id, role=MEMBER):
    with conn:
        conn.execute(
            "INSERT INTO site_members (site_id, user_id, role, added_utc) "
            "VALUES (?,?,?,?) ON CONFLICT(site_id, user_id) DO UPDATE SET "
            "role=excluded.role", (site_id, user_id, role, utcnow()))


def site_role(conn, site_id, user_id):
    """'owner', 'member', or None. None for a signed-out reader."""
    if user_id is None:
        return None
    row = conn.execute(
        "SELECT role FROM site_members WHERE site_id=? AND user_id=?",
        (site_id, user_id)).fetchone()
    return row["role"] if row else None


def sites_for_user(conn, user_id):
    """[(site_id, role)] for every observatory this account belongs to."""
    if user_id is None:
        return []
    return [(r["site_id"], r["role"]) for r in conn.execute(
        "SELECT site_id, role FROM site_members WHERE user_id=? "
        "ORDER BY site_id", (user_id,))]


def site_members(conn, site_id):
    """Everyone at one observatory, owner first."""
    return [dict(r) for r in conn.execute(
        "SELECT m.user_id, m.role, m.added_utc, u.username, u.email "
        "FROM site_members m LEFT JOIN users u ON u.id = m.user_id "
        "WHERE m.site_id=? ORDER BY m.role='owner' DESC, u.username",
        (site_id,))]


# --- edited settings ---------------------------------------------------------

def load_site_overrides(conn, site_id):
    """{field: value} for one site's edited settings."""
    return {r["field"]: json.loads(r["value_json"])
            for r in conn.execute(
                "SELECT field, value_json FROM site_settings WHERE site_id=?",
                (site_id,))}


def site_settings_rows(conn, site_id):
    """The same, with what each edit replaced and who made it.

    Feeds the settings page's "edited" markers and its undo, so an observer
    can see that a limit is not the one the file states, and what it was.
    """
    return {r["field"]: dict(r) for r in conn.execute(
        "SELECT field, value_json, previous_json, changed_utc, changed_by "
        "FROM site_settings WHERE site_id=?", (site_id,))}


def site_settings_stamp(conn, site_id):
    """When this site's settings last changed, or None. Cheap cache key."""
    row = conn.execute(
        "SELECT max(changed_utc) AS s FROM site_settings WHERE site_id=?",
        (site_id,)).fetchone()
    return row["s"] if row else None


def save_site_override(conn, site_id, field, value_json, previous_json,
                       user_id=None):
    """Store one field's new value, keeping what it replaced.

    previous_json is only written when this is the FIRST edit of a field --
    after that the stored previous stays the file's original value, so undo
    always returns to what the site was configured with rather than walking
    back one edit at a time through a long afternoon of tuning.
    """
    with conn:
        conn.execute(
            "INSERT INTO site_settings "
            "(site_id, field, value_json, previous_json, changed_utc, "
            " changed_by) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(site_id, field) DO UPDATE SET "
            "value_json=excluded.value_json, "
            "changed_utc=excluded.changed_utc, "
            "changed_by=excluded.changed_by",
            (site_id, field, value_json, previous_json, utcnow(), user_id))


def clear_site_override(conn, site_id, field):
    """Forget an edit, so the site's file decides this field again."""
    with conn:
        cur = conn.execute(
            "DELETE FROM site_settings WHERE site_id=? AND field=?",
            (site_id, field))
    return cur.rowcount > 0


def set_meta(conn, key, value, site=None):
    """Record a key. `site` None means the deployment rather than a site."""
    site_id = 0 if site is None else site.id
    with conn:
        conn.execute(
            "INSERT INTO meta (site_id,key,value) VALUES (?,?,?) "
            "ON CONFLICT(site_id,key) DO UPDATE SET value=excluded.value",
            (site_id, key, str(value)))


def get_meta(conn, key, default=None, site=None):
    """Read a key. `site` None means the deployment rather than a site."""
    site_id = 0 if site is None else site.id
    row = conn.execute("SELECT value FROM meta WHERE site_id=? AND key=?",
                       (site_id, key)).fetchone()
    return row["value"] if row else default
