# whichNEO for any observatory — plan

Today whichNEO is L01's board. The astronomy is general; the configuration is
not. This is the plan for making it serve any observatory, and the record of
what has been decided and why.

**Nothing here is built yet.** Planning document, written before any code
changes, so the decisions survive the conversation that produced them.

---

## Decided

| question | decision |
|---|---|
| who it is for | **self-service** — anyone signs up and adds their site |
| deployment | **one deployment, many observatories** |
| configuration | **a web settings page** |
| accounts | **an account can observe at several observatories** |
| keep-out wedges | **editable by the observatory**, since the value genuinely differs between sites — but protected, see below |
| night boundary | **ask the site** for its timezone |
| exposure table | **per site.** Luka's 36-frame speed table is Višnjan's, not a general rule |
| wedge safety | **edit in the UI, confirm by typing the obscode** — the pattern GitHub uses for deleting a repository |
| new sites | **immediate, with a cap** on total active sites |
| permissions | **owner edits settings, members observe** |
| L01 | **becomes site number one**, like any other |
| cap | **25 active observatories** — about 5 requests/minute to MPC |
| membership | **the owner invites by email**, reusing the existing invite flow |
| landing page | **your own site if signed in, L01 if not** |
| ranking weights | **per site, and shown in the settings page** |
| plan file format | **per site**, Višnjan's as the placeholder — but see the limits below |
| dormancy | **30 days without a sign-in**, emailed, reactivated on next login |

That is the most ambitious option on every axis, and worth naming plainly:
**it makes whichNEO a service, and you the operator.** Other observatories'
nights come to depend on epyc staying up, and their MPC traffic is spent from
your budget.

---

## What is L01-specific today

`config.py` is a module-level singleton with **113 `config.*` references
across 10 modules**. Every module reaches for a global. That is the thing
that has to change, and it cannot be done one module at a time.

Three categories, and the split is not obvious from reading the file:

**Site and telescope** — obscode, longitude and parallax constants, timezone,
field of view, the horizon mask, the keep-out wedges, the exposure table.

**Observing policy** — magnitude limit, moon separation, sun altitude,
digest2 floor, motion floor, not-seen limit, scatteredness limits, whether to
skip objects already observed from the site.

**Genuinely global** — MPC endpoints, timeouts, cache schema, sky-map
geometry, ranking weights (arguably per-site later).

---

## The data model

The heart of the design is that **some data is about the object and some is
about the object as seen from a site.**

| shared across all observatories | gains a `site_id` |
|---|---|
| NEOCP list | `targets` (observability is per-site) |
| raw astrometry | `ephemeris_cache` (fetched per obscode) |
| digest2 scores | `observer_state` |
| **ds42 scores** | `night_archive` |
| `neocp_history` and outcomes | plan files |

ds42 and the history staying shared is the efficiency win: scored once,
useful to every site. It is also why the research note is unaffected by any
of this.

### MPC load, measured rather than guessed

Only the **ephemeris** fetch is per-observatory. Measured over 300 cycles on
the live board: **1.1 fetches per cycle, ~306 per day per site.**

- 10 sites ≈ 2 requests/minute — fine
- 50 sites ≈ 10/minute — heavy but survivable
- hundreds — not acceptable without talking to MPC first

So the cap is a real number rather than a vague worry, and it is what the
sign-up limit should be derived from — see Permissions and sign-up below.

---

## Sign-up is mostly automatic

`mpcdata/` already answers most of what a new site needs:

- **`ObsCodes.htm` — 2722 codes** with longitude and parallax constants.
  Coordinates auto-fill for essentially any observatory.
- **`details.txt` — 676 codes**, 723 `TEL` lines. L01 returns
  *"1.0-m f/2.9 reflector + CCD"*, plus observers and measurers.

Field of view is not given directly, but aperture and f-ratio are, and some
entries name the detector (`4096x4096 CMOS`).

**An obscode is not optional.** MPC's ephemeris service is queried per
observatory code; without one there is no ephemeris, and therefore no board.
A site that has not yet been assigned a code cannot be served.

So sign-up is: enter an obscode → name, coordinates and telescope are filled
in → the observatory supplies its timezone, its horizon mask, its keep-out
wedges, its exposure table and its policy limits.

---

## The two kinds of restriction, and why the form must keep them apart

This is the subtlety most likely to be lost in a settings page.

**Soft — the horizon mask.** The target still appears, flagged. North at L01
is soft: Trieste's light pollution makes it a poor sky, but targets there stay
visible, because an impactor could be in the north. Removing them was
explicitly rejected.

**Hard — the keep-out wedge.** The target is removed entirely, because the
mount would hit the dome.

A form offering only "minimum altitude per direction" collapses these into
one number and silently picks an interpretation. An observatory with light
pollution to the north types a high number, and we either drop their northern
targets or let them point into a wall.

**Requirements this places on the schema:**

1. Soft and hard are separate structures, not a flag on one.
2. Each entry carries a short free-text **reason**. In six months, "light
   pollution" versus "the dome is there" is what tells someone whether it is
   safe to relax. That reasoning exists today only as a comment in
   `config.py`, and a database cannot hold a comment.
3. The board must keep saying which is which, as it does now: a soft
   violation shows an amber `sky` pill and the target stays; a hard violation
   removes it.

### Protecting the keep-out wedge

It must be editable — the value genuinely differs between observatories — but
it is the one setting where a typo points a telescope at a wall.

**Decided: saving a wedge change requires typing the observatory code**, the
pattern GitHub uses for deleting a repository. It makes an accidental save
nearly impossible without adding a second permissions layer on top of
owner-only editing.

Alongside that:

- the mask and wedges drawn live on the sky map as they are edited, so the
  shape is seen before it is saved
- the previous value kept, so it can be restored

---

## Night boundaries

`NIGHT_ROLLOVER_HOUR_UT = 11` is tuned for Croatia. "Tonight" stops being one
global thing once sites span longitudes, which touches the night archive, the
plan file and every timestamp the history records.

The site supplies its timezone (offered as a default derived from its
longitude, since the obscode gives that). The rollover hour follows from it —
the boundary wants to sit near local noon, not at a fixed UT hour.

---

## Staged plan

Each stage deploys on its own. A and B are invisible to observers.

**A — Extract a `Site` object.** `config.py` becomes the default site; every
module takes the site it is working on. No behaviour change, L01 still the
only site. Proven by the existing suite passing unchanged. This is the big
mechanical one: 113 references.

**B — `site_id` through the data model.** Still one site, so nothing visibly
changes, but the schema stops assuming.

**C — Several sites from files.** A site switcher appears. **Partner
observatories work at this point** — if the work stopped here, most of the
value is delivered.

**D — A settings page.** Read-only first, then editable, with the mask and
wedges previewed on the sky map as they are typed.

**E — Self-service.** Sign-up creates a site, a membership table lets an
account observe at several, and caps limit MPC exposure.

---

## Permissions and sign-up

**Owner edits, members observe.** Whoever created a site owns it; everyone
else marks targets and reads the board. One role boundary, simple to explain.

**Sign-up is immediate, capped at 25 active observatories.** A new site starts
working straight away; the deployment refuses new ones past the limit. 25 is
about 5 requests/minute and ~8,000 ephemeris fetches a day — comfortably
inside what a small institutional service can justify without asking anyone's
permission, with headroom to raise it once real usage is visible.

**Members are invited by the owner, by email.** This reuses the invite flow
already built: an account created with an unusable password and an emailed
single-use link. Nobody can attach themselves to an observatory they do not
work at.

**Ranking weights are per site and shown in the settings page.** A 0.4-m
telescope weighs magnitude differently from a 1-m, and an observatory should
be able to see what its sort is optimising rather than inherit Višnjan's
judgement invisibly. Defaults to L01's values so nobody has to think about it
on day one.

**Landing: your own site if signed in, L01 if not.** A member goes straight to
the observatory they work at; a stranger sees Višnjan, as today. Someone who
belongs to several needs a default, set on their account.

**L01 becomes site number one.** The migration turns today's `config.py` into
the first row of the sites table and nothing knows L01 by name afterwards.
Višnjan therefore runs the same code path as everyone else, so a bug in it is
found rather than hidden.

## Two implementation notes that follow from the shape

**The shared work must be hoisted out of the per-site loop.** ds42 scoring
and `neocp_history.record_cycle` are about the object, not the site. In a
cycle that iterates 25 sites they must run **once**, not 25 times — otherwise
the efficiency win of shared data is thrown away and the history records 25
identical snapshots per poll.

**Dormant sites should stop fetching.** A site whose owner has not signed in
for weeks still costs ~306 ephemeris fetches a day. Skipping sites with no
recent activity is what makes a cap of 25 mean 25 *working* observatories
rather than 25 registrations. It also gives a graceful way to let someone try
the service and drift away without permanently consuming a slot.

### Does the cycle still fit?

Today a cycle is ~3 s: one NEOCP fetch, ~1 ephemeris, ~90 ms of astropy for
122 targets, a plan file. Twenty-five sites means one shared fetch plus
~27 ephemeris fetches and 25× the analysis — on the order of 30–60 s, against
a 300 s interval. It fits, with room. The first thing to watch is not CPU but
the ephemeris fetches bunching when many sites go stale at once.

---

## The plan file: configurable, but only partly

Decided that a site supplies its own format, with Višnjan's as the
placeholder. Building it needs one distinction, because the file is two
different things stapled together.

**The ephemeris lines are fixed-width and column-aligned** — `{rh:02d}
{rm:02d} {rs:04.1f}` and so on in `output.py`, mirroring MPC's layout because
the legacy planner parses them by column position. An observatory's software
either reads that exact layout or it does not. A free-form template here
produces a file nothing can parse, so this part is **not** a free-text
setting. At most it is a choice between named layouts, once a second one
exists.

**The entry lines are metadata** — `* P12pWTl 36 x 30 sec`, `score=`,
`obs=`, `arc=`, `notSeen=`. What a site wants here genuinely varies, and this
is where "your own format" belongs.

### It must not be Jinja, and must not be str.format

This is user-supplied text rendered on our server, so the templating choice is
a security decision, not a convenience one.

- **Jinja** templates from users are a remote-code-execution vector. Not an
  option at any level of escaping.
- **`str.format`** allows attribute traversal — `{t.__class__.__init__.__globals__}`
  reaches the interpreter from a format string. Not an option either.
- **`string.Template`** is the right primitive: `$desig`, `$frames`,
  `$frame_sec`. No attribute access, no method calls, by design. Unknown
  names left alone with `safe_substitute` rather than raising at 3 a.m.

Whatever is offered, the settings page should render a live preview from a
real target, so a format is seen before a night depends on it.

---

## Dormancy

**30 days without a sign-in.** The site is emailed when it goes dormant,
saying it will reactivate when someone logs back in — not deleted, not
silently stopped.

That is what makes the cap of 25 mean 25 working observatories rather than 25
registrations, and it lets somebody try the service and drift away without
permanently holding a slot.

Details to settle when building it: whether any member's sign-in reactivates
or only the owner's, and whether a reactivating site refetches immediately or
waits for the next ordinary cycle (its ephemeris cache will be a month stale,
so: immediately).

---

## Open questions

None blocking. The next step is Stage A.
