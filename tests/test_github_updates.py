"""Contracts for the desktop update channels backed by GitHub Releases."""

import base64
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SPARKLE = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"
KEY = base64.b64encode(bytes(32)).decode("ascii")
SIGNATURE = base64.b64encode(bytes(64)).decode("ascii")


class GitHubUpdateTests(unittest.TestCase):
    def test_build_settings_default_to_distinct_github_release_feeds(self):
        from ccworks.update_feed import configured_feed

        expected = "https://github.com/pu-shd/ccworks/releases/latest/download/"
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual("", configured_feed("macos-arm64", "", ""))
            self.assertEqual(
                expected + "appcast-macos-arm64.xml",
                configured_feed("macos-arm64", "", KEY),
            )
            self.assertEqual(
                expected + "appcast-windows-x64.xml",
                configured_feed("windows-x64", "", KEY),
            )
            custom = "https://example.test/appcast.xml"
            self.assertEqual(custom, configured_feed("windows-x64", custom, KEY))

    def test_repository_override_cannot_redirect_away_from_github(self):
        from ccworks.update_feed import configured_feed

        for value in (
            "https://example.test/account/repo",
            "account/../x",
            "account/..",
        ):
            with (
                self.subTest(repository=value),
                patch.dict(os.environ, {"CCWORKS_UPDATE_REPOSITORY": value}),
                self.assertRaises(ValueError),
            ):
                configured_feed("macos-arm64", "", KEY)

    def test_appcasts_point_to_immutable_platform_assets(self):
        generate = runpy.run_path(str(ROOT / "desktop" / "stage-github-update.py"))[
            "stage_appcast"
        ]
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            for suffix, channel, platform in (
                ("arm64.dmg", "macos-arm64", "macos"),
                ("windows-x64.exe", "windows-x64", "windows-x64"),
            ):
                with self.subTest(channel=channel):
                    asset = directory / f"ccworks-desktop-0.8.0-{suffix}"
                    asset.write_bytes(b"fixture archive")
                    appcast = generate(asset, SIGNATURE, directory, "pu-shd/ccworks")
                    self.assertEqual(f"appcast-{channel}.xml", appcast.name)
                    item = ET.parse(appcast).find("./channel/item")
                    self.assertIsNotNone(item)
                    self.assertEqual("0.8.0", item.findtext(f"{SPARKLE}version"))
                    enclosure = item.find("enclosure").attrib
                    self.assertEqual(
                        "https://github.com/pu-shd/ccworks/releases/download/v0.8.0/"
                        + asset.name,
                        enclosure["url"],
                    )
                    self.assertEqual(str(asset.stat().st_size), enclosure["length"])
                    self.assertEqual(SIGNATURE, enclosure[f"{SPARKLE}edSignature"])
                    self.assertEqual(platform, enclosure[f"{SPARKLE}os"])
                    self.assertEqual(
                        channel == "windows-x64",
                        f"{SPARKLE}installerArguments" in enclosure,
                    )
                    with self.assertRaises(ValueError):
                        generate(
                            asset, "invalid-signature", directory, "pu-shd/ccworks"
                        )
