"""A passing mock suite cannot substitute for dated, approved live evidence."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from ccworks import release_validation
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

    def test_macos_exception_allows_only_release_tooling_changes(self):
        exception = self.root / release_validation.MACOS_EXCEPTION
        exception.write_text("{}\n")
        self.commit()
        allowed = release_validation.MACOS_EXCEPTION_FILES
        validate_candidate(self.root, self.candidate, allowed_changes=allowed)
        (self.root / "app.py").write_text("VALUE = 2\n")
        self.commit()
        with self.assertRaisesRegex(ValueError, "changed after"):
            validate_candidate(self.root, self.candidate, allowed_changes=allowed)


class TestMacOSReleaseException(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "release").mkdir()
        self.evidence = passed()
        self.evidence.update(
            candidate_commit="476839d83b8389309c27582d1b23fc1ddda4933d",
            status="pending",
            approved_for_release=False,
        )
        checks = {"session_reuse", "fields_receipts_allocations"}
        self.evidence["checks"] = {key: key in checks for key in CHECKS}
        self.artifact = self.root / "ccworks-desktop-0.8.0-arm64.dmg"
        self.artifact.write_bytes(b"fixture disk image")
        self.record = {
            "tag": "v0.8.0",
            "platform": "macos-arm64",
            "approved_for_release": True,
            "candidate_commit": self.evidence["candidate_commit"],
            "artifact": self.artifact.name,
            "sha256": hashlib.sha256(self.artifact.read_bytes()).hexdigest(),
            "untested_checks": sorted(CHECKS - checks),
        }
        self.manifest = self.root / "release/macos-v0.8.0-exception.json"
        self.addCleanup(patch.stopall)
        patch.object(release_validation, "validate_candidate").start()
        patch.object(
            release_validation, "_git", return_value=json.dumps(self.evidence)
        ).start()

    def verify(self, tag="v0.8.0", artifact=None):
        self.manifest.write_text(json.dumps(self.record))
        release_validation.validate_macos_exception(
            self.root, tag, self.evidence, artifact=artifact
        )

    def test_exception_approves_only_the_named_mac_artifact(self):
        self.verify(artifact=self.artifact)
        with self.assertRaisesRegex(ValueError, "blocked"):
            validate_evidence(self.evidence, require_live=True)
        self.artifact.write_bytes(b"replaced disk image")
        with self.assertRaisesRegex(ValueError, "digest"):
            self.verify(artifact=self.artifact)

    def test_other_release_tags_and_unauthorized_records_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "tag"):
            self.verify(tag="v0.8.1")
        for key, value in (
            ("platform", "windows-x64"),
            ("artifact", "unrelated.dmg"),
            ("candidate_commit", "f" * 40),
            ("approved_for_release", False),
            ("untested_checks", []),
        ):
            with self.subTest(field=key):
                before = self.record[key]
                self.record[key] = value
                with self.assertRaisesRegex(ValueError, "exception"):
                    self.verify()
                self.record[key] = before

    def test_draft_evidence_cannot_change_after_the_test(self):
        with patch.object(release_validation, "_git", return_value="{}"):
            with self.assertRaisesRegex(ValueError, "evidence"):
                self.verify()
