# Running WhichNEO on epyc (UW Astronomy)

Good for development and for showing the board to people at UW. Whether
observers in Croatia can reach it depends on a firewall rule that has to be
requested — see the bottom of this file.

## 1. Preflight — check the host can do this at all

```bash
git clone https://github.com/devanshi-s04/whichNEO.git ~/whichNEO
cd ~/whichNEO
bash deploy/preflight.sh
```

Read-only; changes nothing. It verifies Python 3.9+, `venv`, that
`Europe/Zagreb` resolves (without tzdata the board silently falls back to
UTC), that MPC is reachable and not proxied, whether `systemd --user` and
lingering are available, free space, and that the port is free.

If it fails, stop there — the failure tells you what to fix or whether epyc is
the wrong host.

## 2. Install and start

```bash
./deploy/epyc_setup.sh
```

Creates a virtualenv (not the shared conda stack — no reason to push
`waitress`, `beautifulsoup4` and `lxml` into an environment other people use),
installs dependencies, runs the selftest, primes the database with one cycle,
then writes and enables `systemd --user` units with the correct absolute
paths. No root at any point.

It also enables lingering, which matters: without it every service stops the
moment you log out.

Use a different port with `WHICHNEO_PORT=9000 ./deploy/epyc_setup.sh`.

## 2b. If there is no systemd — keep it alive with cron

```bash
./deploy/install_keepalive.sh
```

Adds a `@reboot` entry and a check every 3 minutes. It only acts when
`/status` stops answering, so a healthy board is never touched. `run.sh`
supervises its own two children, but nothing supervises `run.sh` — without
this, a hard crash or a container restart leaves the board down until a human
notices. Log: `data/keepalive.log`.

## 2c. Accounts

Reads are open by design: observers should not fumble a password on a dome
screen to see tonight's targets. **Writes — mark observed, hide — require an
account**, on a LAN and on a public port alike. There is no configuration that
turns that off, because the board now answers at
`https://whichneo.juriclab.org/`.

Sign-up is self-service at `/register`. Nothing else is needed to deploy: the
`users` table is created on first run alongside the rest of the schema, and
the session-signing key writes itself to `data/secret_key` (mode 0600) the
first time the site starts.

Two things that are worth getting right:

```bash
# Turn on the Secure flag once every route in is https. Do NOT set it while
# the dome still reaches the board over http://epyc:12600 -- a Secure cookie
# is never sent over http, so nobody can log in and there is no error saying
# why.
export WHICHNEO_HTTPS=1

# Back this up with the database. Losing it does not lose any account, but it
# signs out every observer at once.
ls -l data/secret_key
```

### Copying the database: do not use `cp`

`data/targets.db` runs in WAL mode, so recent commits live in
`data/targets.db-wal` until SQLite checkpoints them. **`cp data/targets.db
somewhere` silently gives you a stale copy** — no error, just missing the most
recent writes. It cost me a confusing half hour: a copy taken minutes after 73
ds42 scores were written contained none of them.

Use SQLite's own backup, which reads through the WAL and is safe while the
board is running:

```bash
python3 -c "
import sqlite3
src = sqlite3.connect('file:data/targets.db?mode=ro', uri=True)
dst = sqlite3.connect('/path/to/copy.db')
src.backup(dst); dst.close(); src.close()"
```

(Or `cp` all three of `targets.db`, `-wal` and `-shm` together, but the
backup API is the one that cannot be got subtly wrong.)

Administration:

```bash
python3 manage.py list
python3 manage.py adduser luka --admin
python3 manage.py passwd luka
python3 manage.py deluser someone
python3 manage.py mailtest you@example.org
```

## 2d. Outgoing mail

Used for one thing: the password-reset link at `/forgot`. The relay is the
one at `infra.juriclab.org`; everything but the password is already in
`config.py`.

```bash
printf '%s' 'the-password' > data/smtp_password
chmod 600 data/smtp_password
python3 manage.py mailtest you@example.org   # confirm before trusting it
```

or set `WHICHNEO_SMTP_PASSWORD` in the environment. **With no password
configured the site runs exactly as before**, minus the reset link — `/forgot`
says so plainly instead of pretending to send.

Two things worth knowing when it appears not to work:

- **The website never reports a send failure.** `/forgot` answers identically
  for an address that exists, one that does not, and one whose message
  bounced — otherwise the form becomes a way to find out which accounts exist,
  which on a board with open sign-up is the one thing an attacker cannot learn
  any other way. Failures go to the log; `manage.py mailtest` is how you see
  the actual error.
- **Only accounts with an email on file can reset this way.** Email is
  optional at sign-up. For everyone else the recovery path is still
  `manage.py passwd`.

Reset links carry no stored secret: they are signed with `data/secret_key` and
contain a fingerprint of the account's current password hash, so a link stops
working the moment it is used and expires after an hour regardless. Replacing
`data/secret_key` invalidates every outstanding link along with every session.

### The shared password still works, for now

The old `data/auth` credential is still accepted on writes, so a script or a
browser already using it keeps working:

```bash
printf 'observer:choose-a-real-password\n' > data/auth
chmod 600 data/auth
```

It is transitional. **Delete `data/auth` once everyone has an account** — it
is one password shared by everyone, which is exactly what accounts exist to
replace, and a write made with it is attributed to the shared name rather than
to a person. `GET /status` still reports `auth`, now meaning "writes are
protected", which is always true.

## 3. Check it

```bash
systemctl --user status whichneo-web.service
systemctl --user list-timers whichneo-update.timer
journalctl --user -u whichneo-update.service -f
curl -s localhost:8080/status | python3 -m json.tool
```

If long-running user processes get reaped on that host, this will not survive
and the answer is Docker on something persistent, or the observatory machine.

## Reaching it

**From UW, over SSH — works today, no permission needed:**

```bash
ssh -N -L 8080:localhost:8080 <you>@epyc.astro.washington.edu
```

then open `http://localhost:8080` on your laptop.

**From Croatia — needs a firewall rule.** Observers will not have UW VPN, so
either a port must be opened or this is not the right host for them.

## Draft request to the sysadmins

> I'm running a small internal web service on epyc for a collaboration with
> Višnjan Observatory in Croatia. It's a Python/Flask app on port 8080 serving
> a read-mostly observing-target board, run under my user account via
> `systemd --user`. Collaborators in Croatia need to reach it during their
> observing nights, so they can't use UW VPN.
>
> Is there a supported way to expose a single port publicly — a reverse proxy
> entry, or a hostname on the department's web server? Happy to put it behind
> HTTP basic auth or an IP allowlist for the observatory. Outbound access is
> only to minorplanetcenter.net; nothing is accepted from the public other
> than page views and a couple of small state-changing buttons.

Worth saying in the request that the load is trivial: one HTTP request to MPC
every five minutes and a handful of page views a night.

## Honest recommendation

If the users are the observers at Višnjan, running it **at the observatory**
(see `VISNJAN.md`) avoids this conversation entirely and removes a
transatlantic dependency from their night. Use epyc to develop and to show
people; move it to Tičan, or to a small VPS, once Luka has seen it.
