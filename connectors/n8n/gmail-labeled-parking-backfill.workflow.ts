/**
 * ONE-OFF BACKFILL: existing labelled mail -> raw_sensors  (ADR 0008, backlog #29)
 *
 * Companion to gmail-labeled-parking.workflow.ts, which is the LIVE connector. That one uses the
 * Gmail *Trigger*, whose only event is `messageReceived` — so it fires on message RECEIPT, never
 * on label application. Labelling mail that is already in the mailbox therefore produces nothing
 * at all (confirmed: 12 messages labelled, 0 executions). This workflow is how that history gets
 * in: a manual run over `label:<LABEL>` using the Gmail *node*.
 *
 * This is the "backfill is a manual one-off run" procedure ADR 0008 names as the accepted cost of
 * the connector tier — there is no rederive.py for ingestion, because the raw signal is what
 * rederive replays FROM.
 *
 * Run it, confirm the rows in Neon, then ARCHIVE it (archive_workflow). It is deliberately not
 * published: a scheduled backfill would re-post the same mail every run, and while a duplicate is
 * non-fatal by design (see below), it is still noise in the timeline.
 *
 * ⚠️ The mapping below is a COPY of the live connector's. Keep the two in sync, or archive this
 * file once the backfill is done. Duplication is tolerable only because this is single-use.
 */
import { workflow, trigger, node, newCredential, expr } from '@n8n/workflow-sdk';

const LABEL = 'aware/parking';
const EVENT_NAME = 'email_labeled_parking';
const INGEST_URL = 'https://vector.prod.rods.me/sensors/gmail';
const USER_ID = 'rods';

const startBackfill = trigger({
  type: 'n8n-nodes-base.manualTrigger',
  version: 1,
  config: { name: 'Run Backfill', position: [0, 0] },
  output: [{}],
});

const fetchLabelledMail = node({
  type: 'n8n-nodes-base.gmail',
  version: 2.2,
  config: {
    name: 'Fetch Labelled Mail',
    position: [220, 0],
    parameters: {
      resource: 'message',
      operation: 'getAll',
      // Bounded on purpose. Vector's Kafka and Neon sinks buffer 500 events with
      // when_full: block, so an unbounded backfill over a large label would apply backpressure
      // to every other producer. Raise deliberately and re-run if the label is bigger.
      returnAll: false,
      limit: 50,
      // Same field-certainty reason as the live connector: documented output names.
      simple: false,
      filters: {
        q: 'label:' + LABEL,
        // Same trap as the trigger: this node ALSO defaults to 'unread'. Backfilling read mail
        // is the entire point, so this must be 'both'.
        readStatus: 'both',
        includeSpamTrash: false,
      },
    },
    credentials: { gmailOAuth2: newCredential('Gmail account') },
  },
  output: [{}],
});

// Mirror of the live connector's mapping. See that file for why each field is shaped this way.
const CANONICAL_BODY =
  '{ "payload": {' +
  ' "event_name": "' + EVENT_NAME + '",' +
  ' "user_id": "' + USER_ID + '",' +
  // Event-time is the mail's own arrival, NOT now — that is what makes a backfill land at the
  // right point in the timeline instead of bunching at the moment it was run (invariant 4).
  ' "timestamp": {{ Math.floor(new Date($json.date).getTime() / 1000) }},' +
  ' "n8n_polled_at": {{ Math.floor(Date.now() / 1000) }},' +
  ' "label": "' + LABEL + '",' +
  // Because this is a backfill, upstream_id is what makes a re-run detectable: a repeated
  // message yields a second row (uuid PK, no ON CONFLICT — a deterministic id would fail the
  // whole 500-event Neon batch), and connector_eval.py groups on this to surface it.
  ' "upstream_id": {{ JSON.stringify($json.id) }},' +
  ' "gmail_thread_id": {{ JSON.stringify($json.threadId) }},' +
  ' "from": {{ JSON.stringify($json.from) }},' +
  ' "from_domain": {{ JSON.stringify(String($json.from || "").split("@").pop().replace(/[>\\s]/g, "").toLowerCase()) }},' +
  ' "subject": {{ JSON.stringify($json.subject) }},' +
  ' "snippet": {{ JSON.stringify(String($json.text || $json.snippet || "").replace(/\\s+/g, " ").trim().slice(0, 200)) }},' +
  // The one field the live connector does not send: marks these rows as history, not live
  // capture, so their latency figures can be excluded from a connector_eval baseline.
  ' "backfill": true' +
  ' } }';

const toCanonicalEvent = node({
  type: 'n8n-nodes-base.set',
  version: 3.4,
  config: {
    name: 'Map To Canonical Event',
    position: [440, 0],
    parameters: { mode: 'raw', jsonOutput: expr(CANONICAL_BODY) },
  },
  output: [{}],
});

const postToIngest = node({
  type: 'n8n-nodes-base.httpRequest',
  version: 4.4,
  config: {
    name: 'Post To Vector Ingest',
    position: [660, 0],
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

export default workflow('connector-gmail-labeled-parking-backfill', 'Connector: gmail — labelled parking mail (BACKFILL, one-off)')
  .add(startBackfill)
  .to(fetchLabelledMail)
  .to(toCanonicalEvent)
  .to(postToIngest);
