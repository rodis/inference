"""Transport-agnostic inference core (ADR 0004).

The derivation logic that engines run inside, with NO dependency on the transport
or state backend. Given an incoming event (a plain envelope dict) and this entity's
state (a `get`/`set` store), it routes the event to the engines that consume it,
resolves recursive derivation IN-PROCESS, and shapes each firing into an emitted
`high_level_events` record. It also builds the `RoutingPlan` — the pure value that
turns loaded definitions into the routing metadata the adapter needs.

**INVARIANT: this module MUST NOT import `quixstreams`** (or any transport/state
backend). The Quix+Kafka binding lives in the adapter `inference.runtime.quix`,
which injects the source events and the per-entity `State` into the pure functions
here. Keeping this module import-clean is what makes the derivation core portable —
a second adapter (an in-memory test harness, a different broker) reuses it unchanged.
The engines depend only on a plain event dict + a `get`/`set` state port + returning
a `Decision`, so they ride along untouched. See doc/adr/0004-scaling-model.md.

The strategy is pluggable: each definition's `engine` string resolves to an
`Engine` (`inference.engines`). This module is strategy-agnostic — it resolves
engines, routes events to them, and shapes/emits the result record.

Config (env-backed settings + constants like the producing APP_NAME) lives in
`inference.runtime.config`.
"""

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import NamedTuple, Protocol

# importing names from the package runs inference/engines/__init__.py, which registers the built-in engines
from inference.engines import Decision, Engine, ScopedState, build_engine
from inference.runtime import config

logger = logging.getLogger("inference.core")


class StateStore(Protocol):
    """The per-entity state *port* the core needs: a namespaced key/value store.

    This is the seam that keeps the core free of any particular state backend. Quix
    `State` satisfies it (the adapter injects one at runtime); so does a plain
    dict-backed stub for tests. `route` wraps whatever it's handed in a `ScopedState`
    (per-definition key prefix), so the raw store only ever sees `get`/`set`.
    """

    def get(self, key: str, default=None): ...
    def set(self, key: str, value) -> None: ...


class Consumer(NamedTuple):
    """An engine bound to the event it produces. `engine.name` is the static engine
    *type* (e.g. "weighted_window"); `produces` is the definition's name — the event
    this engine emits and the key its per-entity state is scoped under.
    """

    produces: str
    engine: Engine


def to_event(name: str, inference_type: str, decision: Decision, user_id: str) -> dict:
    """Shape an engine `Decision` into the full `high_level_events` record.

    `name` is the event produced (the definition's name, e.g. "car_door_opened");
    `inference_type` is the engine *type* that produced it (e.g. "weighted_window").

    The core owns the whole record now — the old `decide → finalize → Vector-re-wraps`
    hop is gone; the adapter produces this straight to Kafka. So this is one step:
    mint the event `id` (inside `message`), build the rest of the `message` (the
    derived event + `derived_from` lineage from the decision's contributors, stamped
    with the entity `user_id`), and add the top-level metadata. Engines only decide;
    all shaping lives here.

    The top-level wrapper is kept identical to the one Vector mints for raw events,
    so every Kafka topic carries the same shape: `name`, `source_app`, `source_type`,
    `message`. `source_type="kafka"` records the entry mechanism (derived events are
    produced straight to Kafka; raw events enter via Vector's `http_server`, so theirs
    reads `"http_server"`). It is metadata only — Vector's persister drops it, so it
    never reaches Neon.

    There is no produce-time / inference-time stamp on the record: the only event-time
    is `message.timestamp` (when the event occurred), and "when the system handled it"
    is the DB-set `ingested_at` column (authoritative persist time). The old wrapper
    `timestamp` (emit time) and `message.processed_at` (inference time) were ~the same
    instant as `ingested_at` and were dropped.

    The per-event id lives in `message.id` — the inference app mints it for derived
    events, Vector mints the same for raw events at ingest (there is no top-level
    "envelope" wrapper id anymore). Lineage is one field: `derived_from`
    (`[{id, name, timestamp}]`). The derived-only `inference_type` (the engine type
    that produced it) also lives in `message`; its presence is how Vector's persister
    keys `event_class=derived` — see deploy/vector/.../shape_for_neon.yml.
    """
    contributors = decision.contributors
    return {
        "name": name,
        "source_app": config.APP_NAME,
        "source_type": "kafka",
        "message": {
            "id": str(uuid.uuid4()),
            "name": name,
            "inference_type": inference_type,
            "user_id": user_id,
            "timestamp": int(decision.occurred_at),   # single event-time field (raw events carry it too); engine windows on it, Neon occurred_at column derives from it
            "confidence_score": decision.score,
            "derived_from": [
                {"id": c["id"], "name": c["name"], "timestamp": c["timestamp"]}
                for c in contributors
            ],
        },
    }


@dataclass(frozen=True)
class RoutingPlan:
    """The routing plan resolved from the event definitions: everything the adapter
    needs to wire the pipeline, computed once from the YAML and transport-agnostic.

    One cohesive value instead of a bag of loose maps — `sink_topics` is a *view* over
    `sink_for` (not separate state to keep in sync), and the declared source topics are
    an internal detail of building the plan, not part of it.

    - `consumers` — input event name → the consumers (engine + produced event) that fire
      on it; the graph a `Router` walks. A name absent from the map is terminal and stops a cascade.
    - `sink_for` — produced event name → the topic it is emitted to.
    - `source_topic` — the single external source topic the runtime consumes.
    """

    consumers: dict[str, list[Consumer]]
    sink_for: dict[str, str]
    source_topic: str

    @property
    def sink_topics(self) -> set[str]:
        """The distinct sink topics — a derived view over `sink_for`."""
        return set(self.sink_for.values())

    @classmethod
    def from_definitions(cls, definitions: list) -> "RoutingPlan":
        """Resolve each definition's engine and derive the whole plan.

        Indexes, per input event name, the consumers that fire on it — each pairing the
        engine with the event it produces (the definition name). Strategy-agnostic from
        here on: the weighted-window specifics live entirely inside the resolved Engine.
        The `source_topic` is whatever's declared but not produced here (our own sinks);
        exactly one is required — see the guard below.
        """
        consumers: dict[str, list[Consumer]] = defaultdict(list)
        sink_for: dict[str, str] = {}
        declared_sources: set[str] = set()
        for d in definitions:
            engine = build_engine(d)
            for input_name in engine.input_event_names():
                consumers[input_name].append(Consumer(produces=d.name, engine=engine))
            sink_for[d.name] = d.sink_topic
            declared_sources.add(d.source_topic)

        # Consume exactly ONE external source (declared sources minus our own sinks).
        # Recursion is in-process, so a second source is never needed; and Quix concat()
        # of multiple sources stalls under auto_offset_reset=latest, so a genuinely
        # separate feed must be merged at ingest (Vector). See doc/adr/0004-scaling-model.md.
        external = sorted(declared_sources - set(sink_for.values()))
        if len(external) != 1:
            raise RuntimeError(
                f"Expected exactly one external source topic, got {external}. "
                "Recursion is in-process (no second source needed) and multi-source concat "
                "stalls with auto_offset_reset=latest; merge separate feeds at ingest "
                "(Vector). See doc/adr/0004-scaling-model.md."
            )

        return cls(consumers=dict(consumers), sink_for=sink_for, source_topic=external[0])


class Router:
    """The routing port the adapter mounts — a `RoutingPlan` plus the behavior that runs it.

    Built from a plan, it is the *single* thing an adapter needs to wire a pipeline: what to
    consume (`source_topic`), how to process (`route`), and where results go (`sink_topics` /
    `sink_for`). So an adapter depends only on `Router`, never on the underlying `RoutingPlan`.

    `route(event, state)` matches the stream framework's stateful-callback signature, so the
    adapter mounts it directly (`apply(router.route, ...)`) with no lambda and the port is
    explicit. Transport-agnostic — it holds only topic *names* + the consumer graph; per-entity
    state flows in per call, so a single Router is shared across all keys.
    """

    def __init__(self, plan: RoutingPlan):
        self._plan = plan

    @staticmethod
    def key_for(event: dict) -> str:
        """The entity a window aggregates over — the partition/state key an adapter shards
        by (the keying policy; ADR 0004 goal 1). Fixed, not plan-derived, hence static;
        exposed on the port so an adapter depends only on `Router`, never a bare function.

        Keys on `user_id`, which Vector stamps on every sensor event at ingest (rejecting
        events without one) and which derived events carry too (stamped in `to_event`). If
        it's ever missing we bucket under an explicit sentinel and warn — deliberately NOT
        under `source_app`: that would silently fragment one entity's state across two keys
        and, once multi-user, collapse different users into the shared producer bucket. A
        missing key must be loud and isolated, not plausibly-wrong.
        """
        msg = event.get("message", {}) if isinstance(event, dict) else {}
        user_id = msg.get("user_id")
        if not user_id:
            logger.warning("event has no user_id; bucketing under '_no_user_id' (name=%s)",
                           msg.get("name"))
            return "_no_user_id"
        return str(user_id)

    @property
    def source_topic(self) -> str:
        """The single external source the pipeline consumes."""
        return self._plan.source_topic

    @property
    def sink_topics(self) -> set[str]:
        """The distinct topics the pipeline emits to."""
        return self._plan.sink_topics

    @property
    def sink_for(self) -> dict[str, str]:
        """Produced event name → the topic it is emitted to."""
        return self._plan.sink_for

    def route(self, event: dict, state: StateStore) -> list[dict]:
        """One incoming event → all derived events (expand semantics), multi-hop resolved
        IN-PROCESS. A fired event is re-enqueued so it can drive further definitions using
        this entity's persisted state — no Kafka round-trip. The consumer graph keeps the
        cascade a DAG: a terminal event matches no consumer and stops.

        `state` is the per-entity store the adapter injects (a `StateStore` port — Quix
        `State` in production), wrapped per produced event in a `ScopedState` so definitions
        share one keyed store without colliding and the core stays backend-agnostic.
        """
        if not isinstance(event, dict):
            return []
        user_id = self.key_for(event)       # entity key for this whole call
        queue, out = [event], []
        while queue:
            ev = queue.pop(0)
            name = (ev.get("message") or {}).get("name")
            for produces, engine in self._plan.consumers.get(name, []):
                decision = engine.decide(ev, ScopedState(state, f"{produces}:"))   # state scoped per produced event
                if decision:
                    logger.info("FIRED %s via %s user=%s score=%s", produces, engine.name, user_id, decision.score)
                    derived = to_event(produces, engine.name, decision, user_id)
                    out.append(derived)
                    queue.append(derived)
        return out
