"""Check optional OpenClaw script dependencies and print install hints."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _try_reexec_with_uv() -> bool:
    """Re-run this script under `uv run` when project .venv exists."""
    if os.environ.get("OPENCLAW_UV_REEXEC") == "1":
        return False
    root = _repo_root()
    if not os.path.isdir(os.path.join(root, ".venv")):
        return False
    if not shutil.which("uv"):
        return False

    env = {**os.environ, "OPENCLAW_UV_REEXEC": "1"}
    cmd = ["uv", "run", "python", *sys.argv]
    print("ℹ️ Re-running with project virtualenv: uv run python", " ".join(sys.argv[1:]))
    raise SystemExit(subprocess.call(cmd, cwd=root, env=env))


def require_modules(*module_names: str, extras: str = "") -> None:
    missing = []
    for name in module_names:
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if not missing:
        return

    if "selenium" in missing:
        _try_reexec_with_uv()

    print("ERROR: missing Python package(s):", ", ".join(missing))
    print("")
    print("From the OpenClaw repo root:")
    print("  uv sync")
    print("  uv run python", " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "scripts/…")
    print("")
    print("Do not use bare python3 unless that interpreter has OpenClaw deps installed.")
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
