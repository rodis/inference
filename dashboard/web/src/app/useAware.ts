import { createContext, useContext } from "react";
import type { Prepared } from "../view";

/** Shared, cross-dashboard data + config — loaded once by DataProvider and consumed by
 *  every dashboard via useAware(). A dashboard's own view state stays inside the dashboard. */
export interface AwareCtx {
  users: string[];
  userId: string;
  setUserId: (u: string) => void;
  status: string;            // "" when loaded; otherwise a loading/error line to show
  eventsCount: number;       // raw row count from the API (for footers etc.)
  prepared: Prepared;        // all / byId / raw / derived / days / derivLevel
  selectedDay: string;       // shared across day-based dashboards so the day persists on nav
  setSelectedDay: (d: string) => void;
  getL: (name: string) => number;
  getCeil: (name: string) => number;
  onHome: (name: string, level: number) => void;
  onLift: (name: string, level: number) => void;
  saved: boolean;
}

export const AwareContext = createContext<AwareCtx | null>(null);

export function useAware(): AwareCtx {
  const ctx = useContext(AwareContext);
  if (!ctx) throw new Error("useAware must be used within <DataProvider>");
  return ctx;
}
