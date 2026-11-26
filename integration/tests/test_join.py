"""Joining a hotspot camera's Wi-Fi when the app's panel asks for it."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dashcam_sync.api import DashcamSyncConnectionError
from custom_components.dashcam_sync.wifi import WifiInterface, WifiUnavailable

from .conftest import FakeApi

W = "custom_components.dashcam_sync.wifi"
ADAPTER = [WifiInterface("wlan0", connected=False, enabled=True, ssid=None)]


async def join(hass: HomeAssistant, fake_api: FakeApi) -> tuple:
    fake_api.push({"type": "join_wifi", "camera": "polestar_2"})
    await hass.async_block_till_done()
    results = fake_api.calls_named("join_result")
    assert len(results) == 1
    return results[0]


async def test_a_join_request_joins_and_reports_back(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    apply = AsyncMock()
    with (
        patch(f"{W}.async_list_wifi_interfaces", return_value=ADAPTER),
        patch(f"{W}.async_apply_profile", apply),
    ):
        assert await join(hass, fake_api) == ("polestar_2", "joined", None)
    profile = apply.await_args.args[1]
    assert profile.ssid == "CAMWIFI" and profile.address == "192.168.1.253" and profile.prefix == 30


@pytest.mark.parametrize(
    ("setup", "state", "reason_part"),
    [
        ({"list": WifiUnavailable("no client")}, "manual", "no client"),
        ({"apply": WifiUnavailable("refused")}, "manual", "refused"),
        ({"list": []}, "failed", "no_adapter"),
        ({"blocked": True}, "failed", "device_in_block"),
        ({"app_down": True}, "failed", "app_unavailable"),
    ],
)
async def test_every_join_outcome_is_reported(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_api: FakeApi,
    setup: dict,
    state: str,
    reason_part: str,
) -> None:
    if setup.get("blocked"):
        fake_api.overlap = {**fake_api.overlap, "blocked": True, "result": "device_in_block"}
    real_wifi = fake_api.async_wifi
    if setup.get("app_down"):

        async def down(camera: str) -> dict:
            raise DashcamSyncConnectionError("gone")

        fake_api.async_wifi = down  # type: ignore[method-assign]
    listing = setup.get("list", ADAPTER)
    list_mock = (
        AsyncMock(side_effect=listing)
        if isinstance(listing, Exception)
        else AsyncMock(return_value=listing)
    )
    apply_mock = AsyncMock(side_effect=setup.get("apply"))
    with (
        patch(f"{W}.async_list_wifi_interfaces", list_mock),
        patch(f"{W}.async_apply_profile", apply_mock),
    ):
        camera, got_state, reason = await join(hass, fake_api)
    fake_api.async_wifi = real_wifi  # type: ignore[method-assign]
    assert camera == "polestar_2" and got_state == state
    assert reason_part in (reason or "")
    if state == "failed":
        apply_mock.assert_not_awaited()  # never connect and hope


async def test_a_join_request_for_a_bad_camera_value_is_ignored(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    fake_api.push({"type": "join_wifi", "camera": None})
    await hass.async_block_till_done()
    assert fake_api.calls_named("join_result") == []


async def test_a_scan_request_is_answered_with_what_home_assistant_sees(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    from types import SimpleNamespace

    aps = [
        SimpleNamespace(ssid="CARDV-1234", signal=62, mode=SimpleNamespace(value="infrastructure")),
        SimpleNamespace(ssid="Home", signal=90, mode="infrastructure"),
    ]
    client = SimpleNamespace(network=SimpleNamespace(access_points=AsyncMock(return_value=aps)))
    with patch(f"{W}._client", return_value=client):
        fake_api.push({"type": "scan_wifi", "interface": "wlp4s0", "request": "r1"})
        await hass.async_block_till_done()
    client.network.access_points.assert_awaited_once_with("wlp4s0")
    [(request, found, error)] = fake_api.calls_named("scan_result")
    assert request == "r1" and error is None
    assert found[0] == {"ssid": "CARDV-1234", "signal": 62, "mode": "infrastructure"}


async def test_a_failed_scan_is_reported_not_swallowed(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    with patch(f"{W}._client", side_effect=WifiUnavailable("no Supervisor client")):
        fake_api.push({"type": "scan_wifi", "interface": "wlp4s0", "request": "r2"})
        await hass.async_block_till_done()
    [(request, found, error)] = fake_api.calls_named("scan_result")
    assert request == "r2" and found == [] and "no Supervisor client" in error


async def test_scan_requests_with_bad_values_are_ignored(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    fake_api.push({"type": "scan_wifi", "interface": 7, "request": "r3"})
    fake_api.push({"type": "scan_wifi", "interface": "wlp4s0"})
    await hass.async_block_till_done()
    assert fake_api.calls_named("scan_result") == []
