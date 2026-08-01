#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["rich>=13"]
# ///
"""The deploy monitor's display: a live dashboard the watching agent cannot reword.

Backs the `push-monitor` skill. That skill splits a pane, drops a Claude agent in it, and asks
it to track a push through five phases (CI -> deploy-state -> Argo -> image -> rollout) and
render a glanceable status block. The rendering half of that arrangement does not work, and
could not:

  1. **A chat agent's tool output is collapsed in its own pane.** The pane shows
     `Ran 1 shell command` with nothing under it, so anything a script prints *from inside the
     agent* is invisible. The agent's only visible surface is its final assistant message.
  2. **That message is prose, and prose drifts.** Observed 2026-08-01: the mandated summary box
     was dropped entirely, `✅` came out as `✓` and once as the bare word `OK`, and the ~70-col
     pane wrapped every one-line-per-phase row into three. The format exists to be glanceable;
     all three failures destroy exactly that.
  3. **The drift is not only cosmetic.** The launching agent decides whether to interrupt the
     user by grepping the pane for `❌|⚠️`. A warning rendered as `WARN` reads as green, so the
     one check that exists to catch a bad deploy silently passes it.

So the display stops being something the model *writes* and becomes something it *updates*: the
agent calls `set` after each phase, and a plain pane — no agent, no tokens — runs `watch` and
draws the state with rich. The visual is then deterministic, correctly sized, and identical on
every run, and `check` gives the launcher a verdict from the JSON instead of from glyphs.

The state file is the contract between the two panes:

    ${XDG_CACHE_HOME:-~/.cache}/inference-deploy/<sha>.json

A fixed path (not the session scratchpad) because the two panes are different processes with
different cwds — the agent's cwd is whichever worktree pushed, and the watcher is started by the
launcher from somewhere else entirely.

Usage:

    deploy_status.py init  --sha <sha> [--branch <b>] [--phases CI,deploy-state,...]
    deploy_status.py set   --sha <sha> --phase <n> --state ok|warn|fail|run|skip|wait
                           [--detail "..."]
    deploy_status.py verdict --sha <sha> --text "..."
    deploy_status.py watch --sha <sha> [--timeout 1800] [--once]
    deploy_status.py check --sha <sha>      # exit 0 green, 1 trouble, 2 incomplete/missing

`uv run` resolves the PEP 723 header above into an ephemeral env, so rich needs no install and
never enters the project's dependencies — this is a developer display, not runtime code.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

DEFAULT_PHASES = ("CI", "deploy-state", "Argo", "image", "rollout")

# state -> (glyph, rich style). `run` is drawn as a spinner instead of a glyph.
STATES = {
    "wait": ("·", "dim"),
    "run": ("", "cyan"),
    "ok": ("✅", "green"),
    "warn": ("⚠️", "yellow"),
    "fail": ("❌", "red bold"),
    "skip": ("⏭️", "dim"),
}
TERMINAL = {"ok", "warn", "fail", "skip"}
TROUBLE = {"warn", "fail"}


def state_dir() -> Path:
    cache = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(cache) / "inference-deploy"


def state_path(sha: str) -> Path:
    return state_dir() / f"{sha}.json"


def load(sha: str) -> dict | None:
    path = state_path(sha)
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        # A partial read races the writer's os.replace only in theory (replace is atomic), but a
        # missing file is entirely normal: `watch` is started before the agent's first `set`.
        return None


def save(sha: str, doc: dict) -> None:
    path = state_path(sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2))
    tmp.replace(path)  # atomic, so the watcher never sees a half-written file


def blank(sha: str, branch: str, phases: tuple[str, ...]) -> dict:
    return {
        "sha": sha,
        "branch": branch,
        "started_at": time.time(),
        "verdict": None,
        "phases": [
            {"n": i, "label": label, "state": "wait", "detail": "", "at": None}
            for i, label in enumerate(phases, start=1)
        ],
    }


def cmd_init(args: argparse.Namespace) -> int:
    phases = tuple(p.strip() for p in args.phases.split(",")) if args.phases else DEFAULT_PHASES
    save(args.sha, blank(args.sha, args.branch, phases))
    print(state_path(args.sha))
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    doc = load(args.sha) or blank(args.sha, args.branch or "", DEFAULT_PHASES)
    for phase in doc["phases"]:
        if phase["n"] == args.phase:
            phase["state"] = args.state
            if args.detail is not None:
                phase["detail"] = args.detail
            if args.label:
                phase["label"] = args.label
            phase["at"] = time.time()
            break
    else:
        print(f"no phase {args.phase} (have 1..{len(doc['phases'])})", file=sys.stderr)
        return 2
    save(args.sha, doc)
    return 0


def cmd_verdict(args: argparse.Namespace) -> int:
    doc = load(args.sha)
    if doc is None:
        print(f"no state for {args.sha}", file=sys.stderr)
        return 2
    doc["verdict"] = args.text
    save(args.sha, doc)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """The launcher's trouble check — a verdict from data, not from glyphs in a scrollback."""
    doc = load(args.sha)
    if doc is None:
        print(f"missing: no state file for {args.sha}")
        return 2
    bad = [p for p in doc["phases"] if p["state"] in TROUBLE]
    unfinished = [p for p in doc["phases"] if p["state"] not in TERMINAL]
    if bad:
        worst = "fail" if any(p["state"] == "fail" for p in bad) else "warn"
        print(f"{worst}: " + "; ".join(f"[{p['n']}] {p['label']} — {p['detail']}" for p in bad))
        return 1
    if unfinished:
        first = unfinished[0]
        print(
            f"incomplete: stopped at [{first['n']}] {first['label']} ({first['state']}), "
            f"{len(unfinished)} phase(s) unfinished"
        )
        return 2
    if not doc.get("verdict"):
        print("incomplete: all phases terminal but no verdict written")
        return 2
    print(f"green: {doc['verdict']}")
    return 0


SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _fit(text: str, cells: int) -> str:
    """Pad or ellipsis-truncate to an exact cell count (emoji are two cells wide)."""
    from rich.cells import cell_len

    if cell_len(text) <= cells:
        return text + " " * (cells - cell_len(text))
    out = ""
    for ch in text:
        if cell_len(out + ch) > cells - 1:
            break
        out += ch
    return out + "…" + " " * max(0, cells - cell_len(out) - 1)


def render(doc: dict | None, sha: str, width: int = 80):
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    if doc is None:
        return Panel(
            Text("waiting for the monitor's first phase…", style="dim"),
            title=f"[bold]{sha[:7]}[/]",
            border_style="dim",
        )

    # Lay the rows out by hand instead of with a Table. rich shrinks a grid to fit by squeezing
    # columns, and it squeezes the *fixed* ones first: at ~48 cols a table dropped the 2-cell
    # glyph column outright while keeping the detail text. That inverts the priority — the glyph
    # is the entire point of a glance, and the detail is the part that can afford an ellipsis. So
    # the mark gets its cells first here, and the detail absorbs whatever is left.
    inner = max(24, width - 4)  # panel border + padding, both sides
    label_w = 15 if inner >= 46 else 9
    detail_w = max(6, inner - 2 - 1 - label_w - 1)

    rows = []
    frame = SPINNER_FRAMES[int(time.time() * 8) % len(SPINNER_FRAMES)]
    for phase in doc["phases"]:
        glyph, style = STATES.get(phase["state"], ("?", "magenta"))
        mark = frame if phase["state"] == "run" else glyph
        detail_style = "dim" if phase["state"] == "wait" else style
        rows.append(
            Text.assemble(
                (_fit(mark, 2), "cyan" if phase["state"] == "run" else style),
                " ",
                (_fit(f"{phase['n']} {phase['label']}", label_w), style if phase["state"] in TROUBLE else ""),
                " ",
                (_fit(phase["detail"] or "", detail_w), detail_style),
            )
        )
    grid = Group(*rows)

    elapsed = int(time.time() - doc.get("started_at", time.time()))
    subtitle = f"{elapsed // 60}m{elapsed % 60:02d}s"
    if doc.get("verdict"):
        subtitle = f"{subtitle} · done"

    worst = "green"
    if any(p["state"] == "fail" for p in doc["phases"]):
        worst = "red"
    elif any(p["state"] == "warn" for p in doc["phases"]):
        worst = "yellow"
    elif not all(p["state"] in TERMINAL for p in doc["phases"]):
        worst = "cyan"

    head = f"[bold]{doc['sha'][:7]}[/]"
    if doc.get("branch"):
        head += f" [dim]·[/] [dim]{doc['branch']}[/]"
    return Panel(grid, title=head, subtitle=f"[dim]{subtitle}[/]", border_style=worst)


def cmd_watch(args: argparse.Namespace) -> int:
    from rich.console import Console
    from rich.live import Live

    console = Console()
    if args.once:
        console.print(render(load(args.sha), args.sha, console.width))
        return 0

    deadline = time.time() + args.timeout
    settled_at: float | None = None
    with Live(console=console, refresh_per_second=8, screen=False) as live:
        while True:
            doc = load(args.sha)
            live.update(render(doc, args.sha, console.width))

            done = doc is not None and doc.get("verdict") and all(
                p["state"] in TERMINAL for p in doc["phases"]
            )
            if done:
                # Linger briefly so the final frame is unmistakably the final frame, then drop
                # out of Live and leave the panel painted in the scrollback for the user.
                settled_at = settled_at or time.time()
                if time.time() - settled_at > 1.0:
                    break
            if time.time() > deadline:
                break
            time.sleep(0.25)

    console.print(render(load(args.sha), args.sha, console.width))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    def with_sha(p):
        p.add_argument("--sha", required=True)
        return p

    p_init = with_sha(sub.add_parser("init", help="create the state file, all phases pending"))
    p_init.add_argument("--branch", default="")
    p_init.add_argument("--phases", default="", help="comma-separated labels (default: 5 phases)")
    p_init.set_defaults(func=cmd_init)

    p_set = with_sha(sub.add_parser("set", help="update one phase"))
    p_set.add_argument("--phase", type=int, required=True)
    p_set.add_argument("--state", required=True, choices=sorted(STATES))
    p_set.add_argument("--detail", default=None)
    p_set.add_argument("--label", default=None)
    p_set.add_argument("--branch", default=None)
    p_set.set_defaults(func=cmd_set)

    p_verdict = with_sha(sub.add_parser("verdict", help="record the closing verdict line"))
    p_verdict.add_argument("--text", required=True)
    p_verdict.set_defaults(func=cmd_verdict)

    p_watch = with_sha(sub.add_parser("watch", help="live dashboard (run this in its own pane)"))
    p_watch.add_argument("--timeout", type=float, default=1800.0)
    p_watch.add_argument("--once", action="store_true", help="render one frame and exit")
    p_watch.set_defaults(func=cmd_watch)

    p_check = with_sha(sub.add_parser("check", help="0 green, 1 trouble, 2 incomplete/missing"))
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
