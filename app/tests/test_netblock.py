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


# ------------------------------------------------------------ point-to-point


def test_point_to_point_claims_only_two_addresses() -> None:
    from dashcam_sync.netblock import block_from_static, point_to_point

    blk = point_to_point("192.168.0.1", "192.168.0.254")
    assert blk.static_address == "192.168.0.254/32" and blk.gateway == "192.168.0.1"
    assert [str(a) for a in blk.claimed()] == ["192.168.0.1", "192.168.0.254"]
    assert blk.mode == "point_to_point" and "only" in blk.describe()
    assert block_from_static("192.168.0.254/32", "192.168.0.1") == blk
    assert block_from_static("192.168.0.9/30", "192.168.0.1", force_point_to_point=True).host == (
        point_to_point("192.168.0.1", "192.168.0.9").host
    )
    with pytest.raises(ConfigError):
        point_to_point("192.168.0.1", "192.168.0.1")
    with pytest.raises(ConfigError):
        point_to_point("192.168.0.1", "127.0.0.1")
    with pytest.raises(ConfigError):
        block_from_static("nonsense", "192.168.0.1")


def test_point_to_point_candidates_come_from_the_top_of_the_camera_subnet() -> None:
    from dashcam_sync.netblock import point_to_point_candidates

    assert point_to_point_candidates("192.168.0.1", limit=3) == [
        "192.168.0.254", "192.168.0.253", "192.168.0.252",
    ]  # fmt: skip
    assert point_to_point_candidates("192.168.1.254", limit=2) == ["192.168.1.253", "192.168.1.252"]


async def test_choose_block_matches_the_real_probe() -> None:
    """Home 192.168.0.0/22, camera 192.168.0.1, something answers at 192.168.0.2."""
    from dashcam_sync.netblock import choose_block

    home = [ipaddress.IPv4Network("192.168.0.0/22")]
    taken = {"192.168.0.2"}

    async def probe(ip: str) -> bool:
        return ip in taken

    report = await choose_block("192.168.0.1", home, probe=probe)
    assert not report.blocked and report.block.static_address == "192.168.0.254/32"
    assert report.result == "overlap_block_free" and report.probed

    taken.add("192.168.0.254")
    report = await choose_block("192.168.0.1", home, probe=probe)
    assert report.block.static_address == "192.168.0.253/32"

    # Fixed block: reported as it is, never swapped.
    fixed = await choose_block("192.168.0.1", home, addressing="block", probe=probe)
    assert fixed.blocked and fixed.result == "address_in_use"

    # The camera's own address is taken: no shape helps, the block's report explains it.
    taken.add("192.168.0.1")
    report = await choose_block("192.168.0.1", home, probe=probe)
    assert report.blocked and report.result == "camera_collides"


async def test_choose_block_keeps_a_free_remembered_pick() -> None:
    from dashcam_sync.netblock import choose_block

    home = [ipaddress.IPv4Network("192.168.0.0/22")]
    kept = await choose_block("192.168.0.1", home, static_address="192.168.0.200/32")
    assert kept.block.static_address == "192.168.0.200/32"
    p2p = await choose_block("192.168.0.1", home, addressing="point_to_point")
    assert p2p.block.static_address == "192.168.0.254/32"
    fixed = await choose_block(
        "192.168.0.1", home, addressing="point_to_point", static_address="192.168.0.9/32",
        known_device_ips=["192.168.0.9"],
    )  # fmt: skip
    assert fixed.blocked and fixed.block.static_address == "192.168.0.9/32"


async def test_nothing_free_reports_the_small_block() -> None:
    from dashcam_sync.netblock import choose_block

    async def everything(ip: str) -> bool:
        return ip != "192.168.0.1"

    home = [ipaddress.IPv4Network("192.168.0.0/22")]
    report = await choose_block("192.168.0.1", home, probe=everything)
    assert report.blocked and report.block.mode == "block"
    only_p2p = await choose_block(
        "192.168.0.1", home, addressing="point_to_point", probe=everything
    )
    assert only_p2p.blocked and only_p2p.block.mode == "point_to_point"


async def test_the_report_says_how_each_address_answered() -> None:
    from dashcam_sync.netblock import choose_block

    async def probe(ip: str) -> str:
        return "refused a connection on port 443 (no ping answer)" if ip == "192.168.0.2" else ""

    home = [ipaddress.IPv4Network("192.168.0.0/22")]
    fixed = await choose_block("192.168.0.1", home, addressing="block", probe=probe)
    assert fixed.as_dict()["evidence"] == {
        "192.168.0.2": "refused a connection on port 443 (no ping answer)"
    }


async def test_choosing_probes_each_address_once_and_in_parallel() -> None:
    """Several taken candidates must still answer well within the integration's timeout."""
    import asyncio
    import time
    from collections import Counter

    from dashcam_sync.netblock import choose_block

    calls: Counter[str] = Counter()
    taken = {"192.168.0.2", "192.168.0.254", "192.168.0.253", "192.168.0.252"}

    async def slow_probe(ip: str) -> bool:
        calls[ip] += 1
        await asyncio.sleep(0.2)  # each probe takes its time, like a real timeout
        return ip in taken

    home = [ipaddress.IPv4Network("192.168.0.0/22")]
    started = time.monotonic()
    report = await choose_block("192.168.0.1", home, probe=slow_probe)
    elapsed = time.monotonic() - started
    assert report.block.static_address == "192.168.0.251/32" and not report.blocked
    assert max(calls.values()) == 1  # the camera and every address: probed once
    assert elapsed < 1.0  # sequential probing would take over 2.5 s
