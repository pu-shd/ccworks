#!/usr/bin/env python3
"""Concur's report-totals confirmation, against the mock server.

`report submit` timed out on a live report and left it in draft while the CLI
was one code path away from printing "[SUCCESS] Successfully submitted".

Submitting is two clicks, not one. The toolbar's Submit Report button opens a
report-totals confirmation rendered as a full-screen overlay, and the report
stays in draft until the dialog's own button is clicked. The toolbar button
remains in the DOM behind that overlay, still matching
`button:has-text('Submit Report')`, so the page-wide locator taking `.first`
resolved to the covered button:

    - locator resolved to <button aria-label="Submit Report"
      data-nuiexp="reportActionButtons.submitButton">
    - <div role="dialog" data-nuiexp="report-totals-modal"> ... subtree
      intercepts pointer events
    - retrying click action

It retried for 30s against a button that could never be clicked, then raised.
The confirmation has to be scoped to inside the dialog. `submit_report` also
never called `_dismiss_modals`, so an unrelated timeline overlay could block
the first click the same way.

The old code additionally returned `{"success": True, "message": "Submit
clicked, verification pending"}` whenever it could not see a success message,
and accepted `page.url.endswith("/nui/expense")` as proof -- which is the URL
the run *starts* on. An unsubmitted report could therefore be reported as
submitted; only a dialog that is gone, or Concur's own success text, is
evidence.
"""
import json
import os
import sys
import threading
import unittest
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("CCWORKS_SKIP_BROWSER_BOOTSTRAP", "1")

from mock_concur_server import MockConcurServer  # noqa: E402
from ccworks.browser_client import ConcurBrowserClient  # noqa: E402

PORT = 8099
BASE_URL = f"http://127.0.0.1:{PORT}"


class SubmitTestCase(unittest.TestCase):
    server = None
    client = None

    @classmethod
    def setUpClass(cls):
        cls.server = MockConcurServer(host="127.0.0.1", port=PORT)
        cls.thread = threading.Thread(target=cls.server.start, daemon=True)
        cls.thread.start()
        cls.client = ConcurBrowserClient(base_url=BASE_URL)
        cls.client.session_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "concur_session_mock.json"
        )
        if not os.path.exists(cls.client.session_file):
            with open(cls.client.session_file, "w") as f:
                f.write('{"cookies": [], "origins": []}')

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.stop()
        except Exception:
            pass

    def report(self, name):
        with urllib.request.urlopen(f"{BASE_URL}/api/reports") as resp:
            for r in json.loads(resp.read().decode("utf-8")):
                if r["name"] == name:
                    return r
        self.fail(f"report {name!r} not found")

    def post(self, path, payload):
        req = urllib.request.Request(
            f"{BASE_URL}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def reconcile_every_row(self, name):
        """Concur disables Submit until every line item is reconciled."""
        rows = self.report(name).get("transactions") or []
        for i in range(len(rows)):
            self.post("/api/reports/reconcile_transaction", {
                "report_name": name,
                "index": i,
                "expense_type": "Software (OIT use only)",
                "business_purpose": "submit test",
                "comment": "submit test",
            })


class TestReportTotalsConfirmation(SubmitTestCase):
    REPORT = "SUBMIT totals confirmation"

    def setUp(self):
        self.client.create_draft_report(self.REPORT, "submit", "", headless=True)
        self.reconcile_every_row(self.REPORT)

    def test_submit_answers_the_totals_dialog_and_the_report_leaves_draft(self):
        res = self.client.submit_report(report_name=self.REPORT, headless=True)

        self.assertTrue(res["success"], res)
        self.assertEqual("Submitted", self.report(self.REPORT)["status"],
                         "the report must actually leave draft, not merely be clicked")

    def test_success_is_only_claimed_once_the_dialog_is_answered(self):
        res = self.client.submit_report(report_name=self.REPORT, headless=True)

        # The dialog is the only thing that submits, so a run that reports
        # success must have gone through it. This is the guard against the old
        # "verification pending" success, which passed on an unsubmitted report.
        self.assertTrue(res.get("confirmed"),
                        "success without answering the confirmation is not evidence "
                        "of submission")
        self.assertNotIn("pending", res["message"].lower())


class TestSubmitBlocked(SubmitTestCase):
    REPORT = "SUBMIT blocked while unreconciled"

    def setUp(self):
        self.client.create_draft_report(self.REPORT, "submit", "", headless=True)
        # deliberately NOT reconciled -- Concur greys out Submit

    def test_disabled_submit_button_raises_and_leaves_the_report_in_draft(self):
        with self.assertRaises(Exception) as ctx:
            self.client.submit_report(report_name=self.REPORT, headless=True)

        self.assertIn("disabled", str(ctx.exception).lower())
        self.assertEqual("Draft", self.report(self.REPORT)["status"],
                         "a refused submit must not change the report's status")


if __name__ == "__main__":
    unittest.main(verbosity=2)
