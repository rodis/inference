/** Email todo tasks — the contract and the one piece of shaping the board needs.
 *
 *  A task is two raw events, never a mutable row: `email_labeled_todo` opens it and
 *  `email_task_closed` closes it, joined on the Gmail message id. `/api/tasks` does that join
 *  in SQL and hands back one row per task, so nothing here has to know about events at all. */

export interface Task {
  upstream_id: string;
  subject: string | null;
  from_name: string | null;
  from: string | null;
  thread_id: string | null;
  opened_epoch: number;
  closed_epoch: number | null;
  closed_via: string | null;
  closed: boolean;
}

export const DEFAULT_LABEL = "aware/todo";

export const tasksUrl = (userId: string, label = DEFAULT_LABEL) =>
  userId
    ? `/api/tasks?user_id=${encodeURIComponent(userId)}&label=${encodeURIComponent(label)}`
    : null;

/** A mail sitting longer than this is drawn as needing attention.
 *
 *  Seven days rather than a tuned number: it is the span a person actually reasons in ("this has
 *  been here over a week"), and nothing downstream branches on it — it only decides which group
 *  a row is drawn in, so being wrong costs a heading, not a decision. */
export const STALE_DAYS = 7;

export const DAY = 86400;

export const ageDays = (epoch: number, now: number) =>
  Math.max(0, Math.floor((now - epoch) / DAY));

/** "12d" / "5h" / "just now" — an age, not a date.
 *
 *  Ages rather than timestamps because the question this board answers is "how long has this
 *  been sitting?", and a reader converting 22 August into eleven days in their head is doing the
 *  board's job for it. The exact date is still on the row, in smaller type. */
export function ageLabel(epoch: number, now: number): string {
  const secs = Math.max(0, now - epoch);
  if (secs < 3600) return "just now";
  if (secs < DAY) return `${Math.floor(secs / 3600)}h`;
  return `${Math.floor(secs / DAY)}d`;
}

export interface TaskGroups {
  stale: Task[];
  recent: Task[];
  closed: Task[];
  open: number;
  oldestDays: number;
  addedThisWeek: number;
  closedThisWeek: number;
}

/** Split the list the way it is read.
 *
 *  **Grouped by staleness, not sorted by date.** "Keep track of it" means noticing what is
 *  rotting, and a flat newest-first list buries exactly that — the oldest thing, the one most
 *  likely to be a problem, ends up furthest from the top. Within each group it is oldest first,
 *  for the same reason.
 *
 *  A reopened task (label re-applied after a close) arrives from the API as open, because the
 *  join compares the LATEST open against the LATEST close. Nothing special is needed here. */
export function groupTasks(tasks: Task[], now: number): TaskGroups {
  const staleBefore = now - STALE_DAYS * DAY;
  const weekAgo = now - 7 * DAY;

  const open = tasks.filter((t) => !t.closed);
  const closed = tasks
    .filter((t) => t.closed)
    .sort((a, b) => (b.closed_epoch ?? 0) - (a.closed_epoch ?? 0));

  const byAge = (a: Task, b: Task) => a.opened_epoch - b.opened_epoch;

  return {
    stale: open.filter((t) => t.opened_epoch < staleBefore).sort(byAge),
    recent: open.filter((t) => t.opened_epoch >= staleBefore).sort(byAge),
    closed,
    open: open.length,
    oldestDays: open.length
      ? ageDays(Math.min(...open.map((t) => t.opened_epoch)), now)
      : 0,
    addedThisWeek: open.filter((t) => t.opened_epoch >= weekAgo).length,
    closedThisWeek: closed.filter((t) => (t.closed_epoch ?? 0) >= weekAgo).length,
  };
}

/** Gmail's own URL for a message. `thread_id` rather than the message id, because that is what
 *  opens the conversation as you'd expect to see it; falling back to a search on the message id
 *  keeps the link working for a task recorded before threads were captured. */
export function gmailLink(task: Task): string {
  return task.thread_id
    ? `https://mail.google.com/mail/u/0/#all/${task.thread_id}`
    : `https://mail.google.com/mail/u/0/#search/rfc822msgid%3A${encodeURIComponent(task.upstream_id)}`;
}

export const senderOf = (task: Task) =>
  task.from_name || task.from || "unknown sender";

export const subjectOf = (task: Task) =>
  task.subject || "(no subject)";
