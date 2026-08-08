import { createBrowserRouter, createMemoryRouter, RouterProvider } from "react-router-dom";
import type { RouteObject } from "react-router-dom";
import DataProvider from "./DataProvider";
import Shell from "./Shell";
import HomeView from "../dashboards/home/HomeView";
import { MODULES } from "./registry";

function NotFound() {
  return <div className="statusline">No such dashboard. <a href="/">Go home →</a></div>;
}

// "/" is a real view, not a redirect: the Home spine + each module's registered HomeCard.
// Module routes stay at /d/:slug, so every deep link that existed before the portal shell
// keeps resolving.
const routes: RouteObject[] = [
  {
    element: <Shell />,
    children: [
      { index: true, element: <HomeView /> },
      ...MODULES.map((m) => {
        const C = m.component;
        return { path: `d/${m.slug}`, element: <C /> };
      }),
      { path: "*", element: <NotFound /> },
    ],
  },
];

// Production uses clean URLs (FastAPI serves the SPA shell for deep links). The static
// preview artifact runs in a sandboxed iframe with no server, so it flips to in-memory
// routing via a global the preview shim sets.
const preview = typeof window !== "undefined" && (window as { __AWARE_PREVIEW__?: boolean }).__AWARE_PREVIEW__;
const router = preview
  ? createMemoryRouter(routes, { initialEntries: ["/"] })
  : createBrowserRouter(routes);

/** DataProvider wraps the router so both the shell and every dashboard share one load. */
export default function App() {
  return (
    <DataProvider>
      <RouterProvider router={router} />
    </DataProvider>
  );
}
