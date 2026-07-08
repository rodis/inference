# Aware dashboard

A small, stateless web app that visualizes the `events` table in Neon as a
Structured-style day timeline: two side-by-side vertical timelines on a shared
per-day time scale (a level-filtered **Timeline** and a selectable **Compare**
series), an editable **logical-level / lift** model, and a recursive dig-down into
each event's derivation lineage.

## Layout

| file | role |
|------|------|
| `index.html` | the whole UI (HTML/CSS/JS, no build step) — fetches its data from `/api/*` |
| `app.py` | FastAPI server: serves `index.html` + read-only Neon endpoints |
| `logical_levels.json` | the logical-level + lift config the UI seeds from (later: per-user, in the DB) |
| `requirements.txt` / `Dockerfile` | container packaging |
| `build_preview.py` | bakes a static snapshot of `index.html` (data inlined) for sharing |

## Endpoints

- `GET /` — the dashboard
- `GET /api/events` — every `events` row, shaped for the page
- `GET /api/levels` — contents of `logical_levels.json`
- `GET /healthz` — liveness

**Read-only**: the dashboard only reads Neon; the inference runtime is the sole writer.

## Authentication

The dashboard exposes one user's life data on a public URL, so the whole surface (page,
static assets, `/api/*`, SSE) sits behind **HTTP Basic auth** — one shared credential, the
simplest thing that fully closes the hole while we have a single user. It's enforced as
middleware in `app.py` (so it also covers the static mount and SPA fallback, which per-route
dependencies don't reach); the browser handles the login prompt and caches the credential,
so the SPA needs no code change.

- `DASHBOARD_PASSWORD` — **required**. If unset, every request except `/healthz` returns
  401 (fail closed). In prod it comes from the `neon-credentials-for-dashboard` Doppler
  secret; set it locally to serve the app.
- `DASHBOARD_USER` — the username, defaults to `aware`.
- `/healthz` is exempt so K8s liveness/readiness probes (which send no credentials) pass.

## Run locally

```bash
cd dashboard
pip install -r requirements.txt
export DATABASE_URL="postgres://USER:PASS@HOST/neondb?sslmode=require"   # Neon connection string
export DASHBOARD_PASSWORD="pick-something"                               # else every request 401s
uvicorn app:app --reload
# open http://localhost:8000  (log in as aware / pick-something)
```

## Container

```bash
docker build -t aware-dashboard dashboard/
docker run -p 8000:8000 \
  -e DATABASE_URL="postgres://…?sslmode=require" \
  -e DASHBOARD_PASSWORD="pick-something" \
  aware-dashboard
```

## Deploying as a pod

Manifests live at `deploy/dashboard/kustomize/base/` (Stakater `application` chart,
same as the runtime) with a standalone ArgoCD app `deploy/argocd/application-dashboard.yml`
(tracks `main`, like Vector). It deploys into the `inference` namespace, **no ingress yet**
(reach it via port-forward or add DNS later).

- **Secret**: `neon-credentials-for-dashboard.yml` is a `DopplerSecret` (same source as
  Vector's, separate managed secret). It mints `neon-credentials-for-dashboard` with keys
  `NEON_DATABASE_URL` (mapped to the app's `DATABASE_URL`) and `DASHBOARD_PASSWORD` (the
  Basic-auth credential). **Both keys must exist in the Doppler `kafka-aiven-credentials/prd`
  config** — add `DASHBOARD_PASSWORD` there before this rolls out, or the Doppler sync fails.
- **Image (CI-built, sha-pinned)**: `publish-images.yml` builds `inference-dashboard` from
  `dashboard/Dockerfile` and bumps `values.yml` to `sha-<short>` on `deploy-state` (same flow
  as the runtime). The app tracks `deploy-state`. To build manually instead:
  ```bash
  docker build -t ghcr.io/rodis/inference-dashboard:latest dashboard/
  docker push ghcr.io/rodis/inference-dashboard:latest
  ```
- **Apply**: register the app once: `kubectl apply -f deploy/argocd/application-dashboard.yml`.
- **Reach it** (no ingress): `kubectl -n inference port-forward svc/aware-dashboard 8000:80`.

**First-deploy ordering** (the `values.yml` must exist on `main` before the CI bump can find it):
1. push the deploy manifests (`deploy/**`) → `mirror-deploy-state` puts them on `deploy-state`;
2. push the code (`dashboard/**`) → `publish-images` builds the image and bumps the tag.

Pushing `deploy/**` and code in one go races on the `deploy-state` force-push — keep them
in separate pushes (see top-level `CLAUDE.md`).

## Logical levels & lift

`logical_levels.json` is the exploration seed:

```jsonc
{
  "levels": { "car_trip": 1, "got_into_the_car": 2, "car_door_opened": 3, ... },  // home level (1=top)
  "lift":   { "got_into_the_car": 1, "got_out_the_car": 1 }                        // also surface up to L1
}
```

Edits in the UI are session-only; this file is the durable source until levels move
into the DB as user-owned data.
