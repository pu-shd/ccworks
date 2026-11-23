"""Resolve per-user state paths outside the working directory."""

import os
import sys
from pathlib import Path


def default_state_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ccworks"
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        if not base.is_absolute():
            raise ValueError("LOCALAPPDATA must be an absolute path.")
        return base / "ccworks"
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "ccworks"


def state_dir(*, desktop: bool = False, create: bool = True) -> Path:
    override = None if desktop else os.environ.get("CCWORKS_STATE_DIR")
    directory = Path(override).expanduser() if override else default_state_dir()
    if desktop and (
        directory.is_symlink() or getattr(directory, "is_junction", lambda: False)()
    ):
        raise ValueError("ccworks Desktop requires a private local state directory.")
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if desktop and os.name != "nt":
            directory.chmod(0o700)
    return directory


def session_file(*, desktop: bool = False, create: bool = True) -> Path:
    return state_dir(desktop=desktop, create=create) / "concur_session.json"


def screenshot_dir() -> Path:
    directory = state_dir() / "screenshots"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory
