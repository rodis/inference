# connectors/ — versioned records of the n8n workflows

**These files are not deployed from here.** They are exported records of workflows that run in
the self-managed n8n instance, committed so the tier is reviewable, diffable and restorable.
Editing a file here changes nothing; edit the workflow in n8n and re-export.

Why bother, then? Because "connector logic is not in git" is the one real cost of
[ADR 0008](../doc/adr/0008-connector-tier-via-n8n.md), and an export recovers most of what git
was giving us — code review, a diff when behaviour changes, and disaster recovery — for the price
of one file. A lost n8n instance becomes a re-import rather than a re-derivation.

## Layout

```
connectors/n8n/<source>-<what-it-watches>.workflow.json
```

e.g. `gmail-labeled-receipts.workflow.json`. One file per workflow; one workflow per thing
watched, so each stays at the three nodes ADR 0008 assumes (Trigger → Set → HTTP Request).

## Exporting

In n8n: open the workflow → **⋯ → Download**. Commit the result unchanged.

⚠️ **Check the export for secrets before committing.** n8n stores credentials separately from
workflows and normally exports only a credential *reference* (an id and name), but any secret
typed directly into a node parameter — an API key in a header, a token in a URL — **is** in the
JSON. Read the diff, don't just `git add`.

## What a connector must do

The contract lives in [`doc/connectors.md`](../doc/connectors.md) — required fields, forbidden
fields, the ~1 MiB ceiling, and why a `200` from ingest does not mean the event was accepted.
The rule that keeps this tier legitimate:

> A connector may authenticate, fetch, and rename fields. It may not decide that something
> happened.

If a mapping needs more than a Set node, it is semantics and belongs in
[`src/inference/`](../src/inference/), not here.

## Building one with the n8n MCP

An `n8n` MCP server is registered in [`.mcp.json`](../.mcp.json) so workflows can be authored
and inspected from here rather than clicked together by hand. Credentials follow the same split
as every other MCP server in this repo:

| File | Committed? | Holds |
|---|---|---|
| [`.mcp.json`](../.mcp.json) | **yes** | the server *definition*, with `${N8N_API_URL}` / `${N8N_API_KEY}` placeholders |
| `.claude/settings.local.json` | **no** — gitignored | the *values*, in its `env` block, plus `n8n` in `enabledMcpjsonServers` |

Claude Code injects that `env` block into the process environment, which is what the `${…}`
placeholders expand from. So the values go here, alongside the existing `AIVEN_TOKEN` and
`GH_BACKLOG_TOKEN`:

```jsonc
// .claude/settings.local.json  (gitignored — never commit this file)
{
  "env": {
    "N8N_API_URL": "https://<your-n8n-host>",
    "N8N_API_KEY": "<key from n8n: Settings → API>"
  },
  "enabledMcpjsonServers": ["redis", "aiven", "neon", "n8n"]
}
```

**Not Doppler, and not a K8s secret** — those serve the cluster, and this MCP server is a local
developer tool that never runs in a pod. Restart Claude Code after editing.

Both vars are **optional**: node search, templates and documentation work without them; they are
needed only to create, deploy or test workflows in the instance. The `env` block takes literal
strings only — no command substitution — so if you would rather not keep an API key on disk,
export the two vars from your shell profile via `op read` instead and leave them out of the file.

n8n also ships a native instance-level MCP server (public preview since April 2026). Worth
switching to if the community server proves limiting — it would be an `{"type": "http", "url":
…}` entry instead, like the `neon` one.

## Checking one is healthy

```bash
NEON_DATABASE_URL=... uv run python scripts/connector_eval.py --days 7
```

Reports latency (trigger lag vs pipeline lag), duplicates, freshness and contract compliance.
Exit code is non-zero when something needs attention. Completeness is the one thing it cannot
check — compare a count at the source against what landed.
