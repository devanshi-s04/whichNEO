"""Observer-facing website.

Reads only from SQLite -- no astronomy and no network on page load, so
rendering stays fast. The updater has already done all the work.
"""

import csv
import dataclasses
import gzip
import io
import json
import math
import sqlite3
import time
from html import escape
from datetime import datetime, timedelta, timezone

from flask import (Flask, Response, abort, g, has_request_context, jsonify,
                   redirect, render_template, request, send_file, session,
                   url_for)

import auth
import config
import db
import ephemeris
import history
import history_plot
import mailer
import moonplot
import observability
import observatories
import pipeline
import ranking
import registry
import siteconf
import sites
import skyframes
import skymap
import uncertainty

app = Flask(__name__)
app.secret_key = auth.secret_key()

# A session that expires overnight is a session that expires mid-observation.
# Thirty days, refreshed on each request, so an observer who logged in at the
# start of the season is still logged in at the end of it.
app.permanent_session_lifetime = timedelta(days=30)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,       # JavaScript has no business reading it
    SESSION_COOKIE_SAMESITE="Lax",      # second line of defence behind CSRF
    # Secure would be right for whichneo.juriclab.org and wrong for
    # http://epyc:12600, which is how the dome actually reaches the board --
    # a Secure cookie there is simply never sent, and nobody can log in.
    SESSION_COOKIE_SECURE=bool(config.REQUIRE_HTTPS),
)


def visible_sites():
    """Every observatory this request may switch between, edits applied.

    Resolved once per request: the switcher, the board and the settings page
    all ask, and each ask would otherwise be another pass over the sites
    table.
    """
    cached = getattr(g, "_sites_all", None)
    if cached is not None:
        return cached
    out = config.SITES
    try:
        conn = db.connect()
        try:
            out = registry.all_sites(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        # Before the database exists there are no signed-up sites by
        # definition, so the files are the whole answer rather than a reason
        # to fail the request.
        pass
    g._sites_all = out
    return out


def site_by_code(code):
    """Resolve an observatory code to a configured site, or None."""
    code = (code or "").strip().upper()
    if not code:
        return None
    for s in visible_sites().values():
        if s.obscode.upper() == code:
            return s
    return None


def current_site():
    """The observatory this request is about.

    Answered in order of how explicitly it was asked for:

      1. `?site=<obscode>` in the URL, which is then remembered
      2. what this browser last switched to
      3. where the signed-in account actually works -- the one it owns, or
         the lowest-numbered one it belongs to
      4. the deployment's default, which is the lowest-numbered site

    So a member goes straight to their own observatory, and a stranger sees
    the board this deployment was built for, exactly as before any of this
    existed.
    """
    if not has_request_context():
        return config.DEFAULT_SITE
    cached = getattr(g, "_site_effective", None)
    if cached is not None:
        return cached
    site = _resolve_site()
    g._site_effective = site
    return site


def _resolve_site():
    sites = visible_sites()
    chosen = site_by_code(request.args.get("site"))
    if chosen is not None:
        if session.get("site") != chosen.obscode:
            session["site"] = chosen.obscode
        return chosen
    remembered = site_by_code(session.get("site"))
    if remembered is not None:
        return remembered

    user = auth.current_user()
    if user is not None:
        try:
            conn = db.connect()
            try:
                mine = db.sites_for_user(conn, user["id"])
            finally:
                conn.close()
        except sqlite3.Error:
            mine = []
        # Owned first, then lowest id, so somebody who belongs to several
        # lands somewhere stable until they switch -- and the switch is then
        # remembered for the session.
        for want in (db.OWNER, db.MEMBER):
            for site_id, role in mine:
                if role == want and site_id in sites:
                    return sites[site_id]

    return sites.get(config.DEFAULT_SITE.id, config.DEFAULT_SITE)


def my_site_roles():
    """{site_id: role} for the signed-in account, once per request."""
    cached = getattr(g, "_my_roles", None)
    if cached is not None:
        return cached
    roles = {}
    user = auth.current_user()
    if user is not None:
        try:
            conn = db.connect()
            try:
                roles = dict(db.sites_for_user(conn, user["id"]))
            finally:
                conn.close()
        except sqlite3.Error:
            roles = {}
    g._my_roles = roles
    return roles


def site_groups():
    """The switcher's two groups: where you work, and everything else.

    Grouped rather than listed flat because the list is bounded by the site
    cap rather than by anything about you -- at twenty-five observatories a
    flat list buries the one or two you actually observe from.
    """
    roles = my_site_roles()
    mine, others = [], []
    for s in visible_sites().values():
        row = {"site": s, "role": roles.get(s.id)}
        (mine if s.id in roles else others).append(row)
    return {"mine": mine, "others": others}


def may_edit_settings(conn, site):
    """Whether this request may change this observatory's configuration.

    The owner created the site and configures it; everyone else observes.
    One role boundary, which is all the plan asks for and all that can be
    explained in a sentence.

    A site defined by a file has no owner row -- nobody signed it up -- so it
    falls to the deployment's admins. Without that, the observatory this
    board was built for would be the one nobody could configure.
    """
    user = auth.current_user()
    if user is None:
        return False
    if db.site_role(conn, site.id, user["id"]) == db.OWNER:
        return True
    return bool(user["is_admin"])


# One zone per site, resolved on demand rather than once at import: the board
# shows times as the people standing in that dome read them, and two
# observatories are not in the same timezone.
_TZ_CACHE = {}


def _tz(site=None):
    name = (site or current_site()).display_tz
    if name not in _TZ_CACHE:
        try:
            from zoneinfo import ZoneInfo
            _TZ_CACHE[name] = ZoneInfo(name)
        except Exception:                            # no tzdata on the host
            _TZ_CACHE[name] = timezone.utc
    return _TZ_CACHE[name]


@app.context_processor
def inject_config():
    """Templates read limits and the horizon mask straight from config, and
    the sortable-column registry straight from ranking so the sort bar and
    the sort logic never drift apart."""
    site = current_site()
    telescope = (observatories.lookup(site.obscode) or {}).get("telescope")
    return {"config": config, "site": site, "sites": visible_sites(),
            "site_groups": site_groups(), "site_telescope": telescope,
            "tzname": _tzabbr(), "ranking": ranking,
            "current_user": auth.current_user(),
            "csrf_token": auth.csrf_token,
            "min_password": auth.MIN_PASSWORD,
            "lifetime_phrase": auth.lifetime_phrase}


def _tzabbr(ts=None):
    d = datetime.fromtimestamp(ts if ts is not None else time.time(), _tz())
    return d.strftime("%Z") or current_site().display_tz


@app.template_filter("localt")
def localt(ts):
    """Unix timestamp -> local clock time at the observatory."""
    if ts is None:
        return "—"
    return datetime.fromtimestamp(float(ts), _tz()).strftime("%H:%M")


@app.template_filter("localdt")
def localdt(ts):
    if ts is None:
        return "—"
    return datetime.fromtimestamp(float(ts), _tz()).strftime("%Y-%m-%d %H:%M")


@app.template_filter("sitename")
def sitename(code):
    """Observatory name for a code, for tooltips in the queue."""
    return observatories.site_name(code) or ""


@app.template_filter("utct")
def utct(ts):
    if ts is None:
        return "—"
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%H:%M")


def get_conn():
    conn = db.connect()
    db.init(conn)
    return conn


def viewer_id():
    """Whose observer state this request sees.

    A signed-out visitor gets None, not 0: 0 is the shared basic-auth bucket,
    a real set of marks made by a real person, and a stranger reading the
    board should not inherit them as their own.
    """
    user = auth.current_user()
    return user["id"] if user else None


def writer_id():
    """Whose observer state this request writes. 0 means the shared
    credential -- a write nobody's name is on."""
    user = auth.current_user()
    return user["id"] if user else 0


def load_sorted(conn, show_observed=False, show_hidden=False, mode=None,
                range_filters=None):
    # Both sides of the merge changed this function, for unrelated reasons:
    # main narrowed observer state to the viewing account, this branch added
    # the range filters. Filter before sorting -- sorting a set you are about
    # to discard most of is wasted work, and the compound sort is the
    # expensive half.
    rows = db.load_targets(conn, include_hidden=show_hidden,
                           include_observed=show_observed,
                           user_id=viewer_id(), site=current_site())
    rows = ranking.apply_range_filters(rows, range_filters)
    return ranking.sort_targets(rows, mode, site=current_site())


def status(conn, site=None):
    """How the last cycle went -- for one observatory, not the deployment."""
    site = site or current_site()
    timings = db.get_meta(conn, "last_update_timings", site=site)
    stamp = db.get_meta(conn, "last_update_utc", "never", site=site)
    try:
        ts = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        ts = None
    return {
        "site": site.obscode,
        "site_name": site.name,
        "last_update_utc": stamp,
        "last_update_ts": ts,
        "last_update_local": localt(ts) if ts else "never",
        "count": db.get_meta(conn, "last_update_count", "0", site=site),
        "observable": db.get_meta(conn, "last_update_observable", "0",
                                  site=site),
        "night": db.get_meta(conn, "night", "-", site=site),
        "mismatches": db.get_meta(conn, "crosscheck_mismatches", "0",
                                  site=site),
        "plan_path": db.get_meta(conn, "plan_path", site=site),
        "ok": db.get_meta(conn, "last_update_ok", "0", site=site) == "1",
        "error": db.get_meta(conn, "last_error", site=site),
        "auth": auth.ENABLED,
        "timings": json.loads(timings) if timings else {},
    }


def pick_upcoming(rows, n=3):
    """The cards answer 'what do I point at next', so they must look forward.

    Sorting by peak time alone is right for the night's plan but wrong here:
    midway through the night the earliest-peaking targets have already
    peaked, and a card would advertise a best moment hours in the past.
    Targets still to peak come first; if the night is nearly over, still-
    observable ones that have passed their best top the list up, flagged so
    the card can say so.
    """
    now = time.time()
    # Done targets stay in the table but never headline a card: these answer
    # "what do I point at next", and something already observed is not it.
    observable = [r for r in rows if r["observable"] and not r.get("observed")]
    ahead, behind = [], []
    for r in observable:
        ts = r.get("max_alt_ts")
        (ahead if ts and ts >= now else behind).append(r)
    for r in behind:
        r["past_peak"] = True
    return (ahead + behind)[:n]


def night_strip(rows, max_lanes=16):
    """Geometry for the night timeline: one lane per observable target,
    positioned as a percentage of the observable span.

    Percentages are computed here rather than in the template so the markup
    stays declarative and the page does no arithmetic on render.
    """
    obs = [r for r in rows
           if r["observable"] and r.get("window_start_ts") and r.get("window_end_ts")]
    if not obs:
        return None

    start = min(r["window_start_ts"] for r in obs)
    end = max(r["window_end_ts"] for r in obs)
    span = end - start
    if span <= 0:
        return None

    def pct(ts):
        return max(0.0, min(100.0, (ts - start) / span * 100.0))

    # Hour ticks across the span, labelled in observatory local time.
    ticks = []
    t = start - (start % 3600) + 3600
    while t < end:
        ticks.append({"label": localt(t), "pct": pct(t)})
        t += 3600

    lanes = []
    for i, r in enumerate(obs):
        left = pct(r["window_start_ts"])
        right = pct(r["window_end_ts"])
        lanes.append({
            "desig": r["desig"],
            "left": round(left, 2),
            "width": round(max(right - left, 0.6), 2),
            "peak": round(pct(r["max_alt_ts"]), 2) if r.get("max_alt_ts") else None,
            "alt": r.get("max_alt"),
            # Done lanes are tinted rather than dropped: the strip shows the
            # shape of the whole night, and how much of it is already behind
            # you is part of that shape.
            "observed": bool(r.get("observed")),
            # Rendered but collapsed past max_lanes, rather than left out
            # entirely -- so "show all" is a client-side unhide, no reload.
            "extra": i >= max_lanes,
        })

    now = time.time()
    return {
        "start": localt(start), "end": localt(end),
        "start_ut": utct(start), "end_ut": utct(end),
        "start_ts": start, "end_ts": end,
        "hours": ticks,
        "lanes": lanes,
        "now_pct": round(pct(now), 2) if start <= now <= end else None,
        "hidden": max(0, len(obs) - max_lanes),
    }


def sky_rows(rows):
    """The queue rows the sky map draws, in the order it draws them.

    One expression, shared, because a marker's number is its position in this
    list and the precomputed replay has to number them the same way the live
    map does. Two copies of `if r["observable"]` would be two chances to
    drift.
    """
    return [r for r in rows if r["observable"]]


def sky_view(conn, rows, now=None):
    """The all-sky map, drawn fresh for this instant.

    Deliberately computed per request rather than stored by the updater: a
    fast mover crosses several degrees in the five minutes between cycles, so
    a cached picture would be visibly behind the sky it claims to show.

    The moon is pure astronomy from site and time. Target positions come from
    ephemeris lines already cached, so this makes no network call.
    """
    now = now if now is not None else time.time()
    site = current_site()
    shown = sky_rows(rows)
    tracks = {d: ephemeris.track(lines)
              for d, lines in db.load_tracks(
                  conn, [r["desig"] for r in shown], site).items()}
    marks = skymap.target_marks(shown, tracks, now, site)
    try:
        moon = observability.moon_state(now, site)
    except Exception:
        moon = None                       # never take the board down for this
    return {
        "svg": skymap.render_svg(marks, moon, localt=localt, site=site,
                                 localdt=localdt),
        "moon": moon,
        "up": sum(1 for m in marks if m["up"]),
        "pending": sum(1 for m in marks if not m["up"]),
        "flagged": sum(1 for m in marks if m["up"] and m["mask"]),
        "total": len(shown),
    }


def archived_sky_view(conn, archived, now=None):
    """The all-sky map for a night that has already ended.

    Same rendering pipeline as sky_view(), sourced from night_archive
    instead of the live tables -- those get rewritten every cycle and
    pruned as objects roll off NEOCP, so nothing about a past night
    survives there once the next one starts.

    Takes an already-loaded archive rather than a night label, because the
    caller has to load it anyway to know the night's bounds, and the payload
    is a JSON blob worth parsing once per request rather than twice.

    Green means the same thing here as on the live board, which is why the
    archive stores who observed each target rather than a single flag: to a
    signed-in observer it means "you shot this", and to a signed-out visitor,
    for whom there is no "you", it falls back to "somebody did".
    """
    if archived is None:
        return None
    now = now if now is not None else archived["end_ts"]
    me = auth.current_user()
    rows = []
    for t in archived["targets"]:
        observers = t.get("observers") or []
        mine = (me["username"] in observers) if me else bool(t["observed"])
        rows.append({"desig": t["desig"], "observed": mine,
                     "vmag": t["vmag"], "score": t["score"]})
    tracks = {t["desig"]: ephemeris.track(t["lines"]) for t in archived["targets"]}
    site = current_site()
    marks = skymap.target_marks(rows, tracks, now, site)
    try:
        moon = observability.moon_state(now, site)
    except Exception:
        moon = None                       # never take the board down for this
    return {"svg": skymap.render_svg(marks, moon, localt=localt, site=site,
                                     localdt=localdt),
            "used": now}


# Every metric a viewer can choose to plot for an object's polled history,
# in the order their checkboxes appear. "default" picks what's plotted
# before anyone touches a checkbox -- score and V mag, the two people ask
# about most; the rest are opt-in so the page doesn't open cluttered.
HISTORY_METRICS = [
    # key, label, color, fmt, default_on, y_max
    ("score", "digest2 score", "#f0a63c", "{:.0f}", True, 100),
    ("vmag", "V magnitude", "#5fc9d4", "{:.1f}", True, None),
    ("hmag", "H magnitude", "#f472b6", "{:.1f}", False, None),
    ("nobs", "observations", "#a78bfa", "{:.0f}", False, None),
    ("arc_days", "arc (days)", "#4ade80", "{:.3f}", False, None),
]


def history_charts(hist_rows, keys=None):
    """One trend chart per HISTORY_METRICS entry that actually has enough
    points to plot, for one object's polled history. Shared by the live
    target page, the archived target page, and the history dashboard's
    per-row preview -- an object's history looks the same everywhere, only
    what else surrounds it (and, via `keys`, how much of it) differs. Which
    ones start visible is a front-end (checkbox) concern on the pages that
    offer all of them -- every requested chart is rendered here regardless,
    since drawing an SVG server-side is cheap and toggling is instant only
    if the markup is already on the page.

    keys=None renders every metric; otherwise only HISTORY_METRICS entries
    whose key is in it (order still follows HISTORY_METRICS, not keys)."""
    if len(hist_rows) < 2:
        return []
    charts = []
    for key, label, color, fmt, default_on, y_max in HISTORY_METRICS:
        if keys is not None and key not in keys:
            continue
        pts = [(history.to_unix(r["snapshot_ts"]), r[key]) for r in hist_rows]
        svg = history_plot.line_svg(pts, label, color=color, fmt=fmt, y_max=y_max)
        if svg:
            charts.append({"key": key, "label": label, "svg": svg,
                           "default": default_on})
    return charts


def true_now(conn, desig, now=None):
    """Where the object genuinely is at this instant, or why it is not shown.

    The stored cur_alt/cur_az are taken from the nearest *usable* ephemeris
    row, which during daylight can be hours away -- fine for "where do I point
    next", wrong for "where is it now". This reads the cached ephemeris
    directly so the two questions get two answers.
    """
    now = now if now is not None else time.time()
    lines = db.load_tracks(conn, [desig], current_site()).get(desig)
    track = ephemeris.track(lines) if lines else None
    if not track:
        return None
    nearest = min(track, key=lambda p: abs(p[0] - now))
    if abs(nearest[0] - now) <= skymap._spacing(track):
        az, alt = skymap._interpolate(track, now)
        return {"up": True, "alt": alt, "az": az}
    ahead = [p for p in track if p[0] > now]
    return {"up": False,
            "next_ts": ahead[0][0] if ahead else None,
            "next_alt": ahead[0][2] if ahead else None,
            "next_az": ahead[0][1] if ahead else None}


def _view_args():
    # Targets marked done stay in the list, coloured green, in their original
    # place in the sequence. They used to be filtered out the moment you
    # clicked done, which hid three things at once: the green row, the green
    # marker on the sky map, and the undo button -- so correcting a misclick
    # meant first finding the row again in a separate view.
    return dict(show_observed=True,
                show_hidden=request.args.get("hidden") == "1",
                mode=request.args.get("sort") or current_site().default_sort,
                range_filters=ranking.parse_range_filters(request.args.get("range")))


@app.route("/")
def index():
    v = _view_args()
    conn = get_conn()
    try:
        rows = load_sorted(conn, v["show_observed"], v["show_hidden"], v["mode"], v["range_filters"])
        upcoming = pick_upcoming(rows)
        strip = night_strip(rows)
        archived_nights = db.list_archived_nights(conn, current_site())
        for n in archived_nights:
            n["start_label"] = localt(n["start_ts"])
            n["end_label"] = localt(n["end_ts"])

        # Bounds the replay slider opens with. Tonight's own window when
        # there is one; otherwise the most recently archived night, so the
        # slider is still useful before tonight's targets are up, or before
        # the first update cycle of a fresh night has run at all.
        #
        # `now_ts` is where the handle belongs and how far right it may go.
        # The span is the whole observable night, which for an evening demo
        # is almost entirely still ahead: opening at end_ts put the handle at
        # 05:00 with 97 percent of the track in the future, and scrubbing
        # "back" from there drew hours of empty daylight sky before reaching
        # anything. Clamped into the span because the night's window can
        # close before someone reloads the page, and because a night whose
        # first target is not up yet starts in the future.
        if strip:
            now_ts = min(max(time.time(), strip["start_ts"]), strip["end_ts"])
            replay_default = {"night": "", "start_ts": strip["start_ts"],
                              "end_ts": strip["end_ts"], "now_ts": now_ts,
                              "now_label": localt(now_ts),
                              "start_label": strip["start"], "end_label": strip["end"]}
        elif archived_nights:
            # A finished night is replayable to its last recorded instant --
            # all of it is the past, so there is no future stop to clamp to.
            latest = archived_nights[0]
            replay_default = dict(latest, now_ts=latest["end_ts"],
                                  now_label=latest["end_label"])
        else:
            replay_default = None

        return render_template(
            "index.html", rows=rows, strip=strip,
            upcoming=upcoming, status=status(conn), sky=sky_view(conn, rows),
            archived_nights=archived_nights, replay_default=replay_default,
            max_score=ranking.max_possible_score(current_site()),
            poll_interval=config.WEB_POLL_INTERVAL_S, **v)
    finally:
        conn.close()


# How far either side of now the replay slider will honour a timestamp. A
# night is hours; a year of slack is generous for anything anyone would want
# to replay, and stays far inside what a clock can represent on any platform.
_REPLAY_WINDOW_S = 366 * 24 * 3600


def _replay_ts(raw, window=None):
    """?ts= as a timestamp we can actually render, or None meaning live.

    `window` is (start_ts, end_ts) for an archived night, which knows exactly
    which instants it can draw. Without it the check is "within a year of
    now", which is right for tonight and wrong for an archive: a night older
    than that would have every ?ts= rejected and the slider would silently
    snap to the end with nothing to explain why.

    A value float() accepts is not necessarily one datetime can represent.
    `?ts=99999999999999` parses fine and then raises out of localt() when the
    response header is built -- ValueError: year 3170843 is out of range --
    turning a public, unauthenticated, frequently polled endpoint into a 500.
    `nan` and `inf` slip through float() the same way.

    The slider only ever sends instants inside the night it is showing, so
    anything outside a sane window is a typo or a probe. Fall back to live,
    exactly as a malformed float already did, rather than failing: a wrong
    query string should not be able to take the map down.
    """
    if not raw:
        return None
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ts):
        return None
    if window is not None:
        # An hour of slack each side: the slider's own end stops are the
        # night's bounds, and rounding a step to the minute can land a
        # fraction outside them.
        lo, hi = window[0] - 3600, window[1] + 3600
        if not (lo <= ts <= hi):
            return None
    elif abs(ts - time.time()) > _REPLAY_WINDOW_S:
        return None
    try:                       # the clock itself must accept it
        datetime.fromtimestamp(ts, _tz())
    except (OverflowError, OSError, ValueError):
        return None
    return ts


@app.route("/skymap.svg")
def skymap_svg():
    """Just the map, so the page can refresh it without a full reload.

    An optional ?ts=<unix time> draws the map as it stood at that instant
    instead of live -- the replay slider under "Sky now" uses this to step
    back through a night, reading the same cached ephemeris tracks the live
    map already does. No new data collection required: those tracks already
    span the whole night, not just the instant being shown.

    An optional ?night=<label> instead draws a night that has already ended,
    from night_archive rather than the live tables -- see archive_night() in
    db.py for why that archive exists at all: without it, a past night's
    tracks are gone by the time anyone wants to look back at them.
    """
    v = _view_args()
    night = request.args.get("night") or None
    conn = get_conn()
    try:
        if night:
            archived = db.load_archived_night(conn, night,
                                              current_site())
            if archived is None:
                return f"No archive for night {night}", 404
            # An archived night knows its own bounds, so scrub against those
            # rather than against "near enough to now" -- tighter for a recent
            # night, and the only thing that keeps a year-old one scrubbable
            # at all once it falls outside _REPLAY_WINDOW_S.
            ts = _replay_ts(request.args.get("ts"),
                            window=(archived["start_ts"], archived["end_ts"]))
            # Range filters deliberately do not apply here, and cannot. The
            # archive keeps a target's designation, score, magnitude and
            # track -- not the altitude, motion or moon distance most of the
            # filters range over, and those were true at a moment that has
            # passed. Narrowing a past night by tonight's numbers would be a
            # fiction. The visible consequence is that with a past night
            # selected the table narrows and the map does not; worth closing
            # later by hiding the control for a past night, not by inventing
            # values the archive never held.
            view = archived_sky_view(conn, archived, now=ts)
            used = view["used"]
            svg = view["svg"]
        else:
            ts = _replay_ts(request.args.get("ts"))
            rows = load_sorted(conn, v["show_observed"], v["show_hidden"],
                               v["mode"], v["range_filters"])
            used = ts if ts is not None else time.time()
            svg = sky_view(conn, rows, now=ts)["svg"]
        return (svg, 200,
                {"Content-Type": "image/svg+xml; charset=utf-8",
                 "Cache-Control": "no-store",
                 "X-Sky-Time": f"{localt(used)} {_tzabbr(used)}"})
    finally:
        conn.close()


@app.route("/skyframes.json")
def skyframes_json():
    """The whole night's map in one payload, so the replay never asks again.

    Each entry is the finished SVG of the map's moving layer for one instant
    -- the same bytes /skymap.svg would put inside its dynamic group -- so
    scrubbing and playback are an assignment to one element rather than a
    round trip. See skyframes.py for why the browser is given finished
    drawing rather than positions to interpret.

    Answers 200 with {"available": false} rather than an error whenever it
    cannot serve a night it is sure about. Those are ordinary states, not
    faults: a fresh database, an afternoon with nothing observable yet, a
    night the updater has not reached, a target that joined the queue after
    the last cycle. The page treats any of them as "keep doing what you did
    before" and goes on rendering frames on demand, which is the behaviour
    this route is an optimisation of rather than a replacement for.

    Deliberately tonight only. An archived night comes out of night_archive,
    whose payload the updater never revisits, so there is nothing precomputed
    to serve and ?night= stays on the server-rendered path.
    """
    v = _view_args()
    site = current_site()
    conn = get_conn()
    try:
        blob = db.load_skyframes(conn, site)
        if blob is None:
            return _no_skyframes("no precomputed night for this site")
        if blob.get("night") != pipeline.night_label(time.time(), site):
            # Built for a night that has rolled over. The frames are still
            # internally consistent, they are simply not tonight's, and the
            # slider's stops are.
            return _no_skyframes("precomputed night is not tonight")
        shown = sky_rows(load_sorted(conn, v["show_observed"],
                                     v["show_hidden"], v["mode"],
                                     v["range_filters"]))
        if not skyframes.covers(blob, [r["desig"] for r in shown]):
            return _no_skyframes("a visible target is not in the payload")
        body = json.dumps({
            "available": True,
            "step": blob["step"],
            "grid": blob["grid"],
            "frames": skyframes.frames(blob, shown, site, localt=localt,
                                       localdt=localdt),
            "built_utc": blob["built_utc"],
            "night": blob["night"],
        }, separators=(",", ":"))
    finally:
        conn.close()
    return _json_maybe_gzipped(body)


def _no_skyframes(reason):
    """Why the fast path is off, as data rather than as an error.

    The reason is for whoever is looking at why a demo is not smooth; the
    page only reads `available`.
    """
    return _json_maybe_gzipped(
        json.dumps({"available": False, "reason": reason}))


def _json_maybe_gzipped(body):
    """JSON, compressed when the client said it could take it.

    A night of frames is a few hundred kilobytes of extremely repetitive
    markup -- measured 544 KB down to 30 KB for the live board's night --
    and waitress does not compress for us. Sent once per page load, so the
    difference is the whole cost of the feature over the wire.
    """
    raw = body.encode()
    headers = {"Content-Type": "application/json; charset=utf-8",
               "Cache-Control": "no-store"}
    if "gzip" in request.headers.get("Accept-Encoding", ""):
        raw = gzip.compress(raw, 6)
        headers["Content-Encoding"] = "gzip"
    headers["Content-Length"] = str(len(raw))
    return raw, 200, headers


@app.route("/rows")
def rows_partial():
    """Server-rendered tbody, polled by the page so refreshes stay cheap."""
    v = _view_args()
    conn = get_conn()
    try:
        return render_template(
            "_rows.html",
            rows=load_sorted(conn, v["show_observed"], v["show_hidden"], v["mode"], v["range_filters"]),
            max_score=ranking.max_possible_score(current_site()), **v)
    finally:
        conn.close()


@app.route("/status")
def status_json():
    conn = get_conn()
    try:
        return jsonify(status(conn))
    finally:
        conn.close()


@app.route("/api/targets")
def api_targets():
    conn = get_conn()
    try:
        rows = load_sorted(conn, True, True)
        for r in rows:
            r.pop("plan_block", None)
        return jsonify({"status": status(conn), "targets": rows})
    finally:
        conn.close()


@app.route("/sites")
def sites_list():
    """Every observatory this deployment serves, and your place in each."""
    conn = get_conn()
    try:
        user = auth.current_user()
        roles = dict(db.sites_for_user(conn, user["id"])) if user else {}
        rows = [{"site": s, "role": roles.get(sid),
                 "members": db.site_members(conn, sid)}
                for sid, s in registry.all_sites(conn).items()]
        return render_template(
            "sites.html", rows=rows, cap=config.ACTIVE_SITE_CAP,
            active=registry.active_count(conn),
            at_capacity=registry.at_capacity(conn))
    finally:
        conn.close()


def _render_new_site(errors=None, form=None, status_code=200):
    conn = get_conn()
    try:
        page = render_template(
            "site_new.html", errors=errors or [], form=form or {},
            cap=config.ACTIVE_SITE_CAP, active=registry.active_count(conn),
            at_capacity=registry.at_capacity(conn),
            named_account=auth.current_user() is not None)
    finally:
        conn.close()
    return (page, status_code) if status_code != 200 else page


@app.get("/sites/new")
def site_new_form():
    """Add an observatory. Reading the form is open; creating needs an
    account, and the page says so rather than hiding behind a login wall."""
    return _render_new_site()


@app.get("/sites/lookup")
def site_lookup():
    """What MPC knows about an observatory code, for the form to fill in.

    The position comes from MPC's own table rather than from anything typed:
    it is the position every ephemeris will be computed for, so a typo here
    would not be a cosmetic error but a board describing a different patch of
    sky.
    """
    code = (request.args.get("obscode") or "").strip().upper()
    where = observatories.geometry(code)
    if not where:
        return jsonify({"known": False, "obscode": code})
    info = observatories.lookup(code) or {}
    return jsonify({
        "known": True, "obscode": code,
        "name": info.get("name") or "",
        "telescope": info.get("telescope") or "",
        "rollover_hour_ut": sites.rollover_hour_for_longitude(
            where["lon_deg"]),
        **where,
    })


@app.post("/sites/new")
@auth.required
def site_create():
    """Register an observatory. Immediate, and capped.

    An observatory code is not optional and cannot be invented: MPC computes
    an ephemeris for a code, so a site without one has no ephemeris and
    therefore no board. The code is checked against MPC's own table, which is
    also where its position comes from.
    """
    if auth.current_user() is None:
        return _render_new_site(
            errors=["Adding an observatory needs a named account, so that "
                    "somebody owns it and can configure it afterwards."],
            form=request.form, status_code=403)

    form = request.form
    code = (form.get("obscode") or "").strip().upper()
    name = " ".join((form.get("name") or "").split())
    tzname = (form.get("display_tz") or "").strip()

    conn = get_conn()
    try:
        errors = []
        where = observatories.geometry(code)
        if not code:
            errors.append("Give the observatory code MPC assigned you.")
        elif not where:
            errors.append(
                f"MPC's table has no position for “{code}”. An ephemeris is "
                "computed for an observatory code, so a site without one "
                "cannot be served. If the code is new, the table in mpcdata/ "
                "may simply predate it.")
        elif site_by_code(code) is not None or db.site_by_obscode(conn, code):
            errors.append(f"{code} is already on this deployment.")

        if not name:
            name = observatories.site_name(code) or code
        if len(name) > 80:
            errors.append("Keep the observatory's name under 80 characters.")

        try:
            tzname = siteconf._timezone(
                siteconf.Spec("display_tz", "Timezone", "tz", "night"),
                tzname)
        except siteconf.SettingsError as e:
            errors.append(str(e))

        if registry.at_capacity(conn):
            errors.append(
                f"This deployment is serving its limit of "
                f"{config.ACTIVE_SITE_CAP} observatories. The cap is what "
                "keeps its load on MPC to a few requests a minute; ask the "
                "operator before it is raised.")

        if errors:
            return _render_new_site(errors=errors, form=form,
                                    status_code=400)

        site_id = registry.next_id(conn)
        definition = sites.new_definition(code, name, tzname, site_id, where)
        # Built through the same loader every file goes through, so a
        # definition that would not load is refused here rather than
        # discovered later by the update cycle.
        sites.from_dict(definition, source=f"new site {code}")
        db.create_site(conn, site_id, code,
                       json.dumps(definition, sort_keys=True),
                       auth.current_user()["id"])
        siteconf.forget(site_id)
        session["site"] = code
        return redirect(url_for("settings", created=code))
    finally:
        conn.close()


@app.route("/settings")
def settings():
    """Everything this observatory is configured to do.

    Reading is open, like the rest of the board: somebody at a dome screen
    should be able to check a limit without signing in. Changing anything
    needs a named account -- see settings_save.
    """
    return _render_settings()


def _settings_groups():
    """The editable scalars, in the order the page shows them."""
    out = {}
    for spec in siteconf.SCALARS:
        out.setdefault(spec.group, []).append(spec)
    return out


def _render_settings(errors=None, form=None, saved=False, status_code=200):
    site = current_site()
    conn = get_conn()
    try:
        base = registry.base_site(conn, site.id) or site
        edited = db.site_settings_rows(conn, site.id)
        # Decided while the connection is still open. Reading it out of the
        # render call below put the query after the `finally` that closes it.
        can_edit = may_edit_settings(conn, site)
    finally:
        conn.close()
    page = render_template(
        "settings.html", site=site, file_site=base, edited=edited,
        groups=_settings_groups(), siteconf=siteconf,
        errors=errors or [], form=form or {}, saved=saved,
        can_edit=can_edit,
        telescope=(observatories.lookup(site.obscode) or {}).get("telescope"),
        # The mask and the wedges as shapes rather than as numbers. A wedge
        # whose arc runs the wrong way round the sky is obvious here and
        # nearly invisible in a table -- which is why this same drawing is
        # what the form redraws as the values are typed.
        sky_svg=skymap.render_svg([], None, site=site))
    return (page, status_code) if status_code != 200 else page


def _proposed_site(form, site):
    """The site the submitted form describes. Raises SettingsError."""
    values = {}
    for spec in siteconf.SCALARS:
        got = siteconf.scalar_from_form(spec, form)
        if got is not None:
            values[spec.name] = got
    values[siteconf.MAX_ALTITUDE] = siteconf.max_altitude_from_form(form)
    values["rank_weights"] = siteconf.rank_weights_from_form(form)
    values["horizon_mask"] = siteconf.horizon_mask_from_form(
        form, site.horizon_mask)
    values["keepout_wedges"] = siteconf.keepout_wedges_from_form(form)
    return values


@app.get("/settings/preview.svg")
def settings_preview():
    """The sky map as the form currently describes it, before anything saves.

    Rendered here rather than redrawn in JavaScript so that the preview and
    the board come from the same code: a preview drawn by a second
    implementation is a preview that can disagree with what actually gets
    enforced, which is the one thing it must never do.
    """
    site = current_site()
    try:
        values = _proposed_site(request.args, site)
        preview = dataclasses.replace(
            site, horizon_mask=values["horizon_mask"],
            keepout_wedges=values["keepout_wedges"],
            max_altitude=values[siteconf.MAX_ALTITUDE])
        svg = skymap.render_svg([], None, site=preview)
    except siteconf.SettingsError as e:
        svg = (f'<svg viewBox="0 0 {config.SKYMAP_SIZE} 60" width="100%" '
               f'xmlns="http://www.w3.org/2000/svg">'
               f'<text x="8" y="24" fill="#cf6154" font-size="12" '
               f'font-family="monospace">Cannot draw this yet:</text>'
               f'<text x="8" y="42" fill="#cf6154" font-size="11" '
               f'font-family="monospace">{escape(str(e))}</text></svg>')
    return Response(svg, mimetype="image/svg+xml",
                    headers={"Cache-Control": "no-store"})


@app.post("/settings")
@auth.required
def settings_save():
    """Apply edited settings, or put one back the way the file has it.

    Deliberately stricter than the rest of the board's writes: marking a
    target observed may be done with the shared credential, which names
    nobody, but a change to where a telescope may point should have a person
    attached to it.
    """
    site = current_site()
    conn = get_conn()
    try:
        if not may_edit_settings(conn, site):
            return _render_settings(
                errors=[f"Only {site.obscode}'s owner can change its "
                        "settings. Everyone else marks targets and reads the "
                        "board. If you work here, ask the owner to add you."],
                form=request.form, status_code=403)
        base = registry.base_site(conn, site.id) or site
        uid = auth.current_user()["id"]
        # The restore buttons sit inside the same form as everything else --
        # a nested form is not valid HTML -- so which field to restore rides
        # in the button's own value.
        action = request.form.get("action", "save")
        if action.startswith("revert:"):
            field = action.split(":", 1)[1]
            if field not in siteconf.EDITABLE:
                return _render_settings(errors=[f"Nothing called {field!r} "
                                                "can be reverted."],
                                        status_code=400)
            db.clear_site_override(conn, site.id, field)
            siteconf.forget(site.id)
            if field == "mpc_server_min_alt":
                db.prune_cache(conn, [], site)
            return redirect(url_for("settings", reverted=field))

        try:
            values = _proposed_site(request.form, site)
        except siteconf.SettingsError as e:
            return _render_settings(errors=[str(e)], form=request.form,
                                    status_code=400)

        changed = {f: v for f, v in values.items()
                   if not siteconf.same(v, getattr(site, f))}

        # The one setting where a typo points a telescope at a wall. Typing
        # the observatory code is the pattern GitHub uses for deleting a
        # repository: it makes an accidental save nearly impossible without
        # inventing a second permissions system on top of the first.
        if "keepout_wedges" in changed:
            typed = (request.form.get("confirm_obscode") or "").strip()
            if typed.upper() != site.obscode.upper():
                return _render_settings(
                    errors=["The keep-out wedges remove sky outright, so "
                            f"saving a change to them needs the observatory "
                            f"code typed exactly: {site.obscode}."],
                    form=request.form, status_code=400)

        for field, value in changed.items():
            db.save_site_override(
                conn, site.id, field, siteconf.dumps(value),
                siteconf.dumps(getattr(base, field)), uid)

        # MPC was asked for nothing below the old floor, so every cached
        # ephemeris is missing exactly the rows a lower floor is asking for.
        # Dropping this site's cache costs one cycle of refetching and is the
        # only way the new floor means anything.
        if "mpc_server_min_alt" in changed:
            db.prune_cache(conn, [], site)

        siteconf.forget(site.id)
        if not changed:
            return redirect(url_for("settings", unchanged=1))
        return redirect(url_for("settings", saved=len(changed)))
    finally:
        conn.close()


_PLAN_NAME_OK = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def _plan_download_name(path):
    """A filename for a saved copy of `path`, safe to put in a header.

    The basename is already the useful half of the name -- it is the night,
    `2026-09-19.txt` -- and the prefix keeps a Downloads folder legible when
    the observer has files from elsewhere in it.

    Characters outside the allowlist are dropped rather than escaped. This
    string goes into a response header, and the path it comes from is a
    database value, not a constant: one containing a quote or a newline
    would otherwise end the header and start whatever came after it. The
    backslash split is not decoration either -- at Ticvan the board runs on
    Windows, so `plan_path` is a backslash path and splitting on "/" alone
    would hand the whole of `C:\\whichNEO\\plans\\2026-09-19.txt` to the header.
    """
    base = (path or "").replace("\\", "/").rsplit("/", 1)[-1]
    safe = "".join(c for c in base if c in _PLAN_NAME_OK)
    stem, dot, ext = safe.rpartition(".")
    if not dot:
        stem, ext = safe, "txt"
    # The stem has to survive as something, or the name is not a name. A
    # plan written under a non-Latin locale can sanitise down to a bare
    # `.txt`, which is a hidden file rather than tonight's plan.
    if not stem:
        stem = "plan"
    return "whichneo-%s.%s" % (stem, ext or "txt")


@app.route("/plan")
def plan_text():
    """The nightly plan file, exactly as written to disk.

    `?download=1` changes nothing about the body -- only whether the browser
    offers to save it instead of displaying it. The bare URL stays byte-exact
    and header-stable because it is what a control room's `curl`/`wget` loop
    is pointed at; see README, "Keeping a copy of the plan on disk".
    """
    conn = get_conn()
    try:
        path = db.get_meta(conn, "plan_path", site=current_site())
    finally:
        conn.close()
    if not path:
        return "No plan written yet.", 404
    try:
        with open(path) as f:
            body = f.read()
    except OSError as e:
        return f"Could not read {path}: {e}", 500
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    if request.args.get("download"):
        headers["Content-Disposition"] = (
            'attachment; filename="%s"' % _plan_download_name(path))
    return body, 200, headers


def _safe_next(raw):
    """Where to go after logging in.

    Only a path on this site. A `next` taken from the query string and handed
    straight to redirect() is an open redirect: a link to our own login page
    that lands the observer on somebody else's, wearing our URL as cover.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return url_for("index")
    return raw


@app.route("/login", methods=["GET", "POST"])
def login():
    nxt = _safe_next(request.values.get("next"))
    if auth.current_user():
        return redirect(nxt)

    if not auth.HASHING_AVAILABLE:
        return render_template("login.html", error=auth.NO_HASHING,
                               username="", next=nxt), 503

    error = None
    username = (request.form.get("username") or "").strip()
    if request.method == "POST":
        if auth.throttled():
            error = ("Too many failed attempts from this address. "
                     "Wait a few minutes and try again.")
        else:
            conn = get_conn()
            try:
                user = db.user_by_name(conn, username)
                ok, rehashed = (False, None)
                if user:
                    ok, rehashed = auth.verify_password(
                        user["password_hash"], request.form.get("password") or "")
                if ok:
                    if rehashed:
                        db.update_password(conn, user["id"], rehashed)
                    db.touch_login(conn, user["id"])
                    auth.start_session(user)
                    auth.clear_failures()
                    return redirect(nxt)
            finally:
                conn.close()
            auth.note_failure()
            # One message for both halves. "No such user" tells a stranger
            # which names exist, which is the first half of a password guess.
            error = "That username and password do not match an account."

    return render_template("login.html", error=error, username=username,
                           next=nxt), (200 if error is None else 401)


@app.route("/register", methods=["GET", "POST"])
def register():
    nxt = _safe_next(request.values.get("next"))
    if not config.ALLOW_SIGNUP:
        return render_template("login.html", error="Sign-up is closed.",
                               username="", next=nxt), 403
    if not auth.HASHING_AVAILABLE:
        return render_template("login.html", error=auth.NO_HASHING,
                               username="", next=nxt), 503
    if auth.current_user():
        return redirect(nxt)

    error = None
    username = (request.form.get("username") or "").strip()
    email = (request.form.get("email") or "").strip()
    if request.method == "POST":
        password = request.form.get("password") or ""
        error = (auth.username_error(username)
                 or auth.password_error(password,
                                        request.form.get("confirm")))
        if error is None:
            conn = get_conn()
            try:
                uid = db.create_user(conn, username,
                                     auth.hash_password(password), email)
                auth.start_session(db.user_by_id(conn, uid))
                return redirect(nxt)
            except sqlite3.IntegrityError:
                error = ("That username or email is already taken."
                         if email else "That username is already taken.")
            finally:
                conn.close()

    return render_template("register.html", error=error, username=username,
                           email=email, next=nxt), (200 if error is None else 400)


RESET_SUBJECT = "WhichNEO password reset"

RESET_BODY = """\
Someone asked to reset the password for the WhichNEO account "{username}"
at {site}.

Open this link within the next {hours} to choose a new one:

    {link}

The link works once. Using it, or letting it expire, makes it useless.

If this was not you, nothing has happened to your account and you can ignore
this message -- but tell whoever runs the board, because it means somebody
knows the account exists.

-- WhichNEO, L01 Tican Station, Visnjan Observatory
"""


@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    """Ask for a reset link.

    This route answers identically whether or not the account exists: same
    page, same wording, same timing (the send is threaded). Anything else
    turns the form into an account-enumeration oracle, which on a board with
    open sign-up is the one piece of information an attacker cannot get any
    other way.
    """
    if not mailer.available():
        return render_template(
            "forgot.html", sent=False,
            error="This board cannot send email yet, so there is no reset "
                  "link. Ask an admin to run `manage.py passwd` for you."), 503

    if request.method == "POST":
        needle = (request.form.get("who") or "").strip()
        conn = get_conn()
        try:
            user = db.user_by_name_or_email(conn, needle)
            if user and user["email"]:
                link = (config.SITE_URL.rstrip("/")
                        + url_for("reset", token=auth.reset_token(user)))
                mailer.send(user["email"], RESET_SUBJECT, RESET_BODY.format(
                    username=user["username"], site=config.SITE_URL, link=link,
                    hours=auth.lifetime_phrase("reset")))
            # No else. An account with no email on file, an account that does
            # not exist, and a successful send all end here the same way.
        finally:
            conn.close()
        return render_template("forgot.html", sent=True)

    return render_template("forgot.html", sent=False)


@app.route("/reset/<token>", methods=["GET", "POST"])
def reset(token):
    conn = get_conn()
    try:
        user = auth.reset_token_user(conn, token)
        if not user:
            return render_template(
                "reset.html", user=None,
                error="This reset link is expired, already used, or not "
                      "valid. Ask for a new one."), 400

        error = None
        if request.method == "POST":
            password = request.form.get("password") or ""
            error = auth.password_error(password, request.form.get("confirm"))
            if error is None:
                db.update_password(conn, user["id"],
                                   auth.hash_password(password))
                db.touch_login(conn, user["id"])
                # Signed in straight away: the person holding this link has
                # just proved they control the address on file, which is the
                # same proof the login form asks for.
                auth.start_session(db.user_by_id(conn, user["id"]))
                return redirect(url_for("index"))
        return render_template("reset.html", user=user, error=error), (
            200 if error is None else 400)
    finally:
        conn.close()


@app.post("/logout")
def logout():
    auth.end_session()
    return redirect(url_for("index"))


@app.post("/mark/<desig>")
@auth.required
def mark(desig):
    action = request.form.get("action", "observed")
    uid = writer_id()
    conn = get_conn()
    try:
        if action == "observed":
            db.set_state(conn, desig, uid, current_site(), observed=1,
                         observed_at_utc=db.utcnow())
        elif action == "unobserved":
            db.set_state(conn, desig, uid, current_site(),
                         observed=0, observed_at_utc=None)
        elif action == "hide":
            db.set_state(conn, desig, uid, current_site(), hidden=1)
        elif action == "restore":
            db.set_state(conn, desig, uid, current_site(), hidden=0)
        # "up" and "down" are deliberately gone rather than left accepting a
        # request nothing can send: the arrows that produced them are removed,
        # and priority_bump is no longer read by the sort or the score. An
        # endpoint that still writes a field nobody reads is how a value like
        # C46JQC1's +5 ends up frozen into an ordering with no way to see it.
        # The observer_state column stays -- that table also holds observed and
        # hidden, and is not worth recreating to drop one unused field.
    finally:
        conn.close()
    return redirect(request.referrer or url_for("index"))


@app.route("/target/<desig>")
def target_detail(desig):
    conn = get_conn()
    try:
        rows = [r for r in db.load_targets(conn, True, True,
                                           user_id=viewer_id(),
                                           site=current_site())
                if r["desig"] == desig]
        if not rows:
            # Gone from the live board -- update_neocp.py rewrites `targets`
            # to just the current NEOCP list every cycle, so a resolved
            # object simply isn't there any more. Its polled history is a
            # separate, append-only database and never loses it -- show
            # what that still has rather than a bare 404, which is exactly
            # the data loss this exists to avoid.
            hconn = history.connect()
            try:
                obj = history.get_object(hconn, desig)
                hist_rows = history.object_history(hconn, desig) if obj else []
            finally:
                hconn.close()
            if not obj:
                return f"Unknown target {desig}", 404
            hist_charts = history_charts(hist_rows)
            return render_template(
                "target_archived.html", obj=obj,
                hist_rows=hist_rows, hist_charts=hist_charts)
        # Drawn from points/rows already cached, so the page makes no network call.
        pts = db.load_offsets(conn, desig, current_site())
        cov = uncertainty.coverage(pts, site=current_site()) if pts else None

        # Same cached lines the sky map reads; full Row objects this time,
        # because the altitude plot needs moon_alt and sun_alt per row, not
        # just the position triple track() returns. Merged with the wider
        # (no altitude floor) fetch scoped only to this plot -- see
        # ephemeris.fetch_gap_fill -- by timestamp, so a hole in the normal
        # feed is patched wherever the wider one actually covers it.
        eph_lines = db.load_tracks(conn, [desig],
                                   current_site()).get(desig)
        gap_fill_lines = db.load_gap_fill_lines(conn, desig,
                                                current_site())
        primary_rows = ephemeris.from_lines(desig, eph_lines or []).rows
        filler_rows = ephemeris.from_lines(desig, gap_fill_lines or []).rows
        seen_ts = {r.ts for r in primary_rows}
        combined_rows = sorted(
            primary_rows + [r for r in filler_rows if r.ts not in seen_ts],
            key=lambda r: r.ts)

        # The Moon needs no orbit fit -- its position is exactly knowable for
        # any instant -- so its curve is computed independently across the
        # whole span rather than only wherever the object has a row, and
        # stays gap-free even when combined_rows above still has a real hole.
        moon_track = None
        if len(combined_rows) >= 2:
            t0, t1 = combined_rows[0].ts, combined_rows[-1].ts
            step_s = 900.0   # 15 min, matching MPC's usual cadence
            n = max(2, int((t1 - t0) / step_s) + 1)
            sample_ts = [t0 + (t1 - t0) * i / (n - 1) for i in range(n)]
            try:
                moon_track = observability.moon_altitudes(sample_ts,
                                                          current_site())
            except Exception:
                moon_track = None   # never take the page down for this

        moon_svg = moonplot.render_svg(
            combined_rows, rows[0]["window_start_ts"], rows[0]["window_end_ts"],
            localt=localt, tzlabel=_tzabbr(), moon_track=moon_track,
            site=current_site()
        ) if len(combined_rows) >= 2 else None

        hconn = history.connect()
        try:
            hist_rows = history.object_history(hconn, desig)
        finally:
            hconn.close()
        hist_charts = history_charts(hist_rows)

        return render_template(
            "target.html", row=rows[0],
            unc_svg=(uncertainty.render_svg(pts, site=current_site())
                     if pts else None),
            unc_points=len(pts) if pts else 0,
            unc_distinct=uncertainty.distinct(pts) if pts else 0,
            unc_coverage=cov,
            unc_extent=uncertainty.extent(pts) if pts else None,
            moon_svg=moon_svg,
            fov=current_site().fov_arcsec,
            now_pos=true_now(conn, desig),
            hist_rows=hist_rows, hist_charts=hist_charts,
            discovery_site=observatories.lookup(rows[0].get("discovery_code")),
            other_sites=[observatories.lookup(c)
                         for c in sorted((rows[0].get("obs_codes") or {}),
                                         key=lambda c: -rows[0]["obs_codes"][c])
                         if c != rows[0].get("discovery_code")])
    finally:
        conn.close()


def _history_query(conn, sort, asc, q):
    """Shared by history_view (first paint) and history_rows_partial (every
    later filter/sort change): apply q's filters, falling back to the
    unfiltered list rather than an empty page if it doesn't parse -- a typo
    in a bookmarked/hand-edited URL shouldn't make it look like there's no
    data at all."""
    try:
        where_sql, params = history.parse_filters(q)
        filter_error = None
    except ValueError as e:
        where_sql, params, filter_error = "", [], str(e)
    objects = history.list_objects(conn, sort, asc, where_sql, params)
    return objects, filter_error


@app.route("/history")
def history_view():
    """Read-only dashboard over neocp_history.py's database. That script
    owns writing to it (a separate background loop -- see its own
    docstring); this route only reads."""
    sort = request.args.get("sort")
    asc = request.args.get("dir") != "desc"
    q = request.args.get("q", "")
    conn = history.connect()
    try:
        objects, filter_error = _history_query(conn, sort, asc, q)
        try:
            initial_filters = [{"field": f, "op": o, "value": v}
                               for f, o, v in history.split_filter_tokens(q)]
        except ValueError:
            initial_filters = []
        return render_template(
            "history.html",
            summary=history.summary(conn),
            objects=objects,
            mode=sort, asc=asc, q=q, filter_error=filter_error,
            filter_fields=history.FILTER_FIELDS,
            initial_filters=initial_filters)
    finally:
        conn.close()


@app.route("/history/rows")
def history_rows_partial():
    """Server-rendered history table body, fetched by the dashboard's
    filter-builder and sortable headers so changing either updates the
    table instantly with no page reload."""
    sort = request.args.get("sort")
    asc = request.args.get("dir") != "desc"
    q = request.args.get("q", "")
    conn = history.connect()
    try:
        objects, _ = _history_query(conn, sort, asc, q)
        return render_template("_history_rows.html", objects=objects, q=q)
    finally:
        conn.close()


@app.route("/history/suggest")
def history_suggest():
    """Autocomplete for the filter bar's value field -- currently just
    designations, the one free-text field where suggesting real values
    (rather than making someone recall an exact desig) actually helps.
    Everything else the filter bar can suggest (field names, operators,
    status values) comes from FILTER_FIELDS client-side with no request."""
    field = request.args.get("field", "")
    prefix = request.args.get("q", "").strip()
    if field != "desig" or not prefix:
        return jsonify([])
    conn = history.connect()
    try:
        rows = conn.execute(
            "SELECT desig FROM objects WHERE desig LIKE ? ORDER BY desig LIMIT 8",
            (f"{prefix}%",)).fetchall()
    finally:
        conn.close()
    return jsonify([r["desig"] for r in rows])


@app.route("/history/charts/<desig>")
def history_charts_fragment(desig):
    """digest2/V-mag charts for one object's row on the history dashboard,
    rendered only when that row's dropdown is actually opened -- rendering
    every row's charts up front (as this used to do) made /history slow to
    load with dozens of objects on it for charts most visits never open."""
    conn = history.connect()
    try:
        hist_rows = history.object_history(conn, desig)
    finally:
        conn.close()
    charts = history_charts(hist_rows, keys=("score", "vmag"))
    return render_template("_history_mini_charts.html", charts=charts)


@app.route("/history/download.db")
@auth.signed_in
def history_download_db():
    """The raw neocp_history.py database, exactly as stored -- full
    fidelity, no export step, and every relation (objects to their
    snapshots) intact for anyone who wants to query it directly.

    Behind a login, unlike the dashboard and the CSV. Reading the board is
    open and should stay open, but this is not a page: one row per object per
    five minutes grows to gigabytes, and an unauthenticated endpoint handing
    out the whole file on demand is a bandwidth commitment rather than a
    read. Nothing in here is secret -- it is all public MPC data -- so this
    is about cost, not confidentiality, and it is one decorator to reverse.
    """
    return send_file(history.DB_PATH, as_attachment=True,
                     download_name="neocp_history.db",
                     mimetype="application/x-sqlite3")


@app.route("/history/download.csv")
def history_download_csv():
    """Same data as history_download_db, flattened to one row per snapshot
    for anyone who'd rather open it in a spreadsheet than a SQL client --
    each object's eventual outcome is repeated on every one of its snapshot
    rows rather than kept in a second file, so nothing needs joining back
    together by hand."""
    conn = history.connect()
    try:
        rows = conn.execute("""
            SELECT s.desig, s.snapshot_ts, s.score, s.ra_deg, s.dec_deg,
                   s.vmag, s.nobs, s.arc_days, s.hmag, s.not_seen_days,
                   s.update_note, s.note_flag, s.is_new,
                   o.first_seen_ts, o.last_seen_ts, o.status, o.resolved_at
            FROM snapshots s JOIN objects o ON o.desig = s.desig
            ORDER BY s.desig, s.snapshot_ts
        """).fetchall()
    finally:
        conn.close()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["desig", "snapshot_ts", "score", "ra_deg", "dec_deg", "vmag",
                "nobs", "arc_days", "hmag", "not_seen_days", "update_note",
                "note_flag", "is_new", "first_seen_ts", "last_seen_ts",
                "status", "resolved_at"])
    w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=neocp_history.csv"})


# --- feedback ----------------------------------------------------------------
#
# A public form that causes an email to be sent is an open relay unless it is
# nailed down, so four things are fixed and none of them come from the sender:
# the recipient (config.FEEDBACK_TO), the subject, the length, and the rate.
# The sender's text reaches the body only -- never a header, where a newline
# would let them add recipients of their own.

FEEDBACK_SUBJECT = "whichNEO feedback — {obscode} {name}"

FEEDBACK_BODY = """{who} sent this from {page}

Observatory: {obscode} {name}
Reply to:    {reply_to}

----------------------------------------------------------------------
{message}
----------------------------------------------------------------------

Stored as feedback #{fid}. Read them all at {site_url}/feedback
"""

_feedback_hits = {}


def _feedback_key():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "?")


def _feedback_throttled():
    cutoff = time.time() - config.FEEDBACK_WINDOW_S
    hits = [t for t in _feedback_hits.get(_feedback_key(), []) if t > cutoff]
    return len(hits) >= config.FEEDBACK_LIMIT


def _note_feedback():
    key = _feedback_key()
    cutoff = time.time() - config.FEEDBACK_WINDOW_S
    hits = [t for t in _feedback_hits.get(key, []) if t > cutoff]
    _feedback_hits[key] = hits + [time.time()]
    if len(_feedback_hits) > 512:          # same opportunistic sweep as auth
        for k in [k for k, v in _feedback_hits.items()
                  if not any(t > cutoff for t in v)]:
            _feedback_hits.pop(k, None)


def _usable_email(addr):
    """Good enough to reply to, and safe to put in a header.

    Deliberately not a full RFC check: the point is to catch a typo and to
    refuse anything carrying a newline, not to adjudicate exotic addresses.
    """
    if not addr or len(addr) > 254:
        return False
    if any(c in addr for c in "\r\n\t ,;<>"):
        return False
    local, _, domain = addr.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".")


@app.route("/feedback", methods=["GET", "POST"])
def feedback():
    """Take a suggestion, store it, then try to mail it."""
    if request.method == "GET":
        user = auth.current_user()
        if user is None:
            return redirect(url_for("login", next=url_for("feedback")))
        if not user["is_admin"]:
            # Not 403-with-a-list: someone who cannot read these should not
            # learn how many there are.
            abort(403)
        conn = get_conn()
        try:
            rows = db.recent_feedback(conn)
            by_id = {s.id: s for s in registry.all_sites(conn).values()}
        finally:
            conn.close()
        for r in rows:
            site = by_id.get(r["site_id"])
            r["obscode"] = site.obscode if site else None
        return render_template("feedback.html", rows=rows)

    # --- POST ---
    if not auth.csrf_ok():
        abort(400)

    # An empty field a person never sees. A bot that fills every input gets a
    # cheerful thank-you and goes no further, which teaches it nothing.
    if (request.form.get("website") or "").strip():
        return render_template("feedback_sent.html", sent=True)

    message = (request.form.get("message") or "").strip()
    reply_to = (request.form.get("reply_to") or "").strip()
    page = (request.form.get("page") or "").strip()[:200]
    user = auth.current_user()

    def again(error):
        return render_template("feedback_sent.html", sent=False,
                               error=error, message=message,
                               reply_to=reply_to, page=page), 400

    if not message:
        return again("There was nothing in the message.")
    if len(message) > config.FEEDBACK_MAX_CHARS:
        return again(f"That is longer than {config.FEEDBACK_MAX_CHARS} "
                     "characters. Please send the short version.")
    if user is None and not _usable_email(reply_to):
        return again("Please leave an email address so there is a way to "
                     "reply, or sign in first.")
    if _feedback_throttled():
        return render_template(
            "feedback_sent.html", sent=False,
            error="That is several messages in a short time. Please wait a "
                  "little before sending another."), 429

    # The account's own address wins over anything typed: we already know who
    # a signed-in observer is, and a form field is not evidence.
    if user is not None:
        reply_to = user["email"] or None
    site = current_site()

    conn = get_conn()
    try:
        fid = db.record_feedback(
            conn, message, site_id=site.id,
            user_id=user["id"] if user else None,
            reply_to=reply_to, page=page)
    finally:
        conn.close()
    _note_feedback()

    def delivered(error):
        # Its own connection: this runs on the sending thread, after the
        # request's connection has been closed.
        c = db.connect()
        try:
            db.mark_feedback_delivered(c, fid, error)
        finally:
            c.close()

    mailer.send_reporting(
        config.FEEDBACK_TO,
        FEEDBACK_SUBJECT.format(obscode=site.obscode, name=site.name),
        FEEDBACK_BODY.format(
            who=user["username"] if user else "Someone not signed in",
            page=page or "the board", obscode=site.obscode, name=site.name,
            reply_to=reply_to or "(none given)", message=message,
            fid=fid, site_url=config.SITE_URL.rstrip("/")),
        reply_to=reply_to, on_done=delivered)

    # "Received", not "sent". It is stored either way, and whether the relay
    # is up is not the sender's problem or their business.
    return render_template("feedback_sent.html", sent=True, page=page)


if __name__ == "__main__":
    import os
    # Default 8080, not 5000: on macOS port 5000 is held by AirPlay Receiver,
    # which answers with a confusing 403 instead of refusing the connection.
    app.run(host=os.environ.get("WHICHNEO_HOST", "0.0.0.0"),
            port=int(os.environ.get("WHICHNEO_PORT", "8080")), debug=False)
