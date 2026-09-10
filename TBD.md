# Open questions for Luka

Everything here is a placeholder in `config.py`. Nothing below was invented to
look complete — each item is a real gap, and each has a concrete default that
is almost certainly wrong in some way.

## 1. Dome / horizon geometry (highest impact)

The current mask came from observer notes that contain two direct conflicts:

| Sector | Azimuth | Currently using | Conflict in the notes |
|---|---|---|---|
| N  | 337.5–22.5° | **blocked** | "avoid north completely" — need the exact azimuth range that counts as north |
| NE | 22.5–67.5°  | 20° | from "above 20 in South and East" |
| E  | 67.5–112.5° | 20° | ” |
| SE | 112.5–157.5°| 20° | ” |
| S  | 157.5–202.5°| 20° | ” |
| SW | 202.5–247.5°| 30° | **never specified** — interpolated between S and W |
| W  | 247.5–292.5°| 40° | "below 30-40 degrees in the west" vs "West = 40" |
| NW | 292.5–337.5°| 40° | "northwest is 50 degrees" vs "Northwest = 40" |

Questions:
- Is the blocked north sector really 45° wide, or narrower/wider?
- Is the restriction a hard dome obstruction, or a practical preference?
- Is the real mask smooth rather than eight discrete sectors? Find_Orb supports
  a continuous horizon profile (`site_L01.txt`) — if one exists, we should use it.

## 2. Upper elevation limit

The notes mention "lower **and upper** elevation" but give no upper value. Fork
mounts commonly have a zenith blind spot. `MAX_ALTITUDE` is currently unset, so
no upper limit is applied at all.

## 3. Exposure rules

`EXPOSURE_SECONDS = 30` was specified. `EXPOSURE_COUNT = 6` is **our** choice —
it preserves the 180 s total from the `36 x 05 sec` example in the original
notes. The real rule almost certainly depends on target magnitude and sky
motion (a fast mover trails and needs shorter frames).

- What sets the number of frames?
- What sets the exposure length?
- Is there a maximum total integration per target?

## 4. Magnitude limit

`MAX_MAG = 21.7` came from the notes. Confirm this is for L01 (1.0-m f/2.9) and
whether it varies with moon, seeing, or target motion.

## 5. Moon rule

`MOON_SEP_MIN = 20°`, applied only when the moon is above the horizon. The notes
said not to over-engineer this. Does separation alone suffice, or should it
scale with lunar phase?

## 6. Ranking

Current score is intrinsic only — digest2 (weight 2.0), arc (1.5), magnitude
(1.5) — with altitude deliberately excluded so the ordering is stable through
the day. Unobservable targets sink below observable ones.

- Are these the right three factors?
- Are the weights sensible?
- **Known artifact:** because brighter scores higher without a cap, a cluster of
  very bright near-duplicate NEOCP entries can dominate the top of the list.
  See "Things to look at" below.

## 7. Survey priorities

`HIGH_PRIORITY_SURVEYS` / `LOW_PRIORITY_SURVEYS` are empty. The tabular NEOCP
feed does not carry the discovering survey at all — we currently guess it from
the designation prefix, which is a heuristic. If survey matters for ranking, we
need to decide whether it is worth pulling per-object observation data.

## 8. Meridian preference

The notes said "basically before it crosses meridian". That currently falls out
of the W/NW restrictions rather than being a separate rule, and pre/post
meridian is shown as a column. Should it also be an explicit scoring term?

---

## Things to look at in the current output

**Near-duplicate bright entries.** A recent run put six `SK000a*` objects at the
top: nearly identical coordinates (all within arcminutes), V ≈ 12.4–13.0,
digest2 83–94. Objects that bright on NEOCP are unusual, and six of them
co-located suggests either one object submitted as multiple tracklets, or
satellite/debris contamination. An observer would likely dismiss these
instantly — which is exactly the kind of rule we want captured. Options: a
brightness ceiling, a positional de-duplication pass, or a designation-prefix
filter.

**Stale positions.** NEOCP gives one position per object, not a live ephemeris.
For anything with a large "Unseen" value the listed position is well out of
date. The per-target page fetches a real MPC ephemeris on demand; the queue
view does not.
