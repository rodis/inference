import { useEffect, useState } from "react";
import { useAware } from "./useAware";

/** Fetch through the module data lane (ctx.client — cached, deduped) as React state.
 *  Pass null to render without fetching (SSR, missing user). Errors surface as a string
 *  so a board can show its own quiet failure line instead of crashing. */
export function useQuery<T>(url: string | null): { data?: T; error?: string } {
  const { client } = useAware();
  const [state, setState] = useState<{ data?: T; error?: string; for?: string }>({});
  useEffect(() => {
    if (!url || !client) return;
    let live = true;
    client.get<T>(url).then(
      (data) => { if (live) setState({ data, for: url }); },
      (e: Error) => { if (live) setState({ error: e.message, for: url }); },
    );
    return () => { live = false; };
  }, [url, client]);
  // A stale result for a different URL (user switched, scope changed) renders as loading,
  // not as the previous query's data wearing the new query's title.
  return state.for === url ? state : {};
}
