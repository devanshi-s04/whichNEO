"""Observer-facing website.

Reads only from SQLite -- no astronomy and no network on page load, so
rendering stays fast. The updater has already done all the work.
"""

import json
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, redirect, render_template, request, url_for

import config
import db
import ranking

app = Flask(__name__)


try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(config.DISPLAY_TZ)
except Exception:                                    # no tzdata on the host
    _TZ = timezone.utc


@app.context_processor
def inject_config():
    """Templates read limits and the horizon mask straight from config."""
    return {"config": config, "tzname": _tzabbr()}


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
    observable = [r for r in rows if r["observable"]]
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


def _view_args():
    return dict(show_observed=request.args.get("observed") == "1",
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
            upcoming=upcoming, status=status(conn),
            max_score=ranking.max_possible_score(),
            poll_interval=config.WEB_POLL_INTERVAL_S, **v)
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
        elif action in ("up", "down"):
            row = conn.execute(
                "SELECT COALESCE(priority_bump,0) AS b FROM observer_state "
                "WHERE desig=?", (desig,)).fetchone()
            current = row["b"] if row else 0.0
            db.set_state(conn, desig,
                         priority_bump=current + (1.0 if action == "up" else -1.0))
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
            return f"Unknown target {desig}", 404
        return render_template("target.html", row=rows[0])
    finally:
        conn.close()


if __name__ == "__main__":
    import os
    # Default 8080, not 5000: on macOS port 5000 is held by AirPlay Receiver,
    # which answers with a confusing 403 instead of refusing the connection.
    app.run(host=os.environ.get("WHICHNEO_HOST", "0.0.0.0"),
            port=int(os.environ.get("WHICHNEO_PORT", "8080")), debug=False)
