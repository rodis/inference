import type { InferredEvent, Interval, Contributor } from "./generated/events";

// Re-export the generated capability/lineage shapes so components import them from one place.
export type { InferredEvent, Interval, Contributor };

// The `message` on the wire is the generated InferredEvent shape (derived events), plus the
// loose fields raw producer events carry (car/device/…). Derived-only fields are optional
// here because raw events lack them. The interval + lineage shapes come from the generated
// contract (contracts/inferred_event.schema.json) — single source of truth, no drift.
export type EventMessage = Partial<InferredEvent> & {
  name: string;
  car?: string;
  device?: string;
  [k: string]: unknown;
};

export interface AwareEvent {
  id: string;
  name: string;
  event_class: "raw" | "derived";
  source_app?: string;
  occurred_epoch: number;
  message: EventMessage;
  // view-computed fields (added client-side)
  epoch: number;
  date: Date;
}

export interface Preferences {
  levels: Record<string, number>;
  lift: Record<string, number>;
}
