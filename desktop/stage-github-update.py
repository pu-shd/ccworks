"""Stage a GitHub Release appcast from a final artifact and its Ed25519 signature."""

import argparse
import base64
from pathlib import Path
import re
import tempfile
from xml.etree import ElementTree as ET

from ccworks.update_feed import appcast_name, github_release_base

SPARKLE = "http://www.andymatuschak.org/xml-namespaces/sparkle"
ET.register_namespace("sparkle", SPARKLE)


def sparkle(name: str) -> str:
    return f"{{{SPARKLE}}}{name}"


def stage_appcast(
    asset: Path, signature: str, output: Path, repository: str | None = None
) -> Path:
    match = re.fullmatch(
        r"ccworks-desktop-([0-9]+\.[0-9]+\.[0-9]+)-(arm64\.dmg|x86_64\.dmg|windows-x64\.exe)",
        asset.name,
    )
    if match is None or not asset.is_file() or asset.is_symlink():
        raise ValueError("Use a final versioned ccworks DMG or Windows installer.")
    length = asset.stat().st_size
    if length < 1:
        raise ValueError("The release asset is empty.")
    try:
        if len(base64.b64decode(signature, validate=True)) != 64:
            raise ValueError
    except ValueError:
        raise ValueError(
            "Use the 64-byte base64 Ed25519 signature of the final asset."
        ) from None

    version, suffix = match.groups()
    platform, extension = suffix.split(".")
    windows = extension == "exe"
    channel = platform if windows else f"macos-{platform}"
    releases = github_release_base(repository)

    feed = ET.Element("rss", version="2.0")
    stream = ET.SubElement(feed, "channel")
    ET.SubElement(stream, "title").text = "ccworks Desktop updates"
    ET.SubElement(
        stream, "link"
    ).text = f"{releases}/latest/download/{appcast_name(channel)}"
    ET.SubElement(stream, "description").text = "Stable ccworks Desktop releases"
    item = ET.SubElement(stream, "item")
    ET.SubElement(item, "title").text = f"ccworks Desktop {version}"
    ET.SubElement(item, "link").text = f"{releases}/tag/v{version}"
    ET.SubElement(item, sparkle("version")).text = version
    ET.SubElement(item, sparkle("shortVersionString")).text = version
    ET.SubElement(item, sparkle("minimumSystemVersion")).text = (
        "10.0.22000" if windows else "13.0.0"
    )
    if platform == "arm64":
        ET.SubElement(item, sparkle("hardwareRequirements")).text = "arm64"
    enclosure = {
        "url": f"{releases}/download/v{version}/{asset.name}",
        "length": str(length),
        "type": "application/octet-stream",
        sparkle("os"): "windows-x64" if windows else "macos",
        sparkle("edSignature"): signature,
    }
    if windows:
        enclosure[sparkle("installerArguments")] = "/SILENT /SP- /NORESTART"
    ET.SubElement(item, "enclosure", enclosure)
    ET.indent(feed)
    output.mkdir(parents=True, exist_ok=True)
    target = output / appcast_name(channel)
    with tempfile.NamedTemporaryFile(
        dir=output, prefix=".appcast-", delete=False
    ) as file:
        staging = Path(file.name)
        try:
            ET.ElementTree(feed).write(file, encoding="utf-8", xml_declaration=True)
            file.close()
            staging.replace(target)
        finally:
            staging.unlink(missing_ok=True)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("asset", type=Path)
    parser.add_argument("--signature", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        target = stage_appcast(args.asset, args.signature, args.output)
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")
    print(f"Staged {target}")


if __name__ == "__main__":
    main()
