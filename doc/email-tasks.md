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
| `email_task_closed` | the task closed | **the sweep, and only the sweep** |

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

So the label is the only thing anyone changes, and **the sweep is the sole producer of
`email_task_closed`** (`reconciler.tasks`, **every 5 minutes**). It asks Gmail *what is labelled
right now?* and emits the difference in both directions:

```
labelled in Gmail, no open event   ->  email_labeled_todo   (repairs a dropped trigger)
open event, not labelled in Gmail  ->  email_task_closed    (done, however it was done)
```

Ticking a row on the board (`POST /api/tasks/close`) removes the label and stops there. It is
therefore not a second path at all — it is the same path, entered from a different button, and
indistinguishable to the sweep from unlabelling the mail on your phone.

**It used to be a second path**, and that is worth recording because the reason it stopped is
not the reason you would guess. Until 2026-10-13 the tick removed the label *and* POSTed
`email_task_closed` at the ingest gateway, which made the close instant and left the sweep as a
repair path. Both producers read the same Neon log, so they could not disagree. Then the sweep's
store of record moved to MySQL (to stop a 15-minute cron waking a scale-to-zero Postgres — see
the reconciler's own notes), and the dashboard cannot reach MySQL: a close emitted from the board
would land in Neon, be invisible to the sweep, and the next run would find the task open-and-
unlabelled and close it **again**. Two closes on the timeline for every tick.

The fix was not to give the dashboard a second credential. It was to notice that *removing the
label already is the close*, so there was never a need for two producers — only for one of them
to be fast. The cost is latency, which is what the interval buys back: the board hides the row
immediately (an overlay that survives a reload, bounded at 30 minutes), and the timeline catches
up on the next sweep.

That interval was **hourly until 2026-09-03** and not by choice: Prefect Managed bills a
60-second minimum per run against a 30,000s/month workspace limit, so 15-minute scheduling was
2,976 runs = 595% of quota — unavailable at any price on the free tier. Argo Workflows removed
the per-run floor (ADR 0012's amendment, *scheduling moves to Argo Workflows*) and MySQL removed
the Neon wake behind it, so what sets the number now is simply how long a ticked task may take to
reach the timeline.

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

The two env vars (`GMAIL_LABEL_URL`, `MAIL_RELAY_TOKEN`) are **optional** in the deploy
manifest. Missing either disables the tick with a 503 naming what is unset; the board still
reads, and closing in Gmail still works, because the sweep is what notices that. There is no
`VECTOR_BASE_URL` any more: the dashboard POSTs at no ingest gateway at all.

## One event name, two producers

`email_labeled_todo` is emitted by both the connector and the sweep. A consumer able to tell
them apart would be reading one of them wrong, so their bodies must match field for field.
`email_task_closed` used to have two producers as well and no longer does — see above.

They cannot share a module: the dashboard image is built with `dashboard/` as its Docker context,
so `src/reconciler` is not importable there — the same build boundary that made
`dashboard/processes.json` a generated file. `tests/test_task_contract.py` is what holds the
line; it reads `dashboard/app.py` and the connector as text (neither can be imported in CI),
checks the connector's fields against `reconciler.tasks`, and asserts that the dashboard POSTs
at **no** `/sensors/…` endpoint, because a restored second producer would double every close on
the timeline without failing anything else.

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
2. **Two Doppler keys** on `neon-credentials-for-dashboard`, or the tick 503s (reading works
   regardless): `GMAIL_LABEL_URL` (the relay webhook) and `MAIL_RELAY_TOKEN`.
3. **`GMAIL_LABEL_URL` in the reconciler's Doppler config** is *not* needed — the sweep only
   reads Gmail and emits events; it never removes a label.

## Files

```
connectors/n8n/gmail-labeled-todo.workflow.ts   label -> event, 3 nodes (the fast path)
connectors/n8n/gmail-label-remove.workflow.ts   the outbound relay: take a label off
src/reconciler/tasks.py                         the pure diff + the sweep
src/reconciler/adapters/ingest.py               generic raw-event POST
src/reconciler/adapters/labels.py               the label relay client
dashboard/app.py                                GET /api/tasks, POST /api/tasks/close (label only)
dashboard/web/src/dashboards/tasks/             the board
deploy/inference/kustomize/base/reconciler/      the CronWorkflow: sweep-tasks, every 5 min
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
