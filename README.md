# visnjan_whichneo

Live NEOCP follow-up target board for **L01 — Tičan Station, Višnjan Observatory**.

Pulls the MPC NEO Confirmation Page every 5 minutes, computes observability for
L01, ranks targets by intrinsic follow-up value, and serves an observer-facing
queue that runs 24/7.

Built independently of the legacy observatory scripts. Every observatory-specific
rule is a configurable placeholder — see **[TBD.md](TBD.md)** for the open
questions that need Luka's input.

## Quick start

```bash
pip install -r requirements.txt

python3 update_neocp.py          # one update cycle
python3 app.py                   # http://localhost:5000
```

Run the updater continuously with `python3 update_neocp.py --loop`, or install
the systemd timer in `deploy/`.

## Architecture

```
MPC NEOCP ──▶ update_neocp.py ──▶ SQLite ──▶ app.py ──▶ browser
              (every 5 min)                            (polls every 20 s)
```

The updater precomputes **all** astronomy and ranking. The website does no
astronomy and makes no network calls on page load, which is what keeps rendering
fast. The single exception is the per-target ephemeris view, which fetches from
MPC on demand.

| File | Role |
|---|---|
| `config.py` | Site, horizon mask, limits, weights. All TBD placeholders live here. |
| `neocp.py` | Fetch + parse the NEOCP tabular feed; on-demand ephemeris. |
| `observability.py` | Alt/az, airmass, transit, moon, sun, observing windows. |
| `ranking.py` | Intrinsic score from digest2 / arc / magnitude. |
| `output.py` | MPCS-format observing block. |
| `db.py` | SQLite schema and state handling. |
| `update_neocp.py` | The 5-minute update cycle, with per-stage timing. |
| `app.py` | Flask website. |

## Design decisions worth knowing

**The site is derived, not hardcoded.** L01's position comes from its official
MPC parallax constants (`config.SITE_RHO_COS_PHI` / `SITE_RHO_SIN_PHI`), which
resolve to 45.2909°N, 13.74930°E, 381 m — the same values Find_Orb and the MPC
use. Changing observatory means changing three constants.

**Ranking is intrinsic, observability is a flag.** The score uses only digest2,
arc length and magnitude — deliberately *not* altitude. The board runs through
the day, and a time-varying score would collapse to near-zero for everything
during Višnjan daylight. Observability is computed separately and sinks
unobservable targets below observable ones, so the top of the list is always
something you could shoot now, while the ordering below stays stable and
interpretable.

**Observer state is a separate table.** `targets` is rewritten wholesale every
cycle; `observer_state` (observed / hidden / manual priority) is never touched
by the updater. That is what makes "mark observed" survive a NEOCP refresh.

**Positions are NEOCP-listed, not ephemerides.** NEOCP gives one position per
object, from the date it was last posted. For objects unseen for days this is
approximate — the queue shows an "Unseen" column so you can see which. Real
pointing should use the ephemeris on the target detail page. Sky motion and
position angle are blank in the observing block for the same reason: they only
exist in the per-object ephemeris.

**IERS auto-download is disabled.** astropy otherwise tries to fetch
earth-orientation data and will throw or hang on a stale table — which would
take down an unattended service at 3 a.m. The resulting error is far below a
degree, irrelevant for horizon flags.

## Performance

Measured on a live run, 86 targets:

| Stage | Time |
|---|---|
| NEOCP fetch | 296 ms |
| Parse | 1.1 ms |
| Observability | 2059 ms |
| Ranking | 0.3 ms |
| Database | 2.2 ms |
| **Total** | **2.4 s** |

That is ~127× inside the 5-minute budget. Page render is ~90 ms for the full
table, ~36 ms for the polled row partial. Timings are recorded every cycle and
shown in the page header, so regressions are visible without profiling.

## Verification

The astronomy was checked against independent calculation rather than assumed:

- Sun altitude at local midnight: computed −39.20°; analytic lower culmination
  `arcsin(−cos(dec+lat))` with dec=+5.5°, lat=45.29° gives −39.2°. Exact match.
- Moon illumination cross-checked against a separate astropy run.
- RA/Dec formatting unit-checked: 328.2945° → 21h 53m 10.68s.
- Target altitudes cross-checked against transit altitudes computed separately.

Run `python3 selftest.py` to re-run these checks.

## Status

Implemented: NEOCP ingest, parsing, SQLite storage, observability, horizon mask,
filtering, intrinsic ranking, observer controls (mark observed / hide / manual
priority), MPCS-format output, automated updates, logging, per-stage profiling.

Not yet done: weather integration, deployment to a permanent host, and every
rule in [TBD.md](TBD.md).
