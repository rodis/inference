/**
 * Relay: outbound mail  (ADR 0008 + ADR 0012)
 *
 * The process tier's mail transport. The reconciler POSTs a fully-composed message and this
 * sends it with n8n's stored SMTP credential — so no personal credential ever has to live in
 * the repo, not even gitignored.
 *
 * THE BOUNDARY, which is the outbound mirror of doc/connectors.md §1:
 *
 *   An outbound relay may AUTHENTICATE and TRANSMIT.
 *   It may not compose, decide, or interpret.
 *
 * Every byte of subject/html/text is built by `reconciler.actions.notify` and passed through
 * untouched. Nothing here knows what an invoice is, which is what keeps this consistent with
 * ADR 0012's rejection of n8n as a place for process state: n8n holds CREDENTIALS, never
 * state and never decisions.
 *
 * AUTH IS NOT OPTIONAL. An unauthenticated endpoint that sends mail from your own address is
 * an open relay — materially worse than the ingest gateway being unauthenticated, because
 * ingest can only write events you can delete. The webhook therefore requires a header
 * credential. Create it in n8n as a **Header Auth** credential named exactly
 * `Aware mail relay token` (any header name/value pair; put the same value in the
 * reconciler's MAIL_RELAY_TOKEN, and set MAIL_RELAY_HEADER if you don't use `X-Relay-Token`).
 *
 * The token is deliberately the ONLY secret the reconciler holds: we mint it, it is scoped to
 * this one webhook, it is revocable in seconds, and it carries no personal data.
 *
 * Request body:  { "to": "...", "subject": "...", "html": "...", "text": "..." }
 *
 * Workflow ID: Ozr1TCuzYKpA8ehl  (pass to update_workflow when editing this file)
 * Deployed: created but NOT published — n8n refuses to publish while the
 *           `Aware mail relay token` Header Auth credential is missing. That refusal is
 *           the fail-closed behaviour this workflow wants: it cannot go live as an open
 *           relay. Create the credential, then publish.
 *
 * Deploy:  validate_workflow -> create_workflow_from_code -> publish_workflow
 *          (this repo copy is the source of truth; edit here, then update_workflow)
 */
import { workflow, trigger, node, newCredential, expr } from '@n8n/workflow-sdk';

// Stable: the reconciler's MAIL_RELAY_URL points at it. Changing it silently breaks sending.
const PATH = '058c90ca-f994-45fe-84ad-39559d60c082';

// Reuses the credential the retired "Workflow: Send Invoice Data" already sent from, so the
// sender identity is unchanged and nothing new has to be configured to start.
const SMTP_CREDENTIAL = 'SMTP account';
const HEADER_CREDENTIAL = 'Aware mail relay token';

const receiveMail = trigger({
  type: 'n8n-nodes-base.webhook',
  version: 2.1,
  config: {
    name: 'Receive Composed Mail',
    position: [0, 0],
    parameters: {
      httpMethod: 'POST',
      path: PATH,
      // See the header note above — never relax this to 'none'.
      authentication: 'headerAuth',
      // responseNode, so the caller learns whether SMTP actually accepted the message rather
      // than just that n8n received the request. A mail that silently fails to send would
      // stall the process at a gate no one is watching.
      responseMode: 'responseNode',
      options: {},
    },
    credentials: { httpHeaderAuth: newCredential(HEADER_CREDENTIAL) },
  },
  output: [{}],
});

const sendMail = node({
  type: 'n8n-nodes-base.emailSend',
  version: 2.1,
  config: {
    name: 'Send Mail',
    position: [220, 0],
    parameters: {
      operation: 'send',
      fromEmail: expr('{{ $json.body.from || "N8N invoice service <rosario.disomma@yahoo.com>" }}'),
      toEmail: expr('{{ $json.body.to }}'),
      subject: expr('{{ $json.body.subject }}'),
      // Both parts, deliberately. The plain-text alternative is what a reply quotes and what
      // a later classification pass reads, so it must not be dropped in favour of HTML.
      emailFormat: 'both',
      text: expr('{{ $json.body.text }}'),
      html: expr('{{ $json.body.html }}'),
      options: {
        // Off: this mail is an invoice approval request, and "sent automatically with n8n"
        // appended to it is both noise and a leak of how the process is built.
        appendAttribution: false,
      },
    },
    credentials: { smtp: newCredential(SMTP_CREDENTIAL) },
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
      respondWith: 'json',
      responseBody: expr('{{ JSON.stringify({ sent: true }) }}'),
      options: { responseCode: 200 },
    },
  },
  output: [{}],
});

export default workflow('relay-outbound-mail', 'Relay: outbound mail')
  .add(receiveMail)
  .to(sendMail)
  .to(respond);
