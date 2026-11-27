"""The Supervisor wrapper: every failure mode falls back instead of raising raw errors."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant

from custom_components.dashcam_sync import wifi
from custom_components.dashcam_sync.wifi import WifiUnavailable


def iface(name: str, kind: str, connected: bool = True, ssid: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        interface=name,
        type=SimpleNamespace(value=kind),
        connected=connected,
        enabled=True,
        wifi=SimpleNamespace(ssid=ssid) if ssid else None,
    )


@pytest.fixture
def supervisor() -> MagicMock:
    client = MagicMock()
    client.network.info = AsyncMock(
        return_value=SimpleNamespace(
            interfaces=[
                iface("eth0", "ethernet"),
                iface("wlan0", "wireless", True, "HOME"),
                iface("wlan1", "wireless", False),
            ]
        )
    )
    client.network.update_interface = AsyncMock()
    # What update_interface does inside: POST the model's dict. The profile goes
    # through here, checked against the Supervisor's own schema (below).
    client.network._client.post = AsyncMock()
    return client


# The Supervisor's schema for POST /network/interface/<name>/update
# (supervisor/api/network.py, SCHEMA_UPDATE): it rejects unknown keys.
SUPERVISOR_UPDATE = vol.Schema(
    {
        vol.Optional("ipv4"): {
            vol.Optional("address"): [str],
            vol.Optional("method"): vol.In(["disabled", "static", "auto"]),
            vol.Optional("gateway"): str,
            vol.Optional("nameservers"): [str],
        },
        vol.Optional("ipv6"): dict,
        vol.Optional("wifi"): {
            vol.Optional("mode"): vol.In(["infrastructure", "mesh", "adhoc", "ap"]),
            vol.Optional("auth"): vol.In(["open", "wep", "wpa-psk"]),
            vol.Optional("ssid"): str,
            vol.Optional("psk"): str,
        },
        vol.Optional("enabled"): bool,
    }
)


def posted(client: MagicMock) -> tuple[str, dict]:
    """The request the Supervisor received, validated the way it validates it."""
    call = client.network._client.post.call_args
    return call.args[0], SUPERVISOR_UPDATE(call.kwargs["json"])


@pytest.fixture
def hassio(supervisor: MagicMock):  # type: ignore[no-untyped-def]
    with (
        patch("custom_components.dashcam_sync.wifi.is_hassio", return_value=True),
        patch(
            "homeassistant.components.hassio.handler.get_supervisor_client", return_value=supervisor
        ),
    ):
        yield supervisor


PROFILE = wifi.profile_from_api(
    {
        "interface": "wlan0",
        "ssid": "CAM",
        "password": "pw123456",
        "static_address": "192.168.1.253/30",
    }
)


def test_profile_from_api() -> None:
    assert (
        PROFILE.address == "192.168.1.253"
        and PROFILE.prefix == 30
        and PROFILE.block == "192.168.1.252/30"
    )
    assert PROFILE.as_placeholders() == {
        "interface": "wlan0", "ssid": "CAM", "address": "192.168.1.253", "prefix": "30", "block": "192.168.1.252/30", "gateway": "empty",
    }  # fmt: skip
    assert (
        wifi.profile_from_api(
            {"interface": "w", "ssid": "s", "static_address": "10.0.0.2/30"}
        ).password
        == ""
    )


async def test_not_hassio_means_manual(hass: HomeAssistant) -> None:
    with pytest.raises(WifiUnavailable):
        await wifi.async_list_wifi_interfaces(hass)
    with pytest.raises(WifiUnavailable):
        await wifi.async_apply_profile(hass, PROFILE)
    with pytest.raises(WifiUnavailable):
        await wifi.async_disconnect(hass, "wlan0")


async def test_missing_client_helper_means_manual(hass: HomeAssistant) -> None:
    with (
        patch("custom_components.dashcam_sync.wifi.is_hassio", return_value=True),
        patch.dict("sys.modules", {"homeassistant.components.hassio.handler": None}),
        pytest.raises(WifiUnavailable),
    ):
        await wifi.async_list_wifi_interfaces(hass)


async def test_lists_only_wireless_interfaces(hass: HomeAssistant, hassio: MagicMock) -> None:
    result = await wifi.async_list_wifi_interfaces(hass)
    assert [(i.name, i.connected, i.ssid) for i in result] == [
        ("wlan0", True, "HOME"),
        ("wlan1", False, None),
    ]
    hassio.network.info.side_effect = RuntimeError("boom")
    with pytest.raises(WifiUnavailable):
        await wifi.async_list_wifi_interfaces(hass)


async def test_apply_profile_point_to_point_names_the_camera_as_gateway(
    hass: HomeAssistant, hassio: MagicMock
) -> None:
    profile = wifi.profile_from_api(
        {"interface": "wlan0", "ssid": "CARDV-5A3F", "password": "pw123456",
         "static_address": "192.168.0.254/32", "gateway": "192.168.0.1"}
    )  # fmt: skip
    assert profile.as_placeholders()["gateway"] == "192.168.0.1"
    await wifi.async_apply_profile(hass, profile)
    _, body = posted(hassio)
    assert body["ipv4"]["address"] == ["192.168.0.254/32"]
    assert body["ipv4"]["gateway"] == "192.168.0.1"
    assert body["ipv4"]["nameservers"] == []  # name resolution stays on Ethernet
    assert PROFILE.as_placeholders()["gateway"] == "empty"


async def test_apply_profile_writes_the_small_block_without_gateway_or_dns(
    hass: HomeAssistant, hassio: MagicMock
) -> None:
    await wifi.async_apply_profile(hass, PROFILE)
    path, body = posted(hassio)  # a key the Supervisor does not know fails here
    assert path == "network/interface/wlan0/update" and body["enabled"] is True
    assert body["wifi"] == {
        "mode": "infrastructure", "auth": "wpa-psk", "ssid": "CAM", "psk": "pw123456"
    }  # fmt: skip
    assert body["ipv4"]["method"] == "static"
    assert body["ipv4"]["address"] == ["192.168.1.253/30"]
    assert "gateway" not in body["ipv4"] or body["ipv4"]["gateway"] is None
    assert body["ipv4"]["nameservers"] == []  # default route stays on Ethernet
    assert body["ipv6"]["method"] == "disabled"

    open_profile = wifi.profile_from_api(
        {"interface": "wlan0", "ssid": "OPEN", "static_address": "10.0.0.2/30"}
    )
    await wifi.async_apply_profile(hass, open_profile)
    assert posted(hassio)[1]["wifi"]["auth"] == "open"

    hassio.network._client.post.side_effect = RuntimeError("denied")
    with pytest.raises(WifiUnavailable):
        await wifi.async_apply_profile(hass, PROFILE)


async def test_the_library_model_alone_would_be_refused(hass: HomeAssistant) -> None:
    """Why the profile is not sent with update_interface: its dict says ``method``."""
    from aiohasupervisor.models.network import AuthMethod, NetworkInterfaceConfig, WifiConfig

    raw = NetworkInterfaceConfig(wifi=WifiConfig(method=AuthMethod.WPA_PSK, ssid="X")).to_dict()
    with pytest.raises(vol.Invalid):
        SUPERVISOR_UPDATE(raw)


async def test_without_client_internals_the_library_call_is_used(
    hass: HomeAssistant, hassio: MagicMock
) -> None:
    hassio.network._client = None
    await wifi.async_apply_profile(hass, PROFILE)
    assert hassio.network.update_interface.await_args.args[0] == "wlan0"


async def test_disconnect(hass: HomeAssistant, hassio: MagicMock) -> None:
    await wifi.async_disconnect(hass, "wlan0")
    interface, config = hassio.network.update_interface.call_args.args
    assert interface == "wlan0" and config.enabled is False and config.ipv4 is None
    hassio.network.update_interface.side_effect = RuntimeError("denied")
    with pytest.raises(WifiUnavailable):
        await wifi.async_disconnect(hass, "wlan0")


async def test_models_import_failure_means_manual(hass: HomeAssistant, hassio: MagicMock) -> None:
    with patch.dict("sys.modules", {"aiohasupervisor.models.network": None}):
        with pytest.raises(WifiUnavailable):
            await wifi.async_apply_profile(hass, PROFILE)
        with pytest.raises(WifiUnavailable):
            await wifi.async_disconnect(hass, "wlan0")


async def test_announced_adapters(hass: HomeAssistant) -> None:
    adapters = [
        {"name": "eth0", "enabled": True},
        {"name": "wlan0", "enabled": True},
        {"name": "wlp3s0", "enabled": False},
    ]
    with patch("homeassistant.components.network.async_get_adapters", return_value=adapters):
        assert await wifi.async_announced_wifi_adapters(hass) == ["wlan0"]
    with patch(
        "homeassistant.components.network.async_get_adapters", side_effect=RuntimeError("x")
    ):
        assert await wifi.async_announced_wifi_adapters(hass) == []


async def test_known_device_ips(hass: HomeAssistant) -> None:
    hass.states.async_set("device_tracker.a", "home", {"ip": "192.168.1.55"})
    hass.states.async_set("sensor.b", "1", {"ip_address": "10.0.0.7", "host": "not-an-ip"})
    hass.states.async_set("sensor.v6", "1", {"ip": "fe80::1", "local_ip": 5})
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    MockConfigEntry(domain="other", data={"host": "192.168.1.200"}).add_to_hass(hass)
    MockConfigEntry(domain="dashcam_sync", data={"host": "192.168.1.9"}).add_to_hass(hass)
    assert wifi.known_device_ips(hass) == ["10.0.0.7", "192.168.1.200", "192.168.1.55"]
