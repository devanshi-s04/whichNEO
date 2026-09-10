"""Observer-facing website.

Reads only from SQLite -- no astronomy and no network calls on page load, so
rendering stays fast. The one exception is the per-target ephemeris view,
which fetches from MPC on demand.
"""

import json

from flask import Flask, jsonify, redirect, render_template, request, url_for

import config
import db
import neocp
import output
import ranking

app = Flask(__name__)


@app.context_processor
def inject_config():
    """Templates read limits and the horizon mask directly from config."""
    return {"config": config}


def get_conn():
    conn = db.connect()
    db.init(conn)
    return conn


def load_sorted(conn, show_observed=False, show_hidden=False):
    rows = db.load_targets(conn, include_hidden=show_hidden,
                           include_observed=show_observed)
    rows.sort(key=ranking.sort_key)
    now = db.get_meta(conn, "last_update_utc", "never")
    for r in rows:
        r["block"] = output.observing_block(r, now if now != "never" else "1970-01-01 00:00")
        r["command"] = output.one_line_command(r)
    return rows


def status(conn):
    timings = db.get_meta(conn, "last_update_timings")
    return {
        "last_update_utc": db.get_meta(conn, "last_update_utc", "never"),
        "count": db.get_meta(conn, "last_update_count", "0"),
        "ok": db.get_meta(conn, "last_update_ok", "0") == "1",
        "error": db.get_meta(conn, "last_error"),
        "timings": json.loads(timings) if timings else {},
    }


@app.route("/")
def index():
    show_observed = request.args.get("observed") == "1"
    show_hidden = request.args.get("hidden") == "1"
    conn = get_conn()
    try:
        return render_template(
            "index.html",
            rows=load_sorted(conn, show_observed, show_hidden),
            status=status(conn),
            config=config,
            max_score=ranking.max_possible_score(),
            show_observed=show_observed,
            show_hidden=show_hidden,
            poll_interval=config.WEB_POLL_INTERVAL_S,
        )
    finally:
        conn.close()


@app.route("/rows")
def rows_partial():
    """Server-rendered tbody, polled by the page so refreshes stay cheap."""
    conn = get_conn()
    try:
        return render_template(
            "_rows.html",
            rows=load_sorted(conn, request.args.get("observed") == "1",
                             request.args.get("hidden") == "1"),
            max_score=ranking.max_possible_score(),
        )
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
        return jsonify({"status": status(conn), "targets": load_sorted(conn)})
    finally:
        conn.close()


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
                "SELECT COALESCE(priority_bump,0) AS b FROM observer_state WHERE desig=?",
                (desig,)).fetchone()
            current = row["b"] if row else 0.0
            db.set_state(conn, desig,
                         priority_bump=current + (1.0 if action == "up" else -1.0))
    finally:
        conn.close()
    return redirect(request.referrer or url_for("index"))


@app.route("/target/<desig>")
def target_detail(desig):
    """On-demand MPC ephemeris for one object -- the only outbound request the
    website makes, kept off the 5-minute loop deliberately."""
    conn = get_conn()
    try:
        rows = [r for r in load_sorted(conn, True, True) if r["desig"] == desig]
        if not rows:
            return f"Unknown target {desig}", 404
        eph_text, eph_error = None, None
        if request.args.get("ephemeris") == "1":
            try:
                eph_text = neocp.fetch_ephemeris(desig)
            except Exception as e:
                eph_error = str(e)
        return render_template("target.html", row=rows[0], eph_text=eph_text,
                               eph_error=eph_error,
                               eph_url=neocp.ephemeris_url(desig), config=config)
    finally:
        conn.close()


if __name__ == "__main__":
    import os
    # Default 8080, not 5000: on macOS port 5000 is held by AirPlay Receiver,
    # which answers with a confusing 403 instead of refusing the connection.
    app.run(host=os.environ.get("WHICHNEO_HOST", "0.0.0.0"),
            port=int(os.environ.get("WHICHNEO_PORT", "8080")), debug=False)
