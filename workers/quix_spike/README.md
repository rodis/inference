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

## Next steps (ADR 0004)

3. **Prove scaling**: a 2-partition test topic, two distinct `vehicle_id`s, run two
   instances — confirm no double-fire and each instance owns disjoint keys.
4. **Drop the Vector emit hop**: produce a full `Envelope` to the real
   `high_level_events`; Vector becomes Kafka→Neon persister only.
5. **Generalize to YAML**: build one topology branch per `EventDefinition` on a
   shared `Application` — the actual 1:1-unbinding migration.

## Cleanup

The spike auto-creates `high_level_events_spike`, a `changelog__…` topic, and a
`group_by` repartition topic on Aiven, plus local RocksDB state under `state/`.
Delete the topics from the Aiven console and `rm -rf state/` to reset.
