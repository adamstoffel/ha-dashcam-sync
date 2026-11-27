"""Static checks hassfest and the Marketplace would also make."""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path

import yaml

from custom_components.dashcam_sync import const

ROOT = Path(__file__).resolve().parent.parent
INTEGRATION = ROOT.parent / "custom_components" / "dashcam_sync"
MANIFEST = json.loads((INTEGRATION / "manifest.json").read_text())
STRINGS = json.loads((INTEGRATION / "strings.json").read_text())


def test_manifest_has_what_the_catalog_requires() -> None:
    for key in ("domain", "documentation", "issue_tracker", "codeowners", "name", "version"):
        assert MANIFEST[key], key
    assert MANIFEST["domain"] == "dashcam_sync" == const.DOMAIN
    assert MANIFEST["iot_class"] == "local_push" and MANIFEST["config_flow"] is True
    assert re.match(r"^\d{4}\.\d{1,2}\.\d+(b\d+)?$", MANIFEST["version"])
    assert MANIFEST["codeowners"][0].startswith("@")
    assert list(MANIFEST) == sorted(
        MANIFEST, key=lambda k: (k not in ("domain", "name"), k)
    )  # hassfest order


def test_version_matches_the_app() -> None:
    app = yaml.safe_load((ROOT.parent / "app/dashcam_sync/config.yaml").read_text())
    assert MANIFEST["version"] == app["version"]


def test_hacs_json() -> None:
    hacs = json.loads((ROOT.parent / "hacs.json").read_text())
    assert hacs["name"] and hacs["homeassistant"] == "2026.11.0"


def test_brand_images() -> None:
    for name, minimum in (("icon.png", 256), ("logo.png", 128)):
        data = (INTEGRATION / "brand" / name).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", data[16:24])
        assert min(width, height) >= minimum


def test_english_translations_are_strings_json() -> None:
    assert json.loads((INTEGRATION / "translations/en.json").read_text()) == STRINGS


def test_every_entity_has_a_name_and_an_icon() -> None:
    icons = json.loads((INTEGRATION / "icons.json").read_text())["entity"]
    keys: dict[str, set[str]] = {}
    for platform, items in STRINGS["entity"].items():
        keys[platform] = set(items)
        assert all("name" in v for v in items.values()), platform
    for platform, items in keys.items():
        assert items <= set(icons[platform]), (platform, items - set(icons[platform]))
    # Every translation_key used in the code exists in the strings.
    used = set(
        re.findall(
            r'(?:super\(\).__init__\(coordinator, |DashcamSyncEntity.__init__\(\s*self,\s*coordinator,\s*)"(\w+)"',
            "".join(p.read_text() for p in INTEGRATION.glob("*.py")),
        )
    )
    known = {k for items in keys.values() for k in items}
    assert used <= known, used - known


def test_services_are_described_everywhere() -> None:
    services = yaml.safe_load((INTEGRATION / "services.yaml").read_text())
    icons = json.loads((INTEGRATION / "icons.json").read_text())["services"]
    assert set(services) == set(STRINGS["services"]) == set(icons)
    assert set(services) == {
        const.SERVICE_SYNC, const.SERVICE_KEEP_CLIP, const.SERVICE_UNKEEP_CLIP,
        const.SERVICE_DELETE_CLIP, const.SERVICE_PURGE,
    }  # fmt: skip
    for name, spec in services.items():
        for field in (spec or {}).get("fields", {}):
            assert field in STRINGS["services"][name]["fields"], (name, field)


def test_every_app_issue_has_a_translation() -> None:
    app_src = (ROOT.parent / "app/dashcam_sync/src/dashcam_sync/daemon.py").read_text()
    keys = set(re.findall(r'add\(\s*"(\w+)"', app_src)) | {
        const.ISSUE_API_MISMATCH, const.ISSUE_ANNOUNCED_ADAPTER, const.ISSUE_APP_UNREACHABLE,
    }  # fmt: skip
    assert keys <= set(STRINGS["issues"]), keys - set(STRINGS["issues"])
    for key in ("subnet_overlap", "default_route_changed", "camera_playback_mode", "storage_low"):
        assert "fix_flow" in STRINGS["issues"][key]  # the fixable ones


def test_flow_steps_have_titles() -> None:
    for step in STRINGS["config"]["step"].values():
        assert step["title"]
    for name, step in STRINGS["options"]["step"].items():
        assert step["title"], name


def test_blueprint_is_importable() -> None:
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor("!input", lambda loader, node: f"INPUT:{loader.construct_scalar(node)}")
    blueprint = yaml.load(
        (ROOT.parent / "blueprints/automation/dashcam_sync/notify_event_clip.yaml").read_text(),
        Loader,
    )
    meta = blueprint["blueprint"]
    assert meta["domain"] == "automation" and meta["source_url"].startswith("https://")
    declared = set(meta["input"])
    used = {
        v.removeprefix("INPUT:") for v in json.dumps(blueprint).split('"') if v.startswith("INPUT:")
    }
    assert used <= declared, used - declared
    assert (blueprint["triggers"][0]["to"]) == "event_clip"


def test_quality_scale_marks_every_rule() -> None:
    rules = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text())["rules"]
    for name, rule in rules.items():
        status = rule if isinstance(rule, str) else rule["status"]
        assert status in ("done", "exempt", "todo"), name
        assert status != "todo", f"{name} is still todo"
        if status == "exempt":
            assert rule["comment"], name  # exempt needs a reason


def _strings(node: object, path: str = ""):  # type: ignore[no-untyped-def]
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, f"{path}.{key}")
    elif isinstance(node, str):
        yield path, node


def test_strings_follow_hassfest_rules() -> None:
    """The rules hassfest enforced on the first CI run, checked locally."""
    for path, text in _strings(STRINGS):
        assert "http://" not in text and "https://" not in text, f"URL in {path}: use a placeholder"
        assert not re.search(r"<[^>]+>", text), f"HTML-like text in {path}"
    for key, issue in STRINGS["issues"].items():
        assert not ("description" in issue and "fix_flow" in issue), (
            f"{key}: description beside fix_flow"
        )


def test_service_targets_use_only_supported_filters() -> None:
    services = yaml.safe_load((INTEGRATION / "services.yaml").read_text())
    for name, spec in services.items():
        assert "device" not in (spec or {}).get("target", {}), name
