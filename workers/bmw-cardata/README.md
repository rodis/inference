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

| descriptor (HA name)              | edge         | canonical signal          | role |
|-----------------------------------|--------------|---------------------------|------|
| `vehicle_motion_state` (isMoving) | false→true   | `car_started_moving`      | trip **start** anchor (self-sufficient) |
| `vehicle_motion_state`            | true→false   | `car_stopped_moving`      | weak end corroborator (red-light unsafe alone) |
| `door_state_front_driver`         | →open        | `car_driver_door_opened`  | end disambiguator (motion-off + door-open = parked) |
| `vehicle_deep_sleep_mode`         | →true        | `car_deep_sleep`          | slow, certain park backstop |

First observation of each descriptor sets a baseline **silently**; only genuine
transitions emit (so a parked car's initial state doesn't mint phantom events on the
hourly reconnect).

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
3. **Refresh-token rotation persistence** — if BMW rotates the refresh token on each
   refresh, a pod restart falls back to the (now stale) env value. Persist the rotated
   token (write back to a Secret / mounted file) before this runs unattended for >2 weeks.
4. **Weights** — once real signals flow, add `car_started_moving` (weight ≥ threshold) to
   `events/got_into_the_car.yml` and the park-confirm to `events/got_out_the_car.yml`, then
   tune against a replay (ADR 0006).
