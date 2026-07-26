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
| `vehicle.cabin.door.lock.status`          | →UNLOCKED       | `car_unlocked`            | **directional** entry cue — observation only, **no weight yet** | ❓ |
| `vehicle.cabin.door.lock.status`          | →SECURED        | `car_locked`              | **directional** exit cue — observation only, **no weight yet** | ❓ |
| `vehicle.isMoving`                        | false→true      | `car_started_moving`      | intended start anchor | ❌ never streams on this X1 |
| `vehicle.isMoving`                        | true→false      | `car_stopped_moving`      | weak end corroborator | ❌ never |
| `vehicle.drivetrain.engine.isActive`      | →on / →off      | `car_ignition_on` / `_off`| intended end anchor | ❌ never |
| `vehicle.vehicle.deepSleepModeActive`     | →true           | `car_deep_sleep`          | slow park backstop | ❌ never |

First observation of each descriptor sets a baseline **silently**; only genuine
transitions emit (so a parked car's initial state doesn't mint phantom events on the
hourly reconnect).

`car_locked`/`car_unlocked` are emitted into `raw_sensors` but appear in **no** weight map:
they persist to Neon for analysis, and the runtime ignores names no engine consumes. Direction
is why they matter — every car-native signal we have today (and the phone's
`car_lock_state_change`) fires at entry *and* exit, which is what caps the door at weight 4.
Weight them only after a replay (`scripts/trip_eval.py`), as ADR 0005/0006 did.

## Stream inventory (unmapped descriptors)

The mapper logs every descriptor it does **not** turn into a signal, once per descriptor per
process, with its value:

```
UNMAPPED descriptor in stream: vehicle.vehicle.travelledDistance = 48213
```

This exists because the silent `continue` it replaced made "the car never sends it"
indistinguishable from "we never looked" — the container subscribes to odometer and GPS
lat/lon, which were being dropped on the floor, and `BMW_DEBUG_LOG_ALL` (whole envelopes, off
by default) had never been enabled in production. Inventory runs *before* the baseline check,
so a descriptor that appears exactly once still shows up. One drive now enumerates the stream:

```bash
kubectl -n inference logs deploy/bmw-cardata | grep UNMAPPED
```

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
| `BMW_DEBUG_LOG_ALL`    |    | off | log every raw envelope. Rarely needed now — the permanent `UNMAPPED` inventory (above) covers the "what else is in the stream" question without the noise |

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
5. **What else is in the stream** — the container also subscribes to `travelledDistance`
   (odometer → trip *distance*, and ground truth for adjudicating junk trips) and
   `currentLocation.latitude/longitude` (a park-location snapshot → the `place` capability for
   trip endpoints, and a phone-independent `arrived_home_by_car`). Neither is mapped; both are
   numeric, so they want a capability/enrichment path rather than the boolean edge machinery
   here. Confirm they actually arrive via the `UNMAPPED` inventory first.
6. **Container expansion** — not subscribed today, cheap to add: **tailgate** (loading/unloading
   → the "did a shop" shape a phone can't see), **passenger door row 1** ("not driving alone"),
   and **fuel level** if exposed (a level jump + a stay = refuelled).
