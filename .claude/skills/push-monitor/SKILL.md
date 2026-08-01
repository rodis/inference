---
name: push-monitor
description: Open a monitor pane for EVERY push to a branch that triggers a workflow, then check back on it later. Invoke automatically and unprompted immediately after any successful `git push` — do not ask first, do not offer it as an option. Also invoke when asked to "watch the deploy", "monitor the push", "keep an eye on CI". Skips silently when not in a herdr session.
---

# Push monitor

## What this is for

Three things at once, and every rule below follows from one of them:

1. **The user watches the pipeline.** The pane is theirs — the whole chain, CI through pods, visible
   without them asking anyone for it.
2. **You watch it in the background**, so you can flag anything they missed. That is the *only*
   reason you look at it at all.
3. **Neither of those interrupts the work.** They keep going with the current agent while the deploy
   proceeds in its own pane.

Deploy cycles here are ~5–10 minutes (runtime image build + Argo sync, and Vector on top), and the
standing rule is **do not poll CI inline**. This delegates the watching instead of abandoning it.

## Two panes: a dashboard the agent draws, and the agent

The status display is **not** the agent's chat output. It cannot be, for a reason worth stating once:

- A Claude agent's **tool output is collapsed in its own pane** — the pane shows `Ran 1 shell command`
  with nothing under it. So anything a script prints *from inside the agent* is invisible, and no
  amount of prettier tooling in its hands changes that. Its only visible surface is its final message.
- **That message is prose, and prose drifts.** On 2026-08-01 the mandated summary box was dropped
  entirely, `✅` came out as `✓` and once as the bare word `OK`, and the ~70-col pane wrapped each
  one-line-per-phase row into three. Every one of those destroys the glanceability the format exists
  for — and the `OK` also defeated the emoji grep this skill used to decide whether to speak.

So the display is a **second, plain pane** — no agent in it, no tokens — running
[`scripts/deploy_status.py`](../../../scripts/deploy_status.py) `watch`, which draws a live rich
panel from a small JSON state file. The agent's job shrinks to calling `set` after each phase. The
visual is then deterministic, correctly sized at any width, and identical on every run.

```
╭─ 344b0cb · main ──────────────────────╮
│ ✅ 1 CI            #140 · 142 tests   │   dashboard pane (top, ~25%)
│ ✅ 2 deploy-state  b9a4eda · sha-…    │   plain shell, rich Live
│ ⠹  3 Argo          polling… 2m        │
│ ·  4 image                            │
│ ·  5 rollout                          │
╰──────────────── 4m12s ────────────────╯
  ── agent transcript below (~75%) ──
```

The state file is `~/.cache/inference-deploy/<sha>.json`, and `deploy_status.py check --sha <sha>`
turns it into an exit code — **0 green, 1 trouble, 2 incomplete** — which is what step 5 uses instead
of grepping glyphs out of a scrollback. The script carries its dependency in a PEP 723 header, so
`uv run` installs rich into an ephemeral env; nothing to set up, and it never touches the project's
dependencies.

## Run it on every push, without being asked

**This is automatic.** After any successful `git push` that triggers a workflow, open the monitor as
part of the same turn. Do **not** ask "want me to open the monitor?", do not present it as an option,
and do not wait to be told — being asked each time is the failure mode this replaces.

The only reasons not to: the preconditions below fail, or the user has said to stop.

Mention it in **one clause** at the end of your normal reply ("…monitor tracking it on the right").
It is infrastructure, not news.

## Preconditions — check in this order, stop at the first failure

1. **A push actually happened.** Only run this after `git push` reports success. Do not run it for a
   local commit — nothing will be watching for anything.
2. **In a herdr session:** `[ -n "$HERDR_PANE_ID" ]`. If unset, **skip the whole skill silently** —
   report the SHAs in the normal way and say nothing about panes. Not being in herdr is the normal
   case for CI, cron and headless runs, not an error worth narrating.
3. **No monitor already running in this workspace.** Reuse rather than stacking panes.

   **Use a per-workspace agent name, lowercased**: herdr agent names reject uppercase (observed
   2026-08-02: workspace id `wB` → `invalid_agent_name`), and workspace ids are mixed-case, so the
   raw `push-monitor-$HERDR_WORKSPACE_ID` fails on any workspace past `wz`. Never use a bare
   `push-monitor` either: `herdr agent get|prompt <target>` resolves a name with **no workspace
   filter**, so two worktrees each running this skill would create two agents with the same name and
   every later `get`/`prompt` would hit an arbitrary one — including the check-back that exists to
   catch failures. Set the lowercased id once and derive both names — the agent's and the dashboard
   label's — from it, so the two never disagree on case:

```bash
WS_LC=$(printf '%s' "$HERDR_WORKSPACE_ID" | tr '[:upper:]' '[:lower:]')
NAME="push-monitor-$WS_LC"
```

   The lookup filters on `workspace_id` exactly (unmodified), so lowercasing the *name* cannot
   collide two workspaces whose ids differ only by case:

```bash
herdr agent list | python3 -c "
import sys,json,os
ws=os.environ.get('HERDR_WORKSPACE_ID')
m=[a for a in json.load(sys.stdin)['result']['agents']
   if a.get('name')==f'push-monitor-{ws.lower()}' and a['workspace_id']==ws]
print(m[0]['pane_id'] if m else '')
"
```

If that prints a pane id, **reuse it** — skip to step 2 and prompt the existing agent with the new
push. Only split new panes when it prints nothing. The dashboard pane is found the same way, by the
label this skill gives it:

```bash
DASH=$(herdr pane list | python3 -c "
import sys,json,os
ws=os.environ.get('HERDR_WORKSPACE_ID')
m=[p for p in json.load(sys.stdin)['result']['panes']
   if p.get('label')==f'deploy-dash-{ws.lower()}' and p['workspace_id']==ws]
print(m[0]['pane_id'] if m else '')
")
```

## Steps

### 1. Split twice: dashboard on top, agent below

Where the monitor area goes depends on whether the right half of the window is already occupied.
Never squeeze the conversation pane a second time — if something is already on the right, the
monitor goes *under it*, not beside it.

**Both panes belong in the PRIMARY checkout, never in a worktree** — pass `$REPO` below, never
`$PWD`. `$PWD` is whatever the launching session happened to be sitting in, which makes the panes'
cwd incidental: land from a worktree session and they inherit *that* worktree. It has to be pinned,
for a reason that bites late and looks unrelated:

- A worktree **can be removed while the deploy is still running** — a ~5–10 minute window. Its
  directory then vanishes under the monitor, and the last thing the monitor does is
  `scripts/lock.sh deploy release`. That fails, so the `deploy` lock sits until its 30-minute TTL and
  every other agent is blocked for no reason — the exact failure the lock exists to prevent.
- Nothing the monitor reads is branch-specific anyway. It watches CI, `deploy-state`, Argo and pods:
  all `main`'s, all remote. The primary checkout is the only one guaranteed to outlive the deploy.

```bash
# The primary checkout — the first entry of `worktree list`, resolvable from inside any worktree.
REPO=$(git worktree list --porcelain | head -1 | cut -d' ' -f2)

RIGHT=$(herdr pane neighbor --current --direction right 2>/dev/null | python3 -c "
import sys,json
print(json.load(sys.stdin).get('result',{}).get('neighbor',{}).get('neighbor_pane_id') or '')
")

if [ -z "$RIGHT" ]; then
  # Nothing on the right: split the current pane in half, vertically.
  DASH=$(herdr pane split --current --direction right --ratio 0.5 --cwd "$REPO" --no-focus \
      --env KUBECONFIG=/Users/rods/.kube/kube_prod \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['pane']['pane_id'])")
else
  # Occupied: split THAT pane horizontally; the new pane is the bottom one.
  DASH=$(herdr pane split "$RIGHT" --direction down --ratio 0.5 --cwd "$REPO" --no-focus \
      --env KUBECONFIG=/Users/rods/.kube/kube_prod \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['pane']['pane_id'])")
fi
herdr pane rename "$DASH" "deploy-dash-$WS_LC"

# Now carve the agent out of the BOTTOM of that pane, leaving the dashboard a slim strip on top.
sleep 1
PANE=$(herdr pane split "$DASH" --direction down --ratio 0.25 --cwd "$REPO" --no-focus \
    --env KUBECONFIG=/Users/rods/.kube/kube_prod \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['pane']['pane_id'])")
```

**`--ratio` is the share kept by the pane being split, not the share given to the new one.** Verified
2026-08-01: `--ratio 0.25` on a 46-row pane left the original at 11 rows and gave the new one 35. So
`0.25` here means *dashboard 25%, agent 75%*, which is what you want — the panel needs 7 rows plus a
shell prompt, and 11 is comfortable. On a short window raise it to `0.35` rather than letting the
panel clip.

Three parsing traps, all of which fail *silently* rather than loudly:

- The neighbour id is at **`result.neighbor.neighbor_pane_id`** — nested one level deeper than it
  looks. Reading `result.neighbor_pane_id` returns `None` for every direction, so the branch above
  would always take the "nothing on the right" path and keep halving the conversation pane.
- When there is no neighbour the key is simply **absent** (not null, not an error, exit status still
  0), so `or ''` is what makes the `-z` test work.
- The second split needs the `sleep` for the same reason the agent start does (see step 2) — herdr
  is still settling the first one.

`--no-focus` matters throughout: the user is mid-conversation and focus should not jump.

### 1b. Start the dashboard

Seed the state file first so the panel opens with the right sha, branch and clock, then run the
watcher in the plain pane. No agent, no permissions, no tokens.

```bash
# $REPO is the primary checkout from step 1 — NOT `git rev-parse --show-toplevel`, which returns
# the worktree root when the skill is invoked from one, and so bakes a deletable path into the
# long-lived watcher command.
uv run "$REPO/scripts/deploy_status.py" init --sha "$SHA" --branch "$(git -C "$REPO" branch --show-current)"
herdr pane run "$DASH" "uv run $REPO/scripts/deploy_status.py watch --sha $SHA --timeout 1800"
```

`watch` tolerates being started before the first `set` (it shows "waiting for the monitor's first
phase…"), holds the final frame on screen when the verdict lands, and exits on the timeout so a dead
monitor does not leave a spinner turning forever.

### 2. Start a Claude agent in it — with a retry

```bash
for i in 1 2 3 4 5; do
  OUT=$(herdr agent start "$NAME" --kind claude --pane "$PANE" --timeout 60000 \
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
herdr agent prompt "$NAME" "<the prompt below>"
```

Do **not** pass `--wait`. The point is to not block.

**Then confirm it actually started — `prompt` does not reliably submit.** A long prompt lands in the
input box as `[Pasted text #1 +54 lines]` and just *sits* there: `agent_status: idle`,
`tokens: None`, `$0.00`. The pane looks alive, the agent has done nothing, and nothing ever reports.
Observed 2026-08-01 on a ~90-line prompt.

```bash
sleep 3
STATE=$(herdr agent get "$NAME" | python3 -c "
import sys,json; print(json.load(sys.stdin)['result']['agent']['agent_status'])")
if [ "$STATE" != "working" ]; then
  herdr agent send-keys "$NAME" enter      # submit the pending paste
fi
```

`send-keys ... enter` submits what is already in the box, so this is idempotent-ish and cheap. Do
**not** re-issue `prompt` — that would paste the text a second time on top of the first.

This check is not optional. Without it the failure is silent and total: the user sees a pane, the
launching agent believes it delegated, and the push goes unwatched.

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
- **before ending any turn while a monitor from an earlier push has not reported.** This is the one
  that matters — it is what makes purpose (2) real. A monitor nobody ever looks at is worse than no
  monitor, because it manufactures the impression the push was verified.

**Never** on a timer, never twice in a turn, never in a loop. One check is a glance; repeated checks
are the polling this skill exists to remove.

Note the asymmetry: you check *often enough to catch a problem*, and speak *only when there is one*.
Those are different thresholds on purpose. Checking is cheap and silent; talking costs the user
attention, which is the thing this skill is protecting.

```bash
uv run "$REPO/scripts/deploy_status.py" check --sha "$SHA"; VERDICT=$?
STATE=$(herdr agent get "$NAME" 2>/dev/null | python3 -c "
import sys,json
try: print(json.load(sys.stdin)['result']['agent']['agent_status'])
except Exception: print('gone')
")
```

**`check` is the whole decision, and it reads the JSON, not the screen.** `0` green — say nothing.
`1` trouble — it prints the offending phases on one line, relay that. `2` incomplete — combine with
`STATE` below to work out whether it is still going or died. You never parse prose, and never grep a
glyph, which is what the previous version did and what a `✓`-instead-of-`✅` silently defeated.

**`agent_status` alone cannot tell you whether it finished.** A monitor that completed and reported
sits at **`idle`** — exactly like one that was started and never prompted. `check` is what
distinguishes them: a finished run has written a verdict into the state file.

| `check` | `agent_status` | meaning | say |
|---|---|---|---|
| 2 | `working` | still running | nothing, unless well past its 20-min budget |
| 0 | `idle` | finished, green | **nothing.** They already have the dashboard |
| 1 | any | a phase is ❌ or ⚠️ | relay `check`'s one-line output |
| 2 | `idle`, `tokens: None` | **never submitted** — the paste is still in the box | send `enter` (see step 3), then say nothing |
| 2 | `idle`, tokens present | ran and died mid-way | surface it — that push is unverified |
| 2 | `blocked` | waiting on input | surface it; it will never finish on its own |
| 2 | `gone` | pane closed before reporting | surface it — the push was never verified |

`tokens` is what separates "never started" from "started and died" — an agent that has done no work
at all reports `tokens: None` and `$0.00`. The first is fixable in one keystroke; the second is not.

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
> **Reporting: you do not draw the status display — you update it.** A dashboard is already running
> in the pane above yours, rendering from a state file. Your output is these calls, one the moment
> each phase settles — **substitute the absolute `$REPO` path when filling this prompt in**, do not
> leave the variable for the monitor to resolve (its shell never had it set, and its pane's cwd is
> the primary checkout, not necessarily where you launched from):
>
> ```bash
> uv run $REPO/scripts/deploy_status.py set --sha <SHA> --phase <1-5> \
>     --state run|ok|warn|fail|skip --detail "<the specific facts>"
> ```
>
> - `run` when you *start* a phase, so the dashboard spins rather than looking stalled.
> - `ok` passed · `warn` passed but a human should look (restarts > 0, a suspected stale render, odd
>   timing) · `fail` broken, stop the chain · `skip` not applicable this run (say why in `--detail`).
> - `--detail` is **facts, not prose**: run number, sha, tag, counts, durations. It renders on one
>   line and is ellipsised, so put the identifying fact first — `"#140 · 142 tests · 1m03s"`, not
>   `"the CI workflow completed successfully after running the tests"`.
>
> Close with the verdict, which is also the completion signal the launching agent checks for:
>
> ```bash
> uv run $REPO/scripts/deploy_status.py verdict --sha <SHA> \
>     --text "<sha> reached running pods — 3 deployments on sha-<short>, 0 restarts"
> ```
>
> Do not print status lines, boxes, tables or emoji into your own chat output — it is collapsed,
> mis-wrapped and invisible where it matters, and it is not what anyone reads. **If every phase is
> `ok`, your entire chat output is one short sentence.** Write detail only for a `warn` or `fail`
> phase, under a `### ❌ Phase N — <name>` heading, and only for that phase.
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
> ### Update the dashboard as you go — never batch it to the end
>
> Call `set --state run` when you begin a phase and again with its outcome the moment it settles.
> The pane above is watchable in flight only if you do; batching leaves it frozen on phase 1 for ten
> minutes, which reads as a hang.
>
> A worked sequence for one phase:
>
> ```bash
> uv run $REPO/scripts/deploy_status.py set --sha <SHA> --phase 3 --state run --detail "polling Argo"
> # ... kubectl calls ...
> uv run $REPO/scripts/deploy_status.py set --sha <SHA> --phase 3 --state ok \
>     --detail "3 apps Synced+Healthy at be0b9a2"
> ```
>
> Then the verdict call from the Reporting section above, which closes the run.
>
> ### Then release the deploy lock — always, success or failure
>
> ```bash
> scripts/lock.sh deploy release
> ```
>
> `.githooks/pre-push` took a lock when this push left the machine so that one push travels the
> whole chain before the next starts. **Release it even when the verdict is ❌** — a failed deploy
> is exactly when someone needs to push a fix. Forgetting blocks every other agent until the
> 30-minute TTL expires.
>
> In the detail for a failing phase, call out specifically:
> - a workflow failure, with the failing job and the relevant log lines only;
> - `Synced` with a stale image (Phase 4) — the confusing one;
> - Argo `ComparisonError`, which wedges an app with an empty revision that a hard refresh will not
>   clear; only a new commit fixes it;
> - pods that rolled but are not healthy.
>
> Keep it tight — a status report, not a narrative. If the whole chain is still incomplete after 20
> minutes, `set` the phase you are stuck on to `warn` with what you last saw, write a `verdict`
> saying where it stopped, and stop rather than waiting indefinitely. A partial dashboard still beats
> silence: it shows at a glance how far it got, and the verdict is what stops the launching agent
> reading the run as "died".

## Notes

- Both panes persist after the run finishes so the user can read the result — the dashboard holds its
  final frame. Leave them; the reuse checks in the preconditions pick them both up on the next push,
  and `init` resets the panel for the new sha.
- **Reusing a dashboard pane needs the watcher restarted**, because a finished `watch` has exited to
  a shell prompt. On reuse, run `init` for the new sha and `herdr pane run "$DASH" …` again before
  prompting the agent.
- If the push was to a branch Argo does not track, say so in the prompt — the agent should then watch
  only Phase 1 and skip Phases 2–5.
- A docs-only push still fires `publish-images.yml` and still rebuilds and redeploys every image,
  because the trigger is `paths-ignore: deploy/**` rather than a source-path allowlist. That is
  wasteful but expected; the monitor should not report it as an anomaly.
- Never push *in order to* trigger this. The skill reacts to a push the user asked for.
