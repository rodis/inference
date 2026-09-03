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
2. **The sweep** (`reconciler.tasks`, hourly) asks Gmail *what is labelled right now?* and emits
   the difference in both directions:

   ```
   labelled in Gmail, no open event   ->  email_labeled_todo   (repairs a dropped trigger)
   open event, not labelled in Gmail  ->  email_task_closed    (done, however it was done)
   ```

The sweep is what makes unlabelling on your phone work, and it is the repair path for the one
inconsistency the tick can leave: if the label comes off but recording it fails, Gmail and the log
disagree for up to an hour and then the sweep settles it. The tick's ordering is load-bearing —
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

## Files

```
connectors/n8n/gmail-labeled-todo.workflow.ts   label -> event, 3 nodes (the fast path)
connectors/n8n/gmail-label-remove.workflow.ts   the outbound relay: take a label off
src/reconciler/tasks.py                         the pure diff + the sweep
src/reconciler/adapters/ingest.py               generic raw-event POST
src/reconciler/adapters/labels.py               the label relay client
dashboard/app.py                                GET /api/tasks, POST /api/tasks/close
dashboard/web/src/dashboards/tasks/             the board
prefect.yaml                                    email-tasks-sweep, hourly at :37
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
