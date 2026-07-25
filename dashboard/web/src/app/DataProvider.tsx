import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchEvents, fetchPreferences, fetchUsers, savePreferences } from "../api";
import type { AwareEvent, Preferences } from "../types";
import { defaultLevelOf, laneCount, prepare } from "../view";
import { AwareContext } from "./useAware";
import type { AwareCtx } from "./useAware";

const EMPTY_PREFS: Preferences = { level: {}, hidden: [] };

/** Loads users + per-user events/prefs once, derives the prepared event graph, and owns
 *  the level/lift config (global to all dashboards). Everything is exposed via context so
 *  navigating between dashboards never re-fetches. */
export default function DataProvider({ children }: { children: React.ReactNode }) {
  const [users, setUsers] = useState<string[]>([]);
  const [userId, setUserId] = useState<string>("");
  const [events, setEvents] = useState<AwareEvent[]>([]);
  const [prefs, setPrefs] = useState<Preferences>(EMPTY_PREFS);
  const [status, setStatus] = useState<string>("Loading…");
  const [saved, setSaved] = useState(false);
  const [selectedDay, setSelectedDay] = useState<string>("");

  useEffect(() => {
    fetchUsers()
      .then((us) => {
        setUsers(us);
        if (us.length) setUserId(us[0]);
        else setStatus("No users in the events table yet.");
      })
      .catch((e) => setStatus("Failed to load users: " + e.message));
  }, []);

  useEffect(() => {
    if (!userId) return;
    setStatus("Loading…");
    Promise.all([fetchEvents(userId), fetchPreferences(userId)])
      .then(([evs, pf]) => { setEvents(evs); setPrefs(pf); setStatus(""); })
      .catch((e) => setStatus("Failed to load data: " + e.message));
  }, [userId]);

  const prepared = useMemo(() => prepare(events), [events]);

  // default the shared day to the latest one whenever a new dataset loads
  useEffect(() => {
    const days = prepared.days;
    if (days.length) setSelectedDay(days[days.length - 1]);
  }, [prepared]);

  // --- the level ladder ----------------------------------------------------------
  // One number per event type, and most types don't need one: the ladder is as tall as the
  // deepest inference in view and a type's depth picks its lane, so `prefs.level` holds only
  // the exceptions. A type with no events in the window has no depth to read, so it falls to
  // the bottom lane until one fires.
  const lanes = useMemo(() => laneCount(prepared.maxDepth), [prepared.maxDepth]);
  const defaultOf = useCallback((name: string) => {
    const d = prepared.depthOf(name);
    return d == null ? null : defaultLevelOf(d, lanes);
  }, [prepared, lanes]);
  const levelOf = useCallback((name: string) => {
    const set = prefs.level[name];
    if (set != null) return Math.min(lanes, Math.max(1, set));
    return defaultOf(name) ?? lanes;
  }, [prefs, defaultOf, lanes]);
  const isHidden = useCallback((name: string) => prefs.hidden.includes(name), [prefs]);
  const overrides = useMemo(
    () => Object.keys(prefs.level).length + prefs.hidden.length,
    [prefs]
  );
  const configured = useMemo(
    () => [...new Set([...Object.keys(prefs.level), ...prefs.hidden])],
    [prefs]
  );

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

  const commit = useCallback((mutate: (p: Preferences) => Preferences) => {
    setPrefs((p) => {
      const next = mutate(p);
      scheduleSave(next);
      return next;
    });
  }, [scheduleSave]);

  // Landing on the depth default stores *nothing* — that's what keeps the config sparse, and
  // what lets a default follow the definitions as they change instead of freezing on save.
  const setLevel = useCallback((name: string, level: number) => commit((p) => {
    const level_ = { ...p.level };
    if (level === defaultOf(name)) delete level_[name];
    else level_[name] = Math.min(lanes, Math.max(1, level));
    return { level: level_, hidden: p.hidden.filter((n) => n !== name) };
  }), [commit, defaultOf, lanes]);

  const setHidden = useCallback((name: string, hidden: boolean) => commit((p) => ({
    level: p.level,
    hidden: hidden
      ? (p.hidden.includes(name) ? p.hidden : [...p.hidden, name])
      : p.hidden.filter((n) => n !== name),
  })), [commit]);

  const resetLevel = useCallback((name: string) => commit((p) => {
    const level = { ...p.level };
    delete level[name];
    return { level, hidden: p.hidden.filter((n) => n !== name) };
  }), [commit]);

  const resetAll = useCallback(() => commit(() => EMPTY_PREFS), [commit]);

  const ctx: AwareCtx = {
    users, userId, setUserId, status, eventsCount: events.length,
    prepared, selectedDay, setSelectedDay,
    lanes, levelOf, defaultOf, isHidden, overrides, configured,
    setLevel, setHidden, resetLevel, resetAll, saved,
  };

  return <AwareContext.Provider value={ctx}>{children}</AwareContext.Provider>;
}
