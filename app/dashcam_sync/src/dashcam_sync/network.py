"""Read-only view of host networking from ``/network/info``.

The app never edits host networking: Core does that through the Supervisor.
The app only reads link state and checks that the default route is safe.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from .detect import candidate_hosts, split_host
from .drivers.http import tcp_open
from .netblock import IPv4Net, parse_networks, tcp_probe
from .options import CameraOptions
from .supervisor import SupervisorClient, SupervisorError


@dataclass(slots=True)
class Interface:
    name: str
    type: str
    enabled: bool
    connected: bool
    primary: bool
    addresses: list[str] = field(default_factory=list)
    gateway: str | None = None
    ready: bool = False
    ssid: str | None = None
    signal: int | None = None

    @property
    def wireless(self) -> bool:
        return self.type == "wireless"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "enabled": self.enabled,
            "connected": self.connected,
            "primary": self.primary,
            "addresses": self.addresses,
            "gateway": self.gateway,
            "ssid": self.ssid,
            "signal": self.signal,
        }


def parse_interfaces(info: dict[str, Any]) -> list[Interface]:
    out: list[Interface] = []
    for raw in info.get("interfaces") or []:
        if not isinstance(raw, dict):
            continue
        ipv4 = raw.get("ipv4") or {}
        wifi = raw.get("wifi") or {}
        out.append(
            Interface(
                name=str(raw.get("interface", "")),
                type=str(raw.get("type", "")),
                enabled=bool(raw.get("enabled", False)),
                connected=bool(raw.get("connected", False)),
                primary=bool(raw.get("primary", False)),
                addresses=[str(a) for a in ipv4.get("address") or []],
                gateway=ipv4.get("gateway"),
                ready=bool(ipv4.get("ready", False)),
                ssid=wifi.get("ssid"),
                signal=wifi.get("signal"),
            )
        )
    return out


class NetworkMonitor:
    """Polls ``/network/info`` (every 15 s) and answers link questions."""

    def __init__(self, client: SupervisorClient | None) -> None:
        self._client = client
        self.interfaces: list[Interface] = []
        self.updated: float = 0.0
        self.error: str | None = None

    @property
    def known(self) -> bool:
        """True once real data has arrived (False in dev mode)."""
        return self.updated > 0

    async def refresh(self) -> list[Interface]:
        if self._client is None or not self._client.available:
            return self.interfaces
        try:
            info = await self._client.network_info()
        except SupervisorError as err:
            self.error = str(err)
            return self.interfaces
        self.error = None
        self.interfaces = parse_interfaces(info)
        self.updated = time.time()
        return self.interfaces

    def set_interfaces(self, interfaces: list[Interface]) -> None:
        """Used by tests and the dev mode to inject state."""
        self.interfaces = interfaces
        self.updated = time.time()

    def interface(self, name: str | None) -> Interface | None:
        return next((i for i in self.interfaces if i.name == name), None)

    @property
    def wireless_interfaces(self) -> list[Interface]:
        return [i for i in self.interfaces if i.wireless]

    @property
    def has_wifi_adapter(self) -> bool:
        return bool(self.wireless_interfaces)

    def home_networks(self, exclude: set[str] | None = None) -> list[IPv4Net]:
        exclude = exclude or set()
        nets: list[IPv4Net] = []
        for iface in self.interfaces:
            if iface.name in exclude or not iface.connected:
                continue
            nets.extend(parse_networks(iface.addresses))
        return nets

    def link_matches(self, camera: CameraOptions) -> bool | None:
        """Is the camera's Wi-Fi joined? ``None`` when nothing is known."""
        if camera.connection != "hotspot" or not self.known:
            return None
        iface = self.interface(camera.interface)
        if iface is None:
            return False
        return bool(iface.connected and iface.ready and (iface.ssid in (None, camera.ssid)))

    def default_route_ok(self, hotspot_interfaces: set[str]) -> bool | None:
        """The primary route must stay on a non-hotspot interface."""
        if not self.known:
            return None
        primary = next((i for i in self.interfaces if i.primary), None)
        if primary is None:
            return False
        return primary.name not in hotspot_interfaces and primary.connected

    def primary_gateway(self) -> str | None:
        primary = next((i for i in self.interfaces if i.primary), None)
        return primary.gateway if primary else None

    async def router_answers(self) -> bool:
        gateway = self.primary_gateway()
        if not gateway:
            return False
        if await tcp_probe(gateway, ports=(53, 80, 443), timeout=1.0):
            return True
        return False


async def camera_reachable(camera: CameraOptions, gateway: str | None = None) -> bool:
    """Cheap TCP reachability of the camera's host(s) on any known port."""
    hosts = candidate_hosts(camera.host, gateway)
    if camera.driver != "auto":
        hosts = candidate_hosts(camera.host, gateway, [camera.driver])
    checks = []
    for host in hosts:
        name, port = split_host(host)
        for p in (port,) if port else (80, 443, 554, 7878, 8192):
            checks.append(tcp_open(name, p, 1.0))
    if not checks:
        return False
    return any(await asyncio.gather(*checks))
