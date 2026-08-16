/**
 * Query: gmail search  (ADR 0012)
 *
 * Answers a Gmail search **on demand**, for the reconciler's `await` stages.
 *
 * THIS IS NOT A CONNECTOR. It has no trigger, no polling, no cursor, and no mapping. It does
 * not notice anything, and it never starts by itself. The reconciler asks a question when it
 * needs the answer; this authenticates and answers. Same shape as the mail relay, other
 * direction.
 *
 * WHY IT REPLACED A CONNECTOR (archived: qOEr2Fx7GHuZXYyc). A polling connector put the
 * "has it happened yet?" loop in TWO places — n8n polling Gmail every 60s, the reconciler
 * polling Neon daily — and that split has a failure mode the tier cannot tolerate: if n8n is
 * down, or its Gmail Trigger drops a message (a documented weakness of that node), the
 * reconciler sees no approval event and concludes "not approved yet". It could not tell
 * "you haven't labelled it" from "nothing is watching". Asking synchronously turns that into
 * an HTTP error, which fails the run loudly instead of stalling it silently.
 *
 * The 60-second polling also bought nothing: the reconciler only acts daily, so the freshness
 * was discarded.
 *
 * DELIBERATELY NO MAPPING. The Gmail node's raw output goes straight back. Extracting an
 * address out of mailparser's `from` object, deriving a domain, trimming a body — all of that
 * is done in `reconciler.adapters.gmail`, where it is unit-tested. A previous version of the
 * parking connector stringified that object and shipped "[objectobject]" to 15 rows; doing it
 * in Python is how that stops being possible.
 *
 * `alwaysOutputData` on the search node matters: with zero matches the node would otherwise
 * emit nothing, no item would reach Respond, and the caller would hang until timeout. An
 * empty result is a normal answer here — most days nothing has been labelled.
 *
 * Request body:  { "q": "label:aware/invoice-approved", "received_after": "<ISO8601>",
 *                  "limit": 25 }
 * Response:      the raw Gmail items (simple:false shape), or [] .
 *
 * Auth: the same `Aware mail relay token` Header Auth credential as the mail relay — one
 * secret for both directions.
 *
 * Workflow ID: OdAax42ey8z02xkS  (pass to update_workflow when editing this file)
 * Deployed: created; publish once the Header Auth credential exists.
 *
 * Deploy:  validate_workflow -> create_workflow_from_code -> publish_workflow
 *          (this repo copy is the source of truth; edit here, then update_workflow)
 */
import { workflow, trigger, node, newCredential, expr } from '@n8n/workflow-sdk';

// Stable: the reconciler's GMAIL_QUERY_URL points at it.
const PATH = 'b7c41e2a-5d38-4f91-9a6e-2c0d84f7b513';
const HEADER_CREDENTIAL = 'Aware mail relay token';

const receiveQuery = trigger({
  type: 'n8n-nodes-base.webhook',
  version: 2.1,
  config: {
    name: 'Receive Query',
    position: [0, 0],
    parameters: {
      httpMethod: 'POST',
      path: PATH,
      // Reading mail must be no more open than sending it.
      authentication: 'headerAuth',
      responseMode: 'responseNode',
      options: {},
    },
    credentials: { httpHeaderAuth: newCredential(HEADER_CREDENTIAL) },
  },
  output: [{}],
});

const searchMail = node({
  type: 'n8n-nodes-base.gmail',
  version: 2.2,
  config: {
    name: 'Search Mail',
    position: [220, 0],
    // See the header note — without this, "nothing matched" hangs the caller.
    alwaysOutputData: true,
    parameters: {
      resource: 'message',
      operation: 'getAll',
      returnAll: false,
      limit: expr('{{ $json.body.limit || 25 }}'),
      // simple:false for FIELD CERTAINTY: the node documents its output shape
      // (date, from{value:[{address,name}]}, id, subject, threadId, labelIds, ...).
      simple: false,
      filters: {
        q: expr('{{ $json.body.q || "" }}'),
        // A real timestamp, not Gmail's day-granularity `after:` operator — the caller's
        // window is honoured exactly rather than rounded to a date.
        receivedAfter: expr('{{ $json.body.received_after }}'),
        // MUST be 'both'. The default 'unread' silently drops anything read before the
        // query runs — and an approval mail is read by definition, since you read it to
        // decide whether to approve it.
        readStatus: 'both',
        includeSpamTrash: false,
      },
    },
    credentials: { gmailOAuth2: newCredential('Gmail account') },
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
      respondWith: 'allIncomingItems',
      options: { responseCode: 200 },
    },
  },
  output: [{}],
});

export default workflow('query-gmail-search', 'Query: gmail search')
  .add(receiveQuery)
  .to(searchMail)
  .to(respond);
