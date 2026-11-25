"""The app manifest, translations and service files must stay consistent with the code."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from dashcam_sync import __version__
from dashcam_sync.options import AppOptions, parse_options

APP = Path(__file__).resolve().parent.parent / "dashcam_sync"
CONFIG: dict[str, Any] = yaml.safe_load((APP / "config.yaml").read_text())


def security_rating(config: dict[str, Any], custom_apparmor: bool) -> int:
    """The arithmetic from the security rating reference."""
    score = 5
    if config.get("ingress"):
        score += 2
    if custom_apparmor:
        score += 1
    if config.get("apparmor") is False:
        score -= 1
    if config.get("host_network"):
        score -= 1
    if config.get("hassio_role") in ("manager", "admin"):
        score -= 1
    if config.get("privileged") or config.get("full_access") or config.get("host_pid"):
        score -= 1
    return min(score, 8)


def test_security_rating_is_the_maximum() -> None:
    assert security_rating(CONFIG, (APP / "apparmor.txt").exists()) == 8
    assert CONFIG["hassio_role"] == "default" and "host_network" not in CONFIG
    assert not CONFIG.get("privileged") and "full_access" not in CONFIG
    assert CONFIG["ingress"] is True and CONFIG["init"] is False


def test_manifest_basics() -> None:
    assert CONFIG["version"] == __version__
    assert CONFIG["slug"] == "dashcam_sync"
    assert CONFIG["arch"] == ["amd64", "aarch64"]  # armhf, armv7 and i386 now warn
    assert CONFIG["homeassistant"] == "2026.11.0"
    assert CONFIG["discovery"] == ["dashcam_sync"]
    assert CONFIG["ports"] == {"8100/tcp": None} and CONFIG["ingress_port"] == 8099
    assert {m["type"] for m in CONFIG["map"]} == {"media", "share"}
    assert CONFIG["timeout"] == 60 and CONFIG["stage"] == "experimental"
    assert re.match(r"^\d{4}\.\d{1,2}\.\d+$", CONFIG["version"])  # CalVer


def test_defaults_in_config_match_the_code() -> None:
    parsed = parse_options(CONFIG["options"])
    assert parsed.model_dump() == AppOptions().model_dump()


def _flatten(options: dict[str, Any], schema: dict[str, Any], prefix: str = "") -> list[str]:
    problems = []
    for key in options:
        if key not in schema:
            problems.append(f"{prefix}{key} has no schema")
    return problems


def test_schema_covers_every_option() -> None:
    schema = CONFIG["schema"]
    assert _flatten(CONFIG["options"], schema) == []
    for section in ("storage", "retention", "sync"):
        assert _flatten(CONFIG["options"][section], schema[section], section + ".") == []
    camera_fields = set(schema["cameras"][0])
    from dashcam_sync.options import CameraOptions

    assert camera_fields == set(CameraOptions.model_fields)
    # The schema keeps to the Supervisor's two-level nesting limit.
    assert all(not isinstance(v, (dict, list)) for v in schema["cameras"][0].values())


def test_translations_name_every_option() -> None:
    tr = yaml.safe_load((APP / "translations" / "en.yaml").read_text())["configuration"]
    for key in CONFIG["schema"]:
        assert tr[key]["name"] and tr[key]["description"], key
    for section in ("cameras", "storage", "retention", "sync"):
        schema = CONFIG["schema"][section]
        fields = set(schema[0] if isinstance(schema, list) else schema)
        assert fields == set(tr[section]["fields"]), section
        for name, entry in tr[section]["fields"].items():
            assert entry["name"] and entry["description"], f"{section}.{name}"


def test_service_files() -> None:
    s6 = APP / "rootfs/etc/s6-overlay/s6-rc.d"
    assert (s6 / "dashcam-syncd/type").read_text().strip() == "longrun"
    assert (s6 / "user/contents.d/dashcam-syncd").exists()
    for script in ("run", "finish"):
        path = s6 / "dashcam-syncd" / script
        assert os.access(path, os.X_OK) and path.read_text().startswith("#!")
    assert "dashcam_sync.daemon" in (s6 / "dashcam-syncd/run").read_text()


def test_dockerfile_and_requirements() -> None:
    dockerfile = (APP / "Dockerfile").read_text()
    assert (
        "--require-hashes" in dockerfile and "ffmpeg" in dockerfile and "BUILD_FROM" in dockerfile
    )
    reqs = (APP / "requirements.txt").read_text()
    for name in ("aiohttp==", "pydantic==", "defusedxml=="):
        assert name in reqs
    assert reqs.count("--hash=sha256:") > 20
    assert (APP / "icon.png").exists() and (APP / "logo.png").exists()


def test_apparmor_is_small_and_explicit() -> None:
    profile = (APP / "apparmor.txt").read_text()
    assert profile.count("{") == profile.count("}")
    for needle in ("/data/**", "/media/**", "/share/**", "network inet", "/usr/bin/ffmpeg"):
        assert needle in profile
    assert "capability sys_admin" not in profile and "capability net_admin" not in profile


def test_docs_carry_the_overlap_guide() -> None:
    docs = (APP / "DOCS.md").read_text()
    for needle in (
        "What the issue is.",
        "How to check it.",
        "How to fix it, if needed.",
        "claiming only a block of four addresses",
        "192.168.1.252",
        "travel router",
    ):
        assert needle in docs, needle


@pytest.mark.parametrize("name", ["repository.yaml"])
def test_repository_yaml(name: str) -> None:
    data = yaml.safe_load((APP.parent / name).read_text())
    assert data["name"] and data["url"].startswith("https://") and data["maintainer"]


def test_every_option_is_documented_in_docs_md() -> None:
    docs = (APP / "DOCS.md").read_text()
    names: set[str] = set()
    for section in ("storage", "retention", "sync"):
        names.update(CONFIG["options"][section])
    from dashcam_sync.options import CameraOptions

    names.update(CameraOptions.model_fields)
    names.discard("name")  # documented as "name", too generic to grep
    missing = sorted(
        n for n in names if f"`{n}`" not in docs and n.replace("network_", "") not in docs
    )
    assert missing == []


def test_every_api_route_is_documented() -> None:
    from aiohttp import web

    from dashcam_sync import api

    app = web.Application()
    api._add_routes(app)
    docs = (APP.parent.parent / "docs" / "api.md").read_text()
    normalised = re.sub(r"\{[^}]+\}", "{}", docs)
    missing = []
    for resource in app.router.resources():
        path = resource.canonical.removeprefix("/api/v1")
        if re.sub(r"\{[^}]+\}", "{}", path) not in normalised:
            missing.append(path)
    assert missing == []
