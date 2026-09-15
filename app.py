"""Observer-facing website.

Reads only from SQLite -- no astronomy and no network on page load, so
rendering stays fast. The updater has already done all the work.
"""

import csv
import io
import json
import time
from datetime import datetime, timezone

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, send_file, url_for)

import auth
import config
import db
import ephemeris
import history
import history_plot
import moonplot
import observability
import observatories
import ranking
import skymap
import uncertainty

app = Flask(__name__)


try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(config.DISPLAY_TZ)
except Exception:                                    # no tzdata on the host
    _TZ = timezone.utc


@app.context_processor
def inject_config():
    """Templates read limits and the horizon mask straight from config, and
    the sortable-column registry straight from ranking so the sort bar and
    the sort logic never drift apart."""
    return {"config": config, "tzname": _tzabbr(), "ranking": ranking}


def _tzabbr(ts=None):
    d = datetime.fromtimestamp(ts if ts is not None else time.time(), _TZ)
    return d.strftime("%Z") or config.DISPLAY_TZ


@app.template_filter("localt")
def localt(ts):
    """Unix timestamp -> local clock time at the observatory."""
    if ts is None:
        return "—"
    return datetime.fromtimestamp(float(ts), _TZ).strftime("%H:%M")


@app.template_filter("localdt")
def localdt(ts):
    if ts is None:
        return "—"
    return datetime.fromtimestamp(float(ts), _TZ).strftime("%Y-%m-%d %H:%M")


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


def load_sorted(conn, show_observed=False, show_hidden=False, mode=None):
    rows = db.load_targets(conn, include_hidden=show_hidden,
                           include_observed=show_observed)
    return ranking.sort_targets(rows, mode)


def status(conn):
    timings = db.get_meta(conn, "last_update_timings")
    stamp = db.get_meta(conn, "last_update_utc", "never")
    try:
        ts = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        ts = None
    return {
        "last_update_utc": stamp,
        "last_update_ts": ts,
        "last_update_local": localt(ts) if ts else "never",
        "count": db.get_meta(conn, "last_update_count", "0"),
        "observable": db.get_meta(conn, "last_update_observable", "0"),
        "night": db.get_meta(conn, "night", "-"),
        "mismatches": db.get_meta(conn, "crosscheck_mismatches", "0"),
        "plan_path": db.get_meta(conn, "plan_path"),
        "ok": db.get_meta(conn, "last_update_ok", "0") == "1",
        "error": db.get_meta(conn, "last_error"),
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
    for r in obs[:max_lanes]:
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
        })

    now = time.time()
    return {
        "start": localt(start), "end": localt(end),
        "start_ut": utct(start), "end_ut": utct(end),
        "hours": ticks,
        "lanes": lanes,
        "now_pct": round(pct(now), 2) if start <= now <= end else None,
        "hidden": max(0, len(obs) - max_lanes),
    }


def sky_view(conn, rows, now=None):
    """The all-sky map, drawn fresh for this instant.

    Deliberately computed per request rather than stored by the updater: a
    fast mover crosses several degrees in the five minutes between cycles, so
    a cached picture would be visibly behind the sky it claims to show.

    The moon is pure astronomy from site and time. Target positions come from
    ephemeris lines already cached, so this makes no network call.
    """
    now = now if now is not None else time.time()
    shown = [r for r in rows if r["observable"]]
    tracks = {d: ephemeris.track(lines)
              for d, lines in db.load_tracks(
                  conn, [r["desig"] for r in shown]).items()}
    marks = skymap.target_marks(shown, tracks, now)
    try:
        moon = observability.moon_state(now)
    except Exception:
        moon = None                       # never take the board down for this
    return {
        "svg": skymap.render_svg(marks, moon, localt=localt),
        "moon": moon,
        "up": sum(1 for m in marks if m["up"]),
        "pending": sum(1 for m in marks if not m["up"]),
        "flagged": sum(1 for m in marks if m["up"] and m["mask"]),
        "total": len(shown),
    }


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
    lines = db.load_tracks(conn, [desig]).get(desig)
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
                mode=request.args.get("sort") or config.DEFAULT_SORT)


@app.route("/")
def index():
    v = _view_args()
    conn = get_conn()
    try:
        rows = load_sorted(conn, v["show_observed"], v["show_hidden"], v["mode"])
        upcoming = pick_upcoming(rows)
        return render_template(
            "index.html", rows=rows, strip=night_strip(rows),
            upcoming=upcoming, status=status(conn), sky=sky_view(conn, rows),
            max_score=ranking.max_possible_score(),
            poll_interval=config.WEB_POLL_INTERVAL_S, **v)
    finally:
        conn.close()


@app.route("/skymap.svg")
def skymap_svg():
    """Just the map, so the page can refresh it without a full reload."""
    v = _view_args()
    conn = get_conn()
    try:
        rows = load_sorted(conn, v["show_observed"], v["show_hidden"], v["mode"])
        return (sky_view(conn, rows)["svg"], 200,
                {"Content-Type": "image/svg+xml; charset=utf-8",
                 "Cache-Control": "no-store"})
    finally:
        conn.close()


@app.route("/rows")
def rows_partial():
    """Server-rendered tbody, polled by the page so refreshes stay cheap."""
    v = _view_args()
    conn = get_conn()
    try:
        return render_template(
            "_rows.html",
            rows=load_sorted(conn, v["show_observed"], v["show_hidden"], v["mode"]),
            max_score=ranking.max_possible_score(), **v)
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


@app.route("/plan")
def plan_text():
    """The nightly plan file, exactly as written to disk."""
    conn = get_conn()
    try:
        path = db.get_meta(conn, "plan_path")
    finally:
        conn.close()
    if not path:
        return "No plan written yet.", 404
    try:
        with open(path) as f:
            return f.read(), 200, {"Content-Type": "text/plain; charset=utf-8"}
    except OSError as e:
        return f"Could not read {path}: {e}", 500


@app.post("/mark/<desig>")
@auth.required
def mark(desig):
    action = request.form.get("action", "observed")
    conn = get_conn()
    try:
        if action == "observed":
            db.set_state(conn, desig, observed=1, observed_at_utc=db.utcnow())
        elif action == "unobserved":
            db.set_state(conn, desig, observed=0, observed_at_utc=None)
        elif action == "hide":
            db.set_state(conn, desig, hidden=1)
        elif action == "restore":
            db.set_state(conn, desig, hidden=0)
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
        rows = [r for r in db.load_targets(conn, True, True)
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
        pts = db.load_offsets(conn, desig)
        cov = uncertainty.coverage(pts) if pts else None

        # Same cached lines the sky map reads; full Row objects this time,
        # because the altitude plot needs moon_alt and sun_alt per row, not
        # just the position triple track() returns.
        eph_lines = db.load_tracks(conn, [desig]).get(desig)
        eph_rows = ephemeris.from_lines(desig, eph_lines).rows if eph_lines else []
        moon_svg = moonplot.render_svg(
            eph_rows, rows[0]["window_start_ts"], rows[0]["window_end_ts"],
            localt=localt, tzlabel=_tzabbr()) if eph_rows else None

        hconn = history.connect()
        try:
            hist_rows = history.object_history(hconn, desig)
        finally:
            hconn.close()
        hist_charts = history_charts(hist_rows)

        return render_template(
            "target.html", row=rows[0],
            unc_svg=uncertainty.render_svg(pts) if pts else None,
            unc_points=len(pts) if pts else 0,
            unc_distinct=uncertainty.distinct(pts) if pts else 0,
            unc_coverage=cov,
            unc_extent=uncertainty.extent(pts) if pts else None,
            moon_svg=moon_svg,
            fov=config.FOV_ARCSEC,
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
def history_download_db():
    """The raw neocp_history.py database, exactly as stored -- full
    fidelity, no export step, and every relation (objects to their
    snapshots) intact for anyone who wants to query it directly."""
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


if __name__ == "__main__":
    import os
    # Default 8080, not 5000: on macOS port 5000 is held by AirPlay Receiver,
    # which answers with a confusing 403 instead of refusing the connection.
    app.run(host=os.environ.get("WHICHNEO_HOST", "0.0.0.0"),
            port=int(os.environ.get("WHICHNEO_PORT", "8080")), debug=False)
