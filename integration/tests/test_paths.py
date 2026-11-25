from __future__ import annotations

from pathlib import Path

from custom_components.dashcam_sync.paths import mime_for, resolve_clip_path


def test_resolve_only_inside_allowed_roots(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    inside = root / "a.mp4"
    inside.write_bytes(b"x")
    outside = tmp_path / "b.mp4"
    outside.write_bytes(b"x")
    roots = (str(root),)
    assert resolve_clip_path(str(inside), roots) == inside.resolve()
    assert resolve_clip_path(str(outside), roots) is None
    assert resolve_clip_path(str(root / ".." / "b.mp4"), roots) is None  # traversal
    assert resolve_clip_path(str(root / "missing.mp4"), roots) is None
    assert resolve_clip_path(None, roots) is None and resolve_clip_path("", roots) is None
    link = root / "link.mp4"
    link.symlink_to(outside)
    assert resolve_clip_path(str(link), roots) is None  # a symlink out of the root is refused


def test_mime_types() -> None:
    assert mime_for("a.MP4") == "video/mp4" and mime_for("a.ts") == "video/mp2t"
    assert mime_for("a.jpg") == "image/jpeg" and mime_for("a.bin") == "application/octet-stream"
