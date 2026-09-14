# Open questions for Luka

**Quick index — what actually needs deciding:**

| # | Question | Blocking |
|---|---|---|
| 1 | Dome mask numbers (SW never specified, NW and W conflict, no upper limit) | which targets we show |
| 3b | **Exposure frame length and count** | PR #3, not deployed |
| 4 | Confirm the recovered legacy thresholds | filter behaviour |
| 5 | Should an object drop out after one night from L01? | queue contents |
| 6 | Does anything read the nightly plan file? | output format |

Everything else is lower stakes. The convention question that was open in §1 is
now resolved and recorded there.

Every item is a placeholder in `config.py`. Nothing here was invented to look
complete — each is a real gap with a default that is probably wrong somewhere.

Thresholds recovered from the observatory's own planner (`planets-new.py`) are
marked **legacy**; they are almost certainly right, but worth confirming that
they are still what you actually use rather than what the code drifted to.

## 1. Dome / horizon geometry — highest impact

This is the one rule the legacy planner does **not** have. Its code carries
only a flat altitude floor, so the sector geometry below exists nowhere except
in your notes, and it is what most changes which targets we show.

| Sector | Azimuth | Using | Problem |
|---|---|---|---|
| N  | 337.5–22.5° | **blocked** | is the blocked sector really 45° wide? |
| NE | 22.5–67.5°  | 20° | from "above 20 in South and East" |
| E  | 67.5–112.5° | 20° | ” |
| SE | 112.5–157.5°| 20° | ” |
| S  | 157.5–202.5°| 20° | ” |
| SW | 202.5–247.5°| 30° | **never specified — we interpolated** |
| W  | 247.5–292.5°| 40° | "below 30-40 in the west" vs "West = 40" |
| NW | 292.5–337.5°| 40° | "northwest is 50" vs "Northwest = 40" |

- Is this a hard dome obstruction or a working preference?
- Is the real mask a smooth profile rather than eight sectors? Find_Orb
  supports a continuous horizon (`site_L01.txt`) — if one exists, we should
  use it directly.

**Azimuth convention: RESOLVED, no longer a question.** MPC reports azimuth
from south; we convert to compass bearings on parse. That the notes are also
compass bearings was confirmed empirically against 579 ephemeris lines in the
legacy planner's own nightly output for 2026-03 to 2026-09:

| Sector | read as compass | read as raw MPC |
|---|---|---|
| N  | **5** | **228** |
| NE | 18 | 27 |
| E  | 98 | 10 |
| SE | 182 | 11 |
| S  | 228 | 5 |
| SW | 27 | 18 |
| W  | 10 | 98 |
| NW | 11 | 182 |

Read as compass, 88% of six months of real targets sit in E/SE/S, five of 579
fall in the north, and W/NW are nearly empty — matching "avoid north
completely", "above 20 in South and East", "West/Northwest = 40" and "before
it crosses meridian". Read as raw MPC, north would be the busiest sector of
all. Only the compass reading is consistent.

What remains open is the *numbers*, not the convention.

## 2. Upper elevation limit

You mentioned "lower **and upper** elevation" but gave no upper value. Fork
mounts commonly have a zenith blind spot. `MAX_ALTITUDE` is unset, so no upper
limit is applied at all.

## 3. Exposure floor

The rule itself is now known — `minutes = 10 + (V − 18) × 5`, **legacy**.

But it is unbounded below: at V=12.5 it returns −17.5 minutes, which the legacy
planner prints verbatim. We clamp at `EXPOSURE_FLOOR_MIN = 1.0`. What should
the real floor be? Is there also a maximum total integration per target?

## 3b. Exposure plan — frame length and frame count

**This is the biggest open question, and there is a complete implementation
waiting on it in PR #3 (not deployed).**

The legacy rule is magnitude-only and ignores sky motion entirely. Since a
moving target trails across the detector, motion is what really limits a single
frame. PR #3 sets frame length from the trailing limit and derives the count
from the legacy total:

```
t = 60 × TRAIL_BUDGET_ARCSEC ÷ motion(″/min)
n = legacy_total ÷ t,  capped at MAX_FRAMES
```

That reproduces ~44 frames for a typical target without anything being
hardcoded, which is close to the "about 48" observers describe.

**The decision needed.** The frame cap binds on **14 of 36** observable
targets. Fast movers then get far less integration than their magnitude asks
for — `A11GP9t` wants 12 minutes and receives 1. One of these must be true:

1. `MAX_FRAMES = 60` is too low; raise it and accept longer sequences.
2. `TRAIL_BUDGET_ARCSEC = 2` is too tight; loosening it lengthens frames and
   cuts the count.
3. Fast movers genuinely cannot be done to full depth in one sequence.

**Values that are guesses:**

| Setting | Default | Why |
|---|---|---|
| `TRAIL_BUDGET_ARCSEC` | 2″ | We hold no pixel scale for L01. Should it be seeing FWHM, or a pixel or two? |
| `MIN_EXPOSURE_S` | 1 s | Below this, readout dominates |
| `MAX_EXPOSURE_S` | 300 s | Guess for the slowest movers |
| `MAX_FRAMES` | 60 | Just above the typical ~44 |

Also worth asking: **what is the per-frame readout time?** We deliberately do
not model it, but at 1-second frames it dominates everything, and without it we
cannot report honest wall-clock time per target.

## 4. Confirm the recovered thresholds

All **legacy**, all now active:

| Setting | Value |
|---|---|
| `MIN_SCORE` | 25 |
| `MIN_ARC_DAYS` | 0.01 |
| `MAX_NOT_SEEN_DAYS` | 4 |
| `MAX_MAG` | 21.6 |
| `MIN_MOTION` | 0.7 ″/min |
| `MOON_SEP_MIN` | 20° |
| `SUN_ALT_MAX` | −15° |
| `MAX_SCATTEREDNESS` | (2000, 2000)″ |
| `NEO_ONLY` | q < 1.3 or e > 0.5 |

Note `MIN_ALT = 15` is effectively dead: we pass `oalt=20` to MPC (as the
legacy planner does), so the server never returns anything below 20°. The 15
can only matter if raised above 20. Worth deciding which number you actually
want.

## 5. Already-observed rule

The legacy planner discards any object whose astrometry already contains an
`L01` observation. We reproduce this (`SKIP_ALREADY_OBSERVED`). Should a target
really drop out after a single night, or should it come back for a second
epoch after some interval?

## 6. Does anything read the plan file?

We write `plans/YYYY-MM-DD.txt` in the legacy format every cycle. Is that file
consumed by a script or by the telescope software, or only read by a person? It
determines how strictly we need to match byte-for-byte.

One known difference: our lines omit the trailing `Map/Offsets` text. That is
HTML link text scraped along with the row, not data. Easy to add back if
something parses positionally.

## 7. Survey priorities

`HIGH_PRIORITY_SURVEYS` / `LOW_PRIORITY_SURVEYS` are empty. The tabular feed
does not carry the discovering survey; we guess it from the designation prefix,
which is a heuristic. Does survey affect your priorities in practice?

## 8. Ordering

We now order chronologically by time of best observable altitude, matching the
legacy planner. Our intrinsic score is a separate column. Is the chronological
sequence what you actually work from, or do you re-order by hand in practice?

---

## Worth a look in the current output

**Bright near-duplicate clusters.** A recent run surfaced six `SK000a*` objects
at nearly identical coordinates with V ≈ 12.5. Objects that bright on NEOCP are
unusual, and six co-located ones suggests one object submitted as several
tracklets, or satellite debris. Should we de-duplicate by position, or cap
brightness?

**Long-period comets.** `A11GrOJ` came through with e=0.998, a=2411 AU, i=126°
(retrograde) — a comet, not a NEO. It is correctly excluded by the q/e rule,
but only because we parse it at all; the legacy parser drops such rows before
the filter ever sees them. Are comets ever wanted?
