import type { AwareEvent } from "../types";
import { fmtTime, humanDur, intervalOf, isSpan, labelOf } from "../view";
import LevelChip, { OverrideFlag } from "./LevelChip";

interface Props {
  event: AwareEvent;
  level: number;
  def: number | null;
  /** Derivation depth of this event — how many layers of inference it stands on. */
  depth: number;
  /** How many of this event's direct contributors are collapsed below the current altitude. */
  hiddenBeneath?: number;
  /** The span this moment happened inside, when the layout isn't already saying so. */
  hostLabel?: string;
  /** A moment with no containing span — worth flagging, it may be an activity we can't infer yet. */
  orphan?: boolean;
  /** Tighter type, for the moments lane where rows are half-weight by design. */
  compact?: boolean;
}

/** The text half of an event card: name + classification chips on line 1, the substantive
 *  detail on line 2. Shared by the Compare lanes and both lanes of the day timeline, so the
 *  L / override / D chip grammar can't drift between them. */
export default function EventBody({ event: e, level, def, depth, hiddenBeneath = 0, hostLabel, orphan, compact }: Props) {
  const iv = isSpan(e) ? intervalOf(e) : null;
  const isDer = e.event_class === "derived";
  const amount = Number(e.message.amount);

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
        <span className="ev-title">{labelOf(e)}</span>
        {Number.isFinite(amount) && amount > 0 && <span className="ev-amount">CHF {amount.toFixed(2)}</span>}
        {!compact && <span className="ev-kind">{isDer ? "inferred" : "signal"}</span>}
        {hiddenBeneath > 0 && (
          <span className="rollup" title="detail collapsed beneath — descend or tap to expand">
            ↓ {hiddenBeneath} below
          </span>
        )}
        {orphan && <span className="orphan" title="not inside any activity we infer">no host</span>}
        <LevelChip level={level} />
        <OverrideFlag level={level} def={def} />
        <span className="dbadge">D{depth}</span>
      </div>
      {(iv || detail || hostLabel) && (
        <div className="ev-meta">
          {iv && <span className="dur">{humanDur(iv.duration_seconds)}</span>}
          {iv && detail ? ` · ${detail}` : iv ? "" : detail}
          {hostLabel && <span className="ev-host"> · in {hostLabel.toLowerCase()}</span>}
        </div>
      )}
    </>
  );
}
