# Aware dashboard

A small, stateless web app that visualizes the `events` table in Neon. A React SPA
served by FastAPI, built as a **registry of dashboards** (`web/src/app/registry.tsx` —
adding one is a module plus an entry, mirroring events-as-data):

| dashboard | what it's for |
|-----------|---------------|
| **Day timeline** | one day as a Structured-style vertical timeline: duration events as capsules ∝ how long they lasted, quiet stretches collapsed, and a pinch/⌘-scroll **semantic zoom** that reveals or folds detail around the point you're looking at |
| **Compare** | any set of event types as parallel lanes on one shared per-day scale, so co-occurring signals line up |
| **Signals** | the raw feed as a table |
| **Levels** | the altitude ladder: where each event *type* lives, drag-and-drop (see below) |

Tap any event for a recursive dig-down into its derivation lineage.

## Layout

| file | role |
|------|------|
| `web/` | the React/Vite/TS SPA — `npm run build` emits `web/dist`, which `app.py` serves |
| `app.py` | FastAPI server: the SPA bundle, read-only Neon event endpoints, and the one write path (`/api/preferences`) |
| `logical_levels.json` | first-run level config for a user who has never saved one |
| `requirements.txt` / `Dockerfile` | container packaging |

## Endpoints

- `GET /` — the dashboard (SPA shell; unknown paths fall back to it for deep links)
- `GET /api/users` — distinct `user_id`s in the `events` table (the selector)
- `GET /api/events?user_id=…&days=N` — that user's events over the last `N` whole days
  (UTC, default 7, max 90), oldest first, shaped for the page
- `GET /api/preferences?user_id=…` / `PUT` — that user's level config (their row, else the seed)
- `GET /api/stream?user_id=…` — SSE seam for the deferred live view (heartbeat only)
- `GET /healthz` — liveness (the one path exempt from auth)

The `days` window is deliberate: the dashboards render **one day at a time** with a short day
picker, so older history can't be displayed — serving it would mean a payload that grows with
every event ever recorded (steeply, once a high-rate source like the movement tracker's location
pings is in the mix). The window is the client's working set; `DAY_WINDOW` in `web/src/api.ts`
is what the SPA asks for, and it sizes the day picker.

The dashboard only ever **reads** the `events` table — the inference runtime is its sole
writer. The one thing the dashboard writes is its own `dashboard_prefs` table.

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

## Checks

```bash
cd dashboard/web
npx tsc -b        # typecheck (src + checks)
npm run check     # render + model checks for the level ladder
```

`npm run check` is an SSR pass (`checks/render-check.tsx`) that drives the Timeline and
Levels dashboards through a real `prepare()` lineage graph and asserts the depth→lane
defaults, the override flags and the hidden-type handling. It exists because the lane a type
lands in is computed from the *shape of the lineage graph* — so a change to `derivLevel`,
`laneCount` or `defaultLevelOf` silently re-points every default. Both run in CI
(`_ci-checks.yml`, the `web` job), which gates image builds.

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

## Levels — the altitude ladder

Two orthogonal numbers ride on every event, and keeping them apart is the point:

- **D — derivation depth.** *Structural*, computed from the lineage graph, not configurable.
  A raw signal is `D1`; an inference built on it is `D2`; one built on *that* is `D3`.
- **L — level.** *How much you care.* Which altitude the event appears at when you zoom
  the timeline: `L1` is the day at a glance, the bottom lane is raw signal.

**The ladder is as tall as the deepest inference in view, and a type's depth picks its
lane** (deepest at the top — `laneCount` / `defaultLevelOf` in `web/src/view.ts`). Depth is
a real signal about altitude: a `D3` event stands on two layers of reasoning and reads as a
claim about your life, a `D1` event is a wire reading. So a new `events/*.yml` definition
lands somewhere sensible with **zero configuration**, and its lane keeps following the
definitions as they change instead of freezing at whatever it was when you last saved.

Depth is not importance, though, so the default is only a default. The **Levels** dashboard
is a drag-and-drop board of lanes where you record the exceptions:

- drag a type **up** to promote it — `credit_card_payment` is the canonical case: `D1`, zero
  inference, and one of the most interesting things in a day;
- drag it **down** to demote a noisy inference out of the headlines;
- drag it **out of the stack** to keep it off the timeline at every altitude —
  `location_ping` is why this exists, since a day holds hundreds once the movement tracker
  is feeding and they would bury everything else.

Keyboard equivalents on a focused type: <kbd>↑</kbd> promote, <kbd>↓</kbd> demote,
<kbd>⌫</kbd> drop, <kbd>↵</kbd> back to the depth default.

Stored per user in `dashboard_prefs`, debounced, as **overrides only**:

```jsonc
{
  "level":  { "credit_card_payment": 1 },   // sparse — types sitting at their depth default are absent
  "hidden": [ "location_ping" ]             // off the timeline entirely
}
```

`logical_levels.json` holds the same shape and is the **first-run** config for a user with
no row yet — a default, *not* an overlay. Merging it would make "reset this override"
impossible, because the seed would immediately put the override back.

> **Superseded:** this replaced an `Assign & lift` sidebar backed by a `levels` + `lift`
> column pair, which stored a *home lane* and a *ceiling* per type. Only the ceiling ever
> affected what rendered (`getCeil` drove every visibility decision; `getL` painted a chip),
> so the home lane was decoration that doubled the write and the UI. The two old columns are
> still on the table, unread, as a rollback path — drop them once this has settled.

## Theme

Light and dark, both designed rather than inverted. The palette is a token set declared
three times in `web/src/styles.css` — the light default, the `prefers-color-scheme: dark`
preference, then an explicit `data-theme` choice which must beat the media query in *both*
directions. Components only ever reference tokens; that's what makes the second theme a
redefinition rather than a second stylesheet. The toggle in the app bar cycles
**system → light → dark** and persists to `localStorage` (`web/src/app/theme.ts`), applied
before the first paint so an override doesn't flash.

Event *category* colours (a trip's blue, a payment's teal) are deliberately outside the
token set: they identify an event type, always sit under white iconography on a filled
shape, and read on either ground.
