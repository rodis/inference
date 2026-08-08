import type { Scope } from "../../app/registry";

/** The /api/money/spend contract — the module's own route (app.py::SPEND_SQL), never the
 *  shared events window. `merchants[].placed` says the label came from the payment×stay
 *  containment join (a curated place) rather than the raw merchant string. */
export interface SpendReport {
  days: number;
  total: number;
  count: number;
  matched: number;
  prev_total: number;
  by_day: { day: string; total: number; count: number }[];
  merchants: { label: string; placed: boolean; count: number; total: number }[];
}

/** The shared period control maps to trailing whole-day windows — honest and picker-free. */
export const SCOPE_DAYS: Partial<Record<Scope, number>> = { week: 7, month: 30 };

export const spendUrl = (userId: string, scope: Scope): string | null => {
  const days = SCOPE_DAYS[scope] ?? 7;
  return userId ? `/api/money/spend?user_id=${encodeURIComponent(userId)}&days=${days}` : null;
};

export const chf = (v: number) =>
  `CHF ${v.toLocaleString("en-CH", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
