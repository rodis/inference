# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Required reading

The [`doc/`](doc/) folder is the source of truth for architecture and design rules. Read these before non-trivial changes:

- [`doc/adr/0004-scaling-model.md`](doc/adr/0004-scaling-model.md) — **the current architecture.** Why the runtime is a single Quix Streams `Application`, the state/partition co-location keystone, and the deploy findings.
- [`doc/adr/0002-recursive-derivation.md`](doc/adr/0002-recursive-derivation.md) — entailment vs correlation; recursive derivation (now resolved in-process by the Quix router).
- [`doc/invariants.md`](doc/invariants.md), [`doc/architecture.md`](doc/architecture.md), [`doc/classes.md`](doc/classes.md) — **partially stale.** They describe the pre-Quix threaded runtime; each carries a banner pointing here. Treat as historical until reconciled.
- [`doc/adr/0001-message-shaping-pipeline.md`](doc/adr/0001-message-shaping-pipeline.md), [`0003-dynamic-event-runtime.md`](doc/adr/0003-dynamic-event-runtime.md) — **superseded by 0004.** Historical decision records (the typed-message/enricher pipeline and the threaded one-process-many-handlers runtime). The code they describe has been removed; the ideas live in git history.

When you modify behavior one of these documents describes, update the document in the same change.

## Big-picture model

An inference event is **data** — a YAML file in [`events/`](events/). A single generic **Quix Streams runtime** loads every definition and runs them all in one `Application`, one process, one consumer group:

- entrypoint [`workers/runtime/quix_main.py`](workers/runtime/quix_main.py) → [`inference.runtime.quix`](src/inference/runtime/quix.py) (`build_runtime()` / `run()`);
- definitions loaded by [`inference.runtime.definition.load_definitions`](src/inference/runtime/definition.py) (the `EventDefinition` schema).

[`quix.py`](src/inference/runtime/quix.py) is the whole runtime (~150 lines). Read it as:

- **`key_for(event)`** — the **entity key** a window aggregates over (`message.user_id`; sentinel `_no_user_id` + warning if missing). Partition + state-ownership unit.
- **Engines ([`inference.engines`](src/inference/engines/))** — the *strategy*, resolved from the definition's `engine` string via a registry. Three built-ins today, all sharing the prune-window-then-score shape: `weighted_window` (weighted sum of distinct contributors vs a threshold, event-time cooldown), `decaying_window` (same, but each contributor's weight fades with age — `half_life_seconds`), and `naive_bayes_window` (log-odds from a prior + per-signal likelihood ratios; `score` is a calibrated posterior, and an `lr < 1` lets a signal count as evidence *against* — used by `car_door_closed` so a CarPlay connect suppresses a bogus close). Each exposes `input_event_names()` (drives routing) + `decide(event, state) -> Decision | None`. Per-entity Quix `State` (RocksDB + changelog) is scoped per definition via `ScopedState` (keys `<def>:window`/`<def>:last_fired`), so definitions share one store without colliding. **No Redis.**
- **`to_event(name, inference_type, decision, user_id)`** — runtime-side shaping in one step: mint the full `high_level_events` event record from an engine `Decision` (`name` = produced event, `inference_type` = engine type). The top-level wrapper is **identical to the one Vector mints for raw events** (`name`, `source_app`, `source_type`, `message`) so every Kafka topic carries the same shape; `source_type` records the entry mechanism (`"kafka"` for derived, `"http_server"` for raw) and is metadata only — the persister drops it, it never reaches Neon. The `message` holds the per-event `id` (no top-level "envelope" id anymore — minted here for derived, by Vector for raw), the `derived_from` lineage, the entity `user_id`, the event-time `timestamp`, and the derived-only `inference_type`. **Time:** the only event-time is `message.timestamp`; "when the system handled it" is the DB-set `ingested_at` column — the old wrapper produce-time `timestamp` and `message.processed_at` (both ~= `ingested_at`) were dropped. The old `decide → finalize → Vector-re-wrap` hop is gone — we produce straight to Kafka — so engines only decide; all shaping lives here.
- **`build_runtime()` / `_route()`** — `build_runtime` resolves each definition's engine, indexes `name → engines` from `input_event_names()`, and wires the topology: consume external source topics → `group_by(key_for)` → one stateful `_route` (`expand=True`) → `to_topic(sink)`. `_route` is the shared router that calls `engine.decide(...)`.

Two things that are *not* obvious from any single file:

1. **One shared keyed router (`_route`), all definitions as data** (not one consumer/branch per event). `_route` loops the engines that consume each incoming event. This is forced by the Aiven free-tier **5-topic cap**: per-definition branches would mint N changelog + N repartition topics; the shared router costs **1 repartition + 1 changelog regardless of definition count**. See ADR 0004.

2. **Recursion is resolved in-process, not through Kafka.** The runtime consumes the **one external** source topic (the definitions' `source_topic` minus sinks — i.e. `raw_sensors`, *not* `high_level_events`; exactly one is required today, see ADR 0004). When the router fires a derived event, it feeds that event back through the consumers map within the same call (a queue), using the entity's persisted window — so e.g. `car_door_opened` immediately drives `got_into_the_car`. Derived events are still produced to `high_level_events` (for persistence + external consumers); they are just not re-consumed. The `name` gatekeeper keeps the graph a DAG. (Caveat: assumes the runtime is the only producer of derived events — true today.)

**Identity.** The definition `name` (snake_case) is the emitted event `name` and the sink-routing key (and the key its per-entity state is scoped under). The emitted `inference_type` is the **engine type** that produced it (e.g. `weighted_window`), not the event name. The whole runtime shares **one** Kafka consumer group (`QUIX_CONSUMER_GROUP`, default `inference-quix-runtime-v1`) — *not* a group per event (that was the threaded model).

**Engine / strategy.** Each definition's `engine` string selects an `Engine` from the registry (`inference.engines`), constructed with its `engine_config` — which the **engine parses itself**; the runtime never knows the config schema. Three engines are registered today: `weighted_window` (`weights`, `threshold`, `window_seconds`, `cooldown_seconds`), `decaying_window` (adds `half_life_seconds`), and `naive_bayes_window` (`prior`, `threshold`, `window_seconds`, `cooldown_seconds`, `signals: {name: {lr}}`). Lineage (`derived_from`) is produced by `finalize` from the decision's contributors; there is no general enricher chain (a known next step if geo/other shaping is wanted).

**Vector's role shrank.** Vector is the **ingest gateway** (producers POST → `raw_sensors`) and the **Neon persister** (`kafka` source over `raw_sensors` + `high_level_events` → Postgres). It is **no longer in the emit path** — the runtime produces the event record straight to Kafka via `to_topic()`.

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

- **No tests, no lint/typecheck in CI, no liveness/readiness probes.** Ruff is configured in `pyproject.toml` but never invoked by a workflow.
- **Enricher chain not run** in the Quix runtime (lineage inlined, geo unimplemented).
- **Single source partition** (`raw_sensors` = 1 partition) — correct and keyed, but no horizontal parallelism until partitions are added (by design — see ADR 0004).

## Commands

```bash
# Local run (from inside workers/ tree so workers/.env is found).
# Loads every events/*.yml; override the dir with EVENTS_DIR.
cd workers/runtime && python quix_main.py

# Build the runtime image locally
docker build -f workers/runtime/Dockerfile -t inference-runtime .

# Lint (configured but not wired into CI)
ruff check .

# Install into a venv for editing
uv sync                      # or: pip install -e .
```
