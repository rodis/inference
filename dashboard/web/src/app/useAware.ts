import { createContext, useContext } from "react";
import type { Prepared } from "../view";
import type { Scope } from "./registry";

/** A module's private data lane: same-origin GET with an in-memory cache keyed by URL, so
 *  navigating back to a board never re-fetches. Modules that aggregate server-side (Money's
 *  spend_by_day, the Brain's policies) fetch through this instead of the shared events
 *  window — the frame supplies the client, never the queries. `invalidate` clears by prefix
 *  (or everything), for the day a module writes. */
export interface QueryClient {
  get<T>(url: string): Promise<T>;
  invalidate(prefix?: string): void;
}

/** Shared, cross-dashboard context — loaded once by DataProvider and consumed by every
 *  dashboard via useAware(). What is *shared* is context (user, period, the selected day,
 *  the level ladder) and the recent-events window the original boards read; a new module's
 *  own data comes through `client` and stays module-private. */
export interface AwareCtx {
  users: string[];
  userId: string;
  setUserId: (u: string) => void;
  status: string;            // "" when loaded; otherwise a loading/error line to show
  eventsCount: number;       // raw row count from the API (for footers etc.)
  prepared: Prepared;        // all / byId / raw / derived / days / derivLevel
  selectedDay: string;       // shared across day-based dashboards so the day persists on nav
  setSelectedDay: (d: string) => void;
  scope: Scope;              // shared period; only modules declaring `scopes` surface it
  setScope: (s: Scope) => void;
  client: QueryClient;       // per-module data lane (see QueryClient)

  // --- the level ladder (one knob per event type; see view.ts) ---
  lanes: number;                              // ladder height = laneCount(maxDepth)
  levelOf: (name: string) => number;          // the lane a type renders in
  defaultOf: (name: string) => number | null; // what its depth implies (null = never seen)
  isHidden: (name: string) => boolean;        // kept off the timeline at every altitude
  overrides: number;                          // how many types sit off their default
  /** Types with a stored entry. Union with `prepared.types` to get every row the levels
   *  board must show — a type can be configured but have no events in the window. */
  configured: string[];
  setLevel: (name: string, level: number) => void;
  setHidden: (name: string, hidden: boolean) => void;
  resetLevel: (name: string) => void;         // back to the depth default
  resetAll: () => void;
  saved: boolean;
}

export const AwareContext = createContext<AwareCtx | null>(null);

export function useAware(): AwareCtx {
  const ctx = useContext(AwareContext);
  if (!ctx) throw new Error("useAware must be used within <DataProvider>");
  return ctx;
}
