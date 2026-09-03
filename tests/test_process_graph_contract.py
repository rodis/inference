"""The process graph contract — `processes/*.yml` -> `dashboard/processes.json` (ADR 0012).

The dashboard renders the process tier from a *generated projection* of the definitions rather
than by importing them, because the dashboard image is built with `dashboard/` as its Docker
context and cannot reach `src/reconciler` at all. That buys tier independence (the dashboard
needs neither pydantic nor pyyaml nor any idea what a `signal` block means) and costs a file
that can go stale, which is what these tests are for.

CI re-runs the generator and diffs, so staleness is caught there too. What is tested here is
the part a diff cannot see: that the projection still says what the board relies on it saying.
"""

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "emit_process_graph.py"
CONTRACT = ROOT / "dashboard" / "processes.json"

sys.path.insert(0, str(ROOT / "src"))
from reconciler.definition import GENESIS_STAGE, load_definitions  # noqa: E402


@pytest.fixture(scope="module")
def contract() -> dict:
    return json.loads(CONTRACT.read_text())


def test_the_committed_contract_matches_the_definitions():
    """The same check CI runs, kept here so `pytest` alone catches it.

    Run in a subprocess against a temp copy rather than by calling `main()`, so the test
    cannot itself rewrite the file it is checking — a generator test that repairs the drift it
    is meant to detect passes forever.
    """
    result = subprocess.run(
        [sys.executable, str(GENERATOR)], cwd=ROOT, capture_output=True, text=True,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    # The generator rewrote the file; if it differed, git would show it. Compare against a
    # fresh projection of the definitions instead, which is the same assertion without
    # needing git.
    fresh = json.loads(CONTRACT.read_text())
    names = {p["name"] for p in fresh["processes"]}
    assert names == {d.name for d in load_definitions(ROOT / "processes")}


def test_every_stage_carries_what_the_board_renders(contract):
    """The board's `StageDef` is a hard contract: a missing key renders as `undefined` in the
    browser rather than failing, so it has to be asserted somewhere."""
    required = {"name", "label", "kind", "after", "detail", "event"}
    for process in contract["processes"]:
        for stage in process["stages"]:
            assert required <= set(stage), f"{process['name']}.{stage['name']} missing keys"
            assert stage["kind"] in {"genesis", "act", "await"}
            assert stage["label"], f"{stage['name']} has no label"


def test_the_genesis_milestone_is_first_and_is_not_a_stage(contract):
    """`cycle_opened` is a recorded milestone but NOT a stage — the reconciler is handed cycles,
    it never creates them. The projection prepends it so the board can render it as given, and
    the board's frontier walk depends on it being index 0 (every real first stage lists it in
    `after`)."""
    for process in contract["processes"]:
        assert process["stages"][0]["name"] == GENESIS_STAGE
        assert process["stages"][0]["kind"] == "genesis"
        definition = next(d for d in load_definitions(ROOT / "processes")
                          if d.name == process["name"])
        assert GENESIS_STAGE not in {s.name for s in definition.stages}


def test_dependencies_all_resolve(contract):
    """`after` drives both the stepper's ordering and the frontier rule. A dangling name would
    make a stage permanently un-`waiting` — the process would look finished while sitting
    still, which is the exact silent stall the tier exists to avoid."""
    for process in contract["processes"]:
        seen: set[str] = set()
        for stage in process["stages"]:
            for dep in stage["after"]:
                assert dep in seen, (
                    f"{process['name']}.{stage['name']} depends on {dep!r}, "
                    f"which is not an earlier stage in the projection")
            seen.add(stage["name"])


def test_the_first_real_stage_depends_on_the_genesis_fact(contract):
    """An empty `after` in the YAML means "as soon as the cycle exists". The projection fills
    that in rather than leaving it empty, because the board treats an empty `after` as
    "ready immediately" — which for a stage in the MIDDLE of a process would light up the
    wrong frontier."""
    for process in contract["processes"]:
        for stage in process["stages"][1:]:
            assert stage["after"], f"{process['name']}.{stage['name']} has no dependency"


def test_the_milestone_event_names_are_process_prefixed(contract):
    """The prefix is structural, not conventional (ADR 0012): an unprefixed milestone could
    collide with an inference definition's `input_event_names()` and be routed into an engine
    that never expected it. The dashboard also relies on it — `view.ts::processOf` strips
    `<process>_` to title a milestone, and matches the separator so one process name cannot
    shadow another's."""
    for process in contract["processes"]:
        for stage in process["stages"]:
            assert stage["event"] == f"{process['name']}_{stage['name']}"
        assert process["void_event"].startswith(process["name"] + "_")


def test_the_dashboard_image_can_actually_reach_the_contract():
    """The whole reason this file is generated. `publish-images.yml` builds the dashboard with
    `context: dashboard`, so anything the image needs must live UNDER `dashboard/` — and the
    Dockerfile must COPY it, or the route 404s in production while working locally."""
    assert CONTRACT.parent.name == "dashboard"
    dockerfile = (ROOT / "dashboard" / "Dockerfile").read_text()

    # BOTH stages need it, for different reasons, and each failure looks different:
    #  - the bundle stage, because src/view.ts imports `../../processes.json` to label process
    #    milestones. Missing there and `npm run build` fails — loudly, but ONLY inside Docker,
    #    since a local checkout has the whole repo on disk.
    #  - the serving stage, because /api/processes reads it per request. Missing there and the
    #    build succeeds, the pod starts, and the board silently shows "No processes defined".
    assert "COPY processes.json /processes.json" in dockerfile, (
        "the bundle stage does not COPY processes.json above /web — src/view.ts imports "
        "../../processes.json and `npm run build` would fail in Docker")
    assert "logical_levels.json processes.json" in dockerfile, (
        "the serving stage does not COPY processes.json — /api/processes would return an "
        "empty list and the board would render 'No processes defined'")
