"""Machine-readable helper checks and the desktop setup operations."""

import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Callable, TextIO

from playwright.sync_api import sync_playwright

from ccworks import __version__, paths
from ccworks.browser_client import (
    ConcurBrowserClient,
    ConcurSessionExpiredError,
    verify_dashboard_session,
)
from ccworks.browser_launch import (
    BrowserLaunch,
    BrowserUnavailable,
    driver_temp_environment,
)
from ccworks.safari_login import (
    LoginCancelled,
    SafariLoginError,
    _require_auth_state,
    run_login,
)
from ccworks.session_state import session_lock
from ccworks.win_update import remove_update_preferences

PROTOCOL = 1
CONCUR_URL = "https://www.concursolutions.com"
BUSY = "ccworks is busy. Wait for the current operation to finish and try again."
LOGIN_FAILED = "Sign-in could not be verified. Choose Sign in to Concur again and wait for the Concur expense dashboard before selecting Done."


def helper_status() -> dict[str, str | int]:
    """Check the installed browser and saved session without updating local state."""
    browser_status = "unavailable"
    sign_in = "unchecked"
    try:
        launch = BrowserLaunch.from_environment(desktop=True)
        with driver_temp_environment() as restore, sync_playwright() as playwright:
            restore()
            browser = launch.launch(playwright.chromium, headless=True)
            browser_status = "ready"
            try:
                sign_in = _probe_sign_in(
                    browser, paths.session_file(desktop=True, create=False)
                )
            finally:
                browser.close()
    except BrowserUnavailable as error:
        browser_status = error.status
    except Exception:
        if browser_status == "ready":
            sign_in = "unavailable"
    return {
        "protocol": PROTOCOL,
        "version": __version__,
        "browser": browser_status,
        "sign_in": sign_in,
    }


def _probe_sign_in(browser, path: Path) -> str:
    if path.is_symlink():
        return "required"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        _require_auth_state(state, CONCUR_URL)
    except (FileNotFoundError, ValueError, SafariLoginError):
        return "required"
    context = browser.new_context(storage_state=state)
    try:
        verify_dashboard_session(context.new_page(), CONCUR_URL)
    except ConcurSessionExpiredError:
        return "required"
    return "ready"


def _setup_sign_in(confirm: Callable[[], bool]) -> tuple[str, str]:
    try:
        client = ConcurBrowserClient(desktop=True)
        run_login(client, confirm=confirm)
    except LoginCancelled:
        return "cancelled", "Sign-in cancelled. Any saved session was kept."
    except BrowserUnavailable as error:
        return "failed", str(error)
    except TimeoutError:
        return "failed", BUSY
    except Exception:
        return "failed", LOGIN_FAILED
    return "ready", "Ready. Return to your ChatGPT Work session."


def setup_sign_in(confirm: Callable[[], bool]) -> str:
    return _setup_sign_in(confirm)[1]


def run_sign_in_bridge(commands: TextIO, events: TextIO) -> int:
    """Exchange sign-in progress and the user's decision with the setup app."""

    def confirm() -> bool:
        try:
            print(json.dumps({"event": "waiting"}), file=events, flush=True)
        except BrokenPipeError:
            return False
        return commands.readline(16) == "done\n"

    status, message = _setup_sign_in(confirm)
    try:
        print(
            json.dumps({"event": "finished", "status": status, "message": message}),
            file=events,
            flush=True,
        )
    except BrokenPipeError:
        return 1
    return int(status == "failed")


def remove_data() -> None:
    """Remove only owned state, refusing to run while a browser operation is active."""
    directory = paths.state_dir(desktop=True, create=False)
    if not directory.is_dir():
        remove_update_preferences()
        return
    path = directory / "concur_session.json"
    with session_lock(path, timeout=0):
        for entry in directory.iterdir():
            if entry.name in {path.name, "screenshots", "logs", "preferences.json"} or (
                re.fullmatch(
                    r"\.(?:login|browser)-[a-z0-9_]{8}|\.session-[a-z0-9_]{8}\.json",
                    entry.name,
                )
            ):
                if not entry.is_symlink() and entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink(missing_ok=True)
        remove_update_preferences()


def quiet_desktop() -> None:
    logging.disable(logging.CRITICAL)
    for name in (
        "DEBUG",
        "PWDEBUG",
        "NODE_DEBUG",
        "NODE_OPTIONS",
        "PLAYWRIGHT_NODEJS_PATH",
    ):
        os.environ.pop(name, None)


def main() -> None:
    quiet_desktop()
    if sys.argv[1:] == ["desktop", "check"]:
        result = helper_status()
        print(json.dumps(result), flush=True)
        sys.exit(
            0 if result["browser"] == "ready" and result["sign_in"] == "ready" else 1
        )
    if sys.argv[1:] == ["desktop", "sign-in"]:
        sys.exit(run_sign_in_bridge(sys.stdin, sys.stdout))
    if sys.argv[1:] == ["desktop", "remove-data"]:
        try:
            remove_data()
        except TimeoutError:
            sys.exit(BUSY)
        except (OSError, ValueError):
            sys.exit(
                "ccworks data could not be removed. Contact IT and mention ccworks."
            )
        print("ccworks data removed.", flush=True)
        return
    if sys.argv[1:2] == ["desktop"]:
        sys.exit("Usage: ccworks desktop {check,remove-data}")
    from ccworks.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
