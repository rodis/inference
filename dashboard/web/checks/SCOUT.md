# Browser scout — brief

A **scout**, not a gate: drive the real dashboard in a real browser against real data, report what
it finds, change nothing automatically. Run on demand. Findings become tickets on board #1.

## The one rule: do not re-check what `npm run check` already owns

[`render-check.tsx`](render-check.tsx) is an SSR pass driving `Shell` / `DayTimeline` /
`TimelineDashboard` / `LevelsDashboard` through a **real** `prepare()` lineage graph and a real
`dayLayout()`, over a fixture that deliberately includes the awkward shapes (a 6h span concurrent
with a 15-min trip, an innermost-wins payment, the 60s phantom capsule, a café stay ending after the
drive home began).

So lane assignment, `supersededIds`, `defaultLevelOf`/`laneCount`, and the containment invariant are
**covered, deterministically and fast**. A browser check that repeats them is a liability: two places
to update, and the slower, flakier copy is the one that will rot.

The scout's whole value is what an SSR pass structurally cannot see.

## What only a rendered page can answer

### 1. Legible vs merely computed (highest value)

`dayLayout()` yields numbers; the SSR check asserts a moment renders *inside* its host. Neither knows
what happens at real font metrics.

- Does a capsule's label fit its capsule, or truncate mid-word / collide with the time / overflow?
- The 60-second `car_trip` capsule is "floored to stay legible" — **is it**, at 375px?
- Do adjacent labels collide when several short events cluster?
- Does the page body ever scroll horizontally? (It must not; wide content scrolls in its own container.)

Check at ~375px, ~768px, ~1440px.

### 2. Client-only paths that are inert under SSR

`render-check.tsx` deliberately silences `useLayoutEffect does nothing on the server`. That means
**scroll anchoring and focus-after-move are tested nowhere today**. Also `/api/stream` — SSE is pure
client runtime.

### 3. Three historical eras, on real rows

ADR 0011 phase 2 left era-aware supersession: pre-Overland days draw `car_trip`; parallel-run days
fold `car_trip` under `trip`; post-cutover days draw `journey`. A fixture approximates this; only
real data proves what a given date actually renders as. **Screenshot one day per era.**

### 4. Rendered contrast, not computed contrast

`inkOn(hex)` is SSR-checkable, but whether the icon survives CSS + theme + the actually-painted
background is a pixel measurement. Measure contrast ratios on screenshots in **both** themes — the
`dataviz` skill has the palette and contrast rules to judge against.

### 5. Regressions of bugs already shipped

The Neon scale-to-zero one: `suspend_timeout=0` kills pooled connections when idle, so the first
request after idle used to 500 (fixed via `check=ConnectionPool.check_connection`). **Idle past the
timeout, reload, assert no 500.** Invisible to every existing test; instant in a browser.

### 6. DOM-vs-database assertions (a genuinely new class)

Query Neon for what *should* be drawn on a day, then assert the DOM matches. This crosses the
backend/frontend seam, which neither `pytest` nor `npm run check` can reach.

- `isEverydayPlace` events are **absent** from the timeline (home dwell is a sampling artifact — ADR 0007).
- One capsule per physical journey — no double-draw across the detector/fusion split.
- `placeUnknown` stays render their unknown state, not blank or `undefined`.
- `carCorroborated` adds a **decoration** to the title and never re-titles the event.

## Scout or gate — pick deliberately

Live Neon data changes daily, so **screenshot diffing against production is noise**. Trusting it is
the same error as tuning a weight map on a count delta, and it fails the same way: you stop believing
the instrument.

- **Scout (start here).** Live data, findings reported, no diffing. Zero infrastructure.
- **Gate (only if the scout keeps paying).** Needs a frozen fixture day served through the API — real
  engineering, not a flag. Then visual regression is trustworthy and can enter CI.

## Safety

`PUT /api/preferences` writes per-user level/lift config to Neon. **Browsing must be read-only** —
do not click through preference toggles against the real user. Use a throwaway `user_id` if
preference UI needs exercising.

Everything else in the app is read-only (`/api/users`, `/api/events`, `/api/stream`).

## Running it

Two processes; Vite proxies `/api` and `/healthz` to uvicorn on :8000.

```bash
# API (from inside workers/ tree so find_dotenv picks up creds, or export explicitly)
DATABASE_URL=... DASHBOARD_PASSWORD=... uv run uvicorn dashboard.app:app --port 8000

# UI
cd dashboard/web && npm run dev
```

Auth is HTTP Basic, fail-closed on every path except `/healthz`: `DASHBOARD_USER` (default `aware`)
and `DASHBOARD_PASSWORD`. The timeline lives at the client route `/d/timeline`; the SPA fallback
serves deep links, so navigating straight there works.

Browser driving is the `claude-in-chrome` skill — it needs site permission granted in the extension
before it can act.

## Reporting a finding

One ticket per finding on board #1 (`Area=dashboard`, `Kind=bug`), with the viewport and the date
that reproduces it, and a screenshot. A finding that turns out to be *data* rather than *rendering*
is still worth recording — it usually means a detector, not the UI.
