#!/usr/bin/env bash
# Generate a phone_is_charging session for *today* by emitting a connect→disconnect pair.
# The runtime's session_window engine pairs them and derives phone_is_charging.
#
#   scripts/emit-charging.sh [duration_minutes]      (default 60)
set -euo pipefail

DUR_MIN="${1:-60}"
HERE="$(cd "$(dirname "$0")" && pwd)"
NOW="$(date +%s)"
START=$(( NOW - DUR_MIN * 60 ))

"$HERE/emit.sh" device_connected_to_power "$START"
sleep 1
"$HERE/emit.sh" device_disconnected_from_power "$NOW"
echo "charge session: ${DUR_MIN} min ending now — runtime should derive phone_is_charging shortly"
