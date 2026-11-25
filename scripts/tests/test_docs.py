"""Documentation hygiene: relative links, images and anchors must resolve."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = [
    *ROOT.glob("*.md"),
    *(ROOT / "docs").rglob("*.md"),
    ROOT / "app" / "README.md",
    ROOT / "app" / "dashcam_sync" / "README.md",
    ROOT / "app" / "dashcam_sync" / "DOCS.md",
    ROOT / "app" / "dashcam_sync" / "CHANGELOG.md",
    ROOT / "integration" / "README.md",
]
LINK = re.compile(
    r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)|!\[[^\]]*\]\(([^)\s]+)\)|<img[^>]+src=\"([^\"]+)\""
)


def slug(heading: str) -> str:
    text = re.sub(r"[`*_]|<[^>]+>", "", heading).strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s", "-", text)


def anchors(path: Path) -> set[str]:
    return {
        slug(m.group(1))
        for m in re.finditer(r"^#{1,6}\s+(.+)$", path.read_text(), re.M)
    }


@pytest.mark.parametrize("doc", sorted(DOCS), ids=lambda p: str(p.relative_to(ROOT)))
def test_links_resolve(doc: Path) -> None:
    text = re.sub(r"```.*?```", "", doc.read_text(), flags=re.S)
    problems = []
    for match in LINK.finditer(text):
        target = next(g for g in match.groups() if g)
        if re.match(r"(https?:|mailto:)", target):
            continue
        path_part, _, fragment = target.partition("#")
        dest = (doc.parent / path_part).resolve() if path_part else doc
        if not dest.exists():
            problems.append(f"{target}: {dest} does not exist")
        elif fragment and dest.suffix == ".md" and fragment not in anchors(dest):
            problems.append(f"{target}: no heading '{fragment}' in {dest.name}")
    assert problems == []


def test_screenshots_exist_and_are_real_pngs() -> None:
    for name in ("overview", "cameras", "clips", "storage", "logs"):
        data = (ROOT / "docs" / "images" / f"{name}.png").read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 10_000, name


def test_my_links_point_at_this_repository() -> None:
    readme = (ROOT / "README.md").read_text()
    assert (
        "repository_url=https%3A%2F%2Fgithub.com%2Fadamstoffel%2Fha-dashcam-sync"
        in readme
    )
