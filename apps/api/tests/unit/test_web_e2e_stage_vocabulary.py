"""The bounded failure-stage vocabulary exists twice and must never drift.

`apps/web/scripts/check-test-reports.ts` is the authority: it validates a stage
marker before it may be persisted in a report. `apps/api/scripts/run_web_e2e.py`
holds a mirror because the runner prints the value into a CI log, where the same
strictness has to apply without importing TypeScript.

Two copies are unavoidable (a TypeScript module cannot be imported by the Python
runner), but a silent divergence would either reject a stage the helper really
emitted - degrading the diagnostic back to ``unknown`` - or let the runner print
an identifier the guard refused. This test pins both sets and their shared shape
to each other, so adding a stage to one list fails the suite until the other and
the pattern agree.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

API_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = API_ROOT / "scripts" / "run_web_e2e.py"
WEB_GUARD = API_ROOT.parent / "web" / "scripts" / "check-test-reports.ts"

#: The identifier shape both sides enforce before the vocabulary is consulted.
STAGE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("revnote_web_e2e_runner_stages", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _typescript_stages() -> tuple[str, ...]:
    """Read the frozen list out of the guard's own source, not a copy here."""
    text = WEB_GUARD.read_text(encoding="utf-8")
    match = re.search(r"export const STAGES = \[(.*?)\] as const", text, re.DOTALL)
    assert match is not None, "STAGES declaration moved; update this test with it"
    return tuple(re.findall(r"'([a-z][a-z0-9_]*)'", match.group(1)))


def test_both_vocabularies_are_the_same_set(runner: ModuleType) -> None:
    """Exact equality, in both directions: no extra name on either side."""
    typescript = _typescript_stages()
    assert typescript, "the guard declares no stage names"
    assert len(typescript) == len(set(typescript)), "the guard repeats a stage name"
    assert set(runner.STAGES) == set(typescript), (
        "stage vocabulary drift: typescript-only="
        f"{sorted(set(typescript) - set(runner.STAGES))} "
        f"python-only={sorted(set(runner.STAGES) - set(typescript))}"
    )


def test_every_stage_matches_the_shared_identifier_shape(runner: ModuleType) -> None:
    """One pattern is used by both guards, so one malformed name breaks both."""
    for name in runner.STAGES:
        assert STAGE_PATTERN.fullmatch(name), name
        assert len(name) <= 64, name
    assert "unknown" in runner.STAGES


def test_unknown_is_the_only_fallback(runner: ModuleType) -> None:
    """An absent, malformed or off-vocabulary marker degrades to ``unknown``."""
    assert runner.safe_stage({}) == "unknown"
    assert runner.safe_stage({"stage": None}) == "unknown"
    assert runner.safe_stage({"stage": ["takeover_ready"]}) == "unknown"
    assert runner.safe_stage({"stage": "Takeover_Ready"}) == "unknown"
    assert runner.safe_stage({"stage": "takeover ready"}) == "unknown"
    assert runner.safe_stage({"stage": "not_a_real_stage"}) == "unknown"
    # A real member survives unchanged, including the takeover markers this
    # suite relies on to separate "clicked" from "the new tree is committed".
    for name in runner.STAGES:
        assert runner.safe_stage({"stage": name}) == name
