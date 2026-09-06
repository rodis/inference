"""The dashboard image ships every module `app.py` imports.

`dashboard/Dockerfile` copies its Python by **name**, not by directory:

    COPY app.py places.py logical_levels.json processes.json ./

That is deliberate — the build context is `dashboard/`, which also holds `web/`, `__pycache__`
and whatever else a working tree accumulates, and a blanket `COPY . .` would ship all of it. But
an allow-list has one failure mode, and it is the worst-shaped one available: adding a module is
a *silent* omission. Every local test passes (the file is on disk), `ruff` passes, the image
builds green, and the container then dies at start on `ModuleNotFoundError` — the first honest
signal arrives after CI, after the deploy-state force-push, and after Argo has rolled the pod.

`places.py` was added on 2026-09-06 and the COPY line was not updated. This test is what stops
the next one, and it is the same shape as `test_every_worker_image_has_a_manifest_to_bump`:
assert the thing the build needs against the thing a person edits.

Read as text, never imported — `app.py` needs fastapi and psycopg, which CI's python job does
not install (see `test_task_contract.py`).
"""

import ast
import pathlib

import pytest

DASHBOARD = pathlib.Path(__file__).resolve().parents[1] / "dashboard"
DOCKERFILE = DASHBOARD / "Dockerfile"


def _copied_names() -> set[str]:
    """Every path named on a `COPY` line in the runtime stage (the one that isn't `--from`)."""
    names: set[str] = set()
    for line in DOCKERFILE.read_text().splitlines():
        line = line.strip()
        if not line.startswith("COPY ") or "--from=" in line:
            continue
        # `COPY <src>... <dest>` — the last token is the destination.
        names.update(line.split()[1:-1])
    return names


def _local_module_imports() -> set[str]:
    """The top-level modules `app.py` imports that are files sitting beside it."""
    tree = ast.parse((DASHBOARD / "app.py").read_text())
    siblings = {p.stem for p in DASHBOARD.glob("*.py")} - {"app"}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found & siblings


def test_app_imports_at_least_one_local_module():
    """Guards the guard: if `app.py` ever stops importing a sibling, the assertion below passes
    vacuously and this file quietly stops testing anything."""
    assert _local_module_imports(), "no sibling imports found — has app.py or this parser drifted?"


@pytest.mark.parametrize("module", sorted(_local_module_imports()))
def test_every_local_module_app_imports_is_copied_into_the_image(module):
    assert f"{module}.py" in _copied_names(), (
        f"dashboard/app.py imports `{module}`, but dashboard/Dockerfile never COPYs "
        f"{module}.py — the image would start and die on ModuleNotFoundError. Add it to the "
        f"COPY line in the runtime stage."
    )


def test_the_data_files_the_api_reads_are_copied():
    """`processes.json` and `logical_levels.json` are read at runtime by path. They have been in
    the image since they were introduced; this pins them so the same allow-list slip can't drop
    one — the symptom there is a 500 on one endpoint rather than a dead pod, which is quieter."""
    copied = _copied_names()
    for data in ("processes.json", "logical_levels.json"):
        assert data in copied, f"dashboard/Dockerfile no longer copies {data}"
