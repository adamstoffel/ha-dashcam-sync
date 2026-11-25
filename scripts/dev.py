#!/usr/bin/env python3
"""Run the whole app against a fake camera, no car or Home Assistant needed.

    python scripts/dev.py            # app panel on http://127.0.0.1:8099, API on :8100
    python scripts/dev.py --demo     # real (generated) video clips with thumbnails; needs ffmpeg

The fake camera serves a few clips over a Novatek-style API. Edit
``.dev-data/options.json`` while it runs: the daemon hot-reloads it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app" / "dashcam_sync" / "src"))
sys.path.insert(0, str(ROOT / "app" / "tests"))

from dashcam_sync.daemon import Daemon, DaemonConfig  # noqa: E402
from dashcam_sync.engine import Timing  # noqa: E402
from fakecam import FakeCamera  # noqa: E402

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


async def main(demo: bool) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    cam = FakeCamera("novatek")
    host = await cam.start()
    sources = ("testsrc2", "smptebars", "mandelbrot")
    for i, (minute, folder, attr) in enumerate(
        ((15, "MOVIE", 32), (16, "MOVIE\\RO", 33), (17, "MOVIE", 32))
    ):
        data = make_demo_video(sources[i]) if demo else None
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
                            "id": "fake",
                            "name": "Fake dashcam",
                            "connection": "lan",
                            "host": host,
                            "driver": "auto",
                            "enabled": True,
                        }  # fmt: skip
                    ],
                    "sync": {"mode": "everything", "remux_to_mp4": False, "thumbnails": demo},
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
    print(f"\nFake camera:  http://{host}/\nPanel:        http://127.0.0.1:8099/\n"
          f"API token:    {daemon.token}\nData:         {DATA}\nCtrl+C to stop.\n")  # fmt: skip
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
