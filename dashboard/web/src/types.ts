export interface Contributor {
  id: string;
  name?: string;
  timestamp?: number;
}

export interface EventMessage {
  id: string;
  name: string;
  user_id?: string;
  timestamp?: number;
  inference_type?: string;
  confidence_score?: number | null;
  derived_from?: Contributor[];
  car?: string;
  device?: string;
  [k: string]: unknown;
}

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
  // synthetic-trip only
  synthetic?: boolean;
  endEpoch?: number;
  durationSec?: number;
}

export interface Preferences {
  levels: Record<string, number>;
  lift: Record<string, number>;
}
