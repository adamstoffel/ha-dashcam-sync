"""Tests for the release gate scripts (run with `pytest scripts`)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import check_hardware_gate as gate  # noqa: E402
import check_versions as versions  # noqa: E402

ROWS = "\n".join(
    f"| Cam {d} | {d} | {a} | pass | pass | pass | pass | n/a | pass | pass |"
    for d in gate.STABLE_DRIVERS
    for a in gate.ARCHES
)
HEADER = "| camera | driver | arch | detect | list | resume | verify | remux | restore | purge |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"


def test_versions_agree_with_the_current_tag() -> None:
    current = next(iter(set(versions.collect().values())))
    assert versions.check(None) == []
    assert versions.check(f"v{current}") == []


def test_versions_reject_a_mismatched_tag() -> None:
    problems = versions.check("v1999.1.0")
    assert problems and all("tag is 1999.1.0" in p for p in problems)


def test_gate_accepts_a_full_passing_matrix() -> None:
    assert gate.validate(HEADER + ROWS) == []


def test_gate_template_is_incomplete_by_design() -> None:
    template = (SCRIPTS.parent / "docs/hardware-gate/TEMPLATE.md").read_text()
    assert gate.validate(template)  # empty cells must not pass


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda t: t.replace("| pass | pass | pass | pass | n/a | pass | pass |", "| pass | fail | pass | pass | n/a | pass | pass |", 1), "list = fail"),
        (lambda t: "\n".join(t.splitlines()[:-1]), "no camera tested for driver http_index on aarch64"),
        (lambda t: t.replace("novatek | amd64", "novatek | armv7"), "no camera tested for driver novatek on amd64"),
    ],
)
def test_gate_blocks_failures_and_gaps(mutation, expected: str) -> None:  # type: ignore[no-untyped-def]
    problems = gate.validate(mutation(HEADER + ROWS))
    assert any(expected in p for p in problems), problems


def test_gate_main(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert gate.main(["x", "v2026.12.0b3"]) == 0  # betas skip the gate
    assert gate.main(["x", "v1900.1.0"]) == 1  # no results file
    assert "does not exist" in capsys.readouterr().out
    assert gate.validate("no table here") == ["no table rows found"]
