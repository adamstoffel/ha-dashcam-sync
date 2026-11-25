"""Camera detection: candidate hosts, port fingerprint, driver probes."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .drivers import DRIVER_BY_NAME, DRIVERS, BaseDriver
from .drivers.http import CameraHttp, tcp_open
from .errors import UnsupportedCamera
from .models import Probe

_LOGGER = logging.getLogger(__name__)

FINGERPRINT_PORTS = (80, 443, 554, 3333, 5000, 7878, 8192)
PORT_TIMEOUT = 1.5


@dataclass(slots=True)
class Detection:
    probe: Probe
    driver: BaseDriver
    ports: dict[int, bool] = field(default_factory=dict)


def candidate_hosts(
    configured: str | None,
    gateway: str | None = None,
    driver_names: list[str] | None = None,
) -> list[str]:
    """The camera entry's host; else the DHCP gateway; else every default host."""
    if configured:
        return [configured]
    hosts: list[str] = []
    if gateway:
        hosts.append(gateway)
    for cls in DRIVERS:
        if driver_names and cls.name not in driver_names:
            continue
        for host in cls.default_hosts:
            if host not in hosts:
                hosts.append(host)
    return hosts


def split_host(host: str) -> tuple[str, int | None]:
    if host.count(":") == 1:
        name, _, port = host.partition(":")
        if port.isdigit():
            return name, int(port)
    return host, None


async def fingerprint(host: str) -> dict[int, bool]:
    """TCP fingerprint, all ports in parallel. An explicit port is tested alone."""
    name, port = split_host(host)
    ports = (port,) if port else FINGERPRINT_PORTS
    results = await asyncio.gather(*(tcp_open(name, p, PORT_TIMEOUT) for p in ports))
    return dict(zip(ports, results, strict=True))


async def detect(
    session: aiohttp.ClientSession,
    hosts: list[str],
    *,
    forced: str = "auto",
    cached: dict[str, Any] | None = None,
) -> Detection:
    """Find the driver that claims the camera.

    ``cached`` is ``{"driver": name, "host": host}`` from the previous visit:
    only that driver is re-probed first, with full detection as the fallback.
    """
    if cached:
        cls = DRIVER_BY_NAME.get(str(cached.get("driver")))
        host = str(cached.get("host", ""))
        if cls is not None and host and (forced in ("auto", cls.name)):
            driver = cls(CameraHttp(session, host))
            probe = await driver.probe(host)
            if probe is not None:
                return Detection(probe, driver, {})
            _LOGGER.info("Cached driver %s no longer claims %s", cls.name, host)

    classes = [c for c in DRIVERS if forced in ("auto", c.name)]
    for host in hosts:
        ports = await fingerprint(host)
        if not any(ports.values()):
            continue
        for cls in classes:
            driver = cls(CameraHttp(session, host))
            probe = await driver.probe(host)
            if probe is not None:
                probe.fingerprint["ports"] = sorted(p for p, ok in ports.items() if ok)
                return Detection(probe, driver, ports)
    raise UnsupportedCamera(f"no driver claimed any of {hosts}")
