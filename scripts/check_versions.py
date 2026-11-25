#!/usr/bin/env python3
"""Fail unless every version agrees with the release tag.

The integration's manifest version must equal the release tag or the
Marketplace refuses the install; the app's config.yaml version doubles as the
image tag. Usage: ``check_versions.py [tag]`` (a leading ``v`` is ignored).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def collect() -> dict[str, str]:
    found = {
        "integration manifest.json": json.loads(
            (
                ROOT / "integration/custom_components/dashcam_sync/manifest.json"
            ).read_text()
        )["version"],
    }
    config = (ROOT / "app/dashcam_sync/config.yaml").read_text()
    found["app config.yaml"] = re.search(
        r'^version:\s*"?([^"\s]+)"?', config, re.MULTILINE
    ).group(1)  # type: ignore[union-attr]
    init = (ROOT / "app/dashcam_sync/src/dashcam_sync/__init__.py").read_text()
    found["app __version__"] = re.search(r'__version__\s*=\s*"([^"]+)"', init).group(1)  # type: ignore[union-attr]
    pyproject = (ROOT / "app/pyproject.toml").read_text()
    found["app pyproject.toml"] = re.search(
        r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE
    ).group(1)  # type: ignore[union-attr]
    return found


def check(tag: str | None) -> list[str]:
    versions = collect()
    problems: list[str] = []
    for name, value in versions.items():
        if not re.fullmatch(r"\d{4}\.\d{1,2}\.\d+(b\d+)?", value):
            problems.append(f"{name}: {value!r} is not CalVer YYYY.M.patch")
    if len(set(versions.values())) != 1:
        problems.append(f"versions disagree: {versions}")
    if tag:
        wanted = tag.removeprefix("v")
        for name, value in versions.items():
            if value != wanted:
                problems.append(f"{name} is {value}, the tag is {wanted}")
    return problems


def main() -> int:
    problems = check(sys.argv[1] if len(sys.argv) > 1 else None)
    for problem in problems:
        print(f"ERROR: {problem}")
    if not problems:
        print("versions OK:", ", ".join(sorted(set(collect().values()))))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
