import type { AwareEvent } from "../types";
import { fmtTime, humanDur, intervalOf, isSpan, labelOf } from "../view";
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
  /** Tighter type, for the moments lane where rows are half-weight by design. */
  compact?: boolean;
  /** Duration of the day's longest activity (`maxSpanSeconds`). Pass it to draw the duration bar;
   *  omit it (the modal, the moments lane) and the card is just text. */
  durMax?: number;
}

/** The text half of an event card: name + chips on line 1, the substantive detail on line 2.
 *  Shared by both lanes of the day timeline, so an activity and a moment can't drift apart in
 *  how they name themselves — the difference between them is weight, not grammar. The L /
 *  override / D chips are supported but unused on the day (see `level`); the modal shows them. */
export default function EventBody({ event: e, level, def = null, depth, hostLabel, orphan, compact, durMax }: Props) {
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
        {orphan && <span className="orphan" title="not inside any activity we infer">no host</span>}
        {level != null && <LevelChip level={level} />}
        {level != null && <OverrideFlag level={level} def={def} />}
        {depth != null && <span className="dbadge">D{depth}</span>}
      </div>
      {(iv || detail || hostLabel) && (
        <div className="ev-meta">
          {iv && <span className="dur">{humanDur(iv.duration_seconds)}</span>}
          {iv && detail ? ` · ${detail}` : iv ? "" : detail}
          {hostLabel && <span className="ev-host"> · in {hostLabel.toLowerCase()}</span>}
        </div>
      )}
      {iv && !!durMax && <DurationBar seconds={iv.duration_seconds} max={durMax} />}
    </>
  );
}

/** Duration as a **horizontal** bar, `seconds / max` of the track — where `max` is the longest
 *  activity of the day, so the day's own shape is the reference and the longest bar is always full.
 *
 *  This is the card's one exactly-proportional channel, and it exists because the capsule beside it
 *  isn't one. The timeline's vertical scale is deliberately elastic (floored so labels fit, log-
 *  compressed so a lull can't run away — see `dayLayout`), which makes a capsule only *roughly*
 *  proportional, and worst for the longest activity on the board. Horizontal space has no such
 *  constraint: nothing has to fit inside the bar, so it can be linear without a floor big enough to
 *  distort it. Vertical answers *when, and roughly how long*; this answers *exactly how long*.
 *
 *  MIN_W is presence, not proportion: a 3-minute charge next to a 2h39 visit is 1.9% of the track
 *  and would round to a sub-pixel sliver, reading as "no bar" — i.e. as missing data rather than as
 *  a short event. Same bargain as CAP_MIN on the capsule, and it's why the exact figure stays in
 *  text right above. */
const MIN_W = 2;
function DurationBar({ seconds, max }: { seconds: number; max: number }) {
  const pct = Math.max(MIN_W, Math.min(100, (seconds / max) * 100));
  return (
    <div className="ev-bar" aria-hidden="true"
      title={`${humanDur(seconds)} — ${Math.round((seconds / max) * 100)}% of the day's longest activity`}>
      <i style={{ width: `${pct}%` }} />
    </div>
  );
}
