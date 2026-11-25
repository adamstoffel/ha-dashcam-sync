"""Persistent "needs restore" flag.

Written to ``/data`` before a camera is put into playback mode, so a crash or
power loss never leaves a camera stuck: on the next contact restore runs first.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class RestoreFlags:
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, camera_id: str) -> Path:
        return self.dir / f"{camera_id}.json"

    def set(self, camera_id: str, driver: str, host: str) -> None:
        payload = {"driver": driver, "host": host, "since": time.time()}
        tmp = self._path(camera_id).with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._path(camera_id))

    def get(self, camera_id: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self._path(camera_id).read_text("utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def clear(self, camera_id: str) -> None:
        self._path(camera_id).unlink(missing_ok=True)

    def pending(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.json"))
