"""Interrupted Wi-Fi and LAN links: a clip is only ever complete when every byte is there.

Each test makes the fake camera fail the way real cameras and networks do, then checks
two things: no clip is recorded (or placed under its final name) unless it is complete,
and the next visit finishes the download without fetching bytes twice.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio

import dashcam_sync.drivers.http as http_mod
from dashcam_sync.catalog import TARGET_LOCAL, TARGET_NETWORK, Catalog
from dashcam_sync.drivers.http import parse_content_range
from dashcam_sync.options import StorageOptions
from dashcam_sync.storage import StorageManager
from fakecam import FakeCamera, make_bytes
from test_engine import Env, make_env

BIG, SMALL = 70_000, 40_000
BIG_NAME, SMALL_NAME = "20261122_101500_F.MP4", "20261122_101600_F.MP4"


@pytest_asyncio.fixture
async def env(
    tmp_path: Path, session: aiohttp.ClientSession, novatek_cam: FakeCamera
) -> AsyncIterator[Env]:
    novatek_cam.add_novatek(BIG_NAME, BIG, time="2026/11/22 10:15:00")
    novatek_cam.add_novatek(
        SMALL_NAME, SMALL, folder="MOVIE\\RO", time="2026/11/22 10:16:00", attr=33
    )
    environment = make_env(tmp_path, session, novatek_cam)
    yield environment
    await environment.post.stop()
    environment.catalog.close()


def final_files(env: Env) -> list[Path]:
    root = env.media / "dashcam_sync"
    return [p for p in root.rglob("*") if p.is_file() and ".partial" not in p.parts]


def assert_exact(env: Env) -> None:
    """Every recorded clip holds exactly the camera's bytes."""
    for clip in env.catalog.list_clips(camera="car"):
        copy = env.catalog.copy(clip.id, TARGET_LOCAL)
        assert copy is not None
        name = SMALL_NAME if clip.size == SMALL else BIG_NAME
        assert Path(copy.path).read_bytes() == make_bytes(name, clip.size), copy.path


def recorded(env: Env) -> int:
    return len(env.catalog.list_clips(camera="car"))


# ---------------------------------------------------------------- clean early end


async def test_a_clean_early_end_keeps_the_partial_file_and_resumes(env: Env) -> None:
    """The body ends politely mid-file and looks like a normal end.

    That can only happen when the body carries no length (chunked or closed): with a
    Content-Length a short body is a payload error, covered by the drop tests.
    """
    env.cam.faults.eof_after = 30_000
    env.cam.faults.hide_length = True  # the listing still gives the size
    first = await env.session()
    # Both files are cut short once: neither is recorded or placed under a final name.
    assert recorded(env) == 0 and first.failures == 2
    assert final_files(env) == []
    unfinished = env.catalog.unfinished("car")
    assert sorted(c.offset for c in unfinished) == [30_000, 30_000]
    for clip in unfinished:
        assert Path(clip.part_path or "").stat().st_size == 30_000  # kept, not discarded

    env.cam.range_requests.clear()
    second = await env.session()
    assert second.result == "ok" and second.clips == 2
    assert all(r.endswith("=30000-") for _, r in env.cam.range_requests)  # resumed
    assert first.bytes + second.bytes == BIG + SMALL  # nothing fetched twice
    assert_exact(env)


# ---------------------------------------------------------------------- stall


async def test_a_stalled_link_times_out_and_resumes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wi-Fi fades: the socket stays open but no bytes arrive."""
    monkeypatch.setattr(
        http_mod,
        "STREAM_TIMEOUT",
        aiohttp.ClientTimeout(total=None, connect=2, sock_connect=2, sock_read=0.3),
    )
    env.cam.faults.stall_after = 25_000  # once per file
    first = await env.session()
    assert first.result == "link_lost" and recorded(env) == 0 and final_files(env) == []
    assert env.cam.recording  # the camera was still put back into recording
    total = first.bytes
    for _ in range(3):  # each visit gets further; each file stalls once
        stats = await env.session()
        total += stats.bytes
        if stats.result == "ok":
            break
    assert stats.result == "ok" and recorded(env) == 2
    assert total == BIG + SMALL  # every resume continued; nothing fetched twice
    assert_exact(env)


# ------------------------------------------------------------- dishonest Range


async def test_a_partial_answer_from_the_wrong_offset_is_refused(env: Env) -> None:
    """A buggy server answers 206 but from an earlier byte: splicing that would corrupt."""
    env.cam.faults.drop_after = 30_000
    await env.session()
    env.cam.faults.drop_after = None
    env.cam.faults.range_wrong_start = 100
    second = await env.session()
    assert second.failures >= 1
    for clip in env.catalog.unfinished("car"):
        part = Path(clip.part_path or "")
        # The partial file is untouched: still a clean prefix of the real file.
        assert (
            part.read_bytes()
            == make_bytes(SMALL_NAME if clip.size == SMALL else BIG_NAME, clip.size)[
                : part.stat().st_size
            ]
        )
    env.cam.faults.range_wrong_start = 0
    third = await env.session()
    assert third.result == "ok" and recorded(env) == 2
    assert_exact(env)


async def test_a_partial_answer_from_byte_zero_restarts_the_file(env: Env) -> None:
    env.cam.faults.drop_after = 30_000
    await env.session()
    env.cam.faults.drop_after = None
    env.cam.faults.range_restart_206 = True
    second = await env.session()
    assert second.result == "ok" and second.clips == 2
    assert_exact(env)


# ------------------------------------------------------- no size known anywhere


@pytest_asyncio.fixture
async def sizeless(tmp_path: Path, session: aiohttp.ClientSession) -> AsyncIterator[Env]:
    """A plain HTTP index camera that gives no sizes: no HEAD length, chunked bodies."""
    cam = FakeCamera("index")
    await cam.start()
    cam.add_index("record", "20261122_101500_F.mp4", BIG)
    cam.faults.hide_length = True
    environment = make_env(tmp_path, session, cam)
    yield environment
    await environment.post.stop()
    environment.catalog.close()
    await cam.stop()


async def test_without_any_size_a_cut_download_is_not_taken_as_complete(
    sizeless: Env,
) -> None:
    env = sizeless
    env.cam.faults.eof_after = 30_000
    first = await env.session()
    assert recorded(env) == 0 and final_files(env) == [] and first.failures == 1
    # The camera was asked for the bytes after the end, and had more.
    assert ("/record/20261122_101500_F.mp4", "bytes=30000-") in env.cam.range_requests

    second = await env.session()
    assert second.result == "ok" and second.clips == 1
    # Completion was confirmed with a 416 past the last byte.
    assert ("/record/20261122_101500_F.mp4", f"bytes={BIG}-") in env.cam.range_requests
    [clip] = env.catalog.list_clips(camera="car")
    copy = env.catalog.copy(clip.id, TARGET_LOCAL)
    assert copy is not None
    assert Path(copy.path).read_bytes() == make_bytes("20261122_101500_F.mp4", BIG)


async def test_without_sizes_or_range_support_nothing_is_marked_complete(
    sizeless: Env,
) -> None:
    env = sizeless
    env.cam.faults.ignore_range = True
    stats = await env.session()
    assert recorded(env) == 0 and final_files(env) == [] and stats.failures == 1
    [clip] = env.catalog.list_clips(camera="car", status="failed")
    assert "cannot be confirmed" in (clip.error or "")


# -------------------------------------------------- crash between steps


async def test_a_crash_after_placing_a_file_does_not_download_it_again(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = env.catalog.complete_clip
    calls = {"n": 0}

    def crash_once(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("power cut")
        return real(*args, **kwargs)

    monkeypatch.setattr(env.catalog, "complete_clip", crash_once)
    first = await env.session()
    assert first.failures == 1 and recorded(env) == 1
    env.cam.requests_files.clear()  # type: ignore[attr-defined]
    second = await env.session()
    assert second.result == "ok" and recorded(env) == 2
    assert env.cam.requests_files == []  # type: ignore[attr-defined]  # adopted, not refetched
    assert not [p for p in final_files(env) if "_2." in p.name]
    assert_exact(env)


# -------------------------------------------------------------- unit checks


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("bytes 100-199/200", (100, 200)),
        ("bytes 0-99/*", (0, None)),
        ("BYTES 5-9/10", (5, 10)),
        ("items 0-1/2", None),
        ("bytes */200", None),
        ("", None),
        (None, None),
    ],
)
def test_content_range_parsing(header: str | None, expected: object) -> None:
    assert parse_content_range(header) == expected


def test_leftover_partials_and_temp_copies_are_cleaned_safely(
    tmp_path: Path, catalog: Catalog
) -> None:
    media = tmp_path / "media"
    mgr = StorageManager(
        catalog, StorageOptions(), media_root=media, share_root=tmp_path / "s", min_free_percent=0
    )
    partial = mgr.local.partial_dir()
    from conftest import at
    from dashcam_sync.models import Kind, Recording

    row, _ = catalog.upsert_recording("car", Recording("/a.MP4", 9, at(9), Kind.NORMAL))
    kept = partial / f"{row.id}.part"
    kept.write_bytes(b"resume me")
    catalog.set_offset(row.id, 9, str(kept))
    orphan = partial / "999.part"
    orphan.write_bytes(b"x")
    old_tmp = media / "dashcam_sync" / "car" / ".clip.mp4.abc.tmp"
    old_tmp.parent.mkdir(parents=True)
    old_tmp.write_bytes(b"x")
    fresh_tmp = old_tmp.with_name(".clip.mp4.def.tmp")
    fresh_tmp.write_bytes(b"x")
    hour_ago = time.time() - 7200
    for path in (kept, orphan, old_tmp):
        os.utime(path, (hour_ago, hour_ago))

    assert mgr.clean_leftovers() == 2
    assert kept.exists() and fresh_tmp.exists()
    assert not orphan.exists() and not old_tmp.exists()


def test_shipping_reuses_a_copy_finished_before_a_crash(tmp_path: Path, catalog: Catalog) -> None:
    from conftest import at
    from dashcam_sync.models import Kind, Recording

    media, nas = tmp_path / "media", tmp_path / "media" / "nas"
    nas.mkdir(parents=True)
    mgr = StorageManager(
        catalog,
        StorageOptions(target="both", network_folder="nas"),
        media_root=media,
        share_root=tmp_path / "s",
        min_free_percent=0,
        mountinfo=f"1 2 0:1 / {nas} rw - cifs //n/s rw\n",
    )
    rec = Recording("/a.MP4", 4, at(9), Kind.NORMAL)
    row, _ = catalog.upsert_recording("car", rec)
    local = mgr.local.root / "car" / "2026" / "11" / "22" / "normal" / "a.mp4"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"data")
    catalog.complete_clip(row.id, sha256="s", target=TARGET_LOCAL, path=str(local), size=4)
    catalog.set_post_status(row.id, "done")
    mgr.queue_network_copy(row.id)
    already = nas / local.relative_to(mgr.local.root)
    already.parent.mkdir(parents=True)
    already.write_bytes(b"data")  # copied, then the app died before recording it
    assert mgr.ship_pending() == 1
    assert catalog.copy(row.id, TARGET_NETWORK).path == str(already)  # type: ignore[union-attr]
    assert not list(nas.rglob("*_2.*"))
