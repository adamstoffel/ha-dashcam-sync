"""Regression tests for defects found by an independent code review."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from pathlib import Path

import pytest

from conftest import at
from dashcam_sync.catalog import (
    TARGET_LOCAL,
    TARGET_NETWORK,
    TARGET_SPOOL,
    Catalog,
    ClipRow,
    CopyRow,
)
from dashcam_sync.errors import ConfigError
from dashcam_sync.hostcheck import validate_camera_host
from dashcam_sync.models import Channel, Kind, Recording
from dashcam_sync.options import RetentionOptions, StorageOptions
from dashcam_sync.postprocess import PostProcessor
from dashcam_sync.retention import RetentionService, Rules, plan_purge, reference_time
from dashcam_sync.storage import StorageManager
from test_daemon_api import TOKEN, Stack, stack, synced, wait_for  # noqa: F401
from test_engine import Env, env, make_env  # noqa: F401

NOW = dt.datetime(2026, 12, 25, 12, 0, tzinfo=dt.UTC)


# ---- retention must not trust a wrong camera clock --------------------------------------------


def clip(start: dt.datetime, done: dt.datetime, kind: Kind = Kind.EVENT, size: int = 100):  # type: ignore[no-untyped-def]
    row = ClipRow(1, "c", "/p", size, start, kind, Channel.FRONT, False, None, "done", size, None,
                  "x", False, "ok", None, done.timestamp(), done.timestamp())  # fmt: skip
    return row, CopyRow(1, TARGET_LOCAL, "/f", size, "present", 0.0)


def test_a_camera_with_a_reset_clock_does_not_get_fresh_clips_purged() -> None:
    just_downloaded = NOW - dt.timedelta(minutes=5)
    for wrong_start in (
        dt.datetime(1970, 1, 1, tzinfo=dt.UTC),
        dt.datetime(2014, 1, 1, tzinfo=dt.UTC),
    ):
        item = clip(wrong_start, just_downloaded, Kind.NORMAL)
        assert reference_time(item[0]) == just_downloaded
        rules = Rules(
            normal_days=14,
            parking_days=30,
            event_days=7,
            photo_days=90,
            max_total_gb=0,
            min_free_percent=0,
        )
        assert plan_purge([item], rules, now=NOW, free_bytes=10**12, total_bytes=10**12) == []
    # Once it really is old (by download time) it does expire.
    old = clip(dt.datetime(1970, 1, 1, tzinfo=dt.UTC), NOW - dt.timedelta(days=20), Kind.NORMAL)
    assert len(plan_purge([old], rules, now=NOW, free_bytes=10**12, total_bytes=10**12)) == 1


def test_a_clock_in_the_future_is_not_trusted_to_keep_clips_forever_either() -> None:
    future = clip(NOW + dt.timedelta(days=400), NOW - dt.timedelta(days=30), Kind.NORMAL)
    rules = Rules(14, 30, 0, 90, 0, 0)
    # Its own (future) timestamp wins, conservatively: it is kept rather than deleted early.
    assert plan_purge([future], rules, now=NOW, free_bytes=10**12, total_bytes=10**12) == []


# ---- deleting a clip must not bring it back -----------------------------------------------------


async def test_a_deleted_clip_is_not_downloaded_again(env: Env) -> None:
    await env.session()
    clips = env.catalog.list_clips(camera="car")
    assert len(clips) == 2
    svc = RetentionService(
        env.catalog, env.storage, RetentionOptions(min_free_percent=0, max_total_gb=0)
    )
    for c in clips:
        svc.delete_clip_everywhere(c.id)
    assert env.catalog.list_clips(camera="car") == []
    env.cam.requests_files.clear()  # type: ignore[attr-defined]
    stats = await env.session()  # the camera still lists both files on its card
    assert stats.clips == 0 and env.cam.requests_files == []  # type: ignore[attr-defined]
    assert env.catalog.tombstones("car") == 2


async def test_a_purged_clip_is_not_downloaded_again(env: Env) -> None:
    await env.session()
    svc = RetentionService(
        env.catalog,
        env.storage,
        RetentionOptions(normal_days=1, event_days=1, min_free_percent=0, max_total_gb=0),
    )
    later = dt.datetime.now(dt.UTC) + dt.timedelta(days=30)
    report = svc.run(now=later)
    assert len(report.deleted) == 2
    env.cam.requests_files.clear()  # type: ignore[attr-defined]
    assert (await env.session()).clips == 0


# ---- the engine worker must survive anything --------------------------------------------------


async def test_unexpected_errors_are_failed_sessions_not_ok_ones(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(self) -> None:  # type: ignore[no-untyped-def]
        raise OverflowError("driver bug")

    monkeypatch.setattr(type(env.worker), "_run_session_body_probe", boom, raising=False)
    from dashcam_sync.drivers.novatek import NovatekDriver

    async def bad_listing(self) -> list:  # type: ignore[no-untyped-def]
        raise OverflowError("parser bug")

    monkeypatch.setattr(NovatekDriver, "list_recordings", bad_listing)
    stats = await env.session()
    assert stats.result == "error" and "OverflowError" in (stats.error or "")
    assert env.cam.recording  # and the camera was still restored
    assert env.catalog.recent_sessions(1)[0]["result"] == "error"


async def test_the_worker_task_survives_a_crashing_tick(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    real = type(env.worker)._tick

    async def flaky(self) -> None:  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("tick bug")
        await real(self)

    monkeypatch.setattr(type(env.worker), "_tick", flaky)
    env.worker.start()
    await wait_for(lambda: env.worker.status.last_sync is not None)
    assert calls["n"] >= 3  # it kept going after two crashes and then synced
    await env.worker.stop()


async def test_a_restore_timeout_is_a_failed_restore_not_a_dead_worker(env: Env) -> None:
    env.flags.set("car", "novatek", env.cam.host)
    env.ctx.timing.restore_timeout = 0.0001  # every restore() "times out"
    stats = await env.session()
    assert stats.result == "link_lost"  # treated like a lost link, retried by the backoff
    assert env.catalog.recent_sessions(1)[0]["result"] == "link_lost"
    assert env.flags.get("car") is not None and env.worker.status.restore_pending


async def test_restore_and_manual_sync_work_after_the_backoff_is_exhausted(env: Env) -> None:
    env.cam.faults.list_error = True
    env.worker.start()
    await wait_for(lambda: len(env.catalog.recent_sessions(50)) >= 3)
    await asyncio.sleep(0.15)
    assert env.worker._need_relink
    env.cam.recording = False
    env.worker.request_restore()
    await wait_for(lambda: env.cam.recording)  # honoured immediately, not at the next link-up
    env.cam.faults.list_error = False
    before = len(env.catalog.recent_sessions(50))
    env.worker.request_sync()
    await wait_for(lambda: len(env.catalog.recent_sessions(50)) > before)
    await wait_for(lambda: env.worker.status.last_sync is not None)
    await env.worker.stop()


# ---- shipping and post-processing ---------------------------------------------------------------


def manager(tmp_path: Path, catalog: Catalog, nas: Path) -> StorageManager:
    return StorageManager(
        catalog,
        StorageOptions(target="both", network_folder="nas"),
        media_root=tmp_path / "media",
        share_root=tmp_path / "share",
        min_free_percent=0,
        mountinfo=f"1 2 0:1 / {nas} rw - cifs a rw\n",
    )


def test_clips_still_being_post_processed_are_not_shipped(tmp_path: Path, catalog: Catalog) -> None:
    nas = tmp_path / "media" / "nas"
    nas.mkdir(parents=True)
    mgr = manager(tmp_path, catalog, nas)
    rec = Recording("/a.ts", 4, at(9), Kind.NORMAL)
    row, _ = catalog.upsert_recording("car", rec)
    path = mgr.local.root / "car" / "a.ts"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"raw!")
    catalog.complete_clip(row.id, sha256="s", target=TARGET_LOCAL, path=str(path), size=4)
    mgr.queue_network_copy(row.id)
    assert catalog.get_clip(row.id).post_status == "pending"  # type: ignore[union-attr]
    assert mgr.ship_pending() == 0 and not any(nas.rglob("*"))  # nothing raw goes to the share
    catalog.set_post_status(row.id, "done")
    assert mgr.ship_pending() == 1
    assert catalog.copy(row.id, TARGET_NETWORK) is not None


def test_spooled_clips_wait_for_post_processing_too(tmp_path: Path, catalog: Catalog) -> None:
    nas = tmp_path / "media" / "nas"
    nas.mkdir(parents=True)
    mgr = manager(tmp_path, catalog, nas)
    row, _ = catalog.upsert_recording("car", Recording("/a.ts", 4, at(9), Kind.NORMAL))
    spooled = mgr.spool.root / "car" / "a.ts"
    spooled.parent.mkdir(parents=True)
    spooled.write_bytes(b"raw!")
    catalog.complete_clip(row.id, sha256="s", target=TARGET_SPOOL, path=str(spooled), size=4)
    assert mgr.ship_pending() == 0 and spooled.exists()
    catalog.set_post_status(row.id, "failed")  # a failed remux must not block shipping forever
    assert mgr.ship_pending() == 1 and not spooled.exists()


def test_a_changed_local_folder_does_not_break_shipping(tmp_path: Path, catalog: Catalog) -> None:
    nas = tmp_path / "media" / "nas"
    nas.mkdir(parents=True)
    mgr = manager(tmp_path, catalog, nas)
    row, _ = catalog.upsert_recording("car", Recording("/a.mp4", 4, at(9), Kind.NORMAL))
    old_root = tmp_path / "media" / "old_folder" / "car"  # the user renamed local_folder since
    old_root.mkdir(parents=True)
    (old_root / "a.mp4").write_bytes(b"data")
    catalog.complete_clip(
        row.id, sha256="s", target=TARGET_LOCAL, path=str(old_root / "a.mp4"), size=4
    )
    catalog.set_post_status(row.id, "done")
    mgr.queue_network_copy(row.id)
    assert mgr.ship_pending() == 1  # used to raise ValueError and kill the ship loop
    assert (nas / "car" / "a.mp4").read_bytes() == b"data"


async def test_post_processing_survives_a_failing_callback_and_restarts(
    tmp_path: Path, catalog: Catalog
) -> None:
    from dashcam_sync.options import SyncOptions

    calls: list[int] = []

    async def on_done(clip_row, final) -> None:  # type: ignore[no-untyped-def]
        calls.append(clip_row.id)
        raise ValueError("callback bug")

    post = PostProcessor(
        catalog,
        lambda: SyncOptions(remux_to_mp4=False, thumbnails=False),
        lambda: StorageOptions(),
        on_done,
    )
    ids = []
    for i in range(2):
        row, _ = catalog.upsert_recording("car", Recording(f"/{i}.mp4", 4, at(9, i), Kind.NORMAL))
        path = tmp_path / f"{i}.mp4"
        path.write_bytes(b"data")
        catalog.complete_clip(row.id, sha256="s", target=TARGET_LOCAL, path=str(path), size=4)
        post.submit(row.id, path)
        ids.append(row.id)
    await post.drain()
    assert calls == ids  # the second clip was still processed after the first callback failed
    assert post._task is not None and not post._task.done()
    await post.stop()
    post.start()  # a finished task is restarted rather than left dead
    assert post._task is not None and not post._task.done()
    await post.stop()


async def test_interrupted_post_processing_resumes_on_startup(stack: Stack, tmp_path: Path) -> None:
    await synced(stack)
    clip_row = stack.daemon.cat.list_clips(camera="car")[0]
    stack.daemon.cat.set_post_status(clip_row.id, "pending")  # as if the app died mid-remux
    stack.daemon._resume_post_processing()
    await stack.daemon.post.drain()  # type: ignore[union-attr]
    assert stack.daemon.cat.get_clip(clip_row.id).post_status == "done"  # type: ignore[union-attr]


# ---- host validation cannot be smuggled around -------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "192.168.1.5:x@homeassistant:8123",  # userinfo hides the real host
        "user@192.168.1.5",
        "192.168.1.5:80/path", "192.168.1.5#frag", "192.168.1.5?q=1", "192.168.1.5\\evil",
        "192.168.1.5 evil", "192.168.1.5:99999", "192.168.1.5:0", "192.168.1.5:ab",
        "[::ffff:172.30.32.2]", "::ffff:172.30.32.2", "[::ffff:8.8.8.8]", "[fe80::1", "[fe80::1]:x",
    ],
)  # fmt: skip
async def test_smuggled_or_mapped_hosts_are_refused(host: str) -> None:
    with pytest.raises(ConfigError) as err:
        await validate_camera_host(host)
    # Refused by the parser or the address rules, never "by luck" because DNS failed.
    assert "resolve" not in str(err.value)


# ---- PATCH validates, and blocking probes stay off the event loop ------------------------------


async def test_patch_with_a_bad_value_is_refused_and_the_camera_survives(stack: Stack) -> None:
    for body in ({"sync_mode": "all"}, {"enabled": "yes please"}, {"name": {"a": 1}}, {"name": 5}):
        async with stack.http.patch(
            f"{stack.api}/api/v1/cameras/car", headers=stack.h(), json=body
        ) as r:
            assert r.status == 400, body
    assert "car" in stack.daemon.workers  # still there, options untouched
    saved = json.loads(stack.options_path.read_text())
    assert saved["cameras"][0].get("sync_mode") is None and saved["cameras"][0]["enabled"] is True


async def test_a_hung_share_does_not_freeze_the_api(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dashcam_sync.storage import Target

    def hung(self, catalog=None):  # type: ignore[no-untyped-def]
        time.sleep(1.0)  # a CIFS mount that stopped answering
        raise OSError("stale")

    monkeypatch.setattr(Target, "check", hung)
    started = time.monotonic()
    slow = asyncio.create_task(stack.get("/api/v1/status"))
    await asyncio.sleep(0.1)
    # The event loop is free: a cheap request completes while the status call is stuck in its thread.
    async with stack.http.get(f"{stack.api}/healthz") as r:
        assert r.status == 200
    assert time.monotonic() - started < 0.8
    slow.cancel()
    with pytest.raises((asyncio.CancelledError, Exception)):
        await slow
