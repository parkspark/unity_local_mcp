"""Stable writable/resource paths for source and PyInstaller executions."""

from __future__ import annotations

import sys
from pathlib import Path


def app_dir() -> Path:
    """Directory for persistent user-facing files such as logs and settings."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """Directory containing files bundled by PyInstaller."""
    bundle = getattr(sys, "_MEIPASS", None)
    return Path(bundle).resolve() if bundle else Path(__file__).resolve().parent

