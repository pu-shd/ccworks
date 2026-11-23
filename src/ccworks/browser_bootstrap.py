"""Lazy install of Playwright's chromium browser.

Homebrew (and any pip-based install) provides the Python `playwright` package
but not its ~180 MB chromium binary — that binary lives in a user cache
(~/Library/Caches/ms-playwright on macOS). Rather than making `brew install`
touch a user cache, we detect the missing binary on first browser use and
install it then, with a TTY prompt when interactive.
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ccworks.browser_bootstrap")

_chromium_verified = False

# What `playwright install chromium` puts on disk. Headless launches use the
# separate headless-shell build, so a registry holding only the full chromium
# is still incomplete for our default (headless) code paths.
_REQUIRED_BROWSERS = ("chromium", "chromium-headless-shell")


def _browsers_registry() -> Optional[Path]:
    """Directory Playwright unpacks browser builds into, or None if unknown.

    Mirrors Playwright's own registry rules: PLAYWRIGHT_BROWSERS_PATH wins,
    with the documented "0" meaning "inside the package"; otherwise the
    per-platform user cache.
    """
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env == "0":
        try:
            import playwright
        except ImportError:
            return None
        return Path(playwright.__file__).parent / "driver" / "package" / ".local-browsers"
    if env:
        return Path(env)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "ms-playwright"
    cache = os.environ.get("XDG_CACHE_HOME")
    return (Path(cache) if cache else Path.home() / ".cache") / "ms-playwright"


def _chromium_installed() -> bool:
    """Return True if Playwright's chromium builds are unpacked on disk.

    Deliberately a filesystem check and not `sync_playwright().chromium
    .executable_path`. That probe answers the same question but starts (and
    immediately stops) the Node driver, and on Python 3.14 a connection that
    never launched a browser tears down noisily -- every ccworks command
    trailed its JSON with `Task was destroyed but it is pending!`, a
    `Future exception was never retrieved`, and a TargetClosedError traceback
    on stderr. Nothing was actually wrong, but the output looked like a crash
    and polluted stderr for callers parsing it.

    Being wrong here is cheap in one direction only, so it fails toward
    installing: a false "missing" costs a re-run of `playwright install
    chromium`, which is a no-op in well under a second when the builds are
    already there.
    """
    try:
        import playwright
    except ImportError:
        return False

    registry = _browsers_registry()
    if registry is None:
        return False

    manifest = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
    try:
        revisions = {
            b["name"]: b["revision"] for b in json.loads(manifest.read_text())["browsers"]
        }
    except Exception as exc:
        logger.debug(f"_chromium_installed: unreadable {manifest}: {exc!r}")
        return False

    for name in _REQUIRED_BROWSERS:
        revision = revisions.get(name)
        if revision is None:  # older Playwright without this build; not required
            continue
        # Registry layout is `<name with dashes as underscores>-<revision>`,
        # with INSTALLATION_COMPLETE written last -- so a download killed
        # halfway reads as missing rather than usable.
        build = registry / f"{name.replace('-', '_')}-{revision}"
        if not (build / "INSTALLATION_COMPLETE").exists():
            logger.debug(f"_chromium_installed: {build} is absent or incomplete")
            return False

    return True


def _install_chromium() -> None:
    """Invoke `python -m playwright install chromium` in a subprocess."""
    logger.info("Downloading Playwright chromium browser (~180 MB)...")
    print(
        "\n[ccworks] Playwright chromium browser is not installed.\n"
        "         Downloading it now (~180 MB). This is a one-time step.\n",
        file=sys.stderr,
    )
    subprocess.check_call(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        stdout=sys.stderr,
        stderr=sys.stderr,
    )
    print("[ccworks] Chromium install complete.\n", file=sys.stderr)


def ensure_chromium() -> None:
    """Install Playwright's chromium binary if missing.

    Idempotent within a single process. Prompts on TTY; installs silently
    (non-interactive) when stdin is not a TTY (CI, piped invocations).
    Honors CCWORKS_SKIP_BROWSER_BOOTSTRAP=1 for callers that want to manage
    the browser binary themselves.
    """
    global _chromium_verified
    if _chromium_verified:
        return

    if os.environ.get("CCWORKS_SKIP_BROWSER_BOOTSTRAP") == "1":
        _chromium_verified = True
        return

    if _chromium_installed():
        _chromium_verified = True
        return

    if sys.stdin.isatty():
        try:
            sys.stderr.write(
                "\n[ccworks] Chromium browser not found. Download now? (~180 MB) [Y/n]: "
            )
            sys.stderr.flush()
            answer = sys.stdin.readline().strip().lower()
            if answer and answer[0] == "n":
                sys.stderr.write(
                    "[ccworks] Skipping browser install. Run "
                    "`python -m playwright install chromium` manually when ready.\n"
                )
                _chromium_verified = True
                return
        except (EOFError, KeyboardInterrupt):
            print("\n[ccworks] Prompt cancelled; will attempt install anyway.", file=sys.stderr)

    _install_chromium()
    _chromium_verified = True
