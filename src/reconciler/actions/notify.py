"""Notification actions (ADR 0012).

The approval request is the process's one *conversational* stage: it shows a human what was
computed and asks them to add anything the system cannot know. Its contract with the reader
is stated in the mail itself — **add your lines, then approve** — because `manual_lines` is
collected when the approval gate closes, so anything added afterwards is missed.
"""

import logging
from decimal import ROUND_HALF_UP, Decimal
from html import escape
from typing import Protocol

from reconciler.actions import ActionContext, register_action
from reconciler.money import lines_from

logger = logging.getLogger("reconciler.actions.notify")

_CENTS = Decimal("0.01")


class Mailer(Protocol):
    """Sending mail, as a port.

    A port because the transport is genuinely open — stdlib SMTP against any provider's app
    password, or a hosted API later — and because a console implementation lets the rendered
    mail be reviewed without sending anything at all.
    """

    def send(self, *, subject: str, html: str, text: str) -> None: ...


def _money(value: Decimal) -> str:
    return f"{value.quantize(_CENTS, rounding=ROUND_HALF_UP):,}"


def render_approval(ctx: ActionContext) -> tuple[str, str, str]:
    """Render (subject, html, text) for the approval request.

    Split out from the action so the exact mail a cycle would send can be rendered and read
    without a mailer wired — which is how this gets reviewed before it is ever sent.
    """
    cycle = ctx.cycle
    lines = [line for milestone in ctx.milestones.values()
             for line in lines_from(milestone.payload)]
    subtotal = sum((line.amount for line in lines), Decimal(0))
    currency = ctx.config.get("currency", "EUR")
    number = cycle.context.get("invoice_number", cycle.key)
    period = cycle.context.get("worked_period")

    subject = f"Invoice {number} — please check and approve"

    period_line = (
        f"Period: {period['start']} to {period['end']}" if period
        else "No worked period — this invoice is manual lines only."
    )
    hint = ctx.config.get(
        "extras_hint",
        "To add an expense or bonus line, add a row before you approve.",
    )

    if lines:
        rows = "".join(
            f"<tr><td style='padding:8px 12px;border-bottom:1px solid #e5e5e5'>"
            f"{escape(line.description)}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #e5e5e5;"
            f"text-align:right;white-space:nowrap'>{currency} {_money(line.amount)}</td></tr>"
            for line in lines
        )
    else:
        rows = ("<tr><td colspan='2' style='padding:8px 12px;color:#888'>"
                "Nothing computed — every line on this invoice will be one you add.</td></tr>")

    html = f"""<div style="font-family:-apple-system,Segoe UI,sans-serif;font-size:14px;
 color:#222;max-width:560px">
  <h2 style="margin:0 0 4px">Invoice {escape(str(number))}</h2>
  <p style="margin:0 0 16px;color:#666">{escape(period_line)}</p>
  <table style="width:100%;border-collapse:collapse;border-top:1px solid #e5e5e5">
    {rows}
    <tr>
      <td style="padding:10px 12px;font-weight:600">Subtotal</td>
      <td style="padding:10px 12px;text-align:right;font-weight:600;white-space:nowrap">
        {currency} {_money(subtotal)}</td>
    </tr>
  </table>
  <p style="margin:16px 0 4px"><strong>Add your lines, then approve.</strong></p>
  <p style="margin:0;color:#666">{escape(hint)} Expense and bonus lines are collected when
  you approve, so anything added afterwards will not appear on this invoice — if that
  happens, void the cycle and re-run it.</p>
  <p style="margin:16px 0 0;color:#999;font-size:12px">Reply to approve. Cycle
  {escape(cycle.key)}.</p>
</div>"""

    text_rows = "\n".join(f"  {line.description}: {currency} {_money(line.amount)}"
                          for line in lines) or "  (nothing computed)"
    text = (
        f"Invoice {number}\n{period_line}\n\n{text_rows}\n\n"
        f"  Subtotal: {currency} {_money(subtotal)}\n\n"
        f"Add your lines, then approve. {hint}\n"
        "Lines are collected when you approve; anything added afterwards will not appear — "
        "void and re-run if that happens.\n\n"
        f"Reply to approve. Cycle {cycle.key}.\n"
    )
    return subject, html, text


@register_action("notify.approval_request")
def approval_request(ctx: ActionContext) -> dict:
    """Send the human the figures and ask them to approve.

    Its own stage rather than a side effect of computing, because the recorded milestone is
    what stops the reconciler re-sending this mail every single day until it is answered.
    """
    subject, html, text = render_approval(ctx)

    mailer = ctx.services.mailer
    if mailer is None:
        raise RuntimeError(
            "notify.approval_request needs a mailer; none configured. "
            "Wire one in Services, or run with the console mailer to preview."
        )
    mailer.send(subject=subject, html=html, text=text)
    logger.info("cycle %s: approval requested (%s)", ctx.cycle.key, subject)

    # Record what was asked, not just that something was. A reader of the milestone should
    # be able to see the figures the human actually approved against.
    return {"subject": subject, "requested_lines": [
        line.as_payload()
        for milestone in ctx.milestones.values()
        for line in lines_from(milestone.payload)
    ]}
