import type { ComponentType } from "react";
import {
  Activity, BrainCircuit, Calendar, CreditCard, Layers, MapPin, Route, ScrollText, Store,
  Workflow,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import TimelineDashboard from "../dashboards/timeline/TimelineDashboard";
import LevelsDashboard from "../dashboards/levels/LevelsDashboard";
import LastJourneyCard from "../dashboards/home/LastJourneyCard";
import SpendDashboard from "../dashboards/money/SpendDashboard";
import SpendHomeCard from "../dashboards/money/SpendHomeCard";
import ProcessesDashboard from "../dashboards/processes/ProcessesDashboard";

/** The portal's whole structure is registry data: sections give the sidebar its groups,
 *  modules give it entries, and the frame (Shell / HomeView / the palette) only ever derives
 *  from these two arrays. Adding a dashboard = write its component + append a module entry;
 *  adding a whole domain = one section row + its modules. The frame is never edited for
 *  either — that's the invariant that makes this a portal rather than a dashboard list.
 *
 *  Two entries were removed 2026-08 rather than kept for symmetry: **Compare** and **Signals**
 *  (see git history) — the two-lane day and the event modal's lineage walk had absorbed both.
 *  The registry seam works in that direction too. */

export type SectionId =
  | "life" | "places" | "journeys" | "money" | "processes" | "health" | "brain" | "config";

/** A planned module: named in the sidebar as a dashed "soon" entry so the portal shows where
 *  it is going. Purely declarative — promoting one to real is moving its title onto a module. */
export interface PlannedModule {
  title: string;
  Icon?: LucideIcon;
}

export interface Section {
  id: SectionId;
  title: string;
  planned?: PlannedModule[];
}

/** Sidebar order. A section renders only when it has at least one module or planned entry. */
export const SECTIONS: Section[] = [
  { id: "life", title: "Life" },
  { id: "places", title: "Places", planned: [{ title: "Stays", Icon: MapPin }] },
  { id: "journeys", title: "Journeys", planned: [{ title: "Journeys", Icon: Route }] },
  { id: "money", title: "Money", planned: [{ title: "Merchants", Icon: Store }] },
  { id: "processes", title: "Processes" },
  { id: "health", title: "Health", planned: [{ title: "Activity", Icon: Activity }] },
  {
    id: "brain", title: "Brain",
    planned: [{ title: "Policies & state", Icon: BrainCircuit }, { title: "Ledger", Icon: ScrollText }],
  },
  { id: "config", title: "Config" },
];

/** How a module reads shared time context. Declaring scopes is what makes the top bar's
 *  period control appear while that module is active; a module with none (the day timeline
 *  owns its own week strip) never sees it. */
export type Scope = "day" | "week" | "month";

/** One ⌘K result. `run` receives a navigate function; anything richer (setting the shared
 *  day, filtering a board) is closed over by the provider that minted the item. */
export interface PaletteItem {
  key: string;
  group: string;               // section header in the palette ("Dashboards", "Places", …)
  label: string;
  detail?: string;             // right-aligned mono hint
  color?: string;              // icon tile fill; defaults to the accent
  Icon?: LucideIcon;
  run: (navigate: (to: string) => void) => void;
}

/** Modules may contribute palette results; the palette merges every provider's items with
 *  its built-ins (modules / days / places). Pure function of the query — no registration
 *  ceremony, no cleanup. */
export type PaletteProvider = (query: string) => PaletteItem[];

export interface ModuleDef {
  slug: string;                 // URL segment: /d/:slug
  title: string;                // sidebar + palette label
  section: SectionId;           // sidebar placement (required — the sidebar derives from it)
  Icon?: LucideIcon;            // icon shown in nav
  component: ComponentType;     // reads shared data via useAware()
  HomeCard?: ComponentType;     // optional glanceable card composed into "/" (a door, not the analysis)
  scopes?: Scope[];             // opts into the shared period control
  palette?: PaletteProvider;    // opts into ⌘K result groups
}

export const MODULES: ModuleDef[] = [
  {
    slug: "timeline", title: "Day timeline", section: "life", Icon: Calendar,
    component: TimelineDashboard, HomeCard: LastJourneyCard,
  },
  {
    slug: "spend", title: "Weekly spend", section: "money", Icon: CreditCard,
    component: SpendDashboard, HomeCard: SpendHomeCard, scopes: ["week", "month"],
  },
  {
    slug: "processes", title: "Invoicing", section: "processes", Icon: Workflow,
    component: ProcessesDashboard,
  },
  { slug: "levels", title: "Levels", section: "config", Icon: Layers, component: LevelsDashboard },
];

/** Sidebar model: sections in declared order, each with its live modules and ghosts.
 *  Empty sections vanish — the sidebar never shows a heading with nothing under it. */
export function sidebarSections() {
  return SECTIONS
    .map((s) => ({
      section: s,
      modules: MODULES.filter((m) => m.section === s.id),
      planned: s.planned ?? [],
    }))
    .filter((s) => s.modules.length || s.planned.length);
}
