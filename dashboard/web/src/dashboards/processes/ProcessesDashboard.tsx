import { useState } from "react";
import { Check, Clock, Circle, Hand, CalendarClock, Ban, Zap, Eye } from "lucide-react";
import { useAware } from "../../app/useAware";
import { useQuery } from "../../app/useQuery";
import {
  chipsOf, cronText, cyclesUrl, processesUrl, stamp, statusOf,
  type Cycle, type ProcessDef, type StageState, type StageStatus,
} from "./process";

/** The Processes board (ADR 0012's process tier) — one long-running process, its cycles, and
 *  exactly where each one stands.
 *
 *  **A vertical stepper, not a node graph.** A process is a DAG in the definition language,
 *  but this one is a chain, and every source consulted says the same thing: a stepper is the
 *  right pattern for a long flow with strong dependencies (payment flows are the canonical
 *  example), while a node-graph earns its layout cost only once branches exist. So the board
 *  renders topological order and shows `after` explicitly whenever a stage's dependency is
 *  NOT simply the row above — which is the honest signal that a real branch has appeared and
 *  this rendering has stopped being sufficient.
 *
 *  What it is for is the question the prior art could never answer: *is it stuck, and on
 *  what?* Hence `waiting` is its own state, drawn differently from "not started" — the
 *  reconciler is actively asking Gmail about that stage every hour, and the ones below it are
 *  merely unreachable. */

const ICON: Record<StageState, typeof Check> = {
  done: Check, waiting: Clock, pending: Circle, skipped: Ban,
};

/** An `await` waits on the world; an `act` is something the reconciler does itself. Worth
 *  distinguishing on the row, because it tells you who is holding the process up — a stalled
 *  `act` is our bug, a stalled `await` is usually a human who has not replied. */
const KIND_ICON = { await: Eye, act: Zap, genesis: CalendarClock } as const;

function StageRow({ s, prevName }: { s: StageStatus; prevName?: string }) {
  const Glyph = ICON[s.state];
  const Kind = KIND_ICON[s.stage.kind];
  const chips = chipsOf(s.message);
  // Only surfaced when the dependency is not the row directly above — see the header note.
  const oddDeps = s.stage.after.filter((d) => d !== prevName);
  return (
    <li className={"pst-row is-" + s.state}>
      <span className="pst-glyph"><Glyph size={13} strokeWidth={3} /></span>
      <div className="pst-body">
        <div className="pst-head">
          <span className="pst-label">{s.stage.label}</span>
          <span className="pst-kind" title={s.stage.kind === "await" ? "waits for a fact" : "the reconciler does it"}>
            <Kind size={10} /> {s.stage.kind}
          </span>
          {s.at != null && <span className="pst-at">{stamp(s.at)}</span>}
          {s.state === "waiting" && <span className="pst-at pst-now">waiting</span>}
        </div>
        <div className="pst-detail">{s.stage.detail}</div>
        {oddDeps.length > 0 && (
          <div className="pst-detail">after {oddDeps.join(" + ")}</div>
        )}
        {chips.length > 0 && (
          <div className="pst-chips">
            {chips.map((c) => (
              <span className="pst-chip" key={c.key}>
                <b>{c.key}</b>
                {c.href
                  ? <a href={c.href} target="_blank" rel="noreferrer">{c.value}</a>
                  : c.value}
              </span>
            ))}
          </div>
        )}
      </div>
    </li>
  );
}

export default function ProcessesDashboard() {
  const { userId, status } = useAware();
  const { data: catalog, error: catErr } = useQuery<{ processes: ProcessDef[] }>(processesUrl);
  const [pick, setPick] = useState<string>();
  const [pickCycle, setPickCycle] = useState<string>();

  const defs = catalog?.processes ?? [];
  const def = defs.find((d) => d.name === pick) ?? defs[0];
  const { data: cycles, error } = useQuery<Cycle[]>(def ? cyclesUrl(def.name, userId) : null);

  if (status) return <div className="statusline">{status}</div>;
  if (catErr) return <div className="statusline">Process definitions unavailable: {catErr}</div>;
  if (!catalog) return <div className="statusline">Loading…</div>;
  if (!def) return <div className="statusline">No processes defined.</div>;

  const cycle = cycles?.find((c) => c.cycle_key === pickCycle) ?? cycles?.[0];
  const st = cycle ? statusOf(def, cycle) : null;
  const schedule = def.opens.find((o) => o.via === "schedule");
  const manual = def.opens.some((o) => o.via === "manual");

  return (
    <>
      <div className="pagehead">
        <div className="eyebrow">Processes</div>
        <h1 className="ptitle">{def.label}</h1>
        <div className="psub">
          {def.cycle_key}
          {schedule && <> · opens {cronText(schedule.cron!)}</>}
          {manual && <> · or by hand</>}
        </div>
      </div>

      {defs.length > 1 && (
        <div className="pst-tabs">
          {defs.map((d) => (
            <button key={d.name} className={"pst-tab" + (d.name === def.name ? " on" : "")}
              onClick={() => { setPick(d.name); setPickCycle(undefined); }}>
              {d.label}
            </button>
          ))}
        </div>
      )}

      {error && <div className="statusline">Cycle query failed: {error}</div>}
      {!error && !cycles && <div className="statusline">Loading cycles…</div>}
      {cycles && cycles.length === 0 && (
        <div className="statusline">
          No cycles recorded yet. {schedule
            ? `One opens ${cronText(schedule.cron!)}.`
            : "This process only opens by hand."}
        </div>
      )}

      {st && cycles && (
        <div className="pst-cols">
          <section className="panel">
            <h3>{st.cycle.cycle_key}</h3>
            <div className="psub">
              {st.done} of {st.total} steps
              {st.voided
                ? " · voided"
                : st.frontier
                  ? ` · waiting on ${st.frontier.label.toLowerCase()}`
                  : " · complete"}
            </div>
            <ol className="pst-list">
              {st.stages.map((s, i) => (
                <StageRow key={s.stage.name} s={s}
                  prevName={i > 0 ? st.stages[i - 1].stage.name : undefined} />
              ))}
            </ol>
          </section>

          <aside className="pst-rail">
            <section className="panel">
              <h3>Cycles</h3>
              <div className="psub">newest first · {cycles.length} shown</div>
              {/* Airflow's grid view, shrunk: cycles as rows and stages as cells answers
                  "how did the last nine go?" in one glance, which a stepper per cycle
                  cannot. Clicking a row loads it into the stepper on the left. */}
              <div className="pst-grid">
                {cycles.map((c) => {
                  const cs = statusOf(def, c);
                  return (
                    <button key={c.cycle_key}
                      className={"pst-gridrow" + (c.cycle_key === st.cycle.cycle_key ? " on" : "")}
                      onClick={() => setPickCycle(c.cycle_key)}
                      title={`${cs.done}/${cs.total} · ${cs.voided ? "voided"
                        : cs.frontier ? "waiting on " + cs.frontier.label : "complete"}`}>
                      <span className="pst-gridkey">{c.cycle_key.replace(/^.*_/, "")}</span>
                      <span className="pst-dots">
                        {cs.stages.map((s) => (
                          <i key={s.stage.name} className={"pst-dot is-" + s.state} />
                        ))}
                      </span>
                      <span className="pst-gridn">{cs.done}/{cs.total}</span>
                    </button>
                  );
                })}
              </div>
            </section>

            <section className="panel">
              <h3>How it advances</h3>
              <div className="psub">the reconciler is a pure function of these milestones</div>
              <ul className="pst-legend">
                <li><Zap size={11} /><span><b>act</b> — the reconciler does it and records it</span></li>
                <li><Eye size={11} /><span><b>await</b> — it watches for a fact and records when it appears</span></li>
                <li><Hand size={11} /><span>a gate marked <b>waiting</b> needs something from the world;
                  it is re-checked hourly, so nothing is lost by leaving it</span></li>
              </ul>
            </section>
          </aside>
        </div>
      )}
    </>
  );
}
