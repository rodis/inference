"""Who may produce a task event, and — where two producers remain — that they agree.

`email_labeled_todo` has two producers: the sweep and the n8n Gmail Trigger. A consumer that
could tell them apart would be reading one of them wrong, so their shapes have to match.

`email_task_closed` has exactly **one**, the sweep, and this file now guards that instead of
guarding a shared shape. The dashboard's tick used to emit one too; it stopped on 2026-10-13,
when the sweep's store of record moved to MySQL — a store the dashboard cannot reach, so a
close emitted there would be invisible to the sweep and the next run would close the task
again. Ticking now only removes the Gmail label, which the sweep reads as the close.

They cannot simply share a module in any case. The dashboard image is built with `dashboard/`
as its Docker context, so `src/reconciler` is not importable there — the same build boundary
that made `dashboard/processes.json` a generated file.

Everything here reads source as **text or AST**, never by importing: `dashboard/app.py` needs
fastapi and psycopg, and CI's python job installs neither.
"""

import pathlib
import re

import pytest

from reconciler.tasks import (
    CLOSED_EVENT,
    OPENED_EVENT,
    opened_body,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard" / "app.py"
CONNECTOR = ROOT / "connectors" / "n8n" / "gmail-labeled-todo.workflow.ts"


def test_the_dashboard_is_not_a_producer_of_task_events():
    """The invariant that replaced the shared-shape check, and the more valuable one.

    The sweep is the sole producer of `email_task_closed`. Restoring the dashboard's POST would
    not fail loudly: both events are legitimate-looking raw rows, the board's latest-open vs
    latest-close join tolerates the duplicate, and the only symptom is a second
    "Email task closed" on the Aware timeline for every task — once from the tick, once from
    the sweep that cannot see it in MySQL. A silent doubling of history is exactly the kind of
    regression a test has to carry, because nothing else will notice.

    Asserted as "the dashboard POSTs at no ingest endpoint at all", which is both easier to
    state and the truer rule: this image holds no store credential and reaches Aware through
    nothing. Its one outbound call is the Gmail label relay.
    """
    source = DASHBOARD.read_text()
    offenders = [line.strip() for line in source.splitlines()
                 if re.search(r"/sensors/\w+", line) and not line.lstrip().startswith("#")]
    assert not offenders, (
        "dashboard/app.py posts at the ingest gateway again: "
        f"{offenders}. The sweep is the sole producer of task events — a second one puts two "
        "closes on the timeline for every tick (tests/test_task_contract.py's header says why)")


def test_closing_a_task_is_a_label_change_and_nothing_else():
    """The positive half: the tick must still *do* something, or the board silently stops
    working while the test above stays green."""
    source = DASHBOARD.read_text()
    assert "_relay_remove_label" in source, "the tick no longer removes the Gmail label"
    assert '"action": "remove"' in source


@pytest.mark.parametrize("event", [OPENED_EVENT, CLOSED_EVENT])
def test_the_event_names_are_written_out_in_the_dashboard_sql(event):
    """The board's SQL names both events as literals (it cannot import the constants). A rename
    on one side has to fail here rather than silently return an empty list."""
    assert event in DASHBOARD.read_text()


# --- the connector half ------------------------------------------------------------------------

@pytest.mark.skipif(not CONNECTOR.exists(), reason="connector record not written yet")
def test_the_connector_emits_the_fields_the_sweep_does():
    """`email_labeled_todo` has two producers too — the n8n Gmail Trigger (fast, ~60s) and the
    sweep (authoritative, every few minutes). The connector is a versioned record of a workflow
    outside git, so this reads it as text; it is still worth pinning, because a field the
    connector drops is one the board renders blank for exactly the tasks that arrived quickly.
    """
    ours = opened_body({"upstream_id": "m1"}, user_id="rods", label="aware/todo", when=1)
    source = CONNECTOR.read_text()
    missing = [f for f in ours if f not in source]
    assert not missing, f"the connector never mentions {missing}"


@pytest.mark.skipif(not CONNECTOR.exists(), reason="connector record not written yet")
def test_the_connector_and_the_sweep_agree_on_the_label_and_the_event_name():
    source = CONNECTOR.read_text()
    assert OPENED_EVENT in source
    assert "aware/todo" in source
