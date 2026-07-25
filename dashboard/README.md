# Aware dashboard

A small, stateless web app that visualizes the `events` table in Neon. A React SPA
served by FastAPI, built as a **registry of dashboards** (`web/src/app/registry.tsx` —
adding one is a module plus an entry, mirroring events-as-data):

| dashboard | what it's for |
|-----------|---------------|
| **Day timeline** | one day as **two parallel timelines** on a shared time scale (see below), with a pinch/⌘-scroll **semantic zoom** that reveals or folds detail around the point you're looking at |
| **Levels** | the altitude ladder: where each event *type* lives, drag-and-drop (see below) |

Tap any event for a recursive dig-down into its derivation lineage.

> **Removed:** a **Compare** board (any event types as parallel lanes on one shared scale) and a
> **Signals** board (the raw feed as a table). Both existed because the day timeline was one
> undifferentiated column: lining signals up meant doing it by eye in a second view, and reading
> the raw feed meant leaving the day. The two-lane day answers both directly — a moment renders
> inside the activity that contains it, and the event modal walks the lineage down to the raw
> contributors — so they had become second ways to see what the day already shows. The registry
> seam is what made deleting them as cheap as adding them was.

## The day timeline: two lanes

A day has two kinds of thing in it, and one column made them look alike. Each lane is labelled
with its own header:

- **Activities** (left) — events with a duration, as capsules whose length is how long they
  lasted. Concurrent activities sit in **sub-columns**, because on a true time scale a 6-hour
  charge genuinely does span a 15-minute trip and the real feed has exactly that. A brief
  overlap does *not* earn one: a stay ends when the fix that broke its cluster arrives, which is
  after the drive away has already started, so a café visit and the trip home overlap by about a
  minute. That's a **handoff**, not concurrency — within `HANDOFF` the column is reused and the
  later capsule is butted below the earlier one, which trades a few px of truth about its start
  for a lane that stays single-file instead of widening to say "two things at once".
  Consecutive capsules in a column are joined by a dotted **connector** across the dead time
  between them (`links`), so the lane reads as a track rather than as capsules floating on a
  background. Per column only: a line between two *concurrent* capsules would claim a sequence
  that isn't there, and two capsules that already touch get nothing.
- **Moments** (right) — points in time, as smaller hollow discs on their own dotted rail.
  Half the visual weight is deliberate: the left lane is the shape of the day, the right lane
  is texture within it.

The lane is decided by **kind alone** (`isSpan`) — does this type read as a duration, and does
it carry an interval. Not by whether the interval looks *plausible*: a 32-second `car_trip` is
a phantom trip, but it is still a trip, and filing it right because it was short put `car_trip`
in both lanes on the same day, which reads as a broken categorisation rather than as the bad
inference it actually is. Short spans stay left and get a floor on their capsule height
(`CAP_MIN`) so they stay legible. If a noisy type crowds the lane, demote it on the levels
board — that's the knob for "I care less about this", and it's per-user.

**One shared scale is what makes it work.** `dayLayout` builds a single piecewise-linear
time→y map for the day and places both lanes on it, so a span's height is just
`Y(end) − Y(start)` — a capsule is proportional to its duration *because* it sits on the same
scale as the discs beside it. A card payment made during a trip therefore lands inside that
trip's vertical range with no alignment maths, and the trip casts a tinted **band** across the
moments lane to say so (figure/ground, not a tether line per dot, so five payments inside one
visit stay legible).

Containment is **time containment, not lineage** (`hostOf`): a payment is not
`derived_from` the trip it happened during, it merely happened during it. That distinction is
the point of the second lane. The innermost (shortest) container wins, so a long charge doesn't
claim a payment that fell inside a short trip.

The scale is deliberately "broken": steps are proportional to elapsed minutes but floored so
labels have room, capped so a lull doesn't run off-screen, and a genuinely quiet stretch
collapses to a labelled divider. Two consequences: a span crowded with moments grows taller
than its duration alone implies, and a busy hour therefore gets more room than a dead one.

**"Quiet" means nothing was happening, not that nothing was reported** (`busy` in `dayLayout`).
A stretch only collapses if no visible span is in progress across it. Without that test the
collapse fires on exactly the events it shouldn't: standing still produces no location fixes at
all (ADR 0007 — the reason `stay` clusters rather than fences), so a 96-minute café visit has no
interior instants and used to collapse *its own duration* into a divider reading "1h 36m quiet",
drawn opaque on top of its own capsule. It also crushed the capsule to near the CAP_MIN floor,
which hid a real overlap — the drive home starts about a minute *before* the stay's cluster
breaks, and at 90px of stay there was nowhere for that to show.

**A note on the feed's noise.** `phone_is_charging` fires ~20×/day with durations like 5s, 9s,
18s, 32s — a flaky car USB toggling power, not twenty charging sessions — and `car_trip` has
phantom entries of the same order. They all draw as (floored) capsules in the activity lane,
which is honest but busy. The fix is upstream in the event definitions, or a demotion on the
levels board; it is deliberately *not* a presentation filter (see above).

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

**One thing kept OUT of these prefs: everyday places.** `hidden` is keyed on event *type*, and
every stay shares the type `stay` — so it cannot express "hide the ones at home". That filter is
`isEverydayPlace`, reading a flag the backend stamps into `message.place` from the `everyday`
column on a `regions` POI row (ADR 0007). Deliberately not a `hidden_places` list here and not a
hardcoded `"Home"` in the dashboard: whether a place is the one you *live in* is a property of
the place, not of one board's layout, so it lives with the place and every consumer sees it. A
stay at an everyday place is still derived and still in Neon — the timeline just declines to
draw it, because home dwell has no natural boundaries (see the scale note above) and surfaces as
one arbitrary fragment per sampling gap. The flag rides on the event rather than being applied
at derive time, so a "show everyday places" toggle is a boolean here, not a re-derive.

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

Event *category* colours (a trip's blue, a payment's teal, a stay's coffee brown) are deliberately
outside the token set: they identify an event type, sit under iconography on a filled shape, and
read on either ground. The ink on that shape is **not** a fixed white — `inkOn` picks white or
dark from the fill's relative luminance. Every icon-on-fill site (the day's capsules, the modal's
header and lineage tiles, the levels board's tokens) asks for it, so a light category colour can't
quietly erase its own icon. Nothing in the palette needs the dark branch today; `stay` did while
it was a light yellow, which is how the function got written.
