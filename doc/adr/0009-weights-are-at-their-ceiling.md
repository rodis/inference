# ADR 0009 — Weights are at their ceiling: reject dynamic weights, prefer vetoes

Status: **Accepted — decision only, no code change.**
Date: 2026-07-29

> Revisits the weight-map tuning established in [`0005-session-gated-derivation.md`](0005-session-gated-derivation.md)
> and the car-native signal programme in [`0006-car-native-trip-signals.md`](0006-car-native-trip-signals.md).
> It changes no code. It records *why* a proposed generalisation of the weight map was rejected,
> and where the effort should go instead — so the same idea is not re-derived from scratch in
> three months.

## Context

Every phone- and car-side boundary signal we have is inconsistent, and by 2026-07-29 that was
measured rather than suspected. Ingest lag over 10 days:

| signal | n | p50 | p95 | max | spread |
|---|---|---|---|---|---|
| `car_lock_state_change` | 110 | 3.2s | 17.7s | **177.8s** | 55× |
| `device_disconnected_from_carplay` | 44 | 2.8s | 34.3s | **164.3s** | 59× |
| `device_disconnected_from_power` | 193 | 3.6s | 5.8s | **123.8s** | 34× |
| `car_driver_door_opened` | 63 | 2.8s | 3.5s | 4.5s | 1.6× |
| Overland sampling gap | 18 edges | ~62s | — | **710s** | 50× |

Flap, counted as transitions strictly inside a trip over 18 trips longer than 4 minutes: the
wireless charger **28** transitions across **8/18 trips (44%)**, CarPlay 3 across 2/18. Coverage:
the BMW CarData lane reaches only ~75% of drive starts and ~88% of ends (measured against CarPlay
sessions), because the car goes silent without retrying while the phone's producers queue.

Against that, four successive attempts to find a *more reliable* signal all failed:

- [#3](https://github.com/rodis/inference/issues/3) — the car's own lock descriptor is
  **direction-less**: it fires on drive-away *and* walk-away.
- [#33](https://github.com/rodis/inference/issues/33) — the car's WiFi association is directional
  but its offset to the boundary is **unstable**: −42s, +13s, +31s across three observations,
  because it tracks the hotspot coming up, not the human.
- [#23](https://github.com/rodis/inference/issues/23) — retiring the phone peripherals costs real
  trips; the non-peripheral pool cannot reach `got_into`'s threshold at all.
- ADR 0006's BMW door is the most punctual signal we have (max lag 4.5s) and is still only ~75%
  available at the boundary.

### The proposal

Given that, a reasonable inference: the assumption that *some* signal is reliable enough to weight
statically is the faulty part. Replace the static weight with a **dynamically derived** one — a base
weight modulated by how that event behaved in the window. An event that usually arrives in *x*
seconds scores 0.5·*y* when it arrives at *x*+10; an event that usually flaps *n* times per trip
scores 0.8·*y* when it flaps more. The search for the perfect signal then becomes unnecessary.

## Decision

**Rejected.** Weights stay static. The effort goes to physical vetoes and to structure that carries
direction, not to richer scoring.

## Why

### 1. The dominant failure mode is ambiguity, not unreliability

This is the decisive reason. The two phantom firings of this week:

```
#2  2026-07-28 11:05:14   car_lock_state_change@11:04:54   (lag 3.0s)
                        + car_driver_door_opened@11:05:14  (lag 2.6s)   = 10 → FIRED
#4  2026-07-29 08:07:11   device_disconnected_from_power@08:04:06 (lag 4.6s)
                        + car_lock_state_change@08:07:11        (lag 3.7s)   = 10 → FIRED
```

**Every contributor behaved perfectly** — prompt, within normal lag, no flap. A behavioural
discount would have scaled each by ~1.0 and both phantoms would have fired identically. They did
not fail because the signals were unreliable. They failed because a lock means "locked *or*
unlocked" and a door means "got in *or* got out". Dynamic weighting sharpens an axis that is not
the one cutting us.

### 2. Lateness is a property of the transport, not of the fact

A `car_lock_state_change` delivered 178s late still carries a correct `occurred_at`: it is true
evidence that a lock happened at T. Discounting it conflates delivery quality with evidential
quality — it penalises a real observation because the network was slow. (Where lateness *does* bite
is that the event may arrive after the decision was made; that is an ordering problem, not a
weighting one, and no discount fixes it.)

### 3. "How it behaved in the window" is not observable when the decision is made

Flap rate over a trip is only known at the *end* of the trip, but the decision fires mid-window. A
discount computed on a partial count makes the score **non-monotonic**: new evidence can lower a
score below a threshold that was already crossed and already fired on, which the cooldown and
`last_fired` machinery has no way to unwind.

### 4. It multiplies the tuning surface, on data that cannot fit what we already have

Each signal would need a base weight, an expected-arrival baseline, a discount curve, a flap
baseline and a discount magnitude — roughly six parameters where there is now one. We have **22
trips**. On 2026-07-29 the candidate `wifi_direction_6` returned metrics **identical** to current
(`real_trips` 10, `junk_trips` 0, `drives_missed` 0/10, `end_error` −42s), because the sample
cannot resolve the ~14 parameters already in play. And the baselines are **non-stationary**: the
BMW door's per-trip coverage drifted 33% → 94% over five days as the CarData stream settled, so any
"usual arrival time" is measured against a moving reference.

### 5. The mechanism already exists and nothing uses it

`decaying_window` — a weighted window whose contributions decay with event age (`half_life_seconds`)
— has been registered since the early runtime and has **zero consumers** across all 9 definitions.
A dynamic-weight engine has been available the whole time and no definition has ever needed it.
That is weak evidence, but it points the same way.

## What we do instead

Every weight change measured on 2026-07-28/29 failed or was neutral:

| candidate | result |
|---|---|
| `car_locked` as an exit signal | `junk_trips` 0→1, `real_trips` 10→8 |
| drop the ambiguous `car_lock_state_change` | `real_trips` 10→8, `junk_trips` 0→1 |
| demote `car_driver_door_opened` to fix #2 | `real_trips` 10→9, `junk_trips` 0→1 |
| retire charger + CarPlay | `real_trips` 6→4, `junk_trips` 0→1 |
| add the WiFi edges at 6/6 | **identical to current** |

In the same window, the one thing that *worked* was not a weight. `validated_session_window`'s
displacement guardrail suppressed a phantom **4 h 03 m** "trip" — the stale `got_into@11:23:30`
pairing with `got_out@15:26:30` — by observing that the bounding box over the whole session was
**23 m × 20 m** against `min_displacement_m: 300`, with 14 accepted fixes and 99.9% coverage, so it
was actively rejecting rather than abstaining.

It works because it is a **physical fact**, not weighted evidence: no threshold over noisy signals,
nothing to tune, and it cannot be defeated by a signal behaving oddly. The same shape is already
proposed in [#9](https://github.com/rodis/inference/issues/9) (two odometer readings inside a
session veto a junk trip) and is the reason `session_gated_window` works at all (a start *entails*
an end — structure, not statistics).

So: **the weight map is at its ceiling. Leverage is in vetoes and in structure that carries
direction.**

## The one piece of the proposal that survives

Discount by **timestamp uncertainty**, not by arrival lateness — and only where the uncertainty is
real and computable.

`ssid_edge` stamps its edge at the first ping that *observed* the change, so the true transition
lies somewhere in the preceding sampling gap: 14s in the good case, **624s** in the worst. That
uncertainty is known at derive time, has units (seconds), and bears directly on whether the event
belongs inside a 300s window. Discounting a ±624s-uncertain edge is defensible in a way that
discounting a late-but-exactly-stamped one is not.

Two caveats if this is ever built:

- `confidence_score` was deliberately removed from the data model (a cross-hop confidence scalar
  was the wrong shape; see `inference.event`). An uncertainty **in seconds** is a physical quantity
  rather than an arbitrary score, which survives the original objection — but it is close enough
  that the reasoning must be re-read first.
- It applies to exactly one producer today. The better fix for that producer is
  [#34](https://github.com/rodis/inference/issues/34) — a dedicated Shortcut that timestamps the
  actual transition, driving the uncertainty to ~0 instead of modelling it.

## Consequences

- The weight maps in `events/got_into_the_car.yml` and `events/got_out_the_car.yml` stay static.
  Tuning them further is explicitly **not** where effort goes.
- The search for a "more reliable signal" is closed as a *strategy*. It still produced value as
  *knowledge* — that `car_locked` is direction-less, the BMW door ~75% available, the WiFi edge
  offset-unstable — which any future scheme needs.
- New effort goes to: #9 (odometer veto), directional structure, and #34 (removing uncertainty at
  the source rather than modelling it).
- This ADR does not claim the weight map is *correct* — #2 and #4 are open bugs. It claims that
  richer weighting will not fix them, because those bugs are not weighting failures.

## What would reverse this

- A veto-based approach failing to fix #2 / #4 while a dynamic weight demonstrably does, adjudicated
  with `scripts/trip_eval.py` (`junk_trips` / `real_trips` / `drives_missed` / `end_error`) — never
  a count delta.
- Enough trips (order 100+, against today's 22) that a six-parameters-per-signal model could be
  fitted without overfitting.
- A failure mode that is genuinely *behavioural* — a signal that is right when prompt and wrong when
  late. None has been observed: the phantoms so far are ambiguity, and ambiguity is invariant to
  timing.
