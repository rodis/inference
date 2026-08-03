---
name: worktree-land
description: Land a worktree's branch — merge it into the primary checkout, run the checks, split the push if it mixes deploy/** with code, push once, and hand off to the push monitor. Use when a worktree's work is ready to ship ("land this branch", "merge and push the worktree", "ship what's in <worktree>"). This is the ONLY way a worktree's work reaches the remote; the pre-push hook refuses pushes from linked worktrees.
---

# Worktree land

> **This skill lives in the repo (`inference/.claude/skills/`), not in `~/.claude/skills/`, and that
> placement is the point.** Landing merges into the primary checkout and pushes to the single deploy
> target — something only the machine holding that checkout may do, and something entirely specific to
> *this* repo's push rules (the `deploy` lock, the mixed-path split, the pre-push hook). A repo-local
> skill travels with the repo through git and is scoped to it automatically, so it cannot leak onto the
> dev boxes the way a global one can: `worktree-land` was rsynced to `awsbox` and `dhbox` by mistake on
> 2026-08-03 precisely because it sat in the global dir. It shares that placement with
> [[push-monitor]], for the same reason.
>
> Its siblings [[worktree-handoff]] and [[worktree-remove]] stay **global** — creating and tearing down
> a worktree is machine-local and repo-agnostic, so they are useful on any box and are synced to both.
> Consequence: their `[[worktree-land]]` cross-references resolve only inside this repo, which is
> correct — on a dev box there is nothing for them to point at.

The third leg of the worktree workflow: [[worktree-handoff]] creates, this ships, [[worktree-remove]]
tears down.

**Why it exists.** Agents develop in parallel across worktrees, but there is exactly **one deploy
target**, so deployment is serial. A linked worktree cannot push at all — `.githooks/pre-push`
refuses it — because two worktrees pushing concurrently is a coin flip over whose change ships. Work
reaches the remote by being merged into the primary checkout and pushed from there, one deploy cycle
at a time.

Nothing here is exotic git. The value is doing the four checks that are easy to skip and expensive to
get wrong: the lock, the checks, the mixed-path split, and the handoff to the monitor.

## Steps

### 1. Paths and branch

- `MAIN_REPO`: the primary checkout (e.g. `/Users/rods/Development/inference`).
- `WORKTREE`: `$MAIN_REPO.worktrees/<name>`.
- `BRANCH`: `git -C "$WORKTREE" branch --show-current`.

Abort if `WORKTREE` resolves to `MAIN_REPO`, or if the branch is the primary's own (`main`) — there is
nothing to land.

### 2. Check the worktree is ready

```bash
git -C "$WORKTREE" status --porcelain                       # must be empty
git -C "$WORKTREE" log --oneline origin/main.."$BRANCH"     # what will land
```

- **Uncommitted changes** → STOP. They will not be landed and the user probably thinks they will.
  Ask whether to commit them first or land without them.
- **Nothing to land** (empty log) → say so and stop; the branch is already in `main`.

### 3. Check the deploy lock *before* merging

```bash
sh "$MAIN_REPO/scripts/lock.sh" deploy status
```

Merging is safe whatever this says — the lock only gates the push. But a deploy in flight means the
push at step 7 will be refused, so surface it now rather than after the merge:

- `free` → proceed.
- `HELD` by a **live** pane → another push is mid-cycle. Offer to merge now and push when it clears,
  or to wait. Do **not** release someone else's lock.
- `HELD` with `(STALE — holder pane … is gone)` → nothing to do; `lock.sh` breaks it on the next
  acquire.

### 4. Bring the primary up to date

```bash
git -C "$MAIN_REPO" fetch origin
git -C "$MAIN_REPO" pull --ff-only
```

If `--ff-only` refuses, the primary has diverged — stop and show the user rather than forcing.

### 5. Merge, from the primary

```bash
git -C "$MAIN_REPO" merge --no-edit "$BRANCH"
```

Merging a branch that is **checked out in another worktree** is fine — verified: git updates `main`
in the primary and the worktree stays on its own branch, untouched. You are not modifying the branch.

**On conflict**, do not resolve it here. The worktree's agent has the context; the primary does not:

```bash
git -C "$MAIN_REPO" merge --abort        # leaves the primary clean — verified
```

Then tell that agent to rebase onto the updated main **in its own worktree** and come back:

```bash
git -C "$WORKTREE" fetch origin && git -C "$WORKTREE" rebase origin/main
```

This is the accepted cost of parallel worktrees: the second one to land rebases.

### 6. Run the checks before spending a deploy cycle

```bash
(cd "$MAIN_REPO" && uv run ruff check . && uv run pytest -q)
```

**The subshell is load-bearing — never a bare `cd "$MAIN_REPO"`.** The Bash tool's working directory
persists across calls, so a bare `cd` here strands the session in the primary checkout for the rest
of the turn. Every step after this one uses `git -C` and so still works, which is exactly what makes
it dangerous: nothing fails, and the *next* thing the agent does — a test run, an edit, a `git
status` — silently addresses `main` instead of the branch it was working on. Parentheses confine the
`cd` to the checks; the venv, `pyproject.toml` and `pytest.ini` all resolve from `$MAIN_REPO` inside
it, which is the only reason the `cd` is needed at all.

CI gates the deploy anyway (`checks` blocks `bump-manifests`, so red code never ships), so this is
not about safety — it is about not burning a 5–10 minute cycle *and* the deploy lock to learn
something `pytest` would have said in seconds. If it fails, the merge stays local; fix it on `main`
or reset and send it back to the worktree.

### 7. Split the push if it mixes `deploy/**` with code

`publish-images.yml` (`paths-ignore: deploy/**`) and `mirror-deploy-state.yml` (`paths: deploy/**`)
now share a concurrency group, so they queue rather than race — but the hook still refuses a mixed
push, and splitting keeps each run's manifests describing its own commit.

```bash
git -C "$MAIN_REPO" diff --name-only origin/main..main | grep -q '^deploy/'   && HAS_DEPLOY=1
git -C "$MAIN_REPO" diff --name-only origin/main..main | grep -qv '^deploy/'  && HAS_CODE=1
```

If both, check whether the commits are **separable** — i.e. no single commit touches both:

```bash
for c in $(git -C "$MAIN_REPO" rev-list origin/main..main); do
  paths=$(git -C "$MAIN_REPO" show --name-only --format= "$c")
  d=$(printf '%s\n' "$paths" | grep -c '^deploy/'); k=$(printf '%s\n' "$paths" | grep -vc '^deploy/')
  [ "$d" -gt 0 ] && [ "$k" -gt 0 ] && echo "MIXED COMMIT: $c"
done
```

- **Separable** → push the last code-only commit first, then the rest. Code first: a `deploy/**`
  mirror carries the existing image tag forward, so it must not run before the image exists.

  ```bash
  git -C "$MAIN_REPO" push origin <last-code-only-sha>:main
  # wait for the monitor's verdict — the deploy lock enforces this anyway
  git -C "$MAIN_REPO" push origin main
  ```

  Each push is its own deploy cycle, so this means two monitor runs. That is correct, not wasteful.

- **A single commit mixes both** → it cannot be split by pushing a prefix. Tell the user, and offer
  to split the commit (`git rebase -i`, edit, `git reset HEAD^`, commit the two halves separately).
  Do not reach for `--no-verify`; the hook is describing a real race, not being fussy.

### 8. Push

```bash
git -C "$MAIN_REPO" push origin main
```

The hook takes the `deploy` lock as part of this. If the push is refused, read the message — it is
one of the three rules and each says what to do.

### 9. Hand off to the monitor

Invoke [[push-monitor]] immediately, unprompted, as that skill requires. It watches CI → deploy-state
→ Argo → pods **and releases the `deploy` lock on its verdict**. Without it the lock sits until the
30-minute TTL and the next agent is blocked for no reason.

### 10. End where you started — in the worktree

Landing is something the worktree's session does *to* the primary checkout; it is not a move into it.
The session was in `$WORKTREE` when the skill began and must be there when it ends, because the work
continues there — the branch is still checked out, the user's next request is about it, and its venv
and `node_modules` are the ones that matter.

```bash
cd "$WORKTREE" && pwd        # assert, don't assume
```

Cheap insurance even when every step above used `git -C` and a subshell: a stray `cd` anywhere in the
run leaks into every later call, and the symptom is not an error — it is a later command quietly
operating on `main`. Check it rather than trusting that nothing leaked.

### 11. Tell the user, and mention the worktree

One or two lines: what landed (branch, commit count, SHA), that the monitor is watching it, and that
the worktree still exists. Do not remove it — that is [[worktree-remove]], and the user may have more
work in flight there.

## Notes

- **Never `git push --no-verify`** to get around a refusal here unless the user explicitly asks. Each
  of the three rules encodes a race that is silent when it bites.
- **The mixed-path trap is easy to fall into even when you are trying not to.** On 2026-08-01 a push
  believed to be docs-only (`f567536`) in fact carried a `deploy/**` deletion: `git rm` had staged it
  earlier, and a later targeted `git add doc/... && git commit` swept the staged deletion in with it.
  The split was described as clean and was not. Check `git diff --name-only origin/main..main`
  against the *staged* reality rather than against what you intended to commit — which is exactly
  what step 7 does, and what the hook now catches.
- **Do not delete the branch** after landing. `worktree-remove` handles branch lifecycle, and the
  worktree still has it checked out.
- If the worktree's agent is still running, tell it the branch landed so it rebases before continuing
  — otherwise its next commits sit on a stale base and the next land conflicts.
- Landing several worktrees in a row: each is a full cycle. Wait for the monitor's verdict between
  them rather than queueing pushes — the lock will enforce it, but queueing just means the second
  agent sits at a refusal it cannot act on.
