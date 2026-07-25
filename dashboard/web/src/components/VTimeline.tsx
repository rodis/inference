import type { AwareEvent } from "../types";
import { ROW, catOf, fmtTime } from "../view";
import type { Scale } from "../view";
import EventBody from "./EventBody";

interface Props {
  events: AwareEvent[];
  /** Time-aligned layout shared across every Compare lane, so the same moment lines up. */
  scale: Scale;
  levelOf: (name: string) => number;
  defaultOf: (name: string) => number | null;
  derivLevel: (e: AwareEvent) => number;
  onSelect: (e: AwareEvent) => void;
}

/** One narrow vertical lane on a shared time scale — the Compare dashboard's column. Cards are
 *  absolutely positioned with a coloured spine between consecutive events, and de-overlapped by
 *  a minimum row height so a tight cluster stays readable.
 *
 *  The day timeline used to share this component through a second layout mode; it now has its
 *  own two-lane component (`DayTimeline`), which left this one to do just the one job. */
export default function VTimeline({ events, scale, levelOf, defaultOf, derivLevel, onSelect }: Props) {
  const sorted = [...events].sort((a, b) => a.epoch - b.epoch);
  if (!sorted.length) {
    return <div className="vt" style={{ height: "auto" }}><div className="vt-empty">— nothing here —</div></div>;
  }

  const placed: { e: AwareEvent; y: number }[] = [];
  let last = -Infinity;
  for (const e of sorted) {
    const y = Math.max(scale.y[e.epoch] ?? 0, last + ROW);
    placed.push({ e, y });
    last = y;
  }
  const height = Math.max(scale.h, last + ROW);

  return (
    <div className="vt" style={{ height }}>
      {placed.map((a, i) => {
        const b = placed[i + 1];
        if (!b) return null;
        return (
          <div key={"line-" + a.e.id} className="vt-line"
            style={{ top: a.y + 19, height: b.y - a.y, background: catOf(a.e.name).c }} />
        );
      })}
      {placed.map(({ e, y }) => {
        const cat = catOf(e.name);
        return (
          <div key={e.id} className="vt-card" style={{ top: y }}>
            <div className="vt-time">{fmtTime(e.date)}</div>
            <div className="vt-circ">
              <div className="vt-circle" style={{ background: cat.c }}>
                <cat.Icon size={18} strokeWidth={2.25} />
              </div>
            </div>
            <button className="vt-body" onClick={() => onSelect(e)}>
              <EventBody event={e} level={levelOf(e.name)} def={defaultOf(e.name)} depth={derivLevel(e)} />
            </button>
          </div>
        );
      })}
    </div>
  );
}
