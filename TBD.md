# Open questions for Luka

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

**Azimuths above are compass bearings from north.** MPC's ephemeris reports
azimuth from *south*; we convert on parse. Please sanity-check that your notes
meant compass bearings too — if they were MPC-convention, every sector is 180°
out.

## 2. Upper elevation limit

You mentioned "lower **and upper** elevation" but gave no upper value. Fork
mounts commonly have a zenith blind spot. `MAX_ALTITUDE` is unset, so no upper
limit is applied at all.

## 3. Exposure floor

The rule itself is now known — `minutes = 10 + (V − 18) × 5`, **legacy**.

But it is unbounded below: at V=12.5 it returns −17.5 minutes, which the legacy
planner prints verbatim. We clamp at `EXPOSURE_FLOOR_MIN = 1.0`. What should
the real floor be? Is there also a maximum total integration per target?

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
