import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useAware } from "../../app/useAware";
import type { AwareEvent } from "../../types";
import { dayKey, packScale } from "../../view";
import VTimeline from "../../components/VTimeline";
import WeekStrip from "../../components/WeekStrip";
import AssignPanel from "../../components/AssignPanel";
import EventModal from "../../components/EventModal";

const MON = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const ALT_NAMES: Record<number, string> = { 1: "headlines", 2: "activity", 3: "micro", 4: "signals" };
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

/** The day at a glance: one altitude-zoomed timeline. Altitude is driven by a pinch /
 *  ⌘-scroll gesture *anchored at the point you're looking at* (the focused event stays put
 *  while detail grows/collapses around it), plus a fixed +/- control for discoverability. */
export default function TimelineDashboard() {
  const { prepared, getL, getCeil, onHome, onLift, status, eventsCount, userId, selectedDay } = useAware();
  const { all, byId, raw, derived, derivLevel } = prepared;

  const [altitude, setAltitude] = useState<number>(1); // 1 = headlines (high) … 4 = signals (ground)
  const [modalEvent, setModalEvent] = useState<AwareEvent | null>(null);

  const revealOf = useCallback((e: AwareEvent) => {
    const displayLevel = getCeil(e.name);
    return Math.max(0, Math.min(1, altitude - displayLevel + 1));
  }, [altitude, getCeil]);

  // All of the day's events stay in the layout; packScale grows each event's slot with its
  // reveal, so lower-layer events grow in/out instead of popping the layout.
  const dayAll = useMemo(() => all.filter((e) => dayKey(e.date) === selectedDay), [all, selectedDay]);
  const packed = useMemo(() => packScale(dayAll, revealOf), [dayAll, revealOf]);
  const shownCount = useMemo(() => dayAll.reduce((n, e) => (revealOf(e) > 0.5 ? n + 1 : n), 0), [dayAll, revealOf]);

  // --- anchored zoom plumbing -------------------------------------------------
  // refs let the once-attached gesture listeners read current layout without re-binding.
  const wrapRef = useRef<HTMLDivElement>(null);
  const altitudeRef = useRef(altitude); altitudeRef.current = altitude;
  const dayAllRef = useRef(dayAll); dayAllRef.current = dayAll;
  const packedRef = useRef(packed); packedRef.current = packed;
  const pendingAnchor = useRef<{ id: string; oldY: number } | null>(null);

  // Set the new altitude, remembering which on-screen event to keep stationary: the
  // visible event nearest the gesture focus that will still be visible afterwards.
  const applyAltitude = useCallback((rawNext: number, clientY: number) => {
    const cur = altitudeRef.current;
    const next = clamp(rawNext, 1, 4);
    if (Math.abs(next - cur) < 0.001) return;
    const wrap = wrapRef.current;
    if (wrap && dayAllRef.current.length) {
      const wrapTop = wrap.getBoundingClientRect().top;
      const pos = packedRef.current.pos;
      let best: AwareEvent | null = null, bestD = Infinity;
      for (const ev of dayAllRef.current) {
        const targetReveal = clamp(next - getCeil(ev.name) + 1, 0, 1);
        if (targetReveal < 0.4) continue; // anchor to something that stays visible
        const y = pos.get(ev.id) ?? 0;
        const d = Math.abs(wrapTop + y - clientY);
        if (d < bestD) { bestD = d; best = ev; }
      }
      pendingAnchor.current = best ? { id: best.id, oldY: pos.get(best.id) ?? 0 } : null;
    }
    altitudeRef.current = next;
    setAltitude(next);
  }, [getCeil]);

  // After re-layout, scroll so the anchored event stays where it was (transition-safe:
  // computed from the scale, not mid-animation DOM measurement).
  useLayoutEffect(() => {
    const a = pendingAnchor.current;
    pendingAnchor.current = null;
    if (!a) return;
    const newY = packed.pos.get(a.id);
    if (newY == null) return;
    const delta = newY - a.oldY;
    if (!delta) return;
    // During a gesture, snap instantly so the anchor stays glued to the fingers. For a
    // discrete +/- step, scroll smoothly so it animates in step with the card glide.
    const wrap = wrapRef.current;
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    const smooth = !reduce && !!wrap && !wrap.classList.contains("zooming");
    window.scrollBy({ top: delta, behavior: smooth ? "smooth" : "auto" });
  }, [altitude, packed]);

  // Pinch (trackpad → ctrl+wheel) and touch-pinch on the timeline. Plain scroll passes
  // through so the page still scrolls normally. Gesture input is coalesced to one update
  // per animation frame, and cards track instantly (no transition) while gesturing — so it
  // feels 1:1 with your fingers. Discrete +/- button steps keep their glide.
  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    let pendingTarget: number | null = null;  // absolute altitude to settle on this frame
    let lastY = 0, raf = 0, idle = 0;
    const flush = () => {
      raf = 0;
      if (pendingTarget != null) { applyAltitude(pendingTarget, lastY); pendingTarget = null; }
    };
    const schedule = () => { if (!raf) raf = requestAnimationFrame(flush); };
    const markZoom = () => { wrap.classList.add("zooming"); clearTimeout(idle); idle = window.setTimeout(() => wrap.classList.remove("zooming"), 160); };

    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey) return;
      e.preventDefault();
      const base = pendingTarget ?? altitudeRef.current;
      pendingTarget = base - e.deltaY * 0.008;
      lastY = e.clientY;
      markZoom();
      schedule();
    };
    const dist = (t: TouchList) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
    let pinch: { d: number; alt: number } | null = null;
    const onTouchStart = (e: TouchEvent) => { if (e.touches.length === 2) { pinch = { d: dist(e.touches), alt: altitudeRef.current }; wrap.classList.add("zooming"); } };
    const onTouchMove = (e: TouchEvent) => {
      if (e.touches.length === 2 && pinch) {
        e.preventDefault();
        lastY = (e.touches[0].clientY + e.touches[1].clientY) / 2;
        pendingTarget = pinch.alt + (dist(e.touches) / pinch.d - 1) * 1.5;
        schedule();
      }
    };
    const onTouchEnd = (e: TouchEvent) => { if (e.touches.length < 2) { pinch = null; wrap.classList.remove("zooming"); } };

    wrap.addEventListener("wheel", onWheel, { passive: false });
    wrap.addEventListener("touchstart", onTouchStart, { passive: false });
    wrap.addEventListener("touchmove", onTouchMove, { passive: false });
    wrap.addEventListener("touchend", onTouchEnd);
    return () => {
      if (raf) cancelAnimationFrame(raf);
      clearTimeout(idle);
      wrap.removeEventListener("wheel", onWheel);
      wrap.removeEventListener("touchstart", onTouchStart);
      wrap.removeEventListener("touchmove", onTouchMove);
      wrap.removeEventListener("touchend", onTouchEnd);
    };
  }, [applyAltitude]);

  const zoomStep = (d: number) => applyAltitude(Math.round(altitudeRef.current) + d, window.innerHeight / 2);

  const summary = useMemo(() => {
    if (!all.length) return null;
    const epochs = all.map((e) => e.epoch);
    const t0 = Math.min(...epochs), t1 = Math.max(...epochs);
    const h = (t1 - t0) / 3600;
    const span = h >= 1 ? `${h.toFixed(1)} h` : `${Math.round((t1 - t0) / 60)} min`;
    const deepest = Math.max(...all.map((e) => derivLevel(e)));
    return { signals: raw.length, inferences: derived.length, deepest, span };
  }, [all, raw, derived, derivLevel]);

  if (status) return <div className="statusline">{status}</div>;

  const dh = selectedDay ? new Date(selectedDay + "T00:00:00") : null;
  const altL = Math.round(altitude);

  return (
    <>
      {dh && (
        <div className="datehead">
          <span className="dnum">{dh.getDate()}.</span> <span className="dmon">{MON[dh.getMonth()]}</span>{" "}
          <span className="dyear">{dh.getFullYear()}</span> <span className="chev">›</span>
        </div>
      )}

      <WeekStrip />

      {summary && (
        <div className="summary">
          <Pill v={summary.signals} k="signals" />
          <Pill v={summary.inferences} k="inferences" accent />
          <Pill v={"D" + summary.deepest} k="deepest" accent />
          <Pill v={summary.span} k="span" />
        </div>
      )}

      <div className="cols">
        <div className="col-main">
          <div className="sheet">
            <div className="handle" />
            <div className="sheet-head">
              <span className="stitle">Timeline</span>
              <span className="zoom-hint">pinch or ⌘-scroll on the timeline to zoom · {shownCount} shown</span>
            </div>
            <div className="vtwrap" ref={wrapRef}>
              <VTimeline events={dayAll} posOf={(e) => packed.pos.get(e.id) ?? 0} packedHeight={packed.h} getL={getL} getCeil={getCeil} derivLevel={derivLevel} onSelect={setModalEvent} revealOf={revealOf} byId={byId} />
            </div>
          </div>
        </div>

        <div className="col-side">
          <div className="card-box">
            <div className="assign-head">
              <span className="ah-title">Assign &amp; lift</span>
              <span className="ah-hint">level = home lane · “also up to” lifts an event into higher views. Persisted per user in Neon.</span>
            </div>
            <AssignPanel all={all} derivLevel={derivLevel} getL={getL} getCeil={getCeil} onHome={onHome} onLift={onLift} />
          </div>
        </div>
      </div>

      <footer>
        <b>Aware</b> — from the <b>events</b> table in Neon (Postgres): {eventsCount} events for <b>{userId}</b>.
        Raw signals come from iPhone Shortcuts via Vector → Kafka; inferences from the runtime, each with a <b>derivation lineage</b>.
        Zoom anchors on what you're looking at — headline inferences up high, raw signals down low. <b>Car trip</b> is synthesized when no real trip exists.
        Logical levels &amp; lifts are saved per user. Tap any event to trace how it was built.
      </footer>

      {/* fixed, always-reachable zoom control — discoverability + keyboard/accessibility */}
      <div className="zoomctl" role="group" aria-label="timeline altitude">
        <button aria-label="zoom in — more detail" onClick={() => zoomStep(+1)}>+</button>
        <span className="zlevel"><b>L{altL}</b>{ALT_NAMES[altL]}</span>
        <button aria-label="zoom out — fewer, higher-level" onClick={() => zoomStep(-1)}>−</button>
      </div>

      <EventModal event={modalEvent} byId={byId} getL={getL} derivLevel={derivLevel} onClose={() => setModalEvent(null)} />
    </>
  );
}

function Pill({ v, k, accent }: { v: string | number; k: string; accent?: boolean }) {
  return (
    <div className={"pill" + (accent ? " a" : "")}>
      <span className="pv">{v}</span>
      <span className="pk">{k}</span>
    </div>
  );
}
