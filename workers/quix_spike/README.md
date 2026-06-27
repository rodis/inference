# Quix Streams spike — `car_door_opened`

A throwaway exploration for **ADR 0004 step 2**: re-implement the `car_door_opened`
weighted-window engine on the **Quix Streams** data plane to feel the concepts —
one `Application` instead of a thread-per-event runtime, per-key `State` instead of
shared Redis, and `group_by(<entity key>)` as the single-writer-per-key mechanism.

It runs **alongside** the live runtime with zero blast radius: its own consumer
group (`quix-spike-car-door-opened-v1`) and its own sink topic
(`high_level_events_spike`), so it never touches the real `high_level_events` or
triggers `got_into_the_car`.

## What it maps to

| Live runtime | This spike |
|---|---|
| `KafkaStreamHandler` + `RuntimeSupervisor` (thread/event) | one `Application`, one consumer group |
| `WeightedWindowEngine` + Redis ZSET/HASH | one stateful `apply()` + Quix `State` |
| cooldown `SET NX EX` | a `State` entry (`last_fired`) |
| `VectorHttpEmitter` POST → Vector | `sdf.to_topic(...)` produces directly |

## Run

```bash
pip install -e '.[quix]'          # or: uv sync --extra quix
cd workers/quix_spike && python main.py   # run from here so workers/.env is found
```

Inject the two contributing events (same entity key = same `source_app`, within
the 600s window) straight into `raw_sensors`:

```python
# uv run python - <<'PY'  (from workers/quix_spike)
import os, json, time, uuid
from dotenv import find_dotenv, load_dotenv; load_dotenv(find_dotenv(usecwd=True))
from confluent_kafka import Producer
p = Producer({"bootstrap.servers": os.environ["KAFKA_BOOTSTRAP_SERVERS"],
    "security.protocol": "SSL", "ssl.ca.location": os.environ["KAFKA_SSL_CA_PATH"],
    "ssl.certificate.location": os.environ["KAFKA_SSL_CERT_PATH"],
    "ssl.key.location": os.environ["KAFKA_SSL_KEY_PATH"]})
now = int(time.time())
for name, ts in [("car_lock_state_change", now), ("device_connected_to_carplay", now + 2)]:
    p.produce("raw_sensors", value=json.dumps({"event_name": name, "source_app": "spike_test",
        "source_type": "test", "timestamp": ts, "envelope_id": str(uuid.uuid4()),
        "message": {"event_name": name, "timestamp": ts}}).encode())
p.flush(10)
PY
```

You should see `🔥 FIRED car_door_opened` with `derived_from` lineage. ✅ Verified
live against Aiven on 2026-06-27.

## The one lesson

Quix `State` is scoped to the **current message key**. With no stable key, every
event sees an empty window and it **never fires** — which is why `group_by(key_for)`
is mandatory, not cosmetic. Stateful aggregation is impossible without a stable
key. With one car, `key_for` falls back to a constant → correct fires, but one
key → one partition → one owner → **no parallelism** (expected; the entity is the
unit of parallelism).

## Known differences from the live engine (deliberate, for discussion)

- **Cooldown is event-time** here (`now - last_fired`) vs the live engine's
  wall-clock Redis TTL. Event-time is deterministic under replay (fixes the
  documented "replay collapses history into one fire" wart).
- **Lineage only, no geo enricher** — the geo enricher is a no-op until a
  geolocated producer exists, so it's omitted.
- Output is the `finalize()`-style superset dict to a **spike** topic; it is not
  yet re-wrapped into a full `Envelope` (that's step 4 — drop the Vector hop).

## Step 3 — scaling proof ✅ (verified live 2026-06-27)

Two instances (`SPIKE_INSTANCE=A`/`B`), one consumer group, a **2-partition**
source topic keyed by `vehicle_id` (`SPIKE_GROUP_BY=false`, so the source
partitions split straight across instances — the production-clean path):

```
Instance A fired: car-1, car-2, car-3
Instance B fired: car-4, car-5, car-6
```

Disjoint key ownership, union = all six, **zero double-fires**. Each vehicle's two
contributors landed on the same partition → same instance → same local state, so
each fired exactly once, owned by exactly one instance. That's single-writer-per-key
+ horizontal split, demonstrated. To reproduce:

```bash
# create a 2-partition keyed source topic (raw_sensors_spike2) via AdminClient first, then:
SPIKE_INSTANCE=A SPIKE_SOURCE_TOPIC=raw_sensors_spike2 SPIKE_GROUP_BY=false \
  SPIKE_CONSUMER_GROUP=quix-spike-cdo-step3 SPIKE_STATE_DIR=state_A python main.py &
SPIKE_INSTANCE=B SPIKE_SOURCE_TOPIC=raw_sensors_spike2 SPIKE_GROUP_BY=false \
  SPIKE_CONSUMER_GROUP=quix-spike-cdo-step3 SPIKE_STATE_DIR=state_B python main.py &
# then inject keyed pairs for several vehicles (Kafka message key = vehicle_id)
```

Two instances in one group need **separate** `SPIKE_STATE_DIR`s locally (RocksDB
lock); in K8s each pod has its own disk so this is automatic.

### ⚠️ Finding: the Aiven free tier caps you at **5 user topics**

Every stateful handler mints a **changelog** topic (and a **repartition** topic if
it uses `group_by`). On the free-0 plan that ceiling is hit almost immediately:
`raw_sensors` + `high_level_events` + one spike's changelog/repartition already
crowds it. Real consequence for the migration — **the number of stateful event
types is bounded by the topic budget**, not just compute. Either pay for a larger
plan, avoid `group_by` (key at ingest so no repartition topic), or share changelog
topics. Recorded in [ADR 0004](../../doc/adr/0004-scaling-model.md).

## Step 4 — drop the Vector emit hop ✅ (verified live 2026-06-27)

The spike now mints the full `high_level_events` **Envelope** itself
(`to_envelope()` — the job Vector's `classify_domain` + `enrich_sensor` transforms
did) and produces it **straight to Kafka** via `to_topic()`. No HTTP POST to Vector.

Proven two ways:
- **Offline**: `to_envelope()` output field-set matches a real `high_level_events`
  message exactly, and parses cleanly through the production `inference.events.Envelope`
  model (message → `OpaqueMessage`, as expected for the unregistered `car_door_opened`).
- **Live**: ran producing to the real `high_level_events`, injected the pair, then
  re-consumed the topic and re-parsed the actual bytes via `Envelope` — found the
  spike's envelope (tagged `source_type: "quix_spike"`, vs the live worker's
  `"http_server"`). The worker mints `envelope_id` now (was Vector's job).

```bash
SPIKE_SINK_TOPIC=high_level_events SPIKE_CONSUMER_GROUP=quix-spike-step4 python main.py
```

Consequence for the architecture: **Vector leaves the emit path** and is reduced to
the Kafka→Neon persister (`kafka_persist` source already consumes `high_level_events`).
The only behavioural shift is `source_type` (`http_server` → the producing component)
and who mints `envelope_id` (Vector → worker).

## Step 5 — generic YAML-driven runtime ✅ (verified live 2026-06-27)

`runtime.py` loads **every** `events/*.yml` (via the same
`inference.runtime.definition.load_definitions` the current runtime uses) and runs
them ALL on **one shared `Application`** — one consumer group, one process. Adding
an event is a YAML change, not another thread/consumer. **The 1:1 event↔thread
binding is gone.**

```bash
cd workers/quix_spike && python runtime.py     # runs all events/*.yml at once
```

Verified live: injected `device_connected_to_power` + `car_lock_state_change` +
`device_connected_to_carplay`, and one process fired **both** derivations:

```
🔥 FIRED car_door_opened  vehicle=spike_test   (lock + carplay)
🔥 FIRED got_into_the_car  vehicle=spike_test  (power + the car_door_opened it just produced)
```

The second is **recursive derivation in-process**: the fired `car_door_opened` is
produced to `high_level_events`, which is also a source topic, so the same router
re-consumes it and feeds `got_into_the_car` — no extra wiring (ADR 0002).

### Design: one shared router, not one branch per definition — and why

The step-3 topic-budget finding **forced** this. Per-definition branches would mint
N changelog + N repartition topics (over the 5-topic free-tier cap at N=3). So the
runtime is a single keyed stateful **router** that loads all definitions as data and
keeps a per-`(definition, entity)` window in **namespaced** state
(`<def>:window`, `<def>:last_fired`). Cost: **1 repartition + 1 changelog total**,
independent of definition count. The definitions stay the source of truth; only the
execution shape changed. On a paid plan, per-definition branches read cleaner — a
real cost/clarity trade, recorded in [ADR 0004](../../doc/adr/0004-scaling-model.md).

### What a real migration still needs (not in this spike)

- mTLS/config from the K8s `ConfigMap`/`Secret` (here it's `workers/.env`);
- the enricher chain (`lineage`/`geo`) run as `.apply()` steps instead of inlined;
- typed message validation at finalize; a deploy manifest; liveness/readiness.

## Env vars

| Var | Default | Purpose |
|---|---|---|
| `SPIKE_SOURCE_TOPIC` | `raw_sensors` | input topic |
| `SPIKE_SINK_TOPIC` | `high_level_events_spike` | output topic (isolated from prod) |
| `SPIKE_CONSUMER_GROUP` | `quix-spike-car-door-opened-v1` | consumer group |
| `SPIKE_GROUP_BY` | `true` | `true`=re-key in-app (step 2); `false`=source already keyed (step 3) |
| `SPIKE_STATE_DIR` | `state` | local RocksDB dir (per-instance when scaling) |
| `SPIKE_INSTANCE` | PID | label to tell instances apart in logs |

## Cleanup

The spike auto-creates `high_level_events_spike`, a `changelog__…` topic, and a
`group_by` repartition topic on Aiven, plus local RocksDB state under `state/`.
Delete the topics from the Aiven console and `rm -rf state/` to reset.
