"""Create the allowlisted source archive used by the disposable Windows build VM."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, ZipFile

ROOT_FILES = (
    "pyproject.toml",
    "README.md",
    "LICENSE.md",
    "requirements.lock",
    "requirements-desktop.lock",
)
BUILD_FILES = (
    "build-windows.ps1",
    "ccworks.iss",
    "ccworks.spec",
    "cli_entry.py",
    "gui_entry.py",
    "smoke.py",
    "test-windows-installer.ps1",
    "test-windows-signing.ps1",
    "windows-signing.ps1",
    "windows-build-payload.py",
    "windows-update-data.py",
)
TEST_FILES = ("__init__.py", "test_desktop.py", "test_windows_update.py")
CLIENT_FIELDS = {"Endpoint", "CodeSigningAccountName", "CertificateProfileName"}


def payload(root: Path, path: Path, *, metadata: Path | None = None) -> None:
    entries = [root / file for file in ROOT_FILES]
    entries += [root / "desktop" / file for file in BUILD_FILES]
    entries += [root / "tests" / file for file in TEST_FILES]
    entries += sorted((root / "src" / "ccworks").glob("*.py"))
    client = None
    if metadata is not None:
        client = json.loads(metadata.read_bytes())
        if (
            not isinstance(client, dict)
            or set(client) != CLIENT_FIELDS
            or not all(isinstance(value, str) and value for value in client.values())
        ):
            raise ValueError(
                "Expected only the Azure endpoint, account and profile in the client metadata."
            )
        endpoint = urlsplit(client["Endpoint"])
        if (
            endpoint.scheme != "https"
            or not (endpoint.hostname or "").endswith(".codesigning.azure.net")
            or endpoint.username is not None
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError(
                "The Azure client metadata must use a regional Artifact Signing endpoint."
            )
    for file in entries:
        if not file.is_file() or file.is_symlink():
            raise ValueError("A Windows build input is missing or is a symlink.")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Write the staged archive to a regular file.")
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for file in entries:
            archive.write(file, file.relative_to(root).as_posix())
        if client:
            archive.writestr(
                "artifact-signing-metadata.json", json.dumps(client, indent=2)
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--metadata", type=Path)
    args = parser.parse_args()
    payload(Path(__file__).resolve().parents[1], args.output, metadata=args.metadata)
    print(f"Prepared Windows build source at {args.output}")


if __name__ == "__main__":
    main()
