from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import at
from dashcam_sync.catalog import TARGET_LOCAL, Catalog
from dashcam_sync.models import Kind, Recording
from dashcam_sync.options import StorageOptions, SyncOptions
from dashcam_sync.postprocess import (
    PostProcessor,
    ffmpeg_remux_args,
    ffmpeg_thumb_args,
    ffprobe_args,
    run_tool,
)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def make_ts(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
         "-c:v", "mpeg2video", "-f", "mpegts", "-y", str(path)],
        check=True,
    )  # fmt: skip


def setup(tmp_path: Path, catalog: Catalog, name: str, ext: str):
    rec = Recording(f"/x/{name}.{ext}", 1, at(10), Kind.EVENT)
    row, _ = catalog.upsert_recording("car", rec)
    path = tmp_path / f"clip.{ext}"
    return row, path


async def test_ts_is_remuxed_thumbnailed_and_described(tmp_path: Path, catalog: Catalog) -> None:
    row, path = setup(tmp_path, catalog, "a", "ts")
    make_ts(path)
    catalog.complete_clip(
        row.id, sha256="abc", target=TARGET_LOCAL, path=str(path), size=path.stat().st_size
    )
    done: list[Path] = []

    async def on_done(clip, final):  # type: ignore[no-untyped-def]
        done.append(final)

    post = PostProcessor(catalog, lambda: SyncOptions(), lambda: StorageOptions(), on_done)
    post.submit(row.id, path)
    await post.drain()
    await post.stop()

    mp4 = path.with_suffix(".mp4")
    assert done == [mp4] and mp4.exists() and not path.exists()  # original removed
    assert mp4.with_suffix(".jpg").stat().st_size > 0
    meta = json.loads(mp4.with_suffix(".json").read_text())
    assert meta["camera"] == "car" and meta["kind"] == "event" and meta["sha256"] == "abc"
    assert meta["source_path"] == "/x/a.ts" and 1.5 < meta["duration"] < 2.6
    copy = catalog.copy(row.id, TARGET_LOCAL)
    assert copy is not None and copy.path == str(mp4)
    clip = catalog.get_clip(row.id)
    assert clip is not None and clip.post_status == "done" and clip.duration


async def test_keep_original_and_options_off(tmp_path: Path, catalog: Catalog) -> None:
    row, path = setup(tmp_path, catalog, "b", "ts")
    make_ts(path)
    catalog.complete_clip(row.id, sha256="x", target=TARGET_LOCAL, path=str(path), size=1)
    post = PostProcessor(
        catalog,
        lambda: SyncOptions(remux_to_mp4=True, thumbnails=False),
        lambda: StorageOptions(keep_original=True),
    )
    result = await post.process(catalog.get_clip(row.id), path)  # type: ignore[arg-type]
    assert path.exists() and result.path.suffix == ".mp4" and result.thumbnail is None

    row2, path2 = setup(tmp_path / "..", catalog, "c", "ts")
    path2 = tmp_path / "other.ts"
    make_ts(path2)
    catalog.complete_clip(row2.id, sha256="x", target=TARGET_LOCAL, path=str(path2), size=1)
    off = PostProcessor(
        catalog, lambda: SyncOptions(remux_to_mp4=False, thumbnails=False), lambda: StorageOptions()
    )
    res = await off.process(catalog.get_clip(row2.id), path2)  # type: ignore[arg-type]
    assert res.path == path2 and path2.exists() and not res.remuxed


async def test_mp4_photo_and_corrupt_input(tmp_path: Path, catalog: Catalog) -> None:
    # A corrupt .ts keeps the original and still gets a sidecar; it never raises.
    row, path = setup(tmp_path, catalog, "d", "ts")
    path.write_bytes(b"not a video at all" * 100)
    catalog.complete_clip(row.id, sha256="x", target=TARGET_LOCAL, path=str(path), size=1)
    post = PostProcessor(catalog, lambda: SyncOptions(), lambda: StorageOptions())
    result = await post.process(catalog.get_clip(row.id), path)  # type: ignore[arg-type]
    assert result.path == path and not result.remuxed and result.thumbnail is None
    assert path.with_suffix(".json").exists()

    jpg_row, jpg = setup(tmp_path, catalog, "p", "jpg")
    jpg.write_bytes(b"\xff\xd8\xff")
    catalog.complete_clip(jpg_row.id, sha256="x", target=TARGET_LOCAL, path=str(jpg), size=3)
    res = await post.process(catalog.get_clip(jpg_row.id), jpg)  # type: ignore[arg-type]
    assert res.thumbnail is None and res.path == jpg


async def test_worker_survives_failure_and_missing_files(tmp_path: Path, catalog: Catalog) -> None:
    row, path = setup(tmp_path, catalog, "e", "mp4")
    catalog.complete_clip(row.id, sha256="x", target=TARGET_LOCAL, path=str(path), size=1)
    calls: list[Path] = []

    async def on_done(clip, final):  # type: ignore[no-untyped-def]
        calls.append(final)

    post = PostProcessor(catalog, lambda: SyncOptions(), lambda: StorageOptions(), on_done)
    post.submit(row.id, tmp_path / "missing.mp4")  # file gone: skipped
    post.submit(9999, path)  # unknown clip: skipped
    path.write_bytes(b"data")
    post.submit(row.id, path)  # junk mp4: processed, ffmpeg fails quietly
    await post.drain()
    await post.stop()
    assert calls == [path]


def test_ffmpeg_args_restrict_protocols_and_never_use_the_shell() -> None:
    for args in (
        ffmpeg_remux_args(Path("a.ts"), Path("a.mp4")),
        ffmpeg_thumb_args(Path("a.mp4"), Path("a.jpg")),
        ffprobe_args(Path("a.mp4")),
    ):
        i = args.index("-protocol_whitelist")
        assert args[i + 1] == "file"
    assert "-nostdin" in ffmpeg_remux_args(Path("a"), Path("b"))


async def test_run_tool_handles_missing_binary_and_timeout() -> None:
    assert (await run_tool(["definitely-not-a-binary"]))[0] == 127
    code, _ = await run_tool(["sleep", "5"], timeout=0.2)
    assert code == 124
    code, out = await run_tool(["echo", "hi"])
    assert code == 0 and out.strip() == "hi"
