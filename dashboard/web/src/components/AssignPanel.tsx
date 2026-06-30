import type { AwareEvent } from "../types";
import { catOf, GROUP_DEFS, groupKey, LCHIP, NLOG, typeLabel } from "../view";

interface Props {
  all: AwareEvent[];
  derivLevel: (e: AwareEvent) => number;
  getL: (name: string) => number;
  getCeil: (name: string) => number;
  onHome: (name: string, level: number) => void;
  onLift: (name: string, level: number) => void;
}

function ARow({ name, derivLevel, getL, getCeil, onHome, onLift, sampleOf }: Props & { name: string; sampleOf: Record<string, AwareEvent> }) {
  const home = getL(name), ceil = getCeil(name), cat = catOf(name);
  const homeBtns = [];
  for (let L = 1; L <= NLOG; L++) {
    const on = home === L, c = LCHIP[L];
    homeBtns.push(
      <button key={"h" + L} type="button" className={on ? "on" : ""}
        style={on ? { background: c.bg, color: c.fg } : undefined}
        onClick={() => onHome(name, L)}>{L}</button>
    );
  }
  let lift = null;
  if (home > 1) {
    const liftBtns = [
      <button key="l-" type="button" className={ceil === home ? "on" : ""} onClick={() => onLift(name, home)}>—</button>,
    ];
    for (let L = 1; L < home; L++) {
      liftBtns.push(
        <button key={"l" + L} type="button" className={ceil === L ? "on" : ""} onClick={() => onLift(name, L)}>L{L}</button>
      );
    }
    lift = <span className="liftgrp"><span className="lgl">up to</span><span className="btns">{liftBtns}</span></span>;
  }
  return (
    <div className="arow">
      <span className="ai" style={{ background: cat.c }}>{cat.e}</span>
      <span className="an">{typeLabel(name)}</span>
      <span className="ad">D{derivLevel(sampleOf[name])}</span>
      <span className="btns">{homeBtns}</span>
      {lift}
    </div>
  );
}

/** "Assign & lift" sidebar — set each event type's home level + lift ceiling. Edits
 *  flow up via onHome/onLift, which persist to Neon (debounced). Mirrors renderAssign. */
export default function AssignPanel(props: Props) {
  const { all, derivLevel } = props;
  const sampleOf: Record<string, AwareEvent> = {};
  all.forEach((e) => { if (!sampleOf[e.name]) sampleOf[e.name] = e; });
  const typeOrder = Object.keys(sampleOf).sort(
    (a, b) => derivLevel(sampleOf[b]) - derivLevel(sampleOf[a]) || a.localeCompare(b)
  );

  return (
    <div className="assign-wrap">
      {GROUP_DEFS.map((g) => {
        const members = typeOrder.filter((n) => groupKey(n) === g.key);
        if (!members.length) return null;
        return (
          <div className="agroup" key={g.key}>
            <div className="agroup-head">
              <span className="gi" style={{ background: g.color }}>{g.icon}</span>
              <span className="gn">{g.label}</span>
              <span className="gc">{members.length}</span>
            </div>
            <div className="agroup-rows">
              {members.map((n) => <ARow key={n} name={n} sampleOf={sampleOf} {...props} />)}
            </div>
          </div>
        );
      })}
    </div>
  );
}
