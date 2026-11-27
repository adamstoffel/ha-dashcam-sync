#!/usr/bin/env python3
"""Guard: every Home Assistant name the integration imports must exist in the HA we support.

The integration's tests run against whichever Home Assistant test package CI can install,
which can be older than ``homeassistant`` in ``integration/hacs.json``. A helper that was
removed in between passes every test and then fails to load on a real install ("cannot
import name ..." when the config flow is loaded). This downloads the Home Assistant wheel
for the minimum supported version (or any version given on the command line, ``latest``
included) and checks every ``from homeassistant... import name`` in the integration against
it, by parsing the source. It cannot see renamed methods or changed behaviour, only
imports, but those are the failures that stop the integration loading at all.

Usage: ``check_ha_api.py [VERSION|latest]``. Needs network access; the parsing is
unit-tested without it (``scripts/tests``).
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = [ROOT / "integration" / "custom_components", ROOT / "integration" / "tests"]
HACS = ROOT / "integration" / "hacs.json"

# Home Assistant now uses Python 3.14 syntax (``except A, B:``); older parsers need parentheses.
_BARE_EXCEPT = re.compile(r"^(\s*)except(\*?) ([^:(\n]+,[^:\n]+):", re.MULTILINE)


def _parse(source: str) -> ast.Module:
    return ast.parse(
        _BARE_EXCEPT.sub(lambda m: f"{m[1]}except{m[2]} ({m[3]}):", source)
    )


def _module_file(root: Path, module: str) -> Path | None:
    base = root.joinpath(*module.split("."))
    for candidate in (base / "__init__.py", base.with_suffix(".py")):
        if candidate.exists():
            return candidate
    return None


def _module_level(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Statements that run at import time: the body, and blocks of ``if``/``try``/``with``
    in it, but never function or class bodies (names there are not importable)."""
    for node in body:
        yield node
        if isinstance(node, ast.If | ast.With | ast.AsyncWith | ast.For | ast.While):
            yield from _module_level(
                node.body + node.orelse if hasattr(node, "orelse") else node.body
            )
        elif isinstance(node, ast.Try | ast.TryStar):
            blocks = node.body + node.orelse + node.finalbody
            for handler in node.handlers:
                blocks += handler.body
            yield from _module_level(blocks)


def _top_level_names(path: Path) -> set[str]:
    """Every name a module can be imported by: definitions, assignments and re-imports."""
    names: set[str] = set()
    for node in _module_level(_parse(path.read_text("utf-8")).body):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(
                n.id
                for t in node.targets
                for n in ast.walk(t)
                if isinstance(n, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.TypeAlias):
            names.add(node.name.id)
        elif isinstance(node, ast.ImportFrom):
            names.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Import):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
    return names


def missing_imports(ha_root: Path, sources: list[Path]) -> list[str]:
    """``file:line: problem`` for each import from homeassistant that the source lacks."""
    problems: list[str] = []
    cache: dict[Path, set[str]] = {}
    for base in sources:
        for file in sorted(base.rglob("*.py")):
            for node in ast.walk(_parse(file.read_text("utf-8"))):
                if not (
                    isinstance(node, ast.ImportFrom)
                    and node.level == 0
                    and node.module
                    and node.module.split(".")[0] == "homeassistant"
                ):
                    continue
                where = f"{file.relative_to(ROOT) if file.is_relative_to(ROOT) else file}:{node.lineno}"
                module_path = _module_file(ha_root, node.module)
                if module_path is None:
                    problems.append(f"{where}: no module {node.module}")
                    continue
                have = cache.setdefault(module_path, _top_level_names(module_path))
                for alias in node.names:
                    if alias.name == "*" or alias.name in have:
                        continue
                    if _module_file(ha_root, f"{node.module}.{alias.name}") is None:
                        problems.append(f"{where}: {node.module} has no {alias.name}")
    return problems


def minimum_version() -> str:
    return str(json.loads(HACS.read_text("utf-8"))["homeassistant"])


def download(version: str, target: Path) -> Path:
    spec = "homeassistant" if version == "latest" else f"homeassistant=={version}"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            spec,
            "--no-deps",
            "--ignore-requires-python",
            "--dest",
            str(target),
            "-q",
        ],
        check=True,
    )
    wheel = next(target.glob("homeassistant-*.whl"))
    with zipfile.ZipFile(wheel) as zf:
        zf.extractall(
            target / "src", [n for n in zf.namelist() if n.startswith("homeassistant/")]
        )
    return target / "src"


def main(argv: list[str]) -> int:
    version = argv[1] if len(argv) > 1 else minimum_version()
    with tempfile.TemporaryDirectory() as tmp:
        ha_root = download(version, Path(tmp))
        problems = missing_imports(ha_root, SOURCES)
    if problems:
        print(
            f"Names the integration imports that Home Assistant {version} does not have:"
        )
        print("\n".join(f"  {p}" for p in problems))
        return 1
    print(f"Every Home Assistant import in the integration exists in {version}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
