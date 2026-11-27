"""Clip paths come from the app; only files under /media or /share may be read."""

from __future__ import annotations

from pathlib import Path

from .const import CLIP_ROOTS


def resolve_clip_path(path: str | None, roots: tuple[str, ...] = CLIP_ROOTS) -> Path | None:
    """Return the resolved path if it exists under an allowed root, else ``None``."""
    if not path:
        return None
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    for root in roots:
        try:
            if resolved.is_relative_to(Path(root).resolve()):
                return resolved
        except OSError:  # pragma: no cover - unresolvable root
            continue
    return None


MIME_BY_SUFFIX = {
    ".mp4": "video/mp4",
    ".ts": "video/mp2t",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def mime_for(path: str) -> str:
    return MIME_BY_SUFFIX.get(Path(path).suffix.lower(), "application/octet-stream")
