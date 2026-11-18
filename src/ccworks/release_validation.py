"""Validate a redacted live-test attestation before a release tag is published."""

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CHECKS = {
    "session_reuse",
    "fields_receipts_allocations",
    "rerun_without_duplicates",
    "three_stage_submission",
    "independent_submitted_status",
    "redacted_events",
}
FIELDS = {
    "schema_version",
    "eligible_from",
    "status",
    "candidate_commit",
    "validated_at",
    "approved_for_release",
    "checks",
}
EVIDENCE = "release/live-validation.json"
WINDOWS_EXCEPTION = "release/windows-v0.8.1-exception.json"
WINDOWS_EXCEPTION_TAG = "v0.8.1"
WINDOWS_EXCEPTION_ARTIFACT = "ccworks-desktop-0.8.1-windows-x64.exe"
WINDOWS_EXCEPTION_CANDIDATE = "476839d83b8389309c27582d1b23fc1ddda4933d"
WINDOWS_EXCEPTION_EVIDENCE = "8c212c0e560371b4df77da678b1a6151f2d45c90"
WINDOWS_INSTALLER_CHECKS = frozenset(
    {
        "authenticode_signatures_and_timestamps",
        "chrome_and_setup_window",
        "invalid_update_signature_rejected",
        "per_user_install_reinstall_uninstall",
        "signed_update_preserves_state",
    }
)
WINDOWS_EXCEPTION_FILES = frozenset(
    {
        WINDOWS_EXCEPTION,
        "pyproject.toml",
        "desktop/ccworks.iss",
        "desktop/test-windows-installer.ps1",
        ".github/workflows/release-validation.yml",
        "src/ccworks/release_validation.py",
        "tests/test_release_validation.py",
    }
)


def validate_evidence(
    data: Any, *, require_live: bool, now: datetime | None = None
) -> None:
    if not isinstance(data, dict) or set(data) != FIELDS:
        raise ValueError("Release evidence has missing or unsupported fields")
    if data["schema_version"] != 1 or type(data["schema_version"]) is not int:
        raise ValueError("Unsupported release-evidence schema")
    try:
        eligible = date.fromisoformat(data["eligible_from"])
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid live-validation date") from error
    if not isinstance(data["status"], str) or data["status"] not in {
        "pending",
        "passed",
        "failed",
    }:
        raise ValueError("Invalid live-validation status")
    if type(data["approved_for_release"]) is not bool:
        raise ValueError("Release approval must be an explicit boolean")
    checks = data["checks"]
    if not isinstance(checks, dict) or set(checks) != CHECKS:
        raise ValueError("Release checklist is incomplete or has unsupported fields")
    if not all(type(value) is bool for value in checks.values()):
        raise ValueError("Checklist results must be explicit booleans")
    commit = data["candidate_commit"]
    if commit is not None and (
        not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit)
    ):
        raise ValueError("Invalid validated candidate commit")
    tested = None
    if data["validated_at"] is not None:
        try:
            value = data["validated_at"]
            if not isinstance(value, str):
                raise ValueError("Invalid timestamp type")
            # Python 3.10 requires an explicit offset instead of the ISO Z form.
            tested = datetime.fromisoformat(
                value[:-1] + "+00:00" if value.endswith("Z") else value
            )
            if tested.tzinfo is None:
                raise ValueError("Missing timezone")
        except (TypeError, ValueError) as error:
            raise ValueError("Invalid live-validation timestamp") from error
    if not require_live:
        return
    if data["status"] != "passed" or not data["approved_for_release"]:
        raise ValueError(
            "Release blocked: live validation and owner approval are pending"
        )
    if not commit or not tested or not all(checks.values()):
        raise ValueError("Release blocked: the live-validation evidence is incomplete")
    try:
        owner_timezone = ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError as error:
        raise ValueError(
            "Release blocked: timezone data is missing; install the pinned development requirements"
        ) from error
    if tested.astimezone(owner_timezone).date() < eligible:
        raise ValueError("Release blocked: validation predates the eligible report")
    if tested > (now or datetime.now(timezone.utc)):
        raise ValueError("Release blocked: validation timestamp is in the future")


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, stderr=subprocess.DEVNULL
    )


def _without_project_version(text: str) -> str:
    normalized, count = re.subn(
        r'(?ms)(^\[project\]\n(?:(?!^\[).)*?^version = )"[^"]+"',
        lambda match: match.group(1) + '"VALIDATED"',
        text,
        count=1,
    )
    if count != 1:
        raise ValueError("Release blocked: package version cannot be verified")
    return normalized


def validate_candidate(
    root: Path,
    candidate: str,
    *,
    allowed_changes: frozenset[str] = frozenset({EVIDENCE, "pyproject.toml"}),
) -> None:
    """Only the evidence and a version-only packaging change may follow the test."""
    try:
        subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", candidate, "HEAD"],
            check=True,
            capture_output=True,
        )
        changed = set(_git(root, "diff", "--name-only", candidate, "HEAD").splitlines())
        if changed - allowed_changes:
            raise ValueError("Release blocked: code changed after live validation")
        if "pyproject.toml" in changed:
            before = _git(root, "show", candidate + ":pyproject.toml")
            after = (root / "pyproject.toml").read_text()
            # No dependencies, build settings or metadata besides version may change.
            if _without_project_version(before) != _without_project_version(after):
                raise ValueError(
                    "Release blocked: packaging changed after live validation"
                )
        if _git(root, "status", "--porcelain", "--untracked-files=all"):
            raise ValueError(
                "Release blocked: the working tree has uncommitted changes"
            )
    except subprocess.CalledProcessError as error:
        raise ValueError(
            "Release blocked: validated commit cannot be verified"
        ) from error


def validate_windows_exception(
    root: Path, tag: str | None, evidence: Any, *, artifact: Path | None = None
) -> None:
    """Verify the owner's one-time authorization against the frozen draft test."""
    if tag != WINDOWS_EXCEPTION_TAG:
        raise ValueError(
            "The Windows x64 release exception is restricted to tag v0.8.1"
        )
    validate_evidence(evidence, require_live=False)
    if (
        evidence["candidate_commit"] != WINDOWS_EXCEPTION_CANDIDATE
        or evidence["status"] != "pending"
        or evidence["approved_for_release"]
        or not evidence["validated_at"]
        or {name for name, passed in evidence["checks"].items() if passed}
        != {"session_reuse", "fields_receipts_allocations"}
    ):
        raise ValueError(
            "The Windows x64 release exception requires the recorded draft evidence"
        )
    try:
        frozen = json.loads(
            _git(root, "show", f"{WINDOWS_EXCEPTION_EVIDENCE}:{EVIDENCE}")
        )
    except subprocess.CalledProcessError as error:
        raise ValueError("The recorded draft evidence cannot be verified") from error
    if evidence != frozen:
        raise ValueError("The recorded draft evidence changed")
    record = json.loads((root / WINDOWS_EXCEPTION).read_text())
    digest = record.get("sha256") if isinstance(record, dict) else None
    expected = {
        "tag": tag,
        "platform": "windows-x64",
        "approved_for_release": True,
        "candidate_commit": WINDOWS_EXCEPTION_CANDIDATE,
        "artifact": WINDOWS_EXCEPTION_ARTIFACT,
        "sha256": digest,
        "windows_installer_checks": sorted(WINDOWS_INSTALLER_CHECKS),
        "untested_checks": sorted(
            name for name, passed in evidence["checks"].items() if not passed
        ),
    }
    if (
        record != expected
        or record["approved_for_release"] is not True
        or not isinstance(digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", digest)
    ):
        raise ValueError("The Windows x64 release exception record is invalid")
    validate_candidate(
        root, WINDOWS_EXCEPTION_EVIDENCE, allowed_changes=WINDOWS_EXCEPTION_FILES
    )
    if artifact is not None:
        if artifact.name != WINDOWS_EXCEPTION_ARTIFACT or artifact.is_symlink():
            raise ValueError(
                "The artifact is outside the Windows x64 release exception"
            )
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != digest:
            raise ValueError("The Windows x64 release artifact digest differs")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-live", action="store_true")
    parser.add_argument("--allow-windows-v081-exception", action="store_true")
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--tag")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    try:
        if args.allow_windows_v081_exception and not args.require_live:
            raise ValueError("The release exception requires --require-live")
        if args.artifact and not args.allow_windows_v081_exception:
            raise ValueError("--artifact requires the Windows x64 release exception")
        data = json.loads((root / EVIDENCE).read_text())
        if args.allow_windows_v081_exception:
            validate_windows_exception(root, args.tag, data, artifact=args.artifact)
        else:
            validate_evidence(data, require_live=args.require_live)
            if args.require_live:
                validate_candidate(root, data["candidate_commit"])
        if args.tag:
            version = re.search(
                r'^version = "([^"]+)"$', (root / "pyproject.toml").read_text(), re.M
            )
            if version is None or args.tag != "v" + version.group(1):
                raise ValueError("Release blocked: tag does not match package version")
    except (OSError, ValueError) as error:
        message = (
            str(error)
            if isinstance(error, ValueError)
            and not isinstance(error, json.JSONDecodeError)
            else "Release evidence is missing or invalid"
        )
        print("FAIL: " + message)
        return 1
    if args.allow_windows_v081_exception:
        print("PASS: owner-approved Windows x64 v0.8.1 release exception verified")
        return 0
    print(
        "PASS: live validation and release approval verified"
        if args.require_live
        else "PASS: release-evidence schema is valid; live approval remains a separate gate"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
