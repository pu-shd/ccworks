"""Stage the pinned WinSparkle DLL and appcast configuration for PyInstaller."""

from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile

from ccworks.update_feed import configured_feed
from ccworks.win_update import UpdateConfig

VERSION = "0.9.4"
DIGEST = "6037df37fc263bd1650a1c4949681a9d40ffe991d01f35892a406cb5d103c976"
PACKAGE = f"WinSparkle-{VERSION}"
URL = (
    f"https://github.com/vslavik/winsparkle/releases/download/v{VERSION}/{PACKAGE}.zip"
)


def prepare(directory: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    key = os.environ.get("CCWORKS_WINSPARKLE_PUBLIC_KEY", "")
    feed = configured_feed(
        "windows-x64", os.environ.get("CCWORKS_WINSPARKLE_FEED_URL", ""), key
    )
    config = UpdateConfig.parse(feed, key)
    if config is None:
        return [], []
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / f"{PACKAGE}.zip"
    if not archive.is_file():
        with urlopen(URL, timeout=60) as response:
            contents = response.read(32 * 1024 * 1024 + 1)
        if sha256(contents).hexdigest() != DIGEST:
            raise ValueError(
                "The WinSparkle archive digest did not match the pinned release."
            )
        archive.write_bytes(contents)
    if sha256(archive.read_bytes()).hexdigest() != DIGEST:
        raise ValueError(
            "The cached WinSparkle archive digest did not match the pinned release."
        )
    with ZipFile(archive) as release:
        for relative in ("x64/Release/WinSparkle.dll", "COPYING", "COPYING.expat"):
            (directory / Path(relative).name).write_bytes(
                release.read(f"{PACKAGE}/{relative}")
            )
    (directory / "settings.json").write_text(
        json.dumps(asdict(config)), encoding="utf-8"
    )
    return (
        [(str(directory / "WinSparkle.dll"), "ccworks-update")],
        [
            (str(directory / file), "ccworks-update")
            for file in ("COPYING", "COPYING.expat", "settings.json")
        ],
    )
