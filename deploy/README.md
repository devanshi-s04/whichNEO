# Deployment

Two independent units: the updater runs on a timer, the website runs
continuously. They share only the SQLite file, which is in WAL mode so the
website reads while the updater writes.

## systemd (preferred)

Edit `WorkingDirectory` in both service files to match the install path, then:

```bash
sudo cp whichneo-*.service whichneo-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now whichneo-update.timer
sudo systemctl enable --now whichneo-web.service

systemctl list-timers whichneo-update.timer   # confirm the schedule
journalctl -u whichneo-update.service -f      # watch update cycles
```

## cron (fallback)

```cron
*/5 * * * * cd /home/devanshi-s04/visnjan_whichneo && /usr/bin/env python3 update_neocp.py >> data/cron.log 2>&1
```

## No init system at all

```bash
python3 update_neocp.py --loop &
python3 app.py
```

## Production web server

`app.py` runs Flask's development server, which is single-threaded and will
serialise concurrent observers. For real use:

```bash
pip install waitress
python3 -m waitress --host=0.0.0.0 --port=5000 app:app
```

## Health check

The page header shows the last successful update time and the cycle duration.
`GET /status` returns the same as JSON, including per-stage timings — suitable
for an external monitor:

```bash
curl -s localhost:5000/status | python3 -m json.tool
```
