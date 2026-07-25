import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useAware } from "../../app/useAware";
import { DAY_WINDOW } from "../../api";
import type { AwareEvent } from "../../types";
import { absorbedIds, catOf, dayKey, dayLayout, humanDur, laneNames } from "../../view";
import DayTimeline from "../../components/DayTimeline";
import WeekStrip from "../../components/WeekStrip";
import EventModal from "../../components/EventModal";

const MON = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

/** The day at a glance: one altitude-zoomed timeline. Altitude is driven by a pinch /
 *  ⌘-scroll gesture *anchored at the point you're looking at* (the focused event stays put
 *  while detail grows/collapses around it), plus a fixed +/- control for discoverability. */
export default function TimelineDashboard() {
  const { prepared, lanes, levelOf, defaultOf, isHidden, status, eventsCount, userId, selectedDay } = useAware();
  const { all, byId, derivLevel } = prepared;

  const [altitude, setAltitude] = useState<number>(1); // 1 = headlines (high) … `lanes` = ground
  const [modalEvent, setModalEvent] = useState<AwareEvent | null>(null);

  // An event's lane comes from the levels board (/d/levels); a type parked there is off the
  // timeline at every altitude, not merely deep — hence the hard zero rather than a low reveal.
  const revealOf = useCallback((e: AwareEvent) => {
    if (isHidden(e.name)) return 0;
    return Math.max(0, Math.min(1, altitude - levelOf(e.name) + 1));
  }, [altitude, levelOf, isHidden]);

  // All of the day's events stay in the layout; dayLayout places each by *time of day* on one
  // shared scale (proportional, quiet gaps collapsed) and reveals detail with altitude, so
  // lower-layer events fade in/out at their true time instead of popping the layout. A visible
  // span's get-in/get-out contributors fold into its capsule (revealDay), so the day reads as
  // a flat list of activities rather than a capsule plus its redundant boundary rows.
  // A type parked on the levels board is dropped here, not merely faded: leaving it in with
  // reveal 0 would keep an invisible card in the layout (and in the tab order) at every
  // altitude, which is not what "off the timeline" means.
  const dayAll = useMemo(
    () => all.filter((e) => dayKey(e.date) === selectedDay && !isHidden(e.name)),
    [all, selectedDay, isHidden]
  );
  const absorbed = useMemo(() => absorbedIds(dayAll, revealOf), [dayAll, revealOf]);
  const revealDay = useCallback((e: AwareEvent) => (absorbed.has(e.id) ? 0 : revealOf(e)), [absorbed, revealOf]);
  const packed = useMemo(() => dayLayout(dayAll, revealDay, (n) => catOf(n).c), [dayAll, revealDay]);
  const shownCount = useMemo(() => dayAll.reduce((n, e) => (revealDay(e) > 0.5 ? n + 1 : n), 0), [dayAll, revealDay]);

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
    const next = clamp(rawNext, 1, lanes);
    if (Math.abs(next - cur) < 0.001) return;
    const wrap = wrapRef.current;
    if (wrap && dayAllRef.current.length) {
      const wrapTop = wrap.getBoundingClientRect().top;
      const pos = packedRef.current.pos;
      let best: AwareEvent | null = null, bestD = Infinity;
      for (const ev of dayAllRef.current) {
        const targetReveal = isHidden(ev.name) ? 0 : clamp(next - levelOf(ev.name) + 1, 0, 1);
        if (targetReveal < 0.4) continue; // anchor to something that stays visible
        const y = pos.get(ev.id) ?? 0;
        const d = Math.abs(wrapTop + y - clientY);
        if (d < bestD) { bestD = d; best = ev; }
      }
      pendingAnchor.current = best ? { id: best.id, oldY: pos.get(best.id) ?? 0 } : null;
    }
    altitudeRef.current = next;
    setAltitude(next);
  }, [lanes, levelOf, isHidden]);

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

  // High-level daily indicators for the selected day (not debug counts). Time in the car is
  // the sum of the day's car_trip durations; spend is the sum of the day's card payments.
  const daily = useMemo(() => {
    const driveSec = dayAll.reduce((s, e) => (e.name === "car_trip" ? s + (e.message.interval?.duration_seconds ?? 0) : s), 0);
    const spent = dayAll.reduce((s, e) => (e.name === "credit_card_payment" ? s + (Number(e.message.amount) || 0) : s), 0);
    return { driveSec, spent };
  }, [dayAll]);

  if (status) return <div className="statusline">{status}</div>;

  const dh = selectedDay ? new Date(selectedDay + "T00:00:00") : null;
  const altL = clamp(Math.round(altitude), 1, lanes);
  const altName = laneNames(lanes)[altL - 1]?.toLowerCase() ?? "";

  return (
    <>
      {dh && (
        <div className="datehead">
          <span className="dnum">{dh.getDate()}.</span> <span className="dmon">{MON[dh.getMonth()]}</span>{" "}
          <span className="dyear">{dh.getFullYear()}</span> <span className="chev">›</span>
        </div>
      )}

      <WeekStrip />

      <div className="summary">
        <Pill v={daily.driveSec ? humanDur(daily.driveSec) : "—"} k="in the car" accent />
        <Pill v={daily.spent ? `CHF ${daily.spent.toFixed(2)}` : "—"} k="spent" accent />
      </div>

      {/* Full width since the "Assign & lift" sidebar moved to its own board (/d/levels) —
          the timeline is the thing you came to read, and the config was permanent furniture. */}
      <div className="sheet">
        <div className="handle" />
        <div className="sheet-head">
          <span className="stitle">Timeline</span>
          <span className="zoom-hint">pinch or ⌘-scroll on the timeline to zoom · {shownCount} shown</span>
        </div>
        <div className="vtwrap" ref={wrapRef}>
          <DayTimeline events={dayAll} layout={packed} onSelect={setModalEvent} revealOf={revealDay} />
        </div>
      </div>

      <footer>
        <b>Aware</b> — from the <b>events</b> table in Neon (Postgres): {eventsCount} events for <b>{userId}</b> over the last {DAY_WINDOW} days.
        Raw signals come from iPhone Shortcuts via Vector → Kafka; inferences from the runtime, each with a <b>derivation lineage</b>.
        Zoom anchors on what you're looking at — headline inferences up high, raw signals down low.
        Which lane a type lives in defaults to its derivation depth; set the exceptions on <b>Levels</b>.
        Tap any event to trace how it was built.
      </footer>

      {/* fixed, always-reachable zoom control — discoverability + keyboard/accessibility */}
      <div className="zoomctl" role="group" aria-label="timeline altitude">
        <button aria-label="zoom in — more detail" onClick={() => zoomStep(+1)}>+</button>
        <span className="zlevel"><b>L{altL}</b>{altName}</span>
        <button aria-label="zoom out — fewer, higher-level" onClick={() => zoomStep(-1)}>−</button>
      </div>

      {/* the timeline's cards carry no L / D / "below" chips — this is where that lives now */}
      <EventModal event={modalEvent} byId={byId} levelOf={levelOf} derivLevel={derivLevel}
        defaultOf={defaultOf} revealOf={revealDay} onClose={() => setModalEvent(null)} />
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
