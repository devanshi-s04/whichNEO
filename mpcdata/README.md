# Vendored observatory reference data

Two static lookup tables, used to turn the observatory codes in MPC astrometry
into something a person can read.

| File | Source | What it gives |
|---|---|---|
| `ObsCodes.htm` | Minor Planet Center | code → observatory name and parallax constants |
| `details.txt` | Project Pluto, distributed with Find_Orb | code → observers, measurers, telescope |

Both are checked in deliberately. `details.txt` has no live source — it is
compiled by Project Pluto from MPEC headers and ships with Find_Orb — so it
must be vendored or lost. Vendoring `ObsCodes.htm` alongside it keeps the
lookups offline, deterministic and free of a startup fetch. Together they are
about 200 KB of plain text.

**Find_Orb itself is not used and not vendored.** Its binaries are
Windows/macOS only and would not run on our host; these are just its data
files. Nothing here is executed.

## Caveats

`details.txt` lists the people who *typically* observe at a site, harvested
from MPEC headers. The 80-column astrometry format records only the
observatory code — never the individual — so these names must never be
presented as the observer of a specific observation.

467 of 497 codes carry observer names. The large automated surveys (703
Catalina, G96 Mount Lemmon) list a telescope and no people, which is accurate:
nobody is at the eyepiece.

Both files drift as new sites appear and staff change. `ObsCodes` can be
refreshed from
<https://minorplanetcenter.net/iau/lists/ObsCodes.html>; `details.txt` only
from a newer Find_Orb release.

`ObsCodes.htm` uses LF, `details.txt` CRLF. The parser strips `\r`.
