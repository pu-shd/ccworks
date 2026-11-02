"""Reproduce the three-stage submission UI without production credentials."""

import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.request
from unittest.mock import patch

from ccworks.browser_client import ConcurBrowserClient
from ccworks.submission import ReportSubmission
from tests.mock_concur_server import MockConcurServer

os.environ.setdefault("CCWORKS_SKIP_BROWSER_BOOTSTRAP", "1")


class TestSubmissionResilience(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = MockConcurServer(host="127.0.0.1", port=8107)
        cls.server.start()
        cls.url = "http://127.0.0.1:8107"

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        session = Path(self.tmp.name) / "session.json"
        session.write_text('{"cookies": [], "origins": []}')
        self.client = ConcurBrowserClient(session_file=str(session), base_url=self.url)
        self.name = "SUBMIT " + self._testMethodName

    def seed(self, **options):
        request = urllib.request.Request(
            self.url + "/api/reports",
            data=json.dumps(
                {
                    "name": self.name,
                    "mock_options": {"native_submit_flow": True, **options},
                    "transactions": [
                        {
                            "id": "synthetic-1",
                            "merchant": "Synthetic Vendor",
                            "amount": "$10.00",
                            "expense_type": "Software (OIT use only)",
                            "business_purpose": "Synthetic business expense",
                            "comment": "Synthetic test",
                            "reconciled": True,
                        }
                    ],
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(201, response.status)

    def report(self):
        with urllib.request.urlopen(self.url + "/api/reports") as response:
            return next(r for r in json.load(response) if r["name"] == self.name)

    def test_delayed_proceed_then_certification_persists_submission(self):
        self.seed(submit_action_delay_ms=300)
        result = self.client.submit_report(self.name)
        self.assertTrue(result["success"], result)
        self.assertTrue(result["verified"], result)
        self.assertEqual("Submitted", self.report()["status"])
        self.assertEqual(1, self.report()["submit_count"])

    def test_dialog_closure_without_server_submission_is_not_success(self):
        self.seed(drop_submission=True)
        result = self.client.submit_report(self.name)
        self.assertFalse(result["success"], result)
        self.assertEqual("Draft", self.report()["status"])
        self.assertEqual(0, self.report().get("submit_count", 0))

    def test_status_hydrates_after_the_named_report_card(self):
        self.seed(status_delay_ms=300)
        result = self.client.submit_report(self.name)
        self.assertTrue(result["success"], result)
        self.assertEqual(1, self.report()["submit_count"])

    def test_rerun_and_lost_acknowledgment_never_duplicate_submission(self):
        self.seed(lose_submit_ack=True)
        first = self.client.submit_report(self.name)
        self.assertTrue(first["success"], first)
        second = self.client.submit_report(self.name)
        self.assertTrue(second["success"], second)
        self.assertEqual("skipped", second["status"])
        self.assertEqual(1, self.report()["submit_count"])

    def test_timeout_after_final_commit_is_verified_without_retrying(self):
        self.seed()
        original = ReportSubmission._record

        def interrupted(operation, phase, outcome):
            if phase == "certification" and outcome == "acknowledged":
                raise TimeoutError("synthetic lost acknowledgment")
            return original(operation, phase, outcome)

        with patch.object(ReportSubmission, "_record", interrupted):
            result = self.client.submit_report(self.name)
        self.assertTrue(result["success"], result)
        self.assertFalse(result["confirmed"], result)
        self.assertTrue(result["verified"], result)
        self.assertEqual(1, self.report()["submit_count"])

    def test_unreadable_verification_is_unknown_and_rerun_checks_saved_status(self):
        self.seed()
        with patch.object(ReportSubmission, "_verify", side_effect=TimeoutError):
            result = self.client.submit_report(self.name)
        self.assertFalse(result["success"], result)
        self.assertEqual("unknown", result["status"])
        repeated = self.client.submit_report(self.name)
        self.assertEqual("skipped", repeated["status"])
        self.assertEqual(1, self.report()["submit_count"])

    def test_short_session_stops_before_any_browser_action(self):
        self.seed()
        with patch.object(self.client, "_session_minutes_left", return_value=0.5):
            with self.assertRaisesRegex(RuntimeError, "reauthenticate"):
                self.client.submit_report(self.name)
        self.assertEqual(0, self.report().get("submit_count", 0))

    def test_ambiguous_report_name_is_refused_without_submission(self):
        self.seed()
        self.seed()
        with self.assertRaisesRegex(RuntimeError, "preflight"):
            self.client.submit_report(self.name)
        self.assertEqual(0, self.report().get("submit_count", 0))

    def test_partial_report_name_cannot_submit_a_different_title(self):
        self.seed()
        with self.assertRaisesRegex(RuntimeError, "preflight"):
            self.client.submit_report(self.name[:-8])
        self.assertEqual("Draft", self.report()["status"])
        self.assertEqual(0, self.report().get("submit_count", 0))
