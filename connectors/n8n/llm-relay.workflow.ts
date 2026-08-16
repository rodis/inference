/**
 * Relay: LLM question  (ADR 0008 + ADR 0012)
 *
 * The process tier's reading transport. The reconciler POSTs a fully-composed system message
 * and prompt; this forwards them to Gemini with n8n's stored credential and returns the reply
 * untouched.
 *
 * THE BOUNDARY, and it is doing real work here:
 *
 *   A relay may AUTHENTICATE and TRANSMIT.
 *   It may not compose, decide, or interpret.
 *
 * The temptation with an LLM node is enormous — it is one text field away from being "ask
 * Gemini whether the payment was submitted", which is exactly what the retired
 * `Workflow: Check Payment Was Submitted` did, right down to a hardcoded `invoice 03-2026` in
 * the prompt. That is the design ADR 0012 replaced: the question belongs beside the stage that
 * asks it, in `processes/*.yml`, so it is versioned, reviewable and testable. **Nothing in this
 * file knows what an invoice is, or that the answer decides anything.** It maps two fields onto
 * Gemini's request shape, which is renaming, not composing.
 *
 * Consequently the prompt is NOT pinned here and must not be: changing what the classifier asks
 * is a change to the process definition, never a change to this workflow.
 *
 * AUTH: the same `Aware mail relay token` Header Auth credential as the other two relays — one
 * token, minted by us, scoped to these webhooks, revocable in seconds, carrying no personal
 * data. Never relax to 'none': an open endpoint here spends someone else's Gemini quota.
 *
 * Request body:  { "system": "...", "prompt": "...", "model": "models/gemini-2.5-flash" }
 *                (`model` optional; `system` optional)
 * Response:      the model's JSON reply, passed through as-is. The reconciler parses it —
 *                see `reconciler.classify.parse_verdict`.
 *
 * Workflow ID: whl11SAS64Tjxl2b  (pass to update_workflow when editing this file). Published.
 *
 * Deploy:  validate_workflow -> create_workflow_from_code -> publish_workflow
 *          (this repo copy is the source of truth; edit here, then update_workflow)
 *
 * GOTCHA, the same one the mail relay hit: `newCredential('Aware mail relay token')` resolves
 * by credential TYPE, not by name, so a create/update binds whichever httpHeaderAuth n8n finds
 * first — here the unrelated `Header Auth account`. After any create_workflow_from_code or
 * update_workflow, rebind by ID over the REST API (`cBeDjEhQ1Nx2G1oX`) and re-publish, then
 * check `authentication` is still `headerAuth`.
 */
import { workflow, trigger, node, newCredential, expr } from '@n8n/workflow-sdk';

// Stable: the reconciler's LLM_RELAY_URL points at it. Changing it silently breaks the
// payment stages — which then stall rather than misfire, but stall forever.
const PATH = 'c203c3d0-f85d-44db-8b5e-50e54dda5b9d';

const GEMINI_CREDENTIAL = 'Google Gemini(PaLM) Api account';
const HEADER_CREDENTIAL = 'Aware mail relay token';

// The default only; the caller may override per request. Flash is the right tier for a binary
// reading of a short email — the same reasoning that picked the cheap end of the ladder before.
const DEFAULT_MODEL = 'models/gemini-2.5-flash';

const receiveQuestion = trigger({
  type: 'n8n-nodes-base.webhook',
  version: 2.1,
  config: {
    name: 'Receive Question',
    position: [0, 0],
    parameters: {
      httpMethod: 'POST',
      path: PATH,
      authentication: 'headerAuth',
      // responseNode: the caller must learn whether the model actually answered. A 200 that
      // only means "n8n received the request" would let an await conclude "no match" from a
      // failure — the one thing this tier must never do.
      responseMode: 'responseNode',
      options: {},
    },
    credentials: { httpHeaderAuth: newCredential(HEADER_CREDENTIAL) },
  },
  output: [{}],
});

const ask = node({
  type: '@n8n/n8n-nodes-langchain.googleGemini',
  version: 1.2,
  config: {
    name: 'Ask Gemini',
    position: [220, 0],
    // For a transient Gemini-side blip only. Safe to repeat because asking a model a question
    // is a READ; the mail relay next door must never do this.
    //
    // TWO, not three, and kept low on purpose: these tries MULTIPLY with the caller's own
    // (`adapters/llm.py`), so the pair is a ceiling on Gemini calls per question — and the key
    // is quota'd. Retrying into a quota error does not eventually succeed; it just spends the
    // day's allowance faster, which is exactly what happened while testing on 2026-08-16.
    //
    // It also does NOT mitigate backlog #70, and that is worth recording so nobody tunes these
    // numbers hoping it will: #70 failed BEFORE this node ran (empty `runData`, and a Bull
    // job-queue stack), so there was nothing for `retryOnFail` to retry.
    retryOnFail: true,
    maxTries: 2,
    waitBetweenTries: 2000,
    parameters: {
      resource: 'text',
      operation: 'message',
      modelId: {
        __rl: true,
        mode: 'id',
        value: expr(`{{ $json.body.model || "${DEFAULT_MODEL}" }}`),
      },
      // Passed through verbatim. Both halves are composed by `reconciler.classify`.
      messages: { values: [{ content: expr('{{ $json.body.prompt }}'), role: 'user' }] },
      // Gemini parses its own reply, so the response body is the verdict object rather than a
      // string containing JSON. The reconciler still validates the shape.
      jsonOutput: true,
      options: {
        systemMessage: expr('{{ $json.body.system || "" }}'),
        // NOT optional. The node's default is 16 tokens, which truncates any real answer
        // mid-object and yields a JSON parse error that looks like a model failure.
        maxOutputTokens: 2048,
        // A classification, not a composition: the same reading of the same mail should not
        // depend on a sampling draw.
        temperature: 0,
      },
    },
    credentials: { googlePalmApi: newCredential(GEMINI_CREDENTIAL) },
  },
  output: [{}],
});

const respond = node({
  type: 'n8n-nodes-base.respondToWebhook',
  version: 1.5,
  config: {
    name: 'Respond',
    position: [440, 0],
    parameters: {
      // The model's reply, unread and unedited. Interpreting it here would make this workflow
      // a decider; it is a wire.
      respondWith: 'json',
      responseBody: expr('{{ JSON.stringify($json) }}'),
      options: { responseCode: 200 },
    },
  },
  output: [{}],
});

export default workflow('relay-llm-question', 'Relay: LLM question')
  .add(receiveQuestion)
  .to(ask)
  .to(respond);
