"""Check optional OpenClaw script dependencies and print install hints."""

from __future__ import annotations

import sys


def require_modules(*module_names: str, extras: str = "") -> None:
    missing = []
    for name in module_names:
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if not missing:
        return

    print("ERROR: missing Python package(s):", ", ".join(missing))
    print("")
    print("From the OpenClaw repo root, install dependencies:")
    print("  uv sync")
    print("  # or")
    print("  pip install -r requirements.txt")
    if extras:
        print("")
        print(extras)
    raise SystemExit(1)


def require_selenium_for_scripts() -> None:
    require_modules(
        "selenium",
        extras=(
            "SMC portal scripts need Selenium in the same Python you invoke.\n"
            "Prefer:\n"
            "  uv run python scripts/probe_smc_portal.py MXY12-150\n"
            "  uv run python scripts/smc_portal_login.py"
        ),
    )
