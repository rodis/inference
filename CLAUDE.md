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

- **`key_for(event)`** — the **entity key** a window aggregates over (prefers `message.vehicle_id`, falls back to `source_app`). Partition + state-ownership unit.
- **`decide(spec, …, state)`** — the weighted-window engine for one definition: prune the window, sum weights of distinct contributing event types, threshold, event-time cooldown. State lives in partition-local **Quix `State`** (RocksDB + changelog), keys **namespaced by definition name** (`<def>:window`, `<def>:last_fired`) so all definitions share one keyed store without colliding. **No Redis.**
- **`to_envelope(result)`** — mints the full `high_level_events` Envelope (what Vector's transforms used to do): `envelope_id`, `event_name`, `message`, `source_type="inference_quix"`, etc.
- **`build_runtime()`** — loads definitions, builds the topology: consume external source topics → `group_by(key_for)` → one stateful **`router`** (`expand=True`) → `to_topic(sink)`.

Two things that are *not* obvious from any single file:

1. **One shared keyed router, all definitions as data** (not one consumer/branch per event). The router loops the definitions that consume each incoming event. This is forced by the Aiven free-tier **5-topic cap**: per-definition branches would mint N changelog + N repartition topics; the shared router costs **1 repartition + 1 changelog regardless of definition count**. See ADR 0004.

2. **Recursion is resolved in-process, not through Kafka.** The runtime consumes only **external** source topics (`union(source_topics) − sink_topics` — i.e. `raw_sensors`, *not* `high_level_events`). When the router fires a derived event, it feeds that event back through the consumers map within the same call (a queue), using the entity's persisted window — so e.g. `car_door_opened` immediately drives `got_into_the_car`. Derived events are still produced to `high_level_events` (for persistence + external consumers); they are just not re-consumed. The `event_name` gatekeeper keeps the graph a DAG. (Caveat: assumes the runtime is the only producer of derived events — true today.)

**Identity.** The definition `name` (snake_case) is the emitted `event_name`/`inference_type` and the sink-routing key. The whole runtime shares **one** Kafka consumer group (`QUIX_CONSUMER_GROUP`, default `inference-quix-runtime-v1`) — *not* a group per event (that was the threaded model).

**Engine config.** A definition's `engine_config` carries `threshold`, `window_seconds`, `cooldown_seconds`, `weights` — read directly by the router's `decide`. The `engine` and `enrichers` fields remain in the schema but only `weighted_window` is implemented today, and the enricher chain (lineage/geo) is **not** yet run (lineage is inlined as `derived_from`; geo is unimplemented). Bringing enrichers into the Quix flow is a known next step.

**Vector's role shrank.** Vector is the **ingest gateway** (producers POST → `raw_sensors`) and the **Neon persister** (`kafka` source over `raw_sensors` + `high_level_events` → Postgres). It is **no longer in the emit path** — the runtime produces the Envelope straight to Kafka via `to_topic()`.

## Adding a new event

1. `events/<name>.yml` — copy an existing definition; set `name`, `engine: weighted_window`, `engine_config` (`weights`, `threshold`, `window_seconds`, `cooldown_seconds`), `source_topics`, `sink_topic`, `event_domain`. (See [`runtime/definition.py`](src/inference/runtime/definition.py).)
2. That's it — the runtime loads it on next start. No new directory, consumer, image structure, or ArgoCD app.

A genuinely new engine *type* (Bayesian, etc.) would mean extending the router in [`quix.py`](src/inference/runtime/quix.py) rather than only editing YAML.

## Local development

Env/secrets live in `workers/.env` (gitignored). The entrypoint loads it via `find_dotenv(usecwd=True)`, which walks upward from the CWD — **run from inside the `workers/` tree**. In K8s the same vars come from the `ConfigMap` (Kafka bootstrap) and `Secret` (Kafka mTLS files mounted at `/etc/kafka/ssl`); `find_dotenv` returns `""` and is skipped.

[`workers/quix_spike/`](workers/quix_spike/) is the **learning artifact** — the step-by-step exploration (its `README.md` tells the story, steps 2→5) that became `inference.runtime.quix`. Useful for understanding; not deployed.

## Deploy-state branch

`deploy/` holds: [`deploy/inference/kustomize/`](deploy/inference/kustomize/) (the runtime), [`deploy/vector/kustomize/`](deploy/vector/kustomize/) (Vector — ingest gateway + Neon persister), and [`deploy/argocd/`](deploy/argocd/) (the two `Application` manifests). Both deploy into the **`inference`** namespace. The `inference-runtime` app tracks `deploy-state`; `inference-vector` tracks `main` directly.

`main` is the source branch. Two workflows keep `deploy-state` (which Argo CD watches) in sync — never commit to `deploy-state`, it is force-pushed:

- **Code changes** (`paths-ignore: deploy/**`) trigger [`publish-images.yml`](.github/workflows/publish-images.yml): build the image, bump `deploy/inference/kustomize/base/*/values.yml` to `sha-<short>`, commit, force-push `deploy-state`.
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
