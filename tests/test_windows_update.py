"""Offline tests for the Windows update configuration and WinSparkle lifecycle."""

import base64
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch


class WindowsUpdateTests(unittest.TestCase):
    def test_catalog_build_has_no_updater_configuration(self):
        from ccworks.win_update import UpdateConfig

        self.assertIsNone(UpdateConfig.parse("", ""))

    def test_release_requires_a_paired_https_feed_and_key(self):
        from ccworks.win_update import UpdateConfig

        key = base64.b64encode(bytes(32)).decode("ascii")
        feed = "https://updates.example.test/windows-x64/appcast.xml"
        self.assertEqual(feed, UpdateConfig.parse(feed, key).feed_url)
        for url, public_key in [
            (feed, ""),
            ("http://example.test/feed", key),
            (feed, "no"),
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                UpdateConfig.parse(url, public_key)

    def test_update_uses_manual_checks_and_requests_shutdown_via_callback(self):
        from ccworks.win_update import UpdateConfig, WinSparkle

        with tempfile.TemporaryDirectory() as folder:
            dll = Mock()
            dll.win_sparkle_set_eddsa_public_key.return_value = 1
            events = Mock()
            with patch("ccworks.win_update.ctypes.CDLL", return_value=dll):
                updater = WinSparkle(
                    Path(folder),
                    UpdateConfig("https://example.test/feed", "test-key"),
                    events,
                )
            dll.win_sparkle_set_automatic_check_for_updates.assert_called_once_with(0)
            dll.win_sparkle_init.assert_called_once_with()
            updater.check()
            dll.win_sparkle_check_update_with_ui.assert_called_once_with()
            callback = dll.win_sparkle_set_shutdown_request_callback.call_args.args[0]
            callback()
            events.assert_called_once_with("shutdown")
            updater.close()
            dll.win_sparkle_cleanup.assert_called_once_with()


class WindowsPackagingTests(unittest.TestCase):
    def test_bundle_checks_pinned_updater_archive_before_extracting(self):
        from hashlib import sha256
        import os
        import runpy
        from zipfile import ZipFile

        root = Path(__file__).resolve().parents[1]
        module = runpy.run_path(str(root / "desktop" / "windows-update-data.py"))
        prepare = module["prepare"]
        name = module["PACKAGE"]
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            package = directory / f"{name}.zip"
            with ZipFile(package, "w") as archive:
                for file in ("x64/Release/WinSparkle.dll", "COPYING", "COPYING.expat"):
                    archive.writestr(f"{name}/{file}", "fixture")
            with patch.dict(
                os.environ,
                {
                    "CCWORKS_WINSPARKLE_FEED_URL": "https://example.test/feed.xml",
                    "CCWORKS_WINSPARKLE_PUBLIC_KEY": base64.b64encode(bytes(32)).decode(
                        "ascii"
                    ),
                },
            ):
                with self.assertRaisesRegex(ValueError, "digest"):
                    prepare(directory)
                self.assertFalse((directory / "WinSparkle.dll").exists())
                with patch.dict(
                    prepare.__globals__,
                    {"DIGEST": sha256(package.read_bytes()).hexdigest()},
                ):
                    binaries, data = prepare(directory)
                self.assertEqual(
                    [(str(directory / "WinSparkle.dll"), "ccworks-update")], binaries
                )
                self.assertEqual(3, len(data))

    def test_vm_payload_only_accepts_azure_noncredential_metadata(self):
        import json
        import runpy
        from zipfile import ZipFile

        root = Path(__file__).resolve().parents[1]
        module = runpy.run_path(str(root / "desktop" / "windows-build-payload.py"))
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            archive = directory / "payload.zip"
            metadata = directory / "metadata.json"
            config = {
                "Endpoint": "https://eus.codesigning.azure.net",
                "CodeSigningAccountName": "example",
                "CertificateProfileName": "public",
            }
            metadata.write_text(json.dumps(config), encoding="utf-8")
            module["payload"](root, archive, metadata=metadata)
            with ZipFile(archive) as bundle:
                self.assertEqual(
                    config, json.loads(bundle.read("artifact-signing-metadata.json"))
                )
                self.assertIn("desktop/ccworks.iss", bundle.namelist())
                self.assertTrue(
                    all(
                        name.split("/")[0] in {"desktop", "src", "tests"}
                        or "/" not in name
                        for name in bundle.namelist()
                    )
                )
                self.assertFalse(
                    any(
                        ".env" in name or "concur_session.json" in name
                        for name in bundle.namelist()
                    )
                )
            metadata.write_text(
                json.dumps({**config, "token": "fixture"}), encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                module["payload"](root, archive, metadata=metadata)
