"""Probe the packaged entry point without reading or changing a saved session."""

import argparse
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("helper", type=Path)
    parser.add_argument("--fresh-chrome", action="store_true")
    args = parser.parse_args()
    helper = str(args.helper.resolve(strict=True))
    release = version("ccworks")
    result = subprocess.run(
        [helper, "--version"], capture_output=True, text=True, check=True, timeout=30
    )
    if result.stderr or result.stdout.strip() != f"ccworks {release}":
        raise RuntimeError("Packaged CLI version check failed")
    result = subprocess.run(
        [helper, "desktop", "check"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "CCWORKS_BROWSER": "invalid-for-smoke-test"},
    )
    expected = {
        "protocol": 1,
        "version": release,
        "browser": "unsupported",
        "sign_in": "unchecked",
    }
    if result.returncode != 1 or result.stderr or json.loads(result.stdout) != expected:
        raise RuntimeError("Packaged desktop check failed")
    print(f"PASS: packaged ccworks {release} CLI and desktop check")

    if args.fresh_chrome:
        from ccworks.paths import session_file

        if session_file(desktop=True, create=False).exists():
            raise RuntimeError(
                "Chrome smoke test requires an account without a saved session"
            )
        result = subprocess.run(
            [helper, "desktop", "check"],
            capture_output=True,
            text=True,
            timeout=45,
            env={**os.environ, "CCWORKS_BROWSER": "chrome"},
        )
        expected.update(browser="ready", sign_in="required")
        if (
            result.returncode != 1
            or result.stderr
            or json.loads(result.stdout) != expected
        ):
            raise RuntimeError(
                "Packaged Playwright driver could not check installed Chrome"
            )
        print("PASS: packaged helper launches installed Chrome")


if __name__ == "__main__":
    main()
