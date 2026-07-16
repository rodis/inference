# ADR 0005 — Session-gated derivation: the start entails the end

Status: **Accepted — implemented (`session_gated_window` engine; `got_out_the_car` migrated)**
Date: 2026-07-14

> Builds on [`0002-recursive-derivation.md`](0002-recursive-derivation.md) (derived events are
> valid pipeline inputs, fed back in-process) and the swappable-engine seam from
> [`0001-message-shaping-pipeline.md`](0001-message-shaping-pipeline.md). It adds a new engine
> strategy; the runtime is unchanged.

---

## Context

A derived event that pairs a **start** with an **end** (`got_into_the_car` → `got_out_the_car`
→ `car_trip`) had a fragile end-detector. `got_out_the_car` was a `weighted_window` requiring
`car_door_closed` **and** `device_disconnected_from_power` within 600s. That assumes the phone
stays on the wireless charger for the whole drive and comes off *at arrival*.

On **2026-07-13** three morning trips produced only **one** `car_trip`. Two never closed: the
phone was pulled off the charger a few minutes into each drive, so the only
`device_disconnected_from_power` fired mid-drive and had aged out of the window by the time
`car_door_closed` arrived (gaps of 1099s and 977s vs a 600s window). `car_door_closed` fired
correctly every time — the end-detector simply had no second signal to pair it with.

The deeper point: the end-detector was **blind to the fact that a trip was open**. We already
hold that state — `session_window` latches the open `got_into_the_car` until an end closes it —
but `got_out_the_car` re-derived "did we get out?" from scratch, ignoring it. Yet a start
*entails* an end: if you got into the car, at some point you'll get out. The start is standing
positive evidence for the end.

## Decision

Add a **`session_gated_window`** engine: a windowed end-detector whose sufficiency bar is
relaxed while an antecedent session is open.

```
fire  ⟺  trigger present  AND  ( session_open  OR  support_score >= support_threshold )
```

- **`trigger`** (`car_door_closed`) is **always required** — the guardrail against premature
  closing. Corroborating signals alone can never end a session.
- **`gate_event`** (`got_into_the_car`) latches "a session is in progress." While open, the
  trigger *alone* fires. The latch is **consumed on fire** (sequential sessions can't reuse it)
  and **expires after `max_open_seconds`** (stale-start protection, mirroring `session_window`'s
  `max_duration`).
- **`support_weights`** (the disconnect signals) is windowed corroboration, needed **only when
  no session is open** — so standalone detection stays as strict as the old `weighted_window`
  (backward compatible when no gate has fired).

### Why a latch, not a weight

The start and end are minutes-to-hours apart, far outside any co-occurrence window, so the start
can't be a windowed contributor. It's **state**, not correlation. And the existing windowed
engines keep the *earliest* sighting per contributor and never reset after firing (the reason
`session_window` is a separate strategy) — a `got_into` weight would linger and mis-fire the next
trip. `session_gated_window` instead keeps the *latest* sighting (fresh signals aren't shadowed by
stale ones, which also sidesteps the earliest-wins pruning hazard) and **resets its window + gate
on every fire**.

### Why keep `got_out_the_car` as its own event

The alternative — folding detection + pairing into `session_window` so `car_trip` derives straight
from `got_into` + end-signals — was rejected: it drops `got_out_the_car` as a first-class timeline
event and couples end-detection into the pairing engine. Keeping them separate means this was a
one-line `engine:` swap in the definition; `car_trip`'s `session_window` is untouched and pairs the
now-more-reliable `got_out_the_car` as before.

### Lineage

`got_out_the_car`'s `derived_from` is the trigger + present support signals (what the exit was
detected *from*). The gate (`got_into_the_car`) is **contextual evidence, not lineage** — the
start→end relationship is captured downstream by `car_trip`'s `session_window`.

### State duplication (accepted)

Both `session_gated_window` (for `got_out_the_car`) and `session_window` (for `car_trip`) latch the
open `got_into_the_car`. State is scoped per-definition and cross-definition reads aren't in the
architecture, so the latch is duplicated. Duplicating one small latch is cheaper than adding
cross-definition state coupling.

## Consequences

- **Positive:** the two lost 2026-07-13 trips now close on `car_door_closed` alone (gated path),
  independent of charger timing — re-verified against the recorded events. Generalises to any
  start→end derivation (future coffee-shop / gym / parked sessions get more robust ends for free).
- **Guardrail preserved:** `car_door_closed` stays necessary, so a stray mid-trip disconnect (or
  pair of them) can't split a trip.
- **Negative / watch:** if a genuine mid-trip `car_door_closed` can occur (locking the car at a
  gas stop / drop-off) while a session is open, it would close the trip early. Low risk in this
  domain (`car_door_closed` needs a `car_lock_state_change`), but it's the failure mode to watch.
- Backfill is not possible — only trips going forward benefit; the historical derived events would
  need replaying through the runtime.

## Alternatives considered

- **Widen the window** — wouldn't help; the 2026-07-13 gaps were 16–18 min.
- **Lower the threshold so `car_door_closed` alone always suffices** — removes the guardrail; a
  stray close ends trips even when we don't know one is open.
- **Add `device_disconnected_from_carplay` as an alternate `weighted_window` contributor** — the
  interim fix actually shipped first; it works but is still a correlation heuristic (relies on a
  disconnect landing near the close) rather than using the session state we already hold. Superseded
  by this ADR; `device_disconnected_from_carplay` is retained here as the no-open-trip corroborator.
