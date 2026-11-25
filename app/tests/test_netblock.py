from __future__ import annotations

import ipaddress

import pytest

from dashcam_sync.errors import ConfigError
from dashcam_sync.netblock import (
    block_from_static,
    check_overlap,
    compute_block,
    parse_networks,
)


def test_spec_example_block() -> None:
    block = compute_block("192.168.1.254")
    assert str(block.network) == "192.168.1.252/30"
    assert str(block.host) == "192.168.1.253"
    assert block.static_address == "192.168.1.253/30"
    assert block.describe() == "192.168.1.252 to 192.168.1.255"


@pytest.mark.parametrize(
    ("camera", "network", "prefix"),
    [
        ("192.168.0.1", "192.168.0.0/30", 30),
        ("192.168.169.1", "192.168.169.0/30", 30),
        ("10.99.77.1", "10.99.77.0/30", 30),
        ("192.168.1.252", "192.168.1.248/29", 29),  # first address of a /30: widen
        ("192.168.1.253", "192.168.1.252/30", 30),
    ],
)
def test_block_widens_until_camera_is_usable(camera: str, network: str, prefix: int) -> None:
    block = compute_block(camera)
    assert str(block.network) == network
    assert block.network.prefixlen == prefix
    cam = ipaddress.IPv4Address(camera)
    assert cam not in (block.first, block.last)
    assert block.host != cam
    assert block.host in block.network
    assert block.host not in (block.first, block.last)


def test_broadcast_like_camera_widens_to_29() -> None:
    block = compute_block("192.168.1.251")  # .251 is the broadcast of 192.168.1.248/30
    assert block.camera == ipaddress.IPv4Address("192.168.1.251")
    assert block.camera not in (block.first, block.last)


def test_invalid_camera_ip() -> None:
    with pytest.raises(ConfigError):
        compute_block("not-an-ip")


def test_block_from_static_roundtrip() -> None:
    block = block_from_static("192.168.1.253/30", "192.168.1.254")
    assert str(block.network) == "192.168.1.252/30"
    with pytest.raises(ConfigError):
        block_from_static("192.168.1.253/30", "192.168.7.7")
    with pytest.raises(ConfigError):
        block_from_static("192.168.1.254/30", "192.168.1.254")


def test_parse_networks_skips_garbage_and_ipv6() -> None:
    nets = parse_networks(["192.168.1.10/24", "fe80::1/64", "junk"])
    assert [str(n) for n in nets] == ["192.168.1.0/24"]


async def test_overlap_informational_when_block_free() -> None:
    block = compute_block("192.168.1.254")

    async def silent(ip: str) -> bool:
        return False

    report = await check_overlap(block, parse_networks(["192.168.1.10/24"]), probe=silent)
    assert report.result == "overlap_block_free"
    assert not report.blocked
    assert report.probed


async def test_no_overlap_with_other_range() -> None:
    block = compute_block("192.168.169.1")
    report = await check_overlap(block, parse_networks(["10.0.0.5/24"]))
    assert report.result == "no_overlap"
    assert not report.probed


async def test_known_device_in_block_blocks() -> None:
    block = compute_block("192.168.1.254")
    report = await check_overlap(
        block, parse_networks(["192.168.1.10/24"]), known_device_ips=["192.168.1.255", "x"]
    )
    assert report.result == "device_in_block"
    assert report.conflicts == ["192.168.1.255"]
    assert report.blocked


async def test_camera_address_collides() -> None:
    block = compute_block("192.168.1.254")

    async def probe(ip: str) -> bool:
        return ip == "192.168.1.254"

    report = await check_overlap(block, parse_networks(["192.168.1.10/24"]), probe=probe)
    assert report.result == "camera_collides"
    assert report.as_dict()["blocked"] is True


async def test_address_in_use() -> None:
    block = compute_block("192.168.1.254")

    async def probe(ip: str) -> bool:
        return ip == "192.168.1.255"

    report = await check_overlap(block, [], probe=probe)
    assert report.result == "address_in_use"
    assert report.conflicts == ["192.168.1.255"]
