"""The whole night's sky map, precomputed one picture per grid step.

Why this exists
---------------
The replay slider used to be one HTTP round trip per frame, each returning a
freshly rendered SVG. Even after the debounce and the memoised moon brought a
frame down to ~25 ms of server time, that shape cannot animate: the map only
moved once you stopped moving the handle, so scrubbing felt stepped, and there
was no continuous playback of a night at all. Nothing that has to ask a server
between one picture and the next can run at 30 frames a second.

So the night is computed once, ahead of time, and the browser only draws. The
backdrop -- disc, horizon mask, keep-out wedges, rings, rose -- never changes
with time and is already on the page; what changes is the moon, its exclusion
locus, and each target's marker. Those are precomputed on a two-minute grid
and shipped as one payload, after which scrubbing and playback make no request
at all.

The rule this module exists to keep
-----------------------------------
The browser must never decide what a marker means. Which targets are drawn,
which carry the poor-sky ring, which are rim ticks and which are removed
outright are this observatory's own limits applied position by position by
observability.keepout_violation and mask_violation -- a soft horizon limit
keeps the target and flags it, a hard keep-out wedge removes it because the
mount would hit the dome. A second copy of that reasoning in JavaScript is a
preview that can disagree with what is actually enforced, which is the one
failure this project has been careful to avoid everywhere else.

So what is shipped is not geometry for the client to interpret: it is the
finished SVG of the moving layer, exactly the bytes skymap.render_svg() would
have put inside its dynamic group. The browser's whole contribution is
assigning that string to one element. test_precomputed_frames_match_the_live_
route checks that byte for byte against what /skymap.svg?ts= serves for the
same instant, so divergence is not a thing to be careful about -- it is a
thing the suite fails on.

Where the work happens, and why it is split in two
--------------------------------------------------
build() is the expensive half and runs in the UPDATER, once per cycle, cached
in SQLite -- page loads do no astronomy, which is this project's central
architectural rule. Measured on the live board's eight-hour night at
two-minute steps (241 instants): 1.6 s for the moon, 66 ms for the markers.
A 300-second cycle does not notice it.

frames() is the cheap half and runs PER REQUEST, because the other half of a
marker is not about the instant at all. Its colour says whether the viewer
(or anybody) has observed that target, its number is its place in the queue
under the sort this viewer chose, and which targets appear at all depends on
this viewer's range filters. Baking those into the cache would mean marking a
target observed did not recolour the replay until the next cycle -- a replay
disagreeing with the board beside it. Measured: 47 ms for the live board's
six targets, 118 ms for a synthetic thirty.
"""

import observability
import skymap

# Two minutes. Chosen against the slider rather than by taste: the control is
# a few hundred pixels wide across a night of eight to ten hours, so at this
# step there is about one precomputed frame per pixel of travel -- the handle
# cannot be moved finely enough to land between two frames, which is what
# makes scrubbing continuous rather than merely fast. Halving it would double
# the payload to buy resolution no pointer can reach.
GRID_STEP_S = 120

# The fields of a target_marks() mark that describe the QUEUE ROW rather than
# the instant. These are the ones frames() re-injects per request, so they
# are deliberately NOT stored: see the module note above.
_PER_REQUEST = ("desig", "index", "observed", "vmag", "score")

PAYLOAD_VERSION = 1


def grid(start_ts, end_ts, step=GRID_STEP_S):
    """The sample instants of a night, on whole minutes.

    Whole minutes on purpose. observability.moon_state() quantises to the
    minute, so a grid landing on half-minutes would have the precomputed moon
    sit at a different instant from the one the live route draws for the same
    timestamp, and the two pictures would not match.
    """
    start = round(float(start_ts) / 60.0) * 60.0
    n = int((float(end_ts) - start) // step)
    return [start + i * step for i in range(max(n, 0) + 1)]


def build(rows, tracks, start_ts, end_ts, site=None, step=GRID_STEP_S):
    """Marks and moon for every instant of a night. The expensive half.

    `rows` fixes which targets the night was computed for, and that list is
    stored alongside the result: a request whose visible targets are not all
    covered must fall back to server rendering rather than quietly draw a map
    with a target missing from it.
    """
    instants = grid(start_ts, end_ts, step)
    moons = observability.moon_states(instants, site)
    marks = []
    for ts in instants:
        marks.append({
            m["desig"]: {k: v for k, v in m.items() if k not in _PER_REQUEST}
            for m in skymap.target_marks(rows, tracks, ts, site)
        })
    return {
        "version": PAYLOAD_VERSION,
        "step": step,
        "grid": instants,
        "moon": moons,
        "marks": marks,
        # Every target the night was built for, not merely the ones that
        # earned a marker somewhere in it: a target inside a keep-out wedge
        # all night long appears in no frame and is still fully covered.
        "built_for": [r["desig"] for r in rows],
    }


def covers(blob, desigs):
    """Can this payload draw every one of these targets?

    A subset is fine and is the normal case -- range filters and the
    observed/hidden toggles narrow what a viewer sees. A target the cache has
    never heard of is not fine: it appeared after the payload was built, and
    drawing the rest without it would show fewer markers than /skymap.svg
    would for the same instant.
    """
    if not blob or blob.get("version") != PAYLOAD_VERSION:
        return False
    return set(desigs) <= set(blob.get("built_for") or ())


def frames(blob, rows, site=None, localt=None, localdt=None):
    """One finished SVG fragment per grid instant, for this request's rows.

    The instant half of each marker comes from the payload, the queue-row
    half from `rows`, and the two are joined here. `rows` must be the same
    list, in the same order, that the live route would hand to
    target_marks(): the marker's number is its position in it.
    """
    common = {}
    for i, r in enumerate(rows, start=1):
        common[r["desig"]] = {
            "desig": r["desig"], "index": i,
            "observed": bool(r.get("observed")),
            "vmag": r.get("vmag"), "score": r.get("score"),
        }
    out = []
    for k in range(len(blob["grid"])):
        at = blob["marks"][k]
        # Row order, not payload order. target_marks() appends as it walks
        # `rows`, and the fragment has to be the same bytes it would have
        # produced -- markers overlap, so their order is what is drawn on top.
        marks = [dict(common[r["desig"]], **at[r["desig"]])
                 for r in rows if r["desig"] in at]
        out.append(skymap.dynamic_svg(marks, blob["moon"][k], site=site,
                                      localt=localt, localdt=localdt))
    return out
