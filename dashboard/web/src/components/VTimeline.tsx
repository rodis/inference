import type { AwareEvent } from "../types";
import { ROW, catOf, fmtTime, humanDur, labelOf, LCHIP, isSpan, intervalOf } from "../view";
import type { Scale } from "../view";

interface Props {
  events: AwareEvent[];
  scale?: Scale;                          // time-aligned layout (Compare lanes)
  posOf?: (e: AwareEvent) => number;      // time-proportional per-event layout (Day timeline)
  packedHeight?: number;                  // total height when posOf is used
  getL: (name: string) => number;
  getCeil: (name: string) => number;
  derivLevel: (e: AwareEvent) => number;
  onSelect: (e: AwareEvent) => void;
  /** 0..1 — how "revealed" an event is at the current altitude (1 = full detail). */
  revealOf?: (e: AwareEvent) => number;
  /** lineage lookup, so a visible inference can count detail hidden beneath it. */
  byId?: Record<string, AwareEvent>;
  /** duration events drawn as a proportional capsule (Day timeline): id → {top,height}. */
  spans?: Map<string, { top: number; height: number }>;
  /** collapsed quiet stretches, rendered as labeled dividers (Day timeline). */
  gaps?: { y: number; seconds: number }[];
  /** ids that start while an earlier event is still running (Day timeline) — flagged "overlapping". */
  overlaps?: Set<string>;
}

function LChip({ lv }: { lv: number }) {
  const c = LCHIP[lv];
  return <span className="lchip" style={{ background: c.bg, color: c.fg }}>L{lv}</span>;
}

/** One vertical timeline — absolute-positioned cards with a colored spine between
 *  consecutive events. Two layout modes: a shared time scale (Compare), or a
 *  reveal-weighted packed layout (Day timeline) where slots grow with altitude. */
export default function VTimeline({ events, scale, posOf, packedHeight, getL, getCeil, derivLevel, onSelect, revealOf, byId, spans, gaps, overlaps }: Props) {
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
  // Draw top→down: dayScale places spans by their *start*, so y order ≠ epoch order.
  placed.sort((a, b) => a.y - b.y);

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
      {/* collapsed quiet stretches → a small labeled divider instead of a big blank */}
      {gaps && gaps.map((g, i) => (
        <div key={"gap-" + i} className="vt-gap" style={{ top: g.y }}>
          <span>{humanDur(g.seconds)} quiet</span>
        </div>
      ))}
      {placed.filter((p) => reveal(p.e) > 0.04).map((a, i, vis) => {
        const b = vis[i + 1];
        if (!b) return null;
        // connect consecutive *visible* events, spanning across any collapsed ones between
        // them, so the spine stays continuous through the zoom transition. Skip it under
        // interlocking capsules (the overlap tucks them together — no thread to show).
        if (b.y - a.y < 24) return null;
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
        const iv = isSpan(e) ? intervalOf(e) : null;    // a span (e.g. car_trip) → capsule + range
        const box = spans?.get(e.id);                   // a duration capsule, sized ∝ how long it lasted
        const timeLabel = iv ? fmtTime(new Date(iv.started_at * 1000)) : fmtTime(e.date);
        // Line 1 carries the event's kind ("signal"/"inferred") next to the name, alongside
        // the L/D chips; line 2 (ev-meta) carries the substantive detail only.
        const kind = isDer ? "inferred" : "signal";
        let detail: string;
        if (isDer) {
          const n = (e.message.derived_from || []).length;
          detail = `${n} source${n === 1 ? "" : "s"}`;
        } else {
          detail = e.message.car || e.message.device || "";
        }
        return (
          <div key={e.id} className="vt-card" style={{ top: y, opacity: r, pointerEvents: r < 0.1 ? "none" : undefined }}>
            <div className="vt-time">{timeLabel}</div>
            <div className="vt-circ">
              {box ? (
                <div className="vt-capsule" style={{ background: cat.c, height: box.height }}><cat.Icon size={18} strokeWidth={2.25} /></div>
              ) : (
                <div className="vt-circle" style={{ background: cat.c, transform: `scale(${0.82 + 0.18 * r})` }}><cat.Icon size={18} strokeWidth={2.25} /></div>
              )}
            </div>
            <button className="vt-body" onClick={() => onSelect(e)} tabIndex={r < 0.1 ? -1 : undefined}>
              {overlaps?.has(e.id) && <div className="ev-overlap">⇅ overlapping</div>}
              <div className="ev-head">
                <span className="ev-title">{labelOf(e)}</span>
                <span className="ev-kind">{kind}</span>
                {hidden > 0 && <span className="rollup" title="detail collapsed beneath — descend or tap to expand">↓ {hidden} below</span>}
                {ceil < home && <span className="liftflag">↑ L{ceil}</span>}
                <LChip lv={home} />
                <span className="dbadge">D{derivLevel(e)}</span>
              </div>
              {iv ? (
                <div className="ev-meta"><span className="dur">{humanDur(iv.duration_seconds)}</span>{` · ${fmtTime(new Date(iv.started_at * 1000))}–${fmtTime(new Date(iv.ended_at * 1000))}`}</div>
              ) : detail ? (
                <div className="ev-meta">{detail}</div>
              ) : null}
            </button>
          </div>
        );
      })}
    </div>
  );
}
