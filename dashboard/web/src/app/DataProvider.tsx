import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchEvents, fetchPreferences, fetchUsers, savePreferences } from "../api";
import type { AwareEvent, Preferences } from "../types";
import { prepare } from "../view";
import { AwareContext } from "./useAware";
import type { AwareCtx } from "./useAware";

const EMPTY_PREFS: Preferences = { levels: {}, lift: {} };

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

  const getL = useCallback((name: string) => prefs.levels[name] ?? 4, [prefs]);
  const getCeil = useCallback((name: string) => {
    const h = getL(name);
    const k = prefs.lift[name];
    return k == null || k > h ? h : k;
  }, [prefs, getL]);

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

  const ctx: AwareCtx = {
    users, userId, setUserId, status, eventsCount: events.length,
    prepared, selectedDay, setSelectedDay, getL, getCeil, onHome, onLift, saved,
  };

  return <AwareContext.Provider value={ctx}>{children}</AwareContext.Provider>;
}
