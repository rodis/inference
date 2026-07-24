import { useState } from "react";
import { ChevronRight } from "lucide-react";
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

function ARow({ name, derivLevel, getL, getCeil, onHome, onLift, sampleOf, depthsOf }: Props & { name: string; sampleOf: Record<string, AwareEvent>; depthsOf: Record<string, Set<number>> }) {
  const home = getL(name), ceil = getCeil(name), cat = catOf(name);
  const depth = derivLevel(sampleOf[name]);
  const seen = depthsOf[name];
  // A type's depth is a property of its *instances*, and it changes when a definition
  // changes shape, so the loaded window can hold more than one. The badge reports the
  // current shape (newest instance); the tooltip owns up to the older ones.
  const depthTitle = seen && seen.size > 1
    ? `derivation depth D${depth} as of the latest event — older ones in view: ${[...seen].sort().map((d) => "D" + d).join(", ")}`
    : `derivation depth D${depth}`;
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
      <span className="ai" style={{ background: cat.c }}><cat.Icon size={14} strokeWidth={2.25} /></span>
      <span className="an">{typeLabel(name)}</span>
      <span className="ad" title={depthTitle}>D{depth}</span>
      <span className="btns">{homeBtns}</span>
      {lift}
    </div>
  );
}

/** "Assign & lift" sidebar — set each event type's home level + lift ceiling. Edits
 *  flow up via onHome/onLift, which persist to Neon (debounced). Mirrors renderAssign. */
export default function AssignPanel(props: Props) {
  const { all, derivLevel } = props;

  // One representative event per type — it's what the D badge and the depth sort read.
  // `all` is ascending, so last-write-wins picks the **newest** instance. Taking the oldest
  // (what this did before) pinned each badge to the most obsolete lineage in the window:
  // car_trip used to be built on the intermediate car_door_opened/closed derivations (D4),
  // and reads directly off got_into/got_out since ADR 0005 (D3) — so the panel said D4
  // while every trip on the timeline was D3. Depth follows the definitions, which change.
  const sampleOf: Record<string, AwareEvent> = {};
  const depthsOf: Record<string, Set<number>> = {};
  all.forEach((e) => {
    sampleOf[e.name] = e;
    (depthsOf[e.name] ??= new Set<number>()).add(derivLevel(e));
  });
  const typeOrder = Object.keys(sampleOf).sort(
    (a, b) => derivLevel(sampleOf[b]) - derivLevel(sampleOf[a]) || a.localeCompare(b)
  );

  // Per-group fold state so the panel stays bounded as more event types accrue: a group
  // whose key is in the set is collapsed to its header. Default is collapsed (all groups
  // folded), so the panel opens compact and the user expands the categories they care about.
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set(GROUP_DEFS.map((g) => g.key)));
  const toggle = (key: string) => setCollapsed((prev) => {
    const next = new Set(prev);
    next.has(key) ? next.delete(key) : next.add(key);
    return next;
  });

  return (
    <div className="assign-wrap">
      {GROUP_DEFS.map((g) => {
        const members = typeOrder.filter((n) => groupKey(n) === g.key);
        if (!members.length) return null;
        const isCollapsed = collapsed.has(g.key);
        return (
          <div className={"agroup" + (isCollapsed ? " collapsed" : "")} key={g.key}>
            <button type="button" className="agroup-head" aria-expanded={!isCollapsed} onClick={() => toggle(g.key)}>
              <ChevronRight className="gchev" size={14} strokeWidth={2.5} />
              <span className="gi" style={{ background: g.color }}><g.Icon size={13} strokeWidth={2.25} /></span>
              <span className="gn">{g.label}</span>
              <span className="gc">{members.length}</span>
            </button>
            {!isCollapsed && (
              <div className="agroup-rows">
                {members.map((n) => <ARow key={n} name={n} sampleOf={sampleOf} depthsOf={depthsOf} {...props} />)}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
