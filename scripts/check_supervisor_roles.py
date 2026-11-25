#!/usr/bin/env python3
"""Nightly guard: the *default* app role must still reach everything the app calls.

The app's security rating of 8 depends on ``hassio_role: default``. This downloads the
Supervisor's API security middleware, evaluates its role regexes (``api_bypass`` plus the
``default`` role's ``role_access``) and checks every Supervisor endpoint the app uses.
Needs network access, so it runs in CI (nightly), not in the unit tests; the parsing is
unit-tested against an excerpt (``scripts/tests``).
"""

from __future__ import annotations

import ast
import re
import sys
import urllib.request

URL = "https://raw.githubusercontent.com/home-assistant/supervisor/main/supervisor/api/middleware/security.py"

# (method, path) the app calls with the default role. See app/.../supervisor.py.
USED = [
    ("GET", "/network/info"),
    ("POST", "/discovery"),
    ("DELETE", "/discovery/6f2a1c3e0d4b4c7e9a8f1b2c3d4e5f60"),
    ("GET", "/addons/self/info"),
    ("POST", "/addons/self/options"),
    ("POST", "/addons/self/restart"),
]
# Not reachable with the default role by design: the wizard falls back to typing the SSID.
EXPECTED_BLOCKED = [("GET", "/network/interface/wlan0/accesspoints")]


def _eval(node: ast.AST, env: dict[str, str]) -> str:
    """Evaluate a string expression made of literals, ``+`` and known names."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _eval(node.left, env) + _eval(node.right, env)
    if isinstance(node, ast.JoinedStr):
        return "".join(_eval(v, env) for v in node.values)
    if isinstance(node, ast.Name):
        return env.get(node.id, "[^/]+")
    raise ValueError(f"cannot evaluate {ast.dump(node)[:60]}")


def _compile_arg(call: ast.Call, env: dict[str, str]) -> re.Pattern[str]:
    return re.compile(_eval(call.args[0], env))


def extract_patterns(
    source: str, name: str = "_V1_PATTERNS"
) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """Return (api_bypass, default-role role_access) from the middleware source."""
    tree = ast.parse(source)
    env: dict[str, str] = {}
    target: ast.Call | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(
            node.value, (ast.Constant, ast.BinOp)
        ):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    try:
                        env[tgt.id] = _eval(node.value, env)
                    except ValueError:
                        pass
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            tgt = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if (
                isinstance(tgt, ast.Name)
                and tgt.id == name
                and isinstance(node.value, ast.Call)
            ):
                target = node.value
    if target is None:
        raise LookupError(f"{name} not found")
    kwargs = {kw.arg: kw.value for kw in target.keywords}
    bypass = _compile_arg(kwargs["api_bypass"], env)  # type: ignore[arg-type]
    roles = kwargs["role_access"]
    assert isinstance(roles, ast.Dict)
    for key, value in zip(roles.keys, roles.values, strict=True):
        if isinstance(key, ast.Name) and key.id == "ROLE_DEFAULT":
            return bypass, _compile_arg(value, env)  # type: ignore[arg-type]
    raise LookupError("ROLE_DEFAULT not found")


def check(source: str) -> list[str]:
    problems: list[str] = []
    try:
        bypass, default = extract_patterns(source)
    except (LookupError, ValueError, SyntaxError, KeyError) as err:
        return [
            f"could not read the Supervisor's role rules ({err}); the layout changed"
        ]

    def allowed(path: str) -> bool:
        return bool(bypass.match(path) or default.match(path))

    for method, path in USED:
        if not allowed(path):
            problems.append(f"the default role can no longer {method} {path}")
    for method, path in EXPECTED_BLOCKED:
        if allowed(path):
            print(
                f"note: the default role can now {method} {path}; the wizard's Wi-Fi scan could use it"
            )
    return problems


def main() -> int:
    with urllib.request.urlopen(URL, timeout=30) as resp:
        source = resp.read().decode()
    problems = check(source)
    for problem in problems:
        print(f"ERROR: {problem}")
    if not problems:
        print("The default role still reaches every Supervisor endpoint the app uses.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
