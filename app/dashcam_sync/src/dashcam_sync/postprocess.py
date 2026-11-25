"""Low-priority post-processing: remux, thumbnail, sidecar.

ffmpeg parses files made by a camera, so it is treated as exposed to hostile
input: only the ``file`` protocol is allowed, every run has a 120 s timeout,
and the child gets CPU and memory limits and the lowest scheduling priority.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import resource
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from .catalog import TARGET_LOCAL, TARGET_NETWORK, Catalog, ClipRow
from .options import StorageOptions, SyncOptions

_LOGGER = logging.getLogger(__name__)

TOOL_TIMEOUT = 120.0
CPU_SECONDS = 150
MEMORY_BYTES = 2 * 1024**3


def _limits() -> None:  # pragma: no cover - runs in the forked child
    with contextlib.suppress(OSError, ValueError):
        os.nice(15)
    with contextlib.suppress(OSError, ValueError):
        resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    with contextlib.suppress(OSError, ValueError):
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))


async def run_tool(args: list[str], timeout: float = TOOL_TIMEOUT) -> tuple[int, str]:
    """Run ffmpeg/ffprobe with limits. Returns (exit code, stdout)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            preexec_fn=_limits,
        )
    except OSError as err:
        _LOGGER.warning("Could not start %s: %s", args[0], err)
        return 127, ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        _LOGGER.warning("%s timed out after %.0f s", args[0], timeout)
        return 124, ""
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


def ffmpeg_remux_args(src: Path, dst: Path) -> list[str]:
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-protocol_whitelist", "file",
        "-i", str(src),
        "-c", "copy", "-movflags", "+faststart",
        "-y", str(dst),
    ]  # fmt: skip


def ffmpeg_thumb_args(src: Path, dst: Path) -> list[str]:
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-protocol_whitelist", "file",
        "-ss", "1", "-i", str(src),
        "-frames:v", "1", "-vf", "scale=320:-2",
        "-y", str(dst),
    ]  # fmt: skip


def ffprobe_args(src: Path) -> list[str]:
    return [
        "ffprobe", "-v", "error", "-protocol_whitelist", "file",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(src),
    ]  # fmt: skip


@dataclass(slots=True)
class PostResult:
    path: Path
    duration: float | None
    thumbnail: Path | None
    remuxed: bool


class PostProcessor:
    """Background worker. Jobs run one at a time and never slow a download."""

    def __init__(
        self,
        catalog: Catalog,
        sync: Callable[[], SyncOptions],
        storage_opts: Callable[[], StorageOptions],
        on_done: Callable[[ClipRow, Path], Awaitable[None]] | None = None,
    ) -> None:
        self.catalog = catalog
        self._sync = sync
        self._storage_opts = storage_opts
        self._on_done = on_done
        self._queue: asyncio.Queue[tuple[int, Path] | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._worker(), name="postprocess")

    async def stop(self) -> None:
        if self._task is not None:
            await self._queue.put(None)
            await self._task
            self._task = None

    def submit(self, clip_id: int, path: Path) -> None:
        self._queue.put_nowait((clip_id, path))
        self.start()

    async def drain(self) -> None:
        """Wait until every queued job has finished (used by tests)."""
        await self._queue.join()

    async def _worker(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                if job is None:
                    return
                clip_id, path = job
                clip = self.catalog.get_clip(clip_id)
                if clip is None or not path.exists():
                    continue
                try:
                    result = await self.process(clip, path)
                except Exception:
                    _LOGGER.exception("Post-processing %s failed", path.name)
                    self.catalog.set_post_status(clip_id, "failed")
                    result_path = path
                else:
                    result_path = result.path
                if self._on_done is not None:
                    fresh = self.catalog.get_clip(clip_id)
                    if fresh is not None:
                        await self._on_done(fresh, result_path)
            finally:
                self._queue.task_done()

    async def process(self, clip: ClipRow, path: Path) -> PostResult:
        sync = self._sync()
        keep_original = self._storage_opts().keep_original
        final = path
        remuxed = False
        if sync.remux_to_mp4 and path.suffix.lower() == ".ts" and shutil.which("ffmpeg"):
            target = path.with_suffix(".mp4")
            tmp = path.with_name(f".{path.stem}.remux.mp4")
            code, _ = await run_tool(ffmpeg_remux_args(path, tmp))
            if code == 0 and tmp.exists() and tmp.stat().st_size > 0:
                os.replace(tmp, target)
                if not keep_original:
                    path.unlink(missing_ok=True)
                final = target
                remuxed = True
            else:
                tmp.unlink(missing_ok=True)
                _LOGGER.warning("Remux of %s failed (exit %s); keeping original", path.name, code)

        duration: float | None = None
        if shutil.which("ffprobe") and final.suffix.lower() in {".mp4", ".ts", ".mov", ".avi"}:
            code, out = await run_tool(ffprobe_args(final), 30)
            if code == 0:
                with contextlib.suppress(ValueError):
                    duration = float(out.strip())

        thumb: Path | None = None
        if sync.thumbnails and shutil.which("ffmpeg"):
            thumb = final.with_suffix(".jpg")
            if final.suffix.lower() in {".jpg", ".jpeg", ".png"}:
                thumb = None
            else:
                code, _ = await run_tool(ffmpeg_thumb_args(final, thumb))
                if code != 0 or not thumb.exists():
                    thumb = None

        self._write_sidecar(clip, final, duration)
        self._record(clip, path, final)
        self.catalog.set_post_status(clip.id, "done", duration)
        return PostResult(final, duration, thumb, remuxed)

    def _record(self, clip: ClipRow, old: Path, new: Path) -> None:
        """Point the primary copy at the final file."""
        for copy in self.catalog.copies(clip.id):
            if copy.path == str(old) and copy.target in (TARGET_LOCAL, TARGET_NETWORK, "spool"):
                self.catalog.add_copy(clip.id, copy.target, str(new), new.stat().st_size)

    def _write_sidecar(self, clip: ClipRow, final: Path, duration: float | None) -> None:
        meta = {
            "camera": clip.camera,
            "channel": clip.channel.value,
            "kind": clip.kind.value,
            "start": clip.start.isoformat(),
            "duration": duration,
            "size": final.stat().st_size,
            "sha256": clip.sha256,
            "source_path": clip.src_path,
        }
        sidecar = final.with_suffix(".json")
        tmp = sidecar.with_name(f".{sidecar.name}.tmp")
        tmp.write_text(json.dumps(meta, indent=2), "utf-8")
        os.replace(tmp, sidecar)
