import type { ComponentType } from "react";
import { Calendar, Layers } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import TimelineDashboard from "../dashboards/timeline/TimelineDashboard";
import LevelsDashboard from "../dashboards/levels/LevelsDashboard";

/** A dashboard is a registered module. Adding one = write its component + append an entry
 *  here. Nav and routes both derive from this array, so the shell never needs editing.
 *
 *  Two entries have been removed rather than kept for symmetry: **Compare** (any event types as
 *  parallel lanes on a shared scale) and **Signals** (the raw feed as a table). Both were built
 *  when the day timeline was one undifferentiated column and reading the feed meant lining
 *  signals up by eye. The two-lane day answers that directly — a moment renders inside the
 *  activity containing it — and the event modal walks the lineage down to the raw contributors,
 *  so both boards had become second ways to see what the day already shows. Removing them is
 *  the registry seam working in the other direction; they're one file plus one line in git
 *  history if a real question ever wants them back. */
export interface DashboardDef {
  slug: string;                 // URL segment: /d/:slug
  title: string;                // nav label
  group?: string;               // optional nav grouping
  Icon?: LucideIcon;            // icon shown in nav
  component: ComponentType;     // reads shared data via useAware()
}

export const DASHBOARDS: DashboardDef[] = [
  { slug: "timeline", title: "Day timeline", group: "Life", Icon: Calendar, component: TimelineDashboard },
  { slug: "levels", title: "Levels", group: "Config", Icon: Layers, component: LevelsDashboard },
];
