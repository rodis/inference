#!/bin/sh
# Serialise the DEPLOY CYCLE, not the push.
#
# The goal is parallel development, serial deployment. Agents work concurrently in linked
# worktrees; one push at a time travels the whole chain (CI -> deploy-state -> Argo -> pods)
# before the next starts. Without this, two pushes minutes apart mean Argo may only ever
# observe the second, and the first commit's image is built, tagged, and never runs anywhere.
#
#   acquire <sha>   taken by .githooks/pre-push, BEFORE anything leaves the machine. The hook
#                   is the reliable choke point; the monitor cannot acquire because there is a
#                   window between `git push` and the agent starting.
#   release         called by the push-monitor once it reaches a Verdict — success OR failure.
#                   A failed deploy must release: you almost certainly need to push a fix.
#   status          human/agent inspection.
#
# The TTL is not defensive padding. The monitor is a Claude agent in a pane: it has been
# observed failing to start silently (prompt pasted, never submitted) and panes get closed
# mid-run. A lock held by something that can vanish without a trace must expire on its own.
set -eu

TTL=${DEPLOY_LOCK_TTL:-1800}                      # 30 min > a full deploy cycle (~5-10 min)
lock="$(git rev-parse --git-common-dir)/deploy.lock"
now=$(date +%s)

age() { [ -f "$lock" ] || return 1; at=$(sed -n 's/^at=//p' "$lock" 2>/dev/null || echo 0); echo $(( now - ${at:-0} )); }

case "${1:-status}" in
  acquire)
    sha=${2:-$(git rev-parse --short HEAD)}
    if a=$(age); then
      if [ "$a" -lt "$TTL" ]; then
        held_sha=$(sed -n 's/^sha=//p' "$lock"); held_by=$(sed -n 's/^by=//p' "$lock")
        cat >&2 <<MSG

  ✗ push refused — a deploy is still in flight.

      commit:  $held_sha
      started: $((a / 60))m ${a}s ago, by $held_by

  One push travels the whole chain before the next starts, so that "did it reach
  pods" stays answerable. Wait for the monitor to report, or if it has died:

      scripts/deploy-lock.sh release

  Override:  git push --no-verify

MSG
        exit 1
      fi
      echo "  ⚠ breaking a stale deploy lock (${a}s old > ${TTL}s TTL) — the monitor likely died" >&2
    fi
    printf 'sha=%s\nat=%s\nby=%s\n' "$sha" "$now" "${HERDR_PANE_ID:-$(hostname):$$}" > "$lock"
    ;;
  release)
    if a=$(age); then rm -f "$lock"; echo "deploy lock released after $((a))s"; else echo "no deploy lock held"; fi
    ;;
  status)
    if a=$(age); then
      echo "HELD  sha=$(sed -n 's/^sha=//p' "$lock")  age=${a}s  by=$(sed -n 's/^by=//p' "$lock")"
      [ "$a" -ge "$TTL" ] && echo "      (stale — past the ${TTL}s TTL, next acquire will break it)"
    else
      echo "free"
    fi
    ;;
  *) echo "usage: $0 {acquire [sha]|release|status}" >&2; exit 2 ;;
esac
