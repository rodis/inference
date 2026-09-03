import { useState } from "react";
import { Check, Mail, ExternalLink } from "lucide-react";
import { useAware } from "../../app/useAware";
import { useQuery } from "../../app/useQuery";
import {
  ageLabel, gmailLink, groupTasks, senderOf, subjectOf, tasksUrl,
  type Task,
} from "./task";

/** The Tasks board — mail you labelled `aware/todo`, and how long it has been sitting.
 *
 *  **The only board that writes.** Ticking a row calls `/api/tasks/close`, which takes the label
 *  off the message and records an `email_task_closed` event. That makes the dashboard a producer
 *  for the first time — a smaller departure than it sounds, since the decision is the click and
 *  the dashboard is only the input device, the same role the iOS Shortcuts play for
 *  `car_lock_state_change`.
 *
 *  Closing is deliberately NOT a local toggle. The task's state lives in the event log, so the
 *  tick has to reach it; what happens locally is only the optimistic redraw, so the row moves
 *  the instant you click instead of after a round trip. If the call fails the row comes back and
 *  says why. */

const stamp = (epoch: number) =>
  new Date(epoch * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });

function TaskRow({ task, now, stale, onClose, busy, failed }: {
  task: Task; now: number; stale: boolean; busy: boolean; failed?: string;
  onClose: (t: Task) => void;
}) {
  const done = task.closed;
  return (
    <li className={"tsk-row" + (stale ? " is-stale" : "") + (done ? " is-done" : "")}>
      <button className="tsk-tick" disabled={busy || done}
        onClick={() => onClose(task)}
        aria-label={done ? "Closed" : `Mark "${subjectOf(task)}" done`}
        title={done ? `closed ${task.closed_via === "sweep" ? "in Gmail" : "here"}` : "Mark done"}>
        <Check size={12} strokeWidth={3.5} />
      </button>

      <div className="tsk-body">
        <div className="tsk-subject">{subjectOf(task)}</div>
        <div className="tsk-from">
          <span className="tsk-glyph"><Mail size={9} strokeWidth={2.5} /></span>
          {senderOf(task)}
          {task.from && task.from_name && <span className="tsk-addr">{task.from}</span>}
          <a className="tsk-link" href={gmailLink(task)} target="_blank" rel="noreferrer">
            open in Gmail <ExternalLink size={9} />
          </a>
        </div>
        {failed && <div className="tsk-failed">{failed}</div>}
      </div>

      <div className="tsk-age">
        <span className="d">
          {done ? "closed" : busy ? "…" : ageLabel(task.opened_epoch, now)}
        </span>
        {stamp(done && task.closed_epoch ? task.closed_epoch : task.opened_epoch)}
      </div>
    </li>
  );
}

export default function TasksDashboard() {
  const { userId, status, client } = useAware();
  const url = tasksUrl(userId);
  const { data, error } = useQuery<Task[]>(url);

  // Optimistic overlay. Keyed by upstream_id so it survives the list re-sorting under it, and
  // merged rather than replacing the fetched rows — the server stays the source of truth and a
  // refetch simply agrees with what is already on screen.
  const [closedNow, setClosedNow] = useState<Record<string, number>>({});
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [failed, setFailed] = useState<Record<string, string>>({});

  if (status) return <div className="statusline">{status}</div>;
  if (error) return <div className="statusline">Tasks unavailable: {error}</div>;
  if (!data) return <div className="statusline">Loading…</div>;

  const now = Math.floor(Date.now() / 1000);
  const tasks = data.map((t) =>
    closedNow[t.upstream_id]
      ? { ...t, closed: true, closed_epoch: closedNow[t.upstream_id], closed_via: "dashboard" }
      : t);
  const g = groupTasks(tasks, now);

  async function close(task: Task) {
    setBusy((b) => ({ ...b, [task.upstream_id]: true }));
    setFailed((f) => { const { [task.upstream_id]: _drop, ...rest } = f; return rest; });
    try {
      const res = await fetch(`/api/tasks/close?user_id=${encodeURIComponent(userId)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          upstream_id: task.upstream_id,
          subject: task.subject,
          from_name: task.from_name,
          opened_epoch: task.opened_epoch,
        }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail.detail || `HTTP ${res.status}`);
      }
      const body = await res.json();
      setClosedNow((c) => ({ ...c, [task.upstream_id]: body.closed_epoch || now }));
      // So the next visit to this board refetches rather than serving the pre-tick list.
      client?.invalidate("/api/tasks");
    } catch (e) {
      // The row stays open, which is the truthful outcome: if the label came off but recording
      // it failed, the hourly sweep closes it — and until then it really is still labelled.
      setFailed((f) => ({ ...f, [task.upstream_id]: (e as Error).message }));
    } finally {
      setBusy((b) => ({ ...b, [task.upstream_id]: false }));
    }
  }

  const rows = (list: Task[], stale: boolean) =>
    list.map((t) => (
      <TaskRow key={t.upstream_id} task={t} now={now} stale={stale} onClose={close}
        busy={!!busy[t.upstream_id]} failed={failed[t.upstream_id]} />
    ));

  return (
    <>
      <div className="pagehead">
        <div className="eyebrow">Processes</div>
        <h1 className="ptitle">Tasks</h1>
        <div className="psub">
          mail you labelled <b>aware/todo</b> · {g.open} open · reconciled hourly
        </div>
      </div>

      <div className="tsk-strip">
        <div className="tsk-stat"><div className="k">Open</div><div className="v">{g.open}</div></div>
        <div className="tsk-stat">
          <div className="k">Oldest</div>
          <div className={"v" + (g.oldestDays >= 7 ? " warn" : "")}>
            {g.oldestDays}<small>days</small>
          </div>
        </div>
        <div className="tsk-stat"><div className="k">Added this week</div><div className="v">{g.addedThisWeek}</div></div>
        <div className="tsk-stat"><div className="k">Closed this week</div><div className="v">{g.closedThisWeek}</div></div>
      </div>

      {tasks.length === 0 ? (
        <div className="panel">
          <div className="tsk-empty">
            <h3>Nothing labelled yet</h3>
            <p>
              Apply the <b>aware/todo</b> label to a mail in Gmail and it shows up here within a
              minute. Take the label off — here or in Gmail — and it closes.
            </p>
          </div>
        </div>
      ) : (
        <div className="panel tsk-panel">
          {g.stale.length > 0 && (
            <>
              <div className="tsk-grouphead">
                <h3>Sitting more than a week</h3>
                <span className="n">{g.stale.length}</span>
                <span className="why">oldest first</span>
              </div>
              <ul className="tsk-list">{rows(g.stale, true)}</ul>
            </>
          )}

          {g.recent.length > 0 && (
            <>
              <div className="tsk-grouphead">
                <h3>This week</h3>
                <span className="n">{g.recent.length}</span>
              </div>
              <ul className="tsk-list">{rows(g.recent, false)}</ul>
            </>
          )}

          {g.closed.length > 0 && (
            <>
              <div className="tsk-grouphead">
                <h3>Closed recently</h3>
                <span className="n">{g.closed.length}</span>
                <span className="why">label removed</span>
              </div>
              <ul className="tsk-list">{rows(g.closed, false)}</ul>
            </>
          )}
        </div>
      )}
    </>
  );
}
