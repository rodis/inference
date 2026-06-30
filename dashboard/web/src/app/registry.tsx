import type { ComponentType } from "react";
import { Calendar, BarChart2, Radio } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import TimelineDashboard from "../dashboards/timeline/TimelineDashboard";
import CompareDashboard from "../dashboards/compare/CompareDashboard";
import SignalsDashboard from "../dashboards/signals/SignalsDashboard";

/** A dashboard is a registered module. Adding one = write its component + append an entry
 *  here. Nav and routes both derive from this array, so the shell never needs editing. */
export interface DashboardDef {
  slug: string;                 // URL segment: /d/:slug
  title: string;                // nav label
  group?: string;               // optional nav grouping
  Icon?: LucideIcon;            // icon shown in nav
  component: ComponentType;     // reads shared data via useAware()
}

export const DASHBOARDS: DashboardDef[] = [
  { slug: "timeline", title: "Day timeline", group: "Life", Icon: Calendar, component: TimelineDashboard },
  { slug: "compare", title: "Compare", group: "Life", Icon: BarChart2, component: CompareDashboard },
  { slug: "signals", title: "Signals", group: "Life", Icon: Radio, component: SignalsDashboard },
];
