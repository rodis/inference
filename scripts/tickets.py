"""Read and write the backlog — the shared queue of open work, so agents don't hold it in memory.

Several Claude sessions run against this repo at once, and each one used to end with suggestions
and pending decisions living only in that session's memory. There was no way to see what was
still open. Open work now lives in a **tracker**, and this is the seam onto it:

  GitHub Projects v2 board "Aware — inference backlog", backed by issues in rodis/inference.

The rule that makes it worth having: **when work surfaces a new open thread, file a ticket.**
Memory is for durable facts and gotchas; the board is for open work. The two drifted badly
before the board existed — seeding it found three memory notes describing problems already
fixed in code, and one "TODO" that was a deliberate won't-do.

Two environment quirks this hides, both of which cost a session to find:

  1. `gh` in an interactive shell is a FUNCTION wrapping `op plugin run` (1Password), which
     wants a biometric prompt and dies non-interactively. Irrelevant here — subprocess resolves
     `gh` on PATH to the real binary, never the shell function — but don't "fix" this by calling
     `gh` through a shell.
  2. The ambient GH_TOKEN is a fine-grained PAT that is READ-ONLY for issues/labels/projects
     (403 on every write). Writes use GH_BACKLOG_TOKEN, a classic token with `repo`+`project`.
     Note `gh project` subcommands additionally demand `read:org`, which that token lacks — so
     board access goes through `gh api graphql`, where `project` alone suffices.

Usage:
  uv run python scripts/tickets.py list                        # the whole queue, by status
  uv run python scripts/tickets.py list --status Ready
  uv run python scripts/tickets.py list --area engines --kind bug
  uv run python scripts/tickets.py view 7
  uv run python scripts/tickets.py new --title "..." --area engines --kind bug \
      --altitude Activity --status Backlog --body-file /tmp/body.md
  uv run python scripts/tickets.py move 7 --status "In progress"
  uv run python scripts/tickets.py close 7 --comment "fixed in abc1234"
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = "rodis/inference"
BOARD_TITLE = "Aware — inference backlog"

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / ".claude" / "settings.local.json"

STATUSES = ["Backlog", "Blocked", "Ready", "In progress", "Done"]
AREAS = [
    "runtime", "engines", "vector", "dashboard",
    "bmw-cardata", "location", "infra", "docs", "tooling",
]
KINDS = ["bug", "feature", "chore", "research", "decision", "doc"]
ALTITUDES = ["Experience", "Activity", "Micro", "Signal", "Platform"]


# ---------------------------------------------------------------- plumbing


def _token() -> str:
    """The classic repo+project token: env first, else .claude/settings.local.json.

    The file fallback matters because Claude Code applies `env` from settings at session
    start, so a token added mid-session isn't in os.environ yet.
    """
    token = os.environ.get("GH_BACKLOG_TOKEN", "").strip()
    if token:
        return token
    try:
        settings = json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return ""
    return str(settings.get("env", {}).get("GH_BACKLOG_TOKEN", "")).strip()


def _env() -> dict[str, str]:
    """Always the classic token — the ambient fine-grained PAT can neither write issues nor
    read Projects v2 at all, so there is no useful read-only fallback to degrade to."""
    token = _token()
    if not token:
        sys.exit(
            "GH_BACKLOG_TOKEN is not set. The ambient GH_TOKEN is a fine-grained PAT that\n"
            "cannot write issues or read the project board.\n"
            f"Put a classic token with repo+project scopes in {SETTINGS} under env."
        )
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    env.pop("GITHUB_TOKEN", None)
    return env


def gh(*args: str) -> str:
    """Run a gh subcommand and return stdout. Resolves the binary, not the shell wrapper."""
    exe = shutil.which("gh")
    if not exe:
        sys.exit("gh is not installed.")
    proc = subprocess.run(
        [exe, *args], capture_output=True, text=True, encoding="utf-8", env=_env()
    )
    if proc.returncode != 0:
        sys.exit(f"gh {' '.join(args[:2])} failed:\n  {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout


def gql(query: str) -> dict:
    out = gh("api", "graphql", "-f", f"query={query}")
    payload = json.loads(out) if out.strip() else {}
    if "errors" in payload:
        sys.exit(f"GraphQL error:\n  {payload['errors']}")
    return payload.get("data", {})


def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------- board


BOARD_QUERY = """
query { viewer { projectsV2(first: 20) { nodes { id title url
  fields(first: 30) { nodes {
    ... on ProjectV2SingleSelectField { id name options { id name } }
  } }
  items(first: 100) { nodes { id
    content { ... on Issue { number title state url comments { totalCount }
      labels(first: 10) { nodes { name } } } }
    fieldValues(first: 12) { nodes {
      ... on ProjectV2ItemFieldSingleSelectValue {
        name field { ... on ProjectV2FieldCommon { name } } }
    } }
  } }
} } } }
"""


def load_board() -> dict:
    """The board plus every item, flattened into {number: {...}} for easy filtering."""
    for node in gql(BOARD_QUERY)["viewer"]["projectsV2"]["nodes"]:
        if node["title"] != BOARD_TITLE:
            continue
        items = {}
        for item in node["items"]["nodes"]:
            content = item.get("content") or {}
            if not content.get("number"):
                continue  # a draft item, not a real issue
            fields = {
                v["field"]["name"]: v["name"]
                for v in item["fieldValues"]["nodes"] if v
            }
            items[content["number"]] = {
                "item_id": item["id"],
                "title": content["title"],
                "state": content["state"],
                "url": content["url"],
                "comments": (content.get("comments") or {}).get("totalCount", 0),
                "fields": fields,
            }
        return {
            "id": node["id"],
            "url": node["url"],
            "fields": {f["name"]: f for f in node["fields"]["nodes"] if f},
            "items": items,
        }
    sys.exit(f"No board titled {BOARD_TITLE!r} found for the authenticated user.")


def set_field(board: dict, item_id: str, field_name: str, value: str) -> None:
    field = board["fields"].get(field_name)
    if not field:
        sys.exit(f"Board has no field {field_name!r}.")
    option = next((o for o in field["options"] if o["name"] == value), None)
    if not option:
        allowed = ", ".join(o["name"] for o in field["options"])
        sys.exit(f"{field_name} has no option {value!r}. Allowed: {allowed}")
    gql(
        'mutation { updateProjectV2ItemFieldValue(input: {projectId: "%s", itemId: "%s", '
        'fieldId: "%s", value: {singleSelectOptionId: "%s"}}) { projectV2Item { id } } }'
        % (board["id"], item_id, field["id"], option["id"])
    )


# ---------------------------------------------------------------- commands


def cmd_list(args: argparse.Namespace) -> None:
    board = load_board()
    rows = []
    for number, it in board["items"].items():
        f = it["fields"]
        if args.status and f.get("Status") != args.status:
            continue
        if args.area and f.get("Area") != args.area:
            continue
        if args.kind and f.get("Kind") != args.kind:
            continue
        if not args.all and it["state"] != "OPEN":
            continue
        rows.append((number, it))

    if not rows:
        print("No tickets match.")
        return

    order = {s: i for i, s in enumerate(STATUSES)}
    rows.sort(key=lambda r: (order.get(r[1]["fields"].get("Status", ""), 99), r[0]))

    current = None
    for number, it in rows:
        status = it["fields"].get("Status", "—")
        if status != current:
            current = status
            print(f"\n{status.upper()}")
        tags = "/".join(
            filter(None, (it["fields"].get("Area"), it["fields"].get("Kind")))
        )
        # Flag discussion: a comment often carries the answer to a question the body
        # only poses, so it must be obvious there is more to read than the body.
        n = it.get("comments", 0)
        discussion = f"  ({n} comment{'s' if n != 1 else ''})" if n else ""
        print(f"  #{number:<3} [{tags}] {it['title']}{discussion}")
    print(f"\n{len(rows)} ticket(s) — {board['url']}")


def cmd_view(args: argparse.Namespace) -> None:
    """Print a ticket AND its comments.

    Comments are included by default and that is the whole point: a ticket often poses a
    design question whose answer arrives later, in a comment, from a different session.
    `gh issue view` shows only a bare `comments: N` count, so reading the body alone would
    silently miss the answer — the exact failure the board exists to prevent.
    """
    board = load_board()
    it = board["items"].get(args.number)
    if it:
        f = it["fields"]
        meta = "  ".join(
            f"{k}={f[k]}" for k in ("Status", "Area", "Kind", "Altitude") if k in f
        )
        print(f"{meta}\n")
    print(gh("issue", "view", str(args.number), "--repo", REPO))

    if args.no_comments:
        return
    payload = json.loads(
        gh("issue", "view", str(args.number), "--repo", REPO, "--json", "comments")
    )
    comments = payload.get("comments") or []
    if not comments:
        return
    print(f"\n{'─' * 60}\ncomments ({len(comments)})")
    for c in comments:
        who = (c.get("author") or {}).get("login", "unknown")
        when = (c.get("createdAt") or "")[:10]
        print(f"\n  ── {who} · {when} ─────────────────────────────")
        for line in (c.get("body") or "").strip().splitlines():
            print(f"  {line}")


def cmd_new(args: argparse.Namespace) -> None:
    body = args.body or ""
    if args.body_file:
        body = Path(args.body_file).read_text()
    if not body.strip():
        sys.exit("A ticket needs a body — pass --body or --body-file.")

    url = gh(
        "issue", "create", "--repo", REPO,
        "--title", args.title, "--body", body,
        "--label", f"area:{args.area}", "--label", f"kind:{args.kind}",
    ).strip()
    meta = json.loads(gh("issue", "view", url, "--repo", REPO, "--json", "id,number"))

    board = load_board()
    added = gql(
        'mutation { addProjectV2ItemById(input: {projectId: "%s", contentId: "%s"}) '
        "{ item { id } } }" % (board["id"], meta["id"])
    )
    item_id = added["addProjectV2ItemById"]["item"]["id"]

    for field_name, value in (
        ("Area", args.area), ("Kind", args.kind),
        ("Altitude", args.altitude), ("Status", args.status),
    ):
        if value:
            set_field(board, item_id, field_name, value)

    print(f"#{meta['number']} {url}")


def cmd_comment(args: argparse.Namespace) -> None:
    """Append a comment — the place to record an answer, a finding, or a decision.

    Prefer this over editing the body: a comment is dated and attributed, so the ticket
    keeps the order in which things were settled instead of silently rewriting history.
    """
    body = args.body or ""
    if args.body_file:
        body = Path(args.body_file).read_text()
    if not body.strip():
        sys.exit("A comment needs a body — pass --body or --body-file.")
    print(gh("issue", "comment", str(args.number), "--repo", REPO, "--body", body).strip())


def cmd_move(args: argparse.Namespace) -> None:
    board = load_board()
    it = board["items"].get(args.number)
    if not it:
        sys.exit(f"#{args.number} is not on the board.")
    set_field(board, it["item_id"], "Status", args.status)
    print(f"#{args.number} -> {args.status}")


def cmd_close(args: argparse.Namespace) -> None:
    extra = ["--comment", args.comment] if args.comment else []
    gh("issue", "close", str(args.number), "--repo", REPO, *extra)
    board = load_board()
    it = board["items"].get(args.number)
    if it:
        set_field(board, it["item_id"], "Status", "Done")
    print(f"#{args.number} closed -> Done")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="show the queue, grouped by status")
    p.add_argument("--status", choices=STATUSES)
    p.add_argument("--area", choices=AREAS)
    p.add_argument("--kind", choices=KINDS)
    p.add_argument("--all", action="store_true", help="include closed tickets")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("view", help="show one ticket, its board fields, and its comments")
    p.add_argument("number", type=int)
    p.add_argument(
        "--no-comments", action="store_true",
        help="body only — comments are shown by default because they often carry the answer",
    )
    p.set_defaults(func=cmd_view)

    p = sub.add_parser("comment", help="append a comment to a ticket")
    p.add_argument("number", type=int)
    p.add_argument("--body")
    p.add_argument("--body-file")
    p.set_defaults(func=cmd_comment)

    p = sub.add_parser("new", help="file a ticket and put it on the board")
    p.add_argument("--title", required=True)
    p.add_argument("--body")
    p.add_argument("--body-file")
    p.add_argument("--area", required=True, choices=AREAS)
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--altitude", choices=ALTITUDES)
    p.add_argument("--status", choices=STATUSES, default="Backlog")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("move", help="change a ticket's status")
    p.add_argument("number", type=int)
    p.add_argument("--status", required=True, choices=STATUSES)
    p.set_defaults(func=cmd_move)

    p = sub.add_parser("close", help="close a ticket and mark it Done")
    p.add_argument("number", type=int)
    p.add_argument("--comment")
    p.set_defaults(func=cmd_close)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
