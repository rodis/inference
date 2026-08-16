# ADR 0012 — Processes are reconciled, not orchestrated

Status: **Accepted — core implemented 2026-08-15** (`processes/*.yml` schema +
pure reconciliation core + tests). Actions and the Prefect entry point are next.
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
        continue                             # a predecessor is unfinished — try a peer branch
    if stage.kind == "act":
        result = ACTIONS[stage.action](ctx)
    else:
        result = classify_signal(stage.signal, since=max(events[d].ts for d in stage.after))
        if not result:
            continue                         # still waiting — but a peer branch may be ready
    emit(stage.name, result)                 # POST to the ingest gateway
    events[stage.name] = result              # advance the LOCAL view — see below
```

**`continue`, not `break`, on an unsatisfied stage.** Stopping the scan would quietly make
`after` a chain: given `a → (b await, c act)`, a stalled `b` would also hold back `c`, which
depends on nothing but `a`. Scanning on is what makes "parallel stages cost nothing" true
rather than aspirational.

**`max(...)`, not `after[-1]`.** In a DAG the list order carries no meaning, so an await must
look from its *latest* predecessor; looking from an earlier branch's timestamp would
re-examine evidence that predates the stage becoming ready.

**`events[stage.name] = result` is not bookkeeping.** Without it a run performs exactly one stage,
because the next stage's `after` is checked against a view that has not moved — so a seven-stage
process would take seven days, and the four `act` stages after `approved` (collect lines, total,
render, send) would trickle out one per day instead of completing the moment approval lands.
Advancing the local view lets a run walk the whole chain of ready stages and stop at the first
genuine wait.

It is also why the emit path's latency does not matter: a milestone reaches Neon through
Vector and Kafka, which the same run would not see if it re-read. The local view is the run's
truth; Neon is the *next* run's truth. Crash-safety is unaffected — a run that dies mid-chain has
already emitted everything it completed, and the next run re-reads and carries on.

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
| Voidable | Mark the cycle terminal and start a new one — no stage needs an amendment path |

### Every stage is `act` or `await`

The only distinction the engine needs is **who authors the completion event**:

- **`act`** — the reconciler does something once and emits the event itself
- **`await`** — the reconciler watches for a fact and emits the event when it appears

Same event stream, different author. `after` is a list, so the process is a DAG rather than a
chain and parallel stages cost nothing later.

```yaml
name: dreamhost_invoice
cycle_key: "dh_invoice_{year}_{seq:03d}"      # seq is a per-YEAR sequence, not the month
opens:
  # `via`, not `on` — YAML 1.1 resolves a bare `on` key to boolean true.
  - {via: schedule, cron: "0 9 1 * *"}        # the regular monthly invoice
  - {via: manual}                             # a bonus / ad-hoc invoice

stages:
  - {name: computed_lines,     kind: act,   action: lines.worked_days}   # may produce ZERO lines
  - {name: approval_requested, kind: act,   after: [computed_lines], action: notify.email}
  - name: approved
    kind: await
    after: [approval_requested]
    signal: {source: gmail, classify: "Does this reply approve the figures?"}
  - {name: manual_lines,       kind: act,   after: [approved], action: lines.manual}
  - {name: total_computed,     kind: act,   after: [manual_lines], action: compute.total}
  - {name: invoice_generated,  kind: act,   after: [total_computed], action: createmypdf.render}
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

### An invoice is a set of lines, and a cycle is opened by an event

The tempting model — *worked days × day rate, plus the occasional extra* — is wrong, and its
wrongness shows up in the invoice number. **Some invoices have no worked days at all**: a Christmas
bonus is a single line with no period behind it. So more than one invoice can exist in a month, and
the number cannot be the month. It is a **per-year sequence**.

Two things follow, and both are generic rather than invoice-specific.

**1. Every line-producing stage may produce zero lines.** An invoice is the sum of the lines its
producers contribute, and `lines.worked_days` is one producer among several rather than the spine
with decorations attached. A regular month: one computed line, zero-to-two manual ones. A Christmas
bonus: zero computed, one manual. No stage becomes conditional, no `when:` guard enters the
definition language — the stage always runs and sometimes contributes nothing, exactly as the
extras table already behaves when it is absent.

> **Absence is graceful everywhere, or the definition language grows conditionals.** This is the
> same property that made a missing extras table harmless, promoted to a rule.

It also dissolves a special case that the prior art carried: `invoice_reference_datetime_start/end`
were treated as properties of the *invoice*, which is why a bonus invoice had nowhere to live. They
are properties of the **worked-days line**. An invoice without that line simply has no period.

**2. A cycle is opened by an event, not by a schedule.** The reconciler does not create cycles; it
advances the ones that exist. A `cycle_opened` event is the genesis fact, and it carries the
invoice number and whatever shape the cycle needs.

The schedule is then just *one producer* of that event, which is why the definition has an `opens:`
list rather than a `schedule:` field. The monthly invoice is opened on a cron; a bonus invoice is
opened by hand, at a time nothing can predict. Had the schedule remained the definition of a cycle,
manual invoices would have needed a parallel mechanism — a second entry point, a fake cron, or a
per-process hack. Making genesis an event costs one reserved event name and buys ad-hoc cycles for
every future process for free.

**Invoice numbers are allocated at `cycle_opened`**, and a voided cycle's re-run **inherits** its
number rather than taking the next one. The invoice was never sent, so DreamHost never saw the
number, and reusing it keeps the sequence gap-free — which is the conservative choice for invoice
records generally, and costs nothing if it turns out not to be required.

### The invoice is not computable — variable line items are the reason `approved` sits early

An invoice is *usually* worked-days × day-rate, but some months carry an expense or a bonus: a
line or two that exists nowhere in any system and can only come from a human. The prior art
handled this with **one n8n Data Table per cycle, named exactly the namespace**, columns
`description` (string) and `amount` (number). One survives — `dh_invoice_4_2026`, holding
`{"Coursera Annual sbscription", 239.4}`.

The mechanism got one important thing right, and it is preserved here: **absence is graceful and
default.** A missing table yields `{}` and the process carries on, because most months have no
extras and a process that blocks waiting to be told "nothing this month" is worse than useless.

Everything else about it is a defect the new design must not inherit:

- **The ordering is backwards.** Extras were read seconds after the flow started, so the table had
  to be populated *before* the run — before you had seen what the invoice would say. You want the
  draft first, then to decide what to add to it. **This is why `manual_lines` comes after
  `approved`**: the approval wait is exactly the window in which a human is looking at the numbers,
  and whatever they add during it is picked up when the gate closes. The contract the approval
  email must state: *add your lines, then approve.*

  This ordering survives the bonus case, which is the one that could have broken it. A cycle with
  no computed lines sends an approval mail showing nothing and asking for lines — which reads
  oddly but is correct, because a manually-opened cycle exists precisely because a human decided
  it should. Approval still means *these are all my lines*, and it is still the last gate before
  anything is generated.
- **Descriptions became keys.** `invoice_amount_extra_<description>` was written as a Redis key,
  so two rows both described "travel" silently overwrite — and since the total sums by the
  `invoice_amount*` prefix, the money just quietly goes missing. (`dotNotation: false` on the Set
  node is already a workaround for a description containing a `.`.) **Extras belong on the event as
  a list**, never flattened into key names:

  ```json
  {"stage": "extras_collected",
   "extras": [{"description": "Coursera Annual sbscription", "amount": 239.40}]}
  ```

- **Decimal parsing is wrong above 999.** The total's Code node does
  `parseFloat(String(v).replace(',', '.'))`, and `String.replace` with a string argument replaces
  only the **first** occurrence: `1,234.50` → `1.234.50` → `parseFloat` → **1.234**. A €1,234.50
  expense becomes €1.23. Latent at current amounts, but it is a money bug, and it argues for typed
  decimal handling rather than string coercion through a key-value store.
- **Matching a table by its name is a silent-failure surface.** The live table is
  `dh_invoice_4_2026` (unpadded), the abandoned `_Process: Global Vars` says `dh_invoice_04_2026`
  (padded), and `cycle_key` above renders `{month:02d}` — padded. Any mismatch produces *no error*,
  just an invoice missing its expenses.

**Where extras live is deliberately not an architectural commitment.** `collect.extras` is a stage
action like any other, so the source is swappable: the existing n8n Data Table works today and
costs nothing to keep, while the target is a Neon table read directly by the reconciler with a
small editor in the process's own dashboard module — making that module both the visualization
*and* the input surface, with the approval email linking straight to it.

> **This was the first real test of the `act`/`await` binary** — trip-wire 1 below — and the binary
> held. Collecting extras looked like it might need a third kind (an input the process waits for),
> but it does not: it is an `act` that reads whatever exists at the moment it fires, defaulting to
> empty. The blocking is already carried by `approved`, and the final PDF landing in your inbox
> before submission is the backstop if a total is wrong.

### Correction is re-running, never amending

**A cycle is never amended. It is voided and re-run.** If an expense surfaces after the PDF
exists, or a figure turns out wrong, the answer is a fresh cycle — not a patch to the old one.

The mechanism is one reserved event and one check:

- **`cycle_voided` is terminal.** The reconciler skips any cycle carrying it, exactly as it skips a
  completed one. Nothing else in the loop changes.
- **The re-run is a new `cycle_key`**, so the voided cycle's events stay in Neon as history. This
  supersedes rather than destroys, which is what append-only events are for and what
  [invariant 19](../invariants.md) expects — a voided attempt is a fact about what happened, and
  the second attempt is structurally identical to a first attempt.

That last property is the entire argument, and it is worth stating as the general rule:

> **Amendment adds a code path to every stage; voiding adds one check to the loop.**

Amending means each stage needs an "unless superseded" clause, the total needs to know which
extras are live, and the already-sent PDF and already-fired emails need reconciling against the new
figures. Re-running has none of that, because the new cycle has never done anything.

**Voiding is only available before `invoice_sent`.** Once the invoice has left for DreamHost the
process is past its point of no return, and what is needed is a credit note — an *accounting*
problem, not a process one. The tier does not model that and should not pretend to.

One consequence to carry: **the invoice number must live on the cycle and be inherited by a
re-run**, so voiding does not burn a number and leave a gap in the sequence.

### The runner: Prefect Cloud on a Managed work pool

**Decided 2026-08-15.** The free tier includes **Managed Execution** — Prefect runs the code on its
own infrastructure, with no worker, no cloud account and nothing on our cluster. Limits are 10
compute-hours per workspace per month, 2 GB RAM and a 24-hour maximum run; a daily reconciler at
~1 minute a run spends well under an hour a month, so the ceiling is roughly an order of magnitude
above the need.

This is the question that actually decided it, and it is worth asking of any managed offering: **a
scheduler that still requires a self-hosted worker buys no infra reduction at all**, only an extra
dependency. Prefect Managed passes; that is what makes external the cheaper answer here rather than
merely the preferred one.

Nothing about the reconciler needs to be inside our network — Neon, the LLM API, SMTP, createmypdf
and the n8n API are all reachable over the public internet with credentials. Had any of them been
cluster-local, this decision would have gone the other way.

**A correction to the reasoning that opened this question.** The earlier framing was *"Prefect earns
its place if you expect more flows."* More processes are expected — but in this design a process is
a **YAML file**, not a flow, and one reconciler loops over all of them. The flow count stays at one
(two, with cycle-opening). So flow count is the wrong reason. The right ones are narrower and
sufficient:

| Need | What Prefect gives |
|---|---|
| Reliable daily trigger | A scheduler that actually fires on time |
| Somewhere to run | Managed execution, zero infra |
| Manual ad-hoc invoice | A deployment run with **parameters** — the `via: manual` genesis path |
| Credentials | Secret blocks, so no second secret store |
| "Did it stop running?" | Run history plus automations (10 on free tier) |
| Debugging | The read-only MCP server |

That is a managed cron with observability, secrets and a parameterised manual trigger — which is
precisely the list, and no more. Note what is *not* on it: task graphs, retries, caching,
concurrency. **This ADR designs orchestration away, so an orchestration platform has little of its
core left to do here** — which is a reason to use a small slice of Prefect deliberately, not a
reason to avoid it.

**Known risks.** Managed Execution is **beta** and its docs warn features may change without
notice; it also pins you to Prefect's own image, so a heavy native dependency could become a
problem (ours are light — the PDF render is an external API call, not a local library).

If either bites, the fallback is **GitHub Actions** — already in this repo, already holding
secrets, and `workflow_dispatch` gives the manual trigger a form for free. Its cron is genuinely
unreliable (10–30 minute delays typical, multi-hour delays and dropped days reported through 2026),
but **the reconciler is unusually tolerant of exactly that**: a missed run costs one day of latency
and nothing else, because the next run recomputes everything from recorded events. That property is
worth noting as a design dividend rather than a fallback detail.

The escape hatch in the other direction is **Inngest**, if a process ever needs to advance the
moment an email lands rather than on the next tick. That is a real pivot — durable event-driven
execution is the paradigm this ADR deliberately declines — and should be taken only with evidence
that daily is too slow.

### Where it lives: a second package in this monorepo

**Decided 2026-08-15.** The tier lives in this repo, as a package beside `inference` rather than
inside it, with definitions at the root mirroring `events/`:

```
processes/*.yml          # definitions — the exact mirror of events/*.yml
src/reconciler/
  definition.py          # the ProcessDefinition schema (pydantic — already a dependency)
  core.py                # PURE: definitions + recorded events → frontier → next action
  actions/               # act implementations; all I/O lives here
  flow.py                # the Prefect entry point — composition root
```

**Beside `inference`, not inside it**, because the ADR's boundary is that the reconciler *acts* and
Aware *observes*. A package whose whole point is calling out — email, PDF services, an LLM — does
not belong inside the package that must never do any of that. There is also little to share: the
reconciler emits **raw** signals through the HTTP gateway like any other producer, so it does not
use `inference.event`'s `InferredEvent` model at all.

**`core.py` is pure, exactly as `inference.runtime.core` is.** The parallel is deliberate and load
-bearing in three ways:

- It is the file-layout expression of the constraint already stated above — *no Prefect-specific
  structure beyond the entry point* — so swapping runners touches `flow.py` and nothing else.
- It keeps the tier testable with **zero new dependencies**. CI installs only
  `pytest ruff pydantic pyyaml` plus `pip install -e . --no-deps`, deliberately, so that a stray
  transport import fails loudly rather than passing on an incidentally-installed package. A
  reconciler core that imports `prefect` or `httpx` at module scope would either break that job or
  force the guard to be weakened.
- Deciding *what to do next* is pure logic over recorded events. Only *doing* it needs the world.

Runner and integration dependencies therefore go in an **optional extra**
(`[project.optional-dependencies] processes = [...]`), never in the base `dependencies` — the
runtime image must not grow a Prefect tree for a component it does not run.

> **Gotcha: do not add `workers/reconciler/`.** `publish-images.yml` auto-discovers every
> `workers/<name>/Dockerfile`, builds `inference-<slug>` and expects to bump
> `deploy/inference/kustomize/base/<slug>/values.yml`. The reconciler has no image and no
> deployment — Prefect Managed runs it from source — so a `workers/` entry would build and publish
> an image nobody runs, against a manifest that does not exist.

### Cycle identity is a body field; `user_id` stays the entity key

**Decided 2026-08-15.** Milestone events carry `user_id` = the human, exactly like every other
producer, and the cycle's identity rides in the body:

```json
{"name": "invoice_approved", "user_id": "<user>", "timestamp": 1755…,
 "process": "dreamhost_invoice", "cycle_key": "dh_invoice_2026_004"}
```

**`user_id` must not carry the cycle.** `Router.key_for`'s own docstring makes the argument: it
keys on `user_id` rather than `source_app` because anything else "would silently fragment one
entity's state across two keys". A cycle key in that slot would do precisely that — every invoice
becoming its own entity, and one user's events scattered across as many state buckets as they have
ever had invoices.

**The keying collision then never arises, because no definition consumes process events.**
`Router.route` walks the consumers map built from each definition's `input_event_names()`; a name
nothing consumes is routed nowhere, touches no state, and simply flows on to Neon through Vector's
persister. Process milestones are therefore exactly ADR 0008 Stage 1's shape — **raw events, visible
in Neon, with no derivation** — and the entity key is only a partitioning detail for a topic that
has one partition.

> **Process event names must be namespaced** (`invoice_*`, or a `process_` prefix) so a milestone
> can never accidentally match an existing definition's `input_event_names()` and get routed into
> an engine that was not expecting it. This is the one thing that would turn a no-op into a bug.

### Why Aware should *not* derive a `process_cycle` event

This answers open question 5, and answers it **no** — which is worth recording because it is the
attractive idea that the section above quietly kills.

A `session_window` pairing `cycle_opened` with `payment_confirmed` would give a whole cycle an
`interval` capability for free, pleasingly symmetric with `car_trip`. It does not work, and the
reason is the Christmas bonus:

- `session_window` keeps **one** open slot per (entity key, definition) — `state.set("open", …)`,
  commented *"remember the (latest) open start"*. It overwrites.
- Entity key is `user_id`, so **all** of one person's cycles share that slot.
- We have established that two cycles can be open at once — a monthly invoice and an ad-hoc bonus.
  The second `cycle_opened` would evict the first, and the first cycle's completion would pair
  against the wrong start, minting a span that never happened.

That is the same class of defect ADR 0011's fusion engine spent three replay-caught mechanics
guarding against, and here it would be introduced for no gain: **a completed cycle's span is
`max(timestamp) - min(timestamp)` grouped by `cycle_key`** — one SQL query the dashboard can run
directly, with no engine, no state, and no concurrency hazard.

The general form, which is the useful part:

> **Aware derives facts it can only learn by watching. A process's own span is not one of those —
> the process knows it.** Deriving it would move a process concern inside the observer to
> re-discover something already recorded.

### n8n holds credentials, never state

The tier uses n8n on both sides, and the distinction matters because ADR 0012 elsewhere
*rejects* n8n (as a place to keep process state, via Wait nodes). What is adopted here is
narrower and compatible:

| Direction | Workflow | What it does |
|---|---|---|
| in | `gmail-labeled-invoice-approved` | a Gmail label becomes a raw event (ADR 0008 Stage 1) |
| out | `mail-relay` | a composed message becomes an email |

> **An outbound relay may authenticate and transmit. It may not compose, decide, or
> interpret.** — the mirror of ADR 0008's connector rule.

The reconciler builds every byte of subject, HTML and text, and decides *whether* to send;
n8n moves bytes using a credential it already holds. No process state, no branching, no
interpretation crosses over — so "the reconciler acts, Aware observes" is untouched.

The reason to do it this way is not architectural neatness: **a personal mail credential
should not live in this repo, gitignored or not.** n8n has a credential store; the repo does
not. The reconciler is left holding exactly **one** secret — a relay token we mint, scoped to
one webhook, revocable in seconds, carrying no personal data — instead of a mail account
password. Direct SMTP remains implemented as a local-testing escape hatch.

Auth on that webhook is mandatory, not a nicety: an unauthenticated endpoint that sends mail
from your own address is an **open relay**, materially worse than the unauthenticated ingest
gateway, which can only write events you can delete.

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
  retire. What survives is one reconciler, a Gmail label rule, and — at least initially — the
  per-cycle extras table.
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
- **Negative — the tier depends on a beta feature of an external service.** Prefect Managed
  Execution is beta and pins us to Prefect's image. Accepted because the fallback is cheap: the
  reconciler is a plain Python function with no Prefect-specific structure, so moving it to GitHub
  Actions or anything else is a change of *caller*, not of design. Keeping the reconciler free of
  Prefect decorators beyond the entry point is therefore a deliberate constraint, not an accident.

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
7. **A `when:` guard appears in a definition.** Conditionals are the symptom that some stage's
   absence is not being handled gracefully; the fix is almost always to let it produce nothing
   rather than to skip it. The Christmas-bonus invoice was the case that would have justified one,
   and it did not need it.

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

1. ~~**Execution environment.**~~ **Resolved 2026-08-15: Prefect Cloud on a Managed work pool** —
   the free tier runs the code on Prefect's infrastructure, so nothing lands on our cluster. See
   *The runner* above.
2. ~~**Is Prefect earning its place?**~~ **Resolved 2026-08-15: yes, for a narrower reason than
   first given.** Not flow count — a process is a YAML file, so the flow count stays at one. It
   earns its place as a managed cron with observability, secrets and a parameterised manual
   trigger.
3. ~~**Where the code lives.**~~ **Resolved 2026-08-15: this monorepo, `src/reconciler/` beside
   `inference`, definitions in `processes/*.yml`.** See *Where it lives* above — including why
   there must be no `workers/reconciler/`.
4. ~~**Cycle keying vs. Aware's entity keying.**~~ **Resolved 2026-08-15: `user_id` stays the
   entity key; the cycle rides in the body as `cycle_key`.** No definition consumes process
   events, so nothing is ever routed and the collision does not arise. Names must be namespaced.
   See *Cycle identity is a body field* above.
5. ~~**Should Aware derive a `process_cycle` event?**~~ **Resolved 2026-08-15: no.**
   `session_window` keeps one open slot per (user, definition), so two concurrent cycles mispair —
   and the span is a `GROUP BY cycle_key` the dashboard can run directly. See *Why Aware should
   not derive a `process_cycle` event* above.
6. **Who owns the `cycle_key` template?** Partly answered by keying on a sequence rather than a
   month — `dh_invoice_{year}_{seq:03d}` no longer encodes a cadence, and the padding mismatch that
   silently emptied the extras table goes with it. What is still open is where the substituted
   values come from for a process whose cycles are not numbered at all.
7. **Backfill.** The current invoice history exists only as email. Whether past cycles are worth
   reconstructing as process events, or the tier simply starts empty, is unresolved.
8. **Where extra line items live.** Keeping the n8n Data Table costs nothing and works today, but
   it is the last reason n8n stays in the process path, and per-cycle tables matched by name fail
   silently. A Neon table plus an editor in the dashboard module is the target; the interim is a
   judgement call about how soon the module exists. Either way it is one `collect.extras`
   implementation, so the choice is reversible.
9. ~~**Should an extra line item be able to arrive late?**~~ **Resolved 2026-08-15: re-run, never
   amend.** See *Correction is re-running, never amending* above.
10. ~~**What is the invoice number, actually?**~~ **Resolved 2026-08-15: a per-year sequence.** It
    coincided with the month only because every invoice so far happened to be a monthly one. A
    re-run inherits its voided cycle's number. What remains open is the smaller mechanical
    question: **where the sequence counter lives** now that Redis is gone. It is the one piece of
    process state that cannot be recomputed from events — though `max(seq) + 1` over the year's
    `cycle_opened` events is a candidate that keeps the "pure function of recorded events" property
    intact, provided cycle opening is serialised.
11. **How is a void triggered?** It is an event like any other, so the candidates are a reply to
    the approval mail (classified), a control in the dashboard module, or a one-line script. The
    module is the natural home, but that makes voiding unavailable until the module exists.
