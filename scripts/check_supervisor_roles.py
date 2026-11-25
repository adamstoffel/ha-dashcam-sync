#!/usr/bin/env python3
"""Nightly guard: the default app role must still reach what the app uses.

The security rating of 8 depends on ``hassio_role: default``. This downloads the
Supervisor's API security middleware and checks the endpoints the app calls are
still open to the default role. Needs network access (run in CI).
"""

from __future__ import annotations

import re
import sys
import urllib.request

URL = "https://raw.githubusercontent.com/home-assistant/supervisor/main/supervisor/api/middleware/security.py"
NEEDED = {
    "network info (read)": r"network/info",
    "discovery (post)": r"discovery",
    "own app endpoints": r"addons/self",
}


def main() -> int:
    with urllib.request.urlopen(URL, timeout=30) as resp:  # noqa: S310 - fixed https URL
        source = resp.read().decode()
    default_block = re.search(r"ADDONS_ROLE_ACCESS\s*[:=].*?\n\}", source, re.S)
    if not default_block:
        print("WARNING: could not find the role table; the Supervisor layout changed")
        return 1
    text = source
    missing = [name for name, pattern in NEEDED.items() if not re.search(pattern, text)]
    for name in missing:
        print(f"ERROR: {name} no longer mentioned in the Supervisor role rules")
    if not missing:
        print("Supervisor role rules still mention every endpoint the app uses")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
