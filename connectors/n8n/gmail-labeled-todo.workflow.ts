/**
 * Connector: Gmail — mail labelled `aware/todo` -> raw_sensors  (ADR 0008)
 *
 * The fast path for a task appearing. Structurally identical to the parking connector — three
 * nodes, no logic beyond field renaming — and for the same reason: this file may authenticate,
 * fetch and rename. It may NOT threshold, correlate, window, or decide that something happened.
 * Applying the label IS the decision, so there is nothing left to interpret.
 *
 * **This connector is an OPTIMISATION, not the source of truth.** `reconciler.tasks` sweeps the
 * same label hourly and emits an open for anything labelled that has no open event — so a
 * message this node drops is repaired within the hour rather than lost. That matters here more
 * than it did for parking: the Gmail Trigger is documented to miss messages, and
 * `gmail-query.workflow.ts`'s own header records why a polling connector could not be trusted
 * as the only witness to a label. What this buys is latency (~60s instead of ~1h), which is
 * exactly what you want between labelling a mail and seeing it on the board.
 *
 * The corollary is a duty: **the body below must match `reconciler.tasks.opened_body` field for
 * field.** Two producers emit `email_labeled_todo`, and a consumer that could tell them apart
 * would be reading one of them wrong. `tests/test_task_contract.py` reads this file as text and
 * fails if a field goes missing.
 *
 * Closing is NOT here and cannot be: Gmail reports a label being *added* and never one being
 * removed, so no trigger can see a task finish. That is the sweep's job.
 *
 * Workflow ID: zpxESD7h2Aovl19i  (pass to update_workflow when editing this file)
 * Deployed: created and PUBLISHED 2026-09-03. Polls every minute; with no `aware/todo` label in
 * Gmail the search simply matches nothing, so it sat harmlessly until the label existed.
 *
 * Deploy:  validate_workflow -> create_workflow_from_code -> publish_workflow
 *          (this repo copy is the source of truth; edit here, then update_workflow)
 */
import { workflow, trigger, node, newCredential, expr } from '@n8n/workflow-sdk';

const LABEL = 'aware/todo';
const EVENT_NAME = 'email_labeled_todo';
// `/sensors/tasks`, not `/sensors/gmail`: `source_app` is the discriminator the board and the
// timeline filter on, and it must match what the sweep and the dashboard tick emit under.
// route_by_app.yml sends anything that is not `overland` to the standard adapter, so a new app
// name needs no Vector change at all.
const INGEST_URL = 'https://vector.prod.rods.me/sensors/tasks';
const USER_ID = 'rods'; // the entity key — must match existing rows or state fragments silently

const watchLabelledMail = trigger({
  type: 'n8n-nodes-base.gmailTrigger',
  version: 1.4,
  config: {
    name: 'Watch Labelled Mail',
    position: [0, 0],
    parameters: {
      // 1 minute is this node's floor — it POLLS, it is not push.
      pollTimes: { item: [{ mode: 'everyMinute' }] },
      // readStatus MUST be 'both'. The default 'unread' silently drops anything read before the
      // next poll — and a mail you are labelling as a task is one you have just read.
      simple: false,
      filters: {
        q: 'label:' + LABEL,
        readStatus: 'both',
        includeSpamTrash: false,
        includeDrafts: false,
      },
    },
    credentials: { gmailOAuth2: newCredential('Gmail account') },
  },
  output: [{}],
});

// One contract field per source line, `raw` mode so it reads as a single object.
// KEEP IN SYNC with reconciler.tasks.opened_body.
const CANONICAL_BODY =
  '{ "payload": {' +
  ' "event_name": "' + EVENT_NAME + '",' +
  ' "user_id": "' + USER_ID + '",' +
  // Event-time is the mail's own `Date` header, so a task's age is how long the MAIL has been
  // sitting rather than how long since we noticed it — the board's whole job is showing what is
  // rotting. MUST be an INTEGER of epoch SECONDS: shape_sensor only checks the field exists, so
  // a string or milliseconds passes ingest and breaks downstream.
  ' "timestamp": {{ Math.floor(new Date($json.date).getTime() / 1000) }},' +
  ' "n8n_polled_at": {{ Math.floor(Date.now() / 1000) }},' +
  ' "label": "' + LABEL + '",' +
  // The dedup key, and here also the JOIN key: the close event carries the same value, and the
  // board pairs them on it. connector_eval.py groups on this exact name.
  ' "upstream_id": {{ JSON.stringify($json.id) }},' +
  ' "gmail_thread_id": {{ JSON.stringify($json.threadId) }},' +
  // `from` is a mailparser OBJECT, not a string: {html, text, value:[{name,address}]}. An
  // earlier version of the parking connector stringified it and shipped "[objectobject]".
  ' "from": {{ JSON.stringify(((($json.from || {}).value || [{}])[0] || {}).address || "") }},' +
  ' "from_name": {{ JSON.stringify(((($json.from || {}).value || [{}])[0] || {}).name || "") }},' +
  ' "from_domain": {{ JSON.stringify(String(((($json.from || {}).value || [{}])[0] || {}).address || "").split("@").pop().toLowerCase()) }},' +
  // The line you actually read on the board.
  ' "subject": {{ JSON.stringify($json.subject) }},' +
  // A bounded prefix, never the whole body — the ~1 MiB ingress and Kafka ceilings, and Neon
  // keeps it forever. 1000 chars is a downstream-extraction budget, not a display budget.
  ' "snippet": {{ JSON.stringify(String($json.text || $json.snippet || "").replace(/\\s+/g, " ").trim().slice(0, 1000)) }}' +
  ' } }';

const toCanonicalEvent = node({
  type: 'n8n-nodes-base.set',
  version: 3.4,
  config: {
    name: 'Map To Canonical Event',
    position: [220, 0],
    parameters: {
      mode: 'raw',
      jsonOutput: expr(CANONICAL_BODY),
    },
  },
  output: [{}],
});

const postToIngest = node({
  type: 'n8n-nodes-base.httpRequest',
  version: 4.4,
  config: {
    name: 'Post To Vector Ingest',
    position: [440, 0],
    parameters: {
      method: 'POST',
      url: INGEST_URL,
      sendBody: true,
      contentType: 'json',
      specifyBody: 'json',
      jsonBody: expr('{{ JSON.stringify($json) }}'),
      options: {},
    },
  },
  output: [{}],
});

export default workflow('connector-gmail-labeled-todo', 'Connector: gmail — labelled todo mail')
  .add(watchLabelledMail)
  .to(toCanonicalEvent)
  .to(postToIngest);
