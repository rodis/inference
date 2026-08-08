import { Car } from "lucide-react";
import type { AwareEvent } from "../types";
import { carCorroborated, fmtTime, humanDur, intervalOf, isSpan, labelOf, pausesOf, routeOf } from "../view";
import LevelChip, { OverrideFlag } from "./LevelChip";

interface Props {
  event: AwareEvent;
  /** Lane (L) chips are opt-in: pass `level`/`def`/`depth` to get the L / override / D grammar.
   *  The day timeline leaves all three out — that classification lives in the event modal there,
   *  so a day of cards reads as what happened rather than as a wall of taxonomy. */
  level?: number;
  def?: number | null;
  /** Derivation depth of this event — how many layers of inference it stands on. */
  depth?: number;
  /** The span this moment happened inside, when the layout isn't already saying so. */
  hostLabel?: string;
  /** A moment with no containing span — worth flagging, it may be an activity we can't infer yet. */
  orphan?: boolean;
}

/** The text half of an event card: name + chips on line 1, the substantive detail on line 2.
 *  Shared by both lanes of the day timeline, so an activity and a moment can't drift apart in
 *  how they name themselves — the difference between them is weight, not grammar. The L /
 *  override / D chips are supported but unused on the day (see `level`); the modal shows them. */
export default function EventBody({ event: e, level, def = null, depth, hostLabel, orphan }: Props) {
  const iv = isSpan(e) ? intervalOf(e) : null;
  const isDer = e.event_class === "derived";
  const amount = Number(e.message.amount);
  // A journey's route lives here, on the detail line, and never in the title — one place rather
  // than two, so it stays short and can't be half-missing. See view.ts::routeOf.
  const route = routeOf(e);
  // Sub-threshold stops the journey carries ("Avia Neuheim 4m") — enrichment, not events.
  const pauses = pausesOf(e);

  let detail: string;
  if (iv) {
    detail = `${fmtTime(new Date(iv.started_at * 1000))}–${fmtTime(new Date(iv.ended_at * 1000))}`;
  } else if (isDer) {
    const n = (e.message.derived_from || []).length;
    detail = `${n} source${n === 1 ? "" : "s"}`;
  } else {
    detail = String(e.message.car || e.message.device || "");
  }

  return (
    <>
      <div className="ev-head">
        {/* `title=` so a truncated name is still readable on hover; the modal has it in full. */}
        <span className="ev-title" title={labelOf(e)}>{labelOf(e)}</span>
        {/* Your own car was involved — the `vehicle` capability corroborated this journey
            (see view.ts::carCorroborated for why evidence presence, not `confirmed`). */}
        {carCorroborated(e) && (
          <span className="ev-car" title="in your car — corroborated by its signals">
            <Car size={13} aria-label="in your car" />
          </span>
        )}
        {Number.isFinite(amount) && amount > 0 && <span className="ev-amount">CHF {amount.toFixed(2)}</span>}
        {orphan && <span className="orphan" title="not inside any activity we infer">no host</span>}
        {level != null && <LevelChip level={level} />}
        {level != null && <OverrideFlag level={level} def={def} />}
        {depth != null && <span className="dbadge">D{depth}</span>}
      </div>
      {(iv || detail || hostLabel) && (
        <div className="ev-meta">
          {iv && <span className="dur">{humanDur(iv.duration_seconds)}</span>}
          {iv && detail ? ` · ${detail}` : iv ? "" : detail}
          {route && <span className="ev-route"> · {route}</span>}
          {pauses && <span className="ev-route" title="stopped along the way"> · via {pauses}</span>}
          {hostLabel && <span className="ev-host"> · in {hostLabel.toLowerCase()}</span>}
        </div>
      )}
    </>
  );
}

/* A horizontal duration bar lived here (width = duration / the day's longest activity) as the
 * card's one exactly-proportional channel, since the capsule is only roughly proportional. Removed
 * 2026-07-27: nobody could tell what the trough's length meant, and the honest answer made it worse
 * — the reference was "the longest activity today", which is invisible on the card and *changes
 * between days*, so the same 2h39 stay drew full one day and half the next. A ratio against an
 * unstated, shifting quantity is not information. The exact duration is already in `humanDur` text
 * above, and comparison at a glance is the capsule's job (approximately — see `dayLayout`). If it
 * comes back, the scale has to be absolute and self-labelled: a fixed span with hour ticks. */
