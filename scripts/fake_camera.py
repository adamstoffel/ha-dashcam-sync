#!/usr/bin/env python3
"""Serve a fake dashcam on a fixed port: ``python scripts/fake_camera.py [family] [port]``.

Families: novatek (default), viidure, index. Handy for trying the setup wizard.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "tests"))

from fakecam import FakeCamera  # noqa: E402


async def main(family: str, port: int) -> None:
    cam = FakeCamera(family)
    if family == "novatek":
        cam.add_novatek("20261122_101500_F.MP4", 300_000, time="2026/11/22 10:15:00")
        cam.add_novatek(
            "20261122_101600_F.MP4",
            200_000,
            folder="MOVIE\\RO",
            time="2026/11/22 10:16:00",
            attr=33,
        )
    elif family == "viidure":
        cam.add_viidure("loop", "20261122_101500_F.ts", 300_000, 1_795_000_000)
        cam.add_viidure("emr", "20261122_101600_F.ts", 200_000, 1_795_000_060)
    else:
        cam.add_index("record", "20261122_101500_F.mp4", 300_000)
    host = await cam.start(port)
    print(f"{family} fake camera listening on http://{host}/ (Ctrl+C to stop)")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(
        main(
            sys.argv[1] if len(sys.argv) > 1 else "novatek",
            int(sys.argv[2]) if len(sys.argv) > 2 else 0,
        )
    )
