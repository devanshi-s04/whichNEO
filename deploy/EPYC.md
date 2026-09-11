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
