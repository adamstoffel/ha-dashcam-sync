#!/usr/bin/env python3
"""Check the versions, and set the one the app store offers.

Two kinds of version live in the repository:

* **The release version**: the integration's manifest (the Marketplace refuses an
  install when it differs from the release tag), the app's ``__version__`` and its
  ``pyproject.toml``. A pull request bumps these together, and they must agree.
* **The app store version**: ``version`` in the app's ``config.yaml``. The Supervisor
  offers an update as soon as it sees a new value there on ``main``, and pulls the image
  of that version, so the release workflow sets it only once the images are published.
  Until then it may lag behind the release version, but never run ahead of it.

Usage:
  ``check_versions.py [tag]``   check; with a tag, the release version must equal it
  ``check_versions.py --set-store VERSION``   write the app store version (release only)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "app/dashcam_sync/config.yaml"
CALVER = re.compile(r"(\d{4})\.(\d{1,2})\.(\d+)(?:b(\d+))?")
STORE_LINE = re.compile(r'^version:\s*"?([^"\s]+)"?\s*$', re.MULTILINE)


def collect() -> dict[str, str]:
    """The release version, from every file that carries it."""
    found = {
        "integration manifest.json": json.loads(
            (ROOT / "custom_components/dashcam_sync/manifest.json").read_text()
        )["version"],
    }
    init = (ROOT / "app/dashcam_sync/src/dashcam_sync/__init__.py").read_text()
    found["app __version__"] = re.search(r'__version__\s*=\s*"([^"]+)"', init).group(1)  # type: ignore[union-attr]
    pyproject = (ROOT / "app/pyproject.toml").read_text()
    found["app pyproject.toml"] = re.search(
        r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE
    ).group(1)  # type: ignore[union-attr]
    return found


def store_version() -> str:
    """The version the app store offers (config.yaml)."""
    return STORE_LINE.search(CONFIG.read_text()).group(1)  # type: ignore[union-attr]


def order(version: str) -> tuple[int, int, int, int]:
    """Sort key: a beta comes before the release of the same number."""
    m = CALVER.fullmatch(version)
    if m is None:
        raise ValueError(f"{version!r} is not CalVer YYYY.M.patch")
    year, month, patch, beta = m.groups()
    return int(year), int(month), int(patch), int(beta) if beta else 1_000_000


def check(tag: str | None) -> list[str]:
    versions = collect()
    store = store_version()
    problems: list[str] = []
    for name, value in {**versions, "app config.yaml": store}.items():
        if not CALVER.fullmatch(value):
            problems.append(f"{name}: {value!r} is not CalVer YYYY.M.patch")
    if problems:
        return problems
    if len(set(versions.values())) != 1:
        problems.append(f"release versions disagree: {versions}")
        return problems
    release = next(iter(versions.values()))
    if order(store) > order(release):
        problems.append(
            f"app config.yaml offers {store}, ahead of the release version {release}: "
            "the app store must never offer a version whose images may not exist"
        )
    if tag:
        wanted = tag.removeprefix("v")
        if release != wanted:
            problems.append(f"the release version is {release}, the tag is {wanted}")
    return problems


def set_store(version: str) -> bool:
    """Make config.yaml offer ``version``. False when it already offers it or a newer one."""
    order(version)  # validates
    if order(store_version()) >= order(version):
        return False
    text = CONFIG.read_text()
    CONFIG.write_text(STORE_LINE.sub(f'version: "{version}"', text, count=1))
    return True


def main(argv: list[str]) -> int:
    if argv[:1] == ["--set-store"] and len(argv) == 2:
        changed = set_store(argv[1].removeprefix("v"))
        print(f"app store version: {store_version()}" + ("" if changed else " (unchanged)"))
        return 0
    problems = check(argv[0] if argv else None)
    for problem in problems:
        print(f"ERROR: {problem}")
    if not problems:
        release = next(iter(collect().values()))
        print(f"versions OK: release {release}, app store offers {store_version()}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
