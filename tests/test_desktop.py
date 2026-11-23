"""Offline tests shared by the native and Docker desktop checks."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import Error

from ccworks import paths
from ccworks.browser_client import ConcurBrowserClient


class DesktopContractTests(unittest.TestCase):
    def test_windows_uses_local_appdata_and_preserves_developer_override(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / "Données locales"
            with (
                patch.object(paths.sys, "platform", "win32"),
                patch.dict(os.environ, {"LOCALAPPDATA": str(base)}, clear=True),
            ):
                self.assertEqual(base / "ccworks", paths.state_dir())
                os.environ["CCWORKS_STATE_DIR"] = str(base / "Developer")
                self.assertEqual(base / "Developer", paths.state_dir())

    def test_lock_remains_stable_and_serializes_independent_processes(self):
        with tempfile.TemporaryDirectory(prefix="ccworks données ") as folder:
            state = Path(folder) / "concur_session.json"
            lock = Path(f"{state}.lock")
            child = """
import sys
from pathlib import Path
from ccworks.session_state import session_lock
print('waiting', flush=True)
with session_lock(Path(sys.argv[1])):
    print('acquired', flush=True)
    assert sys.stdin.readline(), 'parent closed input before release'
"""
            from ccworks.session_state import session_lock

            with session_lock(state):
                first = subprocess.Popen(
                    [sys.executable, "-u", "-c", child, str(state)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    text=True,
                )
                self.addCleanup(self.stop_child, first)
                self.assertEqual("waiting", first.stdout.readline().strip())
                with self.assertRaises(TimeoutError):
                    with session_lock(state, timeout=0):
                        self.fail("The same file was locked twice")
                self.assertTrue(lock.is_file())
            self.assertEqual("acquired", first.stdout.readline().strip())
            with self.assertRaises(TimeoutError):
                with session_lock(state, timeout=0):
                    self.fail("The parent bypassed the child lock")
            first.stdin.write("release\n")
            first.stdin.flush()
            self.assertEqual(0, first.wait(timeout=10))
            with session_lock(state, timeout=0):
                self.assertTrue(lock.is_file())

    @staticmethod
    def stop_child(child):
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)

    def test_desktop_uses_chrome_without_developer_bootstrap(self):
        from ccworks.browser_launch import BrowserLaunch

        chromium = Mock()
        with (
            tempfile.TemporaryDirectory() as path,
            patch.dict(os.environ, {"CCWORKS_STATE_DIR": path}),
            patch("ccworks.browser_client.ensure_chromium") as bootstrap,
        ):
            client = ConcurBrowserClient(browser_launch=BrowserLaunch("chrome"))
        bootstrap.assert_not_called()
        client.browser_launch.launch(chromium, headless=False)
        chromium.launch.assert_called_once_with(
            channel="chrome", headless=False, timeout=15000
        )

    def test_missing_or_blocked_browser_errors_are_redacted(self):
        from ccworks import desktop

        for message, status in [
            ("Executable doesn't exist: private details", "missing"),
            ("Browser failed to open: private details", "blocked"),
        ]:
            with (
                self.subTest(status=status),
                patch.dict(os.environ, {"CCWORKS_BROWSER": "chrome"}),
                patch("ccworks.desktop.sync_playwright") as playwright,
            ):
                engine = playwright.return_value.__enter__.return_value.chromium
                engine.launch.side_effect = Error(message)
                result = desktop.helper_status()
                self.assertEqual(status, result["browser"])
                self.assertEqual("unchecked", result["sign_in"])
                self.assertNotIn("private details", json.dumps(result))

    def test_custom_chromium_requires_exact_validated_version(self):
        from ccworks.browser_launch import BrowserLaunch, BrowserUnavailable

        with tempfile.TemporaryDirectory(prefix="ccworks données ") as folder:
            executable = Path(folder) / "Approved Chromium.exe"
            executable.touch(mode=0o700)
            with patch.dict(
                os.environ,
                {
                    "CCWORKS_BROWSER": "chromium",
                    "CCWORKS_CHROMIUM_EXECUTABLE": str(executable),
                    "CCWORKS_CHROMIUM_VERSION": "123.0.0.1",
                },
            ):
                config = BrowserLaunch.from_environment(desktop=True)
                engine = Mock()
                engine.launch.return_value.version = "123.0.0.2"
                with self.assertRaises(BrowserUnavailable) as error:
                    config.launch(engine, headless=True)
                self.assertEqual("unsupported", error.exception.status)
                engine.launch.assert_called_once_with(
                    executable_path=str(executable), headless=True, timeout=15000
                )
                engine.launch.return_value.close.assert_called_once()

    def test_cleanup_waits_for_owner_and_preserves_unrelated_files(self):
        from ccworks.desktop import remove_data
        from ccworks.session_state import session_lock

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            state = directory / "concur_session.json"
            state.write_text("old session", encoding="utf-8")
            runtime = directory / ".browser-12345678"
            runtime.mkdir()
            (runtime / "test.txt").write_text("old profile", encoding="utf-8")
            receipt = directory / "receipt.pdf"
            receipt.touch()
            with patch.object(paths, "default_state_dir", return_value=directory):
                with session_lock(state):
                    with self.assertRaises(TimeoutError):
                        remove_data()
                    self.assertTrue(state.exists())
                remove_data()
                remove_data()
            self.assertFalse(state.exists())
            self.assertFalse(runtime.exists())
            self.assertTrue(receipt.exists())

    def test_setup_cancels_or_promotes_using_shared_browser(self):
        from ccworks import desktop
        from ccworks.browser_launch import BrowserLaunch
        from ccworks.safari_login import LoginCancelled

        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "session.json"
            previous = b'{"previous": true}'
            state.write_bytes(previous)
            capture = {
                "cookies": [
                    {
                        "name": "JWT",
                        "domain": "www.concursolutions.com",
                        "value": "fixture",
                        "expires": -1,
                    }
                ]
            }
            approved = BrowserLaunch("chrome")
            client = ConcurBrowserClient(
                desktop=True, browser_launch=approved, session_file=str(state)
            )

            def headed(staged, confirm):
                self.assertIs(approved, staged.browser_launch)
                if not confirm():
                    raise LoginCancelled("cancelled")
                Path(staged.session_file).write_text(
                    json.dumps(capture), encoding="utf-8"
                )

            with (
                patch("ccworks.desktop.ConcurBrowserClient", return_value=client),
                patch.object(ConcurBrowserClient, "run_headed_login", headed),
                patch.object(
                    ConcurBrowserClient,
                    "check_session_validity",
                    return_value={
                        "success": True,
                        "authenticated": True,
                    },
                ),
                patch.object(ConcurBrowserClient, "verify_session_reuse") as verify,
            ):
                self.assertIn("cancelled", desktop.setup_sign_in(lambda: False))
                self.assertEqual(previous, state.read_bytes())
                verify.assert_not_called()

                verify.side_effect = RuntimeError("sensitive browser details")
                self.assertEqual(
                    desktop.LOGIN_FAILED, desktop.setup_sign_in(lambda: True)
                )
                self.assertEqual(previous, state.read_bytes())

                verify.side_effect = None
                self.assertIn("Ready", desktop.setup_sign_in(lambda: True))
                self.assertEqual(capture, json.loads(state.read_bytes()))

    def test_status_verifies_without_refreshing_saved_state(self):
        from ccworks import desktop
        from ccworks.browser_client import ConcurSessionExpiredError

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            session = directory / "concur_session.json"
            state = {
                "cookies": [
                    {
                        "name": "JWT",
                        "domain": "www.concursolutions.com",
                        "value": "fake-session",
                        "expires": -1,
                    }
                ]
            }
            session.write_text(json.dumps(state), encoding="utf-8")
            mtime = session.stat().st_mtime_ns
            with (
                patch.object(paths, "default_state_dir", return_value=directory),
                patch("ccworks.desktop.sync_playwright"),
                patch("ccworks.desktop.verify_dashboard_session") as verify,
            ):
                ready = desktop.helper_status()
                self.assertEqual("ready", ready["sign_in"])
                verify.assert_called_once()
                verify.side_effect = ConcurSessionExpiredError()
                self.assertEqual("required", desktop.helper_status()["sign_in"])
                verify.side_effect = RuntimeError("sensitive network trace")
                unavailable = desktop.helper_status()
                self.assertEqual("unavailable", unavailable["sign_in"])
                self.assertNotIn("sensitive network trace", json.dumps(unavailable))
            self.assertEqual(mtime, session.stat().st_mtime_ns)
            self.assertEqual(state, json.loads(session.read_bytes()))
            self.assertFalse(Path(f"{session}.lock").exists())

    def test_status_handles_absent_state_without_creating_it(self):
        from ccworks import desktop

        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "private state"
            with (
                patch.object(paths, "default_state_dir", return_value=state),
                patch("ccworks.desktop.sync_playwright") as playwright,
            ):
                result = desktop.helper_status()
            self.assertEqual(1, result["protocol"])
            self.assertEqual("ready", result["browser"])
            self.assertEqual("required", result["sign_in"])
            self.assertFalse(state.exists())
            self.assertNotIn(str(state), json.dumps(result))
            playwright.return_value.__enter__.return_value.chromium.launch.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
