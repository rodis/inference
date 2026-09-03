# Email todo tasks

**Current truth.** You label a mail `aware/todo` in Gmail; it appears on the Tasks board with its
sender and subject and how long it has been sitting. Tick it off — or take the label off in Gmail
— and it closes.

Built 2026-09-03. Three components, none of them new infrastructure.

## The shape: two events, never a row

A task is **two raw events**, joined on the Gmail message id:

| event | meaning | producers |
|---|---|---|
| `email_labeled_todo` | the task opened | the n8n connector (fast) and the sweep (authoritative) |
| `email_task_closed` | the task closed | the dashboard tick, and the sweep |

Both carry `upstream_id` (the Gmail message id), and the open list is an **anti-join**: labelled
messages whose latest open is newer than their latest close. Same move the process board makes
over `cycle_key`.

**Why not a session engine.** `session_window` pairs a start and an end, which is exactly the
shape of a task — but it holds *one open slot per (user, definition)*, so a second concurrent
task overwrites the first. ADR 0012 refused it for process cycles for the same reason. A SQL join
has no such limit and needs no engine at all.

**Why not a process.** `processes/*.yml` describes a cycle with stages and waits. A task has two
states, so modelling one would mint a cycle per email and hand eleven-stage machinery to a
boolean.

**Latest-open vs latest-close, not "has ever been closed".** Re-applying the label to a mail you
finished last month is a legitimate reopen. A plain `IS NULL` anti-join would refuse it forever:
the old close would suppress the new open, the task would be invisible on the board while visibly
labelled in Gmail — and the sweep, seeing a label with no open, would emit *another* open every
hour for as long as the label stayed on. A permanent event-per-hour loop whose only symptom is a
growing table. Comparing timestamps makes reopening work by construction.

## Closing is the hard half

Gmail reports a label being **added** and never one being removed. So no trigger can see a task
finish, and closing cannot come from a connector at all.

Two paths, and they cannot disagree, because both derive from the label rather than from each
other:

1. **The tick** (`POST /api/tasks/close`) removes the label, then records the close. Instant.
2. **The sweep** (`reconciler.tasks`, **every 15 minutes**) asks Gmail *what is labelled right
   now?* and emits the difference in both directions:

   ```
   labelled in Gmail, no open event   ->  email_labeled_todo   (repairs a dropped trigger)
   open event, not labelled in Gmail  ->  email_task_closed    (done, however it was done)
   ```

The sweep is what makes unlabelling on your phone work, and it is the repair path for the one
inconsistency the tick can leave: if the label comes off but recording it fails, Gmail and the log
disagree until the next sweep settles it.

That interval was **hourly until 2026-09-03**, and not by choice. Prefect Managed bills a
60-second minimum per run against a 30,000s/month workspace limit, so 15-minute scheduling was
2,976 runs = 595% of quota — unavailable at any price on the free tier. Unlabelling a mail on your
phone and watching it sit on the board for another 50 minutes was the visible cost. Scheduling now
runs on Argo Workflows on the cluster, where the interval costs a few seconds of a node already
paid for (ADR 0012's amendment, *scheduling moves to Argo Workflows*). The tick's ordering is load-bearing —
label first, event only on success — because the reverse would record a close against a mail still
sitting in the label, hiding a task that is not done.

### The guard on absence

The sweep closes tasks on the *absence* of evidence, which is dangerous, so it is bounded. The
Gmail search looks back `--lookback` days (default 365); a mail older than that is simply not in
the answer, which is indistinguishable from "not labelled any more". So **a task that opened
before the search horizon is never closed** (`tasks.diff`'s `stale_horizon`). Without it,
shortening the lookback would silently retire every long-lived task at once — and an insurance
renewal or a passport appointment legitimately sits for months.

## The dashboard became a producer

Ticking a task is the first thing the dashboard does besides read and write its own
`dashboard_prefs` row. That is a smaller departure than it sounds:

- **The decision is the click.** The dashboard is the input device, which is precisely the role
  the iOS Shortcuts play for `car_lock_state_change` — a human action captured where it happens
  and POSTed to `/sensors/<app>`.
- **It holds no Google credential.** The OAuth token stays in n8n; the dashboard calls
  `Relay: gmail label remove`, which may authenticate and transmit but decides nothing. ADR 0008's
  boundary is about connectors inventing facts, and nothing here does.
- **It needed no infrastructure.** `route_by_app.yml` routes every app that is not `overland` to
  the standard adapter, so `/sensors/tasks` works with no Vector change, no transform and no new
  topic — the same reason the reconciler's milestones needed none.

The three env vars (`GMAIL_LABEL_URL`, `MAIL_RELAY_TOKEN`, `VECTOR_BASE_URL`) are **optional** in
the deploy manifest. Missing any of them disables the tick with a 503 naming what is unset; the
board still reads, and closing in Gmail still works, because the sweep is what notices that.

## One event name, two producers

`email_labeled_todo` is emitted by both the connector and the sweep, and `email_task_closed` by
both the tick and the sweep. A consumer able to tell them apart would be reading one of them
wrong, so their bodies must match field for field.

They cannot share a module: the dashboard image is built with `dashboard/` as its Docker context,
so `src/reconciler` is not importable there — the same build boundary that made
`dashboard/processes.json` a generated file. `tests/test_task_contract.py` is what stops the
duplication drifting; it reads `dashboard/app.py` and the connector as text and AST (neither can
be imported in CI) and compares them against `reconciler.tasks`.

## What is live, and what is left

Deployed to n8n 2026-09-03 and **published**:

| workflow | id | tested |
|---|---|---|
| `Connector: gmail — labelled todo mail` | `zpxESD7h2Aovl19i` | polls every minute; matches nothing until the label exists |
| `Relay: gmail label remove` | `WT077ZcgeAHRiEB5` | end to end, without changing a label (see below) |

The relay was tested by asking it to remove `aware/parking` from a message that verifiably did
not carry it: every node ran — header auth, label listing, the name→id filter, the Gmail modify
call — and the message's labels were byte-identical afterwards. A missing and a wrong token both
returned 403.

⚠️ **The credential trap bit again.** `newCredential('Aware mail relay token')` resolves by
credential *type*, so n8n bound `Header Auth account` to the relay's webhook instead, and it
would have accepted the wrong shared secret. Rebind by ID (`cBeDjEhQ1Nx2G1oX`) after any create
or update — the connector files record the exact call.

Still needed before a task can appear:

1. **Create the `aware/todo` label in Gmail** and apply it to something. Only you can.
2. **Three Doppler keys** on `neon-credentials-for-dashboard`, or the tick 503s (reading works
   regardless): `GMAIL_LABEL_URL` (the relay webhook), `MAIL_RELAY_TOKEN`, `VECTOR_BASE_URL`.
3. **`GMAIL_LABEL_URL` in the reconciler's Doppler config** is *not* needed — the sweep only
   reads Gmail and emits events; it never removes a label.

## Files

```
connectors/n8n/gmail-labeled-todo.workflow.ts   label -> event, 3 nodes (the fast path)
connectors/n8n/gmail-label-remove.workflow.ts   the outbound relay: take a label off
src/reconciler/tasks.py                         the pure diff + the sweep
src/reconciler/adapters/ingest.py               generic raw-event POST
src/reconciler/adapters/labels.py               the label relay client
dashboard/app.py                                GET /api/tasks, POST /api/tasks/close
dashboard/web/src/dashboards/tasks/             the board
deploy/inference/kustomize/base/reconciler/      the CronWorkflow: sweep-tasks, every 15 min
prefect.yaml                                    email-tasks-sweep, the daily backstop at 06:37
```

## Running it

```bash
# See what the sweep disagrees with Gmail about. Writes nothing, sends nothing.
(cd workers && uv run python -m reconciler.run sweep-tasks --dry-run)

# For real
(cd workers && uv run python -m reconciler.run sweep-tasks)
```

`:37` rather than `:17`, so it does not queue behind the invoice advance for the same n8n relay
and the same Managed-pool slot.

## Not built

- **No due dates and no priority.** A label carries neither, and inferring them from the mail is a
  classifier — which ADR 0012 puts in the reconciler, and which nothing here needs yet.
- **No nudging.** The board is read-and-tick; the outbound lane (Pushcut) is proven but unused
  here. A "this has sat for three weeks" notification would be a `notify` stage, not a new tier.
- **Nothing derives from these events.** They are raw rows, like `email_labeled_parking`. A
  derived `task` event with capabilities would need the `on_event` engine that
  [`connectors.md`](connectors.md) §2 sketches and that does not exist yet.
