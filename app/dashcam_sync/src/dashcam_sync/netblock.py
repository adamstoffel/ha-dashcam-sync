"""Small-block addressing for hotspot cameras and the overlap checks.

Many cameras live on 192.168.1.x or 192.168.0.x, ranges that home networks
also use. Linux always routes to the most specific prefix, so Home Assistant
claims only the smallest block that holds both itself and the camera, and
only that block is routed to the car.
"""

from __future__ import annotations

import asyncio
import ipaddress
import shutil
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal

from .errors import ConfigError

IPv4Net = ipaddress.IPv4Network
IPv4Addr = ipaddress.IPv4Address


@dataclass(frozen=True, slots=True)
class CameraBlock:
    """The reserved block and the addresses inside it."""

    network: IPv4Net
    camera: IPv4Addr
    host: IPv4Addr

    @property
    def static_address(self) -> str:
        """Home Assistant's interface address with prefix, e.g. 192.168.1.253/30."""
        return f"{self.host}/{self.network.prefixlen}"

    @property
    def first(self) -> IPv4Addr:
        return self.network.network_address

    @property
    def last(self) -> IPv4Addr:
        return self.network.broadcast_address

    def addresses(self) -> list[IPv4Addr]:
        return list(self.network)

    def describe(self) -> str:
        return f"{self.first} to {self.last}"


def _usable(network: IPv4Net, addr: IPv4Addr) -> bool:
    """Usable host address: inside the block, not first or last."""
    return addr in network and addr != network.network_address and addr != network.broadcast_address


def compute_block(camera_ip: str) -> CameraBlock:
    """Smallest block (from /30 upward) where the camera is a usable host."""
    try:
        camera = ipaddress.IPv4Address(camera_ip)
    except ValueError as err:
        raise ConfigError(f"not an IPv4 address: {camera_ip}") from err
    for prefix in range(30, 7, -1):
        network = ipaddress.IPv4Network((int(camera) & (~0 << (32 - prefix)), prefix))
        if not _usable(network, camera):
            continue
        host = _pick_host(network, camera)
        if host is not None:
            return CameraBlock(network=network, camera=camera, host=host)
    raise ConfigError(f"no usable block around {camera_ip}")


def _pick_host(network: IPv4Net, camera: IPv4Addr) -> IPv4Addr | None:
    """Another usable host, nearest first, preferring the lower neighbour."""
    for delta in (-1, 1, -2, 2, -3, 3):
        candidate = IPv4Addr(int(camera) + delta)
        if candidate != camera and _usable(network, candidate):
            return candidate
    for addr in network.hosts():
        if addr != camera:
            return addr
    return None


def block_from_static(static_address: str, camera_ip: str) -> CameraBlock:
    """Rebuild a block from a stored static_address plus the camera IP."""
    iface = ipaddress.ip_interface(static_address)
    if not isinstance(iface, ipaddress.IPv4Interface):
        raise ConfigError("static_address must be IPv4")
    camera = ipaddress.IPv4Address(camera_ip)
    network = iface.network
    if camera not in network:
        raise ConfigError("camera address is outside the static block")
    if iface.ip == camera:
        raise ConfigError("Home Assistant and the camera cannot share an address")
    return CameraBlock(network=network, camera=camera, host=iface.ip)


def parse_networks(addresses: Iterable[str]) -> list[IPv4Net]:
    """Turn ``['192.168.1.10/24', ...]`` from /network/info into networks."""
    nets: list[IPv4Net] = []
    for text in addresses:
        try:
            iface = ipaddress.ip_interface(text)
        except ValueError:
            continue
        if isinstance(iface, ipaddress.IPv4Interface):
            nets.append(iface.network)
    return nets


OverlapResult = Literal[
    "no_overlap",
    "overlap_block_free",
    "device_in_block",
    "address_in_use",
    "camera_collides",
]
BLOCKING: frozenset[str] = frozenset({"device_in_block", "address_in_use", "camera_collides"})


@dataclass(slots=True)
class OverlapReport:
    """Outcome of the overlap check, with the data the wizard shows."""

    result: OverlapResult
    block: CameraBlock
    home_networks: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    probed: bool = False

    @property
    def blocked(self) -> bool:
        return self.result in BLOCKING

    def as_dict(self) -> dict[str, object]:
        return {
            "result": self.result,
            "blocked": self.blocked,
            "block": str(self.block.network),
            "block_first": str(self.block.first),
            "block_last": str(self.block.last),
            "camera": str(self.block.camera),
            "home_address": str(self.block.host),
            "static_address": self.block.static_address,
            "home_networks": self.home_networks,
            "conflicts": self.conflicts,
            "probed": self.probed,
        }


ProbeFn = Callable[[str], Awaitable[bool]]


async def tcp_probe(ip: str, ports: tuple[int, ...] = (80, 443), timeout: float = 1.0) -> bool:
    """True when something answers: ping, an accepted or a refused TCP port.

    A refused connection still proves a host exists at the address.
    """
    if await _ping(ip, timeout):
        return True

    async def one(port: int) -> bool:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
        except ConnectionRefusedError:
            return True
        except (TimeoutError, OSError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return True

    results = await asyncio.gather(*(one(p) for p in ports))
    return any(results)


async def _ping(ip: str, timeout: float) -> bool:
    binary = shutil.which("ping")
    if binary is None:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "-c",
            "1",
            "-W",
            "1",
            ip,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            code = await asyncio.wait_for(proc.wait(), timeout + 1)
        except TimeoutError:
            proc.kill()
            return False
    except OSError:
        return False
    return code == 0


async def check_overlap(
    block: CameraBlock,
    home_networks: Iterable[IPv4Net],
    *,
    known_device_ips: Iterable[str] = (),
    probe: ProbeFn | None = None,
) -> OverlapReport:
    """Run the pre-flight check described in the network section.

    ``probe`` is only meaningful while the car is away: it tests the block's
    addresses (and the camera's own) over Ethernet. Pass ``None`` to skip it.
    """
    nets = list(home_networks)
    overlaps = any(block.network.overlaps(net) for net in nets)
    report = OverlapReport(
        result="overlap_block_free" if overlaps else "no_overlap",
        block=block,
        home_networks=[str(n) for n in nets],
    )

    in_block: list[str] = []
    for text in known_device_ips:
        try:
            ip = ipaddress.IPv4Address(text)
        except ValueError:
            continue
        if ip in block.network:
            in_block.append(str(ip))
    if in_block:
        report.result = "device_in_block"
        report.conflicts = sorted(set(in_block))
        return report

    if probe is not None:
        report.probed = True
        camera_answers = await probe(str(block.camera))
        if camera_answers:
            report.result = "camera_collides"
            report.conflicts = [str(block.camera)]
            return report
        answering = [
            str(addr)
            for addr in block.addresses()
            if addr != block.camera and await probe(str(addr))
        ]
        if answering:
            report.result = "address_in_use"
            report.conflicts = answering
            return report
    return report
