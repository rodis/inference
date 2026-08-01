---
name: push-monitor
description: After pushing commits to GitHub, hand CI/deploy watching to a background Claude agent in a split herdr pane instead of polling inline. Use immediately after any `git push` to a branch that triggers a workflow, or when asked to "watch the deploy", "monitor the push", "keep an eye on CI". Skips silently when not in a herdr session.
---

# Push monitor

Deploy cycles here are ~5–10 minutes (runtime image build + Argo sync, and Vector on top of that),
and the standing rule is **do not poll CI inline** — report the SHAs and hand back. This skill keeps
that rule while removing its cost: the watching is delegated to a separate agent in its own pane, so
the user sees progress without either of us blocking on it.

## Preconditions — check in this order, stop at the first failure

1. **A push actually happened.** Only run this after `git push` reports success. Do not run it for a
   local commit — nothing will be watching for anything.
2. **In a herdr session:** `[ -n "$HERDR_PANE_ID" ]`. If unset, **skip the whole skill silently** —
   report the SHAs in the normal way and say nothing about panes. Not being in herdr is the normal
   case for CI, cron and headless runs, not an error worth narrating.
3. **No monitor already running in this workspace.** Reuse rather than stacking panes:

```bash
herdr agent list | python3 -c "
import sys,json,os
ws=os.environ.get('HERDR_WORKSPACE_ID')
m=[a for a in json.load(sys.stdin)['result']['agents']
   if a.get('name')=='push-monitor' and a['workspace_id']==ws]
print(m[0]['pane_id'] if m else '')
"
```

If that prints a pane id, **reuse it** — skip to step 3 and prompt the existing agent with the new
push. Only split a new pane when it prints nothing.

## Steps

### 1. Split, in one of two places

Where the monitor goes depends on whether the right half of the window is already occupied. Never
squeeze the conversation pane a second time — if something is already on the right, the monitor goes
*under it*, not beside it.

```bash
RIGHT=$(herdr pane neighbor --current --direction right 2>/dev/null | python3 -c "
import sys,json
print(json.load(sys.stdin).get('result',{}).get('neighbor',{}).get('neighbor_pane_id') or '')
")

if [ -z "$RIGHT" ]; then
  # Nothing on the right: split the current pane in half, vertically.
  PANE=$(herdr pane split --current --direction right --ratio 0.5 --cwd "$PWD" --no-focus \
      --env KUBECONFIG=/Users/rods/.kube/kube_prod \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['pane']['pane_id'])")
else
  # Occupied: split THAT pane horizontally; the new pane is the bottom one.
  PANE=$(herdr pane split "$RIGHT" --direction down --ratio 0.5 --cwd "$PWD" --no-focus \
      --env KUBECONFIG=/Users/rods/.kube/kube_prod \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['pane']['pane_id'])")
fi
```

Two parsing traps, both of which fail *silently* rather than loudly:

- The neighbour id is at **`result.neighbor.neighbor_pane_id`** — nested one level deeper than it
  looks. Reading `result.neighbor_pane_id` returns `None` for every direction, so the branch above
  would always take the "nothing on the right" path and keep halving the conversation pane.
- When there is no neighbour the key is simply **absent** (not null, not an error, exit status still
  0), so `or ''` is what makes the `-z` test work.

`--no-focus` matters throughout: the user is mid-conversation and focus should not jump.

### 2. Start a Claude agent in it — with a retry

```bash
for i in 1 2 3 4 5; do
  OUT=$(herdr agent start push-monitor --kind claude --pane "$PANE" --timeout 60000 \
        -- --permission-mode auto --model sonnet 2>&1)
  case "$OUT" in *'"agent_started"'*) echo "$OUT"; break ;; esac
  [ "$i" = 5 ] && { echo "FAILED: $OUT"; herdr pane close "$PANE"; exit 1; }
  sleep 2
done
```

Everything after `--` is passed to the agent binary.

**`--permission-mode auto` is what makes this usable unattended** — the monitor runs `gh` and
`kubectl` repeatedly with nobody watching its pane, and a permission prompt would stall it silently
until someone noticed. (Valid modes: `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk`,
`plan`.) `auto` is the right level: this job only *reads* CI and cluster state, so it needs to not
stall, not to be unrestricted.

**`--model sonnet`** — the work is polling two APIs and comparing strings against a checklist. It is
long-running and repetitive rather than hard, which is what Sonnet is for; spending Opus on it buys
nothing.

**The retry is required, not defensive.** The pane must be at an interactive shell prompt, and a
freshly split pane is *not* one for the first second or two while zsh loads its profile. Running the
split and the start back-to-back fails with:

```
{"error":{"code":"agent_pane_busy","message":"agent target pane w3:p4 is not an available shell"}}
```

`--timeout` does not cover this — it governs readiness *after* attaching, not waiting for the shell
to exist. This bites only when the two commands run in one script; interactively they are far enough
apart to mask it, which is exactly how it got missed the first time.

Success returns `"interactive_ready": true`. On give-up, close the pane and fall back to reporting
the SHAs inline — do not leave a dead pane behind.

Note the error is returned as JSON on stdout with exit status 0, so check the payload, not `$?`.

### 3. Hand it the task

```bash
herdr agent prompt push-monitor "<the prompt below>"
```

Do **not** pass `--wait`. The point is to not block.

### 4. Tell the user, in one line

Name the SHA, what will run, and that the pane on the right is watching it. Then continue or hand
back — do **not** wait for it.

### 5. Check back — once, later, and only report the exceptions

Handing off is not the same as forgetting. A monitor whose pane was closed, or that stalled, leaves
a push nobody verified — and the user should hear about *that*, while a clean green run needs no
comment at all because they already have the pane.

**When to check.** Opportunistically, **at most once per turn**, at a natural moment:

- before launching a new monitor (the reuse lookup in the preconditions already reads this);
- when the user asks;
- when you are about to hand back and a monitor from an earlier push has not reported yet.

**Never** on a timer, never twice in a turn, never in a loop. One check is a glance; repeated checks
are the polling this skill exists to remove.

```bash
STATE=$(herdr agent get push-monitor 2>/dev/null | python3 -c "
import sys,json
try: print(json.load(sys.stdin)['result']['agent']['agent_status'])
except Exception: print('gone')
")
VERDICT=$(herdr pane read "$PANE" 2>/dev/null | grep -E '^\s*Verdict:' | tail -1)
```

**`agent_status` alone cannot tell you whether it finished.** A monitor that completed and reported
sits at **`idle`** — exactly like one that was started and never prompted. That is why the prompt
mandates a final line beginning `Verdict:`: it is the completion signal, and grepping for it is the
only reliable check. Keep the two in sync if you edit the prompt.

| state | meaning | say |
|---|---|---|
| `working` | still running | nothing, unless well past its 20-min budget |
| `idle` + a `Verdict:` line | finished | **nothing if green.** Relay only a failure, in one line |
| `idle`, no `Verdict:` | never ran, or died mid-way | surface it — that push is unverified |
| `blocked` | waiting on input | surface it; it will never finish on its own |
| `gone` | pane closed before reporting | surface it — the push was never verified |

**Report exceptions only.** The whole point is that a green deploy costs the user zero attention. If
the verdict is clean, do not narrate it, do not summarise the phases, do not congratulate the
pipeline. Mention it only if the user asked, or if something in it needs action.

Known wrinkle: `--permission-mode auto` is not total. A monitor has been observed parked on
"Let me wait for permission to run the gh command…" mid-run — it recovered on its own, but that is
what a `blocked` state will look like if it does not.

## The monitor prompt

Fill in the SHA and branch; keep the rest, it encodes gotchas the agent will otherwise hit. The job
is **five phases** and is not done at green CI — green CI only means an image exists.

> Track commit `<SHA>` on `<branch>` in `rodis/inference` all the way from CI to running pods, then
> report once and stop.
>
> **Tooling.** Call `/opt/homebrew/bin/gh` by absolute path — the `gh` shell function is wrapped in
> `op plugin run` and dies with "interactive IO not available" in an agent session, while
> `gh --version` still works so it looks fine. Ambient `GH_TOKEN` is read-only, which is all you
> need. `KUBECONFIG=/Users/rods/.kube/kube_prod` is already exported in this pane; if a `kubectl`
> call says "connection refused" or "no configuration", set it explicitly rather than assuming the
> cluster is down. There is **no `argocd` CLI** — read Argo state via `kubectl` against the
> Application CRs in the `argocd` namespace.
>
> **Everything below is read-only. Never `kubectl apply`, `edit`, `delete`, `rollout restart`, or
> `argocd sync`.** `selfHeal` is on, so a manual cluster change is both futile and misleading — the
> only correct fix for a bad image is a new commit. If something is broken, report it; do not repair
> it.
>
> ### Phase 1 — CI
> A push touching anything outside `deploy/**` triggers `publish-images.yml`: the `_ci-checks.yml`
> gate (ruff/pytest/typecheck/drift), then a per-component image build, a `values.yml` bump to
> `sha-<short>`, a commit, and a force-push of `deploy-state`. A push touching only `deploy/**`
> triggers `mirror-deploy-state.yml` instead. Both force-push `deploy-state`, so a push containing
> *both* code and `deploy/**` races — flag that as a bug if you see it.
> Poll `gh run list --commit <SHA>` then `gh run watch <id>`. On failure, stop here and report the
> failing job plus only the relevant log lines.
>
> ### Phase 2 — deploy-state
> Get the new head: `gh api repos/rodis/inference/commits/deploy-state --jq .sha`.
> Confirm it moved and note the image tag the bump commit wrote.
>
> ### Phase 3 — Argo picks it up
> ```
> kubectl -n argocd get applications -o custom-columns='NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status,REV:.status.sync.revision,TARGET:.spec.source.targetRevision'
> ```
> Three apps: `inference-runtime` and `inference-dashboard` track **deploy-state**;
> `inference-vector` tracks **main** directly.
>
> **The revision comparison people get wrong:** `.status.sync.revision` is the **deploy-state** sha
> (e.g. `261c62ac…`) — *not* the commit you pushed to main. Compare it against the Phase 2 head, not
> against `<SHA>`. For `inference-vector`, which tracks main, it *is* `<SHA>`.
>
> Want `Synced` + `Healthy` against the new revision. Argo polls on its own schedule, so allow a few
> minutes before calling it stuck.
>
> ### Phase 4 — the change actually reached the cluster
> **`Synced` is not proof.** A values-only change on a force-pushed branch has been observed
> reporting `Synced` against a *stale Helm render* for ~25 minutes. So verify the thing that matters
> — the image tag actually on the Deployment:
> ```
> kubectl -n inference get deploy -o custom-columns='NAME:.metadata.name,READY:.status.readyReplicas,IMAGE:.spec.template.spec.containers[0].image'
> ```
> Expect `ghcr.io/rodis/inference-*:sha-<short>` to match the tag from Phase 2. Deployments:
> `application` is the **runtime** (Stakater chart names it generically), plus `aware-dashboard`,
> `bmw-cardata`, and `vector` (upstream image, only moves on a Vector config/chart change).
> If Argo says `Synced` but the image tag is still the old one, that is the stale-render case — say
> so explicitly, it is the single most confusing failure here.
>
> ### Phase 5 — rollout
> ```
> kubectl -n inference rollout status deploy/application --timeout=300s
> ```
> and the same for any other deployment whose tag changed. Then confirm pods are actually up, not
> just that the rollout command returned:
> ```
> kubectl -n inference get pods -o wide
> ```
> Watch for `CrashLoopBackOff`, `ImagePullBackOff`, or a new pod stuck `Pending` while an old one
> still serves — image drift shows up exactly that way.
>
> ### Report
> One short paragraph per phase that had something to say, then **a final line beginning literally
> `Verdict:`** — did the commit reach running pods or not. That prefix is the completion signal the
> launching agent greps for; without it your run is indistinguishable from one that never started.
> Call out specifically:
> - a workflow failure, with the failing job and the relevant log lines only;
> - `Synced` with a stale image (Phase 4) — the confusing one;
> - Argo `ComparisonError`, which wedges an app with an empty revision that a hard refresh will not
>   clear; only a new commit fixes it;
> - pods that rolled but are not healthy.
>
> Keep it tight — this is a status report, not a narrative. If the whole chain is still incomplete
> after 20 minutes, say where it stopped and stop rather than waiting indefinitely.

## Notes

- The pane persists after the run finishes so the user can read the result. Leave it; the reuse check
  in the preconditions will pick it up on the next push.
- If the push was to a branch Argo does not track, say so in the prompt — the agent should then watch
  only Phase 1 and skip Phases 2–5.
- A docs-only push still fires `publish-images.yml` and still rebuilds and redeploys every image,
  because the trigger is `paths-ignore: deploy/**` rather than a source-path allowlist. That is
  wasteful but expected; the monitor should not report it as an anomaly.
- Never push *in order to* trigger this. The skill reacts to a push the user asked for.
