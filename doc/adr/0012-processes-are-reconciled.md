# ADR 0012 — Processes are reconciled, not orchestrated

Status: **Proposed — not implemented.**
Date: 2026-08-15

> This ADR introduces a **process tier**: a sibling of the connector tier (ADR 0008), one
> altitude *above* inference rather than below it. Where a connector feeds Aware, a process
> *acts* and reports its milestones back into Aware as ordinary raw signals.

## Context

Aware observes. Every tier built so far — sensors, connectors, engines, capabilities — turns
signals into facts about things that already happened. Nothing in the system *does* anything,
and that is deliberate: the outbound Pushcut lane is the single exception, and it is one
notification wide.

But some of life is a **process**, not an observation. The concrete instance driving this ADR
is the monthly DreamHost invoice, which was built and then abandoned:

1. On the 1st, gather the data needed to generate the invoice
2. Email it for validation, and wait for approval
3. Generate a PDF (createmypdf — never implemented)
4. Email the invoice so it can be submitted
5. Wait for the submission confirmation email
6. Wait for the payment to be submitted
7. Wait for the payment confirmation email

Only step 1 is computation. **Steps 2 through 7 are waits, measured in days to weeks**, and
each resumes on an event that arrives from outside — a human answering, or an email landing.

### What was actually built, and why it stalled

A Prefect flow calling **seven n8n webhooks**, backed by **12 workflows / 79 nodes** and a
Redis keyspace. Plus one workflow (`Check Payment Was Submitted`, 13 nodes) that Prefect never
calls, and three dead ones (`_Process: Global Vars`, `Error Handler - Subflow`, `My workflow`).

State lived as flat Redis keys under a per-cycle namespace, TTL'd to end-of-month:

```
dh_invoice_<n>_<year>_process_time_range_{start,end}      ← the calendar month
dh_invoice_<n>_<year>_invoice_reference_datetime_{start,end}   ← the previous month
                     _invoice_amount_main
                     _invoice_amount_extra_<description>   (0..n)
                     _invoice_total_amount
```

Three sub-workflows existed solely to emulate a hash over that: `Get metadata`
(`KEYS *_process_*` + strip the prefix in a Code node), `Get State` (`KEYS *_invoice_*`, same),
and `Update State` (fan a flat object back out into prefixed `SET`s). Every step of the flow
read the whole namespace, changed one field, and wrote it back.

It is easy to read that as pure over-engineering. **It is not.** Redis was the one correct
instinct in the design: a process spanning weeks cannot hold state in a Python local. What went
wrong is that the *finished* half — the parts that run back-to-back in a single flow — pays the
full price of durable state while needing none of it, and the half that needed it was never
built. The work stopped precisely at the hard question:

> *"…send an email to me so that I could validate the data (**trigger the workflow continuation
> in some way**)."*

That parenthesis is the whole problem. Resuming a suspended process weeks later needs a resume
token, a correlation ID, a callback endpoint that outlives the runtime, and a story for what
happens when any of those is lost. It is the standard saga problem, and it is genuinely hard.

Two smaller artifacts of the stall are worth recording, because they are symptoms of the same
gap rather than sloppiness:

- `retries=12, retry_delay_seconds=10` on step 1 is **not a retry**. Step 1 reads a Redis key a
  human sets by hand; the retry loop is a human gate expressed as a two-minute poll.
- `retries=100` on the remaining steps is ~17 minutes of retrying a call whose only realistic
  failure is "n8n is down" — retry standing in for a wait it could not express.

### The generalisation

The invoice is the first of many. Any process with human gates and inbound-event waits has this
shape, and paying the saga cost per process is not viable — the same argument ADR 0008 makes
about paying ADR 0006's per-producer cost for every source.

## Decision

**A process is a declarative list of stages, advanced by a generic reconciler that runs on a
schedule and is a pure function of the events recorded so far.**

Two normative rules bound the tier.

> **1. The reconciler acts; Aware observes.** Process milestones are POSTed to the existing
> ingest gateway (`/sensors/<app>`) as ordinary raw signals. Aware never drives a process, never
> calls out, and needs no runtime change to support one.

> **2. Processes are definitions-as-data.** `processes/*.yml` + one generic reconciler +
> swappable **stage kinds** — structurally the same move as `events/*.yml` + one Quix runtime +
> swappable engines. A second process is a YAML file, not a component.

### Reconciliation, not suspension

Nothing is suspended and nothing is resumed. The reconciler runs (daily is ample for a process
measured in weeks), reads the events already recorded for the current cycle, derives which stage
is on the frontier, performs the one action that stage is missing, and exits.

```python
events = load_events(cycle_key)              # from Neon
for stage in definition.stages:
    if stage.name in events:                 # already done
        continue
    if not all(d in events for d in stage.after):
        break                                # frontier not reached
    if stage.kind == "act":
        emit(stage.name, ACTIONS[stage.action](ctx))
    else:
        found = classify_signal(stage.signal, since=events[stage.after[-1]].ts)
        if not found:
            break                            # still waiting — exit, try tomorrow
        emit(stage.name, found)
```

This is the Argo CD pattern the deploy chain already uses: declare the desired end state, observe
what is true, close the gap. **The "trigger the continuation" problem dissolves** — nobody
triggers anything. A fact is recorded, and the next run notices.

The properties that follow are not incidental; they are the entire reason to prefer this over a
suspended flow:

| Property | Why |
|---|---|
| Crash-safe | No in-flight state exists to lose |
| Idempotent | A stage with its event recorded is skipped |
| Re-runnable | Every run is a pure function of recorded events |
| Debuggable | The process state *is* a query, not a runtime introspection |
| Correctable | Delete a wrong event; the next run re-reconciles |

### Every stage is `act` or `await`

The only distinction the engine needs is **who authors the completion event**:

- **`act`** — the reconciler does something once and emits the event itself
- **`await`** — the reconciler watches for a fact and emits the event when it appears

Same event stream, different author. `after` is a list, so the process is a DAG rather than a
chain and parallel stages cost nothing later.

```yaml
name: dreamhost_invoice
cycle_key: "dh_invoice_{year}_{month:02d}"
schedule: "0 9 1 * *"

stages:
  - {name: data_gathered,      kind: act,   action: compute.invoice_amounts}
  - {name: approval_requested, kind: act,   after: [data_gathered], action: notify.email}
  - name: approved
    kind: await
    after: [approval_requested]
    signal: {source: gmail, classify: "Does this reply approve the figures?"}
  - {name: invoice_generated,  kind: act,   after: [approved], action: createmypdf.render}
  - {name: invoice_sent,       kind: act,   after: [invoice_generated], action: notify.email}
  - name: payment_submitted
    kind: await
    after: [invoice_sent]
    signal:
      source: gmail
      from: accounting@dreamhost.com
      classify: "Payment SUBMITTED — not merely processed?"
  - name: payment_confirmed
    kind: await
    after: [payment_submitted]
    signal: {source: gmail, classify: "Does this confirm the payment cleared?"}
```

`approval_requested` being its own `act` stage is load-bearing, not bookkeeping: without it the
reconciler re-sends the approval email every single day until answered.

### Semantic classification belongs in the reconciler, not in a capability deriver

Telling *submitted* from *processed* in email prose is real semantic work, and an LLM is the
right tool. The existing n8n workflow already does exactly this, with a Gemini call and a
hardcoded `invoice 03-2026` in the prompt.

The tempting placement is Aware's capability seam — extraction after Kafka, per ADR 0008's
transformation table. **Rejected, for three reasons:**

1. **Blocking network I/O inside the router.** `raw_sensors` is one partition and the Quix
   runtime is one process handling every definition (ADR 0004). An LLM call in `Router.route`
   stalls all derivation behind it.
2. **It breaks replay.** [Invariant 19](../invariants.md) promises derived state is rebuildable
   from retained raws by re-running `rederive.py`. A non-deterministic classifier makes a replay
   produce *different* history — which is worse than no replay, because it looks like it worked.
3. **Blast radius.** It is a change to `src/inference/runtime/`, the one area CLAUDE.md fences
   off precisely because a bug there breaks all derivation at once.

So: **Aware ingests and stores the raw mail; the reconciler classifies lazily.** Only for the
stage currently on the frontier, only within the window since its predecessor. Three consequences
follow, all good:

- The derivation core stays pure and deterministic.
- LLM cost is per *open process*, not per mail ever received.
- The question lives next to the stage that asks it, so `classify:` is part of the definition
  rather than buried in a deriver.

A misclassification stays cheap and visible: the decision is recorded as an event carrying its
evidence, the raw mail is still in Neon, and correcting it means deleting the event.

### The visualization comes free

The stated goal was that each step become a node in a diagram, later added to the Aware UI. In
this design that is not additional work: **the definition is the graph** (node list, edges from
`after`) and **the events are the state** (which nodes are lit, and when each fired).

Because the definition is data, the dashboard module is generic too — it renders *any* process,
so the second process needs no UI work at all. That is the same payoff `events/*.yml` gives, one
tier up.

## Consequences

- **Positive:** the saga problem is not solved, it is *avoided*. No resume tokens, no correlation
  IDs, no callback endpoint outliving the runtime, no suspended-run state to lose.
- **Positive:** 12 n8n workflows, 79 nodes, 7 webhooks, the Redis keyspace, the namespace-prefix
  flattening, the UUID-as-API contract and the manual `current_invoice_number_digits` gate all
  retire. What survives is one reconciler plus a Gmail label rule.
- **Positive:** process N+1 is a YAML file plus any genuinely new action. `notify`,
  `await_email` and `render_pdf` are written once.
- **Positive:** Aware needs **no runtime change, no new engine, no new topic**. Milestones enter
  through the existing gateway like any other producer.
- **Negative — a new failure domain whose mode is silence.** A reconciler that stops running
  looks exactly like a process that is legitimately waiting. Freshness must be measured, the same
  lesson ADR 0008 records about n8n.
- **Negative — latency is bounded by the poll interval.** Daily is right for an invoice; a
  process needing minutes needs a different trigger, and this design does not provide one.
- **Negative — LLM classification is non-deterministic and will occasionally be wrong.** Mitigated
  by recording evidence and making correction a delete, but not eliminated.
- **Neutral — Prefect's MCP server is read-only.** The [official
  server](https://github.com/PrefectHQ/prefect-mcp-server) (beta) inspects deployments, runs and
  logs; it does not trigger. Useful for debugging a process conversationally, not for driving one.
  Actuation stays on the REST API.

## How we will know if this was wrong

1. **A process needs a stage kind that is neither `act` nor `await`.** The binary was too coarse.
2. **Stage actions accumulate process-specific branching.** `ACTIONS` becoming a place where
   `if process == "dreamhost_invoice"` appears means the definition is not carrying enough.
3. **The reconciler starts holding state between runs.** Any persisted cursor, lock or
   high-water-mark means it is not a pure function of recorded events, and the crash-safety
   argument is void.
4. **Classification accuracy is bad enough to need review before every transition.** Then the
   human gate was the wrong thing to remove, and stages should be approve-by-default rather than
   classify.
5. **Process N+1 is not "one YAML file plus maybe one action"** — measured, not assumed. This is
   ADR 0008's trip-wire 5, and it is the one that matters most.
6. **Milestone events pollute the Aware timeline.** If process events need suppressing everywhere
   they are displayed, they may not belong on `raw_sensors` at all.

## Alternatives considered

- **Prefect suspension** (`suspend_flow_run` / `wait_for_input`). Native, first-class, and the
  obvious answer for the human gate. Rejected as the *primary* mechanism because it solves step 2
  and not steps 5–7: waiting weeks on an inbound email still needs something to receive that email
  and call the resume API, so the correlation problem returns — now with a suspended run attached
  to it. Reconciliation needs neither. Suspension remains available if a stage ever needs
  sub-hour resumption.
- **n8n Wait nodes.** n8n genuinely is designed for this shape, and a Wait-on-webhook node can
  wait indefinitely. Rejected because it puts the process state back inside n8n — invisible to
  git, invisible to the dashboard, and with the state living in exactly the place ADR 0008's
  boundary rule says decisions must not live.
- **Keep Redis as the process store.** Rejected: Neon already holds every event, and a second
  store means the dashboard cannot see process state without a second reader. The events *are*
  the state.
- **A new Aware engine (`saga_window` or similar).** Rejected on the boundary rule: it would
  invert Aware from a passive observer into an orchestrator that sends email and calls PDF
  services. Engines are windowed detectors over signals; a process is neither windowed nor a
  detection.
- **LLM classification as a capability deriver.** Rejected for the three reasons above. Recorded
  here because it was the initial recommendation in this design conversation and is the more
  obvious placement.
- **A Gmail filter or hand-applied label as the classifier**, per ADR 0008's Stage 1 pattern.
  Cheaper, deterministic, and genuinely correct for coarse routing — but it cannot separate
  *submitted* from *processed*, which is the distinction the process actually turns on.

## Open questions

1. **Execution environment.** Prefect Cloud's free tier gives 1 workspace, 2 users and 400 flow
   runs/min — ample. Whether it includes managed execution is unconfirmed; if not, the reconciler
   needs a worker on the existing K8s cluster. A `workers/reconciler/Dockerfile` would be
   auto-discovered by `publish-images.yml` and cost no new CI wiring.
2. **Is Prefect earning its place?** A daily idempotent loop is also just a CronJob on a cluster
   that already runs everything else. Prefect buys run history, alerting and the MCP inspection
   surface. Worth it if more flows follow; not obviously worth it for one.
3. **Where the code lives.** Not `src/inference/` — that is the derivation core. Likely
   `processes/*.yml` beside `events/*.yml`, with the runner under `workers/`. Or its own repo,
   if the tier is genuinely independent.
4. **Cycle keying vs. Aware's entity keying.** Aware keys state by `message.user_id`
   ([`core.Router.key_for`](../../src/inference/runtime/core.py)). A process cycle key is a
   different partitioning concept. If two cycles of the same process are ever open at once, how
   do their events stay distinguishable to a consumer?
5. **Should Aware derive a `process_cycle` event?** A `session_window` pairing the first and last
   milestone would give the whole cycle an `interval` capability for free — pleasingly symmetric
   with `car_trip`. But it is only worth doing if something consumes the span.
6. **Who owns the `cycle_key` template?** `dh_invoice_{year}_{month:02d}` encodes a monthly
   cadence in a string. A quarterly or event-triggered process needs something else.
7. **Backfill.** The current invoice history exists only as email. Whether past cycles are worth
   reconstructing as process events, or the tier simply starts empty, is unresolved.
