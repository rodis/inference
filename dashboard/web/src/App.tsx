import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchEvents, fetchPreferences, fetchUsers, savePreferences } from "./api";
import type { AwareEvent, Preferences } from "./types";
import { buildScale, catOf, CAT, dayKey, prepare, typeLabel } from "./view";
import VTimeline from "./components/VTimeline";
import AssignPanel from "./components/AssignPanel";
import EventModal from "./components/EventModal";

const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MON = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const CMP_DEFAULT = ["car_lock_state_change", "device_connected_to_carplay", "device_disconnected_from_carplay", "car_door_opened", "car_door_closed"];
const EMPTY_PREFS: Preferences = { levels: {}, lift: {} };

export default function App() {
  const [users, setUsers] = useState<string[]>([]);
  const [userId, setUserId] = useState<string>("");
  const [events, setEvents] = useState<AwareEvent[]>([]);
  const [prefs, setPrefs] = useState<Preferences>(EMPTY_PREFS);
  const [status, setStatus] = useState<string>("Loading…");

  const [selectedDay, setSelectedDay] = useState<string>("");
  const [filterLevel, setFilterLevel] = useState<number>(1);
  const [cmpSel, setCmpSel] = useState<Set<string>>(new Set());
  const [modalEvent, setModalEvent] = useState<AwareEvent | null>(null);
  const [saved, setSaved] = useState(false);

  // --- load users once ---
  useEffect(() => {
    fetchUsers()
      .then((us) => {
        setUsers(us);
        if (us.length) setUserId(us[0]);
        else setStatus("No users in the events table yet.");
      })
      .catch((e) => setStatus("Failed to load users: " + e.message));
  }, []);

  // --- load events + prefs whenever the selected user changes ---
  useEffect(() => {
    if (!userId) return;
    setStatus("Loading…");
    Promise.all([fetchEvents(userId), fetchPreferences(userId)])
      .then(([evs, pf]) => {
        setEvents(evs);
        setPrefs(pf);
        setStatus("");
      })
      .catch((e) => setStatus("Failed to load data: " + e.message));
  }, [userId]);

  const prepared = useMemo(() => prepare(events), [events]);
  const { all, byId, raw, derived, days, derivLevel } = prepared;

  // when a new dataset loads, jump to the latest day + seed the compare cluster
  useEffect(() => {
    if (!days.length) return;
    setSelectedDay(days[days.length - 1]);
    const present = new Set(all.map((e) => e.name));
    setCmpSel(new Set(CMP_DEFAULT.filter((n) => present.has(n))));
  }, [prepared]); // eslint-disable-line react-hooks/exhaustive-deps

  const getL = useCallback((name: string) => prefs.levels[name] ?? 4, [prefs]);
  const getCeil = useCallback((name: string) => {
    const h = getL(name);
    const k = prefs.lift[name];
    return k == null || k > h ? h : k;
  }, [prefs, getL]);

  // --- persistence (debounced PUT) ---
  const saveTimer = useRef<number | undefined>(undefined);
  const scheduleSave = useCallback((next: Preferences) => {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      if (!userId) return;
      savePreferences(userId, next)
        .then(() => { setSaved(true); window.setTimeout(() => setSaved(false), 1500); })
        .catch(() => {});
    }, 400);
  }, [userId]);

  const onHome = useCallback((name: string, level: number) => {
    setPrefs((p) => {
      const next = { levels: { ...p.levels, [name]: level }, lift: { ...p.lift, [name]: level } };
      scheduleSave(next);
      return next;
    });
  }, [scheduleSave]);

  const onLift = useCallback((name: string, level: number) => {
    setPrefs((p) => {
      const next = { levels: p.levels, lift: { ...p.lift, [name]: level } };
      scheduleSave(next);
      return next;
    });
  }, [scheduleSave]);

  // --- the two timelines on a shared per-day scale ---
  const { dayMain, dayCmp, scale } = useMemo(() => {
    const main = all.filter((e) => dayKey(e.date) === selectedDay && getCeil(e.name) <= filterLevel && filterLevel <= getL(e.name));
    const cmp = all.filter((e) => dayKey(e.date) === selectedDay && cmpSel.has(e.name));
    return { dayMain: main, dayCmp: cmp, scale: buildScale([...main, ...cmp].map((e) => e.epoch)) };
  }, [all, selectedDay, filterLevel, cmpSel, getL, getCeil]);

  // --- summary ---
  const summary = useMemo(() => {
    if (!all.length) return null;
    const epochs = all.map((e) => e.epoch);
    const t0 = Math.min(...epochs), t1 = Math.max(...epochs);
    const h = (t1 - t0) / 3600;
    const span = h >= 1 ? `${h.toFixed(1)} h` : `${Math.round((t1 - t0) / 60)} min`;
    const deepest = Math.max(...all.map((e) => derivLevel(e)));
    return { signals: raw.length, inferences: derived.length, deepest, span };
  }, [all, raw, derived, derivLevel]);

  const allTypes = useMemo(() => [...new Set(all.map((e) => e.name))], [all]);
  const chipOrder = useMemo(
    () => allTypes.slice().sort((a, b) => (CAT[a] ? 0 : 1) - (CAT[b] ? 0 : 1) || a.localeCompare(b)),
    [allTypes]
  );

  function toggleCmp(name: string) {
    setCmpSel((prev) => {
      const next = new Set(prev);
      next.has(name) ? next.delete(name) : next.add(name);
      return next;
    });
  }

  if (status) {
    return (
      <div className="wrap">
        <Appbar users={users} userId={userId} onUser={setUserId} saved={saved} />
        <div className="statusline">{status}</div>
      </div>
    );
  }

  const dh = selectedDay ? new Date(selectedDay + "T00:00:00") : null;

  return (
    <div className="wrap">
      <Appbar users={users} userId={userId} onUser={setUserId} saved={saved} />

      {dh && (
        <div className="datehead">
          <span className="dnum">{dh.getDate()}.</span> <span className="dmon">{MON[dh.getMonth()]}</span>{" "}
          <span className="dyear">{dh.getFullYear()}</span> <span className="chev">›</span>
        </div>
      )}

      <div className="weekstrip">
        {days.map((dk) => {
          const d = new Date(dk + "T00:00:00");
          const cats = [...new Set(all.filter((e) => dayKey(e.date) === dk).map((e) => catOf(e.name).c))].slice(0, 4);
          return (
            <button key={dk} className={"daycell" + (dk === selectedDay ? " sel" : "")} onClick={() => setSelectedDay(dk)}>
              <div className="dow">{DOW[d.getDay()]}</div>
              <div className="dn">{d.getDate()}</div>
              <div className="dots">{cats.map((c, i) => <i key={i} style={{ background: c }} />)}</div>
            </button>
          );
        })}
      </div>

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
            <div className="dualbar">
              <div className="dualbar-col">
                <span className="stitle">Timeline</span>
                <div className="seg">
                  <span className="lbl">level</span>
                  {[1, 2, 3, 4].map((lv) => (
                    <button key={lv} type="button" aria-pressed={lv === filterLevel} onClick={() => setFilterLevel(lv)}>L{lv}</button>
                  ))}
                </div>
              </div>
              <div className="dualbar-col">
                <span className="stitle"><span className="accent">Compare</span></span>
                <div className="cmpchips">
                  {chipOrder.map((name) => {
                    const cat = catOf(name), on = cmpSel.has(name);
                    return (
                      <button key={name} className={"cmp-chip" + (on ? " on" : "")}
                        style={on ? { background: cat.c, borderColor: cat.c, color: "#fff" } : undefined}
                        onClick={() => toggleCmp(name)}>
                        <span className="cd" style={{ background: on ? "#fff" : cat.c }} />{typeLabel(name)}
                      </button>
                    );
                  })}
                </div>
              </div>
            </div>
            <div className="dualwrap">
              <VTimeline events={dayMain} scale={scale} getL={getL} getCeil={getCeil} derivLevel={derivLevel} onSelect={setModalEvent} />
              <VTimeline events={dayCmp} scale={scale} getL={getL} getCeil={getCeil} derivLevel={derivLevel} onSelect={setModalEvent} />
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
        <b>Aware</b> — from the <b>events</b> table in Neon (Postgres): {events.length} events for <b>{userId}</b>.
        Raw signals come from iPhone Shortcuts via Vector → Kafka; inferences from the runtime, each with a <b>derivation lineage</b>.
        The two timelines share one per-day time scale, so rows at the same moment line up. <b>Car trip</b> is synthesized when no real trip exists.
        Logical levels &amp; lifts are saved per user. Tap any event to trace how it was built.
      </footer>

      <EventModal event={modalEvent} byId={byId} getL={getL} derivLevel={derivLevel} onClose={() => setModalEvent(null)} />
    </div>
  );
}

function Appbar({ users, userId, onUser, saved }: { users: string[]; userId: string; onUser: (u: string) => void; saved: boolean }) {
  return (
    <div className="appbar">
      <div className="applogo">🚗</div>
      <span className="appname">Aware</span>
      {users.length > 0 && (
        <span className="userselect">
          <label htmlFor="usersel">user</label>
          <select id="usersel" value={userId} onChange={(e) => onUser(e.target.value)}>
            {users.map((u) => <option key={u} value={u}>{u}</option>)}
          </select>
        </span>
      )}
      <span className={"saveflag" + (saved ? " show" : "")}>saved ✓</span>
      <span className="sub">personal telemetry · {userId || "rods"}</span>
    </div>
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
