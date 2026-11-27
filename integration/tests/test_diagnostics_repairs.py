from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.repairs.websocket_api import RepairsFlowIndexView  # noqa: F401
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.dashcam_sync.api import DashcamSyncConnectionError
from custom_components.dashcam_sync.const import DOMAIN
from custom_components.dashcam_sync.issues import issue_id
from custom_components.dashcam_sync.repairs import DashcamSyncFixFlow, async_create_fix_flow
from custom_components.dashcam_sync.wifi import WifiUnavailable

from .conftest import FakeApi, setup_entry

ISSUES = [
    {"key": "camera_playback_mode", "camera": "family_car", "placeholders": {}},
    {"key": "subnet_overlap", "camera": "family_car",
     "placeholders": {"result": "device_in_block", "conflicts": ["192.168.1.255"], "block": "192.168.1.252 to 192.168.1.255"}},
    {"key": "default_route_changed", "camera": "family_car", "placeholders": {"reason": "default_route_moved"}},
    {"key": "storage_low", "camera": None, "placeholders": {"target": "local", "free_percent": 4.5}},
    {"key": "unsupported_camera", "camera": "family_car", "placeholders": {}},
    {"key": "no_wifi_adapter", "camera": None, "placeholders": {}},
]  # fmt: skip


async def test_diagnostics_are_redacted(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_api: FakeApi,
    hass_client: ClientSessionGenerator,
) -> None:
    fake_api.options["cameras"][0].update(
        {"ssid": "CAMWIFI", "wifi_password": "secretpw", "host": "192.168.1.254"}
    )
    diag = await get_diagnostics_for_config_entry(hass, hass_client, loaded)
    text = json.dumps(diag)
    for secret in (
        '"tok"',
        "CAMWIFI",
        "secretpw",
        "192.168.1.254",
        "local-dashcam-sync",
        "wlan0",
        "/media/dashcam_sync",
    ):
        assert secret not in text, secret
    assert diag["app_version"] == "2026.11.0" and diag["websocket_connected"] in (True, False)
    cam = diag["cameras"]["family_car"]
    assert cam["driver"] == "novatek" and cam["capabilities"]["delete"] is True
    assert diag["sessions"][0]["clips"] == 2 and "x.x.x.x" not in diag["sessions"][0].get(
        "error", ""
    )
    assert diag["storage"]["targets"]["local"]["online"] is True


async def test_diagnostics_when_the_app_is_down(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_api: FakeApi,
    hass_client: ClientSessionGenerator,
) -> None:
    fake_api.error = DashcamSyncConnectionError("down")
    diag = await get_diagnostics_for_config_entry(hass, hass_client, loaded)
    assert diag["sessions"] == [] and "options" in diag


async def test_app_issues_become_repair_issues_and_clear(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    registry = ir.async_get(hass)
    fake_api.push({"type": "snapshot", **{**fake_api.status, "issues": ISSUES}})
    await hass.async_block_till_done()
    expected = {issue_id(i["key"], i["camera"]) for i in ISSUES}
    assert expected <= {i.issue_id for i in registry.issues.values() if i.domain == DOMAIN}
    overlap = registry.async_get_issue(DOMAIN, "subnet_overlap_family_car")
    assert overlap.is_fixable and overlap.severity == ir.IssueSeverity.ERROR
    assert overlap.translation_placeholders["conflicts"] == "192.168.1.255"
    assert registry.async_get_issue(DOMAIN, "unsupported_camera_family_car").is_fixable is False
    assert (
        registry.async_get_issue(DOMAIN, "storage_low_hub").translation_placeholders["free_percent"]
        == "4.5"
    )

    fake_api.push({"type": "snapshot", **{**fake_api.status, "issues": ISSUES[:1]}})
    await hass.async_block_till_done()
    remaining = {i.issue_id for i in registry.issues.values() if i.domain == DOMAIN}
    assert (
        "camera_playback_mode_family_car" in remaining
        and "subnet_overlap_family_car" not in remaining
    )

    fake_api.push({"type": "snapshot", **{**fake_api.status, "issues": []}})
    await hass.async_block_till_done()
    assert not [i for i in registry.issues.values() if i.domain == DOMAIN]


async def test_api_version_mismatch_and_announced_adapter_issues(
    hass: HomeAssistant, fake_api: FakeApi, entry: MockConfigEntry
) -> None:
    fake_api.info["api_version"] = 7
    with patch(
        "custom_components.dashcam_sync.async_announced_wifi_adapters", return_value=["wlan0"]
    ):
        await setup_entry(hass, entry)
    registry = ir.async_get(hass)
    mismatch = registry.async_get_issue(DOMAIN, "api_mismatch")
    assert mismatch is not None and mismatch.translation_placeholders == {
        "app_api": "7",
        "integration_api": "1",
    }
    adapter = registry.async_get_issue(DOMAIN, "announced_wifi_adapter")
    assert adapter is not None and adapter.translation_placeholders == {"adapters": "wlan0"}
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get_issue(DOMAIN, "api_mismatch") is None  # cleaned up on unload


async def test_own_issues_are_cleared_when_fixed(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    from custom_components.dashcam_sync.issues import IssueSync

    sync = IssueSync(hass, loaded)
    sync.async_set_own("api_mismatch", True, app_api="2", integration_api="1")
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, "api_mismatch") is not None
    sync.async_set_own("api_mismatch", False)
    assert registry.async_get_issue(DOMAIN, "api_mismatch") is None
    sync.async_set_own("api_mismatch", False)  # idempotent


# ------------------------------------------------------------------- fix flows


async def run_flow(
    hass: HomeAssistant, loaded: MockConfigEntry, key: str, camera: str | None
) -> dict:
    flow = await async_create_fix_flow(
        hass, f"{key}_{camera}", {"entry_id": loaded.entry_id, "key": key, "camera": camera}
    )
    flow.hass = hass
    flow.handler = DOMAIN
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "confirm"
    return await flow.async_step_confirm({})


async def test_fix_restore(hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi) -> None:
    result = await run_flow(hass, loaded, "camera_playback_mode", "family_car")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert fake_api.calls_named("restore") == [("family_car",)]


async def test_fix_overlap_recheck(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    fake_api.overlap = {"result": "device_in_block", "blocked": True, "conflicts": []}
    result = await run_flow(hass, loaded, "subnet_overlap", "family_car")
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": "still_blocked"}
    fake_api.overlap = {"result": "overlap_block_free", "blocked": False, "conflicts": []}
    result = await run_flow(hass, loaded, "subnet_overlap", "family_car")
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_fix_default_route_disconnects_the_wifi(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    with patch(
        "custom_components.dashcam_sync.repairs.async_disconnect", new=AsyncMock()
    ) as disconnect:
        result = await run_flow(hass, loaded, "default_route_changed", "family_car")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    disconnect.assert_awaited_once_with(hass, "wlan0")
    with patch(
        "custom_components.dashcam_sync.repairs.async_disconnect", side_effect=WifiUnavailable("x")
    ):
        result = await run_flow(hass, loaded, "default_route_changed", "family_car")
    assert result["errors"] == {"base": "wifi_unavailable"}


async def test_fix_storage_low_runs_purge(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    result = await run_flow(hass, loaded, "storage_low", None)
    assert result["type"] is FlowResultType.CREATE_ENTRY and fake_api.calls_named("purge") == [
        (False,)
    ]


async def test_fix_failures(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    fake_api.error = DashcamSyncConnectionError("down")
    result = await run_flow(hass, loaded, "camera_playback_mode", "family_car")
    assert result["errors"] == {"base": "app_unavailable"}
    fake_api.error = None
    result = await run_flow(hass, loaded, "unsupported_camera", "family_car")
    assert result["errors"] == {"base": "not_fixable"}
    flow = DashcamSyncFixFlow({"entry_id": "missing", "key": "storage_low"})
    flow.hass = hass
    flow.handler = DOMAIN
    result = await flow.async_step_confirm({})
    assert result["errors"] == {"base": "app_unavailable"}


async def test_fix_flow_through_the_repairs_api(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_api: FakeApi,
    hass_client: ClientSessionGenerator,
) -> None:
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "repairs", {})
    fake_api.push({"type": "snapshot", **{**fake_api.status, "issues": ISSUES[:1]}})
    await hass.async_block_till_done()
    client = await hass_client()
    resp = await client.post(
        "/api/repairs/issues/fix",
        json={"handler": DOMAIN, "issue_id": "camera_playback_mode_family_car"},
    )
    assert resp.status == 200
    flow = await resp.json()
    assert flow["step_id"] == "confirm"
    resp = await client.post(f"/api/repairs/issues/fix/{flow['flow_id']}")
    assert (await resp.json())["type"] == "create_entry"
    assert fake_api.calls_named("restore")
    _ = pytest
