"""Emit the InferredEvent JSON Schema — the shared Python↔TS contract (Stage 1, step 3).

The Pydantic `InferredEvent` (inference.event) is the single source of truth for a derived
event's `message` shape. This dumps its JSON Schema to `contracts/inferred_event.schema.json`;
the dashboard regenerates its TypeScript types from that file (`npm run gen:types` in
dashboard/web) so the frontend never hand-maintains — and never drifts from — the shape.

Run from the repo root:  uv run python scripts/emit_event_schema.py
Re-run whenever inference.event changes, and commit the updated schema + regenerated .ts.
"""

import json
from pathlib import Path

from inference.event import InferredEvent

OUT = Path(__file__).resolve().parent.parent / "contracts" / "inferred_event.schema.json"


def main() -> None:
    # mode="serialization" so computed fields (Interval.duration_seconds) are included —
    # they're in the emitted JSON, so they must be in the contract the frontend generates from.
    schema = InferredEvent.model_json_schema(mode="serialization")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(schema, indent=2) + "\n")
    print(f"wrote {OUT} ({len(schema.get('properties', {}))} top-level properties)")


if __name__ == "__main__":
    main()
