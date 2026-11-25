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


_FORBIDDEN = set("@/?#\\ \t\r\n")


def _split(host: str) -> str:
    """The host part of ``host[:port]``; anything that could smuggle a different host is refused."""
    host = host.strip()
    if any(ch in _FORBIDDEN for ch in host):
        raise ConfigError("a camera address is a host name or IP address, optionally with :port")
    if host.startswith("["):
        if "]" not in host:
            raise ConfigError("malformed IPv6 address")
        name, rest = host[1 : host.index("]")], host[host.index("]") + 1 :]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            raise ConfigError("malformed port")
        return name
    if host.count(":") == 1:
        name, _, port = host.partition(":")
        if not port.isdigit() or not 0 < int(port) < 65536:
            raise ConfigError("the port must be a number")
        return name
    return host


def _check_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address, allow_loopback: bool
) -> None:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped  # ::ffff:172.30.32.2 is still 172.30.32.2
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
