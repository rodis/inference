# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Required reading

The [`doc/`](doc/) folder is the source of truth for architecture and design rules. Read these before non-trivial changes — they exist precisely so they don't have to be re-derived from the code:

- [`doc/architecture.md`](doc/architecture.md) — pipeline diagram, payload envelope, deployment model, configuration layers.
- [`doc/invariants.md`](doc/invariants.md) — design rules that must hold across the codebase. Verify these still hold when adding engines, observers, or transport adapters.
- [`doc/classes.md`](doc/classes.md) — per-class reference (protocols, algorithm, result shape).

When you modify behavior that one of these documents describes, update the document in the same change.

## Big-picture model

A worker is one process = one inference engine = one pod. The transport layer (`KafkaStreamHandler`) is engine-agnostic; the engine (`WeightedWindowEngine` today) is transport-agnostic and owns its own backend (Redis). The worker's [`main.py`](workers/home_arrival/main.py) is pure wiring — no logic.

Two cross-cutting rules drive most of the structure and are *not* obvious from reading any single file:

1. **Directory name is the source of truth for worker identity.** Each worker derives both forms at the top of `main.py`:
   - `WORKER_NAME = Path(__file__).parent.name` — snake_case, used everywhere at the data layer (Redis keys, emitted `inference_type`, `message.event_name`, Vector URL path, `RULES["name"]`, `APPLICATION`, logger names).
   - `WORKER_SLUG = WORKER_NAME.replace("_", "-")` — kebab-case, used at the infra layer (Kafka consumer group, K8s/Docker naming).

   The Dockerfile must preserve the directory structure when copying `main.py` into the image (`COPY workers/<name>/main.py ./workers/<name>/main.py`). Flattening it to `/app/main.py` makes `WORKER_NAME` resolve to `"app"` and silently mislabels every downstream identity. See the worker-identity rule in [`doc/invariants.md`](doc/invariants.md).

2. **Engine-owned infrastructure.** If an engine needs Redis, Postgres, or any other backend, it reads its own connection config from env vars inside the engine module. The worker layer never plumbs backend config through. `config.py` only holds infrastructure the wiring layer touches directly (Kafka, Vector). Adding a new engine that uses Redis should *not* require changes to `config.py` or `main.py` beyond import + instantiation.

## Adding a new worker

1. `workers/<name>/main.py` — copy from an existing worker. Don't touch the `WORKER_NAME` / `WORKER_SLUG` derivation; just adjust `RULES`, `KAFKA_SOURCE_TOPICS`, `KAFKA_SINK_TOPIC`, `EVENT_DOMAIN`.
2. `workers/<name>/Dockerfile` — copy from an existing worker. The two `<name>` occurrences in the `COPY` and `CMD` lines must match the directory name.
3. `deploy/kustomize/base/<slug>/` — `helmChart.yml`, `kustomization.yml`, `values.yml`. Slug is the kebab-case form (`home-arrival`, not `home_arrival`).
4. Add the slug to `deploy/kustomize/base/kustomization.yml`.
5. ArgoCD application: `deploy/argocd/application-<slug>.yml`.

The `publish-images.yml` workflow auto-discovers any `workers/*/Dockerfile`, builds the image as `ghcr.io/<owner>/inference-<slug>`, then bumps `deploy/kustomize/base/<slug>/values.yml` and force-pushes to the `deploy-state` branch. No manual image-tag editing.

## Local development

Local secrets and env live in `workers/.env` (gitignored). `config.py` loads it via `find_dotenv(usecwd=True)`, which walks upward from the current directory — **you must run the worker from inside the `workers/` tree** (`cd workers/home_arrival && python main.py`) for the `.env` to be found. Running from the repo root will fail with "Required environment variable … is not set".

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
# Local run (from inside workers/ tree)
cd workers/home_arrival && python main.py

# Build a worker image locally
docker build -f workers/home_arrival/Dockerfile -t inference-home-arrival .

# Lint (configured but not wired into CI)
ruff check .

# Install into a venv for editing
uv sync                      # or: pip install -e .
```
