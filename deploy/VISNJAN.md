# Running WhichNEO at Tičan — for Luka

This is the recommended home: the observers are there, so the board is reached
over the observatory LAN with no firewall, no VPN and no dependency on
university infrastructure. It runs alongside the existing planner rather than
replacing it.

## What it needs

- A machine that stays on through the night — the one that already runs
  `START.bat` is ideal.
- Python 3.9 or newer.
- Outbound HTTPS to `minorplanetcenter.net` (the same access the current
  planner already uses).
- About 200 MB of disk. No GPU, no database server, nothing else.

It does **not** need an inbound port from the internet. Observers open it on
the local network.

## Install (Windows)

```bat
git clone https://github.com/devanshi-s04/whichNEO.git
cd whichNEO
pip install -r requirements.txt
python selftest.py
```

`selftest.py` should print `all checks passed`. If it does, the astronomy and
all the parsers are working on that machine.

## Run

Two processes. Easiest is two shortcuts, or one `.bat`:

```bat
start "whichneo-updater" python update_neocp.py --loop
python -m waitress --host=0.0.0.0 --port=8080 app:app
```

Then from any machine on the observatory network:

```
http://<the machine's LAN address>:8080
```

## Keep it running unattended

Windows Task Scheduler, "At startup", two tasks:

| Task | Program | Arguments |
|---|---|---|
| whichneo-update | `python` | `update_neocp.py --loop` |
| whichneo-web | `python` | `-m waitress --host=0.0.0.0 --port=8080 app:app` |

Set both to "Run whether user is logged on or not" and "Restart if the task
fails". `nssm` is a tidier alternative if it is already used there.

## Checking it is alive

```
http://<machine>:8080/status
```

Returns the last update time, per-stage timings and the cross-check mismatch
count. The thing to watch is `last_update_utc` going stale — the web server can
be perfectly healthy while the updater is wedged.

Logs are in `data/update.log`. Nightly plan files are written to `plans/` in
the same format the current planner uses, so they can be compared directly.

## What it does to the machine

- One HTTP request to MPC every 5 minutes for the object list, plus a few
  per-object requests only when new observations change an orbit.
- A warm update cycle takes about 2 seconds; a cold start about 110.
- Nothing is written outside the `whichNEO` folder.
- It never submits anything to MPC and never controls the telescope.

## Firewall

If Windows Firewall prompts on first run, allow Python on **private
networks only**. There is no reason to expose port 8080 to the internet.
