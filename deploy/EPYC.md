# Running WhichNEO on epyc (UW Astronomy)

Good for development and for showing the board to people at UW. Whether
observers in Croatia can reach it depends on a firewall rule that has to be
requested — see the bottom of this file.

## Install

```bash
git clone https://github.com/devanshi-s04/whichNEO.git ~/whichNEO
cd ~/whichNEO
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 selftest.py          # expect: all checks passed
```

A virtualenv rather than the shared conda stack — this pulls in `waitress`,
`beautifulsoup4` and `lxml`, and there is no reason to push those into an
environment other people use.

## Run as user services (no root needed)

`systemd --user` keeps both processes alive without sysadmin involvement:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/whichneo-update.service deploy/whichneo-update.timer \
   deploy/whichneo-web.service ~/.config/systemd/user/
# edit WorkingDirectory to /home/<you>/whichNEO and point ExecStart at
# .venv/bin/python
systemctl --user daemon-reload
systemctl --user enable --now whichneo-update.timer whichneo-web.service
loginctl enable-linger "$USER"     # survives logout
```

`enable-linger` matters: without it the services stop when you log out.

Check:

```bash
systemctl --user status whichneo-web.service
journalctl --user -u whichneo-update.service -f
curl -s localhost:8080/status | python3 -m json.tool
```

If long-running user processes get reaped on that host, this approach will not
survive and the answer is Docker on something persistent, or the observatory
machine.

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
