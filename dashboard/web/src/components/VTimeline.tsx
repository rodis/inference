import type { AwareEvent } from "../types";
import { ROW, catOf, fmtTime, humanDur, labelOf, LCHIP } from "../view";
import type { Scale } from "../view";

interface Props {
  events: AwareEvent[];
  scale?: Scale;                          // time-aligned layout (Compare lanes)
  posOf?: (e: AwareEvent) => number;      // reveal-weighted per-event layout (Day timeline)
  packedHeight?: number;                  // total height when posOf is used
  getL: (name: string) => number;
  getCeil: (name: string) => number;
  derivLevel: (e: AwareEvent) => number;
  onSelect: (e: AwareEvent) => void;
  /** 0..1 — how "revealed" an event is at the current altitude (1 = full detail). */
  revealOf?: (e: AwareEvent) => number;
  /** lineage lookup, so a visible inference can count detail hidden beneath it. */
  byId?: Record<string, AwareEvent>;
}

function LChip({ lv }: { lv: number }) {
  const c = LCHIP[lv];
  return <span className="lchip" style={{ background: c.bg, color: c.fg }}>L{lv}</span>;
}

/** One vertical timeline — absolute-positioned cards with a colored spine between
 *  consecutive events. Two layout modes: a shared time scale (Compare), or a
 *  reveal-weighted packed layout (Day timeline) where slots grow with altitude. */
export default function VTimeline({ events, scale, posOf, packedHeight, getL, getCeil, derivLevel, onSelect, revealOf, byId }: Props) {
  const reveal = revealOf ?? (() => 1);
  const sorted = [...events].sort((a, b) => a.epoch - b.epoch);
  if (!sorted.length) {
    return <div className="vt" style={{ height: "auto" }}><div className="vt-empty">— nothing here —</div></div>;
  }

  // placement: packed (per-event, reveal-weighted) or time-scale (with row de-overlap)
  let placed: { e: AwareEvent; y: number }[];
  let height: number;
  if (posOf) {
    placed = sorted.map((e) => ({ e, y: posOf(e) }));
    height = packedHeight ?? Math.max(...placed.map((p) => p.y)) + ROW;
  } else {
    const sc = scale!;
    placed = [];
    let last = -1e9;
    sorted.forEach((e) => {
      let y = sc.y[e.epoch] ?? 0;
      if (y < last + ROW) y = last + ROW;
      placed.push({ e, y });
      last = y;
    });
    height = Math.max(sc.h, last + ROW);
  }

  // how many direct contributors of `e` are currently collapsed (below altitude)
  const hiddenBeneath = (e: AwareEvent): number => {
    if (!byId) return 0;
    return (e.message.derived_from || []).reduce((n, p) => {
      const child = byId[p.id];
      return child && reveal(child) < 0.5 ? n + 1 : n;
    }, 0);
  };

  return (
    <div className="vt" style={{ height }}>
      {placed.filter((p) => reveal(p.e) > 0.04).map((a, i, vis) => {
        const b = vis[i + 1];
        if (!b) return null;
        // connect consecutive *visible* events, spanning across any collapsed ones between
        // them, so the spine stays continuous through the zoom transition.
        const op = Math.min(reveal(a.e), reveal(b.e));
        return (
          <div key={"line-" + a.e.id} className="vt-line"
            style={{ top: a.y + 19, height: b.y - a.y, background: catOf(a.e.name).c, opacity: op }} />
        );
      })}
      {placed.map(({ e, y }) => {
        const cat = catOf(e.name), home = getL(e.name), ceil = getCeil(e.name);
        const isDer = e.event_class === "derived";
        const r = reveal(e);
        const hidden = hiddenBeneath(e);
        let meta: string;
        if (e.synthetic) meta = `${humanDur(e.durationSec!)} · until ${fmtTime(new Date(e.endEpoch! * 1000))}`;
        else if (isDer) meta = `inferred · D${derivLevel(e)} · ${(e.message.derived_from || []).length} sources`;
        else meta = `signal${e.message.car ? " · " + e.message.car : e.message.device ? " · " + e.message.device : ""}`;
        return (
          <div key={e.id} className="vt-card" style={{ top: y, opacity: r, pointerEvents: r < 0.1 ? "none" : undefined }}>
            <div className="vt-time">{fmtTime(e.date)}</div>
            <div className="vt-circ">
              <div className="vt-circle" style={{ background: cat.c, transform: `scale(${0.82 + 0.18 * r})` }}>{cat.e}</div>
            </div>
            <button className="vt-body" onClick={() => onSelect(e)} tabIndex={r < 0.1 ? -1 : undefined}>
              {e.synthetic ? (
                <div className="ev-meta"><span className="dur">{humanDur(e.durationSec!)}</span>{` · until ${fmtTime(new Date(e.endEpoch! * 1000))}`}</div>
              ) : (
                <div className="ev-meta">{meta}</div>
              )}
              <div className="ev-title">{labelOf(e)}</div>
              <div className="ev-tags">
                {hidden > 0 && <span className="rollup" title="detail collapsed beneath — descend or tap to expand">↓ {hidden} below</span>}
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
