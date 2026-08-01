#!/bin/sh
# Named advisory locks, shared across every worktree of this repo.
#
#   lock.sh <name> acquire [detail]
#   lock.sh <name> release
#   lock.sh <name> status
#
# Two locks exist today:
#
#   deploy    Serialises the DEPLOY CYCLE, not the push. The goal is parallel development,
#             serial deployment: one push travels the whole chain (CI -> deploy-state -> Argo
#             -> pods) before the next starts. Without it, two pushes minutes apart mean Argo
#             may only ever observe the second, and the first commit's image is built, tagged,
#             and never runs anywhere. Acquired by .githooks/pre-push — the only choke point
#             that fires BEFORE anything leaves the machine — and released by the push-monitor
#             on its Verdict, success OR failure (a failed deploy is when you need to push a fix).
#
#   history   Guards `rederive.py --produce`, which rewrites derived history. NOTE this is the
#             SECOND line of defence, not the first: rederive is not idempotent, so two runs in
#             SEQUENCE over one window duplicate just as thoroughly as two in parallel. The lock
#             only closes the window where two simultaneous runs both pre-check, both see
#             nothing, and both produce. The real guard is rederive's own --replace pre-flight.
#
# The TTL is not defensive padding. A holder can be a Claude agent in a pane, and one has been
# observed failing to start silently (prompt pasted, never submitted); panes also get closed
# mid-run. A lock held by something that can vanish without a trace must expire on its own.
set -eu

# NB: no braces in this message — a `}` inside ${x:?...} closes the expansion early and the
# remainder lands in the variable ("deploy}" instead of "deploy").
name=${1:?usage: lock.sh NAME acquire|release|status}
shift
case "$name" in
  *[!a-z0-9-]*) echo "lock name must be [a-z0-9-]: $name" >&2; exit 2 ;;
esac

TTL=${LOCK_TTL:-${DEPLOY_LOCK_TTL:-1800}}         # 30 min > a full deploy cycle (~5-10 min)
lock="$(git rev-parse --git-common-dir)/${name}.lock"
now=$(date +%s)

age() { [ -f "$lock" ] || return 1; at=$(sed -n 's/^at=//p' "$lock" 2>/dev/null || echo 0); echo $(( now - ${at:-0} )); }

# A holder that no longer exists is stale NOW, whatever the TTL says. Locks record the herdr
# pane that took them, and herdr answers `pane_not_found` for a pane that is gone — so a monitor
# whose pane was closed, or a worktree torn down mid-rederive, frees its lock immediately
# instead of blocking every other agent for the full 30 minutes. Falls back to the TTL when the
# holder is not a herdr pane (a bare hostname:pid, where there is no cheap liveness check).
holder_gone() {
  by=$(sed -n 's/^by=//p' "$lock" 2>/dev/null) || return 1
  case "$by" in w[0-9]*:p*) ;; *) return 1 ;; esac
  command -v herdr >/dev/null 2>&1 || return 1
  # herdr writes this error to STDERR and exits 1, so `2>/dev/null` would discard the very
  # thing being matched and every dead holder would look alive. Merge the streams.
  herdr pane get "$by" 2>&1 | grep -q 'pane_not_found'
}

case "${1:-status}" in
  acquire)
    detail=${2:-$(git rev-parse --short HEAD)}
    if a=$(age); then
      if holder_gone; then
        echo "  ⚠ breaking the $name lock — holder pane $by no longer exists" >&2
      elif [ "$a" -lt "$TTL" ]; then
        held_detail=$(sed -n 's/^detail=//p' "$lock"); held_by=$(sed -n 's/^by=//p' "$lock")
        cat >&2 <<MSG

  ✗ refused — the '$name' lock is held.

      holder:  $held_detail
      started: $((a / 60))m ${a}s ago, by $held_by

  Wait for the holder to finish. If it has died:

      scripts/lock.sh $name release

MSG
        exit 1
      else
        echo "  ⚠ breaking a stale $name lock (${a}s old > ${TTL}s TTL) — the holder likely died" >&2
      fi
    fi
    printf 'detail=%s\nat=%s\nby=%s\n' "$detail" "$now" "${HERDR_PANE_ID:-$(hostname):$$}" > "$lock"
    ;;
  release)
    if a=$(age); then rm -f "$lock"; echo "$name lock released after $((a))s"; else echo "no $name lock held"; fi
    ;;
  status)
    if a=$(age); then
      echo "HELD  $(sed -n 's/^detail=//p' "$lock")  age=${a}s  by=$(sed -n 's/^by=//p' "$lock")"
      if holder_gone; then
        echo "      (STALE — holder pane $by is gone; next acquire will break it)"
      elif [ "$a" -ge "$TTL" ]; then
        echo "      (stale — past the ${TTL}s TTL, next acquire will break it)"
      fi
    else
      echo "free"
    fi
    ;;
  *) echo "usage: $0 <name> {acquire [detail]|release|status}" >&2; exit 2 ;;
esac
