"""Per-camera sync state machine.

Assumes the camera can vanish at any second: every step can resume, and the
camera is always put back into recording (``restore()`` runs from a ``finally``
block, and a persisted flag makes the next contact restore first).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

from .catalog import TARGET_LOCAL, Catalog, ClipRow
from .detect import Detection, candidate_hosts, detect
from .drivers import DRIVER_BY_NAME, BaseDriver
from .drivers.http import CameraHttp
from .errors import (
    CameraError,
    LinkLost,
    ProtocolError,
    StorageError,
    StorageFull,
    UnsupportedCamera,
)
from .events import EventBus
from .models import CHANNEL_ORDER, Kind, Recording
from .options import CameraOptions, SyncMode, SyncOptions
from .postprocess import PostProcessor
from .restoreflag import RestoreFlags
from .storage import PARTIAL_DIR, Destination, StorageManager

_LOGGER = logging.getLogger(__name__)

CHECKPOINT_BYTES = 4 * 1024 * 1024
SPEED_WINDOW = 1.0
MAX_CONSECUTIVE_FILE_FAILURES = 3
LIST_FAILURES_BEFORE_REPAIR = 3

# Values of the "Sync status" sensor enum.
IDLE, DETECTING, PREPARING, LISTING, DOWNLOADING, RESTORING, ERROR = (
    "idle",
    "detecting",
    "preparing",
    "listing",
    "downloading",
    "restoring",
    "error",
)


@dataclass(slots=True)
class Timing:
    """Intervals in seconds; tests shrink them."""

    poll: float = 15.0
    hint_poll: float = 3.0
    hint_duration: float = 300.0
    backoff: tuple[float, ...] = (5.0, 15.0, 60.0)
    restore_timeout: float = 20.0


# ----------------------------------------------------------------- queueing


def build_queue(
    recs: list[Recording],
    mode: SyncMode,
    recent_minutes: int,
) -> list[Recording]:
    """Order the clips to fetch.

    Events newest first, then parking, then normal clips from the last
    ``recent_minutes`` (measured against the newest clip on the camera, so a
    wrong camera clock does not matter), then the remaining normal clips
    newest first, then photos. Front/rear files with the same start time stay
    adjacent so a pair is never left half-saved.

    ``events_only`` stops after events and parking; ``events_first`` also
    takes the recent window; ``everything`` drains the whole queue.
    """
    normal = [r for r in recs if r.kind is Kind.NORMAL]
    newest = max((r.start for r in normal), default=None)
    window_start = newest - dt.timedelta(minutes=recent_minutes) if newest is not None else None

    def bucket(rec: Recording) -> int:
        if rec.kind is Kind.EVENT:
            return 0
        if rec.kind is Kind.PARKING:
            return 1
        if rec.kind is Kind.NORMAL:
            return 2 if window_start is not None and rec.start >= window_start else 3
        return 4

    limit = {"events_only": 1, "events_first": 2, "everything": 4}[mode]
    chosen = [r for r in recs if bucket(r) <= limit]
    chosen.sort(key=lambda r: (bucket(r), -r.start.timestamp(), CHANNEL_ORDER[r.channel], r.path))
    return chosen


# ------------------------------------------------------------------- status


@dataclass(slots=True)
class CameraStatus:
    id: str
    name: str
    enabled: bool = True
    connected: bool = False
    sync_status: str = IDLE
    driver: str | None = None
    model: str | None = None
    firmware: str | None = None
    clips_remaining: int = 0
    bytes_remaining: int = 0
    speed_bps: float = 0.0
    eta_seconds: float | None = None
    last_sync: str | None = None
    last_event_clip: str | None = None
    sd_free_bytes: int | None = None
    restore_pending: bool = False
    unsupported: bool = False
    blocked_reasons: list[str] = field(default_factory=list)
    error: str | None = None
    live_url: str | None = None
    capabilities: dict[str, bool] = field(default_factory=dict)
    arrival_hint: bool = False
    current_file: str | None = None
    sync_mode: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "connected": self.connected,
            "sync_status": self.sync_status,
            "driver": self.driver,
            "model": self.model,
            "firmware": self.firmware,
            "clips_remaining": self.clips_remaining,
            "bytes_remaining": self.bytes_remaining,
            "speed_bps": round(self.speed_bps),
            "eta_seconds": None if self.eta_seconds is None else round(self.eta_seconds),
            "last_sync": self.last_sync,
            "last_event_clip": self.last_event_clip,
            "sd_free_bytes": self.sd_free_bytes,
            "restore_pending": self.restore_pending,
            "unsupported": self.unsupported,
            "blocked_reasons": self.blocked_reasons,
            "error": self.error,
            "live_url": self.live_url,
            "capabilities": self.capabilities,
            "arrival_hint": self.arrival_hint,
            "current_file": self.current_file,
            "sync_mode": self.sync_mode,
        }


@dataclass(slots=True)
class SyncContext:
    """Everything a worker needs; built once by the daemon (or a test)."""

    session: aiohttp.ClientSession
    catalog: Catalog
    storage: Callable[[], StorageManager]
    bus: EventBus
    flags: RestoreFlags
    post: PostProcessor
    sync_options: Callable[[], SyncOptions]
    link_up: Callable[[CameraOptions], Awaitable[bool]]
    blocked: Callable[[str], list[str]] = lambda _id: []
    gateway: Callable[[CameraOptions], str | None] = lambda _cam: None
    after_session: Callable[[], Awaitable[None]] | None = None
    timing: Timing = field(default_factory=Timing)
    now: Callable[[], float] = time.time


@dataclass(slots=True)
class SessionStats:
    clips: int = 0
    bytes: int = 0
    failures: int = 0
    result: str = "ok"
    error: str | None = None


class CameraWorker:
    """Owns one camera: link watching, sessions, backoff, restore."""

    def __init__(self, ctx: SyncContext, camera: CameraOptions) -> None:
        self.ctx = ctx
        self.camera = camera
        self.status = CameraStatus(id=camera.id, name=camera.display_name, enabled=camera.enabled)
        self._driver: BaseDriver | None = None
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._manual = False
        self._restore_requested = False
        self._hint_until = 0.0
        self._stopping = False
        self._failures = 0
        self._need_relink = False
        self._synced_this_link = False
        self._remaining_base = 0
        self.status.restore_pending = ctx.flags.get(camera.id) is not None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name=f"camera-{self.camera.id}")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def update(self, camera: CameraOptions) -> None:
        """Hot reload of this camera's options."""
        self.camera = camera
        self.status.name = camera.display_name
        self.status.enabled = camera.enabled
        self._publish_status()  # sync_mode / enabled changes reach subscribers now
        self._wake.set()

    def request_sync(self) -> None:
        self._manual = True
        self._wake.set()

    def request_restore(self) -> None:
        self._restore_requested = True
        self._wake.set()

    def arrival_hint(self) -> None:
        """Probe every 3 s for 5 minutes instead of every 15 s."""
        self._hint_until = self.ctx.now() + self.ctx.timing.hint_duration
        self._wake.set()

    # ------------------------------------------------------------- plumbing

    def _publish_status(self) -> None:
        self.status.arrival_hint = self.ctx.now() < self._hint_until
        self.status.sync_mode = self.camera.sync_mode or self.ctx.sync_options().mode
        self.ctx.bus.publish("camera", camera=self.camera.id, status=self.status.as_dict())

    def _set(self, state: str) -> None:
        if self.status.sync_status != state:
            self.status.sync_status = state
            self._publish_status()

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake early for manual requests and option changes."""
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), seconds)

    def _poll_interval(self) -> float:
        timing = self.ctx.timing
        return timing.hint_poll if self.ctx.now() < self._hint_until else timing.poll

    # ----------------------------------------------------------------- main

    async def run(self) -> None:
        try:
            while not self._stopping:
                await self._tick()
        except asyncio.CancelledError:
            raise

    async def _tick(self) -> None:
        cam = self.camera
        blocked = self.ctx.blocked(cam.id)
        if blocked != self.status.blocked_reasons:
            self.status.blocked_reasons = blocked
            self._publish_status()
        if not cam.enabled or blocked:
            self._set_connected(False)
            self._manual = False
            await self._sleep(self._poll_interval())
            return

        up = await self.ctx.link_up(cam)
        if not up:
            self._set_connected(False)
            self._need_relink = False
            self._failures = 0
            await self._sleep(self._poll_interval())
            return

        self._set_connected(True)
        if self._need_relink:
            # Backoff exhausted: wait for the link to drop and come back.
            await self._sleep(self._poll_interval())
            return

        manual, self._manual = self._manual, False
        if self._restore_requested:
            self._restore_requested = False
            await self._standalone_restore()
            return
        if self._should_sync(manual):
            result = await self._run_session()
            await self._after_session(result)
        else:
            await self._sleep(self._poll_interval())

    def _should_sync(self, manual: bool) -> bool:
        """A session runs on each link-up edge, or on demand."""
        if manual:
            return True
        return not self._synced_this_link

    def _set_connected(self, connected: bool) -> None:
        if connected == self.status.connected:
            return
        self.status.connected = connected
        if not connected:
            self._synced_this_link = False
            self.status.sync_status = IDLE if self.status.sync_status != ERROR else ERROR
        self._publish_status()

    async def _after_session(self, result: SessionStats) -> None:
        if result.result in ("ok", "unsupported", "storage_full", "partial"):
            self._synced_this_link = True
            self._failures = 0
            return
        # Transient failure: back off 5 s, 15 s, 60 s, then wait for a new link-up.
        backoff = self.ctx.timing.backoff
        if self._failures < len(backoff):
            delay = backoff[self._failures]
            self._failures += 1
            await self._sleep(delay)
        else:
            self._need_relink = True
            self._synced_this_link = True

    # ------------------------------------------------------------- restore

    async def _standalone_restore(self) -> None:
        """The Restore recording button: detect if needed, then restore()."""
        try:
            driver = await self._detect()
            await self._restore(driver)
        except (CameraError, UnsupportedCamera) as err:
            self.status.error = str(err)
            self._set(ERROR)
        else:
            self._set(IDLE)
        finally:
            await self._close_driver()

    async def _restore(self, driver: BaseDriver) -> None:
        """Always safe to call; clears the persistent flag on success."""
        self._set(RESTORING)
        try:
            await asyncio.wait_for(driver.restore(), self.ctx.timing.restore_timeout)
        except (CameraError, TimeoutError, OSError) as err:
            self.status.restore_pending = True
            self.status.error = f"restore failed: {err}"
            self._publish_status()
            raise
        self.ctx.flags.clear(self.camera.id)
        self.status.restore_pending = False
        if self.status.error and self.status.error.startswith("restore failed"):
            self.status.error = None
        self._publish_status()

    async def _restore_in_finally(self, driver: BaseDriver) -> None:
        """Restore that survives cancellation and swallows camera errors."""
        task = asyncio.ensure_future(self._restore(driver))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(task, self.ctx.timing.restore_timeout)
            raise
        except (CameraError, TimeoutError, OSError) as err:
            _LOGGER.warning(
                "%s: restore failed, will retry on next contact: %s", self.camera.id, err
            )

    # ----------------------------------------------------------- detection

    async def _detect(self) -> BaseDriver:
        self._set(DETECTING)
        cam = self.camera
        cached = self.ctx.catalog.kv_get(f"driver:{cam.id}")
        hosts = candidate_hosts(
            cam.host, self.ctx.gateway(cam), None if cam.driver == "auto" else [cam.driver]
        )
        flag = self.ctx.flags.get(cam.id)
        if flag and not cached:
            cached = {"driver": flag.get("driver"), "host": flag.get("host")}
        try:
            detection: Detection = await detect(
                self.ctx.session, hosts, forced=cam.driver, cached=cached
            )
        except UnsupportedCamera:
            self.status.unsupported = True
            self._publish_status()
            raise
        self.status.unsupported = False
        probe = detection.probe
        self.ctx.catalog.kv_set(f"driver:{cam.id}", {"driver": probe.driver, "host": probe.host})
        self.status.driver = probe.driver
        self.status.model = probe.model or self.status.model
        self.status.firmware = probe.firmware or self.status.firmware
        self.status.capabilities = detection.driver.capabilities.as_dict()
        self.status.live_url = detection.driver.live_url()
        self._driver = detection.driver
        return detection.driver

    async def _close_driver(self) -> None:
        driver, self._driver = self._driver, None
        if driver is not None:
            with contextlib.suppress(Exception):
                await driver.close()

    # ------------------------------------------------------------- session

    async def _run_session(self) -> SessionStats:
        cam = self.camera
        stats = SessionStats()
        catalog = self.ctx.catalog
        session_id = catalog.start_session(cam.id)
        driver: BaseDriver | None = None
        needs_restore = False
        self.status.error = None
        try:
            driver = await self._detect()
            if self.ctx.flags.get(cam.id) is not None:
                # A previous visit died in playback mode: fix that first.
                await self._restore(driver)
            info = await driver.info()
            self.status.sd_free_bytes = info.free_bytes
            if info.firmware:
                self.status.firmware = info.firmware
            self.ctx.flags.set(cam.id, driver.name, driver.host)
            self.status.restore_pending = True
            needs_restore = True
            self._set(PREPARING)
            await driver.prepare()
            self._set(LISTING)
            try:
                recs = await driver.list_recordings()
            except ProtocolError as err:
                stats.result, stats.error = "list_failed", str(err)
                raise
            await self._download_all(driver, recs, stats)
        except UnsupportedCamera as err:
            stats.result, stats.error = "unsupported", str(err)
        except LinkLost as err:
            stats.result, stats.error = "link_lost", str(err)
            _LOGGER.info("%s: link lost: %s", cam.id, err)
        except StorageFull as err:
            stats.result, stats.error = "storage_full", str(err)
        except CameraError as err:
            if stats.result == "ok":
                stats.result = "error"
            stats.error = stats.error or str(err)
            _LOGGER.warning("%s: session failed: %s", cam.id, err)
        except asyncio.CancelledError:
            stats.result = "cancelled"
            raise
        finally:
            if driver is not None and needs_restore:
                await self._restore_in_finally(driver)
            await self._close_driver()
            self._finish(session_id, driver, stats)
        if self.ctx.after_session is not None and stats.clips:
            with contextlib.suppress(Exception):
                await self.ctx.after_session()
        return stats

    def _finish(self, session_id: int, driver: BaseDriver | None, stats: SessionStats) -> None:
        self.ctx.catalog.end_session(
            session_id, stats.result, stats.clips, stats.bytes, stats.error
        )
        status = self.status
        status.current_file = None
        status.speed_bps = 0.0 if stats.result != "ok" else status.speed_bps
        ok = stats.result in ("ok", "partial")
        if ok:
            status.last_sync = dt.datetime.now(dt.UTC).isoformat()
            self._set(IDLE)
            self.ctx.bus.publish(
                "session", camera=self.camera.id, result="sync_completed",
                clips=stats.clips, bytes=stats.bytes,
            )  # fmt: skip
        else:
            status.error = stats.error
            self._set(ERROR if stats.result != "link_lost" else IDLE)
            self.ctx.bus.publish(
                "session", camera=self.camera.id, result="sync_failed",
                reason=stats.result, error=stats.error,
            )  # fmt: skip
        if stats.result == "list_failed" and (
            self.ctx.catalog.consecutive_sessions_with(self.camera.id, "list_failed")
            >= LIST_FAILURES_BEFORE_REPAIR
        ):
            self.ctx.bus.publish("issue", camera=self.camera.id, key="listing_keeps_failing")
        self._publish_status()

    # ------------------------------------------------------------ downloads

    async def _download_all(
        self, driver: BaseDriver, recs: list[Recording], stats: SessionStats
    ) -> None:
        sync = self.ctx.sync_options()
        mode: SyncMode = self.camera.sync_mode or sync.mode
        catalog = self.ctx.catalog
        pending: list[tuple[Recording, ClipRow]] = []
        for rec in build_queue(recs, mode, sync.recent_minutes):
            if catalog.is_known_done(self.camera.id, rec):
                continue
            row, _ = catalog.upsert_recording(self.camera.id, rec)
            pending.append((rec, row))

        self._remaining_base = sum(r.size for r, _ in pending)
        self.status.clips_remaining = len(pending)
        self.status.bytes_remaining = self._remaining_base
        self.status.eta_seconds = None
        self._set(DOWNLOADING)
        self._publish_status()

        failures_in_row = 0
        for rec, row in pending:
            if self._stopping:
                break
            self.status.current_file = rec.start.strftime("%Y%m%d_%H%M%S") + f"_{rec.channel.value}"
            try:
                await self._download_one(driver, rec, row, stats)
            except (LinkLost, StorageFull):
                raise
            except (CameraError, StorageError, OSError) as err:
                _LOGGER.warning("%s: %s failed: %s", self.camera.id, rec.path, err)
                catalog.mark_failed(row.id, str(err))
                stats.failures += 1
                failures_in_row += 1
                self._remaining_base = max(0, self._remaining_base - rec.size)
                self.status.clips_remaining = max(0, self.status.clips_remaining - 1)
                if failures_in_row >= MAX_CONSECUTIVE_FILE_FAILURES:
                    stats.result, stats.error = "error", str(err)
                    raise CameraError(str(err)) from err
                continue
            failures_in_row = 0
            self._remaining_base = max(0, self._remaining_base - rec.size)
            self.status.clips_remaining = max(0, self.status.clips_remaining - 1)
            self.status.bytes_remaining = self._remaining_base
            self._update_eta()
            self._publish_status()
        if stats.failures and stats.result == "ok":
            stats.result = "partial"
            stats.error = f"{stats.failures} clip(s) failed"

    def _update_eta(self) -> None:
        speed = self.status.speed_bps
        self.status.eta_seconds = self.status.bytes_remaining / speed if speed > 0 else None

    async def _download_one(
        self, driver: BaseDriver, rec: Recording, row: ClipRow, stats: SessionStats
    ) -> None:
        storage = self.ctx.storage()
        catalog = self.ctx.catalog
        dest = storage.primary_destination(rec.size)
        part = self._part_path(row, dest)
        part.parent.mkdir(parents=True, exist_ok=True)

        offset = min(row.offset, part.stat().st_size) if part.exists() else 0
        if offset > rec.size:
            offset = 0
        stream = await driver.open(rec, offset)
        try:
            if stream.start != offset:
                # The camera ignored Range and answered 200: start over.
                offset = stream.start
                self._note_range_ignored(driver)
            mode = "r+b" if part.exists() else "wb"
            with part.open(mode) as fh:
                fh.truncate(offset)
                fh.seek(offset)
                catalog.set_offset(row.id, offset, str(part))
                written = offset
                last_checkpoint = offset
                window_start = time.monotonic()
                window_bytes = 0
                try:
                    async for chunk in stream:
                        await asyncio.to_thread(fh.write, chunk)
                        written += len(chunk)
                        stats.bytes += len(chunk)
                        window_bytes += len(chunk)
                        self.status.bytes_remaining = max(0, self._remaining_base - written)
                        now = time.monotonic()
                        if now - window_start >= SPEED_WINDOW:
                            inst = window_bytes / (now - window_start)
                            old = self.status.speed_bps
                            self.status.speed_bps = inst if old == 0 else 0.7 * old + 0.3 * inst
                            self._update_eta()
                            self._publish_status()
                            window_start, window_bytes = now, 0
                        if written - last_checkpoint >= CHECKPOINT_BYTES:
                            fh.flush()
                            os.fsync(fh.fileno())
                            catalog.set_offset(row.id, written)
                            last_checkpoint = written
                finally:
                    # On a dropped link too: record how far we got so the next
                    # visit resumes from here instead of the last checkpoint.
                    fh.flush()
                    os.fsync(fh.fileno())
                    catalog.set_offset(row.id, written)
        except BaseException:
            # Keep the partial file and its offset: the next visit resumes it.
            with contextlib.suppress(Exception):
                await stream.aclose()
            raise
        await stream.aclose()

        size = part.stat().st_size
        expected_total = (
            stream.start + stream.content_length if stream.content_length is not None else None
        )
        if rec.size and size != rec.size:
            self._discard(part, row)
            raise ProtocolError(f"size {size} does not match listing {rec.size}")
        if expected_total is not None and size != expected_total:
            self._discard(part, row)
            raise ProtocolError(f"size {size} does not match Content-Length {expected_total}")

        digest = await asyncio.to_thread(_sha256, part)
        final = storage.place(self.camera.id, rec, part, dest)
        catalog.complete_clip(
            row.id, sha256=digest, target=dest.kind, path=str(final), size=final.stat().st_size
        )
        stats.clips += 1
        self.ctx.post.submit(row.id, final)
        storage.queue_network_copy(row.id)
        await self._delete_from_camera(driver, rec)

    def _part_path(self, row: ClipRow, dest: Destination) -> Path:
        if row.part_path and Path(row.part_path).exists():
            return Path(row.part_path)
        return dest.target.root / PARTIAL_DIR / f"{row.id}.part"

    def _discard(self, part: Path, row: ClipRow) -> None:
        part.unlink(missing_ok=True)
        self.ctx.catalog.reset_clip(row.id)

    def _note_range_ignored(self, driver: BaseDriver) -> None:
        self.ctx.catalog.kv_set(f"range_ignored:{self.camera.id}", True)
        _LOGGER.info("%s: camera ignored Range, restarting file", self.camera.id)

    async def _delete_from_camera(self, driver: BaseDriver, rec: Recording) -> None:
        """Only verified files, never locked ones, events only with their own switch."""
        sync = self.ctx.sync_options()
        if not sync.delete_from_camera or not driver.capabilities.delete:
            return
        if rec.kind is Kind.EVENT:
            if not sync.delete_events_from_camera:
                return
        elif rec.locked:
            return  # a user-protected clip that is not an event stays on the card
        try:
            await driver.delete(rec)
        except CameraError as err:
            _LOGGER.warning("%s: could not delete %s: %s", self.camera.id, rec.path, err)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


__all__ = [
    "DRIVER_BY_NAME",
    "TARGET_LOCAL",
    "CameraHttp",
    "CameraStatus",
    "CameraWorker",
    "SessionStats",
    "SyncContext",
    "Timing",
    "build_queue",
]
