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
connectors/n8n/<source>-<what-it-watches>.workflow.ts     ← preferred: SDK source
connectors/n8n/<source>-<what-it-watches>.workflow.json    ← fallback: UI export
```

e.g. `gmail-labeled-receipts.workflow.ts`. One file per workflow; one workflow per thing
watched, so each stays at the three nodes ADR 0008 assumes (Trigger → Set → HTTP Request).

## Prefer SDK source over a JSON export

n8n's official MCP server authors workflows as **`@n8n/workflow-sdk` TypeScript** and pushes
them in with `create_workflow_from_code` / `update_workflow`. That means the committed artifact
can be **real source code** rather than a serialised blob:

```ts
import { workflow, trigger, node } from '@n8n/workflow-sdk';
// … nodes …
export default workflow('id', 'name').add(gmailTrigger).to(toCanonical).to(postToVector);
```

Commit that. It reviews like code and diffs meaningfully, where a workflow JSON diff is mostly
node ids and canvas coordinates. Round-trip through `validate_workflow` before
`update_workflow`, and keep the file the thing you edit — so the repo copy stays the source of
truth rather than drifting behind the instance.

A UI export (**⋯ → Download**) is still the right fallback for a workflow built by hand in the
canvas.

⚠️ **Check either form for secrets before committing.** n8n stores credentials separately and
normally emits only a credential *reference* (id and name), but any secret typed directly into a
node parameter — an API key in a header, a token in a URL — **is** in the output. Read the diff,
don't just `git add`.

## What a connector must do

The contract lives in [`doc/connectors.md`](../doc/connectors.md) — required fields, forbidden
fields, the ~1 MiB ceiling, and why a `200` from ingest does not mean the event was accepted.
The rule that keeps this tier legitimate:

> A connector may authenticate, fetch, and rename fields. It may not decide that something
> happened.

If a mapping needs more than a Set node, it is semantics and belongs in
[`src/inference/`](../src/inference/), not here.

## Building one with the n8n MCP

Workflows are authored through **n8n's official instance-level MCP server** — first-party, 25
tools, at `POST https://<n8n-host>/mcp-server/http`.

Enable it once in n8n at **Settings → Instance-level MCP**, then mint a token on the **Access
Token** tab. Note this is a **different credential from the REST API key** — the API key returns
401 here.

> ⚠️ **It must be reached through the `mcp-remote` stdio bridge, not as a `type: "http"` server**
> (which is how [`.mcp.json`](../.mcp.json) configures it). n8n's MCP server is **POST-only**:
> `GET /mcp-server/http` returns **404**, so there is no server→client SSE stream. Claude Code's
> native HTTP transport requires that stream and treats its absence as fatal — it connects
> successfully, reports `hasTools: true`, then immediately drops with
> `Failed to open SSE stream: Not Found` and exposes **zero tools**. `mcp-remote` hits the same
> 404, logs it, and correctly carries on over POST. (Per the MCP spec the GET stream is optional
> and a server declining it should answer **405**; n8n answering 404 is likely why the strict
> client gives up.)

Credentials follow the same split as every other MCP server in this repo:

| File | Committed? | Holds |
|---|---|---|
| [`.mcp.json`](../.mcp.json) | **yes** | the server *definition*, with `${N8N_API_URL}` / `${N8N_MCP_TOKEN}` placeholders |
| `.claude/settings.local.json` | **no** — gitignored | the *values*, in its `env` block, plus `n8n` in `enabledMcpjsonServers` |

```jsonc
// .claude/settings.local.json  (gitignored — never commit this file)
{
  "env": {
    "N8N_API_URL": "https://<your-n8n-host>",
    "N8N_MCP_TOKEN": "<Settings → Instance-level MCP → Access Token>",
    "N8N_API_KEY": "<Settings → API — only needed for direct REST calls>"
  },
  "enabledMcpjsonServers": ["redis", "aiven", "neon", "n8n"]
}
```

Claude Code injects that `env` block into the process environment, which is what the `${…}`
placeholders expand from. **Not Doppler, and not a K8s secret** — those serve the cluster, and
this is a local developer tool that never runs in a pod. Restart Claude Code after editing; a
newly *added* MCP server is picked up at startup, and `/mcp reconnect` will not do it.

The tools worth knowing: `get_sdk_reference`, `search_nodes` and `get_node_types` (exact
parameter names) for authoring; `validate_workflow` → `create_workflow_from_code` /
`update_workflow` to push; `publish_workflow` to activate. And `prepare_test_pin_data` +
`test_workflow`, which run a workflow against **pinned data** — so a connector can be tested
without calling Gmail and without posting real events into `raw_sensors`.

> The community `czlonkowski/n8n-mcp` server is an alternative (24 tools, JSON-based). It is not
> used here: the official one covers the same ground, adds pin-data testing, needs no local npx
> process, and authors workflows as committable SDK code. It also currently needs a `zod@3.25`
> pin to start at all (`Cannot find module 'zod/v3'`), which surfaces as a server with zero
> tools and no error.

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
