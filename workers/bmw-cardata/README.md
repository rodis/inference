# bmw-cardata subscriber

Car-native trip signals from **BMW CarData** into the inference pipeline — the producer
side of [ADR 0006](../../doc/adr/0006-car-native-trip-signals.md). Thin **transport + auth
only**: it owns the OAuth token refresh + MQTT subscription and posts canonical raw
signals to Vector. **No trip logic here** — that stays in the engines
(`got_into_the_car` / `got_out_the_car` / `car_trip`).

```
BMW MQTT ({gcid}/{vin})                     this worker
  │  MQTT v5 / TLS                          ┌───────────────────────────────┐
  │  user=gcid  password=id_token  ───────► │ auth   refresh id_token hourly │
  ▼                                         │ mqtt   subscribe {gcid}/{vin}  │
 descriptor updates ───────────────────────►│ mapper edge→canonical signal   │
                                            │ ingest POST /sensors/bmw       │
                                            └───────────────┬───────────────┘
                                                            ▼  {"payload":{event_name,user_id,timestamp}}
                            Vector `standard` lane (shape_sensor → enrich_sensor) → raw_sensors → engines
```

## Why it POSTs to the existing `standard` lane (no new Vector transform)

We control this producer's body, so it emits the canonical
`{"payload": {"event_name", "user_id", "timestamp", ...}}` shape directly to
`/sensors/bmw`. The 2nd path segment (`bmw`, ≠ `owntracks`) routes to Vector's `standard`
adapter (`shape_sensor`). OwnTracks needed a bespoke `owntracks_to_canonical` adapter only
because its body is a fixed 3rd-party shape; ours isn't — so **no Vector change is
required**, and we add a producer, not a Kafka topic (stays under the Aiven 5-topic cap).

## Signals emitted (ADR 0006 — asymmetric)

| descriptor                                | edge            | canonical signal          | role | seen live? |
|-------------------------------------------|-----------------|---------------------------|------|------------|
| `vehicle.cabin.door.row1.driver.isOpen`   | →open           | `car_driver_door_opened`  | non-directional corroborator, in both weight maps (4 / 5) | ✅ ~94%/trip |
| `vehicle.cabin.door.status`               | →UNLOCKED       | `car_unlocked`            | **directional** entry cue — observation only, **no weight yet** | ❓ id corrected 07-27, awaiting a drive |
| `vehicle.cabin.door.status`               | →SECURED        | `car_locked`              | **directional** exit cue — observation only, **no weight yet** | ❓ same |
| `vehicle.isMoving`                        | false→true      | `car_started_moving`      | intended start anchor | ❌ never streams on this X1 |
| `vehicle.isMoving`                        | true→false      | `car_stopped_moving`      | weak end corroborator | ❌ never |
| `vehicle.drivetrain.engine.isActive`      | →on / →off      | `car_ignition_on` / `_off`| intended end anchor | ❌ never |
| `vehicle.vehicle.deepSleepModeActive`     | →true           | `car_deep_sleep`          | slow park backstop | ❌ never |

First observation of an **edge** descriptor sets a baseline **silently**; only genuine
transitions emit (so a parked car's initial state doesn't mint phantom events on the
hourly reconnect). Readings follow the opposite rule — see below.

### Readings and the catch-all (2026-07-27)

Everything else the car streams used to reach only a *rotating* pod log, so it was
unrecoverable within days. It is now persisted too:

| descriptor | canonical signal | payload |
|---|---|---|
| `vehicle.vehicle.travelledDistance` | `car_odometer` | `{odometer_km}` |
| `…currentLocation.latitude` | `car_gps_latitude` | `{latitude}` |
| `…currentLocation.longitude` | `car_gps_longitude` | `{longitude}` |
| `…currentLocation.altitude` | `car_gps_altitude` | `{altitude}` |
| `vehicle.drivetrain.fuelSystem.level` | `car_fuel_level` | `{fuel_level}` |
| *anything else that changes* | `car_state_change` | `{descriptor, value}` |

The catch-all is deliberate: one event name for the whole tail of the stream instead of
fifteen, keeping the name space small while making the history queryable —

```sql
select message->>'descriptor', message->>'value', occurred_at
from events where name = 'car_state_change' order by occurred_at;
```

**Two kinds of descriptor, two rules.** An *edge* (door, lock, motion) is baseline-silent,
because a retained "door closed" must not mint a phantom on every reconnect. A *reading*
(odometer, GPS, fuel) is the opposite: the value **is** the fact, there is no phantom to
guard against, so the first observation is emitted and repeats are de-duplicated instead.
Reversed, you would either lose the first reading or emit one per reconnect.

**GPS is deliberately NOT fused** — one event per component. A first cut paired lat/lon
into a single `car_location` inside a 10s window; that was reversed, and the reasoning is
worth keeping because it generalises to anything else added here.

A latitude without a longitude is useless to a *consumer*, which makes pairing tempting.
But this is a **producer**, and pairing here is a derivation decision whose inputs never
reach Neon. That breaks the invariant the pipeline rests on — derived events are a *cache*,
raw signals are the *source of truth*, and [`scripts/rederive.py`](../../scripts/rederive.py)
rebuilds the former from the latter. A window baked in at ingest is unreplayable: if 10s is
wrong there is nothing to re-derive from, and tuning it needs exactly the components the
fusion threw away.

It was lossy in practice too. Altitude arrives **~4 minutes** after lat/lon on this car, so
it fell outside the window and was dropped entirely. And it would have corrupted the one
measurement this mapping exists to make: if lat and lon update at different rates, fusing
them hides it.

So the rule for this mapper is **one descriptor in, one event out**. Anything wanting a
point joins the components downstream, where the window is visible, tunable and
re-derivable.

All of these are **observation-only**: no engine consumes these names, so deploying them
cannot change any derivation. `car_odometer` is the prerequisite for a junk-trip veto — a
trip whose odometer delta is 0 is provably not a trip, which is a physical fact rather than
a heuristic weight.

⚠️ **Cadence is unmeasured.** Whether `travelledDistance` and the GPS point update *during*
a drive or only in the park/wake batch is exactly what mapping them will reveal. If GPS
turns out to stream continuously, GPS event volume could be much higher than the
handful/day the park-only model predicts — worth a look at the first day's rows.

`car_locked`/`car_unlocked` are emitted into `raw_sensors` but appear in **no** weight map:
they persist to Neon for analysis, and the runtime ignores names no engine consumes. Direction
is why they matter — every car-native signal we have today (and the phone's
`car_lock_state_change`) fires at entry *and* exit, which is what caps the door at weight 4.
Weight them only after a replay (`scripts/trip_eval.py`), as ADR 0005/0006 did.

## Stream inventory

The mapper logs each descriptor's **first appearance**, once per descriptor per process,
with its value:

```
descriptor in stream: vehicle.vehicle.travelledDistance = 24809
```

It originally existed because the silent `continue` it replaced made "the car never sends
it" indistinguishable from "we never looked". Now that nothing is discarded, its remaining
job is narrower but still real: it covers the **mapped-but-never-changing** case, which
emits no event at all. That is precisely how we can say this X1 never sends `isMoving`
rather than that we stopped looking. Inventory runs *before* the baseline check, so a
descriptor appearing exactly once still shows up.

```bash
kubectl -n inference logs deploy/bmw-cardata | grep 'descriptor in stream'
```

**Read once per process, so this is the stream's _vocabulary_, not its per-trip cadence.** The
first message after connect is a full state dump, so nearly every id below was logged from that
rather than from driving. Per-trip cadence is now answerable from Neon instead — query the
`car_state_change` / `car_odometer` / `car_gps_*` rows by `occurred_at`.

### First inventory (2026-07-27, one drive) — 24 descriptors

The container is **much** larger than the 8 ADR 0006 recorded. Grouped by what they'd be good for:

| descriptor | value seen | why it matters |
|---|---|---|
| `vehicle.vehicle.travelledDistance` | `24809` | odometer → trip **distance**; also ground truth for junk-trip adjudication (a phantom covers ~0 km) |
| `…navigation.currentLocation.latitude` / `.longitude` / `.altitude` | `47.207…` / `8.5747…` / `663` | car-native park location → `place` on trip endpoints, phone-independent `arrived_home_by_car` |
| `vehicle.drivetrain.fuelSystem.level` | `18` | **not previously known to exist** — a level *jump* + a stay = refuelled |
| `vehicle.cabin.door.status` | `'SECURED'` | the central lock — the **directional** signal (now mapped) |
| `vehicle.body.trunk.isOpen`, `vehicle.body.trunk.door.isOpen` | `False` | loading/unloading → the "did a shop" shape |
| `…door.row1.passenger.isOpen`, `…row2.driver`, `…row2.passenger` | `False` | "not driving alone" |
| `vehicle.body.hood.isOpen` | `False` | maintenance, not trips |
| `…window.row{1,2}.{driver,passenger}.status`, `…sunroof.status`, `…sunroof.tiltStatus` | `'CLOSED'` | low value |
| `…antiTheftAlarmSystem.alarm.armStatus`, `.alarm.isOn` | `'doorsOnly'`, `False` | arm status tracks lock, so partly redundant with the lock |
| `…preConditioning.activity`, `.remainingTime`, `.isRemoteEngineStartAllowed` | `'INACTIVE'`, `0`, `False` | climate; no trip value on a petrol car |
| `vehicle.vehicle.timeSetting` | `'utc'` | config, not telemetry |

Two ADR **descriptor ids were wrong** (transcribed from the kvanbiesen source, never verified
against the live stream): the lock is `vehicle.cabin.door.status`, not `…door.lock.status`, and GPS
lives under `vehicle.cabin.infotainment.navigation.currentLocation.*`, not `vehicle.currentLocation.*`.
The first is why the `car_locked`/`car_unlocked` mapping shipped 07-26 never fired.

## Config (env; `workers/.env` locally, ConfigMap/Secret in K8s)

| var | required | default | notes |
|-----|----------|---------|-------|
| `BMW_CLIENT_ID`        | ✅ | — | CarData client id (device-code-flow) |
| `BMW_REFRESH_TOKEN`    | ✅ | — | long-lived (2wk) token from the one-time device flow — **secret** |
| `BMW_VIN`              | ✅ | — | vehicle VIN (topic + which car) |
| `BMW_USER_ID`          | ✅ | — | entity key events are tagged with (VIN→user_id) |
| `VECTOR_BASE_URL`      | ✅ | — | Vector ingest base (same var the old runtime used) |
| `BMW_TOKEN_URL`        |    | `https://customer.bmwgroup.com/gcdm/oauth/token` | |
| `BMW_MQTT_HOST`        |    | `customer.streaming-cardata.bmwgroup.com` | ✅ confirmed (TLS 1.3, MQTT v3.1.1) |
| `BMW_MQTT_PORT`        |    | `9000` | ✅ confirmed |
| `BMW_TOPIC_TEMPLATE`   |    | `{gcid}/+` | ✅ confirmed (wildcard, all VINs on the gcid) |
| `BMW_INGEST_PATH`      |    | `/sensors/bmw` | |
| `BMW_REFRESH_MARGIN_SECONDS` | | `300` | refresh id_token this long before expiry |
| `BMW_DEBUG_LOG_ALL`    |    | off | log every raw envelope. Should now be unnecessary — the whole stream persists to Neon (`car_state_change` et al.), which answers both "what else is in the stream" and "how often", without the noise or the log rotation |

## Run locally

```bash
cd workers/bmw-cardata
pip install -r requirements.txt
# set BMW_* + VECTOR_BASE_URL in workers/.env
python main.py
```

## Deploy (when activation clears + a real token exists)

Follows the repo's auto-discovery: `publish-images.yml` finds `workers/bmw-cardata/Dockerfile`
→ builds `inference-bmw-cardata` → bumps `deploy/inference/kustomize/base/bmw-cardata/values.yml`.
**Still TODO** (do at deploy time): add `deploy/inference/kustomize/base/bmw-cardata/`
(`helmChart.yml` + `kustomization.yml` + `values.yml`, mirroring `runtime/`), a Secret for
the BMW creds (`BMW_CLIENT_ID`/`BMW_REFRESH_TOKEN`/`BMW_VIN`/`BMW_USER_ID`), and reference
it in `deploy/inference/kustomize/base/kustomization.yml`.

## Open items (finalize against real data / the Integration Guide)

1. ✅ **MQTT broker** — CONFIRMED 2026-07-20 (live connect + subscribe granted):
   `customer.streaming-cardata.bmwgroup.com:9000`, **MQTT v3.1.1**, **TLS 1.3 minimum**
   (needs OpenSSL 3 / Python 3.13 — macOS LibreSSL can't), topic `{gcid}/+`, password = id_token.
   Still TODO: the **message envelope** shape — capture a real driving message (`_iter_updates`
   logs the first one) and lock the parser; only descriptor ids are confirmed so far.
2. ✅ **Descriptor ids** (`mapper.DESCRIPTOR_*`) — CONFIRMED against the live container +
   telematicData snapshot (`vehicle.isMoving`, `…engine.isActive`, `…door.row1.driver.isOpen`,
   `…deepSleepModeActive`). Which engine descriptor is red-light-stable still needs a live drive.
3. ✅ **Refresh-token rotation persistence** — BMW rotates on every refresh; the rotated token
   is persisted to Neon (`bmw_cardata_tokens`, see `token_store.py`), so restarts resume.
   Consequence: **don't refresh this token out of band** (e.g. to poke the REST API) — the
   running pod prefers its in-memory copy and would fail its next hourly refresh.
4. **Weights** — the motion/ignition anchors this planned for never stream (see the ADR 0006
   Outcome banner), so `car_driver_door_opened` at 4/5 is what shipped. Next candidate is
   `car_locked`/`car_unlocked` **if** the inventory shows the lock descriptor arriving: it is the
   only car-native signal that is directional. Tune with `scripts/trip_eval.py`, not a count delta.
5. **What else is in the stream** — ✅ answered by the 07-27 inventory above. Odometer and GPS do
   arrive; both are numeric snapshots, so they want a capability/enrichment path, not the boolean
   edge machinery here (the blocker is that a capability deriver only sees its event's *lineage*,
   which a passive reading isn't part of — see the ADR 0006 addendum).
6. ~~**Container expansion**~~ — not needed: trunk, all four doors and **fuel level** turn out to
   already be subscribed. Nothing to add; just map what's there.
7. **`{lock, door} = 10` fires `got_out_the_car` at ENTRY** (found 07-27). Both are
   non-directional and both sit at 5 in `got_out`, so an entry unlock + door-open hits the
   threshold with no gate. Observed 4× since the door was added on 07-24. Harmless *so far* only
   because each real arrival landed outside the 600s cooldown — on a sub-10-minute drive the
   phantom's cooldown would swallow the real arrival and leave the trip open. This is the exact
   mirror of the entry-side phantom that ADR 0005 rev 2026-07-24 fixed by demoting both ambiguous
   signals to 4 in `got_into_the_car`; the same pair on the exit side was left at 5. Adjudicate
   with `scripts/trip_eval.py` before changing a weight — a count delta is what hid this class
   the first time.
