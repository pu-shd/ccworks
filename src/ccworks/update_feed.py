"""Stable GitHub appcast URLs shared by the desktop builders."""

import os
import re

DEFAULT_REPOSITORY = "pu-shd/ccworks"
CHANNELS = ("macos-arm64", "macos-x86_64", "windows-x64")


def github_release_base(repository: str | None = None) -> str:
    if repository is None:
        repository = os.environ.get("CCWORKS_UPDATE_REPOSITORY", DEFAULT_REPOSITORY)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repository):
        raise ValueError("CCWORKS_UPDATE_REPOSITORY must be a GitHub owner/repository.")
    if repository.split("/")[-1] in {".", ".."}:
        raise ValueError("CCWORKS_UPDATE_REPOSITORY must be a GitHub owner/repository.")
    return f"https://github.com/{repository}/releases"


def appcast_name(channel: str) -> str:
    if channel not in CHANNELS:
        raise ValueError("Unsupported desktop update channel.")
    return f"appcast-{channel}.xml"


def configured_feed(channel: str, override: str, public_key: str) -> str:
    if override or not public_key:
        return override
    return f"{github_release_base()}/latest/download/{appcast_name(channel)}"
