"""Generic Quix Streams runtime (ADR 0004) — the deployable form of the spike.

Loads every `events/*.yml` (the same `EventDefinition`s the threaded runtime used)
and runs them all on ONE shared Quix `Application`: one consumer group, one process,
partition-keyed state. This is the productionised version of `workers/quix_spike/`
— see [`doc/adr/0004-scaling-model.md`](../../../doc/adr/0004-scaling-model.md).

It replaces the thread-per-event `RuntimeSupervisor`:
  * no Redis — window + cooldown live in partition-local Quix `State` (RocksDB +
    changelog), single-writer-per-key by construction;
  * no Vector emit hop — the full `high_level_events` Envelope is minted here and
    produced straight to Kafka via `to_topic()` (Vector stays the ingest gateway +
    Neon persister);
  * recursive derivation (ADR 0002) is free — a fired event lands on `high_level_events`,
    which is also a source topic, so the same router re-consumes it.

One shared keyed router (not one branch per definition) because each stateful
operator + `group_by` mints Kafka topics, and the Aiven free tier caps user topics
at 5; the router costs 1 repartition + 1 changelog regardless of definition count.

Config comes from env (set by the K8s ConfigMap/Secret, or `workers/.env` locally):
  KAFKA_BOOTSTRAP_SERVERS, KAFKA_SSL_{CA,CERT,KEY}_PATH, EVENTS_DIR,
  QUIX_CONSUMER_GROUP, QUIX_STATE_DIR.
"""

import logging
import os
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from quixstreams import Application
from quixstreams.state import State

from inference.runtime.definition import load_definitions

logger = logging.getLogger("inference.quix")


def _ssl_config() -> dict:
    # Defaults match the kafka-ssl Secret volume mount in deploy/.../runtime/values.yml.
    return {
        "security.protocol": "SSL",
        "ssl.ca.location": os.environ.get("KAFKA_SSL_CA_PATH", "/etc/kafka/ssl/ca-cert.pem"),
        "ssl.certificate.location": os.environ.get("KAFKA_SSL_CERT_PATH", "/etc/kafka/ssl/access-cert.pem"),
        "ssl.key.location": os.environ.get("KAFKA_SSL_KEY_PATH", "/etc/kafka/ssl/access-key.pem"),
    }


def key_for(value: dict) -> str:
    """The entity a window aggregates over — the partition/state key (ADR 0004 goal 1).

    Keys on `user_id`, which Vector stamps on every sensor event at ingest (defaulting
    to "rods" today) and which derived events carry too (set in `decide`). If it's ever
    missing we bucket under an explicit sentinel and warn — deliberately NOT under
    `source_app`: that would silently fragment one entity's state across two keys
    (user_id vs producer name) and, once multi-user, collapse different users into the
    shared producer bucket. A missing key must be loud and isolated, not plausibly-wrong.
    """
    msg = value.get("message", {}) if isinstance(value, dict) else {}
    user_id = msg.get("user_id")
    if not user_id:
        logger.warning("event has no user_id; bucketing under '_no_user_id' (event_name=%s)",
                       msg.get("event_name"))
        return "_no_user_id"
    return str(user_id)


def to_envelope(result: dict) -> dict:
    """Wrap an engine result in the `high_level_events` Envelope shape — the job
    Vector's classify_domain + enrich_sensor transforms used to do. The worker now
    mints `envelope_id` and stamps the metadata itself.
    """
    name = result["inference_type"]
    return {
        "envelope_id": str(uuid.uuid4()),
        "event_name": name,
        "inference_type": name,
        "message": result["message"],
        "processed_at": time.time(),
        "source_app": name,
        "source_type": "inference_quix",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def decide(spec: dict, name: str, now: int, value: dict, state: State):
    """Weighted-window engine for ONE definition; state keys namespaced by definition
    name so many definitions share one keyed store without colliding.
    """
    wkey, ckey = f"{spec['name']}:window", f"{spec['name']}:last_fired"
    window = state.get(wkey, {})
    window = {k: v for k, v in window.items() if now - v["ts"] <= spec["window"]}
    if name not in window or now < window[name]["ts"]:
        window[name] = {"ts": now, "envelope_id": value.get("envelope_id")}
    state.set(wkey, window)

    score = sum(spec["weights"].get(k, 0) for k in window)
    if score < spec["threshold"]:
        return None
    if now - state.get(ckey, 0) < spec["cooldown"]:
        return None
    state.set(ckey, now)

    occurred_at = sum(v["ts"] for v in window.values()) / len(window)
    return {
        "inference_type": spec["name"],
        "message": {
            "event_name": spec["name"],
            "user_id": key_for(value),
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


def build_runtime() -> Application:
    events_dir = Path(os.environ.get("EVENTS_DIR", "events"))
    definitions = load_definitions(events_dir)
    if not definitions:
        raise RuntimeError(f"No enabled event definitions found under {events_dir}")

    consumers: dict[str, list[dict]] = defaultdict(list)
    sink_for: dict[str, str] = {}
    union_topics: set[str] = set()
    for d in definitions:
        cfg = d.engine_config
        spec = {
            "name": d.name,
            "weights": cfg.get("weights", {}),
            "threshold": cfg["threshold"],
            "window": cfg["window_seconds"],
            "cooldown": cfg.get("cooldown_seconds", 1800),
        }
        for event_name in spec["weights"]:
            consumers[event_name].append(spec)
        sink_for[d.name] = d.sink_topic
        union_topics.update(d.source_topics)

    # Consume only EXTERNAL source topics — those not produced by this runtime.
    # Recursive derivation (a definition consuming another's output, e.g.
    # got_into_the_car ← car_door_opened) is handled IN-PROCESS by the router (see
    # below), so we never re-consume our own sink. This also sidesteps a Quix issue
    # where `concat()` of multiple source topics + auto_offset_reset=latest fails to
    # consume new messages — and keeps us to a single source topic (no concat).
    sink_topics = set(sink_for.values())
    source_topics = sorted(union_topics - sink_topics)

    logger.info("Loaded %d definition(s): %s; consuming %s (external); sinks %s",
                len(definitions), [d.name for d in definitions],
                source_topics, sorted(sink_topics))

    ssl = _ssl_config()
    app = Application(
        broker_address=os.environ["KAFKA_BOOTSTRAP_SERVERS"],
        consumer_group=os.environ.get("QUIX_CONSUMER_GROUP", "inference-quix-runtime-v1"),
        auto_offset_reset="latest",
        consumer_extra_config=ssl,
        producer_extra_config=ssl,
        state_dir=os.environ.get("QUIX_STATE_DIR", "state"),
    )
    sources = {t: app.topic(t, value_deserializer="json") for t in source_topics}
    sinks = {t: app.topic(t, value_serializer="json") for t in sorted(sink_topics)}

    def router(value, state: State):
        """One incoming event in → all derived events out (expand=True), including
        multi-hop derivations resolved IN-PROCESS. A fired event is fed back through
        the same consumers map (queue), so e.g. car_door_opened immediately drives
        got_into_the_car using that entity's persisted window — no Kafka round-trip,
        no need to consume high_level_events. The event_name gatekeeper keeps the
        graph a DAG (terminal events match no consumer and stop the cascade).
        """
        if not isinstance(value, dict):
            return []
        msg = value.get("message") or {}
        queue = [(msg.get("event_name"), int(msg.get("timestamp", 0)), value)]
        out = []
        while queue:
            name, now, val = queue.pop(0)
            for spec in consumers.get(name, []):
                result = decide(spec, name, now, val, state)
                if result:
                    logger.info("FIRED %s user=%s score=%s",
                                result["inference_type"], result["message"]["user_id"],
                                result["message"]["confidence_score"])
                    env = to_envelope(result)
                    out.append(env)
                    queue.append((env["message"]["event_name"],
                                  int(env["message"]["timestamp"]), env))
        return out

    # Single combined stream over external source topics (one topic here → no concat).
    sdf = None
    for t in source_topics:
        stream = app.dataframe(sources[t])
        sdf = stream if sdf is None else sdf.concat(stream)
    sdf = sdf.group_by(key_for, name="entity")
    sdf = sdf.apply(router, stateful=True, expand=True)
    sdf = sdf.to_topic(lambda value, key, ts, headers: sinks[sink_for[value["event_name"]]])
    return app


def run() -> None:
    build_runtime().run()
