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


async def settle(hass: HomeAssistant, rounds: int = 50) -> None:
    """Let entry background tasks (joins, scans) run; the WebSocket loop never ends, so
    ``wait_background_tasks`` cannot be used."""
    import asyncio

    for _ in range(rounds):
        await hass.async_block_till_done()
        await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def link_comes_up():  # type: ignore[no-untyped-def]
    """The link check itself is tested in test_wifi; here the camera's Wi-Fi just connects."""
    with patch(
        "custom_components.dashcam_sync.wifi.async_verify_join",
        AsyncMock(return_value=("joined", None)),
    ) as verify:
        yield verify


async def join(hass: HomeAssistant, fake_api: FakeApi) -> tuple:
    """The final answer; a profile that was applied is first reported as connecting."""
    fake_api.push({"type": "join_wifi", "camera": "family_car"})
    await settle(hass)
    results = [r for r in fake_api.calls_named("join_result") if r[1] != "connecting"]
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
        assert await join(hass, fake_api) == ("family_car", "joined", None)
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
    assert camera == "family_car" and got_state == state
    assert reason_part in (reason or "")
    if state == "failed":
        apply_mock.assert_not_awaited()  # never connect and hope


async def test_a_join_request_for_a_bad_camera_value_is_ignored(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    fake_api.push({"type": "join_wifi", "camera": None})
    await settle(hass)
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
        await settle(hass)
    client.network.access_points.assert_awaited_once_with("wlp4s0")
    [(request, found, error)] = fake_api.calls_named("scan_result")
    assert request == "r1" and error is None
    assert found[0] == {"ssid": "CARDV-1234", "signal": 62, "mode": "infrastructure"}


async def test_a_failed_scan_is_reported_not_swallowed(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    with patch(f"{W}._client", side_effect=WifiUnavailable("no Supervisor client")):
        fake_api.push({"type": "scan_wifi", "interface": "wlp4s0", "request": "r2"})
        await settle(hass)
    [(request, found, error)] = fake_api.calls_named("scan_result")
    assert request == "r2" and found == [] and "no Supervisor client" in error


async def test_scan_requests_with_bad_values_are_ignored(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    fake_api.push({"type": "scan_wifi", "interface": 7, "request": "r3"})
    fake_api.push({"type": "scan_wifi", "interface": "wlp4s0"})
    await settle(hass)
    assert fake_api.calls_named("scan_result") == []


async def test_the_join_applies_the_address_the_check_just_picked(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    """The check runs first: when it switches to point-to-point, the join uses that."""
    real_overlap = fake_api.async_overlap

    async def picks_point_to_point(camera: str, known: list[str], probe: bool = True) -> dict:
        fake_api.wifi = {**fake_api.wifi, "static_address": "192.168.1.200/32",
                         "gateway": "192.168.1.254", "mode": "point_to_point"}  # fmt: skip
        return await real_overlap(camera, known, probe)

    fake_api.async_overlap = picks_point_to_point  # type: ignore[method-assign]
    apply = AsyncMock()
    with (
        patch(f"{W}.async_list_wifi_interfaces", return_value=ADAPTER),
        patch(f"{W}.async_apply_profile", apply),
    ):
        assert await join(hass, fake_api) == ("family_car", "joined", None)
    profile = apply.await_args.args[1]
    assert profile.address == "192.168.1.200" and profile.prefix == 32
    assert profile.gateway == "192.168.1.254"


async def test_joins_for_one_camera_never_overlap(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    import asyncio

    running = 0
    peak = 0

    async def slow_apply(*_args: object) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1

    with (
        patch(f"{W}.async_list_wifi_interfaces", return_value=ADAPTER),
        patch(f"{W}.async_apply_profile", side_effect=slow_apply),
    ):
        fake_api.push({"type": "join_wifi", "camera": "family_car"})
        fake_api.push({"type": "join_wifi", "camera": "family_car"})
        await settle(hass)
    assert peak == 1
    states = [r[1] for r in fake_api.calls_named("join_result")]
    assert states == ["connecting", "joined", "connecting", "joined"]  # one after the other


async def test_unloading_cancels_a_running_join(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    import asyncio

    started = asyncio.Event()

    async def hang(*_args: object) -> None:
        started.set()
        await asyncio.sleep(3600)

    with (
        patch(f"{W}.async_list_wifi_interfaces", return_value=ADAPTER),
        patch(f"{W}.async_apply_profile", side_effect=hang),
    ):
        fake_api.push({"type": "join_wifi", "camera": "family_car"})
        await asyncio.wait_for(started.wait(), 5)
        assert await hass.config_entries.async_unload(loaded.entry_id)
        await settle(hass)
    assert fake_api.calls_named("join_result") == []


@pytest.mark.parametrize(
    ("visible", "reason"), [(["CAMWIFI"], "wrong_password"), (["HOME"], "not_in_range")]
)
async def test_a_link_the_supervisor_could_not_activate_says_why(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi, visible: list, reason: str
) -> None:
    """Not the "join it by hand" steps: the profile was fine, the camera refused it."""
    from custom_components.dashcam_sync.wifi import WifiLinkFailed

    aps = [{"ssid": s} for s in visible]
    with (
        patch(f"{W}.async_list_wifi_interfaces", return_value=ADAPTER),
        patch(
            f"{W}.async_apply_profile",
            AsyncMock(side_effect=WifiLinkFailed("Activating connection failed")),
        ),
        patch(f"{W}.async_scan_access_points", AsyncMock(return_value=aps)),
        patch(f"{W}.JOIN_RETRY_DELAY", 0),  # a refused link is tried again first
    ):
        assert await join(hass, fake_api) == ("family_car", "failed", reason)


async def test_a_refused_link_is_tried_again(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi, link_comes_up: AsyncMock
) -> None:
    """A weak link drops handshake frames, and the camera refuses as if the password
    were wrong; a later try often gets through (seen on a real SGK camera)."""
    link_comes_up.side_effect = [
        ("failed", "wrong_password"),
        ("failed", "wrong_password"),
        ("joined", None),
    ]
    apply = AsyncMock()
    with (
        patch(f"{W}.async_list_wifi_interfaces", return_value=ADAPTER),
        patch(f"{W}.async_apply_profile", apply),
        patch(f"{W}.JOIN_RETRY_DELAY", 0),
    ):
        assert await join(hass, fake_api) == ("family_car", "joined", None)
    assert apply.await_count == 3
    states = [r[1] for r in fake_api.calls_named("join_result")]
    assert states == [
        "connecting",
        "connecting",
        "connecting",
        "joined",
    ]  # each try resets the wait


@pytest.mark.parametrize(
    ("answers", "tries", "final"),
    [
        ([("failed", "not_in_range")], 1, ("failed", "not_in_range")),  # nothing to retry
        ([("failed", "wrong_password")] * 3, 3, ("failed", "wrong_password")),  # gives up
    ],
)
async def test_retries_only_a_camera_that_is_there_and_stop(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_api: FakeApi,
    link_comes_up: AsyncMock,
    answers: list,
    tries: int,
    final: tuple,
) -> None:
    link_comes_up.side_effect = answers
    apply = AsyncMock()
    with (
        patch(f"{W}.async_list_wifi_interfaces", return_value=ADAPTER),
        patch(f"{W}.async_apply_profile", apply),
        patch(f"{W}.JOIN_RETRY_DELAY", 0),
    ):
        assert await join(hass, fake_api) == ("family_car", *final)
    assert apply.await_count == tries
