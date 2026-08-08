# ADR 0011 — Claims, not certainties: detectors detect, one enriched journey carries graded support

Status: **Proposed — not implemented.**
Date: 2026-08-08

> Builds on [`0009-weights-are-at-their-ceiling.md`](0009-weights-are-at-their-ceiling.md) (ambiguity,
> not unreliability, is the dominant failure mode) and [`0010-trips-from-motion.md`](0010-trips-from-motion.md)
> (a trip is movement between two stays). Supersedes the framing of
> [#42](https://github.com/rodis/inference/issues/42) (retire `car_trip` or keep it): the answer
> proposed here is *neither* — both stay, demoted to detectors, and the event consumers see is derived
> from them. Absorbs the corroboration work of [#46](https://github.com/rodis/inference/issues/46)
> (whose gap-tolerance rules relocate into the pairing proposed here) and gives
> [#47](https://github.com/rodis/inference/issues/47)'s adjudicator a single seam to patch.

## Context

### The castle

Every engine today has fire-or-don't logic: contributors either match and mint an event, or expire
unobserved. Derivation then **chains those binaries**: `got_out_the_car` collapses "lock + door,
which is genuinely direction-ambiguous" into a hard yes; `car_trip` pairs two such yeses; `vehicle`
until #46 required both to land inside a 60-second window. Each level thresholds early and discards
what it knew, so downstream inherits a certainty that was never in the evidence. When a piece is
missing the whole claim vanishes (#36's lost drives — a real 13-minute drive with no event at all);
when a piece is wrong the claim is confidently false (#2's phantom exit at entry, #38's
time-inverted session, #26's sub-2-minute phantoms). The defect family is the *shape* of the
architecture, not a tuning residue.

The history reads as whack-a-mole because it was: the BMW door was added to check the lock (ADR
0006), the gate was added to check the door (ADR 0005), displacement was added to check the gate
(#23). Each new sensor carries its own failure modes, which the next addition is recruited to
validate. ADR 0009 closed the search for a *more reliable signal* as a strategy; this ADR addresses
the remaining half of the problem — that even reliable signals are being forced through
prematurely-binary decisions.

### The proposal this ADR evaluates (and where it came from)

Accept that derived events are not certainties. Two versions were on the table:

1. **Scalar confidence, propagated.** `got_into` happened with probability *x*, `got_out` with *y*,
   so `car_trip` with *f(x, y)*, and `vehicle` with *z* derived from that — thresholds pushed to
   consumers, `confidence_score` (removed from the data model) resurrected as a float.
2. **Structural split + graded support.** Detectors emit binary *claims* judged by physical
   guardrails, exactly as today — but nothing consumers see is a bare chained binary: one top-level
   journey event is derived from the *union* of detectors, enriched with all capabilities, and
   carries a **support grade derived from the topology of its evidence** rather than a fitted
   number.

## Decision

**Version 2.** Concretely, four connected changes:

### 1. Detectors detect; the inference is its own event

- `trip` becomes (or is renamed to reflect) **pure geometry**: "this entity displaced from one
  settled cluster to another", with its known limits (needs ≥ `min_fixes`, arrival knowable only
  `settle_seconds` late). It sheds its capabilities *and* the #46 corroboration machinery — no
  `corroborating_events`, no marks. A detector.
- `car_trip` becomes **pure peripherals**: "it appears you entered and exited your car across this
  span" — prompt (fires on `got_out`), Overland-independent, direction-fuzzy. Also a detector; also
  sheds capabilities.
- A new top-level event (working name **`drive`**; naming is an open question below) is derived
  from both, and **it alone is enriched**: `interval`, `journey`, `vehicle`, and the new `support`.
  Capabilities live where the meaning lives — today `interval` exists on both `trip` and `car_trip`
  with subtly different meanings (movement span vs session span), which is the "two meanings for
  one field" trap in permanent, structural form.

### 2. Union, not intersection

The top event is minted when **either** detector fires — geometry alone (a borrowed car, a train:
ADR 0010's founding case), session alone (an Overland outage: #42's measured ~7% fallback,
contingent on one iOS app's health), or both (the common case, and the only route to
`vehicle: confirmed`). "Travelling in my own car" is the corroborated case of one event, not a
separate event. This is what dissolves #42: nothing retires, and the month-of-data question it was
waiting on stops mattering.

### 3. Evidence flows through recursion

Today the router re-enqueues a **clean envelope** — a downstream engine consuming `trip` sees
`{id, name, user_id, timestamp}` and nothing else (the documented gotcha in `core.md`). A fusion
engine cannot derive `journey` from that; it needs the fixes. The change: a derived event re-enters
the router **carrying its decision's sources**, and a downstream engine may fold them into its own
decision. Capabilities still exist only where declared, so nothing changes for any current
definition (`trip.yml` and `car_trip.yml` simply declare none); the envelope stays clean of
*semantics* — what propagates is evidence, append-only up the DAG. The pairing logic that folds a
car session into a geometric span inherits #46's evidence-gap tolerance wholesale (a cold-start
entry and a parking-search exit sit minutes outside any fixed window regardless of which level does
the pairing), and `scripts/vehicle_eval.py` is already the adjudicator for it.

### 4. Graded support, not scalar confidence

The top event carries a `support` field — an **enum derived from evidence topology**, e.g.
`corroborated` (independent evidence kinds agree: geometry + car session), `single_source` (one
detector, nothing contradicting), with the exact levels to be fixed at implementation. This is
`confidence_score` resurrected in the one shape that survives its removal: not a probability, but a
statement of *what kind of evidence backs this claim*, deterministic, explainable ("corroborated:
CarPlay + door landed in-gap"), and re-derivable. Consumers own their thresholds — the timeline can
draw a `single_source` journey paler instead of not at all; the outbound action lane (#18) can
require `corroborated`. `vehicle`'s absent/one-sided/confirmed ladder and `session_gated_window`'s
context-conditioned bar show the system has been converging on exactly this, discretely, without
ever needing a calibrated number.

## Why scalar confidence was rejected — again, and on the record

Version 1 fails for reasons ADR 0009 already measured; they transfer intact:

1. **Ambiguity is invariant to scoring.** The July phantoms fired with every contributor prompt,
   in-lag and flap-free. P(evidence | real exit) = P(evidence | phantom at entry) when the sensors
   are direction-blind, so the phantom and the real event get the *same score* under any scheme.
   A confidence architecture emits confident phantoms.
2. **Calibration has nowhere to come from.** One user, ~2 drives a day, 22 labelled trips at last
   count. `naive_bayes_window` — which emitted a genuinely calibrated posterior — was removed
   2026-07-27 with one consumer and its distinguishing feature unused. Hand-assigned percentages
   are weights with more decimal places and false authority; fitted ones are ADR 0009 §4 (the
   sample cannot resolve the parameters already in play).
3. **The inputs are correlated.** Lock, door and CarPlay fire from one physical act; multiplying
   them as independent evidence is flatly wrong, and *f(x, y)* compounds whatever miscalibration
   the inputs carry.
4. **Thresholds relocate; they do not dissolve.** The dashboard either draws an event or doesn't;
   Pushcut either notifies or doesn't. Moving the cut to consumers is good (presentation is already
   a consumer concern) — but that argument is satisfied by a three-level enum exactly as well as by
   a float, without inventing numbers no eval can adjudicate (`trip_eval` judges fired/didn't;
   judging "was 63% right" needs proper scoring rules and ~50× the ground truth).

## Consequences

- **What dissolves:** #42's deadlock (nothing retires); the dashboard double-draw (one enriched
  event to render); the castle chains — a wrong or missing boundary can no longer erase or invert
  the authoritative journey, only degrade its `support`/`vehicle`.
- **What it costs:** the recursion invariant is revised (evidence sidecar propagates — `core.md`
  §gotcha and `invariants.md` need matching edits); fusion state holds pending detector output until
  pairing resolves (bounded by the pairing timeout); the top event inherits `trip`'s latency
  (+6.4 min mean vs `car_trip` — the action lane therefore keeps consuming `car_trip` directly);
  history needs a rederive once shipped; the dashboard migrates to the new event.
- **What stays true:** detection remains deterministic and replayable (`backtest.py` /
  `vehicle_eval.py` / `rederive.py` unaffected in kind); physical guardrails keep judging the
  detectors exactly as today; ADR 0009's "vetoes and structure, not richer scoring" is *extended*
  by this ADR, not contradicted — the grade is structure made visible.
- **Open questions:** naming (keep `trip` as the consumer-facing event and rename the geometry
  detector, vs mint `drive`/`journey_event` and freeze `trip`— history continuity favours the
  former, honesty of scope the latter); the exact `support` levels; whether `car_trip`'s
  `validated_session_window` displacement guardrail stays (it should — the detector must still not
  emit physically-impossible sessions).

## What would validate it

Replay-adjudicated, before any deploy, on the same footing as #46:

- The fused event must reproduce **≥ 19/27 own-car `confirmed` with the borrowed-car legs clean**
  (the 2026-08-08 `vehicle_eval` baseline) — the refactor may not cost recall the current
  architecture already has.
- The union must demonstrably produce a journey from each side alone: the borrowed-car vet legs
  (geometry only) and a simulated Overland outage over a week with known drives (session only).
- Timeline count per day must not inflate: one physical journey → one fused event, adjudicated
  against the 25-day replay.

## What would reverse it

- The fusion pairing turning out to need per-signal tuning that exceeds what the two detectors
  needed separately — the point is to *remove* load-bearing pairing, and if it reappears inside the
  fusion engine the split bought nothing.
- A measured need for finer gradation than the enum can express — genuine downstream consumers
  making *different decisions* at (say) five distinct support levels — which is the point at which
  the scalar debate reopens, on ADR 0009's terms (order-100+ labelled trips first).
