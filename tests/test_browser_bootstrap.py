#!/usr/bin/env python3
"""Guards the lazy chromium bootstrap's "is it installed?" check.

The check used to answer the question by starting Playwright's Node driver and
reading `chromium.executable_path`. That was correct but noisy: a driver
connection that never launches a browser tears down badly on Python 3.14, so
every ccworks command trailed its JSON with `Task was destroyed but it is
pending!`, a `Future exception was never retrieved`, and a TargetClosedError
traceback on stderr -- output that reads like a crash and breaks anything
parsing stderr. It is a disk check now, and these tests pin both the answers it
gives and the fact that it starts nothing.

Pure filesystem/mock checks: no browser is started and nothing is downloaded.
"""
import ast
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from ccworks import browser_bootstrap  # noqa: E402

BOOTSTRAP_SOURCE = (REPO_ROOT / "src" / "ccworks" / "browser_bootstrap.py").read_text()

try:
    import playwright  # noqa: F401

    HAVE_PLAYWRIGHT = True
except ImportError:  # the pure-unit CI job may run without it
    HAVE_PLAYWRIGHT = False

requires_playwright = unittest.skipUnless(
    HAVE_PLAYWRIGHT, "playwright package not installed"
)


def _required_revisions() -> dict:
    """{browser name: revision} for the builds the bootstrap insists on.

    Read from Playwright's own manifest so the test pins the naming and marker
    rules rather than a revision number that changes with every upgrade.
    """
    manifest = pathlib.Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
    revisions = {b["name"]: b["revision"] for b in json.loads(manifest.read_text())["browsers"]}
    return {
        name: revisions[name]
        for name in browser_bootstrap._REQUIRED_BROWSERS
        if name in revisions
    }


def _populate(registry: pathlib.Path, revisions: dict, complete: bool = True) -> None:
    """Lay out build directories the way `playwright install` leaves them."""
    for name, revision in revisions.items():
        build = registry / f"{name.replace('-', '_')}-{revision}"
        build.mkdir(parents=True, exist_ok=True)
        if complete:
            (build / "INSTALLATION_COMPLETE").touch()


class TestChromiumInstalled(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = pathlib.Path(self.tmp.name)
        patcher = mock.patch.dict(
            os.environ, {"PLAYWRIGHT_BROWSERS_PATH": str(self.registry)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    @requires_playwright
    def test_complete_registry_reads_as_installed(self):
        _populate(self.registry, _required_revisions())
        self.assertTrue(browser_bootstrap._chromium_installed())

    @requires_playwright
    def test_empty_registry_reads_as_missing(self):
        self.assertFalse(browser_bootstrap._chromium_installed())

    @requires_playwright
    def test_headless_shell_alone_is_not_enough(self):
        # `chromium` and `chromium-headless-shell` are separate downloads and
        # headless launches need the shell, so a registry holding only one of
        # them is incomplete whichever one is missing.
        revisions = _required_revisions()
        if len(revisions) < 2:
            self.skipTest("this Playwright release ships a single chromium build")
        for present, revision in revisions.items():
            with self.subTest(present=present), tempfile.TemporaryDirectory() as partial:
                with mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": partial}):
                    _populate(pathlib.Path(partial), {present: revision})
                    self.assertFalse(browser_bootstrap._chromium_installed())

    @requires_playwright
    def test_interrupted_download_reads_as_missing(self):
        # The build directory exists but INSTALLATION_COMPLETE -- written last
        # -- does not, which is what a killed download leaves behind.
        _populate(self.registry, _required_revisions(), complete=False)
        self.assertFalse(browser_bootstrap._chromium_installed())

    @requires_playwright
    def test_build_absent_from_manifest_is_not_required(self):
        # An older Playwright ships no headless-shell entry; the bootstrap must
        # not demand a build that release never had.
        revisions = _required_revisions()
        _populate(self.registry, revisions)
        first = next(iter(revisions))
        with mock.patch.object(browser_bootstrap, "_REQUIRED_BROWSERS", (first, "no-such-browser")):
            self.assertTrue(browser_bootstrap._chromium_installed())

    def test_unreadable_manifest_fails_toward_installing(self):
        # Being wrong is only cheap in the "install again" direction: a repeat
        # `playwright install chromium` is a sub-second no-op, while a wrong
        # "present" surfaces as a launch failure much later.
        with mock.patch.object(
            pathlib.Path, "read_text", side_effect=OSError("boom")
        ):
            self.assertFalse(browser_bootstrap._chromium_installed())

    @requires_playwright
    def test_check_never_starts_a_playwright_driver(self):
        # The regression this module exists to prevent. Starting the driver is
        # what produced the asyncio teardown noise on every command.
        _populate(self.registry, _required_revisions())
        import playwright.sync_api

        with mock.patch.object(
            playwright.sync_api,
            "sync_playwright",
            side_effect=AssertionError("bootstrap started a Playwright driver"),
        ):
            self.assertTrue(browser_bootstrap._chromium_installed())

    def test_module_has_no_sync_playwright_call(self):
        # Belt-and-braces for environments without playwright installed: parse
        # the module and look for the identifier in code, not in prose.
        tree = ast.parse(BOOTSTRAP_SOURCE)
        identifiers = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                identifiers.update(a.name for a in node.names)
        self.assertNotIn("sync_playwright", identifiers)


class TestBrowsersRegistry(unittest.TestCase):
    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "/custom/browsers"}):
            self.assertEqual(
                browser_bootstrap._browsers_registry(), pathlib.Path("/custom/browsers")
            )

    @requires_playwright
    def test_zero_means_inside_the_package(self):
        with mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "0"}):
            registry = browser_bootstrap._browsers_registry()
        self.assertEqual(registry.name, ".local-browsers")
        self.assertTrue(registry.is_relative_to(pathlib.Path(playwright.__file__).parent))

    def test_platform_defaults(self):
        cases = {
            "darwin": pathlib.Path.home() / "Library" / "Caches" / "ms-playwright",
            "linux": pathlib.Path("/cache") / "ms-playwright",
        }
        for platform, expected in cases.items():
            with self.subTest(platform=platform):
                env = {"XDG_CACHE_HOME": "/cache"}
                with mock.patch.object(sys, "platform", platform), mock.patch.dict(
                    os.environ, env, clear=False
                ):
                    os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
                    self.assertEqual(browser_bootstrap._browsers_registry(), expected)


class TestEnsureChromium(unittest.TestCase):
    def setUp(self):
        # The module memoizes its answer for the life of the process.
        patcher = mock.patch.object(browser_bootstrap, "_chromium_verified", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_skip_env_installs_nothing(self):
        with mock.patch.object(browser_bootstrap, "_install_chromium") as install, \
             mock.patch.object(browser_bootstrap, "_chromium_installed") as check, \
             mock.patch.dict(os.environ, {"CCWORKS_SKIP_BROWSER_BOOTSTRAP": "1"}):
            browser_bootstrap.ensure_chromium()
        install.assert_not_called()
        check.assert_not_called()

    def test_present_installs_nothing(self):
        with mock.patch.object(browser_bootstrap, "_install_chromium") as install, \
             mock.patch.object(browser_bootstrap, "_chromium_installed", return_value=True), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CCWORKS_SKIP_BROWSER_BOOTSTRAP", None)
            browser_bootstrap.ensure_chromium()
        install.assert_not_called()

    def test_missing_installs_without_prompting_off_tty(self):
        with mock.patch.object(browser_bootstrap, "_install_chromium") as install, \
             mock.patch.object(browser_bootstrap, "_chromium_installed", return_value=False), \
             mock.patch.object(sys.stdin, "isatty", return_value=False), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CCWORKS_SKIP_BROWSER_BOOTSTRAP", None)
            browser_bootstrap.ensure_chromium()
        install.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
