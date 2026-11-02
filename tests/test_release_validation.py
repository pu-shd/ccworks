"""A passing mock suite cannot substitute for dated, approved live evidence."""

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from ccworks.release_validation import CHECKS, validate_candidate, validate_evidence


def passed():
    return {
        "schema_version": 1,
        "eligible_from": "2026-12-01",
        "status": "passed",
        "candidate_commit": "a" * 40,
        "validated_at": "2026-12-01T12:00:00-05:00",
        "approved_for_release": True,
        "checks": {key: True for key in CHECKS},
    }


class TestLiveReleaseEvidence(unittest.TestCase):
    NOW = datetime(2026, 12, 2, tzinfo=timezone.utc)

    def test_pending_manifest_is_valid_for_development_but_blocks_release(self):
        path = Path(__file__).resolve().parents[1] / "release/live-validation.json"
        data = json.loads(path.read_text())
        validate_evidence(data, require_live=False)
        # The real manifest may legitimately become passed after the live test.
        # Keep this negative test independent of its eventual approval state.
        data = {**data, "status": "pending", "approved_for_release": False}
        with self.assertRaisesRegex(ValueError, "blocked"):
            validate_evidence(data, require_live=True, now=self.NOW)

    def test_only_complete_approved_live_evidence_can_pass(self):
        validate_evidence(passed(), require_live=True, now=self.NOW)
        mutations = {
            "status": "pending",
            "approved_for_release": False,
            "candidate_commit": None,
            "validated_at": None,
            "checks": {key: False for key in CHECKS},
        }
        for key, value in mutations.items():
            with self.subTest(field=key), self.assertRaises(ValueError):
                data = passed()
                data[key] = value
                validate_evidence(data, require_live=True, now=self.NOW)

    def test_calendar_date_uses_the_report_owners_timezone(self):
        for timestamp in ("2026-11-30T23:59:59-05:00", "2026-12-01T02:00:00+00:00"):
            with (
                self.subTest(timestamp=timestamp),
                self.assertRaisesRegex(ValueError, "predates"),
            ):
                data = passed()
                data["validated_at"] = timestamp
                validate_evidence(data, require_live=True, now=self.NOW)
        data = passed()
        data["validated_at"] = "2026-12-03T12:00:00-05:00"
        with self.assertRaisesRegex(ValueError, "future"):
            validate_evidence(data, require_live=True, now=self.NOW)

    def test_utc_z_timestamp_is_portable_to_supported_python_versions(self):
        data = passed()
        data["validated_at"] = "2026-12-01T17:00:00Z"
        validate_evidence(data, require_live=True, now=self.NOW)

    def test_missing_timezone_data_is_an_explicit_configuration_failure(self):
        with patch(
            "ccworks.release_validation.ZoneInfo", side_effect=ZoneInfoNotFoundError
        ):
            with self.assertRaisesRegex(ValueError, "timezone data"):
                validate_evidence(passed(), require_live=True, now=self.NOW)

    def test_untyped_extra_or_personal_evidence_fields_are_rejected(self):
        invalid = [[], {**passed(), "report_name": "private"}]
        for key, value in (
            ("approved_for_release", "yes"),
            ("schema_version", True),
            ("candidate_commit", "not-a-commit"),
            ("status", []),
            ("validated_at", "2026-12-01"),
        ):
            data = passed()
            data[key] = value
            invalid.append(data)
        for data in invalid:
            with (
                self.subTest(value_type=type(data).__name__),
                self.assertRaises(ValueError),
            ):
                validate_evidence(data, require_live=True, now=self.NOW)


class TestValidatedCodeIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git("init", "-q")
        (self.root / "release").mkdir()
        (self.root / "release/live-validation.json").write_text("{}\n")
        (self.root / "pyproject.toml").write_text('[project]\nversion = "0.7.1"\n')
        (self.root / "app.py").write_text("VALUE = 1\n")
        self.commit()
        self.candidate = self.git("rev-parse", "HEAD")

    def git(self, *args):
        return subprocess.check_output(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=Synthetic Test",
                "-c",
                "user.email=synthetic@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def test_evidence_and_version_only_promotion_are_allowed(self):
        (self.root / "release/live-validation.json").write_text('{"status":"passed"}\n')
        (self.root / "pyproject.toml").write_text('[project]\nversion = "0.8.0"\n')
        self.commit()
        validate_candidate(self.root, self.candidate)

    def test_code_packaging_and_dirty_changes_require_revalidation(self):
        for path, text in (
            ("app.py", "VALUE = 2\n"),
            (
                "pyproject.toml",
                '[project]\nversion = "0.8.0"\ndependencies = ["new"]\n',
            ),
        ):
            with self.subTest(path=path):
                self.git("reset", "--hard", self.candidate)
                (self.root / path).write_text(text)
                self.commit()
                with self.assertRaisesRegex(ValueError, "changed after"):
                    validate_candidate(self.root, self.candidate)
        self.git("reset", "--hard", self.candidate)
        (self.root / "app.py").write_text("VALUE = 3\n")
        with self.assertRaisesRegex(ValueError, "uncommitted"):
            validate_candidate(self.root, self.candidate)
