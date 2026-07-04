"""Quix Streams adapter — binds the transport-agnostic core to Kafka (ADR 0004).

The composition root. It constructs the shared Quix `Application`, wires the one
keyed pipeline, and runs it. All derivation logic lives in `inference.runtime.core`;
this module owns everything Kafka/Quix-specific and injects the source events +
per-entity `State` into the core's pure functions. It replaces the thread-per-event
`RuntimeSupervisor`:

  * no Redis — per-entity state lives in partition-local Quix `State` (RocksDB +
    changelog), single-writer-per-key by construction;
  * no Vector emit hop — the full `high_level_events` record is minted by the core
    (`to_event`) and produced straight to Kafka via `to_topic()` (Vector stays the
    ingest gateway + Neon persister);
  * recursive derivation (ADR 0002) is resolved IN-PROCESS by `core.Router`, not
    re-consumed from Kafka, so the runtime consumes only external source topics.

One shared keyed pipeline (not one branch per definition) because each stateful
operator + `group_by` mints Kafka topics, and the Aiven free tier caps user topics
at 5; the shared router costs 1 repartition + 1 changelog regardless of definition
count. See [`doc/adr/0004-scaling-model.md`](../../../doc/adr/0004-scaling-model.md).

Config (env-backed settings) lives in `inference.runtime.config`; env is set by the
K8s ConfigMap/Secret + image ENV, or `workers/.env` locally.
"""

import logging

from quixstreams import Application

from inference.runtime import config
from inference.runtime.core import RoutingPlan, Router
from inference.runtime.definition import load_definitions

logger = logging.getLogger("inference.quix")


def _wire_topology(app: Application, router: Router) -> None:
    """Wire the one keyed pipeline: consume the source → `group_by` entity key → the
    stateful `router.route` (expand=True) → route each produced event to its sink topic.

    Depends only on the `Router` (the routing port) — never on the underlying `RoutingPlan`
    or any bare core function. This is the adapter↔core seam: `group_by(router.key_for)`
    injects the core keying policy, and the stateful `apply` hands the `Router` its event +
    the per-entity Quix `State`.
    """
    sinks = {t: app.topic(t, value_serializer="json") for t in sorted(router.sink_topics)}
    sdf = app.dataframe(app.topic(router.source_topic, value_deserializer="json"))
    sdf = sdf.group_by(router.key_for, name="entity")
    sdf = sdf.apply(router.route, stateful=True, expand=True)
    sdf.to_topic(lambda value, key, ts, headers: sinks[router.sink_for[value["name"]]])


def build_runtime() -> Application:
    definitions = load_definitions(config.EVENTS_DIR)
    if not definitions:
        raise RuntimeError(f"No enabled event definitions found under {config.EVENTS_DIR}")

    router = Router(RoutingPlan.from_definitions(definitions))
    logger.info("Loaded %d definition(s): %s; consuming %s; sinks %s",
                len(definitions), [d.name for d in definitions],
                router.source_topic, sorted(router.sink_topics))

    ssl = config.kafka_ssl()
    app = Application(
        broker_address=config.kafka_bootstrap(),
        consumer_group=config.CONSUMER_GROUP,
        auto_offset_reset="latest",
        consumer_extra_config=ssl,
        producer_extra_config=ssl,
        state_dir=config.STATE_DIR,
    )
    _wire_topology(app, router)
    return app


def run() -> None:
    build_runtime().run()
