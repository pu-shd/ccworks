"""One browser launch configuration for login, verification and operations."""

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from typing import Callable, Iterator, Literal

from playwright.sync_api import Browser, BrowserType, Error

BROWSER_MESSAGES = {
    "missing": "Install your organization’s approved Chrome or Chromium browser from the software catalog, then try again.",
    "blocked": "Your organization’s browser settings prevented ccworks from opening Chrome. Contact IT and mention ccworks.",
    "unsupported": "This Chromium build is unavailable or unsupported by ccworks. Contact IT to verify the approved browser configuration.",
}


class BrowserUnavailable(RuntimeError):
    def __init__(self, status: str):
        super().__init__(BROWSER_MESSAGES[status])
        self.status = status


@dataclass(frozen=True)
class BrowserLaunch:
    kind: Literal["playwright", "chrome", "chromium"] = "playwright"
    executable: Path | None = None
    version: str | None = None

    @classmethod
    def from_environment(cls, *, desktop: bool = False) -> "BrowserLaunch":
        mode = os.environ.get("CCWORKS_BROWSER", "chrome" if desktop else "playwright")
        if mode == "chrome":
            return cls("chrome")
        if mode == "playwright" and not desktop:
            return cls()
        if mode == "chromium":
            executable = Path(os.environ.get("CCWORKS_CHROMIUM_EXECUTABLE", ""))
            version = os.environ.get("CCWORKS_CHROMIUM_VERSION", "")
            if (
                not executable.is_absolute()
                or not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version)
                or (sys.platform == "win32" and executable.suffix.lower() != ".exe")
            ):
                raise BrowserUnavailable("unsupported")
            return cls("chromium", executable, version)
        raise BrowserUnavailable("unsupported")

    def launch(self, chromium: BrowserType, *, headless: bool) -> Browser:
        if self.kind == "playwright":
            return chromium.launch(headless=headless)
        try:
            if self.kind == "chrome":
                return chromium.launch(
                    channel="chrome", headless=headless, timeout=15000
                )
            if self.executable is None or not self.executable.is_file():
                raise BrowserUnavailable("missing")
            if not os.access(self.executable, os.X_OK):
                raise BrowserUnavailable("blocked")
            browser = chromium.launch(
                executable_path=str(self.executable), headless=headless, timeout=15000
            )
        except Error as error:
            message = str(error).lower()
            missing = (
                "executable doesn't exist" in message
                or "distribution 'chrome' is not found" in message
            )
            raise BrowserUnavailable("missing" if missing else "blocked") from None

        if browser.version != self.version:
            browser.close()
            raise BrowserUnavailable("unsupported")
        return browser


@contextmanager
def driver_temp_environment(
    directory: Path | None = None,
) -> Iterator[Callable[[], None]]:
    with TemporaryDirectory(prefix=".browser-", dir=directory) as temp:
        previous = {name: os.environ.get(name) for name in ("TMPDIR", "TEMP", "TMP")}

        def restore() -> None:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        try:
            for name in previous:
                os.environ[name] = temp
            yield restore
        finally:
            restore()
