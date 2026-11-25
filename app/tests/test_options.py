from __future__ import annotations

import json
from pathlib import Path

import pytest

from dashcam_sync.options import (
    AppOptions,
    CameraOptions,
    invalid_camera_entries,
    load_options,
    parse_options,
    validate_cameras,
)


def cam(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "car",
        "name": "Car",
        "connection": "hotspot",
        "interface": "wlan0",
        "ssid": "DASH",
        "driver": "auto",
        "enabled": True,
    }
    base.update(kw)
    return base


def test_defaults() -> None:
    opts = AppOptions()
    assert opts.storage.target == "local"
    assert opts.retention.normal_days == 14
    assert opts.retention.network_normal_days == 90
    assert opts.sync.mode == "events_first"


def test_camera_id_pattern_enforced() -> None:
    with pytest.raises(ValueError):
        CameraOptions.model_validate({"id": "Bad ID"})
    assert CameraOptions.model_validate({"id": "ok_1"}).display_name == "ok_1"


def test_empty_strings_become_none() -> None:
    c = CameraOptions.model_validate({"id": "a", "ssid": "  ", "host": ""})
    assert c.ssid is None and c.host is None


def test_folder_must_be_plain() -> None:
    with pytest.raises(ValueError):
        parse_options({"storage": {"local_folder": "../etc"}})
    with pytest.raises(ValueError):
        parse_options({"storage": {"local_folder": ".hidden"}})


def test_purge_time_validation() -> None:
    assert parse_options({"retention": {"purge_time": "04:15:00"}}).retention.purge_time == "04:15"
    with pytest.raises(ValueError):
        parse_options({"retention": {"purge_time": "25:00"}})


def test_bad_entry_does_not_stop_the_rest(tmp_path: Path) -> None:
    raw = {"cameras": [cam(), {"id": "BAD ID"}, "junk"]}
    path = tmp_path / "options.json"
    path.write_text(json.dumps(raw))
    opts, problems = load_options(path)
    assert [c.id for c in opts.cameras] == ["car"]
    assert set(problems) == {"BAD ID", "#2"}
    assert invalid_camera_entries(raw) == problems


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    opts, problems = load_options(tmp_path / "nope.json")
    assert opts.cameras == [] and problems == {}


def test_options_must_be_object(tmp_path: Path) -> None:
    path = tmp_path / "o.json"
    path.write_text("[]")
    with pytest.raises(ValueError):
        load_options(path)


def test_validation_rules() -> None:
    opts = parse_options(
        {
            "cameras": [
                cam(id="a", interface=None),
                cam(id="b", ssid=None, interface="wlan1"),
                cam(id="c", connection="lan"),
                cam(id="d", static_address="bad", interface="wlan2"),
                cam(id="e", static_address="192.168.1.253/30", interface="wlan3"),
                cam(id="g", connection="lan", host="10.0.0.9"),
            ]
        }
    )
    problems = validate_cameras(opts)
    assert problems["a"] == ["hotspot_needs_interface"]
    assert problems["b"] == ["hotspot_needs_ssid"]
    assert problems["c"] == ["lan_needs_host"]
    assert problems["d"] == ["static_address_invalid"]
    assert "e" not in problems
    assert "g" not in problems


def test_only_one_hotspot_camera_per_interface() -> None:
    opts = parse_options({"cameras": [cam(id="a"), cam(id="b")]})
    problems = validate_cameras(opts)
    assert "a" not in problems
    assert problems["b"] == ["interface_in_use"]


def test_disabled_camera_does_not_hold_interface() -> None:
    opts = parse_options({"cameras": [cam(id="a", enabled=False), cam(id="b")]})
    assert validate_cameras(opts) == {}


def test_duplicate_ids() -> None:
    opts = parse_options(
        {"cameras": [cam(id="a", interface="wlan0"), cam(id="a", interface="wlan1")]}
    )
    assert "duplicate_id" in validate_cameras(opts)["a"]


def test_network_folder_rules() -> None:
    opts = parse_options(
        {
            "cameras": [cam()],
            "storage": {"target": "both", "network_folder": ""},
        }
    )
    assert validate_cameras(opts)["car"] == ["network_folder_missing"]
    opts = parse_options(
        {
            "cameras": [cam()],
            "storage": {"target": "network", "network_folder": "nas"},
        }
    )
    assert validate_cameras(opts, network_mount_ok=lambda f: False)["car"] == [
        "network_folder_not_mounted"
    ]
    assert validate_cameras(opts, network_mount_ok=lambda f: True) == {}
    assert validate_cameras(opts) == {}
