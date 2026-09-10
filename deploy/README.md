# Deployment

Two processes share one SQLite file (WAL mode, so the website reads while the
updater writes):

| Process | What it does | Cadence |
|---|---|---|
| `update_neocp.py --loop` | fetches MPC, filters, ranks, writes the plan file | every 5 min |
| `app:app` (waitress) | serves the board, does no astronomy | continuous |

`run.sh` starts both and stops both if either dies — a board serving silently
stale data is worse than one that is visibly down.

## GitHub Pages will not work

Worth stating plainly, since it comes up: Pages serves **static files only**.
This app needs a live process to poll MPC every five minutes, and a server to
accept "mark observed" and the manual priority controls. Neither is possible
on Pages.

There is a degraded fallback — a GitHub Actions cron that runs the updater and
commits a rendered HTML page to Pages — but it loses every observer control,
and Actions' scheduled runs are throttled and routinely delayed well past five
minutes. Not recommended for something used during a live session.

## Option A — Docker (most portable)

```bash
docker build -t whichneo .
docker run -d --name whichneo --restart unless-stopped \
  -p 8080:8080 \
  -v whichneo-data:/app/data \
  -v whichneo-plans:/app/plans \
  whichneo
```

Everything is pinned inside the image including `tzdata`, which the board
needs to render Višnjan local time. Has a healthcheck on `/status`.

## Option B — systemd on a Linux host

Edit `WorkingDirectory` in both unit files, then:

```bash
sudo cp whichneo-*.service whichneo-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now whichneo-update.timer
sudo systemctl enable --now whichneo-web.service

systemctl list-timers whichneo-update.timer
journalctl -u whichneo-update.service -f
```

## Option C — the observatory's own machine

The legacy planner already runs at Tičan from `START.bat`, so there is a
machine there that stays on through the night. `waitress` is pure Python and
runs on Windows, so:

```bat
pip install -r requirements.txt
start python update_neocp.py --loop
python -m waitress --host=0.0.0.0 --port=8080 app:app
```

This is the option with the fewest dependencies on anyone else's
infrastructure, and observers reach it over the observatory LAN with no
firewall or VPN involved.

## Choosing a host

The binding constraint is not compute — a cycle is about two seconds warm.
It is **who needs to reach the page, and from where**.

| Host | Reachable from Croatia? | Notes |
|---|---|---|
| Observatory machine | yes, over the LAN | no external dependency; needs the box to stay up |
| Public VPS | yes | ~$5/month, full control, needs TLS and basic hardening |
| `epyc` (UW Astronomy) | **usually not** | university hosts are firewalled; a public port normally needs sysadmin involvement, and observers would otherwise need UW VPN |
| GitHub Pages | n/a | static only — cannot run this |

If observers at Višnjan are the users, running it at Višnjan or on a small VPS
is the honest answer. `epyc` works well for development and for you to look at
it, but exposing it to Croatia is a request to UW sysadmins, not a config
change.

## Behind a reverse proxy

`waitress` should sit behind nginx or Caddy for TLS. Caddy is two lines:

```
whichneo.example.org {
    reverse_proxy 127.0.0.1:8080
}
```

## Health

`GET /status` returns the last update time, per-stage timings and the
cross-check mismatch count — suitable for an external monitor:

```bash
curl -s localhost:8080/status | python3 -m json.tool
```

Alert on `last_update_utc` going stale rather than on process liveness: the
web server can be perfectly healthy while the updater is wedged.
