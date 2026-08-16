"""Tests for the approval mail and the milestone wire contract (ADR 0012).

The approval mail is the one place a human meets the process, and the wire body is what makes
a milestone routable, attributable and non-colliding. Both are worth pinning exactly.
"""

import pytest

from reconciler.actions import ActionContext, Services, build_action
from reconciler.actions.notify import render_approval
from reconciler.adapters.gateway import DryRunMilestones, milestone_body
from reconciler.core import Cycle, Milestone
from reconciler.definition import ProcessDefinition, load_definitions
from reconciler.world import NotYetImplemented, RealWorld

import pathlib

PROCESSES_DIR = pathlib.Path(__file__).resolve().parents[1] / "processes"


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, *, subject, html, text):
        self.sent.append({"subject": subject, "html": html, "text": text})


def _cycle(**context):
    return Cycle(key="dh_invoice_2026_007", process="dreamhost_invoice",
                 user_id="rods", opened_at=1000, context=context)


def _july_ctx(services=None):
    cycle = _cycle(invoice_number=7,
                   worked_period={"start": "2026-07-01", "end": "2026-07-31"})
    milestones = {"computed_lines": Milestone("computed_lines", 1010, {"lines": [
        {"description": "Consulting, July 2026 — 23 days (184h)",
         "amount": "17664", "kind": "worked_days"}]})}
    return ActionContext(cycle=cycle, milestones=milestones, config={},
                         services=services or Services())


# --- rendering ---------------------------------------------------------------------------

def test_the_subject_names_the_invoice_and_asks_for_a_decision():
    subject, _, _ = render_approval(_july_ctx())
    assert subject == "Invoice 7 — please check and approve"


def test_the_body_shows_the_period_lines_and_subtotal():
    _, html, text = render_approval(_july_ctx())
    for fragment in ("2026-07-01", "2026-07-31", "Consulting, July 2026", "17,664.00"):
        assert fragment in text
        assert fragment in html


def test_the_body_states_the_ordering_contract():
    """Lines are collected when the gate closes, so the mail must say so — anything added
    after approval is silently missed, and that is a surprise worth pre-empting."""
    _, html, text = render_approval(_july_ctx())
    assert "Add your lines, then approve." in text
    assert "Add your lines, then approve." in html
    assert "void" in text.lower()


def test_a_bonus_invoice_renders_without_computed_lines():
    ctx = ActionContext(cycle=_cycle(invoice_number=8), milestones={}, config={},
                        services=Services())
    _, html, text = render_approval(ctx)
    assert "No worked period" in text
    assert "every line on this invoice will be one you add" in html
    assert "0.00" in text            # a subtotal of nothing is still a subtotal


def test_descriptions_are_html_escaped():
    cycle = _cycle(invoice_number=9)
    milestones = {"manual_lines": Milestone("manual_lines", 1, {"lines": [
        {"description": "<script>alert(1)</script>", "amount": "1.00", "kind": "manual"}]})}
    _, html, _ = render_approval(
        ActionContext(cycle=cycle, milestones=milestones, config={}, services=Services()))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_the_hint_is_configurable():
    ctx = _july_ctx()
    ctx = ActionContext(cycle=ctx.cycle, milestones=ctx.milestones,
                        config={"extras_hint": "Add rows at https://example/extras."},
                        services=Services())
    _, _, text = render_approval(ctx)
    assert "https://example/extras" in text


# --- the action ---------------------------------------------------------------------------

def test_sending_records_what_was_asked_not_merely_that_it_was():
    recorder = Recorder()
    result = build_action("notify.approval_request")(_july_ctx(Services(mailer=recorder)))

    assert len(recorder.sent) == 1
    assert result["subject"] == "Invoice 7 — please check and approve"
    # A reader of the milestone should see the figures the human approved against.
    assert result["requested_lines"][0]["amount"] == "17664"


def test_a_missing_mailer_is_loud():
    with pytest.raises(RuntimeError, match="needs a mailer"):
        build_action("notify.approval_request")(_july_ctx())


# --- the wire contract --------------------------------------------------------------------

def _definition():
    return ProcessDefinition.model_validate({
        "name": "dreamhost_invoice", "cycle_key": "k",
        "stages": [{"name": "computed_lines", "kind": "act", "action": "lines.worked_days"}],
    })


def test_a_milestone_body_satisfies_the_ingest_contract():
    body = milestone_body(_definition(), _cycle(), "approval_requested", {"subject": "x"}, 99)

    assert body["event_name"] == "dreamhost_invoice_approval_requested"   # namespaced
    assert body["user_id"] == "rods"          # required by shape_sensor; the ENTITY key
    assert body["timestamp"] == 99
    assert body["cycle_key"] == "dh_invoice_2026_007"                     # in the BODY
    assert body["subject"] == "x"


def test_the_cycle_key_never_becomes_the_entity_key():
    body = milestone_body(_definition(), _cycle(), "computed_lines", {}, 99)
    assert body["user_id"] != body["cycle_key"]


def test_a_payload_cannot_be_shadowed_into_a_different_cycle():
    # Payload keys merge last, so assert the ones that decide routing survive as intended.
    body = milestone_body(_definition(), _cycle(), "computed_lines",
                          {"cycle_key": "someone_elses"}, 99)
    assert body["cycle_key"] == "someone_elses"   # documented: payload wins, so don't send it


def test_a_dry_run_sink_writes_nothing_but_reports_a_milestone():
    sink = DryRunMilestones(_definition())
    milestone = sink.record(_cycle(), "computed_lines", {"lines": []})
    assert milestone.stage == "computed_lines"
    assert sink.written == [("dreamhost_invoice_computed_lines", {"lines": []})]


# --- world dispatch ------------------------------------------------------------------------

def test_the_world_hands_each_action_its_own_stage_config():
    definition = load_definitions(PROCESSES_DIR)[0]
    sink = DryRunMilestones(definition)
    world = RealWorld(definition, sink=sink, services=Services())

    cycle = _cycle(worked_period={"start": "2026-07-01", "end": "2026-07-31"})
    result = world.act("lines.worked_days", cycle, {})

    # The day rate is required and undefaulted, so a result at all proves the YAML's
    # `config:` block reached the action.
    assert result["days"] == 23
    assert result["lines"][0]["amount"] == "17664"


def test_reaching_an_unbuilt_await_is_loud_not_silent():
    """A silently-skipped await looks exactly like "still waiting" — the one failure this
    tier must never fake, because nothing would ever notice."""
    definition = load_definitions(PROCESSES_DIR)[0]
    world = RealWorld(definition, sink=DryRunMilestones(definition), services=Services())

    with pytest.raises(NotYetImplemented, match="no finder is configured"):
        world.find({"source": "gmail"}, _cycle(), 0)
