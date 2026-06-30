import type { ComponentType } from "react";
import TimelineDashboard from "../dashboards/timeline/TimelineDashboard";
import CompareDashboard from "../dashboards/compare/CompareDashboard";
import SignalsDashboard from "../dashboards/signals/SignalsDashboard";

/** A dashboard is a registered module. Adding one = write its component + append an entry
 *  here. Nav and routes both derive from this array, so the shell never needs editing. */
export interface DashboardDef {
  slug: string;                 // URL segment: /d/:slug
  title: string;                // nav label
  group?: string;               // optional nav grouping
  icon?: string;                // emoji shown in nav
  component: ComponentType;     // reads shared data via useAware()
}

export const DASHBOARDS: DashboardDef[] = [
  { slug: "timeline", title: "Day timeline", group: "Life", icon: "🗓️", component: TimelineDashboard },
  { slug: "compare", title: "Compare", group: "Life", icon: "📊", component: CompareDashboard },
  { slug: "signals", title: "Signals", group: "Life", icon: "📡", component: SignalsDashboard },
];
