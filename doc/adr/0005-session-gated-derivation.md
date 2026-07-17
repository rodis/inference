# ADR 0005 — Raw-signal car derivation + session-gated ends

Status: **Accepted — implemented (`session_gated_window` engine; `car_door_*` removed; `got_into`/`got_out` derive from raw signals)**
Date: 2026-07-14 (revised 2026-07-17)

> Builds on [`0002-recursive-derivation.md`](0002-recursive-derivation.md) (derived events are
> valid pipeline inputs, fed back in-process) and the swappable-engine seam from
> [`0001-message-shaping-pipeline.md`](0001-message-shaping-pipeline.md). It adds one engine
> strategy and removes two intermediate definitions; the runtime is unchanged.

> **Revision 2026-07-17.** The original ADR added `session_gated_window` with a *required
> trigger* (`car_door_closed`) and kept the `car_door_*` layer. Two more missed trips showed the
> `car_door_*` layer itself was the fragility, so this revision removes it and reworks the gate
> from a required trigger to a *latched bonus weight*. The history is kept below for the reasoning.

> **Revision 2026-07-17 (charger anchor).** `got_into_the_car` originally used equal weights
> (5/5/5, threshold 10) — a plain 2-of-3. The park-and-settle **CarPlay flap** (CarPlay bounces
> connect/disconnect several times while parking, with a stale entry-unlock still in the window)
> fired it on the transient CarPlay+lock pair, minting an ~8s **phantom trip** whose `got_into`
> then **cooldown-swallowed the real trip's `got_into`**, so the genuine arrival never formed a
> `car_trip`. Fix: weight the wireless charger as the **anchor** (`device_connected_to_power` 6,
> CarPlay/lock 5 each, threshold 11) so entry requires the charger — the reliable "settled in to
> drive" signal — plus one corroborator; CarPlay+lock alone (10 < 11) no longer opens a trip.
> Trade-off: `got_into` is no longer tolerant of a *missing charger* (a trip where the phone never
> goes on the charger won't open) — acceptable because the charger is used on every observed trip,
> and it's the whole disambiguator between settling in to drive and a CarPlay flicker. Verified
> in-memory by replaying the 2026-07-17 raw stream: the phantom is gone and the real arrival trip
> forms. `got_out` is unchanged — during the flap it may still emit a stray exit with no open
> session (harmless: no `car_trip` forms, and its cooldown clears before the real arrival).

---

## Context

Getting in/out of the car derived through an intermediate `car_door_*` layer:

```
car_lock_state_change + device_connected_to_carplay      → car_door_opened  → (+power)  got_into_the_car
car_lock_state_change + device_disconnected_from_carplay → car_door_closed  → (+…)      got_out_the_car  → car_trip
```

Each `car_door_*` was an **AND of two signals** (weights summed exactly to threshold), and each
`got_*` then required **another** signal on top. So the path was a chain of ANDs, and any single
missing signal collapsed the whole cascade. `car_lock_state_change` is the flaky one — it only
fires when the lock state actually *changes*.

Three real misses, all the same shape:

- **2026-07-13** — two of three morning trips never closed. The phone came off the charger
  mid-drive, so the only `device_disconnected_from_power` had aged out of the window by arrival;
  `got_out_the_car` (then requiring `car_door_closed` + a charger-disconnect) had no second signal.
- **2026-07-16** — a trip never opened. At entry CarPlay + charger both fired but no
  `car_lock_state_change` did, so `car_door_opened` (which requires it) never fired → no
  `got_into_the_car` → `got_out_the_car` fired at arrival with no open session → no `car_trip`.

The deeper points: (1) the `car_door_*` AND-layer is a single point of failure feeding both
`got_*`; (2) a start *entails* an end — if you got into the car you'll get out — and that open-trip
state is standing evidence we already hold (`session_window` latches it) but the end-detector
ignored.

## Decision

**Remove `car_door_opened` and `car_door_closed`.** Derive both `got_*` directly from the three
raw signals, so no single flaky signal is mandatory:

- **`got_into_the_car`** — `weighted_window`, **charger-anchored** over the three raw entry
  signals: `device_connected_to_power` (6, the anchor), `device_connected_to_carplay` (5),
  `car_lock_state_change` (5); threshold 11. Any pair *including the charger* fires (power+CarPlay
  — the 2026-07-16 no-lock case; power+lock); the two flap-prone signals alone (CarPlay+lock = 10)
  do not (see the charger-anchor revision above). A single signal, or an exit (which carries the
  *disconnect* variants, not counted here), can't reach threshold. (Originally 5/5/5 threshold 10;
  retuned after the 2026-07-17 CarPlay-flap phantom trip.)

- **`got_out_the_car`** — `session_gated_window`: a 2-of-3 over the raw *exit* signals **plus a
  latched open-trip gate**. `got_into_the_car` opens the gate; while open it adds `gate_weight` to
  the score, so a single *reliable* exit signal can close a known-open trip.

### The engine: gate as a latched bonus weight (not a required trigger)

```
score = Σ weights[present windowed signals]  (+ gate_weight if a session is open)
fire  ⟺  score >= threshold
```

- `gate_event` (`got_into_the_car`) latches "a trip is in progress", is **consumed on fire**
  (sequential trips can't reuse it), and **expires after `max_open_seconds`** (stale-start
  protection, mirroring `session_window`'s `max_duration`).
- `gate_weight < threshold` and scoring only runs when a *windowed* signal arrives, so the gate can
  never fire on its own — at least one real exit signal is always required.

### Weight tuning is the safety mechanism

The three exit signals are not equally trustworthy, and two of them are dangerous as a single
"you left" cue, so the weights are asymmetric (threshold 10, `gate_weight` 4):

| signal | weight | + gate | rationale |
|---|---|---|---|
| `device_disconnected_from_carplay` | 6 | **10 → fires** | CarPlay tracks the car; a disconnect is a reliable exit |
| `car_lock_state_change` | 5 | 9 → no fire | direction-ambiguous, **shared with `got_into`** — a lock right after entry must not close the trip |
| `device_disconnected_from_power` | 5 | 9 → no fire | blips mid-drive when the phone comes off the charger |

Any **two** raw signals still reach 10 with no gate (6+5, 5+5), so a real arrival fires even with no
open session. Only the reliable single signal (CarPlay-disconnect) is trusted via the gate. This is
what makes it safe: a lock-change at entry (5+4=9) and a mid-drive charger unplug (5+4=9) both stay
below threshold.

### Why a latch, not a windowed weight for the gate

The start and end are minutes-to-hours apart, far outside any co-occurrence window, so the start
can't be a windowed contributor — it's **state**. And the windowed engines keep the *earliest*
sighting per contributor and never reset after firing (the reason `session_window` is separate) — a
`got_into` weight would linger and mis-fire the next trip. `session_gated_window` keeps the *latest*
sighting (fresh signals aren't shadowed by stale ones, sidestepping the earliest-wins pruning
hazard) and **resets its window + gate on every fire**.

### Lineage & state duplication

`got_out_the_car`'s `derived_from` is the raw exit signals it fired on; the gate
(`got_into_the_car`) is **contextual evidence, not lineage** — the start→end relationship is
captured downstream by `car_trip`'s `session_window`. Both `session_gated_window` and that
`session_window` independently latch the open `got_into_the_car`; state is scoped per-definition and
cross-definition reads aren't in the architecture, so duplicating one small latch is cheaper than
adding that coupling.

## Consequences

- **Positive:** all three missed trips are covered — verified in-memory against the real defs:
  July-16 entry fires on CarPlay+power (no lock); July-13 arrivals fire on CarPlay-disconnect+lock;
  a lone CarPlay-disconnect closes a known-open trip. Two fewer definitions; the entry and exit
  paths are now symmetric and each tolerant of any one missing signal.
- **Guards verified:** a lock-change at entry and a mid-drive charger unplug both stay below
  threshold (9 < 10), so neither false-closes an open trip.
- **`naive_bayes_window` is now unused** — `car_door_closed` was its only consumer. The engine stays
  registered (a valid strategy in the toolbox); nothing references it.
- **Dashboard leftovers:** `car_door_opened`/`car_door_closed` still appear in dashboard legend/level
  config (`view.ts`, `logical_levels.json`, compare defaults) — harmless (no data), cleaned
  separately in the dashboard component.
- Backfill is not possible — only trips going forward benefit.

## Alternatives considered

- **Keep `car_door_*`, just fix the ends** (the original 2026-07-14 decision) — left the AND-gate
  entry fragility in place; July-16 proved it insufficient.
- **Gate as a required trigger** (original engine design) — needs a single reliable "end" signal to
  mandate; with `car_door_*` gone there is no such signal, and the raw-2-of-3 + bonus-weight gate is
  simpler and covers the degraded single-signal case.
- **Uniform 2-of-3 with no gate at all** — simplest, and fixes every *observed* miss, but gives up
  the "one reliable signal closes a known-open trip" recovery for free; the bonus-weight gate keeps
  it at low cost.
- **Lower `got_out`'s bar so any single signal closes it** — a mid-drive charger blip or an entry
  lock-change would split/false-close trips.
