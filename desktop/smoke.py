"""Probe the packaged entry point without reading or changing a saved session."""

import argparse
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess

# Enough to diagnose a failure without flooding the CI log.
OUTPUT_LIMIT = 2000


def _clip(text) -> str:
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    text = (text or "").strip()
    if len(text) > OUTPUT_LIMIT:
        return f"{text[:OUTPUT_LIMIT]}... [{len(text) - OUTPUT_LIMIT} more characters]"
    return text or "<empty>"


def _failure(message: str, detail: str, stdout, stderr, expected=None) -> RuntimeError:
    lines = [
        f"{message} ({detail})",
        f"stdout: {_clip(stdout)}",
        f"stderr: {_clip(stderr)}",
    ]
    if expected is not None:
        lines.append(f"expected: {json.dumps(expected, sort_keys=True)}")
    return RuntimeError("\n".join(lines))


def run_check(
    cmd, env, timeout, message, expected_code, expected_json=None, expected_text=None
):
    """Run one helper check; on any mismatch, raise with everything it printed."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env
        )
    except subprocess.TimeoutExpired as e:
        raise _failure(
            message, f"timed out after {timeout}s", e.stdout, e.stderr
        ) from None
    try:
        parsed = json.loads(result.stdout) if expected_json is not None else None
    except json.JSONDecodeError:
        parsed = None
    ok = (
        result.returncode == expected_code
        and not result.stderr
        and (expected_json is None or parsed == expected_json)
        and (expected_text is None or result.stdout.strip() == expected_text)
    )
    if not ok:
        raise _failure(
            message,
            f"exit code {result.returncode}",
            result.stdout,
            result.stderr,
            expected_json if expected_json is not None else expected_text,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("helper", type=Path)
    parser.add_argument("--fresh-chrome", action="store_true")
    args = parser.parse_args()
    helper = str(args.helper.resolve(strict=True))
    release = version("ccworks")
    run_check(
        [helper, "--version"],
        env=None,
        timeout=30,
        message="Packaged CLI version check failed",
        expected_code=0,
        expected_text=f"ccworks {release}",
    )
    expected = {
        "protocol": 1,
        "version": release,
        "browser": "unsupported",
        "sign_in": "unchecked",
    }
    run_check(
        [helper, "desktop", "check"],
        env={**os.environ, "CCWORKS_BROWSER": "invalid-for-smoke-test"},
        timeout=30,
        message="Packaged desktop check failed",
        expected_code=1,
        expected_json=expected,
    )
    print(f"PASS: packaged ccworks {release} CLI and desktop check")

    if args.fresh_chrome:
        from ccworks.paths import session_file

        if session_file(desktop=True, create=False).exists():
            raise RuntimeError(
                "Chrome smoke test requires an account without a saved session"
            )
        run_check(
            [helper, "desktop", "check"],
            env={**os.environ, "CCWORKS_BROWSER": "chrome"},
            timeout=45,
            message="Packaged Playwright driver could not check installed Chrome",
            expected_code=1,
            expected_json={**expected, "browser": "ready", "sign_in": "required"},
        )
        print("PASS: packaged helper launches installed Chrome")


if __name__ == "__main__":
    main()
