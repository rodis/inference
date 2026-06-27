"""Quix Streams spike — port of `car_door_opened` (ADR 0004, step 2).

A standalone, throwaway-ish exploration that re-implements the `car_door_opened`
weighted-window engine on the Quix Streams data plane, to *feel* the concepts we
discussed:

  * one `Application` (one consumer group) instead of KafkaStreamHandler +
    RuntimeSupervisor + a per-event thread;
  * per-key state in Quix `State` (RocksDB + changelog) instead of a shared Redis
    ZSET/HASH + `SET NX EX` cooldown;
  * `group_by(<entity key>)` so all of one entity's events share one state slot —
    the single-writer-per-key property, by construction.

It runs ALONGSIDE the live runtime safely: different consumer group, and it
produces to a separate spike topic (not the real `high_level_events`), so it
cannot trigger `got_into_the_car` or pollute anything.

Run it from inside the `workers/` tree so `workers/.env` is found:

    pip install -e '.[quix]'           # or: uv sync --extra quix
    cd workers/quix_spike && python main.py

Then inject the two contributing events via the Vector ingest contract (see
README.md) and watch it fire.

THE ONE THING TO INTERNALISE: Quix `State` is scoped to the *current message
key*. If raw_sensors messages arrive with no key (or a per-message key like
envelope_id), every event would see an empty window and it would NEVER fire.
`group_by(key_for)` is what gives contributors a *stable, shared* key so the
window can accumulate. Stateful aggregation is impossible without a stable key —
that is the whole lesson, made concrete.
"""

import logging
import os
import time
import uuid
from datetime import datetime, timezone

from dotenv import find_dotenv, load_dotenv
from quixstreams import Application
from quixstreams.state import State

# workers/.env (gitignored) — walk upward from CWD, like inference.config does.
if dotenv_path := find_dotenv(usecwd=True, raise_error_if_not_found=False):
    load_dotenv(dotenv_path)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("quix_spike")


# --- config (mirrors events/car_door_opened.yml) ------------------------------

WEIGHTS = {"car_lock_state_change": 5, "device_connected_to_carplay": 5}
THRESHOLD = 10
WINDOW_SECONDS = 600
COOLDOWN_SECONDS = 600

# A label so two instances in one consumer group are distinguishable in the logs
# (step 3 — proving partition/key split). Defaults to the PID.
INSTANCE = os.environ.get("SPIKE_INSTANCE", str(os.getpid()))

SOURCE_TOPIC = os.environ.get("SPIKE_SOURCE_TOPIC", "raw_sensors")
# Separate sink so we never touch the real high_level_events during the spike.
SINK_TOPIC = os.environ.get("SPIKE_SINK_TOPIC", "high_level_events_spike")
CONSUMER_GROUP = os.environ.get("SPIKE_CONSUMER_GROUP", "quix-spike-car-door-opened-v1")

# Kafka mTLS — same env contract as inference.config; defaults to the K8s mount.
_SSL = {
    "security.protocol": "SSL",
    "ssl.ca.location": os.environ.get("KAFKA_SSL_CA_PATH", "/etc/kafka/ssl/ca-cert.pem"),
    "ssl.certificate.location": os.environ.get("KAFKA_SSL_CERT_PATH", "/etc/kafka/ssl/access-cert.pem"),
    "ssl.key.location": os.environ.get("KAFKA_SSL_KEY_PATH", "/etc/kafka/ssl/access-key.pem"),
}


# --- the entity key -----------------------------------------------------------

def key_for(value: dict) -> str:
    """The entity a window aggregates over — the partition/state key.

    Prefers a real `vehicle_id` once producers stamp one (ADR 0004); falls back to
    `source_app`, then a constant. With one car + the constant fallback, every
    event shares one key: the window accumulates and fires correctly, but with no
    parallelism (one key → one partition → one owner). To *prove* the scaling
    property (step 3), inject events carrying two distinct `vehicle_id`s and run
    two instances — each instance will own a disjoint set of keys.
    """
    msg = value.get("message", {}) if isinstance(value, dict) else {}
    return str(msg.get("vehicle_id") or value.get("source_app") or "_single_car")


# --- the engine, as one stateful function -------------------------------------

def weighted_window(value: dict, state: State):
    """Faithful port of WeightedWindowEngine.decide(), Redis → Quix State.

    `state` is automatically scoped to `key_for(value)`, so the window and the
    cooldown below are per-entity with zero key-threading in our code.

    NOTE — one deliberate behavioural change worth understanding: the cooldown
    here is *event-time* (`now - last_fired`), whereas the live engine's cooldown
    is *wall-clock* (Redis `SET NX EX` TTL) while its window is event-time. That
    split is the documented replay wart (a replay collapses history into one
    fire). Event-time cooldown is deterministic under replay — a small, defensible
    improvement to discuss, not an accident.
    """
    if not isinstance(value, dict):
        return None
    msg = value.get("message") or {}
    name = msg.get("event_name")
    if name not in WEIGHTS:                       # gatekeeper (engine's event_name filter)
        return None

    entity = key_for(value)                       # the vehicle this derived event is about
    now = int(msg.get("timestamp", 0))

    # window: {event_name: {"ts": earliest_ts, "envelope_id": id}} — dedup-earliest.
    # Storing envelope_id lets us emit `derived_from` lineage (the LineageEnricher).
    window = state.get("window", {})
    window = {k: v for k, v in window.items() if now - v["ts"] <= WINDOW_SECONDS}  # prune
    if name not in window or now < window[name]["ts"]:
        window[name] = {"ts": now, "envelope_id": value.get("envelope_id")}
    state.set("window", window)

    score = sum(WEIGHTS[k] for k in window)       # weighted sum over distinct present types
    if score < THRESHOLD:
        return None

    last_fired = state.get("last_fired", 0)       # cooldown — replaces SET NX EX
    if now - last_fired < COOLDOWN_SECONDS:
        return None
    state.set("last_fired", now)

    occurred_at = sum(v["ts"] for v in window.values()) / len(window)
    # Output mirrors pipeline.finalize() + LineageEnricher (a superset message).
    return {
        "inference_type": "car_door_opened",
        "message": {
            "event_name": "car_door_opened",
            "vehicle_id": entity,                 # carry the entity key onto the derived event
            "timestamp": int(occurred_at),
            "confidence_score": score,
            "occurred_at": occurred_at,
            "sources": list(window.keys()),
            "evidence": {k: v["ts"] for k, v in window.items()},
            "derived_from": [
                {"envelope_id": v["envelope_id"], "event_name": k, "timestamp": v["ts"]}
                for k, v in window.items()
            ],
        },
    }


def to_envelope(result: dict) -> dict:
    """Wrap the engine output in the `high_level_events` Envelope shape (ADR 0004 step 4).

    This is the job Vector's `classify_domain` + `enrich_sensor` transforms did when
    the worker POSTed to the HTTP gateway. Dropping that hop means the worker mints
    `envelope_id` and stamps the metadata itself, then produces straight to Kafka via
    `to_topic()`. The output matches the live wire shape field-for-field (verified
    against a real high_level_events message), except `source_type` — which is now
    "quix_spike" instead of "http_server" because it no longer comes through the HTTP
    source. That field is also our distinguisher from the live worker's output.
    """
    name = result["inference_type"]
    return {
        "envelope_id": str(uuid.uuid4()),          # was minted by Vector's enrich_sensor
        "event_name": name,
        "inference_type": name,
        "message": result["message"],
        "processed_at": time.time(),
        "source_app": name,                        # Vector set this from the URL path segment
        "source_type": "quix_spike",               # was "http_server"; now produced directly
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_app() -> Application:
    app = Application(
        broker_address=os.environ["KAFKA_BOOTSTRAP_SERVERS"],
        consumer_group=CONSUMER_GROUP,
        auto_offset_reset="latest",               # matches builder.py:75 (no replay)
        consumer_extra_config=_SSL,
        producer_extra_config=_SSL,
        # Per-instance local RocksDB dir. In K8s each pod has its own disk; locally
        # two instances in one group must not share a state dir (RocksDB lock).
        state_dir=os.environ.get("SPIKE_STATE_DIR", "state"),
    )
    source = app.topic(SOURCE_TOPIC, value_deserializer="json")
    sink = app.topic(SINK_TOPIC, value_serializer="json")

    sdf = app.dataframe(source)
    if os.environ.get("SPIKE_GROUP_BY", "true").lower() == "true":
        # Step 2 path: re-key in-app. Needs a repartition topic (a shuffle), but
        # works even when the source isn't keyed by entity.
        sdf = sdf.group_by(key_for, name="entity")
    # else (step 3, production-clean): the source is ALREADY keyed by vehicle_id at
    # produce time, so State scopes to the Kafka message key directly — no shuffle,
    # and the source partitions split straight across instances.
    sdf = sdf.apply(weighted_window, stateful=True)
    sdf = sdf.filter(lambda v: v is not None)      # drop the no-fire messages
    sdf = sdf.update(
        lambda v: log.info(
            "🔥 [%s] FIRED car_door_opened vehicle=%s score=%s",
            INSTANCE, v["message"]["vehicle_id"], v["message"]["confidence_score"],
        )
    )
    sdf = sdf.apply(to_envelope)                    # step 4: shape the full Envelope ourselves
    sdf = sdf.to_topic(sink)                        # produce straight to Kafka — no Vector hop
    return app


if __name__ == "__main__":
    log.info(
        "Quix spike: %s → [%s] → %s (group=%s)",
        SOURCE_TOPIC, "weighted_window", SINK_TOPIC, CONSUMER_GROUP,
    )
    build_app().run()
