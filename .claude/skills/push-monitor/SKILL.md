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

### 1. Split a pane on the right

```bash
herdr pane split --current --direction right --ratio 0.38 --cwd "$PWD" --no-focus
```

`--no-focus` matters: the user is mid-conversation in the left pane and focus should not jump. Parse
the new id out of the JSON (`.result.pane.pane_id`, e.g. `w3:p3`):

```bash
PANE=$(herdr pane split --current --direction right --ratio 0.38 --cwd "$PWD" --no-focus \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['pane']['pane_id'])")
```

### 2. Start a Claude agent in it

```bash
herdr agent start push-monitor --kind claude --pane "$PANE" --timeout 60000
```

The pane must be at an interactive shell prompt — a freshly split pane is. Success returns
`"interactive_ready": true`. If it fails, close the pane (`herdr pane close "$PANE"`) and fall back
to reporting the SHAs inline; do not leave a dead pane behind.

### 3. Hand it the task

```bash
herdr agent prompt push-monitor "<the prompt below>"
```

Do **not** pass `--wait`. The point is to not block.

### 4. Tell the user, in one line

Name the SHA, what will run, and that the pane on the right is watching it. Then continue or hand
back. **Do not** check on the monitor afterwards unless the user asks — that would reintroduce the
polling this exists to avoid.

## The monitor prompt

Fill in the SHA and branch; keep the rest, it encodes gotchas the agent will otherwise hit.

> Watch the GitHub Actions run for commit `<SHA>` on `<branch>` in `rodis/inference`, then report and
> stop.
>
> **Tooling:** call `/opt/homebrew/bin/gh` by absolute path — the `gh` shell function is wrapped in
> `op plugin run` and dies with "interactive IO not available" in an agent session, and
> `gh --version` still works so it looks fine. The ambient `GH_TOKEN` is read-only, which is all you
> need here.
>
> **What runs:** a push touching anything outside `deploy/**` triggers `publish-images.yml` — the
> `_ci-checks.yml` gate (ruff/pytest/typecheck/drift), then a per-component image build, a
> `values.yml` bump to `sha-<short>`, a commit, and a force-push of `deploy-state`. A push touching
> only `deploy/**` triggers `mirror-deploy-state.yml` instead. Both force-push `deploy-state`, so a
> push containing *both* code and `deploy/**` races and is a bug worth flagging.
>
> **How to watch:** poll with `gh run list --commit <SHA>` / `gh run watch <id>`. Budget ~5–10
> minutes. Do not spin at a tight interval — every ~30–60s is plenty.
>
> **Report when it finishes:**
> - the conclusion of each workflow, and for a failure the failing job plus the relevant log lines
>   (not the whole log);
> - whether `deploy-state` was force-pushed and to which image tag;
> - anything that looks stuck rather than failed — Argo CD `ComparisonError` leaves an app wedged
>   with an empty revision that a hard refresh will not clear, and only a new commit fixes it.
>
> Keep it short. One paragraph on success. If it is still running after 15 minutes, say so and stop
> rather than waiting indefinitely.

## Notes

- The pane persists after the run finishes so the user can read the result. Leave it; the reuse check
  in the preconditions will pick it up on the next push.
- If the push was to a branch Argo does not track, say so in the prompt — the agent should then watch
  only the workflow and skip the deploy-state and Argo parts.
- Never push *in order to* trigger this. The skill reacts to a push the user asked for.
