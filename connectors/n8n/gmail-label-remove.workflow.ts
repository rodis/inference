/**
 * Relay: gmail label remove  (ADR 0008 / ADR 0012)
 *
 * Takes a label off a message, on request. The **outbound** counterpart to
 * `gmail-query.workflow.ts`: that one answers a question, this one changes something.
 *
 * THIS IS NOT A CONNECTOR. No trigger, no polling, no cursor. It never starts by itself and it
 * decides nothing — a person decided the task was done, either by ticking the box on the Aware
 * board or (in which case this is not involved at all) by unlabelling the mail themselves. The
 * relay authenticates and transmits. That is the whole of ADR 0008's boundary, and this stays
 * comfortably inside it.
 *
 * WHY THROUGH n8n. The Gmail OAuth credential lives in n8n's store and stays there — the user's
 * standing rule is that credentials belong in n8n, never in the repo, not even gitignored. So
 * the dashboard holds one secret of its own (the shared relay token) rather than a Google
 * refresh token.
 *
 * WHY THE LABEL IS RESOLVED HERE. Gmail's modify API takes label *IDs*, not names, while every
 * caller naturally knows the name (`aware/todo`). Looking the id up is plumbing, not semantics —
 * it is the same class of work as authenticating — so it belongs on this side rather than
 * forcing every caller to carry an opaque `Label_8837...`. It also means creating a second task
 * label needs no change here.
 *
 * Deliberately a Filter node rather than a Code node: `doc/connectors.md` names a Code node as
 * the smell that a workflow has started doing semantics, and there is no reason to reach for one
 * to compare two strings.
 *
 * IDEMPOTENT, and the caller relies on it. Removing a label that is already gone is a no-op for
 * Gmail, which is what lets `reconciler.adapters.labels` refuse to retry: a caller that retried
 * through a timeout could not tell "it did not happen" from "it happened and the reply was
 * lost", and the second case races the close event emitted on success.
 *
 * Request body:  { "message_id": "18f2...", "label": "aware/todo", "action": "remove" }
 * Response:      200 with the modified message, or 200 with [] if the label was not present.
 *
 * `action` is accepted and currently ignored — it is there so a future `add` (reopening a task
 * from the board) is a change to this file rather than a second workflow and a second URL.
 *
 * Auth: the same `Aware mail relay token` Header Auth credential as the mail relay and the
 * query relay — one secret for every direction.
 *
 * Workflow ID: WT077ZcgeAHRiEB5  (pass to update_workflow when editing this file)
 * Deployed: created and PUBLISHED 2026-09-03, then tested end to end without changing a single
 * label — by asking it to remove `aware/parking` from a message verifiably lacking it. Every
 * node ran (auth, label listing, the name->id filter, the modify call); the message's labels
 * were identical afterwards. Auth rejects a missing and a wrong token with 403.
 *
 * ⚠️ CREDENTIAL TRAP, hit again here. `newCredential('Aware mail relay token')` resolves by
 * credential **TYPE**, not by name: on creation n8n bound `Header Auth account` instead, and
 * the relay would have accepted the wrong shared secret. Rebind by ID after any create or
 * update — `cBeDjEhQ1Nx2G1oX` is `Aware mail relay token`, the same one the query relay uses:
 *
 *     PUT /api/v1/workflows/WT077ZcgeAHRiEB5   with nodes[Receive Request].credentials
 *         .httpHeaderAuth = {id: 'cBeDjEhQ1Nx2G1oX', name: 'Aware mail relay token'}
 *
 * Deploy:  validate_workflow -> create_workflow_from_code -> publish_workflow
 *          (this repo copy is the source of truth; edit here, then update_workflow)
 */
import { workflow, trigger, node, newCredential, expr } from '@n8n/workflow-sdk';

// Stable: the dashboard's GMAIL_LABEL_URL points at it.
const PATH = 'e4f7a1c9-2b60-4d83-9f15-7ac3e50b8d24';
const HEADER_CREDENTIAL = 'Aware mail relay token';

const receiveRequest = trigger({
  type: 'n8n-nodes-base.webhook',
  version: 2.1,
  config: {
    name: 'Receive Request',
    position: [0, 0],
    parameters: {
      httpMethod: 'POST',
      path: PATH,
      // Changing mail must be no more open than reading it.
      authentication: 'headerAuth',
      responseMode: 'responseNode',
      options: {},
    },
    credentials: { httpHeaderAuth: newCredential(HEADER_CREDENTIAL) },
  },
  output: [{}],
});

const listLabels = node({
  type: 'n8n-nodes-base.gmail',
  version: 2.2,
  config: {
    name: 'List Labels',
    position: [220, 0],
    parameters: {
      resource: 'label',
      operation: 'getAll',
      returnAll: true,
    },
    credentials: { gmailOAuth2: newCredential('Gmail account') },
  },
  output: [{}],
});

// Name -> id. A Filter, not a Code node (see the header).
const findTheLabel = node({
  type: 'n8n-nodes-base.filter',
  version: 2.3,
  config: {
    name: 'Find The Label',
    position: [440, 0],
    // Without this, a label name that matches nothing passes zero items, every downstream node
    // is skipped, Respond never fires and the CALLER HANGS until its timeout. With it, the
    // chain reaches the Gmail node and fails fast and loudly instead — which is the right
    // treatment for what can only be a setup error (the label does not exist at all). Note the
    // ordinary case is different and needs no handling: removing a label from a message that
    // does not carry it is a Gmail no-op, verified against a real message on deploy day.
    alwaysOutputData: true,
    parameters: {
      conditions: {
        // The v2.3 shape exactly, from get_node_types: caseSensitive + typeValidation, and
        // no `version` key (that one belongs to the If node's older schema).
        options: { caseSensitive: true, typeValidation: 'strict' },
        combinator: 'and',
        conditions: [
          {
            leftValue: expr('{{ $json.name }}'),
            operator: { type: 'string', operation: 'equals' },
            rightValue: expr("{{ $('Receive Request').item.json.body.label }}"),
          },
        ],
      },
      options: {},
    },
  },
  output: [{}],
});

const removeLabel = node({
  type: 'n8n-nodes-base.gmail',
  version: 2.2,
  config: {
    name: 'Remove Label',
    position: [660, 0],
    parameters: {
      resource: 'message',
      operation: 'removeLabels',
      // Reached back through the trigger, because by this point the item in hand is a LABEL,
      // not the request — the message id only exists on the webhook's own item.
      messageId: expr("{{ $('Receive Request').item.json.body.message_id }}"),
      // The id resolved by the filter above. An ARRAY containing an expression, not an
      // expression that evaluates to an array: `labelIds` is declared `string[]`, and
      // validate_workflow rejects the latter with "expected array, got string". n8n resolves
      // expressions inside array elements, so this is both type-correct and right at runtime.
      labelIds: [expr('{{ $json.id }}')],
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
    position: [880, 0],
    parameters: {
      respondWith: 'allIncomingItems',
      options: { responseCode: 200 },
    },
  },
  output: [{}],
});

export default workflow('relay-gmail-label-remove', 'Relay: gmail label remove')
  .add(receiveRequest)
  .to(listLabels)
  .to(findTheLabel)
  .to(removeLabel)
  .to(respond);
