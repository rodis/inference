# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Required reading

The [`doc/`](doc/) folder is the source of truth for architecture and design rules. Read these before non-trivial changes — they exist precisely so they don't have to be re-derived from the code:

- [`doc/architecture.md`](doc/architecture.md) — pipeline diagram, payload envelope, deployment model, configuration layers.
- [`doc/invariants.md`](doc/invariants.md) — design rules that must hold across the codebase. Verify these still hold when adding engines, observers, or transport adapters.
- [`doc/classes.md`](doc/classes.md) — per-class reference (protocols, algorithm, result shape).
- [`doc/adr/`](doc/adr/) — architecture decision records. [`0001-message-shaping-pipeline.md`](doc/adr/0001-message-shaping-pipeline.md) is a **design-only** target (decide → enrich → emit, capability model, future persistence); not yet implemented.

When you modify behavior that one of these documents describes, update the document in the same change.

## Big-picture model

> **ADR 0003 is being rolled out.** An inference event is now defined as data — a YAML file in [`events/`](events/) — and a single generic **runtime** ([`workers/runtime/main.py`](workers/runtime/main.py)) loads every definition and runs one handler per definition in one process/pod. This replaces the old one-pod-per-event model where identity was the directory name. See [`doc/adr/0003-dynamic-event-runtime.md`](doc/adr/0003-dynamic-event-runtime.md). Phase 1 (this) bakes `events/` into the runtime image; Phase 2 moves them to a mounted ConfigMap so a YAML edit rolls the pod with no rebuild.

A handler is one engine + one enrichment pipeline + one Kafka consumer, assembled from an `EventDefinition` by [`runtime/builder.py`](src/inference/runtime/builder.py). The transport layer (`KafkaStreamHandler`) is engine-agnostic; engines are transport-agnostic and own their own backend (Redis). The runtime `main.py` is pure wiring — no logic.

Two cross-cutting rules drive most of the structure and are *not* obvious from reading any single file:

1. **The definition `name` is the source of truth for event identity.** From `EventDefinition.name`:
   - snake_case `name` — data layer: Redis keys, emitted `inference_type`/`event_name`, Vector URL path, logger names.
   - `slug = name.replace("_", "-")` — infra layer: Kafka consumer group (`inference-<slug>-v1`).

   (Engines/enrichers are resolved from string keys in the definition via the registries in [`runtime/registry.py`](src/inference/runtime/registry.py); concrete engines/enrichers self-register, so framework code names none of them.) See the identity rule in [`doc/invariants.md`](doc/invariants.md).

2. **Engine-owned infrastructure.** If an engine needs Redis, Postgres, or any other backend, it reads its own connection config from env vars inside the engine module. The runtime/wiring layer never plumbs backend config through. `config.py` only holds infrastructure the wiring layer touches directly (Kafka, Vector). Adding a new engine that uses Redis should *not* require changes to `config.py` or the runtime beyond registering its builder.

## Adding a new event

1. `events/<name>.yml` — copy an existing definition; set `name`, `engine`, `engine_config`, `source_topics`, `sink_topic`, `event_domain`, `enrichers`. (See [`runtime/definition.py`](src/inference/runtime/definition.py) for the schema.)
2. If the event needs a new engine or enricher, add it under `src/inference/` and decorate it with `@register_engine(...)` / `@register_enricher(...)`, and make sure the runtime entrypoint imports its module.

That's it — no new directory, Dockerfile, kustomize triplet, or ArgoCD app. In Phase 1 the runtime image is rebuilt (CI auto-builds the single `inference-runtime` image from `workers/runtime/Dockerfile` and bumps `deploy/kustomize/base/runtime/values.yml`); Phase 2 will make a definition change a ConfigMap roll with no rebuild.

## Local development

Local secrets and env live in `workers/.env` (gitignored). `config.py` loads it via `find_dotenv(usecwd=True)`, which walks upward from the current directory — **you must run the worker from inside the `workers/` tree** (`cd workers/car_door_opened && python main.py`) for the `.env` to be found. Running from the repo root will fail with "Required environment variable … is not set".

In K8s the same env vars come from a `ConfigMap` (Kafka, Vector) and `Secret` (Redis, Kafka mTLS files); `find_dotenv` returns `""` and is skipped.

## Deploy-state branch

`main` is the source branch. CI builds images on push to `main`, bumps `deploy/kustomize/base/*/values.yml`, and **force-pushes** the result to the `deploy-state` branch. Argo CD watches `deploy-state`. Implications:

- Never commit directly to `deploy-state`; it is overwritten.
- A change to `deploy/` on `main` will reach Argo CD only after the next CI run for that worker. Paths under `deploy/**` are excluded from the workflow trigger to avoid rebuild loops, so a deploy-only PR will not retrigger the workflow — bump something in `src/` or `workers/` (or run the workflow manually) to refresh `deploy-state`.

## What is intentionally not here yet

- **No tests.** `[project.optional-dependencies] dev = []`, no `tests/` directory, no CI test job. If you write tests, the engine accepts an optional `redis_config` dict precisely so it can be exercised against `fakeredis` without env vars.
- **No lint/typecheck in CI.** Ruff is configured in `pyproject.toml` but never invoked by a workflow.
- **No liveness/readiness probes** in the Helm values. The consumer loop will keep polling silently even if Redis is unreachable (errors are caught, logged, and the offset is committed).

## Commands

```bash
# Local run (from inside workers/ tree so workers/.env is found).
# Loads every events/*.yml; override the dir with EVENTS_DIR.
cd workers/runtime && python main.py

# Build the runtime image locally
docker build -f workers/runtime/Dockerfile -t inference-runtime .

# Lint (configured but not wired into CI)
ruff check .

# Install into a venv for editing
uv sync                      # or: pip install -e .
```
