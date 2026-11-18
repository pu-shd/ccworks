"""Contracts for the macOS setup window's local helper pipe."""

import io
import json
import unittest
from unittest.mock import patch

from ccworks import desktop


class DesktopBridgeTests(unittest.TestCase):
    def test_only_done_confirms_and_eof_cancels(self):
        for answer, confirmed in (
            ("done\n", True),
            ("cancel\n", False),
            ("", False),
            ("yes\n", False),
        ):
            with self.subTest(answer=answer):
                pipe = io.StringIO()

                def login(confirm):
                    actual = confirm()
                    self.assertTrue(
                        pipe.getvalue(), "tell the window when Chrome is ready"
                    )
                    self.assertEqual(confirmed, actual)
                    return (
                        ("ready", "Ready.")
                        if confirmed
                        else ("cancelled", "Cancelled.")
                    )

                with patch.object(desktop, "_setup_sign_in", side_effect=login):
                    self.assertEqual(
                        0, desktop.run_sign_in_bridge(io.StringIO(answer), pipe)
                    )
                events = [json.loads(line) for line in pipe.getvalue().splitlines()]
                self.assertEqual({"event": "waiting"}, events[0])
                self.assertEqual("finished", events[1]["event"])
                self.assertEqual(
                    "ready" if confirmed else "cancelled", events[1]["status"]
                )

    def test_startup_failure_has_a_terminal_event(self):
        pipe = io.StringIO()
        with patch.object(
            desktop, "_setup_sign_in", return_value=("failed", desktop.BUSY)
        ):
            self.assertEqual(1, desktop.run_sign_in_bridge(io.StringIO(), pipe))
        self.assertEqual(
            {"event": "finished", "status": "failed", "message": desktop.BUSY},
            json.loads(pipe.getvalue()),
        )


class SparkleSettingsTests(unittest.TestCase):
    def test_catalog_build_has_no_feed(self):
        from desktop.macos.bundle_config import sparkle_settings

        self.assertEqual({}, sparkle_settings("", ""))

    def test_configured_build_requires_signed_payloads_and_manual_checks(self):
        import base64

        from desktop.macos.bundle_config import sparkle_settings

        key = base64.b64encode(bytes(range(32))).decode("ascii")
        feed = "https://updates.example.test/arm64/appcast.xml"
        settings = sparkle_settings(feed, key)
        self.assertEqual(feed, settings["SUFeedURL"])
        self.assertEqual(key, settings["SUPublicEDKey"])
        self.assertTrue(settings["SUVerifyUpdateBeforeExtraction"])
        self.assertFalse(settings["SUEnableAutomaticChecks"])
        self.assertFalse(settings["SUAllowsAutomaticUpdates"])

        for invalid in (
            "http://updates.example.test/feed",
            "",
            "https://user@updates.example.test",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                sparkle_settings(invalid, key)
        with self.assertRaises(ValueError):
            sparkle_settings(feed, "invalid key")
