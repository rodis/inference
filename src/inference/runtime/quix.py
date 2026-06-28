"""Generic Quix Streams runtime (ADR 0004).

Loads every `events/*.yml` and runs them all on ONE shared Quix `Application`: one
consumer group, one process, partition-keyed state. See
[`doc/adr/0004-scaling-model.md`](../../../doc/adr/0004-scaling-model.md).

It replaces the thread-per-event `RuntimeSupervisor`:
  * no Redis — per-entity state lives in partition-local Quix `State` (RocksDB +
    changelog), single-writer-per-key by construction;
  * no Vector emit hop — the full `high_level_events` event record is minted here and
    produced straight to Kafka via `to_topic()` (Vector stays the ingest gateway +
    Neon persister);
  * recursive derivation (ADR 0002) is resolved IN-PROCESS — a fired event is fed
    back through the router within the same call (see `_route`), not re-consumed
    from Kafka, so the runtime consumes only external source topics.

The strategy is pluggable: each definition's `engine` string resolves to an
`Engine` (`inference.engines`). This module is strategy-agnostic — it resolves
engines, routes events to them, and shapes/emits the result envelope.

One shared keyed router (not one branch per definition) because each stateful
operator + `group_by` mints Kafka topics, and the Aiven free tier caps user topics
at 5; the router costs 1 repartition + 1 changelog regardless of definition count.

Config (env-backed settings + constants like the producing APP_NAME) lives in
`inference.runtime.config`; env is set by the K8s ConfigMap/Secret + image ENV, or
`workers/.env` locally.
"""

import logging
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from quixstreams import Application
from quixstreams.state import State

# importing names from the package runs inference/engines/__init__.py, which registers the built-in engines
from inference.engines import Decision, ScopedState, build_engine
from inference.runtime import config
from inference.runtime.definition import load_definitions

logger = logging.getLogger("inference.quix")


def key_for(value: dict) -> str:
    """The entity a window aggregates over — the partition/state key (ADR 0004 goal 1).

    Keys on `user_id`, which Vector stamps on every sensor event at ingest (rejecting
    events without one) and which derived events carry too (stamped in `finalize`). If
    it's ever missing we bucket under an explicit sentinel and warn — deliberately NOT
    under `source_app`: that would silently fragment one entity's state across two keys
    and, once multi-user, collapse different users into the shared producer bucket. A
    missing key must be loud and isolated, not plausibly-wrong.
    """
    msg = value.get("message", {}) if isinstance(value, dict) else {}
    user_id = msg.get("user_id")
    if not user_id:
        logger.warning("event has no user_id; bucketing under '_no_user_id' (event_name=%s)",
                       msg.get("event_name"))
        return "_no_user_id"
    return str(user_id)


def to_event(name: str, decision: Decision, user_id: str) -> dict:
    """Shape an engine `Decision` into the full `high_level_events` record.

    The runtime owns the whole record now — the old `decide → finalize →
    Vector-re-wraps` hop is gone; we produce straight to Kafka. So this is one step:
    mint the event `id` (inside `message`), build the rest of the `message` (the
    derived event + `derived_from` lineage from the decision's contributors, stamped
    with the entity `user_id`), and add the top-level metadata. Engines only decide;
    all shaping lives here.

    The top-level wrapper is kept identical to the one Vector mints for raw events,
    so every Kafka topic carries the same shape: `event_name`, `source_app`,
    `source_type`, `timestamp`, `message`. `source_type="kafka"` records the entry
    mechanism (derived events are produced straight to Kafka; raw events enter via
    Vector's `http_server`, so theirs reads `"http_server"`). It is metadata only —
    Vector's persister drops it, so it never reaches Neon.

    The per-event id lives in `message.id` — the inference app mints it for derived
    events, Vector mints the same for raw events at ingest (there is no top-level
    "envelope" wrapper id anymore). Lineage is one field: `derived_from`
    (`[{id, event_name, timestamp}]`). Derived-only metadata lives in `message`:
    `inference_type` (its presence is how Vector's persister keys `event_class=derived`
    — see deploy/vector/.../shape_for_neon.yml) and `processed_at`.
    """
    contributors = decision.contributors
    return {
        "event_name": name,
        "source_app": config.APP_NAME,
        "source_type": "kafka",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": {
            "id": str(uuid.uuid4()),
            "event_name": name,
            "inference_type": name,
            "user_id": user_id,
            "timestamp": int(decision.occurred_at),
            "confidence_score": decision.score,
            "occurred_at": decision.occurred_at,
            "processed_at": time.time(),
            "derived_from": [
                {"id": c["id"], "event_name": c["event_name"], "timestamp": c["timestamp"]}
                for c in contributors
            ],
        },
    }


def _route(value: dict, state: State, consumers: dict) -> list[dict]:
    """One incoming event → all derived events (expand=True), multi-hop resolved
    IN-PROCESS. A fired event is re-enqueued so it can drive further definitions
    using this entity's persisted state — no Kafka round-trip. The consumers index
    (event_name → engines) keeps the graph a DAG: a terminal event matches no engine
    and stops the cascade.
    """
    if not isinstance(value, dict):
        return []
    user_id = key_for(value)            # entity key for this whole call (group_by scoped state to it)
    queue, out = [value], []
    while queue:
        event = queue.pop(0)
        name = (event.get("message") or {}).get("event_name")
        for engine in consumers.get(name, []):
            decision = engine.decide(event, ScopedState(state, f"{engine.name}:"))
            if decision:
                logger.info("FIRED %s user=%s score=%s", engine.name, user_id, decision.score)
                derived = to_event(engine.name, decision, user_id)
                out.append(derived)
                queue.append(derived)
    return out


def build_runtime() -> Application:
    definitions = load_definitions(config.EVENTS_DIR)
    if not definitions:
        raise RuntimeError(f"No enabled event definitions found under {config.EVENTS_DIR}")

    # Resolve each definition's engine (by its `engine` string) and index which event
    # names each engine consumes. Both are strategy-agnostic from here on — the
    # weighted-window specifics live entirely inside the resolved Engine.
    consumers: dict[str, list] = defaultdict(list)
    sink_for: dict[str, str] = {}
    declared_sources: set[str] = set()
    for d in definitions:
        engine = build_engine(d)
        for event_name in engine.input_event_names():
            consumers[event_name].append(engine)
        sink_for[d.name] = d.sink_topic
        declared_sources.add(d.source_topic)

    # The runtime consumes exactly ONE external source topic — the declared sources that
    # aren't produced by this runtime. Recursive derivation is resolved IN-PROCESS by
    # `_route` (a definition's derived contributors are never re-consumed from Kafka), so
    # a second source is never needed; and Quix `concat()` of multiple sources stalls
    # under auto_offset_reset=latest. A genuinely separate feed must be merged into this
    # one topic at the edge (Vector). Allowing multiple sources is future work — see
    # doc/adr/0004-scaling-model.md.
    sink_topics = set(sink_for.values())
    external_sources = sorted(declared_sources - sink_topics)
    if len(external_sources) != 1:
        raise RuntimeError(
            f"Expected exactly one external source topic, got {external_sources}. "
            "Recursion is in-process (no second source needed) and multi-source concat "
            "stalls with auto_offset_reset=latest; merge separate feeds at ingest "
            "(Vector). See doc/adr/0004-scaling-model.md."
        )
    [source_topic] = external_sources

    logger.info("Loaded %d definition(s): %s; consuming %s; sinks %s",
                len(definitions), [d.name for d in definitions],
                source_topic, sorted(sink_topics))

    ssl = config.kafka_ssl()
    app = Application(
        broker_address=config.kafka_bootstrap(),
        consumer_group=config.CONSUMER_GROUP,
        auto_offset_reset="latest",
        consumer_extra_config=ssl,
        producer_extra_config=ssl,
        state_dir=config.STATE_DIR,
    )
    sinks = {t: app.topic(t, value_serializer="json") for t in sorted(sink_topics)}

    sdf = app.dataframe(app.topic(source_topic, value_deserializer="json"))
    sdf = sdf.group_by(key_for, name="entity")
    sdf = sdf.apply(lambda value, state: _route(value, state, consumers), stateful=True, expand=True)
    sdf = sdf.to_topic(lambda value, key, ts, headers: sinks[sink_for[value["event_name"]]])
    return app


def run() -> None:
    build_runtime().run()
