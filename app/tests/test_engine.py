from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import pytest_asyncio

from conftest import at
from dashcam_sync.catalog import TARGET_LOCAL, TARGET_NETWORK, Catalog
from dashcam_sync.engine import (
    CameraWorker,
    SyncContext,
    Timing,
    build_queue,
)
from dashcam_sync.events import EventBus
from dashcam_sync.models import Channel, Kind, Recording
from dashcam_sync.options import CameraOptions, StorageOptions, SyncOptions
from dashcam_sync.postprocess import PostProcessor
from dashcam_sync.restoreflag import RestoreFlags
from dashcam_sync.storage import StorageManager
from fakecam import FakeCamera, make_bytes

# --------------------------------------------------------------- build_queue


def rec(
    name: str, minute: int, kind: Kind = Kind.NORMAL, channel: Channel = Channel.FRONT
) -> Recording:
    return Recording(f"/{name}", 10, at(10, minute), kind, channel)


def test_queue_order_matches_spec() -> None:
    recs = [
        rec("n_old", 0),
        rec("photo", 59, Kind.PHOTO),
        rec("n_new", 59),
        rec("n_recent", 55),
        rec("park", 30, Kind.PARKING),
        rec("ev_old", 5, Kind.EVENT),
        rec("ev_new", 40, Kind.EVENT),
    ]
    got = [r.path for r in build_queue(recs, "everything", 10)]
    assert got == [
        "/ev_new",
        "/ev_old",
        "/park",
        "/n_new",
        "/n_recent",
        "/n_old",
        "/photo",
    ]


def test_queue_modes() -> None:
    recs = [
        rec("n_old", 0),
        rec("n_new", 59),
        rec("park", 30, Kind.PARKING),
        rec("ev", 40, Kind.EVENT),
        rec("photo", 1, Kind.PHOTO),
    ]
    assert [r.path for r in build_queue(recs, "events_only", 10)] == ["/ev", "/park"]
    assert [r.path for r in build_queue(recs, "events_first", 10)] == ["/ev", "/park", "/n_new"]
    assert len(build_queue(recs, "everything", 10)) == 5


def test_front_and_rear_stay_paired() -> None:
    recs = [
        rec("a_rear", 20, Kind.EVENT, Channel.REAR),
        rec("b", 30, Kind.EVENT),
        rec("a_front", 20, Kind.EVENT),
    ]
    got = [r.path for r in build_queue(recs, "everything", 10)]
    assert got == ["/b", "/a_front", "/a_rear"]


def test_recent_window_uses_camera_clock_not_host_clock() -> None:
    recs = [rec("a", 50), rec("b", 59), rec("c", 40)]
    assert [r.path for r in build_queue(recs, "events_first", 10)] == ["/b", "/a"]
    assert build_queue([], "everything", 10) == []


# ------------------------------------------------------------------- harness


@dataclass
class Env:
    ctx: SyncContext
    cam: FakeCamera
    worker: CameraWorker
    catalog: Catalog
    storage: StorageManager
    post: PostProcessor
    media: Path
    flags: RestoreFlags
    bus: EventBus
    link: dict[str, bool]
    sync: SyncOptions

    async def session(self):  # type: ignore[no-untyped-def]
        result = await self.worker._run_session()
        await self.post.drain()
        return result


def make_env(
    tmp_path: Path,
    session: aiohttp.ClientSession,
    cam: FakeCamera,
    *,
    sync: SyncOptions | None = None,
    storage_opts: StorageOptions | None = None,
    camera: dict | None = None,
    min_free: int = 0,
) -> Env:
    media = tmp_path / "media"
    media.mkdir(parents=True, exist_ok=True)
    catalog = Catalog(":memory:")
    sync = sync or SyncOptions(mode="everything", remux_to_mp4=False, thumbnails=False)
    storage_opts = storage_opts or StorageOptions()
    storage = StorageManager(
        catalog,
        storage_opts,
        media_root=media,
        share_root=tmp_path / "share",
        min_free_percent=min_free,
    )
    flags = RestoreFlags(tmp_path / "data" / "restore")
    bus = EventBus()
    post = PostProcessor(catalog, lambda: sync, lambda: storage_opts)
    link = {"up": True}

    async def link_up(_cam: CameraOptions) -> bool:
        return link["up"]

    ctx = SyncContext(
        session=session,
        catalog=catalog,
        storage=lambda: storage,
        bus=bus,
        flags=flags,
        post=post,
        sync_options=lambda: sync,
        link_up=link_up,
        timing=Timing(
            poll=0.02, hint_poll=0.005, hint_duration=0.2, backoff=(0.01, 0.01), restore_timeout=2
        ),
    )
    options = CameraOptions(
        id="car",
        name="Car",
        connection="lan",
        host=cam.host,
        driver="auto",
        **(camera or {}),
    )
    worker = CameraWorker(ctx, options)
    return Env(ctx, cam, worker, catalog, storage, post, media, flags, bus, link, sync)


@pytest_asyncio.fixture
async def env(
    tmp_path: Path, session: aiohttp.ClientSession, novatek_cam: FakeCamera
) -> AsyncIterator[Env]:
    novatek_cam.add_novatek("20261122_101500_F.MP4", 70_000, time="2026/11/22 10:15:00")
    novatek_cam.add_novatek(
        "20261122_101600_F.MP4", 40_000, folder="MOVIE\\RO", time="2026/11/22 10:16:00", attr=33
    )
    environment = make_env(tmp_path, session, novatek_cam)
    yield environment
    await environment.post.stop()
    environment.catalog.close()


# ------------------------------------------------------------------ sessions


async def test_full_session_downloads_verifies_and_restores(env: Env) -> None:
    stats = await env.session()
    assert stats.result == "ok" and stats.clips == 2 and stats.failures == 0
    assert env.cam.recording and env.cam.mode == "movie"  # camera is back in recording
    assert env.flags.get("car") is None and not env.worker.status.restore_pending
    clips = env.catalog.list_clips(camera="car")
    assert len(clips) == 2
    by_kind = {c.kind: c for c in clips}
    event = by_kind[Kind.EVENT]
    assert event.locked and event.sha256
    copy = env.catalog.copy(event.id, TARGET_LOCAL)
    assert copy is not None
    path = Path(copy.path)
    assert path.relative_to(env.media).parts[:2] == ("dashcam_sync", "car")
    assert "event" in path.parts and path.name.endswith("_front.mp4")
    assert path.read_bytes() == make_bytes("20261122_101600_F.MP4", 40_000)
    # Nothing is left half-written and the sidecar exists.
    assert not list((env.media / "dashcam_sync" / ".partial").glob("*"))
    assert path.with_suffix(".json").exists()
    assert env.worker.status.last_sync is not None and env.worker.status.sync_status == "idle"
    assert env.worker.status.clips_remaining == 0 and env.worker.status.bytes_remaining == 0
    assert env.catalog.recent_sessions(1)[0]["result"] == "ok"


async def test_events_are_downloaded_before_normal_clips(env: Env) -> None:
    await env.session()
    files = list(env.cam.requests_files)  # type: ignore[attr-defined]
    assert files[0].endswith("101600_F.MP4") and files[1].endswith("101500_F.MP4")


async def test_second_session_downloads_nothing_new(env: Env) -> None:
    await env.session()
    env.cam.requests_files.clear()  # type: ignore[attr-defined]
    stats = await env.session()
    assert stats.clips == 0 and stats.result == "ok"
    assert env.cam.requests_files == []  # type: ignore[attr-defined]


async def test_interrupted_download_resumes_without_downloading_twice(env: Env) -> None:
    env.cam.faults.drop_after = 30_000
    first = await env.session()
    assert first.result == "link_lost"
    # The camera was put back into recording even though the link dropped mid-file.
    assert env.cam.recording and env.cam.mode == "movie" and env.flags.get("car") is None
    env.cam.faults.drop_after = None
    interrupted = [c for c in env.catalog.unfinished("car") if c.offset > 0]
    assert len(interrupted) == 1 and interrupted[0].offset >= 30_000 - 1
    part = Path(interrupted[0].part_path or "")
    assert part.exists() and part.stat().st_size == interrupted[0].offset

    env.cam.range_requests.clear()
    second = await env.session()
    assert second.result == "ok" and second.clips == 2
    assert env.cam.range_requests and env.cam.range_requests[0][1].startswith("bytes=")
    total_listed = 70_000 + 40_000
    assert second.bytes + first.bytes <= total_listed + 1  # nothing downloaded twice
    for clip in env.catalog.list_clips(camera="car"):
        copy = env.catalog.copy(clip.id, TARGET_LOCAL)
        assert copy is not None
        name = "20261122_101600_F.MP4" if clip.kind is Kind.EVENT else "20261122_101500_F.MP4"
        assert Path(copy.path).read_bytes() == make_bytes(name, clip.size)


async def test_camera_that_ignores_range_restarts_the_file(env: Env) -> None:
    env.cam.faults.drop_after = 30_000
    await env.session()
    env.cam.faults.drop_after = None
    env.cam.faults.ignore_range = True
    second = await env.session()
    assert second.result == "ok" and second.clips == 2
    assert env.catalog.kv_get("range_ignored:car") is True
    for clip in env.catalog.list_clips(camera="car"):
        copy = env.catalog.copy(clip.id, TARGET_LOCAL)
        assert copy is not None and len(Path(copy.path).read_bytes()) == clip.size


async def test_size_mismatch_is_rejected_and_discarded(env: Env) -> None:
    env.cam.files[0].list_size = 70_001  # the listing lies about the first file
    env.cam.files[1].list_size = None
    stats = await env.session()
    assert stats.clips == 1 and stats.failures == 1 and stats.result == "partial"
    failed = list(env.catalog.list_clips(camera="car", status="failed"))
    assert len(failed) == 1
    assert not list((env.media / "dashcam_sync" / ".partial").glob("*.part"))
    assert env.cam.recording


async def test_listing_failure_still_restores(env: Env) -> None:
    env.cam.faults.list_error = True
    stats = await env.session()
    assert stats.result == "list_failed"
    assert env.cam.recording and env.cam.mode == "movie" and env.flags.get("car") is None
    events = env.bus.subscribe()
    await env.session()
    env.cam.faults.list_error = False
    assert env.catalog.consecutive_sessions_with("car", "list_failed") == 2
    del events


async def test_three_failed_listings_raise_an_issue_event(env: Env) -> None:
    queue = env.bus.subscribe()
    env.cam.faults.list_error = True
    for _ in range(3):
        await env.session()
    seen = []
    while not queue.empty():
        seen.append(queue.get_nowait())
    assert any(e["type"] == "issue" and e["key"] == "listing_keeps_failing" for e in seen)
    assert any(e["type"] == "session" and e["result"] == "sync_failed" for e in seen)


async def test_failed_restore_keeps_flag_and_next_contact_restores_first(env: Env) -> None:
    env.cam.fail_restore = True
    stats = await env.session()
    assert stats.result == "ok"  # downloads finished
    assert not env.cam.recording  # but the camera is still stopped
    assert env.flags.get("car") is not None and env.worker.status.restore_pending

    env.cam.fail_restore = False
    env.cam.requests.clear()
    await env.session()
    assert env.cam.recording and env.flags.get("car") is None
    cmds = [r for r in env.cam.requests if "cmd=" in r]
    # Restore ran before the new session started listing.
    first_record_start = next(i for i, r in enumerate(cmds) if "cmd=2001&par=1" in r)
    first_list = next(i for i, r in enumerate(cmds) if "cmd=3015" in r)
    assert first_record_start < first_list


async def test_crash_leaves_flag_and_restart_restores(env: Env) -> None:
    # Simulate a crash in playback mode: flag on disk, camera stopped.
    env.flags.set("car", "novatek", env.cam.host)
    env.cam.recording = False
    env.cam.mode = "playback"
    fresh = CameraWorker(env.ctx, env.worker.camera)
    assert fresh.status.restore_pending  # picked up from /data at startup
    await fresh._run_session()
    assert env.cam.recording and env.cam.mode == "movie"


async def test_restore_runs_even_when_cancelled_mid_download(env: Env) -> None:
    env.cam.faults.chunk_delay = 0.01
    env.cam.add_novatek("20261122_101700_F.MP4", 400_000, time="2026/11/22 10:17:00")
    env.worker.start()
    for _ in range(300):
        if env.worker.status.sync_status == "downloading":
            break
        await asyncio.sleep(0.01)
    assert env.worker.status.sync_status == "downloading"
    assert not env.cam.recording  # in playback while downloading
    await env.worker.stop()
    assert env.cam.recording and env.cam.mode == "movie"
    assert env.flags.get("car") is None


async def test_unsupported_camera_sets_flag_and_stops(
    tmp_path: Path, session: aiohttp.ClientSession
) -> None:
    cam = FakeCamera("novatek")
    cam.family = "nothing"
    await cam.start()
    try:
        env = make_env(tmp_path, session, cam)
        stats = await env.session()
        assert stats.result == "unsupported" and env.worker.status.unsupported
        assert env.flags.get("car") is None
        env.catalog.close()
    finally:
        await cam.stop()


async def test_storage_full_stops_the_session(
    tmp_path: Path, session: aiohttp.ClientSession, novatek_cam: FakeCamera
) -> None:
    novatek_cam.add_novatek("a.MP4", 1000)
    env = make_env(tmp_path, session, novatek_cam, min_free=99)
    stats = await env.session()
    assert stats.result == "storage_full" and stats.clips == 0
    assert novatek_cam.recording
    env.catalog.close()


async def test_delete_from_camera_rules(
    tmp_path: Path, session: aiohttp.ClientSession, novatek_cam: FakeCamera
) -> None:
    novatek_cam.add_novatek("20261122_101500_F.MP4", 1000)
    novatek_cam.add_novatek("20261122_101600_F.MP4", 1000, folder="MOVIE\\RO", attr=33)
    # Off by default: nothing is ever deleted.
    env = make_env(tmp_path, session, novatek_cam)
    await env.session()
    assert novatek_cam.deleted == []
    env.catalog.close()


async def test_delete_from_camera_only_verified_and_events_need_own_switch(
    tmp_path: Path, session: aiohttp.ClientSession, novatek_cam: FakeCamera
) -> None:
    novatek_cam.add_novatek("20261122_101500_F.MP4", 1000)
    novatek_cam.add_novatek("20261122_101600_F.MP4", 1000, folder="MOVIE\\RO", attr=33)
    sync = SyncOptions(
        mode="everything", remux_to_mp4=False, thumbnails=False, delete_from_camera=True
    )
    env = make_env(tmp_path, session, novatek_cam, sync=sync)
    await env.session()
    assert novatek_cam.deleted == ["A:\\CARDV\\MOVIE\\20261122_101500_F.MP4"]
    env.catalog.close()

    # With the separate option, events go too, but a clip that failed verification never does.
    novatek_cam.deleted.clear()
    novatek_cam.files = []
    novatek_cam.add_novatek("20261122_101500_F.MP4", 1000)
    novatek_cam.add_novatek("20261122_101600_F.MP4", 1000, folder="MOVIE\\RO", attr=33)
    novatek_cam.files[0].list_size = 999  # fails verification
    sync2 = SyncOptions(
        mode="everything",
        remux_to_mp4=False,
        thumbnails=False,
        delete_from_camera=True,
        delete_events_from_camera=True,
    )
    env2 = make_env(tmp_path / "two", session, novatek_cam, sync=sync2)
    await env2.session()
    assert novatek_cam.deleted == ["A:\\CARDV\\MOVIE\\RO\\20261122_101600_F.MP4"]
    env2.catalog.close()


async def test_network_copy_queued_for_both(
    tmp_path: Path, session: aiohttp.ClientSession, novatek_cam: FakeCamera
) -> None:
    novatek_cam.add_novatek("20261122_101500_F.MP4", 1000)
    nas = tmp_path / "media" / "nas"
    nas.mkdir(parents=True)
    env = make_env(
        tmp_path,
        session,
        novatek_cam,
        storage_opts=StorageOptions(target="both", network_folder="nas"),
    )
    env.storage = StorageManager(
        env.catalog,
        StorageOptions(target="both", network_folder="nas"),
        media_root=env.media,
        share_root=tmp_path / "share",
        min_free_percent=0,
        mountinfo=f"1 2 0:1 / {nas} rw - cifs a rw\n",
    )
    env.ctx.storage = lambda: env.storage
    await env.session()
    assert env.storage.ship_pending() == 1
    clip = env.catalog.list_clips(camera="car")[0]
    net = env.catalog.copy(clip.id, TARGET_NETWORK)
    assert net is not None and Path(net.path).exists()
    env.catalog.close()


# ----------------------------------------------------------------- the loop


async def wait_for(predicate, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


async def test_loop_syncs_on_link_up_edge_only(env: Env) -> None:
    env.link["up"] = False
    env.worker.start()
    await asyncio.sleep(0.1)
    assert not env.worker.status.connected and env.cam.requests == []
    env.link["up"] = True
    await wait_for(lambda: env.worker.status.last_sync is not None)
    assert env.worker.status.connected and env.cam.recording
    sessions = len(env.catalog.recent_sessions(50))
    await asyncio.sleep(0.15)
    assert len(env.catalog.recent_sessions(50)) == sessions  # no repeat while link stays up
    # Link drops and returns: a new session starts.
    env.link["up"] = False
    await wait_for(lambda: not env.worker.status.connected)
    env.link["up"] = True
    await wait_for(lambda: len(env.catalog.recent_sessions(50)) == sessions + 1)
    await env.worker.stop()


async def test_manual_sync_and_restore_requests(env: Env) -> None:
    env.worker.start()
    await wait_for(lambda: env.worker.status.last_sync is not None)
    count = len(env.catalog.recent_sessions(50))
    env.worker.request_sync()
    await wait_for(lambda: len(env.catalog.recent_sessions(50)) == count + 1)
    env.cam.recording = False
    env.worker.request_restore()
    await wait_for(lambda: env.cam.recording)
    await env.worker.stop()


async def test_failed_sessions_back_off_then_wait_for_new_link(env: Env) -> None:
    env.cam.faults.list_error = True
    env.worker.start()
    await wait_for(lambda: len(env.catalog.recent_sessions(50)) >= 3)
    await asyncio.sleep(0.2)
    # backoff = (0.01, 0.01): 1 initial + 2 retries, then it stops retrying.
    assert len(env.catalog.recent_sessions(50)) == 3
    env.link["up"] = False
    await wait_for(lambda: not env.worker.status.connected)
    env.cam.faults.list_error = False
    env.link["up"] = True
    await wait_for(lambda: env.worker.status.last_sync is not None)
    await env.worker.stop()


async def test_disabled_or_blocked_camera_never_connects(env: Env) -> None:
    env.worker.update(env.worker.camera.model_copy(update={"enabled": False}))
    env.worker.start()
    await asyncio.sleep(0.1)
    assert env.cam.requests == []
    env.worker.update(env.worker.camera.model_copy(update={"enabled": True}))
    env.ctx.blocked = lambda cid: ["subnet_overlap"]
    await asyncio.sleep(0.1)
    assert env.cam.requests == [] and env.worker.status.blocked_reasons == ["subnet_overlap"]
    env.ctx.blocked = lambda cid: []
    await wait_for(lambda: env.worker.status.last_sync is not None)
    await env.worker.stop()


async def test_arrival_hint_polls_faster(env: Env) -> None:
    assert env.worker._poll_interval() == env.ctx.timing.poll
    env.worker.arrival_hint()
    assert env.worker._poll_interval() == env.ctx.timing.hint_poll
    await asyncio.sleep(0.25)
    assert env.worker._poll_interval() == env.ctx.timing.poll
    env.worker.publish_status()
    assert env.worker.status.arrival_hint is False


async def test_status_events_are_published(env: Env) -> None:
    queue = env.bus.subscribe()
    await env.session()
    types = []
    while not queue.empty():
        types.append(queue.get_nowait())
    states = {e["status"]["sync_status"] for e in types if e["type"] == "camera"}
    assert {"detecting", "preparing", "listing", "downloading", "restoring", "idle"} <= states
    assert any(e["type"] == "session" and e["result"] == "sync_completed" for e in types)
    assert dt.datetime.fromisoformat(env.worker.status.last_sync or "").tzinfo is not None
    with contextlib.suppress(Exception):
        env.bus.unsubscribe(queue)


async def test_last_sync_and_last_event_survive_a_restart(env: Env) -> None:
    await env.session()
    original = env.worker.status
    assert original.last_sync
    reborn = CameraWorker(env.ctx, env.worker.camera)  # a fresh worker, as after an app restart
    assert (
        reborn.status.last_event_clip == "2026-11-22T09:16:00+00:00"
        or reborn.status.last_event_clip
    )
    assert reborn.status.last_event_clip.startswith("2026-11-22")  # type: ignore[union-attr]
    assert reborn.status.last_sync is not None
    # A camera that never synced has nothing to restore.
    other = CameraWorker(env.ctx, env.worker.camera.model_copy(update={"id": "other"}))
    assert other.status.last_sync is None and other.status.last_event_clip is None
