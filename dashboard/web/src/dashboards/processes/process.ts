/** The process tier's contracts and the one derivation the board needs (ADR 0012).
 *
 *  Two routes, two shapes: `/api/processes` is the DEFINITION (generated from
 *  `processes/*.yml` by scripts/emit_process_graph.py) and `/api/processes/:name/cycles` is
 *  the STATE (milestone rows out of Neon). Keeping them apart is the whole design — "the
 *  definition is the graph, the events are the state" — and it is why this board can show a
 *  stage that has *not* happened, which a purely event-sourced view structurally cannot. */

export type StageKind = "genesis" | "act" | "await";

export interface StageDef {
  name: string;
  label: string;
  kind: StageKind;
  after: string[];
  detail: string;      // the action that runs, or what the await is watching
  event: string;       // the milestone event name, e.g. dreamhost_invoice_total_computed
}

export interface ProcessDef {
  name: string;
  label: string;
  cycle_key: string;
  opens: { via: "schedule" | "manual"; cron: string | null }[];
  stages: StageDef[];
  void_event: string;
}

export interface Milestone {
  name: string;
  epoch: number;
  message: Record<string, unknown>;
}

export interface Cycle {
  cycle_key: string;
  opened_epoch: number;
  last_epoch: number;
  milestone_count: number;
  milestones: Milestone[];
}

export const processesUrl = "/api/processes";
export const cyclesUrl = (name: string, userId: string) =>
  userId ? `/api/processes/${encodeURIComponent(name)}/cycles?user_id=${encodeURIComponent(userId)}` : null;

/** Where a stage stands. `waiting` is the frontier — the one thing a reader is actually
 *  looking for — and is deliberately distinct from `pending`: "we are asking Gmail about this
 *  every hour" and "this cannot start yet" look identical in a two-state done/not-done view,
 *  which is exactly the ambiguity that made the prior art impossible to reason about. */
export type StageState = "done" | "waiting" | "pending" | "skipped";

export interface StageStatus {
  stage: StageDef;
  state: StageState;
  at?: number;                          // when the milestone was recorded (evidence time)
  message?: Record<string, unknown>;
}

export interface CycleStatus {
  cycle: Cycle;
  voided: boolean;
  stages: StageStatus[];
  frontier?: StageDef;                  // absent when the cycle is complete or voided
  done: number;
  total: number;
}

/** Intersect a definition with what actually happened.
 *
 *  This is the reconciler's own rule, re-applied in the browser: a stage is done when its
 *  milestone exists, and the frontier is the first stage that is not done and whose `after`
 *  are all done. Re-deriving rather than reading a stored status is what makes the page
 *  unable to disagree with the runner — there is no status column to go stale.
 *
 *  **Order comes from the definition, never from the timestamps.** A satisfied `await` is
 *  stamped with its EVIDENCE's time, not the run clock, so `data_approved` legitimately
 *  predates the `approval_requested` that asked for it (observed on both real invoice
 *  cycles). Sorting by `epoch` here would render the process out of order and make the
 *  approval look like it preceded the request. */
export function statusOf(def: ProcessDef, cycle: Cycle): CycleStatus {
  const seen = new Map<string, Milestone>();
  for (const m of cycle.milestones) seen.set(m.name, m);
  const voided = seen.has(def.void_event);

  const doneNames = new Set<string>();
  let frontier: StageDef | undefined;

  const stages: StageStatus[] = def.stages.map((stage) => {
    const m = seen.get(stage.event);
    if (m) {
      doneNames.add(stage.name);
      return { stage, state: "done" as StageState, at: m.epoch, message: m.message };
    }
    // A voided cycle stops where it stopped: nothing is waiting on anything any more, so
    // every unreached stage reads `skipped` rather than implying the reconciler is still
    // watching for it. Correction is a re-run under a new key, never an amendment.
    if (voided) return { stage, state: "skipped" as StageState };
    const ready = stage.after.every((dep) => doneNames.has(dep));
    if (ready && !frontier) {
      frontier = stage;
      return { stage, state: "waiting" as StageState };
    }
    return { stage, state: "pending" as StageState };
  });

  return {
    cycle, voided, stages, frontier,
    done: doneNames.size,
    total: def.stages.length,
  };
}

/** Envelope keys every milestone carries. Excluded from the rendered detail because they are
 *  identity and routing, not facts about the step — and they are already on screen (the cycle
 *  key in the switcher, the stage name as the row's own title). */
const ENVELOPE = new Set(["id", "name", "process", "user_id", "cycle_key", "timestamp"]);

export interface Chip { key: string; value: string; href?: string }

/** A milestone's payload as a few compact chips.
 *
 *  **Generic, not a per-stage lookup table.** The eleven invoice stages emit eleven different
 *  payload shapes and process #2 will emit more; a table keyed on stage name would put
 *  per-process knowledge in shipped code and leave every future process blank. So this
 *  classifies by VALUE shape instead — scalars print, URLs become links, collections collapse
 *  to a count, nested objects are left to the raw view. The same move `capabilities.vehicle`
 *  makes in the backend, and for the same reason. */
export function chipsOf(message: Record<string, unknown> | undefined, max = 4): Chip[] {
  if (!message) return [];
  const chips: Chip[] = [];
  for (const [key, value] of Object.entries(message)) {
    if (ENVELOPE.has(key) || value == null) continue;
    if (typeof value === "string") {
      if (/^https?:\/\//.test(value)) {
        // Presigned and short-lived (CraftMyPDF hands out a 7-day S3 signature), so an old
        // cycle's link will 403 rather than 404. Rendered anyway: within the week it is the
        // single most useful thing on the row.
        chips.push({ key, value: "open", href: value });
      } else {
        chips.push({ key, value: value.length > 48 ? value.slice(0, 47) + "…" : value });
      }
    } else if (typeof value === "number" || typeof value === "boolean") {
      chips.push({ key, value: String(value) });
    } else if (Array.isArray(value)) {
      chips.push({ key, value: `${value.length}` });
    }
    if (chips.length >= max) break;
  }
  return chips;
}

export const stamp = (epoch: number) =>
  new Date(epoch * 1000).toLocaleString(undefined, {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  });

/** "0 9 1 * *" → "monthly on the 1st, 09:00". Covers the shapes a process `opens:` cron
 *  actually takes (a fixed minute/hour on a day-of-month, or an hourly sweep) and falls back
 *  to the raw expression, which is honest rather than wrong. */
export function cronText(cron: string): string {
  const [min, hour, dom, mon, dow] = cron.split(/\s+/);
  const time = `${hour.padStart(2, "0")}:${min.padStart(2, "0")}`;
  if (mon === "*" && dow === "*" && /^\d+$/.test(dom)) {
    const n = Number(dom);
    const ord = n === 1 ? "1st" : n === 2 ? "2nd" : n === 3 ? "3rd" : `${n}th`;
    return `monthly on the ${ord}, ${time}`;
  }
  if (dom === "*" && mon === "*" && dow === "*" && hour === "*") return `hourly at :${min.padStart(2, "0")}`;
  return cron;
}
