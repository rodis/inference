import { useEffect, useState } from "react";
import type { AwareEvent } from "../types";
import { catOf, fmtTime, fmtTimeSec, humanDur, inkOn, labelOf, typeLabel } from "../view";
import LevelChip, { OverrideFlag } from "./LevelChip";

/** A contributor type that floods the tree (a `stay` derives from *dozens* of location pings,
 *  issue #32) collapses into one summary row per type. The rule is structural — any type at or
 *  past this count, not a name list — so the next bulk source is covered without a code change.
 *  The row is evidence ("104 pings back this"), not navigation, so it doesn't drill. */
const COLLAPSE_AT = 6;

interface Props {
  event: AwareEvent | null;
  byId: Record<string, AwareEvent>;
  levelOf: (name: string) => number;
  derivLevel: (e: AwareEvent) => number;
  /** The lane depth puts this type at, when the caller has one — drives the ↑/↓ override flag. */
  defaultOf?: (name: string) => number | null;
  /** 0..1 reveal at the caller's current altitude; lets the header say how much of this
   *  event's lineage is collapsed out of sight on the board behind the modal. */
  revealOf?: (e: AwareEvent) => number;
  onClose: () => void;
}

/** One summary row standing in for a bulk contributor type: count + the span the fixes cover.
 *  A plain div, not a button — there is nothing to drill into that the count doesn't already say. */
function CollapsedRow({ name, events }: { name: string; events: AwareEvent[] }) {
  const cat = catOf(name);
  let from = events[0].date, to = events[0].date;
  for (const ev of events) {
    if (ev.date < from) from = ev.date;
    if (ev.date > to) to = ev.date;
  }
  return (
    <div className="drow dcollapsed" title={`${events.length} contributing events of this type — collapsed (issue #32)`}>
      <span className="dtile" style={{ background: cat.c, color: inkOn(cat.c) }}><cat.Icon size={15} strokeWidth={2.25} /></span>
      <span className="dn">{events.length} {typeLabel(name).toLowerCase()}{events.length === 1 ? "" : "s"}</span>
      <span className="dg" />
      <span className="dt">{fmtTime(from)} – {fmtTime(to)}</span>
    </div>
  );
}

/** One level of the derivation tree: individual rows for the sparse contributors, a summary
 *  row per bulk type. Collapsed rows render after the drillable ones so evidence trails story. */
function DKidList({ kids, byId, levelOf, derivLevel, onOpen }: { kids: AwareEvent[]; byId: Record<string, AwareEvent>; levelOf: (n: string) => number; derivLevel: (e: AwareEvent) => number; onOpen: (e: AwareEvent) => void }) {
  const countByName = new Map<string, number>();
  for (const k of kids) countByName.set(k.name, (countByName.get(k.name) || 0) + 1);
  const shown = kids.filter((k) => (countByName.get(k.name) || 0) < COLLAPSE_AT);
  const bulkNames = [...countByName.keys()].filter((n) => (countByName.get(n) || 0) >= COLLAPSE_AT);
  return (
    <>
      {shown.map((k) => <DNode key={k.id} e={k} byId={byId} levelOf={levelOf} derivLevel={derivLevel} onOpen={onOpen} />)}
      {bulkNames.map((n) => <CollapsedRow key={n} name={n} events={kids.filter((k) => k.name === n)} />)}
    </>
  );
}

/** Recursive lineage node — the derivation tree under an event. Each row is clickable to
 *  refocus the modal on that contributor (drill down into how *it* was built). */
function DNode({ e, byId, levelOf, derivLevel, onOpen }: { e: AwareEvent; byId: Record<string, AwareEvent>; levelOf: (n: string) => number; derivLevel: (e: AwareEvent) => number; onOpen: (e: AwareEvent) => void }) {
  const kids = (e.message.derived_from || []).map((p) => byId[p.id]).filter(Boolean) as AwareEvent[];
  const cat = catOf(e.name);
  return (
    <div className="dnode">
      <button className="drow" onClick={() => onOpen(e)} title="Open this event">
        <span className="dtile" style={{ background: cat.c, color: inkOn(cat.c) }}><cat.Icon size={15} strokeWidth={2.25} /></span>
        <span className="dn">{labelOf(e)}</span>
        <span className="dg" />
        <span className="dt">{fmtTimeSec(e.date)}</span>
        <LevelChip level={levelOf(e.name)} />
        <span className="dbadge">D{derivLevel(e)}</span>
        <span className="dchev">›</span>
      </button>
      {kids.length > 0 && (
        <div className="dkids">
          <DKidList kids={kids} byId={byId} levelOf={levelOf} derivLevel={derivLevel} onOpen={onOpen} />
        </div>
      )}
    </div>
  );
}

export default function EventModal({ event, byId, levelOf, derivLevel, defaultOf, revealOf, onClose }: Props) {
  // a drill trail so contributors can be opened recursively, with a way back up
  const [trail, setTrail] = useState<AwareEvent[]>([]);
  useEffect(() => { setTrail(event ? [event] : []); }, [event]);

  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => { if (ev.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (!event) return <div className="ov" role="dialog" aria-modal="true" aria-label="Event derivation" />;

  const e = trail[trail.length - 1] || event;
  const parent = trail.length > 1 ? trail[trail.length - 2] : null;
  const open = (c: AwareEvent) => setTrail((t) => [...t, c]);
  const back = () => setTrail((t) => (t.length > 1 ? t.slice(0, -1) : t));

  const kids = (e.message.derived_from || []).map((p) => byId[p.id]).filter(Boolean) as AwareEvent[];
  const dl = derivLevel(e);
  const lv = levelOf(e.name);
  const cat = catOf(e.name);
  // how many direct contributors are collapsed below the altitude of the board behind us
  const hiddenBeneath = revealOf
    ? kids.reduce((n, k) => (revealOf(k) < 0.5 ? n + 1 : n), 0)
    : 0;
  // The raw box is a *view*, so a bulk lineage is trimmed here too (full lineage stays in Neon —
  // the copy button copies what's shown, so the note travels with it).
  const df = e.message.derived_from || [];
  const rawMessage = df.length >= COLLAPSE_AT
    ? { ...e.message, derived_from: [...df.slice(0, 3), `… ${df.length - 3} more contributors trimmed for display — the full lineage is in the event store`] }
    : e.message;
  const raw = { id: e.id, name: e.name, event_class: e.event_class, source_app: e.source_app, occurred_epoch: e.occurred_epoch, message: rawMessage };
  const rawJson = JSON.stringify(raw, null, 2);

  return (
    <div className="ov show" role="dialog" aria-modal="true" aria-label="Event derivation"
      onClick={(ev) => { if (ev.target === ev.currentTarget) onClose(); }}>
      <div className="modal">
        <div className="modal-head">
          <button className="x" aria-label="Close" onClick={onClose}>✕</button>
          <div className="htile" style={{ background: cat.c, color: inkOn(cat.c) }}><cat.Icon size={22} strokeWidth={2.25} /></div>
          <div>
            <div className="mlabel">
              {e.event_class === "derived"
                ? "Inference · " + (e.message.inference_type || "weighted_window")
                : "Raw signal · " + (e.source_app || "shortcut")}
            </div>
            <div className="mtitle">{labelOf(e)}</div>
            <div className="mmeta">
              <span className="mt">{fmtTimeSec(e.date)}{e.message.interval ? " · " + humanDur(e.message.interval.duration_seconds) : ""}{
                /* The modal is the one place wide enough for both ends, so it spells out the whole
                   route where the card shows only the informative half (view.ts::routeOf). */
                e.message.journey
                  ? " · " + (e.message.journey.origin?.label ?? "somewhere") + " \u2192 " +
                            (e.message.journey.destination?.label ?? "somewhere")
                  : ""}</span>
              <LevelChip level={lv} />
              {defaultOf && <OverrideFlag level={lv} def={defaultOf(e.name)} />}
              <span className="dbadge">D{dl}</span>
              {hiddenBeneath > 0 && (
                <span className="rollup" title="contributors collapsed below the current altitude — zoom in to see them on the timeline">
                  ↓ {hiddenBeneath} below
                </span>
              )}
            </div>
          </div>
        </div>
        <div className="modal-body">
          {parent && (
            <button className="drillback" onClick={back}>‹ back to {labelOf(parent)}</button>
          )}
          <p className="explain">
            {kids.length ? (
              <>Aware built this from <b>{kids.length}</b> event{kids.length > 1 ? "s" : ""} — derivation level <b>D{dl}</b>. Tap any contributor to see how it was built.</>
            ) : (
              <>A <b>raw signal</b> — derivation level <b>D1</b>. Nothing precedes it; it's what the phone actually sensed.</>
            )}
          </p>
          {kids.length ? (
            <div className="dtree"><DKidList kids={kids} byId={byId} levelOf={levelOf} derivLevel={derivLevel} onOpen={open} /></div>
          ) : (
            <div className="dleaf-note">— end of lineage —</div>
          )}
          <details className="rawbox">
            <summary>
              Raw event JSON
              <button className="copybtn" onClick={(ev) => { ev.preventDefault(); ev.stopPropagation(); navigator.clipboard?.writeText(rawJson); }}>copy</button>
            </summary>
            <pre className="rawjson">{rawJson}</pre>
          </details>
        </div>
      </div>
    </div>
  );
}
