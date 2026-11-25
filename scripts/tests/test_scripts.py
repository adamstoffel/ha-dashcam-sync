"""Tests for the release gate scripts (run with `pytest scripts`)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import check_hardware_gate as gate
import check_versions as versions

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
        (
            lambda t: t.replace(
                "| pass | pass | pass | pass | n/a | pass | pass |",
                "| pass | fail | pass | pass | n/a | pass | pass |",
                1,
            ),
            "list = fail",
        ),
        (
            lambda t: "\n".join(t.splitlines()[:-1]),
            "no camera tested for driver http_index on aarch64",
        ),
        (
            lambda t: t.replace("novatek | amd64", "novatek | armv7"),
            "no camera tested for driver novatek on amd64",
        ),
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


# ---- the Supervisor role checker -------------------------------------------------------------

import check_supervisor_roles as roles

EXCERPT = (Path(__file__).parent / "fixtures" / "security_excerpt.py").read_text()


def test_roles_checker_accepts_the_real_rules_excerpt() -> None:
    assert roles.check(EXCERPT) == []


def test_roles_checker_notices_when_an_endpoint_is_closed_to_the_default_role() -> None:
    narrowed = EXCERPT.replace(
        'r"|/.+/info"\n            r")$"', 'r"|/host/info"\n            r")$"'
    )
    problems = roles.check(narrowed)
    assert any("GET /network/info" in p for p in problems)
    closed_discovery = EXCERPT.replace('r"|/discovery.*"\n        ', "")
    assert any("/discovery" in p for p in roles.check(closed_discovery))
    no_self = EXCERPT.replace('r"|/addons/self/(?!security|update)[^/]+"\n        ', "")
    assert any("/addons/self/options" in p for p in roles.check(no_self))


def test_roles_checker_reports_a_changed_layout_instead_of_passing() -> None:
    assert "layout changed" in roles.check("x = 1")[0]
    assert "layout changed" in roles.check("def broken(:")[0]


def test_the_endpoints_listed_are_the_ones_the_app_calls() -> None:
    src = (
        SCRIPTS.parent / "app/dashcam_sync/src/dashcam_sync/supervisor.py"
    ).read_text()
    used_in_app = set(
        re.findall(r'"/(?:network/info|discovery|addons/self/[a-z]+)', src)
    )
    listed = {path.split("/", 2)[1] for _, path in roles.USED}
    assert {"network", "discovery", "addons"} <= listed
    assert any("/network/info" in u for u in used_in_app) and any(
        "/discovery" in u for u in used_in_app
    )
