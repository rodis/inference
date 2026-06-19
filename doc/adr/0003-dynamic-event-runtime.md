# ADR 0003 — Dynamic event runtime: YAML-defined, multi-handler, single deployment

Status: **Accepted — Phase 1 implemented (runtime + loader + single deployment); Phases 2–3 pending**
Date: 2026-06-19

> Decision record for the **multi-handler runtime** that ADR 0001 deferred (the "Worker facade" /
> event-definition open question) and ADR 0002 named as the fix for deployment-granularity waste.
> It builds on [`0001-message-shaping-pipeline.md`](0001-message-shaping-pipeline.md) (decide → enrich
> → emit, the engine/transport split) and [`0002-recursive-derivation.md`](0002-recursive-derivation.md)
> (entailment vs correlation, the no-cycles constraint). Sections describing the runtime are
> **target-state**; nothing here is implemented yet. The normative docs
> ([`architecture.md`](../architecture.md), [`invariants.md`](../invariants.md),
> [`classes.md`](../classes.md), [`CLAUDE.md`](../../CLAUDE.md)) are updated when each phase lands.

---

## Context

The project's value is **experimenting with inferences** — creating, tuning, and retiring derived
events cheaply. The current structure makes that expensive because two things bind an *event* to
*infrastructure*:

1. **Identity is the directory.** `WORKER_NAME = Path(__file__).parent.name` ([worker `main.py`](../../workers/car_door_opened/main.py))
   — an event *is* a source tree. The emitted `event_name`, Redis keys, consumer group, image name,
   and Vector path all derive from the folder.
2. **Deployment is per-event.** One process = one engine = one pod (see [`architecture.md`](../architecture.md)).
   An event *is* a Deployment.

So "create an event" today means: new `workers/<name>/` dir + `main.py` + `Dockerfile` + a
`deploy/kustomize/base/<slug>/` triplet + an ArgoCD `Application` + a per-event CI image build +
(optionally) a `@register` message class. "Rename an event" means all of the above plus an
orphan-delete of the live ArgoCD app and a Kafka replay (observed in the `home_arrival → car_door_opened`
rename: cascade risk to shared infra, and a wall-clock-cooldown replay that collapsed history into one
backfilled fire). This is the opposite of dynamic, and it dominates the cost of every experiment.

The config planes are also inverted (see ADR 0001 follow-up): the thing changed *most* often (rules:
weights, threshold, window) is baked into the image (hardest path — rebuild), while the thing changed
*rarely* (broker URL) is a runtime ConfigMap (cheapest path).

## Decision

**The unit of change becomes an event *definition* (data), not a worker (code + pod).** A single
**generic runtime** process loads many definitions and runs one **handler** per definition. Identity
moves from the filesystem to the definition's `name` field.

Chosen model (vs a runtime-CRUD/DB control plane — see [Alternatives](#alternatives-considered)):
**gitops YAML loaded at startup.** Definitions are YAML in the repo, surfaced to the runtime as a
Kubernetes ConfigMap via kustomize `configMapGenerator`. Editing a definition changes the ConfigMap's
content hash, which rolls the single Deployment automatically — so a change is a **commit + rollout**,
never a new pod / Dockerfile / ArgoCD app / per-event image build.

### Definition schema (target)

One YAML per event under `events/` (snake_case filename = `name`). Fields are today's `RULES` + the
wiring constants from `main.py`, lifted into data:

```yaml
# events/car_door_opened.yml
name: car_door_opened          # identity — replaces WORKER_NAME (source of truth is this field)
enabled: true                  # quick experiment toggle (skip-load when false)
engine: weighted_window        # selects the engine class (future: static_entailment, ADR 0002)
engine_config:
  threshold: 10
  window_seconds: 600
  cooldown_seconds: 10
  weights:
    car_lock_state_change: 5
    device_connected_to_carplay: 5
source_topics: [raw_sensors]
sink_topic: high_level_events
event_domain: sensors
enrichers:                     # ordered chain; sets availability + order + config (ADR 0001)
  - lineage
  - { geo: { strategy: centroid } }
capabilities: [geo_located, derived]   # see open question on typed messages
```

### Generic runtime (target)

Replaces the per-worker `main.py` with **one** entrypoint:

1. Load + validate all `events/*.yml` (a Pydantic `EventDefinition` model — strict). A malformed or
   disabled definition is **skipped and logged**, never fatal to the others.
2. For each definition: instantiate the engine (by `engine` key), the `EnrichmentPipeline` (by
   `enrichers`), the observer, the emitter (Vector URL from `event_domain`/`name`/`sink_topic`), and a
   Kafka consumer.
3. Run handlers concurrently and supervise them.

`config.py` keeps owning only infra (Kafka, Vector). Per ADR 0001's Engine-Owned Infrastructure rule,
engines still read their own backend (Redis) config from env — the runtime does not plumb it.

### Identity, derived (unchanged formula, new source)

The snake_case `name` → kebab `slug` derivation is **kept**, but its source is the definition field,
not the directory:

- `name` (snake_case) — emitted `event_name`, Redis keys `inference:<name>:*`, `RULES["name"]`, logger.
- `slug = name.replace("_", "-")` — Kafka consumer group `inference-<slug>-v1`.

This **replaces** the "directory name is the source of truth for worker identity" invariant
([`invariants.md`](../invariants.md), CLAUDE.md) with "the definition `name` is the source of truth."
The copy-paste-drift protection that the directory rule gave us is now given by definitions being data
(no `main.py` to copy).

### Concurrency model

**One Kafka consumer per definition**, each in its own thread, each with its own consumer group
(`inference-<slug>-v1`). Rationale: preserves today's per-event offset independence and isolation
(one event's lag/rebalance doesn't couple to another's), and is the smallest semantic change from the
current one-consumer-per-pod model. The work is IO-bound (Kafka poll + Redis round-trips), so the GIL
is not a bottleneck and `confluent_kafka`'s blocking C consumer fits a thread-per-handler design.
(Alternative — one shared consumer over the union of source topics, dispatching by `event_name` —
is cheaper on connections but couples offsets/rebalancing across events; rejected for now.)

New groups use **`auto.offset.reset=latest`** (`runtime/builder.py`): a newly added event (or a new
topic added to an existing group) starts at the **tail**, not the beginning. This applies only when
the group has no committed offset, so existing handlers are unaffected. It is the fix for the
new-event **replay** problem (a new `earliest` group replays all history, which the wall-clock
cooldown vs event-time window collapses into junk fires + log spam). Trade-off: a new event doesn't
backfill its window from history — desired, since replay-backfill was useless anyway.

**Deferred: shared-consumer + dispatcher fan-out.** A single union consumer → dispatch queue →
per-handler queues (read shared topics once; add a handler without minting a consumer group) was
evaluated and **deferred (YAGNI)**. At current scale (2–3 handlers, low volume) per-handler consumers
+ `latest` are simpler *and* more robust. Revisit when **either** (a) runtime hot-reload of definitions
becomes a goal — add/remove a handler without rolling the pod, where "add a thread + queue" beats
"mint a group + trigger a rebalance" — **or** (b) handler count / topic-read volume grows enough that
N× reads or rebalance churn is a measured cost. Constraints a future implementer must respect (learned
in review, so they aren't relearned): a `confluent_kafka` Consumer is single-thread only (poll +
commit on one thread); the poll thread must never block longer than `max.poll.interval.ms` or it's
evicted (so backpressure is drop-with-log, never block-the-consumer); **at-least-once watermark commit
is mandatory** because the engine is loss-sensitive (a dropped contributor silently suppresses a fire,
while duplicates are safe via the idempotent `zadd` + cooldown lock); and the fan-out **loses
per-handler crash isolation** (one shared consumer thread stalls every event).

### Deployment

- **One image** (`inference-runtime`) built once by CI — not one per event.
- **One** `deploy/kustomize/base/runtime/` (helmChart + values) and **one** ArgoCD `Application`.
- Definitions ship via `configMapGenerator` over `events/*.yml`, mounted read-only into the runtime.
  A YAML edit → new ConfigMap hash → ArgoCD rolls the Deployment → handlers reload. No image rebuild
  needed to change a definition (only to change runtime *code*).

## Phased rollout

1. **Phase 1 — generic runtime + loader. ✅ IMPLEMENTED.** `EventDefinition` model + loader
   (`runtime/definition.py`), engine/enricher registries (`runtime/registry.py`), builder
   (`runtime/builder.py`), thread-per-handler supervisor (`runtime/supervisor.py`), and a generic
   entrypoint (`workers/runtime/main.py`). `car_door_opened` ported to `events/car_door_opened.yml`;
   the per-worker `workers/car_door_opened/` dir and its deploy/argocd files removed; collapsed to one
   `runtime` Deployment / one `inference-runtime` ArgoCD app. Identity rule (`invariants.md`),
   `CLAUDE.md`, and `architecture.md` updated. Definitions are baked into the image (Phase 2 moves them
   to a ConfigMap). `KafkaStreamHandler.start(..., handle_signals=False)` + `stop()` added so handlers
   run in threads (signal.signal is main-thread-only).
2. **Phase 2 — ConfigMap wiring.** `configMapGenerator` over `events/*.yml` so a YAML edit rolls the
   Deployment with no image rebuild. Document the "edit YAML → rollout" loop.
3. **Phase 3 — engine plurality.** Register `static_entailment` (ADR 0002) as a second `engine` value;
   entailment handlers now co-reside in the runtime with correlation handlers — the deployment-waste
   fix ADR 0002 wanted, for free.

## Open questions

- **Typed messages for YAML events.** `@register`/capability mixins (`GeoLocated`, `Derived`) are
  Python classes; YAML definitions can't declare nominal types. Options: (a) a generic
  `DerivedMessage` shape for all YAML-defined emitted events, with capabilities declared in YAML
  (`capabilities: [...]`) and mapped to mixins by the runtime; (b) keep `@register` classes opt-in
  (code) for events needing strict validation, YAML events default to generic. Leaning (a) for emitted
  events; the nominal-`isinstance` enricher gate (ADR 0001 Phase 2b) needs a story for runtime-declared
  capabilities.
- **Per-handler failure isolation.** One pod runs all handlers — a crash/leak in one affects all (vs
  pod-per-event isolation). Thread supervision + restart? Best-effort per-handler error boundaries
  exist in the pipeline but not around the consume loop.
- **Independent scaling.** A single hot event can't be scaled apart from the rest. Acceptable now;
  future sharding (group definitions across N runtime replicas) is out of scope.
- **Replay on add.** A new definition = a new consumer group = replay from `earliest`, re-triggering the
  wall-clock-cooldown collapse (see the cooldown/replay note). Decide `latest` default, offset
  pre-seeding, or an event-time cooldown — likely its own change.
- **Forward path to DB-driven.** The YAML schema should map 1:1 to a future `event_definitions` table
  so the runtime-CRUD north star (deferred) is not a rewrite — just a different loader + hot-reload.

## Alternatives considered

- **Runtime CRUD via Postgres + hot-reload (the "very dynamic" end-state).** Definitions as DB rows,
  created/updated/deleted via API, runtime hot-loads handlers with no rollout. Rejected *for now* —
  needs hot-reload, consumer-group/offset lifecycle management, validation, and a control surface
  (API/UI). Kept as the north star; the YAML schema is designed to migrate to it.
- **Keep one-pod-per-event, only lift `RULES` into a ConfigMap.** Fixes the inverted config plane but
  not the per-event deployment cost (still a pod/Dockerfile/ArgoCD app per event). Half a fix.
- **Shared single consumer dispatching by `event_name`.** Fewer Kafka connections, but couples
  offsets and rebalancing across events. Rejected; revisit only if connection count becomes a problem.

---

When a phase lands, update the normative docs per the "update docs alongside behavior" rule in
[`CLAUDE.md`](../../CLAUDE.md) — in particular the worker-identity invariant and the "Adding a new
worker" section both change meaning under this ADR.
