#!/usr/bin/env python3
"""Run the whole app against a fake camera, no car or Home Assistant needed.

    python scripts/dev.py            # app panel on http://127.0.0.1:8099, API on :8100
    python scripts/dev.py --demo     # real (generated) video clips with thumbnails; needs ffmpeg
    DASHCAM_DEMO_FOOTAGE=dir python scripts/dev.py --demo   # clips cut from real dashcam videos in dir
                                     # (how docs/images were made; see docs/images/CREDITS.md)

The fake camera serves a few clips over a Novatek-style API. Edit
``.dev-data/options.json`` while it runs: the daemon hot-reloads it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app" / "dashcam_sync" / "src"))
sys.path.insert(0, str(ROOT / "app" / "tests"))

from dashcam_sync.daemon import Daemon, DaemonConfig
from dashcam_sync.engine import Timing
from fakecam import FakeCamera

DATA = Path(tempfile.gettempdir()) / "dashcam-demo"


def make_demo_video(source: str, seconds: int = 4) -> bytes:
    """A small real H.264 clip, so the panel can show thumbnails and play it."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "clip.mp4"
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"{source}=size=640x360:rate=15",
             "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
            check=True,
        )  # fmt: skip
        return out.read_bytes()


def cut_footage(path: Path, start: float, seconds: int = 6) -> bytes:
    """A clip cut from real dashcam footage (DASHCAM_DEMO_FOOTAGE), for realistic screenshots."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "clip.mp4"
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-ss", str(start), "-i", str(path), "-t", str(seconds),
             "-an", "-vf", "scale=1280:-2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
            check=True,
        )  # fmt: skip
        return out.read_bytes()


def footage_clips(folder: Path, count: int) -> list[bytes]:
    """``count`` clips spread across the videos in ``folder`` (any format ffmpeg reads)."""
    videos = sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith("."))
    if not videos:
        raise SystemExit(f"DASHCAM_DEMO_FOOTAGE: no videos in {folder}")
    clips = []
    for i in range(count):
        video = videos[i % len(videos)]
        length = float(
            subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                 str(video)],
                check=True, capture_output=True, text=True,
            ).stdout.strip() or 60
        )  # fmt: skip
        # Spread the cuts along each video, clear of its first and last seconds.
        share = (i // len(videos) + 1) / (count // len(videos) + 2)
        clips.append(cut_footage(video, max(0.0, length * share - 3)))
    return clips


async def main(demo: bool) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # The fake camera runs in this process; its access log is not something the app logs.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    cam = FakeCamera("novatek")
    host = await cam.start()
    sources = ("testsrc2", "smptebars", "mandelbrot")
    footage = os.environ.get("DASHCAM_DEMO_FOOTAGE")
    real = footage_clips(Path(footage), len(sources)) if demo and footage else None
    for i, (minute, folder, attr) in enumerate(
        ((15, "MOVIE", 32), (16, "MOVIE\\RO", 33), (17, "MOVIE", 32))
    ):
        data = (real[i] if real else make_demo_video(sources[i])) if demo else None
        cam.add_novatek(
            f"20261122_10{minute}00_F.MP4", 400_000 + minute * 10_000,
            folder=folder, time=f"2026/11/22 10:{minute}:00", attr=attr, data=data,
        )  # fmt: skip
    DATA.mkdir(exist_ok=True)
    options = DATA / "options.json"
    if not options.exists():
        options.write_text(
            json.dumps(
                {
                    "cameras": [
                        {
                            "id": "family_car",
                            "name": "Family car",
                            "connection": "lan",
                            "host": host,
                            "driver": "auto",
                            "enabled": True,
                        }  # fmt: skip
                    ],
                    "sync": {
                        "mode": "everything",
                        "remux_to_mp4": False,
                        "thumbnails": demo,
                    },
                },
                indent=2,
            )
        )
    daemon = Daemon(
        DaemonConfig(
            data_dir=DATA,
            media_root=DATA / "media",
            share_root=DATA / "share",
            bind_host="127.0.0.1",
            ingress_allow=("127.0.0.1",),
            timing=Timing(poll=2, hint_poll=1),
            use_supervisor=False,
            api_token="dev-token-" + "x" * 30,
            allow_loopback_cameras=True,
        )
    )
    (DATA / "media").mkdir(exist_ok=True)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, daemon.request_stop)
    await daemon.start()
    # Credentials are never printed; the panel needs none, the token API (8100) takes
    # the dev token set in DaemonConfig above.
    print(f"\nFake camera:  http://{host}/\nPanel:        http://127.0.0.1:8099/\n"
          f"Token API:    http://127.0.0.1:8100/ (dev token: see api_token in scripts/dev.py)\n"
          f"Data:         {DATA}\nCtrl+C to stop.\n", flush=True)  # fmt: skip
    await daemon.wait()
    await daemon.stop()
    await cam.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--demo", action="store_true", help="generate real video clips (needs ffmpeg)"
    )
    parser.add_argument("--fresh", action="store_true", help="delete .dev-data first")
    args = parser.parse_args()
    if args.fresh:
        shutil.rmtree(DATA, ignore_errors=True)
    asyncio.run(main(args.demo))
