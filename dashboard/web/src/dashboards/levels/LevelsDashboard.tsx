import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useAware } from "../../app/useAware";
import { catOf, LANE_BLURB, laneNames, typeLabel } from "../../view";
import LevelChip, { OverrideFlag } from "../../components/LevelChip";

/** A drop target: a lane number, or "off" for the tray. */
type Zone = number | "off";

const DRAG_SLOP = 5;    // px of movement before a press becomes a drag rather than a tap

/** The levels board — where each event *type* lives on the timeline's altitude ladder.
 *
 *  The ladder is as tall as the deepest inference in view and a type's derivation depth
 *  picks its lane, so the only thing this page collects is the exceptions: drag a type up
 *  when it matters more than its depth suggests (a card payment is D1 and a headline), down
 *  when it matters less, or out of the stack entirely when it shouldn't be drawn at all.
 *  An untouched type stores nothing, which is what lets its lane follow the definitions as
 *  they change instead of freezing at whatever it was the day you first saved.
 *
 *  This replaced the "Assign & lift" sidebar, which asked for a home lane *and* a ceiling
 *  per type — two numbers where only the ceiling ever changed what rendered. */
export default function LevelsDashboard() {
  const {
    prepared, lanes, levelOf, defaultOf, isHidden, overrides, configured,
    setLevel, setHidden, resetLevel, resetAll, status, userId,
  } = useAware();
  const { types, depthOf, depthsOf, maxDepth } = prepared;

  const [altitude, setAltitude] = useState(1);
  const boardRef = useRef<HTMLDivElement>(null);

  // Every type the board must show: seen in the window, plus any that is only *configured*
  // (hidden or overridden types keep their row even after their events age out of the
  // window, or the setting would be stranded with no way to undo it).
  const rows = useMemo(() => {
    const all = [...new Set([...types, ...configured])];
    return all.sort((a, b) => (depthOf(b) ?? 0) - (depthOf(a) ?? 0) || typeLabel(a).localeCompare(typeLabel(b)));
  }, [types, configured, depthOf]);

  const names = useMemo(() => laneNames(lanes), [lanes]);
  const live = useMemo(() => rows.filter((n) => !isHidden(n)), [rows, isHidden]);
  const parked = useMemo(() => rows.filter((n) => isHidden(n)), [rows, isHidden]);

  // --- moves ---------------------------------------------------------------------
  const [lastMoved, setLastMoved] = useState<string | null>(null);
  const move = useCallback((name: string, zone: Zone) => {
    if (zone === "off") setHidden(name, true);
    else setLevel(name, zone);
    setLastMoved(name);
  }, [setHidden, setLevel]);

  // Keep focus on the token you just moved (and flash it), so a keyboard run of several
  // moves doesn't dump focus back to the top of the document on each re-render.
  useLayoutEffect(() => {
    if (!lastMoved) return;
    const el = boardRef.current?.querySelector<HTMLElement>(`[data-name="${CSS.escape(lastMoved)}"]`);
    el?.focus({ preventScroll: true });
    el?.classList.remove("landed");
    // reflow between remove and add, or an unchanged class list replays nothing
    void el?.offsetWidth;
    el?.classList.add("landed");
    setLastMoved(null);
  }, [lastMoved]);

  const onKeyDown = useCallback((e: React.KeyboardEvent, name: string) => {
    const cur = isHidden(name) ? lanes + 1 : levelOf(name);
    if (e.key === "ArrowUp") { e.preventDefault(); move(name, Math.max(1, Math.min(lanes, cur - 1))); }
    else if (e.key === "ArrowDown") { e.preventDefault(); move(name, cur >= lanes ? "off" : cur + 1); }
    else if (e.key === "Backspace" || e.key === "Delete") { e.preventDefault(); move(name, "off"); }
    else if (e.key === "Enter") { e.preventDefault(); resetLevel(name); setLastMoved(name); }
  }, [isHidden, lanes, levelOf, move, resetLevel]);

  // --- drag ----------------------------------------------------------------------
  // Pointer events rather than HTML5 drag-and-drop: one code path covers trackpad, touch
  // and pen, and the dragged token is a plain absolutely-positioned clone we can style.
  const press = useRef<{ name: string; x0: number; y0: number; dx: number; dy: number; w: number; moved: boolean } | null>(null);
  const [dragName, setDragName] = useState<string | null>(null);
  const [dragAt, setDragAt] = useState<{ x: number; y: number; w: number } | null>(null);
  const [overZone, setOverZone] = useState<Zone | null>(null);

  const endDrag = useCallback(() => {
    press.current = null;
    setDragName(null); setDragAt(null); setOverZone(null);
  }, []);

  const onPointerDown = (e: React.PointerEvent<HTMLButtonElement>, name: string) => {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    const r = e.currentTarget.getBoundingClientRect();
    press.current = {
      name, x0: e.clientX, y0: e.clientY,
      dx: e.clientX - r.left, dy: e.clientY - r.top, w: r.width, moved: false,
    };
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  const onPointerMove = (e: React.PointerEvent<HTMLButtonElement>) => {
    const p = press.current;
    if (!p) return;
    if (!p.moved) {
      if (Math.hypot(e.clientX - p.x0, e.clientY - p.y0) < DRAG_SLOP) return;
      p.moved = true;
      setDragName(p.name);
    }
    setDragAt({ x: e.clientX - p.dx, y: e.clientY - p.dy, w: p.w });
    // the floating clone sits in a pointer-events:none layer, so this reads the board
    const zone = document.elementFromPoint(e.clientX, e.clientY)?.closest<HTMLElement>("[data-zone]");
    const z = zone?.dataset.zone;
    setOverZone(z == null ? null : z === "off" ? "off" : Number(z));
  };

  const onPointerUp = () => {
    const p = press.current;
    if (p?.moved && overZone != null) move(p.name, overZone);
    endDrag();
  };

  // --- what the timeline shows at the previewed altitude --------------------------
  const inView = useMemo(
    () => live.filter((n) => levelOf(n) <= altitude).sort((a, b) => levelOf(a) - levelOf(b) || typeLabel(a).localeCompare(typeLabel(b))),
    [live, levelOf, altitude]
  );
  const below = live.length - inView.length;

  if (status) return <div className="statusline">{status}</div>;

  const token = (name: string) => {
    const cat = catOf(name), lv = levelOf(name), def = defaultOf(name);
    const depth = depthOf(name), seen = depth != null;
    const ds = depthsOf(name);
    const dir = def == null || lv === def ? "" : lv < def ? " up" : " down";
    return (
      <button
        key={name}
        type="button"
        data-name={name}
        className={"tok" + dir + (seen ? "" : " unseen") + (dragName === name ? " dragging" : "")}
        aria-label={`${typeLabel(name)} — level ${lv}${dir ? ", moved off its default" : ""}`}
        onPointerDown={(e) => onPointerDown(e, name)}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={endDrag}
        onKeyDown={(e) => onKeyDown(e, name)}
      >
        <span className="ti" style={{ background: cat.c }}><cat.Icon size={14} strokeWidth={2.25} /></span>
        <span className="tn">{typeLabel(name)}</span>
        <span className="dbadge" title={seen
          ? (ds.length > 1
              ? `derivation depth D${depth} as of the latest event — older ones in view: ${ds.map((d) => "D" + d).join(", ")}`
              : `derivation depth D${depth}`)
          : "defined or configured, but no events in this window — no depth to default from"}>
          {seen ? `D${depth}` : "unseen"}
        </span>
        <OverrideFlag level={lv} def={def} />
      </button>
    );
  };

  return (
    <>
      <div className="pagehead">
        <div className="eyebrow">altitude · one lane per level of abstraction</div>
        <h1 className="ptitle">Where each event type lives</h1>
        <p className="page-intro">
          The ladder is as tall as the deepest inference in view (<b>D{maxDepth}</b> today, so{" "}
          <b>{lanes} lanes</b>), and a type's <b>derivation depth</b> picks its lane — deeper
          reasoning sits higher. Drag a type to overrule that, or out of the stack to keep it off
          the timeline entirely. Only the exceptions are saved.
        </p>
      </div>

      <div className="cols">
        <div className="col-main">
          <div className="sheet board" ref={boardRef}>
            <div className="toolbar">
              <div className="ctl">
                <span>Preview altitude</span>
                <div className="seg" role="group" aria-label="preview altitude">
                  {names.map((_, i) => (
                    <button key={i} type="button" aria-pressed={altitude === i + 1}
                      onClick={() => setAltitude(i + 1)}>L{i + 1}</button>
                  ))}
                </div>
              </div>
              <div className="ctl">
                <span>{overrides === 1 ? "1 override" : `${overrides} overrides`}</span>
                <button type="button" className="linkbtn" disabled={!overrides} onClick={resetAll}>
                  Reset all to depth
                </button>
              </div>
            </div>

            <div className="lanes">
              {names.map((laneName, i) => {
                const L = i + 1;
                const here = live.filter((n) => levelOf(n) === L);
                // a ghost marks where an override came *from*, so the displacement is visible
                const ghosts = live.filter((n) => defaultOf(n) === L && levelOf(n) !== L);
                const shown = L <= altitude;
                return (
                  <div className={`lane l${Math.min(L, 4)}` + (shown ? "" : " out")} key={L}>
                    <div className="lane-rail">
                      <span className="lnum"><LevelChip level={L} /><span className="lname">{laneName}</span></span>
                      <span className="lsub">{LANE_BLURB[laneName] ?? ""}</span>
                      <span className={"lsub" + (shown ? " inview" : "")}>
                        {shown ? "◂ in view" : `hidden at L${altitude}`}
                      </span>
                    </div>
                    <div className={"drop" + (overZone === L ? " over" : "")} data-zone={L}>
                      {here.map(token)}
                      {ghosts.map((n) => {
                        const cat = catOf(n);
                        return (
                          <span className={"ghost" + (dragName === n ? " lit" : "")} key={"g-" + n}>
                            <span className="ti" style={{ background: cat.c }}><cat.Icon size={14} strokeWidth={2.25} /></span>
                            <span className="gt">{typeLabel(n)} · default</span>
                          </span>
                        );
                      })}
                      {!here.length && !ghosts.length && <span className="empty">nothing lives here</span>}
                    </div>
                  </div>
                );
              })}
            </div>

            <div className="tray">
              <div className="tray-rail">
                <span className="lname">Off the timeline</span>
                <span className="lsub">still stored &amp; queryable</span>
              </div>
              <div className={"drop" + (overZone === "off" ? " over" : "")} data-zone="off">
                {parked.map(token)}
                {!parked.length && <span className="empty">drag a type here to keep it out of every altitude</span>}
              </div>
            </div>
          </div>
        </div>

        <div className="col-side">
          <div className="panel">
            <h3>In view at L{altitude}</h3>
            <div className="psub">
              {inView.length} of {live.length} types
              {parked.length ? ` · ${parked.length} dropped` : ""}
            </div>
            <div className="preview">
              {inView.map((n) => {
                const cat = catOf(n), lv = levelOf(n), def = defaultOf(n);
                return (
                  <div className={"prow" + (def != null && lv < def ? " lifted" : "")} key={n}>
                    <span className="pd" style={{ background: cat.c }} />
                    <span className="pn">{typeLabel(n)}</span>
                    <span className="pl">{def != null && lv < def ? "↑ " : ""}L{lv}</span>
                  </div>
                );
              })}
              {!inView.length && <span className="pempty">nothing at this altitude</span>}
              {below > 0 && <div className="pdivide">{below} more, deeper down</div>}
            </div>
          </div>

          <div className="panel">
            <h3>Without a mouse</h3>
            <div className="psub">focus a type, then</div>
            <div className="keys">
              <div><kbd>↑</kbd><span>promote one lane</span></div>
              <div><kbd>↓</kbd><span>demote one lane</span></div>
              <div><kbd>⌫</kbd><span>drop off the timeline</span></div>
              <div><kbd>↵</kbd><span>back to the depth default</span></div>
            </div>
          </div>
        </div>
      </div>

      {/* the dragged token, following the pointer in a layer that ignores hit-testing */}
      {dragName && dragAt && (
        <div className="draglayer">
          <div className="tok floating" style={{ left: dragAt.x, top: dragAt.y, width: dragAt.w }}>
            <span className="ti" style={{ background: catOf(dragName).c }}>
              {(() => { const C = catOf(dragName).Icon; return <C size={14} strokeWidth={2.25} />; })()}
            </span>
            <span className="tn">{typeLabel(dragName)}</span>
          </div>
        </div>
      )}

      <footer>
        <b>Levels</b> — saved per user in Neon (<b>{userId}</b>), as a sparse override map plus a
        hidden list. A type sitting where its depth puts it stores nothing at all, so defaults keep
        following the event definitions in <b>events/</b> as those change.
        Depth (<b>D</b>) is structural — how many layers of inference an event stands on.
        Level (<b>L</b>) is how much you care. They agree by default and part company on purpose.
      </footer>
    </>
  );
}
