# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Required reading

The [`doc/`](doc/) folder is the source of truth for architecture and design rules. Read these before non-trivial changes:

- [`doc/adr/0004-scaling-model.md`](doc/adr/0004-scaling-model.md) — **the current architecture.** Why the runtime is a single Quix Streams `Application`, the state/partition co-location keystone, and the deploy findings.
- [`doc/adr/0002-recursive-derivation.md`](doc/adr/0002-recursive-derivation.md) — entailment vs correlation; recursive derivation (now resolved in-process by the Quix router).
- [`doc/adr/0005-session-gated-derivation.md`](doc/adr/0005-session-gated-derivation.md) — the `session_gated_window` engine: a start entails its end, so an open session relaxes the end-detector (the required `trigger` fires on the gate alone), with the trigger kept necessary as the guardrail.
- [`doc/adr/0006-car-native-trip-signals.md`](doc/adr/0006-car-native-trip-signals.md) — **proposed, not implemented.** Fuse car-native BMW CarData signals into the `got_into`/`got_out` weight maps via an HA-independent MQTT subscriber (a producer into `raw_sensors`, no new topic). Reliability comes from a *second independent source*, not more phone signals; start (`isMoving`→true) and end (a park-confirm, never raw `isMoving`-false) are asymmetric.
- [`doc/vector-pipeline.md`](doc/vector-pipeline.md) — **current truth for Vector.** The ingest + persist + metrics lanes with a graph, and the two-level ingest URL grammar (`/<domain>/<app>`): domain routes to a topic (first level), app routes to a body adapter within that domain (second level). Supersedes ADR 0001's Vector-transform description.
- [`doc/invariants.md`](doc/invariants.md), [`doc/architecture.md`](doc/architecture.md), [`doc/classes.md`](doc/classes.md) — **partially stale.** They describe the pre-Quix threaded runtime; each carries a banner pointing here. Treat as historical until reconciled.
- [`doc/adr/0001-message-shaping-pipeline.md`](doc/adr/0001-message-shaping-pipeline.md), [`0003-dynamic-event-runtime.md`](doc/adr/0003-dynamic-event-runtime.md) — **superseded by 0004.** Historical decision records (the typed-message/enricher pipeline and the threaded one-process-many-handlers runtime). The code they describe has been removed; the ideas live in git history.

When you modify behavior one of these documents describes, update the document in the same change.

## Big-picture model

An inference event is **data** — a YAML file in [`events/`](events/). A single generic **Quix Streams runtime** loads every definition and runs them all in one `Application`, one process, one consumer group:

- entrypoint [`workers/runtime/quix_main.py`](workers/runtime/quix_main.py) → [`inference.runtime.quix`](src/inference/runtime/quix.py) (`build_runtime()` / `run()`);
- definitions loaded by [`inference.runtime.definition.load_definitions`](src/inference/runtime/definition.py) (the `EventDefinition` schema).

The runtime is two small modules split on one rule — **the derivation core never imports `quixstreams`** (so it's portable across transports/state backends and drivable in-memory for tests; a second adapter reuses it unchanged):

- [`core.py`](src/inference/runtime/core.py) — the **transport-agnostic inference core**: entity keying, routing + in-process recursion, output shaping, and the pure definition→topology planning. Everything here is plain functions over dicts + a `get`/`set` state port (`StateStore`); no Kafka/Quix.
- [`quix.py`](src/inference/runtime/quix.py) — the **Quix/Kafka adapter** and composition root: builds the `Application`, wires the one keyed pipeline, injects the source events + per-entity `State` into the core, and runs it.

Read them as:

- **`core.Router.key_for(event)`** — the **entity key** a window aggregates over (`message.user_id`; sentinel `_no_user_id` + warning if missing). Partition + state-ownership unit; a static method on `Router` (the keying policy is part of the port), which the adapter feeds to `group_by(router.key_for)`.
- **Engines ([`inference.engines`](src/inference/engines/))** — the *strategy*, resolved from the definition's `engine` string via a registry. Six built-ins today. Three share a prune-window-then-score shape: `weighted_window` (weighted sum of distinct contributors vs a threshold, event-time cooldown), `decaying_window` (same, but each contributor's weight fades with age — `half_life_seconds`), and `naive_bayes_window` (log-odds from a prior + per-signal likelihood ratios; `score` is a calibrated posterior, and an `lr < 1` lets a signal count as evidence *against*; no definition uses it today — it drove the former `car_door_closed`, removed in ADR 0005). `session_window` is a different strategy: it pairs a *start* + *end* event into one session (`got_into_the_car` + `got_out_the_car` → `car_trip`), holding the open start in state until the end closes it. `session_gated_window` (ADR 0005) is a weighted window plus a **latched gate**: `got_out_the_car` scores the raw exit signals (any 2-of-3 fires it), and while an open-trip gate (`got_into_the_car`, consumed on fire) is set it adds a bonus weight so a single *reliable* signal (a CarPlay-disconnect) closes a trip we already know is open — weights are tuned (ADR 0005) so the direction-ambiguous lock and the noisy charger-disconnect can't single-fire it (guarding against a lock at entry or a mid-drive unplug). `geofence` does **server-side geofencing**: it consumes the raw `location_ping` stream (OwnTracks HTTP), tests each point against a region (haversine vs radius, per-user owner + accuracy gate), and fires on the containment edge — `entered_<slug>`/`left_<slug>` — which then feed downstream derivations (e.g. `arrived_home_by_car` / `left_home_by_car`) via in-process recursion. Its region definitions are **data in Neon**, not YAML: `inference.runtime.regions.load_region_definitions` reads the `regions` table and expands each row into two `entered_/left_` geofence definitions at startup (so the phone stays a dumb lat/lon sensor and regions are stable/shareable/dashboard-editable). This adds the runtime's **only Neon read** — best-effort, so a Neon blip degrades to "no region events," never a crash. Each exposes `input_event_names()` (drives routing) + `decide(event, state) -> Decision | None`. Per-entity state (Quix `State` = RocksDB + changelog in production, any `StateStore` otherwise) is scoped per definition via `ScopedState` (keys `<def>:window`/`<def>:last_fired`), so definitions share one store without colliding. **No Redis.**
- **`core.to_event(name, inference_type, decision, user_id)`** — core-side shaping in one step: mint the full `high_level_events` event record from an engine `Decision` (`name` = produced event, `inference_type` = engine type). The top-level wrapper is **identical to the one Vector mints for raw events** (`name`, `source_app`, `source_type`, `message`) so every Kafka topic carries the same shape; `source_type` records the entry mechanism (`"kafka"` for derived, `"http_server"` for raw) and is metadata only — the persister drops it, it never reaches Neon. The `message` holds the per-event `id` (no top-level "envelope" id anymore — minted here for derived, by Vector for raw), the `derived_from` lineage, the entity `user_id`, the event-time `timestamp`, and the derived-only `inference_type`. **Time:** the only event-time is `message.timestamp`; "when the system handled it" is the DB-set `ingested_at` column — the old wrapper produce-time `timestamp` and `message.processed_at` (both ~= `ingested_at`) were dropped. The old `decide → finalize → Vector-re-wrap` hop is gone — we produce straight to Kafka — so engines only decide; all shaping lives here.
- **`quix.build_runtime()` / `core.Router`** — `build_runtime` (adapter) builds a `core.RoutingPlan.from_definitions(...)` — one value holding the `name → engines` consumers index (from `input_event_names()`), the produced-name → sink map (`sink_for`, with `sink_topics` a derived view), and the single external `source_topic` — then wires the topology: consume the external source → `group_by(router.key_for)` → one stateful `core.Router(plan).route` (`expand=True`) → `to_topic(sink)`. `core.Router` is the shared router — its `route(event, state)` matches the stateful-callback signature (mounted with no lambda) and calls `engine.decide(...)`.

Two things that are *not* obvious from any single file:

1. **One shared keyed router (`core.Router`), all definitions as data** (not one consumer/branch per event). `Router.route` loops the engines that consume each incoming event. This is forced by the Aiven free-tier **5-topic cap**: per-definition branches would mint N changelog + N repartition topics; the shared router costs **1 repartition + 1 changelog regardless of definition count**. See ADR 0004.

2. **Recursion is resolved in-process, not through Kafka.** The runtime consumes the **one external** source topic (the definitions' `source_topic` minus sinks — i.e. `raw_sensors`, *not* `high_level_events`; exactly one is required today, see ADR 0004). When the router fires a derived event, it feeds that event back through the consumers map within the same call (a queue), using the entity's persisted window — so e.g. `got_into_the_car` immediately opens `got_out_the_car`'s gate, and `got_out_the_car` immediately drives `car_trip`. Derived events are still produced to `high_level_events` (for persistence + external consumers); they are just not re-consumed. The `name` gatekeeper keeps the graph a DAG. (Caveat: assumes the runtime is the only producer of derived events — true today.)

**Identity.** The definition `name` (snake_case) is the emitted event `name` and the sink-routing key (and the key its per-entity state is scoped under). The emitted `inference_type` is the **engine type** that produced it (e.g. `weighted_window`), not the event name. The whole runtime shares **one** Kafka consumer group (`QUIX_CONSUMER_GROUP`, default `inference-quix-runtime-v1`) — *not* a group per event (that was the threaded model).

**Engine / strategy.** Each definition's `engine` string selects an `Engine` from the registry (`inference.engines`), constructed with its `engine_config` — which the **engine parses itself**; the runtime never knows the config schema. Six engines are registered today: `weighted_window` (`weights`, `threshold`, `window_seconds`, `cooldown_seconds`), `decaying_window` (adds `half_life_seconds`), `naive_bayes_window` (`prior`, `threshold`, `window_seconds`, `cooldown_seconds`, `signals: {name: {lr}}`), `session_window` (`start_event`, `end_event`, `max_duration_seconds`), `session_gated_window` (`gate_event`, `gate_weight`, `max_open_seconds`, `window_seconds`, `threshold`, `weights`, `cooldown_seconds`), and `geofence` (`lat`, `lon`, `radius_m`, `direction: enter|leave`, `owner`, optional `max_accuracy_m` — synthesized from the Neon `regions` table, not hand-written in YAML). Lineage (`derived_from`) is produced by `core.to_event` from the decision's contributors; there is no general enricher chain (a known next step if geo/other shaping is wanted).

**Vector's role shrank.** Vector is the **ingest gateway** (producers POST → `raw_sensors`) and the **Neon persister** (`kafka` source over `raw_sensors` + `high_level_events` → Postgres). It is **no longer in the emit path** — the runtime produces the event record straight to Kafka via `to_topic()`. The full transform topology (and the two-level `/<domain>/<app>` ingest URL grammar) is documented in [`doc/vector-pipeline.md`](doc/vector-pipeline.md).

## Adding a new event

1. `events/<name>.yml` — copy an existing definition; set `name`, `engine: weighted_window`, `engine_config` (`weights`, `threshold`, `window_seconds`, `cooldown_seconds`), `source_topic`, `sink_topic`. (See [`runtime/definition.py`](src/inference/runtime/definition.py).)
2. That's it — the runtime loads it on next start. No new directory, consumer, image structure, or ArgoCD app.

A new **strategy** is a new `Engine` class in [`src/inference/engines/`](src/inference/engines/) + `@register_engine("<name>")` + `engine: <name>` in a definition — no runtime change. (This is ADR 0001's swappable-engine seam, re-established in the Quix runtime.)

## Local development

Env/secrets live in `workers/.env` (gitignored). The entrypoint loads it via `find_dotenv(usecwd=True)`, which walks upward from the CWD — **run from inside the `workers/` tree**. In K8s the same vars come from the `ConfigMap` (Kafka bootstrap) and `Secret` (Kafka mTLS files mounted at `/etc/kafka/ssl`); `find_dotenv` returns `""` and is skipped.

## Deploy-state branch

`deploy/` holds: [`deploy/inference/kustomize/`](deploy/inference/kustomize/) (the runtime), [`deploy/vector/kustomize/`](deploy/vector/kustomize/) (Vector — ingest gateway + Neon persister), [`deploy/dashboard/kustomize/`](deploy/dashboard/kustomize/) (the read-only Aware dashboard — Stakater chart, reads Neon, no ingress yet), and [`deploy/argocd/`](deploy/argocd/) (the three `Application` manifests). All deploy into the **`inference`** namespace. The `inference-runtime` and `inference-dashboard` apps track `deploy-state`; `inference-vector` tracks `main` directly.

`main` is the source branch. Two workflows keep `deploy-state` (which Argo CD watches) in sync — never commit to `deploy-state`, it is force-pushed:

- **Code changes** (`paths-ignore: deploy/**`) trigger [`publish-images.yml`](.github/workflows/publish-images.yml): build each **component** image (auto-discovered `workers/<name>/Dockerfile` → `inference-<slug>`, plus the explicitly-declared `dashboard/Dockerfile` → `inference-dashboard`), bump that component's `values.yml` (`deploy/inference/kustomize/base/<slug>/values.yml` for workers, `deploy/dashboard/kustomize/base/values.yml` for the dashboard) to `sha-<short>`, commit, force-push `deploy-state`.
- **Deploy-only changes** (`paths: deploy/**`) trigger [`mirror-deploy-state.yml`](.github/workflows/mirror-deploy-state.yml): mirror `main`→`deploy-state` **carrying the existing `deploy-state` image tag forward**.

Pushing **both** code and `deploy/**` in one commit races on the `deploy-state` force-push — split them into separate pushes (code first).

## Runtime state in K8s

Quix `State` is local RocksDB at `/tmp/quix-state` (set in the Dockerfile). The container root filesystem is read-only, so an **`emptyDir`** is mounted there (see `deploy/inference/kustomize/base/runtime/values.yml`). State is **ephemeral by design** — recovered from the Kafka changelog on restart/reschedule, consistent with the no-in-cluster-persistence rule.

## What is intentionally not here yet

- **No liveness/readiness probes.** (Tests + CI now exist — see below — but the runtime has no health probes yet.)
- **Enricher chain** — the capability seam (`src/inference/capabilities.py`) is the enricher chain re-established (ADR 0001); geo enrichment is still unimplemented.
- **Single source partition** (`raw_sensors` = 1 partition) — correct and keyed, but no horizontal parallelism until partitions are added (by design — see ADR 0004).

## Commands

```bash
# Local run (from inside workers/ tree so workers/.env is found).
# Loads every events/*.yml; override the dir with EVENTS_DIR.
cd workers/runtime && python quix_main.py

# Build the runtime image locally
docker build -f workers/runtime/Dockerfile -t inference-runtime .

# Lint + tests (both run in CI — .github/workflows/ci.yml)
uv run ruff check .
uv run pytest                # tests/ exercise the import-clean core in-memory (no Kafka/Quix)

# Regenerate the shared contract after changing inference.event (CI checks it's current)
uv run python scripts/emit_event_schema.py            # -> contracts/inferred_event.schema.json
(cd dashboard/web && npm run gen:types)               # -> src/generated/events.ts

# Install into a venv for editing
uv sync --extra dev          # dev extras = pytest + ruff; or: pip install -e .
```
