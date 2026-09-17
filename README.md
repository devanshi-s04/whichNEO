# visnjan_whichneo

NEOCP follow-up planner for **L01 — Tičan Station, Višnjan Observatory**.

Pulls the MPC NEO Confirmation Page every 5 minutes, applies the observatory's
filter cascade, and produces a **time-ordered observing sequence** for the
night — served as a web page and written as a plain-text plan file.

Conceptually modelled on the observatory's own planner (`planets-new.py`), with
its rules and output format reproduced, its bugs fixed, and its numbers
independently verified. See **[TBD.md](TBD.md)** for what still needs Luka's
confirmation.

## Quick start

```bash
pip install -r requirements.txt

python3 update_neocp.py     # one update cycle
python3 app.py              # http://localhost:8080
python3 selftest.py         # 50 verification checks
```

Port 8080, not 5000 — macOS runs AirPlay Receiver on 5000 and it answers with
a confusing `403` instead of refusing the connection.

## How it works

```
neocp.txt ────────┐
neocp_info (q,e) ─┼──▶ update_neocp.py ──▶ SQLite ──▶ app.py ──▶ browser
confirmeph2 (per ─┘      (every 5 min)         └──▶ plans/YYYY-MM-DD.txt
  object, for L01)
```

**Three MPC sources.** The list feed gives designation, digest2 score, arc and
not-seen. `neocp_info` gives `e` and `a`, from which `q = a(1−e)`. The
confirmation-page CGI generates a full-night ephemeris *for L01*, which is the
only source of sky motion, moon distance and solar altitude.

**Ordering is chronological**, by the time each target reaches its best
*observable* altitude. Work down the list and each target is near its peak when
you reach it. This matches the legacy planner and is a scheduling order, not a
league table. The intrinsic score (digest2, arc, magnitude) is kept as a
separate sortable column — it answers "what is most worth having at all" and
deliberately excludes altitude so it stays meaningful during the day.

**The observing night runs 11:00 → 11:00 UT**, so evening and the small hours
belong to one night and one plan file.

## Filter cascade

| Filter | Default | Source |
|---|---|---|
| digest2 score | ≥ 25 | legacy |
| Arc | ≥ 0.01 d | legacy |
| Not seen | ≤ 4 d | legacy |
| Magnitude | ≤ 21.6 | legacy |
| NEO-like | `q < 1.3` or `e > 0.5` | legacy |
| Scatteredness | ≤ (2000, 2000)″ | legacy |
| Already observed from L01 | discard | legacy |
| Sun altitude | < −15° | legacy |
| Sky motion | ≥ 0.7 ″/min | legacy |
| Moon distance | ≥ 20° | legacy |
| Altitude | ≥ 15°, and MPC's own `oalt=20` | legacy |
| **Azimuth dome mask** | **per sector, N blocked** | **ours** |

The dome mask is the one rule the legacy planner does not have — its code
carries only a flat altitude floor. Everything else is reproduced so the two
systems can be compared directly.

Exposure time uses the observatory's own rule, recovered from the legacy code:
`minutes = 10 + (V − 18) × 5`.

## Things the legacy planner gets wrong, fixed here

- **Azimuth convention.** MPC reports azimuth measured from *south*. Applying a
  compass-bearing dome mask to those values rotates every target by 180° —
  blocked northern sky reads as open southern sky. Caught by the cross-check
  and now converted on parse; `selftest.py` locks it down.
- **Column collision in `neocp_info`.** `e` and `a` run together when `a` is
  large (`0.9982411.744`). The legacy parser requires exactly 13 whitespace
  fields and silently drops those rows — precisely the most extreme orbits.
  Parsed here by three-decimal structure instead.
- **Filters evaluated once, ever.** The legacy planner analyses an object the
  first time it appears and never re-checks it, so a target rejected at 19:00
  for being low stays rejected after it rises. Re-evaluated every cycle here.
- **Corrupted interpolated line.** `updateLineFromData()` splices fixed indices
  into a whitespace-split line; its own output contains `128 128 +45 +45`.
  Lines are formatted from parsed values here.
- **Use-after-delete.** The legacy removal path deletes a list element then
  reads it, misreporting which object vanished.
- **Negative exposures.** The formula is unbounded below and yields −17.5
  minutes at V=12.5. Clamped, with the floor flagged as TBD.
- **Timezone-dependent timestamps.** The legacy code runs UTC ephemeris times
  through `time.mktime()`, which reinterprets them as local. The error mostly
  cancels; here everything is UTC throughout.

## Verification

`selftest.py` runs 50 checks. Two matter most:

- **Analytic vs astropy** — the fast path used for observing windows is
  cross-validated against astropy's full transform. Catching a 0.67° azimuth
  drift here is what originally surfaced missing precession handling.
- **Azimuth convention** — pins MPC-south to compass-north conversion, the
  error with the largest silent blast radius.

At runtime, every cycle recomputes MPC's alt/az and lunar separation with
astropy and flags disagreements beyond tolerance, so a change to MPC's page
format surfaces as a visible mismatch rather than quietly wrong pointing.

## Performance

| | Cold cache | Typical | Warm |
|---|---|---|---|
| Full cycle | ~38 s | ~33 s | **~2 s** |

Page render is ~140 ms; the polled row partial ~38 ms. Twelve concurrent
requests are all served in under 1.8 s.

Ephemerides are cached against a signature of the object's NEOCP row, so they
are re-requested only when new observations change the solution. Objects
rejected by the cheap list-level filters never trigger a per-object request at
all.

### Against the legacy planner

Benchmarked as request patterns on identical client code — `planets-new.py`
cannot be run here (it needs `requests_cache` and `playsound`, and prompts
interactively), so this measures the architecture rather than the binary.

| | Legacy | Here |
|---|---|---|
| Per object | 2.65 s sequential | 0.95 s, 4 workers |
| Objects fetched | all 82 | 40 |
| Cold start | 218 s | 38 s |
| | | **5.7× faster** |

Roughly 2.8× of that comes from concurrency and 2× from pre-filtering: half
the NEOCP list fails a score, arc, not-seen or NEO check that costs nothing,
and those objects never generate a request. The legacy planner calls
`getEphemerides()` as the first line of `analyzePlanet`, before any filter, so
every object costs three sequential requests regardless.

The consequence matters more than the ratio. A legacy cold start uses 73% of
its own 300 s budget, which is why it can only analyse each object once ever —
and that is exactly the staleness bug. Re-evaluating everything every cycle,
which is what correctness requires, costs it 218 s and costs us about 2.

## Files

| File | Role |
|---|---|
| `sites.py` | The `Site` type, and loading sites from `sites/*.toml`. |
| `sites/` | One TOML file per observatory. `L01.toml` is Višnjan, and its TBDs. |
| `config.py` | The deployment: MPC endpoints, storage, mail, accounts, ds42. |
| `neocp.py` | NEOCP list + `neocp_info` orbital parameters. |
| `ephemeris.py` | Per-object MPC ephemeris, scatteredness, observation history. |
| `pipeline.py` | Night model, filter cascade, row selection, cross-check. |
| `ranking.py` | Chronological ordering; intrinsic score. |
| `output.py` | Nightly plan file in the legacy format. |
| `observability.py` | Independent astropy calculations. |
| `db.py` | SQLite: targets, observer state, ephemeris cache. Per-site tables carry `site_id`; ds42 scores and the history are shared. |
| `update_neocp.py` | The 5-minute cycle, with per-stage timing. |
| `app.py` | Flask website. |
| `auth.py` | Accounts: argon2id passwords, sessions, CSRF. |
| `mailer.py` | Outgoing mail. Password resets, nothing else. |
| `manage.py` | Account administration from the shell. |

Observer state (`observed` / `hidden`) lives in its own table the updater never
touches, so marking a target observed survives a refresh.

## Accounts

Anyone can read the board. Changing it — mark done, hide — needs an account,
and sign-up is self-service at `/register`.

**Observer state is per account.** What you have marked done is your record of
your night, not a fact about the target: two people working the same list do
not overwrite each other, and one person clearing their queue does not clear
anybody else's. The cost is that the same object could be integrated twice by
two observers who cannot see each other, so every row also carries a `done by
<name>` marker when another account has already shot it.

The nightly plan file is built from `targets` alone and never consults
observer state. The plan is the observatory's, not one observer's.

Forgotten passwords reset by email at `/forgot`, for accounts that gave one.
The link is signed rather than stored — it carries a fingerprint of the
account's current password hash, so using it makes it useless — and the form
answers identically whether or not the account exists, so it cannot be used to
find out who has one. Without an email on file the recovery path is still
`manage.py passwd` on epyc.

See `deploy/EPYC.md` for the deployment details — the `WHICHNEO_HTTPS` flag,
`data/secret_key`, `data/smtp_password`, and retiring the old shared password.

## Known formatting difference

Our plan file omits the trailing `Map/Offsets` text that appears on the legacy
planner's ephemeris lines. That string is HTML link text scraped along with the
row, not data. Everything else lines up column-for-column.
