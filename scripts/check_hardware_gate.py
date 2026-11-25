#!/usr/bin/env python3
"""The hardware release gate: nothing stable releases until the matrix has been run.

Reads ``docs/hardware-gate/<version>.md``, a markdown table with one row per
camera and host. Requires, for every stable driver, a row on amd64 and on
aarch64, and no failed cell. Beta tags (``...b1``) skip the gate.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STABLE_DRIVERS = ("novatek", "viidure_app", "http_index")
ARCHES = ("amd64", "aarch64")
STEPS = ("detect", "list", "resume", "verify", "remux", "restore", "purge")
OK = {"pass", "n/a"}


def parse(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    header: list[str] | None = None
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip().lower() for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue
        if header is None:
            header = cells
            continue
        if len(cells) == len(header):
            rows.append(dict(zip(header, cells, strict=True)))
    return rows


def validate(text: str) -> list[str]:
    rows = parse(text)
    problems: list[str] = []
    if not rows:
        return ["no table rows found"]
    for row in rows:
        for step in STEPS:
            value = row.get(step, "")
            if value not in OK:
                problems.append(
                    f"{row.get('camera', '?')} ({row.get('driver')}, {row.get('arch')}): {step} = {value or 'missing'}"
                )
    covered = {(r.get("driver"), r.get("arch")) for r in rows}
    for driver in STABLE_DRIVERS:
        for arch in ARCHES:
            if (driver, arch) not in covered:
                problems.append(f"no camera tested for driver {driver} on {arch}")
    return problems


def main(argv: list[str]) -> int:
    version = argv[1].removeprefix("v") if len(argv) > 1 else ""
    if re.search(r"b\d+$", version):
        print("beta tag: hardware gate skipped")
        return 0
    path = ROOT / "docs" / "hardware-gate" / f"{version}.md"
    if not path.exists():
        print(
            f"ERROR: {path.relative_to(ROOT)} does not exist; run the hardware matrix first"
        )
        return 1
    problems = validate(path.read_text())
    for problem in problems:
        print(f"ERROR: {problem}")
    if not problems:
        print(f"hardware gate for {version} passed")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
