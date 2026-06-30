#!/usr/bin/env bash
# Emit a raw sensor event to the Vector ingest gateway → raw_sensors → inference runtime.
#
#   scripts/emit.sh <event_name> [timestamp_epoch] [user_id] [source_app]
#
# Defaults: timestamp = now, user_id = rods, source_app = manual.
# Override the host with VECTOR_HOST (default https://vector.prod.rods.me).
#
# Examples:
#   scripts/emit.sh device_connected_to_power
#   scripts/emit.sh device_connected_to_power 1782840000 rods shortcut
set -euo pipefail

EVENT="${1:?usage: emit.sh <event_name> [timestamp_epoch] [user_id] [source_app]}"
TS="${2:-$(date +%s)}"
USER_ID="${3:-rods}"
APP="${4:-manual}"
HOST="${VECTOR_HOST:-https://vector.prod.rods.me}"

# Vector route: /<domain>/<app>[/<topic>]. domain=sensors → topic defaults to raw_sensors.
# Producers POST {"payload": {...}}; the payload becomes message.* (event_name → name).
curl -fsS -X POST "$HOST/sensors/$APP/raw_sensors" \
  -H 'Content-Type: application/json' \
  -d "{\"payload\":{\"event_name\":\"$EVENT\",\"user_id\":\"$USER_ID\",\"timestamp\":$TS}}"
echo "sent: $EVENT @ $TS (user=$USER_ID app=$APP)"
