"""Project task aliases — npm-scripts style via uv.

Usage (from the repo root):
  uv run build        # package the exe with Nuitka -> build/EplusDRMDownloader.exe
  uv run lint         # ruff check
  uv run format       # ruff format
  uv run typecheck    # pyright type check
  uv run check        # pre-commit run --all-files (lint + format + typecheck)
"""

from __future__ import annotations

import subprocess
import sys


def _run(*cmd: str) -> None:
    sys.exit(subprocess.call(list(cmd)))


def build() -> None:
    _run(
        "uv",
        "run",
        "python",
        "-m",
        "nuitka",
        "--onefile",
        "--mingw64",
        "--windows-console-mode=force",
        "--output-dir=build",
        "--output-filename=EplusDRMDownloader.exe",
        "main.py",
    )


def lint() -> None:
    _run("uv", "run", "ruff", "check", "main.py")


def format() -> None:
    _run("uv", "run", "ruff", "format", "main.py")


def typecheck() -> None:
    _run("uv", "run", "pyright", "main.py")


def check() -> None:
    _run("uv", "run", "pre-commit", "run", "--all-files")
