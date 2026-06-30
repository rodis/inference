import type { AwareEvent } from "../types";
import { ROW, catOf, fmtTime, humanDur, labelOf, LCHIP } from "../view";
import type { Scale } from "../view";

interface Props {
  events: AwareEvent[];
  scale: Scale;
  getL: (name: string) => number;
  getCeil: (name: string) => number;
  derivLevel: (e: AwareEvent) => number;
  onSelect: (e: AwareEvent) => void;
}

function LChip({ lv }: { lv: number }) {
  const c = LCHIP[lv];
  return <span className="lchip" style={{ background: c.bg, color: c.fg }}>L{lv}</span>;
}

/** One vertical timeline — absolute-positioned cards on the shared time scale, with a
 *  colored spine between consecutive events. Mirrors the original renderVT(). */
export default function VTimeline({ events, scale, getL, getCeil, derivLevel, onSelect }: Props) {
  const sorted = [...events].sort((a, b) => a.epoch - b.epoch);
  if (!sorted.length) {
    return <div className="vt" style={{ height: "auto" }}><div className="vt-empty">— nothing here —</div></div>;
  }

  // place Y (de-overlap events sharing a timestamp within this column)
  const placed: { e: AwareEvent; y: number }[] = [];
  let last = -1e9;
  sorted.forEach((e) => {
    let y = scale.y[e.epoch] ?? 0;
    if (y < last + ROW) y = last + ROW;
    placed.push({ e, y });
    last = y;
  });
  const height = Math.max(scale.h, last + ROW);

  return (
    <div className="vt" style={{ height }}>
      {placed.slice(0, -1).map((a, i) => {
        const b = placed[i + 1];
        return (
          <div key={"line-" + a.e.id} className="vt-line"
            style={{ top: a.y + 19, height: b.y - a.y, background: catOf(a.e.name).c }} />
        );
      })}
      {placed.map(({ e, y }) => {
        const cat = catOf(e.name), home = getL(e.name), ceil = getCeil(e.name);
        const isDer = e.event_class === "derived";
        let meta: string;
        if (e.synthetic) meta = `${humanDur(e.durationSec!)} · until ${fmtTime(new Date(e.endEpoch! * 1000))}`;
        else if (isDer) meta = `inferred · D${derivLevel(e)} · ${(e.message.derived_from || []).length} sources`;
        else meta = `signal${e.message.car ? " · " + e.message.car : e.message.device ? " · " + e.message.device : ""}`;
        return (
          <div key={e.id} className="vt-card" style={{ top: y }}>
            <div className="vt-time">{fmtTime(e.date)}</div>
            <div className="vt-circ"><div className="vt-circle" style={{ background: cat.c }}>{cat.e}</div></div>
            <button className="vt-body" onClick={() => onSelect(e)}>
              {e.synthetic ? (
                <div className="ev-meta"><span className="dur">{humanDur(e.durationSec!)}</span>{` · until ${fmtTime(new Date(e.endEpoch! * 1000))}`}</div>
              ) : (
                <div className="ev-meta">{meta}</div>
              )}
              <div className="ev-title">{labelOf(e)}</div>
              <div className="ev-tags">
                {ceil < home && <span className="liftflag">↑ L{ceil}</span>}
                <LChip lv={home} />
                <span className="dbadge">D{derivLevel(e)}</span>
              </div>
            </button>
          </div>
        );
      })}
    </div>
  );
}
