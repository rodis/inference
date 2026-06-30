import { useEffect, useState } from "react";
import type { AwareEvent } from "../types";
import { catOf, fmtTimeSec, humanDur, labelOf, LCHIP } from "../view";

interface Props {
  event: AwareEvent | null;
  byId: Record<string, AwareEvent>;
  getL: (name: string) => number;
  derivLevel: (e: AwareEvent) => number;
  onClose: () => void;
}

function LChip({ lv }: { lv: number }) {
  const c = LCHIP[lv];
  return <span className="lchip" style={{ background: c.bg, color: c.fg }}>L{lv}</span>;
}

/** Recursive lineage node — the derivation tree under an event. Each row is clickable to
 *  refocus the modal on that contributor (drill down into how *it* was built). */
function DNode({ e, byId, getL, derivLevel, onOpen }: { e: AwareEvent; byId: Record<string, AwareEvent>; getL: (n: string) => number; derivLevel: (e: AwareEvent) => number; onOpen: (e: AwareEvent) => void }) {
  const kids = (e.message.derived_from || []).map((p) => byId[p.id]).filter(Boolean) as AwareEvent[];
  const cat = catOf(e.name);
  return (
    <div className="dnode">
      <button className="drow" onClick={() => onOpen(e)} title="Open this event">
        <span className="dtile" style={{ background: cat.c }}><cat.Icon size={15} strokeWidth={2.25} /></span>
        <span className="dn">{labelOf(e)}</span>
        <span className="dg" />
        <span className="dt">{fmtTimeSec(e.date)}</span>
        <LChip lv={getL(e.name)} />
        <span className="dbadge">D{derivLevel(e)}</span>
        <span className="dchev">›</span>
      </button>
      {kids.length > 0 && (
        <div className="dkids">
          {kids.map((k) => <DNode key={k.id} e={k} byId={byId} getL={getL} derivLevel={derivLevel} onOpen={onOpen} />)}
        </div>
      )}
    </div>
  );
}

export default function EventModal({ event, byId, getL, derivLevel, onClose }: Props) {
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
  const conf = e.message.confidence_score;
  const dl = derivLevel(e);
  const cat = catOf(e.name);
  const raw = { id: e.id, name: e.name, event_class: e.event_class, source_app: e.source_app, occurred_epoch: e.occurred_epoch, message: e.message };
  const rawJson = JSON.stringify(raw, null, 2);

  return (
    <div className="ov show" role="dialog" aria-modal="true" aria-label="Event derivation"
      onClick={(ev) => { if (ev.target === ev.currentTarget) onClose(); }}>
      <div className="modal">
        <div className="modal-head">
          <button className="x" aria-label="Close" onClick={onClose}>✕</button>
          <div className="htile" style={{ background: cat.c }}><cat.Icon size={22} strokeWidth={2.25} /></div>
          <div>
            <div className="mlabel">
              {e.event_class === "derived"
                ? "Inference · " + (e.message.inference_type || "weighted_window")
                : "Raw signal · " + (e.source_app || "shortcut")}
            </div>
            <div className="mtitle">{labelOf(e)}</div>
            <div className="mmeta">
              <span className="mt">{fmtTimeSec(e.date)}{e.durationSec != null ? " · " + humanDur(e.durationSec) : ""}</span>
              <LChip lv={getL(e.name)} />
              <span className="dbadge">D{dl}</span>
              {conf != null
                ? <span className="conf">confidence {conf}</span>
                : e.synthetic ? <span className="planned">planned rollup</span> : null}
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
            <div className="dtree">{kids.map((k) => <DNode key={k.id} e={k} byId={byId} getL={getL} derivLevel={derivLevel} onOpen={open} />)}</div>
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
