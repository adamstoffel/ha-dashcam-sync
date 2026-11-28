"""Tests for the release gate scripts (run with `pytest scripts`)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import check_ha_api as ha_api
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


# ---- CI triggers -----------------------------------------------------------------------------


def test_ci_runs_on_main_and_pull_requests() -> None:
    ci = (SCRIPTS.parent / ".github/workflows/ci.yml").read_text()
    assert "push:\n    branches: [main]" in ci and "pull_request:" in ci


def test_a_version_bump_on_main_releases_itself() -> None:
    """auto-release.yml decides; release.yml does the work, for a tag or when called."""
    wf = SCRIPTS.parent / ".github/workflows"
    auto, release = (wf / "auto-release.yml").read_text(), (wf / "release.yml").read_text()
    assert "push:\n    branches: [main]" in auto
    assert "git ls-remote --exit-code --tags origin" in auto  # an existing tag: nothing to do
    assert "check_versions.py" in auto and "check_hardware_gate.py" in auto
    assert "uses: ./.github/workflows/release.yml" in auto and "tag: ${{ needs.plan.outputs.tag }}" in auto
    assert 'tags: ["v*"]' in release and "workflow_call:" in release  # by hand or called
    assert "TAG: ${{ inputs.tag || github.ref_name }}" in release
    assert "tag_name: ${{ env.TAG }}" in release  # the release creates the tag last
    assert "github.ref_name" not in release.replace("inputs.tag || github.ref_name", "")


def _fake_ha(tmp_path: Path) -> Path:
    """A tiny stand-in for the homeassistant package, using Python 3.14's bare ``except A, B``."""
    pkg = tmp_path / "ha" / "homeassistant" / "helpers"
    pkg.mkdir(parents=True)
    (pkg.parent / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "target.py").write_text(
        "from os import path as re_exported\n"
        "class TargetSelection: ...\n"
        "def async_extract_referenced_entity_ids(): ...\n"
        "LIMIT = 3\n"
        "def risky():\n"
        "    try:\n"
        "        pass\n"
        "    except KeyError, ValueError:\n"
        "        pass\n"
    )
    (pkg / "service.py").write_text("def extract_entity_ids(): ...\n")
    (pkg / "entity_registry.py").write_text("")
    return tmp_path / "ha"


def _integration(tmp_path: Path, imports: str) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "mod.py").write_text(imports)
    return src


def test_ha_api_check_accepts_names_that_exist(tmp_path: Path) -> None:
    src = _integration(
        tmp_path,
        "from homeassistant.helpers.target import TargetSelection, LIMIT, re_exported\n"
        "from homeassistant.helpers import entity_registry as er\n"  # a submodule
        "from homeassistant.helpers.service import extract_entity_ids\n",
    )
    assert ha_api.missing_imports(_fake_ha(tmp_path), [src]) == []


def test_ha_api_check_finds_a_name_that_was_removed(tmp_path: Path) -> None:
    src = _integration(
        tmp_path,
        "from homeassistant.helpers.service import async_extract_referenced_entity_ids\n"
        "from homeassistant.helpers.target import TargetSelection\n",
    )
    [problem] = ha_api.missing_imports(_fake_ha(tmp_path), [src])
    assert (
        "homeassistant.helpers.service has no async_extract_referenced_entity_ids"
        in problem
    )
    assert problem.endswith("mod.py:1: " + problem.split(": ", 1)[1])


def test_ha_api_check_finds_a_module_that_was_removed(tmp_path: Path) -> None:
    src = _integration(tmp_path, "from homeassistant.helpers.gone import thing\n")
    [problem] = ha_api.missing_imports(_fake_ha(tmp_path), [src])
    assert "no module homeassistant.helpers.gone" in problem


def test_ha_api_check_ignores_everything_that_is_not_homeassistant(
    tmp_path: Path,
) -> None:
    src = _integration(
        tmp_path, "from os import nothing_here\nfrom .local import x\nimport json\n"
    )
    assert ha_api.missing_imports(_fake_ha(tmp_path), [src]) == []


def test_the_minimum_ha_version_comes_from_hacs_json() -> None:
    assert re.fullmatch(r"\d{4}\.\d{1,2}\.\d+", ha_api.minimum_version())


def test_install_on_ha_copies_both_parts_and_bumps_the_version(tmp_path: Path) -> None:
    """Runs the real script with a fake wget that serves this checkout as the tarball."""
    import os
    import subprocess
    import tarfile

    root = SCRIPTS.parent
    tarball = tmp_path / "src.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        for part in ("app/dashcam_sync", "custom_components/dashcam_sync"):
            tar.add(root / part, arcname=f"ha-dashcam-sync-x/{part}")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "wget").write_text(f"#!/bin/sh\ncat {tarball}\n")
    (bin_dir / "wget").chmod(0o755)
    apps, config = tmp_path / "local_apps", tmp_path / "config"
    apps.mkdir()
    config.mkdir()
    (config / "configuration.yaml").write_text("")
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "DASHCAM_APPS_DIR": str(apps),
        "DASHCAM_CONFIG_DIR": str(config),
    }
    run = subprocess.run(
        ["sh", str(SCRIPTS / "install-on-ha.sh"), "some-branch"],
        env=env, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert run.returncode == 0, run.stderr
    cfg = (apps / "dashcam_sync" / "config.yaml").read_text()
    assert "\nimage:" not in cfg
    version = re.search(r'^version: "(\d{4})\.(\d+)\.(\d{12})"$', cfg, re.MULTILINE)
    assert version, cfg[:300]
    assert (config / "custom_components" / "dashcam_sync" / "manifest.json").exists()
    assert "Check for updates" in run.stdout  # no `ha` command here: the manual steps
    assert os.access(SCRIPTS / "install-on-ha.sh", os.X_OK)

    # A second install right away (same second) must still count as newer, or the
    # Supervisor would not rebuild the changed source.
    again = subprocess.run(
        ["sh", str(SCRIPTS / "install-on-ha.sh"), "some-branch"],
        env=env, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert again.returncode == 0, again.stderr
    cfg2 = (apps / "dashcam_sync" / "config.yaml").read_text()
    version2 = re.search(r'^version: "\d{4}\.\d+\.(\d{12})"$', cfg2, re.MULTILINE)
    assert version2 and int(version2[1]) > int(version[3])
    assert not list(apps.glob(".dashcam_sync.*"))  # no staging or backup left behind


def test_ha_api_check_does_not_count_names_inside_functions_or_classes(
    tmp_path: Path,
) -> None:
    ha = _fake_ha(tmp_path)
    (ha / "homeassistant" / "helpers" / "nested.py").write_text(
        "class Example:\n"
        "    def removed_helper(self): ...\n"
        "def outer():\n"
        "    local_only = 1\n"
        "try:\n"
        "    from x import guarded\n"
        "except ImportError:\n"
        "    guarded = None\n"
        "if True:\n"
        "    conditional = 1\n"
    )
    src = _integration(
        tmp_path,
        "from homeassistant.helpers.nested import Example, guarded, conditional\n"
        "from homeassistant.helpers.nested import removed_helper, local_only\n",
    )
    problems = ha_api.missing_imports(ha, [src])
    assert len(problems) == 2
    assert any("no removed_helper" in p for p in problems)
    assert any("no local_only" in p for p in problems)
