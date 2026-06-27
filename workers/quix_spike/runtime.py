"""Generic Quix runtime — ADR 0004 step 5 (the actual 1:1-unbinding migration).

Loads EVERY `events/*.yml` definition (via the SAME `inference.runtime.definition.
load_definitions` the current runtime uses) and runs them all on ONE shared Quix
`Application` — one consumer group, one process — instead of the thread-per-event
`RuntimeSupervisor`. Adding an event becomes a YAML change, not another consumer
or thread. That is the unbinding we set out to achieve.

WHY ONE SHARED ROUTER, not one branch per definition
-----------------------------------------------------
The step-3 finding bites here: Aiven free-0 caps at **5 user topics**, and every
stateful Quix operator mints a **changelog** (+ a **repartition** topic per
`group_by`). The textbook "one branch per EventDefinition" would need N changelogs
+ N repartition topics — over budget at N=3. So the runtime is a SINGLE keyed
stateful **router** that loads all definitions as DATA and keeps a per-(definition,
entity) window in namespaced state. Cost: **1 repartition + 1 changelog, regardless
of how many events you add.** The definitions stay the source of truth; only the
execution shape changed. (On a paid plan, per-definition branches read cleaner.)

Recursive derivation (ADR 0002) falls out for free: a fired event is produced to
its sink (`high_level_events`), which is also a source topic, so the same router
re-consumes it and feeds the next definition — all in one process. The gatekeeper
(event must be in some definition's `weights`) drops terminal events, so the graph
stays a DAG.

    cd workers/quix_spike && python runtime.py     # runs ALL events/*.yml at once
"""

import logging
import os
from collections import defaultdict
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from quixstreams import Application
from quixstreams.state import State

if dotenv_path := find_dotenv(usecwd=True, raise_error_if_not_found=False):
    load_dotenv(dotenv_path)

from inference.runtime.definition import load_definitions  # noqa: E402  (after dotenv)
from main import key_for, to_envelope                      # noqa: E402  (shared pure helpers)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("quix_runtime")

EVENTS_DIR = Path(os.environ.get("EVENTS_DIR", Path(__file__).resolve().parents[2] / "events"))
CONSUMER_GROUP = os.environ.get("SPIKE_CONSUMER_GROUP", "quix-spike-runtime-v1")

_SSL = {
    "security.protocol": "SSL",
    "ssl.ca.location": os.environ.get("KAFKA_SSL_CA_PATH", "/etc/kafka/ssl/ca-cert.pem"),
    "ssl.certificate.location": os.environ.get("KAFKA_SSL_CERT_PATH", "/etc/kafka/ssl/access-cert.pem"),
    "ssl.key.location": os.environ.get("KAFKA_SSL_KEY_PATH", "/etc/kafka/ssl/access-key.pem"),
}


def decide(spec: dict, name: str, now: int, value: dict, state: State):
    """The weighted-window engine for ONE definition, with state keys namespaced by
    definition name (`<def>:window`, `<def>:last_fired`) so many definitions share
    one keyed state store without colliding. Returns a finalize()-style result or None.
    """
    wkey, ckey = f"{spec['name']}:window", f"{spec['name']}:last_fired"
    window = state.get(wkey, {})
    window = {k: v for k, v in window.items() if now - v["ts"] <= spec["window"]}  # prune
    if name not in window or now < window[name]["ts"]:
        window[name] = {"ts": now, "envelope_id": value.get("envelope_id")}
    state.set(wkey, window)

    if sum(spec["weights"].get(k, 0) for k in window) < spec["threshold"]:
        return None
    if now - state.get(ckey, 0) < spec["cooldown"]:                                # cooldown
        return None
    state.set(ckey, now)

    occurred_at = sum(v["ts"] for v in window.values()) / len(window)
    score = sum(spec["weights"].get(k, 0) for k in window)
    return {
        "inference_type": spec["name"],
        "message": {
            "event_name": spec["name"],
            "vehicle_id": key_for(value),
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
    definitions = load_definitions(EVENTS_DIR)

    # Index: which definitions consume each event_name, and where each one sinks.
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

    log.info("Loaded %d definitions; consuming %s; sinks %s",
             len(definitions), sorted(union_topics), sorted(set(sink_for.values())))

    app = Application(
        broker_address=os.environ["KAFKA_BOOTSTRAP_SERVERS"],
        consumer_group=CONSUMER_GROUP,
        auto_offset_reset="latest",
        consumer_extra_config=_SSL,
        producer_extra_config=_SSL,
        state_dir=os.environ.get("SPIKE_STATE_DIR", "state"),
    )
    sources = {t: app.topic(t, value_deserializer="json") for t in sorted(union_topics)}
    sinks = {t: app.topic(t, value_serializer="json") for t in sorted(set(sink_for.values()))}

    def router(value, state: State):
        """One event in → zero or more derived events out (expand=True). Each
        incoming event is offered to every definition that lists it in `weights`."""
        if not isinstance(value, dict):
            return []
        msg = value.get("message") or {}
        name = msg.get("event_name")
        now = int(msg.get("timestamp", 0))
        out = []
        for spec in consumers.get(name, []):
            result = decide(spec, name, now, value, state)
            if result:
                out.append(to_envelope(result))
        return out

    # Merge all source topics into one stream, key by entity once, route statefully.
    sdf = None
    for t in sorted(union_topics):
        stream = app.dataframe(sources[t])
        sdf = stream if sdf is None else sdf.concat(stream)
    sdf = sdf.group_by(key_for, name="entity")              # 1 repartition topic
    sdf = sdf.apply(router, stateful=True, expand=True)     # 1 changelog topic
    sdf = sdf.update(lambda v: log.info(
        "🔥 FIRED %s vehicle=%s score=%s",
        v["event_name"], v["message"]["vehicle_id"], v["message"]["confidence_score"]))
    # Per-record sink routing: each derived event goes to its definition's sink_topic.
    sdf = sdf.to_topic(lambda value, key, ts, headers: sinks[sink_for[value["event_name"]]])
    return app


if __name__ == "__main__":
    log.info("Generic Quix runtime starting (group=%s, events=%s)", CONSUMER_GROUP, EVENTS_DIR)
    build_runtime().run()
