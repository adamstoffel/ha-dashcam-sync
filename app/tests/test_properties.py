"""Property-based tests: the pure logic and every parser of camera-supplied data."""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import re

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dashcam_sync.catalog import TARGET_LOCAL, ClipRow, CopyRow
from dashcam_sync.drivers.base import classify, timestamp_from_name
from dashcam_sync.drivers.http_index import parse_links
from dashcam_sync.drivers.novatek import NovatekDriver
from dashcam_sync.drivers.viidure import ViidureDriver, _extract_files
from dashcam_sync.engine import build_queue
from dashcam_sync.errors import ConfigError, ProtocolError
from dashcam_sync.models import CHANNEL_ORDER, Channel, Kind, Recording, safe_extension
from dashcam_sync.netblock import compute_block
from dashcam_sync.options import invalid_camera_entries, parse_options, validate_cameras
from dashcam_sync.profile import redact
from dashcam_sync.retention import Rules, plan_purge
from dashcam_sync.storage import clip_relative_path, mount_fstype

# DASHCAM_FUZZ=1 runs a much deeper search (used nightly).
FAST = settings(
    max_examples=3000 if os.environ.get("DASHCAM_FUZZ") else 150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

ipv4 = st.integers(0, 2**32 - 1).map(ipaddress.IPv4Address)
starts = st.datetimes(
    min_value=dt.datetime(2020, 1, 1), max_value=dt.datetime(2030, 1, 1), timezones=st.just(dt.UTC)
)
recordings = st.builds(
    Recording,
    path=st.text(max_size=40),
    size=st.integers(0, 10**9),
    start=starts,
    kind=st.sampled_from(Kind),
    channel=st.sampled_from(Channel),
    locked=st.booleans(),
)


# ------------------------------------------------------------------ addressing


@FAST
@given(ipv4)
def test_block_holds_the_camera_and_a_distinct_usable_host(camera: ipaddress.IPv4Address) -> None:
    try:
        block = compute_block(str(camera))
    except ConfigError:
        # Only addresses that are a network or broadcast address at every size.
        for prefix in range(8, 31):
            net = ipaddress.IPv4Network((int(camera) & (~0 << (32 - prefix)), prefix))
            assert camera in (net.network_address, net.broadcast_address)
        return
    net = block.network
    assert 8 <= net.prefixlen <= 30
    assert camera in net and block.host in net
    assert camera not in (net.network_address, net.broadcast_address)
    assert block.host not in (net.network_address, net.broadcast_address)
    assert block.host != camera
    assert block.static_address == f"{block.host}/{net.prefixlen}"
    if net.prefixlen == 30:  # the common case stays the smallest block
        assert net.num_addresses == 4


@FAST
@given(ipv4)
def test_block_is_the_smallest_one_where_the_camera_is_usable(
    camera: ipaddress.IPv4Address,
) -> None:
    try:
        block = compute_block(str(camera))
    except ConfigError:
        return
    for prefix in range(block.network.prefixlen + 1, 31):
        smaller = ipaddress.IPv4Network((int(camera) & (~0 << (32 - prefix)), prefix))
        assert camera in (smaller.network_address, smaller.broadcast_address)  # not usable there


# ----------------------------------------------------------------------- queue


@FAST
@given(
    st.lists(recordings, max_size=30, unique_by=lambda r: (r.path, r.start, r.channel)),
    st.integers(0, 120),
)
def test_queue_is_a_prioritised_subset_that_keeps_pairs_adjacent(
    recs: list[Recording], recent: int
) -> None:
    everything = build_queue(recs, "everything", recent)
    first = build_queue(recs, "events_first", recent)
    only = build_queue(recs, "events_only", recent)
    assert len(everything) == len(recs) and set(everything) == set(recs)
    assert set(only) <= set(first) <= set(everything)
    assert all(r.kind in (Kind.EVENT, Kind.PARKING) for r in only)
    assert not any(r.kind is Kind.PHOTO for r in first)
    # Events first, then parking, then normal, then photos.
    rank = {Kind.EVENT: 0, Kind.PARKING: 1, Kind.NORMAL: 2, Kind.PHOTO: 3}
    ranks = [rank[r.kind] for r in everything]
    assert ranks == sorted(ranks)
    # Within a kind the newest comes first, and front/rear of one start stay together.
    for kind in Kind:
        starts_of_kind = [r.start for r in everything if r.kind is kind]
        assert starts_of_kind == sorted(starts_of_kind, reverse=True) or kind is Kind.NORMAL
    for i, rec in enumerate(everything[:-1]):
        nxt = everything[i + 1]
        if rec.start == nxt.start and rec.kind is nxt.kind:
            assert CHANNEL_ORDER[rec.channel] <= CHANNEL_ORDER[nxt.channel]


# ------------------------------------------------------------------- retention


def _row(i: int, kind: Kind, age_days: int, size: int, keep: bool, now: dt.datetime):  # type: ignore[no-untyped-def]
    start = now - dt.timedelta(days=age_days)
    clip = ClipRow(i, "c", f"/{i}", size, start, kind, Channel.FRONT, False, None, "done", size,
                   None, "x", keep, "ok", None, 0.0, 0.0)  # fmt: skip
    return clip, CopyRow(i, TARGET_LOCAL, f"/f{i}", size, "present", 0.0)


@FAST
@given(
    st.lists(
        st.tuples(st.sampled_from(Kind), st.integers(0, 400), st.integers(1, 10**9), st.booleans()),
        max_size=25,
    ),
    st.tuples(st.integers(0, 60), st.integers(0, 60), st.integers(0, 60), st.integers(0, 60)),
    st.integers(0, 5),
    st.integers(0, 90),
    st.integers(0, 10**12),
)
def test_purge_never_touches_keep_and_meets_its_targets(
    items, ages, cap_gb, min_free, free
) -> None:  # type: ignore[no-untyped-def]
    now = dt.datetime(2026, 12, 25, tzinfo=dt.UTC)
    rows = [_row(i, k, age, size, keep, now) for i, (k, age, size, keep) in enumerate(items)]
    rules = Rules(*ages, max_total_gb=cap_gb, min_free_percent=min_free)
    total = 10**12
    plan = plan_purge(rows, rules, now=now, free_bytes=min(free, total), total_bytes=total)
    chosen = {c.clip.id for c in plan}
    keep_ids = {c.id for c, _ in rows if c.keep}
    assert not chosen & keep_ids
    assert chosen <= {c.id for c, _ in rows}
    assert len(chosen) == len(plan)  # no duplicates
    # Age rules: everything past its limit that is not kept is gone.
    for clip, _ in rows:
        days = rules.days_for(clip.kind)
        if days and not clip.keep and clip.start <= now - dt.timedelta(days=days):
            assert clip.id in chosen
    # Size cap: what remains fits, unless only kept clips are left.
    if cap_gb:
        remaining = sum(p.size for c, p in rows if c.id not in chosen)
        kept = sum(p.size for c, p in rows if c.keep)
        assert remaining <= max(cap_gb * 1024**3, kept)


# --------------------------------------------------------------------- parsers


@FAST
@given(st.text(max_size=200))
def test_name_and_path_helpers_never_crash(text: str) -> None:
    stamp = timestamp_from_name(text)
    assert stamp is None or stamp.tzinfo is not None
    kind, channel, locked = classify(text)
    assert isinstance(kind, Kind) and isinstance(channel, Channel) and isinstance(locked, bool)
    assert re.fullmatch(r"[a-z0-9]{1,4}", safe_extension(text))


@FAST
@given(st.from_regex(r"[a-z0-9_]{1,32}", fullmatch=True), recordings)
def test_local_paths_never_contain_camera_text(camera_id: str, rec: Recording) -> None:
    rel = clip_relative_path(camera_id, rec)
    assert not rel.is_absolute() and ".." not in rel.parts
    assert rel.parts[0] == camera_id and rel.parts[-2] == rec.kind.value
    assert re.fullmatch(r"\d{8}_\d{6}_(front|rear|interior)\.[a-z0-9]{1,4}", rel.name)


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=20),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=25,
)


@FAST
@given(json_values)
def test_viidure_parsers_survive_any_json(data) -> None:  # type: ignore[no-untyped-def]
    driver = ViidureDriver.__new__(ViidureDriver)
    try:
        files = _extract_files(data)
    except ProtocolError:
        return
    for item in files:
        rec = driver._parse("loop", item)
        assert rec is None or isinstance(rec, Recording)


@FAST
@given(st.text(max_size=300))
def test_novatek_list_parser_survives_any_text(text: str) -> None:
    from defusedxml import ElementTree

    driver = NovatekDriver.__new__(NovatekDriver)
    try:
        root = ElementTree.fromstring(text.encode())
    except Exception:
        return
    for node in root.iter("File"):
        rec = driver._parse_file(node)
        assert rec is None or isinstance(rec, Recording)


@FAST
@given(
    st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=40),
    st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=40),
)
def test_novatek_file_nodes_with_hostile_fields(name: str, size: str) -> None:
    from xml.sax.saxutils import escape

    from defusedxml import ElementTree

    xml = (
        f"<LIST><File><NAME>{escape(name)}</NAME><FPATH>A:\\{escape(name)}</FPATH>"
        f"<SIZE>{escape(size)}</SIZE><TIME>{escape(name)}</TIME><ATTR>{escape(size)}</ATTR></File></LIST>"
    )
    try:
        root = ElementTree.fromstring(xml.encode())
    except Exception:
        return
    driver = NovatekDriver.__new__(NovatekDriver)
    for node in root.iter("File"):
        rec = driver._parse_file(node)
        assert rec is None or isinstance(rec.size, int)


@FAST
@given(st.text(max_size=300))
def test_html_link_parser_never_crashes(html: str) -> None:
    assert isinstance(parse_links(html), list)


# ------------------------------------------------------------- redaction etc.


@FAST
@given(
    st.lists(
        st.sampled_from(
            [
                "192.168.1.10",
                "10.9.8.7",
                "8.8.8.8",
                "AA:BB:CC:DD:EE:FF",
                "aa-bb-cc-dd-ee-ff",
                "hello",
                "{",
                '"ssid":"Home"',
                "<SSID>x</SSID>",
            ]
        ),
        max_size=12,
    ),
    st.text(max_size=20),
)
def test_redaction_removes_every_foreign_address_and_mac(parts: list[str], noise: str) -> None:
    text = noise.join(parts)
    out = redact(text, "192.168.1.254")
    assert not re.search(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b", out)
    for ip in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", out):
        assert ip in ("192.168.1.254", "x.x.x.x") or noise_contains_digits(noise)
    assert "Home" not in out or '"ssid":"REDACTED"' in out


def noise_contains_digits(noise: str) -> bool:
    return bool(re.search(r"\d", noise))


@FAST
@given(st.text(max_size=400))
def test_mountinfo_parser_never_crashes(text: str) -> None:
    assert mount_fstype("/media/x", text) is None or isinstance(mount_fstype("/media/x", text), str)


@FAST
@given(
    st.dictionaries(
        st.sampled_from(["log_level", "cameras", "storage", "retention", "sync", "x"]),
        json_values,
        max_size=5,
    )
)
def test_options_parsing_is_total(raw) -> None:  # type: ignore[no-untyped-def]
    """Garbage options either parse or raise ValueError, never anything else."""
    try:
        invalid_camera_entries(raw)
    except (TypeError, AttributeError) as err:  # pragma: no cover - would be a bug
        pytest.fail(f"unexpected {type(err).__name__}: {err}")
    try:
        opts = parse_options(raw)
    except ValueError:
        return
    validate_cameras(opts)
