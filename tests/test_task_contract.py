"""One event name, two producers — kept in agreement (email todo tasks).

`email_task_closed` is emitted by both the reconciler's sweep and the dashboard's tick, and
`email_labeled_todo` by both the sweep and the n8n connector. A consumer that could tell them
apart would be reading one of them wrong, so their shapes have to match.

They cannot simply share a module. The dashboard image is built with `dashboard/` as its Docker
context, so `src/reconciler` is not importable there — the same build boundary that made
`dashboard/processes.json` a generated file. The duplication is therefore deliberate, and this
is what stops it drifting.

Everything here reads source as **text or AST**, never by importing: `dashboard/app.py` needs
fastapi and psycopg, and CI's python job installs neither.
"""

import ast
import pathlib

import pytest

from reconciler.tasks import (
    CLOSED_EVENT,
    CLOSED_VIA_DASHBOARD,
    OPENED_EVENT,
    TASK_INGEST_APP,
    OpenTask,
    closed_body,
    opened_body,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard" / "app.py"
CONNECTOR = ROOT / "connectors" / "n8n" / "gmail-labeled-todo.workflow.ts"


def _module_constant(path: pathlib.Path, name: str):
    """A module-level literal, read without importing the module."""
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path.name} has no module-level {name}")


def test_the_dashboard_and_the_sweep_emit_the_same_close():
    """The whole point of this file.

    A field added to `closed_body` and not to the dashboard's tuple would mean a task closed by
    ticking carries less than one closed by the sweep — and which one you got would depend on
    how you happened to finish it.
    """
    ours = closed_body(OpenTask(upstream_id="m1", subject="s", from_name="n", opened_at=1),
                       user_id="rods", label="aware/todo", when=2,
                       closed_via=CLOSED_VIA_DASHBOARD)
    theirs = _module_constant(DASHBOARD, "TASK_CLOSED_FIELDS")
    assert set(ours) == set(theirs), (
        "reconciler.tasks.closed_body and dashboard/app.py::TASK_CLOSED_FIELDS disagree; "
        f"only in the reconciler: {set(ours) - set(theirs)}; "
        f"only in the dashboard: {set(theirs) - set(ours)}")


def test_the_dashboard_posts_to_the_ingest_app_the_sweep_uses():
    """`source_app` is the discriminator the timeline and the board filter on, so a task closed
    by ticking must not land under a different app from one closed by the sweep."""
    assert f"/sensors/{TASK_INGEST_APP}" in DASHBOARD.read_text()


@pytest.mark.parametrize("event", [OPENED_EVENT, CLOSED_EVENT])
def test_the_event_names_are_written_out_in_the_dashboard_sql(event):
    """The board's SQL names both events as literals (it cannot import the constants). A rename
    on one side has to fail here rather than silently return an empty list."""
    assert event in DASHBOARD.read_text()


# --- the connector half ------------------------------------------------------------------------

@pytest.mark.skipif(not CONNECTOR.exists(), reason="connector record not written yet")
def test_the_connector_emits_the_fields_the_sweep_does():
    """`email_labeled_todo` has two producers too — the n8n Gmail Trigger (fast, ~60s) and the
    sweep (authoritative, hourly). The connector is a versioned record of a workflow that runs
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
