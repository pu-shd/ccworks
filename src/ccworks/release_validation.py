"""Validate a redacted live-test attestation before a release tag is published."""

import argparse
from datetime import date, datetime, timezone
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


def validate_candidate(root: Path, candidate: str) -> None:
    """Only the evidence and a version-only packaging change may follow the test."""
    try:
        subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", candidate, "HEAD"],
            check=True,
            capture_output=True,
        )
        changed = set(_git(root, "diff", "--name-only", candidate, "HEAD").splitlines())
        if changed - {EVIDENCE, "pyproject.toml"}:
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-live", action="store_true")
    parser.add_argument("--tag")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    try:
        data = json.loads((root / EVIDENCE).read_text())
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
    print(
        "PASS: live validation and release approval verified"
        if args.require_live
        else "PASS: release-evidence schema is valid; live approval remains a separate gate"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
