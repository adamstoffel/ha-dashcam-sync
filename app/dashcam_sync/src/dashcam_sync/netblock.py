"""Small-block addressing for hotspot cameras and the overlap checks.

Many cameras live on 192.168.1.x or 192.168.0.x, ranges that home networks
also use. Linux always routes to the most specific prefix, so Home Assistant
claims only the smallest block that holds both itself and the camera, and
only that block is routed to the car.

When the camera's range sits inside the home network and an address in that
block is taken, there is a second shape, **point-to-point**: Home Assistant
takes one free address next to the camera as a /32 and reaches the camera
through a host route (set as the profile's gateway, which NetworkManager
installs as a direct route). Only two addresses are claimed: Home
Assistant's and the camera's. ``choose_block`` picks the shape.
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
    point_to_point: bool = False

    @property
    def mode(self) -> Literal["block", "point_to_point"]:
        return "point_to_point" if self.point_to_point else "block"

    @property
    def static_address(self) -> str:
        """Home Assistant's interface address with prefix, e.g. 192.168.1.253/30."""
        return f"{self.host}/{self.network.prefixlen}"

    @property
    def gateway(self) -> str | None:
        """Point-to-point only: the camera, so a direct route to it is installed."""
        return str(self.camera) if self.point_to_point else None

    def claimed(self) -> list[IPv4Addr]:
        """Every address Home Assistant routes to the car (unreachable at home meanwhile)."""
        if self.point_to_point:
            return sorted({self.camera, self.host})
        return list(self.network)

    @property
    def first(self) -> IPv4Addr:
        return min(self.claimed())

    @property
    def last(self) -> IPv4Addr:
        return max(self.claimed())

    def addresses(self) -> list[IPv4Addr]:
        return self.claimed()

    def describe(self) -> str:
        if self.point_to_point:
            return f"{self.camera} and {self.host} only"
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


def point_to_point(camera_ip: str, host_ip: str) -> CameraBlock:
    """Home Assistant at ``host_ip``/32 with a direct route to the camera."""
    try:
        camera = ipaddress.IPv4Address(camera_ip)
        host = ipaddress.IPv4Address(host_ip)
    except ValueError as err:
        raise ConfigError(f"not an IPv4 address: {err}") from err
    if host == camera:
        raise ConfigError("Home Assistant and the camera cannot share an address")
    if host.is_loopback or host.is_multicast or host.is_unspecified or host.is_link_local:
        raise ConfigError(f"{host} cannot be Home Assistant's address")
    return CameraBlock(
        network=ipaddress.IPv4Network(f"{host}/32"), camera=camera, host=host, point_to_point=True
    )


def point_to_point_candidates(camera_ip: str, prefix: int = 24, limit: int = 8) -> list[str]:
    """Addresses to try for Home Assistant, from the top of the camera's own subnet.

    The camera answers only addresses inside its subnet (almost always a /24), so
    the address must be there; the top end is usually outside the camera's DHCP
    pool (which hands out from .100 or .2 upwards).
    """
    camera = ipaddress.IPv4Address(camera_ip)
    subnet = ipaddress.IPv4Network(f"{camera}/{prefix}", strict=False)
    out: list[str] = []
    for value in range(int(subnet.broadcast_address) - 1, int(subnet.network_address), -1):
        addr = ipaddress.IPv4Address(value)
        if addr != camera:
            out.append(str(addr))
        if len(out) >= limit:
            break
    return out


def block_from_static(
    static_address: str, camera_ip: str, *, force_point_to_point: bool = False
) -> CameraBlock:
    """Rebuild a block from a stored static_address plus the camera IP.

    A /32 (or ``force_point_to_point``) means point-to-point.
    """
    try:
        iface = ipaddress.ip_interface(static_address)
    except ValueError as err:
        raise ConfigError(f"not an address with a prefix: {static_address}") from err
    if not isinstance(iface, ipaddress.IPv4Interface):
        raise ConfigError("static_address must be IPv4")
    if force_point_to_point or iface.network.prefixlen == 32:
        return point_to_point(camera_ip, str(iface.ip))
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
    # How each conflicting address answered, e.g. "answers ping".
    evidence: dict[str, str] = field(default_factory=dict)

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
            "mode": self.block.mode,
            "gateway": self.block.gateway,
            "claimed": [str(a) for a in self.block.claimed()] if self.block.point_to_point else [],
            "home_networks": self.home_networks,
            "conflicts": self.conflicts,
            "probed": self.probed,
            "evidence": self.evidence,
        }


# A probe answers with how the address answered (truthy text), or a false value.
ProbeFn = Callable[[str], Awaitable[bool | str]]


async def tcp_probe(ip: str, ports: tuple[int, ...] = (80, 443), timeout: float = 1.0) -> str:
    """How something answers at ``ip``: ping, an accepted or a refused TCP port.

    Returns ``""`` when nothing does. A refused connection normally proves a host
    exists (it sent a reset), so it counts, but it is named, because a firewall
    rejecting on the host's behalf looks the same.
    """
    if await _ping(ip, timeout):
        return "answers ping"

    async def one(port: int) -> str:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
        except ConnectionRefusedError:
            return f"refused a connection on port {port} (no ping answer)"
        except (TimeoutError, OSError):
            return ""
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return f"accepts connections on port {port}"

    results = await asyncio.gather(*(one(p) for p in ports))
    accepted = [r for r in results if r.startswith("accepts")]
    return (accepted or [r for r in results if r] or [""])[0]


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
    claimed = block.claimed()
    overlaps = any(addr in net for net in nets for addr in claimed)
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
        if ip in claimed:
            in_block.append(str(ip))
    if in_block:
        report.result = "device_in_block"
        report.conflicts = sorted(set(in_block))
        return report

    if probe is not None:
        report.probed = True

        async def ask(addr: str) -> bool:
            answer = await probe(addr)
            if answer:
                report.evidence[addr] = answer if isinstance(answer, str) else "answered"
            return bool(answer)

        if await ask(str(block.camera)):
            report.result = "camera_collides"
            report.conflicts = [str(block.camera)]
            return report
        others = [str(a) for a in block.addresses() if a != block.camera]
        answers = await asyncio.gather(*(ask(a) for a in others))  # in parallel
        answering = [a for a, yes in zip(others, answers, strict=True) if yes]
        if answering:
            report.result = "address_in_use"
            report.conflicts = answering
            return report
    return report


Addressing = Literal["auto", "block", "point_to_point"]


async def choose_block(
    camera_ip: str,
    home_networks: Iterable[IPv4Net],
    *,
    addressing: Addressing = "auto",
    static_address: str | None = None,
    known_device_ips: Iterable[str] = (),
    probe: ProbeFn | None = None,
) -> OverlapReport:
    """Pick Home Assistant's address on the camera network and check it.

    - ``block``: the small block (``static_address`` fixes it), checked.
    - ``point_to_point``: ``static_address`` fixes Home Assistant's address;
      without one, the first free candidate next to the camera.
    - ``auto``: keep ``static_address`` (an earlier pick) while it is still
      free, else the small block, else point-to-point. When nothing is free,
      the small block's report is returned: it explains the conflict best.

    A conflict on the camera's own address cannot be avoided by any shape.
    """
    nets = list(home_networks)
    known = list(known_device_ips)
    # Each address is probed once, however many shapes include it, and candidates are
    # probed together: the whole choice must fit the integration's request timeout.
    probed: dict[str, asyncio.Task[bool | str]] = {}

    async def once(ip: str) -> bool | str:
        assert probe is not None
        if ip not in probed:
            probed[ip] = asyncio.ensure_future(probe(ip))
        return await probed[ip]

    cached = once if probe is not None else None

    async def check(block: CameraBlock) -> OverlapReport:
        return await check_overlap(block, nets, known_device_ips=known, probe=cached)

    if addressing == "block":
        block = (
            block_from_static(static_address, camera_ip)
            if static_address
            else compute_block(camera_ip)
        )
        return await check(block)
    if addressing == "point_to_point" and static_address:
        return await check(block_from_static(static_address, camera_ip, force_point_to_point=True))

    first: OverlapReport | None = None
    if addressing == "auto":
        if static_address:
            remembered = await check(block_from_static(static_address, camera_ip))
            if not remembered.blocked or remembered.result == "camera_collides":
                return remembered
        first = await check(compute_block(camera_ip))
        if not first.blocked or first.result == "camera_collides":
            return first
    candidates = point_to_point_candidates(camera_ip)
    if cached is not None:
        await asyncio.gather(*(cached(c) for c in candidates))
    for candidate in candidates:
        report = await check(point_to_point(camera_ip, candidate))
        if not report.blocked or report.result == "camera_collides":
            return report
        first = first or report
    assert first is not None
    return first
