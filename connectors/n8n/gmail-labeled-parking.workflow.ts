/**
 * Connector: Gmail — labelled mail -> raw_sensors  (ADR 0008, backlog #29)
 *
 * Watches ONE Gmail label and POSTs each matching message to the existing ingest gateway as a
 * canonical raw signal. Three nodes, and deliberately no logic beyond field renaming: this file
 * may authenticate, fetch and rename. It may NOT threshold, correlate, window, or decide that
 * something happened — see doc/connectors.md §1. Anything semantic (which merchant, how much,
 * which parking zone) is a capability deriver in src/inference/, AFTER Kafka, so a parser bug is
 * fixed by re-running rederive.py instead of destroying the evidence (invariant 19).
 *
 * First label is Parkingpay parking-expiry mail, because backlog #24 has already done the
 * semantic analysis for it. The event name asserts only that a LABEL was applied — not that the
 * mail means a parking session ended. #24 is explicit that the expiry time says nothing about
 * when the car actually left.
 *
 * Deploy:  validate_workflow -> create_workflow_from_code -> publish_workflow
 *          (this repo copy is the source of truth; edit here, then update_workflow)
 */
import { workflow, trigger, node, newCredential, expr } from '@n8n/workflow-sdk';

const LABEL = 'aware/parking';
const EVENT_NAME = 'email_labeled_parking';
const INGEST_URL = 'https://vector.prod.rods.me/sensors/gmail';
const USER_ID = 'rods'; // the entity key — must match existing rows or state fragments silently

const watchLabelledMail = trigger({
  type: 'n8n-nodes-base.gmailTrigger',
  version: 1.4,
  config: {
    name: 'Watch Labelled Mail',
    position: [0, 0],
    parameters: {
      // 1 minute is the floor for this node — it POLLS, it is not push. Mail latency is
      // therefore 0-60s by construction, which connector_eval.py reports as "trigger lag".
      pollTimes: { item: [{ mode: 'everyMinute' }] },

      // readStatus MUST be 'both'. The node defaults to 'unread', which silently drops any
      // message read before the next poll — a completeness hole that looks like nothing at all,
      // since ingest is at-most-once and acks on receipt. Likely part of why this node has a
      // reputation for "missing emails".
      // simple:false is for FIELD CERTAINTY, not for the body: it returns documented names
      // (id, threadId, subject, from, date, text, ...). The body is trimmed to a bounded
      // snippet in the next node and never leaves n8n whole.
      simple: false,
      maxResults: 10, // bounds a burst so a backfill cannot flood Vector's 500-event buffer
      filters: {
        q: 'label:' + LABEL, // Gmail search syntax — avoids needing a label ID lookup
        readStatus: 'both',
        includeSpamTrash: false,
        includeDrafts: false,
      },
    },
    credentials: { gmailOAuth2: newCredential('Gmail account') },
  },
  output: [{}],
});

// The whole mapping, one contract field per source line. `raw` mode keeps it readable as a
// single object instead of a dozen opaque `assignments` rows. No newlines in the JSON — they
// buy nothing and only invite escaping bugs.
const CANONICAL_BODY =
  '{ "payload": {' +
  ' "event_name": "' + EVENT_NAME + '",' +
  ' "user_id": "' + USER_ID + '",' +
  // Event-time = when the mail arrived, never when we polled (invariant 4). MUST be an INTEGER
  // of epoch SECONDS: shape_sensor only checks the field EXISTS, so a string or milliseconds
  // passes ingest and then breaks in capabilities.py / core._lineage, which subscript
  // message["timestamp"].
  //
  // KNOWN LIMITATION: this is the `Date` HEADER, which the SENDER sets — a skewed sender clock
  // therefore skews occurred_at. Gmail's own `internalDate` (receipt time) would be
  // authoritative, but this trigger does not expose it: with simple:false the node documents its
  // output as {id, threadId, labelIds, headers, html, text, textAsHtml, subject, date, to, from,
  // messageId, replyTo} — `date` is present, `internalDate` is not. Acceptable for machine-sent
  // mail like Parkingpay; revisit if a source turns out to lie about its clock. The symptom to
  // watch is a NEGATIVE pipeline lag in connector_eval.py, which flags exactly this.
  // (validate_workflow warns INVALID_EXPRESSION_PATH here; its path checking is unreliable —
  // a deliberately nonsense field name produces no warning at all — so the node's own
  // documented output is the better authority.)
  ' "timestamp": {{ Math.floor(new Date($json.date).getTime() / 1000) }},' +
  // Stamped at fetch time so connector_eval.py can separate n8n's trigger lag from our pipeline
  // lag (~3.3s measured). Seconds, for the same reason as above.
  ' "n8n_polled_at": {{ Math.floor(Date.now() / 1000) }},' +
  ' "label": "' + LABEL + '",' +
  // upstream_id is the contract's dedup key and must use this exact name — connector_eval.py
  // groups on it. Never used to derive message.id: events.id is a uuid PK and Vector's postgres
  // sink has no ON CONFLICT, so a deterministic id would fail the whole 500-event batch and
  // stall persistence for unrelated sources. A duplicate must be visible, not fatal.
  ' "upstream_id": {{ JSON.stringify($json.id) }},' +
  ' "gmail_thread_id": {{ JSON.stringify($json.threadId) }},' +
  ' "from": {{ JSON.stringify($json.from) }},' +
  ' "from_domain": {{ JSON.stringify(String($json.from || "").split("@").pop().replace(/[>\\s]/g, "").toLowerCase()) }},' +
  ' "subject": {{ JSON.stringify($json.subject) }},' +
  // A bounded preview, NOT the body. Whole bodies would breach the ~1 MiB nginx ingress and
  // Kafka ceilings, and would sit in Neon JSONB forever. upstream_id is enough to re-fetch the
  // full mail if extraction ever needs it.
  ' "snippet": {{ JSON.stringify(String($json.text || $json.snippet || "").replace(/\\s+/g, " ").trim().slice(0, 200)) }}' +
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
      // The mapping node already produced the exact body, so pass it through untouched.
      jsonBody: expr('{{ JSON.stringify($json) }}'),
      options: {},
    },
  },
  output: [{}],
});

export default workflow('connector-gmail-labeled-parking', 'Connector: gmail — labelled parking mail')
  .add(watchLabelledMail)
  .to(toCanonicalEvent)
  .to(postToIngest);
