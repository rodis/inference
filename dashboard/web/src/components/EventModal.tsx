import { useEffect } from "react";
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

/** Recursive lineage node — the derivation tree under an event (mirrors dnodeHTML). */
function DNode({ e, byId, getL, derivLevel }: { e: AwareEvent; byId: Record<string, AwareEvent>; getL: (n: string) => number; derivLevel: (e: AwareEvent) => number }) {
  const kids = (e.message.derived_from || []).map((p) => byId[p.id]).filter(Boolean) as AwareEvent[];
  const cat = catOf(e.name);
  return (
    <div className="dnode">
      <div className="drow">
        <span className="dtile" style={{ background: cat.c }}>{cat.e}</span>
        <span className="dn">{labelOf(e)}</span>
        <span className="dg" />
        <span className="dt">{fmtTimeSec(e.date)}</span>
        <LChip lv={getL(e.name)} />
        <span className="dbadge">D{derivLevel(e)}</span>
      </div>
      {kids.length > 0 && (
        <div className="dkids">
          {kids.map((k) => <DNode key={k.id} e={k} byId={byId} getL={getL} derivLevel={derivLevel} />)}
        </div>
      )}
    </div>
  );
}

export default function EventModal({ event, byId, getL, derivLevel, onClose }: Props) {
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => { if (ev.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (!event) return <div className="ov" role="dialog" aria-modal="true" aria-label="Event derivation" />;

  const e = event;
  const kids = (e.message.derived_from || []).map((p) => byId[p.id]).filter(Boolean) as AwareEvent[];
  const conf = e.message.confidence_score;
  const dl = derivLevel(e);
  const cat = catOf(e.name);

  return (
    <div className="ov show" role="dialog" aria-modal="true" aria-label="Event derivation"
      onClick={(ev) => { if (ev.target === ev.currentTarget) onClose(); }}>
      <div className="modal">
        <div className="modal-head">
          <button className="x" aria-label="Close" onClick={onClose}>✕</button>
          <div className="htile" style={{ background: cat.c }}>{cat.e}</div>
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
          <p className="explain">
            {kids.length ? (
              <>Aware built this from <b>{kids.length}</b> event{kids.length > 1 ? "s" : ""} — derivation level <b>D{dl}</b>. Each was itself built from what's beneath it, down to raw signals.</>
            ) : (
              <>A <b>raw signal</b> — derivation level <b>D1</b>. Nothing precedes it; it's what the phone actually sensed.</>
            )}
          </p>
          {kids.length ? (
            <div className="dtree">{kids.map((k) => <DNode key={k.id} e={k} byId={byId} getL={getL} derivLevel={derivLevel} />)}</div>
          ) : (
            <div className="dleaf-note">— end of lineage —</div>
          )}
        </div>
      </div>
    </div>
  );
}
