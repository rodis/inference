"""Composition and scheduling (ADR 0012).

`app` is where a run gets wired; `flow.py` and `run.py` are two thin skins over it. What is
worth pinning here is the small amount of *logic* that lives at that seam — which sequence
number a new cycle takes, and which period a scheduled open covers — plus the one piece of
duplication the design could not avoid: the cron appears in the process definition AND in
`prefect.yaml`, and nothing but a test keeps them honest.
"""

import pathlib
from datetime import date

import pytest
import yaml

from reconciler import app
from reconciler.core import Cycle
from reconciler.definition import load_definitions

PROCESSES = pathlib.Path(__file__).resolve().parents[1] / "processes"
PREFECT_YAML = pathlib.Path(__file__).resolve().parents[1] / "prefect.yaml"


def _definition(name="dreamhost_invoice"):
    return next(d for d in load_definitions(PROCESSES) if d.name == name)


def _cycle(key):
    return Cycle(key=key, process="dreamhost_invoice", user_id="rods", opened_at=0, context={})


# --- the sequence a new cycle takes ------------------------------------------------------------

def test_the_sequence_is_read_back_out_of_the_key():
    """Generated and compared rather than parsed: the same `str.format` that minted the key
    reads it back, so a change to the `{seq:03d}` spec cannot desynchronise the two."""
    definition = _definition()
    assert app.sequence_of(definition, "dh_invoice_2026_008", 2026) == 8
    assert app.sequence_of(definition, "dh_invoice_2026_008", 2025) is None   # wrong year
    assert app.sequence_of(definition, "something_else", 2026) is None


def test_the_next_sequence_is_one_past_the_highest_used_this_year():
    definition = _definition()
    cycles = [_cycle("dh_invoice_2026_007"), _cycle("dh_invoice_2026_008")]
    assert app.next_sequence(definition, cycles, 2026) == 9


def test_a_year_with_no_cycles_starts_at_one():
    assert app.next_sequence(_definition(), [], 2027) == 1


def test_last_year_s_cycles_do_not_advance_this_year_s_sequence():
    """The sequence is PER YEAR (the invoice number is not the month — that mistake is what
    the old design's padding bug came from), so January must restart at 1."""
    definition = _definition()
    cycles = [_cycle("dh_invoice_2026_011"), _cycle("dh_invoice_2027_001")]
    assert app.next_sequence(definition, cycles, 2027) == 2
    assert app.next_sequence(definition, cycles, 2028) == 1


def test_a_gap_left_by_a_hand_opened_cycle_is_not_reused():
    """One past the HIGHEST, not the first free slot. A number already sent to a client must
    never be minted again, and a gap is cheaper than a collision."""
    definition = _definition()
    cycles = [_cycle("dh_invoice_2026_001"), _cycle("dh_invoice_2026_005")]
    assert app.next_sequence(definition, cycles, 2026) == 6


# --- the period a scheduled open covers --------------------------------------------------------

@pytest.mark.parametrize("today,expected", [
    (date(2026, 9, 1), {"start": "2026-08-01", "end": "2026-08-31"}),
    (date(2026, 3, 1), {"start": "2026-02-01", "end": "2026-02-28"}),   # short month
    (date(2028, 3, 1), {"start": "2028-02-01", "end": "2028-02-29"}),   # leap year
    (date(2027, 1, 1), {"start": "2026-12-01", "end": "2026-12-31"}),   # across the year
])
def test_a_cron_on_the_first_invoices_the_month_that_just_ended(today, expected):
    assert app.previous_month(today) == expected


# --- the duplication that needed a guard -------------------------------------------------------

def test_every_scheduled_opener_has_a_prefect_deployment_on_the_same_cron():
    """The definition says WHEN a cycle opens; Prefect is what makes it happen. Those are two
    files, and a cron edited in one and not the other fails silently — as a month with no
    invoice, noticed when someone wonders why they were not paid."""
    deployments = yaml.safe_load(PREFECT_YAML.read_text())["deployments"]
    scheduled_crons = {c["cron"] for d in deployments for c in (d.get("schedules") or [])}

    for definition in load_definitions(PROCESSES):
        for opener in definition.opens:
            if opener.via == "schedule":
                assert opener.cron in scheduled_crons, (
                    f"{definition.name} opens on {opener.cron!r} but no prefect.yaml "
                    f"deployment is scheduled for it")


def test_the_deployments_point_at_flows_that_exist():
    """An entrypoint typo is only discovered at the first scheduled run — which for a monthly
    opener is up to a month later."""
    import ast

    deployments = yaml.safe_load(PREFECT_YAML.read_text())["deployments"]
    root = PREFECT_YAML.parent

    for deployment in deployments:
        path, _, func = deployment["entrypoint"].partition(":")
        source = (root / path).read_text()
        # Parsed, not imported: prefect is an optional extra and CI does not install it.
        names = {n.name for n in ast.walk(ast.parse(source))
                 if isinstance(n, ast.FunctionDef)}
        assert func in names, f"{deployment['name']} points at missing {path}:{func}"


def test_the_wiring_imports_with_no_third_party_packages_installed():
    """CI installs with `pip --no-deps`, so every module a test reaches must import against a
    bare interpreter. `app.py` pulls in every adapter, and `adapters/neon.py` had a
    module-level `import psycopg` — which collapsed the ENTIRE pytest collection in CI while
    passing locally, where psycopg is installed. Simulated here rather than discovered there.
    """
    import importlib
    import sys

    # Exactly what CI does NOT install: it has pytest, ruff, pydantic and pyyaml,
    # then `pip install -e . --no-deps`. Everything else must be imported lazily.
    blocked = {"psycopg", "dotenv", "prefect", "anthropic", "quixstreams"}
    saved = {k: v for k, v in sys.modules.items()
             if k.startswith("reconciler") or k.split(".")[0] in blocked}
    try:
        for name in list(sys.modules):
            if name.startswith("reconciler") or name.split(".")[0] in blocked:
                del sys.modules[name]
        for name in blocked:
            sys.modules[name] = None      # makes `import <name>` raise ImportError

        importlib.import_module("reconciler.app")
        importlib.import_module("reconciler.run")
    finally:
        for name in list(sys.modules):
            if name.startswith("reconciler") or name.split(".")[0] in blocked:
                del sys.modules[name]
        sys.modules.update(saved)


def test_no_worker_image_directory_was_added_for_the_reconciler():
    """ADR 0012, explicitly: there must be NO `workers/reconciler/`. That path is
    auto-discovered by publish-images.yml, which would start building an image and bumping a
    manifest that does not exist. The tier runs on Prefect's infrastructure, not the cluster."""
    assert not (PREFECT_YAML.parent / "workers" / "reconciler").exists()
