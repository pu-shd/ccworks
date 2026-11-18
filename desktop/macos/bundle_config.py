"""Configure the finished macOS bundle from release environment variables."""

import base64
import binascii
import os
import platform
from pathlib import Path
import plistlib
import sys
from urllib.parse import urlsplit

from ccworks.update_feed import configured_feed


def sparkle_settings(feed: str, key: str) -> dict[str, str | bool]:
    if not feed and not key:
        return {}
    if not feed or not key:
        raise ValueError(
            "Set both CCWORKS_SPARKLE_FEED_URL and CCWORKS_SPARKLE_PUBLIC_KEY."
        )
    try:
        url = urlsplit(feed)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.query
            or url.fragment
            or any(char.isspace() for char in feed)
        ):
            raise ValueError
    except ValueError:
        raise ValueError("The Sparkle appcast must be a stable HTTPS URL.") from None
    try:
        if len(base64.b64decode(key, validate=True)) != 32:
            raise ValueError
    except (ValueError, binascii.Error):
        raise ValueError(
            "The Sparkle public key must be a base64-encoded Ed25519 key."
        ) from None
    return {
        "SUFeedURL": feed,
        "SUPublicEDKey": key,
        "SUEnableAutomaticChecks": False,
        "SUAllowsAutomaticUpdates": False,
        "SUVerifyUpdateBeforeExtraction": True,
    }


def main() -> None:
    try:
        key = os.environ.get("CCWORKS_SPARKLE_PUBLIC_KEY", "")
        feed = configured_feed(
            f"macos-{platform.machine()}",
            os.environ.get("CCWORKS_SPARKLE_FEED_URL", ""),
            key,
        )
        settings = sparkle_settings(feed, key)
    except ValueError as error:
        sys.exit(str(error))
    if sys.argv[1:] == ["--validate"]:
        return
    (plist,) = sys.argv[1:]
    path = Path(plist)
    metadata = plistlib.loads(path.read_bytes())
    metadata.pop("LSBackgroundOnly", None)
    metadata.update(
        CFBundleDisplayName="ccworks Desktop",
        CFBundleName="ccworks Desktop",
        CFBundleExecutable="ccworks-desktop",
        CFBundleVersion=metadata["CFBundleShortVersionString"],
        NSHighResolutionCapable=True,
        LSMinimumSystemVersion="13.0",
        **settings,
    )
    path.write_bytes(plistlib.dumps(metadata))


if __name__ == "__main__":
    main()
