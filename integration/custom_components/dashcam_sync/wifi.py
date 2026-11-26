"""The one module that touches Core's Supervisor client.

Core's Supervisor client is not a documented API for custom integrations, so
every use of it lives here, wrapped to fail soft: when anything is missing or
changes, callers fall back to guided manual setup (the exact values to type
under Settings, System, Network) instead of breaking.
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.hassio import is_hassio

_LOGGER = logging.getLogger(__name__)


class WifiUnavailable(HomeAssistantError):
    """Wi-Fi cannot be managed from here: use the manual steps."""


@dataclass(frozen=True, slots=True)
class WifiInterface:
    name: str
    connected: bool
    enabled: bool
    ssid: str | None


@dataclass(frozen=True, slots=True)
class WifiProfile:
    """The values the options flow shows for manual setup."""

    interface: str
    ssid: str
    password: str
    address: str
    prefix: int
    block: str
    # Point-to-point only: the camera, so NetworkManager adds a direct route to it.
    gateway: str | None = None

    def as_placeholders(self) -> dict[str, str]:
        return {
            "interface": self.interface,
            "ssid": self.ssid,
            "address": self.address,
            "prefix": str(self.prefix),
            "block": self.block,
            "gateway": self.gateway or "empty",
        }


def profile_from_api(wifi: dict[str, Any]) -> WifiProfile:
    """Build the profile from the app's ``/cameras/<id>/wifi`` answer."""
    iface = ipaddress.ip_interface(wifi["static_address"])
    return WifiProfile(
        interface=str(wifi["interface"]),
        ssid=str(wifi["ssid"]),
        password=str(wifi.get("password") or ""),
        address=str(iface.ip),
        prefix=iface.network.prefixlen,
        block=str(iface.network),
        gateway=str(ipaddress.IPv4Address(wifi["gateway"])) if wifi.get("gateway") else None,
    )


def _client(hass: HomeAssistant) -> Any:
    if not is_hassio(hass):
        raise WifiUnavailable("Home Assistant is not running with a Supervisor")
    try:
        from homeassistant.components.hassio.handler import get_supervisor_client
    except ImportError as err:  # the helper moved in a Core update
        raise WifiUnavailable("Supervisor client not available") from err
    return get_supervisor_client(hass)


async def async_list_wifi_interfaces(hass: HomeAssistant) -> list[WifiInterface]:
    """Wireless interfaces the host has (empty means no usable adapter)."""
    client = _client(hass)
    try:
        info = await client.network.info()
    except Exception as err:
        raise WifiUnavailable(f"cannot read network info: {err}") from err
    out: list[WifiInterface] = []
    for iface in info.interfaces:
        if str(getattr(iface.type, "value", iface.type)) != "wireless":
            continue
        out.append(
            WifiInterface(
                name=iface.interface,
                connected=bool(iface.connected),
                enabled=bool(iface.enabled),
                ssid=iface.wifi.ssid if iface.wifi else None,
            )
        )
    return out


async def async_scan_access_points(hass: HomeAssistant, interface: str) -> list[dict[str, Any]]:
    """Networks the interface can see, as Search networks under Settings > System > Network does."""
    client = _client(hass)
    try:
        found = await client.network.access_points(interface)
    except Exception as err:
        raise WifiUnavailable(f"scan failed: {err}") from err
    out: list[dict[str, Any]] = []
    for ap in found:
        mode = getattr(ap, "mode", None)
        out.append(
            {
                "ssid": getattr(ap, "ssid", None),
                "signal": getattr(ap, "signal", None),
                "mode": str(getattr(mode, "value", mode) or ""),
            }
        )
    return out


async def async_apply_profile(hass: HomeAssistant, profile: WifiProfile) -> None:
    """Join the camera hotspot with the address the app picked.

    Infrastructure mode, the camera's SSID with a WPA passphrase, a static IPv4
    address with no DNS servers, and IPv6 disabled. A small block has no
    gateway. Point-to-point (a /32) names the camera as gateway: that is how
    NetworkManager gets a direct route to it; the route it adds ranks below the
    wired one, and the app disconnects the camera if the primary route ever moves.
    """
    client = _client(hass)
    try:
        from aiohasupervisor.models.network import (
            AuthMethod,
            InterfaceMethod,
            IPv4Config,
            IPv6Config,
            NetworkInterfaceConfig,
            WifiConfig,
            WifiMode,
        )
    except ImportError as err:
        raise WifiUnavailable("Supervisor client models changed") from err
    config = NetworkInterfaceConfig(
        enabled=True,
        wifi=WifiConfig(
            mode=WifiMode.INFRASTRUCTURE,
            method=AuthMethod.WPA_PSK if profile.password else AuthMethod.OPEN,
            ssid=profile.ssid,
            psk=profile.password or None,
        ),
        ipv4=IPv4Config(
            method=InterfaceMethod.STATIC,
            address=[ipaddress.IPv4Interface(f"{profile.address}/{profile.prefix}")],
            gateway=ipaddress.IPv4Address(profile.gateway) if profile.gateway else None,
            nameservers=[],
        ),
        ipv6=IPv6Config(method=InterfaceMethod.DISABLED),
    )
    try:
        await client.network.update_interface(profile.interface, config)
    except Exception as err:
        raise WifiUnavailable(f"the Supervisor refused the Wi-Fi profile: {err}") from err


async def async_disconnect(hass: HomeAssistant, interface: str) -> None:
    """Kill switch: take the interface down so nothing is routed to the car."""
    client = _client(hass)
    try:
        from aiohasupervisor.models.network import NetworkInterfaceConfig
    except ImportError as err:
        raise WifiUnavailable("Supervisor client models changed") from err
    try:
        await client.network.update_interface(interface, NetworkInterfaceConfig(enabled=False))
    except Exception as err:
        raise WifiUnavailable(f"could not disconnect {interface}: {err}") from err


async def async_announced_wifi_adapters(hass: HomeAssistant) -> list[str]:
    """Wireless adapters Home Assistant announces itself on (it should not)."""
    try:
        from homeassistant.components import network
    except ImportError:  # pragma: no cover
        return []
    try:
        adapters = await network.async_get_adapters(hass)
    except Exception:  # the network integration is optional here
        _LOGGER.debug("Cannot read network adapters", exc_info=True)
        return []
    return [a["name"] for a in adapters if a["enabled"] and a["name"].startswith(("wl", "wlan"))]


_IP_ATTRS = ("ip", "ip_address", "host", "local_ip")


def known_device_ips(hass: HomeAssistant) -> list[str]:
    """IPv4 addresses Home Assistant knows for other devices.

    Used by the overlap check to name real devices that sit inside the
    camera's reserved block. Looks at entity attributes and config entries.
    """
    found: set[str] = set()

    def consider(value: Any) -> None:
        if not isinstance(value, str):
            return
        try:
            addr = ipaddress.ip_address(value.strip())
        except ValueError:
            return
        if addr.version == 4:
            found.add(str(addr))

    for state in hass.states.async_all():
        for attr in _IP_ATTRS:
            consider(state.attributes.get(attr))
    for entry in hass.config_entries.async_entries():
        if entry.domain == "dashcam_sync":
            continue
        consider(entry.data.get("host"))
    return sorted(found)


async def async_join_camera(
    hass: HomeAssistant, api: Any, camera_id: str
) -> tuple[str, str | None]:
    """Join a hotspot camera's Wi-Fi when the panel asks; returns ``(state, reason)``.

    The same checks as the options flow: the address block must be free, the adapter must
    exist, and anything the Supervisor client cannot do falls back to manual setup.
    """
    from .api import DashcamSyncError  # local import keeps this module free of the client

    try:
        # The check first: it may pick a new address, which the profile then carries.
        report = await api.async_overlap(camera_id, known_device_ips(hass))
        profile = profile_from_api(await api.async_wifi(camera_id))
    except (DashcamSyncError, KeyError, ValueError) as err:
        return "failed", f"app_unavailable: {err}"
    if report.get("blocked"):
        return "failed", str(report.get("result") or "address_in_use")
    try:
        adapters = await async_list_wifi_interfaces(hass)
    except WifiUnavailable as err:
        return "manual", str(err)
    if profile.interface not in {a.name for a in adapters}:
        return "failed", f"no_adapter: {profile.interface}"
    try:
        await async_apply_profile(hass, profile)
    except WifiUnavailable as err:
        return "manual", str(err)
    return "joined", None
