# ADR 0006 — Car-native trip signals (BMW CarData), fused via an HA-independent subscriber

Status: **Accepted — implemented, but scaled down by vehicle reality.** Subscriber deployed 24/7
([`workers/bmw-cardata/`](../../workers/bmw-cardata/)); `car_driver_door_opened` fused into
`got_into`/`got_out` as an independent corroborator. See the **Outcome** banner.
Date: 2026-07-18 (scaffold 2026-07-19, deployed + tuned 2026-07-24)

> **Outcome (2026-07-24), after live deployment on the target BMW X1 sDrive20i:**
> The original plan — a car-native **`isMoving`/ignition anchor** — is **not achievable on this
> vehicle**: over 6 days / ~30 trips with streaming enabled, `vehicle.isMoving` and the engine
> descriptors **never streamed** (nor populated REST). This X1 emits *discrete state-change
> batches* (doors, windows, lock, alarm, GPS snapshot, odometer) around wake/park — not continuous
> motion telemetry. The end-anchor design below (motion-off + door, ignition-off) is therefore
> moot. **What survived:** `car_driver_door_opened` — the one reliable, phone-**independent**
> signal. Its coverage improved as the stream settled (33% → **~94%** per-trip, 07-21→24), so it's
> added to both weight maps as a **corroborator** (weight 5, sub-threshold alone; ADR-0005 guards
> preserved). Because it's non-directional (fires on entry *and* exit) it can't fire a trip alone —
> it's insurance for a phone-signal miss, validated safe/neutral by a **backtest** over 21 days of
> real history (`scripts/backtest.py`; car_trip 66→68, re-times a few multi-leg days, no regression).
>
> **Correction (same day):** that validation was *count*-based and therefore missed a real
> regression. At weight 5 the door opened a new phantom path on the entry side — `power + door` = 11
> — because the door is non-directional and the wireless charger re-seats mid-drive, so the pair
> fires at the *exit*. "car_trip 66→68" hid it: the added trips were partly junk (0–110s spans). The
> door's weight in `got_into_the_car` is now **4** (ADR 0005 rev 2026-07-24); in `got_out_the_car` it
> stays 5. The lesson is in the tooling: a count delta can't adjudicate quality, so
> [`scripts/trip_eval.py`](../../scripts/trip_eval.py) now scores junk-trips / real-trips /
> drives-missed, and is the check to run before changing these weight maps.
> The MQTT envelope, broker params (MQTT v3.1.1 / TLS 1.3 / `{gcid}/+`), the portal
> **"Configure Stream"** per-descriptor gotcha, and the refresh-token-rotation → Neon-persistence
> fix are all captured in [[project-bmw-cardata-onboarding]].
>
> **Addendum (2026-07-26) — half the container was never being read.** Reviewing the whole BMW
> surface rather than the door alone: the container subscribes to **8** descriptors, the mapper
> handled **4**, and Neon has ever seen **1**. Those two gaps are different failures.
> `isMoving`/ignition/deep-sleep are genuinely dead on this vehicle (above). But
> `vehicle.cabin.door.lock.status`, `vehicle.vehicle.travelledDistance` and
> `…currentLocation.latitude/longitude` were **subscribed and silently discarded** — the mapper
> matched four descriptor ids and `continue`d past everything else with no log, and the one path
> that would have revealed them (`BMW_DEBUG_LOG_ALL`, whole envelopes) was never enabled in
> production. So we could not distinguish "the car never sends it" from "we never looked". Two
> changes follow:
>
> 1. **A permanent stream inventory.** Unmapped descriptors are logged once per id per process,
>    with their value, *before* the baseline check (so a descriptor seen exactly once still shows
>    up). One drive now enumerates the stream, at a cost of a handful of log lines.
> 2. **`vehicle.cabin.door.lock.status` mapped → `car_locked` / `car_unlocked`**, emitted but in
>    **no weight map** — data first, weights after a replay. It is worth the speculative wiring
>    because it is **directional**, and non-directionality is the binding constraint on this whole
>    lane: the driver door fires at entry *and* exit (hence weight 4 in `got_into_the_car`, after
>    the phantom it opened at 5), and the phone's `car_lock_state_change` has the same defect —
>    it's the reason for the accepted [door open/close disambiguation] fragility. A car-native
>    lock *state* would be the first signal that is both phone-independent and knows which way it
>    went. `SELECTIVE_LOCKED` and friends are deliberately left unmapped and logged rather than
>    guessed at.
>
> **First inventory (2026-07-27, one drive).** It paid for itself immediately, and mostly by
> contradicting this ADR. **24** unmapped descriptors — the container is far larger than the 8
> recorded above — and **two of the ids recorded here were wrong**, transcribed from the
> kvanbiesen source and never checked against the live stream: the central lock is
> `vehicle.cabin.door.status` (`"SECURED"`), *not* `…door.lock.status`, and GPS lives under
> `vehicle.cabin.infotainment.navigation.currentLocation.*`, *not* `vehicle.currentLocation.*`.
> The first is why the `car_locked`/`car_unlocked` mapping added the previous day never fired —
> it was watching an id this car does not have. Fixed 07-27; the full descriptor table lives in
> the [subscriber README](../../workers/bmw-cardata/README.md).
>
> Confirmed arriving: odometer (`travelledDistance`), GPS lat/lon/altitude, **fuel level**
> (`vehicle.drivetrain.fuelSystem.level` — not previously known to exist, and a level *jump* + a
> stay = refuelled), the trunk, all four doors, windows, sunroof, alarm arm-status, preconditioning.
> So the "container expansion" idea below is moot: tailgate, passenger doors and fuel are already
> subscribed. Nothing to add — just map what's already there.
>
> **Caveat on the inventory's resolution.** Once-per-process gives the stream's *vocabulary*, not
> its per-trip cadence: the first message after connect is a full state dump, so nearly every id
> was logged from that rather than from driving. Which descriptors actually *change* at entry/exit
> still needs mapping them (or a temporary `BMW_DEBUG_LOG_ALL` window).
>
> **Unrelated bug the same drive exposed — `{lock, door} = 10` fires `got_out_the_car` at ENTRY.**
> `car_lock_state_change` (5) + `car_driver_door_opened` (5) hits `got_out`'s threshold of 10 with
> no gate, and *both are non-directional*, so an entry unlock followed by the entry door-open
> fires an exit. Live on 2026-07-27: `got_out_the_car` at 08:33:04 from exactly that pair, 17s
> **before** the `got_into_the_car` for the same entry. Observed 4× since the door joined the map
> on 07-24. It has been harmless so far only by luck — each real arrival happened to fall outside
> the phantom's 600s cooldown (today's drive was 14m23s). On a **sub-10-minute drive the cooldown
> would swallow the real arrival**, leaving the trip open until something stale closed it or
> `max_open_seconds` expired: the same failure shape as the window_seconds bug fixed 2026-07-25.
> This is the precise mirror of the *entry*-side phantom that ADR 0005 rev 2026-07-24 fixed by
> demoting both ambiguous signals to 4 in `got_into_the_car` — the identical pair on the exit side
> was left at 5, so the fix only covered half the symmetry. Not changed here: a weight change is
> exactly what `scripts/trip_eval.py` exists to adjudicate (junk trips / drives missed), and
> reasoning from 4 observations is how the door's first validation went wrong.
>
> Still unmapped, and deliberately: the odometer and GPS pair. Both are **numeric snapshots, not
> edges**, so they don't belong in this boolean mapper — they are capability/enrichment material
> (trip distance; a park-location `place` for trip endpoints; a phone-independent
> `arrived_home_by_car`). The odometer additionally gives *ground truth* for the junk-trip
> problem: a 0–110s phantom covers ~0 km, which adjudicates trip quality directly instead of by
> duration heuristic (the failure mode that made the first door validation wrong — see the
> Correction above). Worth adding to the container later: tailgate (loading/unloading — the "did a
> shop" shape), passenger door ("not alone"), fuel level (a jump + a stay = refuelled).

> Builds on [`0005-session-gated-derivation.md`](0005-session-gated-derivation.md): the same
> `got_into_the_car` (`weighted_window`) / `got_out_the_car` (`session_gated_window`) →
> `car_trip` (`session_window`) cascade. This ADR **adds a second, independent signal source**
> (the car itself) as new weighted contributors to those two windows. No engine change, no new
> Kafka topic, no runtime change — a new producer and retuned weight maps.

## Context

`car_trip` derives entirely from **iPhone proxies** — CarPlay connect/disconnect, the phone's
`car_lock_state_change`, the wireless-charger connect/disconnect, geofence. ADR 0005 is a record of
how fragile that is: three missed trips, a phantom trip, and a standing accepted fragility
([door open/close disambiguation]) all trace to the same root — **every phone signal shares one
failure domain, the phone.** If the phone is dead, left behind, or backgrounded, we don't lose one
contributor, we lose *all of them at once*. Adding more phone-derived signals doesn't fix this:
they're correlated, they fail together. What raises reliability is a **second, independent source.**

The car is that source. BMW **CarData** exposes an official MQTT stream (broker
`customer.streaming-cardata.bmwgroup.com:9000`, MQTT v5/TLS, OAuth2 device-code auth; free in the
EEA) carrying car-native telemetry — motion (`isMoving`), location, odometer, doors/lock, charging.
These come off the car's own SIM and do not care about the phone's state. The vehicle is confirmed
eligible (already streaming into a Home Assistant instance).

Two hard facts shaped the decision:

1. **One connection per GCID.** *(Deploy consequence, learned 2026-07-25: this also rules out a
   rolling update. `maxSurge` starts the new subscriber while the old one still holds the only
   permitted connection, so they evict each other — surfacing as `mqtt connect failed: Not
   authorized` for ~56s until the old pod dies. The `bmw-cardata` Deployment is therefore pinned
   to a no-overlap rollout (`maxSurge: 0`) via a kustomize patch; see `deploy/inference/kustomize/base/bmw-cardata/`.
   That bounded the overlap but didn't remove it — the subscriber also had to shut down cleanly.
   It had NO SIGTERM handler, so Python's default action killed it outright and `stop()` never
   ran; and `stop()` called `loop_stop()` before `disconnect()`, so the DISCONNECT packet could
   never be flushed by the thread that writes it. Both fixed 2026-07-25: the broker is now told
   the session is over instead of having to notice a dead socket.)*
   The MQTT username is the account-level **GCID**, not the per-client
   `client_id`; the topic is `{GCID}/{VIN}`. Two direct subscribers on the same account evict each
   other. So our subscriber and HA's direct subscription are **mutually exclusive** — creating our
   own CarData client does not dodge this (same GCID). The sanctioned multi-consumer pattern is a
   single upstream subscriber that fans out.
2. **No first-class trip or ignition event, and `isMoving` is start-good / end-bad.** CarData does
   not emit "trip started/ended." `isMoving` false→true is a clean **start** anchor, but the car
   goes `isMoving=false` at every red light and in every traffic jam — so raw `isMoving`-false is
   **not** a trip end. A naive "stopped → close the trip" would slam `car_trip` shut at the first
   stop light (`got_out`'s 600s cooldown makes the *first* stop win).

## Decision

### Ownership & topology — the machinery is ours, HA is demoted

The inference logic and its infrastructure live on **our** side. We run the **sole** BMW
subscriber; **HA drops its direct BMW subscription** (chosen 2026-07-18 over running our own broker
to fan out to HA — minimal infra, no broker). HA is no longer in the critical path; it may later
become a *source* that feeds us or a *sink* we push to over MQTT, but never load-bearing.

```
BMW MQTT ({GCID}/{VIN})
      │  (OAuth device-flow, hourly ID-token refresh, reconnect — all owned by the subscriber)
      ▼
  bmw-cardata subscriber pod ──POST /bmw/cardata (X-Limit-U: user_id)──► Vector
      │                                                                    │ bmw_to_canonical adapter
      ▼                                                                    ▼
  (VIN → user_id map)                                                  raw_sensors
                                                                            │
                                                                            ▼
                                    got_into_the_car / got_out_the_car ──► car_trip
```

### The subscriber (new worker: `workers/bmw-cardata/`)

A thin **transport + auth** component — deliberately *not* where trip logic lives:

- Owns the OAuth 2.0 **device-code flow** (one-time, to mint the refresh token) and the ongoing
  refresh loop — the **ID token is the MQTT password and expires hourly**, refresh token lives 2
  weeks — plus reconnect/backoff.
- Subscribes `{GCID}/{VIN}`, maps each descriptor **edge** to a canonical signal, and POSTs it to
  Vector's HTTP ingest at `/sensors/bmw`. It does **no** trip derivation — only descriptor→signal
  naming. **Refinement during scaffolding:** because we control this producer's body, it emits the
  canonical `{"payload":{event_name,user_id,timestamp}}` shape straight to the existing `standard`
  sensors lane (2nd path segment `bmw` ≠ `owntracks` → `shape_sensor`), so **no new Vector transform
  is needed** (unlike OwnTracks, whose bespoke adapter exists only because its body is a fixed 3rd-party
  shape). Still adds a producer, not a topic.
- Carries `user_id` via the `X-Limit-U` header (VIN→user_id map in config), so events land on the
  right entity/partition — same convention as OwnTracks.
- Secrets (Doppler/K8s): `client_id`, `GCID`, refresh token, VIN→user_id map. Auto-discovered
  `Dockerfile` → `inference-bmw-cardata` image → its own `values.yml` under the runtime app
  (existing publish-images pattern; no new ArgoCD app).

Container-budget note: containers are capped at **10 per account** (not per client) — reclaiming
HA's frees room. Select the trip-relevant descriptors on our client.

### The fusion — start and end are asymmetric

New **car-native** contributors are added to the ADR-0005 weight maps, tuned so **each source is
independently sufficient** (car alone can fire; the phone quorum still fires when the car is silent —
underground, coverage gap, lapsed subscription), and no single ambiguous signal fires.

**`got_into_the_car` — `car_started_moving` as a self-sufficient anchor.** `isMoving` false→true is
authoritative and phone-independent. Give it weight **≥ threshold** so it single-fires `got_into`;
the 600s cooldown swallows the red-light re-starts that follow. Keeping threshold 11:

| signal | weight | rationale |
|---|---|---|
| `car_started_moving` | **11** | car-native, independent — **fires `got_into` alone** |
| `device_connected_to_power` | 6 | phone anchor (ADR 0005) — power+CarPlay / power+lock still = 11 |
| `device_connected_to_carplay` | 5 | phone corroborator |
| `car_lock_state_change` | 5 | phone corroborator |

This directly retires the 2026-07-16 "trip never opened" class *and* the 0005 limitation that entry
required the charger — the car opens the trip on its own; the phone quorum is now the degraded-mode
fallback.

**`got_out_the_car` — car END anchor is a *park-confirm*, NOT raw `isMoving`-false.** Because the car
stops at every light, the reliable car-native end is a **single-per-trip** park event. The target
vehicle (**BMW X1 sDrive20i, petrol**) rules out the charging-based park-confirm, and its current
CarData container selection exposes only binary state signals (motion, doors, hood/tailgate, alarm,
deep-sleep) — no lock/ignition/odometer. So the park-confirm is a **two-signal combo immune to the
red-light case**:

> `car_parked` ⟺ `vehicle_motion_state` off **and** `door_state_front_driver` open (co-windowed)

At a red light the car goes `motion→off` but the driver door stays shut (no fire); on arrival
`motion→off` *and* the driver door opens. `vehicle_deep_sleep_mode → on` is a **certain-but-slow**
backstop (the car deep-sleeps minutes after you walk away — good for confirmation, too late to be the
timely trigger). This `car_parked` feeds the `session_gated_window` (threshold 10, gate_weight 4)
weighted to close a trip on its own. Raw `car_stopped_moving` (`motion→off` alone) is at most a *weak
corroborator*, never enough to fire alone. The existing phone exit signals are unchanged.

If, when provisioning our own client, the X1 catalogue offers **central-lock** or **odometer**, add
them: a car-native lock-locked is a cleaner single-shot end, and monotonic odometer confirms a trip
happened and its distance. **Location** is also worth a container — it feeds the existing
`geofence` engine (`arrived_home_by_car` / `left_home_by_car`) from the car instead of the phone.
All subject to the 10-container-per-account cap.

`car_trip` (the `session_window`) is **untouched** — it just receives more reliable start/end events.

## Consequences

- **Positive:** trip start becomes phone-independent (the biggest, safest win — covers every 0005
  "never opened" miss without leaning on the charger). End gains an independent car-native
  corroborator on top of the reliable CarPlay-disconnect.
- **Graceful degradation both ways:** car silent → phone quorum still fires; phone silent → car fires.
- **Infra cost is one pod**, no broker, no new Kafka topic (subscriber is a producer into
  `raw_sensors`). HA is removed from the path.
- **Single-brand / EU-only / subscription-dependent:** non-BMW cars and lapsed ConnectedDrive/SIM
  fall back to the phone signals — which is why fusion, not replacement.
- **HA loses its BMW entities** (accepted 2026-07-18).

## Scope: read-only telemetry (no actuation)

BMW CarData is a **data-sharing** product — read-only. Its entire API surface is `GET`
telemetry (`telematicData`, `basicData`, `chargingHistory`, `image`, `vehicles/mappings`, …)
plus `POST`/`DELETE /customers/containers` to configure *which data you receive*. There are
**no command endpoints** — no lock/unlock, no climate/preconditioning start, no remote engine
start, no flash/honk, no door open. (The `lock`/`precondition`/`start` tokens in the spec are
descriptor *names*, i.e. readable state, not actions.) Verified against
`swagger-customer-api-v1.json` 2026-07-19.

Remote **actions** live in a different, **unofficial** BMW API — ConnectedDrive / My BMW
"Remote Services" (what `bimmer_connected` wraps), which is reverse-engineered and currently
deprecated/unreliable. So car actuation is **out of scope** for this system: our pipeline
consumes CarData as a sensor (read), and the actuation loop stays on the phone
([[project-outbound-action-pushcut]]) — building car control on the fragile ConnectedDrive
path (and the HA-in-the-loop dependency it reintroduces) is explicitly not pursued unless BMW
ships an official remote-services API.

## Alternatives considered

- **Run our own MQTT broker and fan out to HA** — keeps BMW in HA, and matches the future
  "push events to HA over MQTT" vision, but adds a broker to run today. Deferred; revisit if/when
  bidirectional HA is wanted.
- **A second CarData client alongside HA's direct subscription** — impossible: same GCID, the
  connections evict each other.
- **Forward from HA (`rest_command` automation)** — zero new creds and reuses HA's working auth, but
  puts HA in the critical path doing (or transporting) the machinery. Rejected: logic/infra stays
  ours.
- **Replace the phone signals with car signals** — gives up the independent-source redundancy; a
  BMW-stream gap (documented: broker drops, `isMoving` model-dependent) would then be a total miss.
- **Treat `isMoving`-false as the trip end** — closes the trip at the first red light.

## What the target vehicle exposes (verified 2026-07-18)

Read off the owner's HA (kvanbiesen integration) — a **BMW X1 sDrive20i (petrol)**. Currently
selected descriptors surface **only binary sensors**: `vehicle_motion_state` (`device_class:
moving`), all four `door_state_*`, `hood_state`, `tailgate_state`/`tailgate_door_state`,
`alarm_active`, `vehicle_deep_sleep_mode`, `preconditioning_*`. **No** `sensor.*` / `device_tracker.*`
/ `lock.*` — so no odometer, location, central-lock, ignition, or charging **in the current
selection** (charging is moot: petrol). This is the *selected subset*, not the car's ceiling — our
own client re-selects containers.

- **Resolved (was Q2):** `isMoving` is a first-class triggerable entity (`vehicle_motion_state`) →
  `car_started_moving` start anchor is viable.
- **Resolved (was Q1):** end anchor for this car = `motion` off + driver-door open (see Decision),
  since charging/lock/ignition aren't available.

## Confirmed on the live account (2026-07-20)

Account **activated** (~24h after "Reset BMW CarData"). Token flow works; REST API confirmed
(`api-cardata.bmwgroup.com`, `Bearer` + `x-version: v1`). VIN `WBA31EE0605Y73638` (PRIMARY),
gcid `50e598cf-…`. Created container **`J00I035N193CD` "Inference Trip Signals"** (ACTIVE) via
`POST /customers/containers` with the trip descriptors — **exact ids now known** (from the
kvanbiesen source + our telematicData snapshot):

- `vehicle.isMoving` — START anchor
- `vehicle.drivetrain.engine.isActive` ("ignition state") — **STRONG END anchor** (ignition off =
  parked; unlike raw motion-off it doesn't fire at red lights) — this supersedes the earlier
  "motion-off + driver-door" end design, which stays as corroborators. (Watch the swapped catalogue
  labels: `isActive`=ignition vs `isIgnitionOn`=engine; confirm which is start-stop-stable on a drive.)
- `vehicle.cabin.door.row1.driver.isOpen` — end disambiguator; `vehicle.cabin.door.lock.status`
  ("SECURED") — car-native lock corroborator; `vehicle.vehicle.deepSleepModeActive` — slow backstop.
- `vehicle.vehicle.travelledDistance` — odometer; GPS `…currentLocation.latitude/.longitude` — enables
  car-native geofencing (`arrived_home_by_car`) later.

Value encodings: string enums (`"OPEN"`/`"CLOSED"`, `"SECURED"`, `"true"`/`"false"`) + ISO-8601
timestamps — handled by the subscriber's mapper. `isMoving`/engine read `null` at rest (stream-pushed
on change when driving).

## Open questions

1. **MQTT stream:** ✅ connection CONFIRMED 2026-07-20 (live connect + subscribe granted):
   `customer.streaming-cardata.bmwgroup.com:9000`, MQTT v3.1.1, TLS 1.3 min, topic `{gcid}/+`,
   password = id_token. Remaining: capture a real *driving* message to lock the payload envelope
   (`mapper._iter_updates`) — 0 messages arrive while the car is parked/asleep.
2. ~~Which engine descriptor (`isActive` vs `isIgnitionOn`) is red-light-stable~~ — moot: neither
   streams on this vehicle (Outcome banner).
3. Final weight/threshold numbers, tuned against a replay of real fused streams (as ADR 0005 was).
4. ✅ Refresh-token rotation persistence — done (Neon `bmw_cardata_tokens`).
5. **Does the central lock produce usable edges?** ✅ the descriptor exists and arrives
   (`vehicle.cabin.door.status = "SECURED"`), but the 07-26 mapping watched a wrong id, so no
   `car_locked`/`car_unlocked` has been emitted yet. Fixed 07-27 — **awaiting the next drive**,
   which also has to confirm the value vocabulary (only `SECURED` observed so far; `UNLOCKED` is
   assumed, and anything else is logged rather than guessed). If it works, it's the first
   *directional* car-native signal and the next weight-map change.
6. **Fix `{lock, door} = 10` firing `got_out_the_car` at entry** (see the Addendum). Tune with
   `scripts/trip_eval.py` over real history, not from the 4 observed cases.
7. **A `distance` capability from the odometer** — needs the lineage-visibility question answered
   first: a passive reading isn't a contributor, so no deriver can see it today.
