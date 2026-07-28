# ADR 0008 — A connector tier via n8n, starting with email

Status: **Accepted — Stage 1 in progress.** The ingest contract is proven end-to-end
(2026-07-28, synthetic event through the live lane); the first real workflow is next.
Date: 2026-07-28

> Operational reference: [`doc/connectors.md`](../connectors.md). This ADR is the *why*.

## Context

Every producer today is a **sensor**: iOS Shortcuts (`shortcut`), phone location
(`owntracks`, `overland`), the car (`bmw`). Each emits a boolean edge or a coordinate, and
each cost a bespoke component to obtain — ADR 0006 is a long record of what one producer
costs when you own it end-to-end (OAuth device flow, hourly token refresh, rotation
persistence in Neon, a `maxSurge: 0` rollout patch, a SIGTERM handler, a descriptor mapper).

Email is the natural next source and a different *kind* of source — the first **document**
feed, where the payload is meaningful text rather than an edge. Receipts, bookings, bills and
deliveries all arrive as mail, and a hand-applied (or filter-applied) Gmail **label** is a
cheap, reliable human classifier that keeps the hard question (*what does this mail mean?*)
out of the producer entirely.

But email is also the first of a *long tail*: Calendar, Todoist, banking, Strava, Stripe.
Paying ADR 0006's per-producer cost for each is not viable. So the question this ADR answers
is not "how do we read Gmail" but **"what is the cheapest correct way to onboard source
N+1?"**

Two constraints shaped the answer, both surfaced as objections to an earlier draft of this
work:

1. **Per-source transformation cannot keep accumulating in Vector.** `owntracks_to_canonical`
   and `overland_to_canonical` are hand-written VRL. VRL is not unit-testable from `pytest`,
   is deploy-coupled (~5 min cycle), and grows the routing tree. Ten sources of it is not a
   pipeline, it is a liability.
2. **A self-managed n8n instance already exists**, already holds working Gmail credentials,
   has connectors for hundreds of apps, and can be driven by an MCP server to *generate*
   workflows.

## Decision

**Third-party sources are onboarded as n8n workflows that POST to the existing ingest
gateway.** No new worker, no new Vector transform, no new Kafka topic, no new ArgoCD app.

The tier is bounded by one normative rule, which is the entire basis for accepting that
connector logic lives outside git:

> **A connector may authenticate, fetch, and rename/reshape fields onto the canonical ingest
> contract. It may not threshold, correlate, window, join across events, or decide that
> something happened.**

### Why this is not the Home-Assistant pattern ADR 0006 rejected

ADR 0006 rejected forwarding BMW data via an HA `rest_command` because it "puts HA in the
critical path doing (or transporting) the machinery," concluding *logic/infra stays ours.*
That objection is about **logic**, and the rule above satisfies it. Three further reasons the
cases differ:

- **The precedent already exists and is load-bearing.** The iOS Shortcuts are exactly this
  tier — GUI-configured, not in git, pure transport — and they carry
  `device_connected_to_power`, `car_lock_state_change` and `credit_card_payment` today. n8n
  is that tier named and given a contract, not a new compromise.
- **Truth is unaffected.** A connector's output lands on `raw_sensors` → Neon as a raw event,
  so the raw signal remains the system of record and everything derived stays rebuildable
  ([invariant 19](../invariants.md)).
- **HA was load-bearing for something else.** n8n is not in the path of any existing feed,
  so a failure is confined to the sources it fronts.

### n8n solves the VRL-scaling problem rather than adding to it

The reason `owntracks`/`overland` need adapters is that those apps POST **directly** at us
with a body shape outside our control — `bmw_cardata/ingest.py` says exactly this. **n8n
inverts that**: with n8n in the middle the body is ours again, so a Set node emits canonical
shape and the shared `standard` lane suffices. Hence:

> **No new Vector transform for an n8n-fronted source, ever.** `route_by_app` stops growing.

### Transformation splits at the Kafka boundary

| Duty | Where |
|---|---|
| Envelope mapping, field trimming, batch fan-out | n8n (Set / Split Out) |
| **Semantic extraction** (merchant, amount, status) | **`src/inference/`, after Kafka** |
| Inference (thresholds, correlation, sessions) | `events/*.yml` |

**A pre-Vector "shaper worker" was considered and rejected.** The argument is
[invariant 19](../invariants.md): a parser running **before** Kafka destroys whatever it
misparses, irrecoverably, whereas one running **after** leaves the raw body retained in Neon,
so a bug is fixed by correcting the parser and re-running `rederive.py`. Extraction therefore
belongs in the existing capability seam, and the tier needs no new component at all.

The checkable form of the boundary: **if a mapping needs more than a Set node it is
semantics, not envelope mapping.** A Code node is the smell.

### Email specifically — Stage 1 emits raw signals only

One workflow per label, three nodes, emitting `email_labeled_<slug>` with metadata plus
Gmail's snippet — never the body (the ~1 MiB ingress and Kafka ceilings, and a permanent
privacy cost). The name asserts only what is known: *a label was applied*, not that the mail
*is* a receipt.

**No engine and no definition in Stage 1.** Raw events are visible in Neon as
`event_class: 'raw'` without any derivation. This is ADR 0006's most expensive lesson —
its `car_locked` mapping watched a descriptor id that did not exist, and its first weight
change was validated on a count delta and shipped a regression. Map data first; decide what
it means once it is on the wire.

## What n8n actually buys — and what it does not

Recorded plainly, because an earlier draft of this work overstated it.

**It does not buy latency for email.** n8n's Gmail Trigger is a **polling** trigger with a
**1-minute minimum** — it is not push. A poller we wrote would also have run at ~60s. What it
buys for Gmail is that we write and maintain **no poller, no cursor/high-water-mark, no OAuth
refresh loop, and no Neon state table** — precisely the `bmw_cardata/token_store.py` shape of
work, including its hand-created table with no migration.

**It does buy push where the source has webhooks.** Stripe, GitHub, Todoist, Calendly and
similar fire genuine webhooks, and n8n's Webhook node makes those effectively instant. We
could not match that per-source cheaply. Gmail simply is not such a source.

**Known wrinkle:** n8n's community reports the Gmail Trigger *missing* messages at 1-minute
polling. Combined with ingest being at-most-once and acknowledging on receipt, completeness
is measured rather than assumed. A second, sharper one: the trigger's `readStatus` defaults to
**`unread`**, silently dropping any message read before the next poll — that default is a
completeness hole, not a preference, and is plausibly part of the reputation above.

**It does NOT insulate us from Google's OAuth lifecycle, which is where this claim was too
generous.** "n8n already holds working Gmail credentials" was taken as given; on first use the
credential failed with `invalid_grant`, and its record showed no successful token refresh since
2026-03-23 — roughly four months dead, with the workflow that depends on it showing zero
executions. The likely cause is the OAuth **consent screen sitting in "Testing"**, where Google
expires refresh tokens after **7 days**. That is a property of the Google Cloud OAuth *client*,
not of the client library, so n8n cannot help: it removes the refresh *loop* we would have
written, not the consent-screen configuration we would have had to get right anyway. What n8n
genuinely saves on a source like Gmail is the poller, the cursor and the state table — a smaller
list than first claimed.

The lesson generalises past OAuth: **a credential's existence is not its validity.** Test a
connector's auth before treating it as a reason to prefer one design over another.

**True Gmail push is a deliberate Stage 3.** `users.watch` + Cloud Pub/Sub → an n8n Webhook
node gives seconds. It costs the GCP project and OAuth setup n8n was chosen to avoid, plus a
renewal job — a watch **expires silently after 7 days**, which is the classic way such a
pipeline goes quiet unnoticed. Only worth it if measurement shows 60s is inadequate.

## Consequences

- **Positive:** source N+1 costs a workflow, not a worker. No image, no `values.yml`, no
  Doppler secret, no rollout, no ArgoCD app. Third-party credentials stay in one place.
- **Positive:** the VRL adapter count is frozen at two.
- **Negative — connector logic is not deployed from git, but much less so than assumed.**
  n8n's official instance-level MCP server authors workflows as **`@n8n/workflow-sdk`
  TypeScript** (`create_workflow_from_code`, `validate_workflow`, `update_workflow`), so the
  artifact is *source code*, not an exported JSON blob. Committing that source to
  [`connectors/n8n/`](../../connectors/) gives real review and meaningful diffs, and makes a
  lost instance a re-run rather than a re-derivation. What remains outside git is only the
  *deploy step* — the code still has to be pushed into the instance, and the instance is still
  the thing actually executing. That is a materially smaller gap than "logic lives in a GUI",
  and it is the strongest argument for the official MCP server over the community one.
- **Negative — no scripted historical backfill.** Fetching two years of a label is a manual
  one-off n8n run with a date range, not a `rederive.py`-style replay.
- **Negative — n8n becomes a new failure domain**, and its failure mode is *silence*, which
  is indistinguishable from "nothing happened at the source." Freshness is therefore a
  first-class measurement.
- **Neutral:** ingest remains unauthenticated. Already true; this tier raises the stakes.

## How we will know if this was wrong

The thesis is falsifiable, and these are the trip-wires:

1. **A new Vector VRL adapter becomes necessary** for an n8n-fronted source — the tier failed
   at its one job.
2. **p95 trigger lag materially exceeds the 60s poll floor** — n8n is adding latency rather
   than just inheriting it.
3. **Loss rate is non-zero** and not fixable by workflow configuration.
4. **Extraction logic appears in n8n Code nodes** — the boundary rule is eroding, and
   inference is going invisible to `backtest.py`.
5. **Cost of source N+1 is not "one workflow, zero repo changes, minutes"** — measured, not
   assumed.

[`scripts/connector_eval.py`](../../scripts/connector_eval.py) exists to answer 2, 3 and 5
with data rather than impression — the same reason
[`trip_eval.py`](../../scripts/trip_eval.py) exists.

## Alternatives considered

- **Our own `workers/gmail/` poller**, mirroring `bmw-cardata`. Fully git-tracked,
  pytest-able, and the only option supporting scripted backfill. Rejected as the *default*
  because it pays ADR 0006's per-producer cost for every source in the long tail: a Google
  Cloud OAuth client, a refresh-token store, a Gmail cursor persisted to Neon, an image, a
  Doppler secret and a deploy — to achieve the same ~60s latency n8n gives for free. Remains
  the right answer for any source n8n cannot reach.
- **A pre-Vector "shaper" worker** receiving arbitrary third-party bodies and emitting
  canonical events. Rejected: it destroys what it misparses (invariant 19), and it recreates
  in Python the job Vector's `standard` lane already does.
- **More Vector VRL adapters, one per source.** Rejected: constraint 1 above.
- **A new `email` ingest domain** (`/email/gmail` → `raw_email`). Rejected: a new domain
  means a new topic, and the runtime requires exactly one external source topic
  (invariant 6). Connectors stay in the `sensors` domain.
- **Gmail `users.watch` + Pub/Sub from the start.** Deferred to Stage 3 — it reintroduces the
  GCP setup n8n avoids, plus 7-day silent-expiry renewal, for a latency gain we have not yet
  shown we need.

## Open questions

1. **What is the real loss rate?** The reported Gmail-Trigger misses make this the first
   thing to measure, before a second source is added.
2. **Ingest authentication.** A shared-secret header checked in `parse_path` is cheap; the
   question is whether to do it now or when the tier grows. Leaning now.
3. **Does the boundary rule survive contact with a messy source?** The first source needing
   more than a Set node is the real test.
4. **Where do future-dated events live?** Bookings and travel are events about a time that
   has not happened yet, and the data model has no notion of that. A modelling question, not
   a mapping one — and the reason travel is not Stage 1.
