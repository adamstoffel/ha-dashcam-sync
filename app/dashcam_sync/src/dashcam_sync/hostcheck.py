"""Where the wizard may point the app.

The panel's probe, capture and station-mode calls take a host from the
request. A camera lives on a private or link-local network, so anything else is
refused: this stops the endpoints being used to make the app fetch from the
internet, from the Supervisor, or from Home Assistant Core's own network.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

from .errors import ConfigError

# The Home Assistant Docker network: the Supervisor (172.30.32.2), Core (172.30.32.1) and apps.
INTERNAL_NETWORK = ipaddress.ip_network("172.30.32.0/23")
BLOCKED_NAMES = frozenset({"supervisor", "homeassistant", "hassio", "core-ssh", "localhost"})


def _split(host: str) -> str:
    host = host.strip()
    if host.startswith("[") and "]" in host:
        return host[1 : host.index("]")]
    if host.count(":") == 1:
        return host.rsplit(":", 1)[0]
    return host


def _check_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address, allow_loopback: bool
) -> None:
    if address.is_loopback:
        if allow_loopback:
            return
        raise ConfigError("loopback addresses are not cameras")
    if isinstance(address, ipaddress.IPv4Address) and address in INTERNAL_NETWORK:
        raise ConfigError("that address belongs to Home Assistant itself")
    if address.is_unspecified or address.is_multicast or address.is_reserved:
        raise ConfigError("not a usable camera address")
    if not (address.is_private or address.is_link_local):
        raise ConfigError("cameras are on a private network; public addresses are refused")


async def validate_camera_host(host: str, *, allow_loopback: bool = False) -> str:
    """Return ``host`` if it may be probed, else raise :class:`ConfigError`."""
    name = _split(host)
    if not name:
        raise ConfigError("a camera address is required")
    try:
        _check_address(ipaddress.ip_address(name), allow_loopback)
        return host
    except ValueError:
        pass  # not an IP literal: a hostname
    if name.lower().rstrip(".") in BLOCKED_NAMES:
        raise ConfigError(f"{name} is not a camera")
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(name, None, type=socket.SOCK_STREAM)
    except OSError as err:
        raise ConfigError(f"cannot resolve {name}") from err
    for info in infos:
        _check_address(ipaddress.ip_address(info[4][0]), allow_loopback)
    return host
