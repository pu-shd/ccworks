#!/usr/bin/env python3
"""Concur's save dialogs, against the mock server.

Both of these blocked a save while `apply-json` reported the row as written.
The detector looked for `.sapMDialog` / `[role='dialog']`; Concur renders
`.sapcnqr-message-dialog` with `role="alertdialog"`, so neither matched:

1. A required-field rejection ("Before you can continue, you must provide valid
   information for: Expense Type") read as a successful save. The row's business
   purpose and comment were reported as set and were not.

2. "Update Other Items?" -- raised whenever a field changes on an expense that
   carries allocations -- left the save uncommitted until answered. ccworks
   clicked Save, recognised nothing, and moved on, so the edit was discarded.
   Observed live: a row kept its previous business purpose across three
   consecutive "successful" writes.

The field read-back cannot catch either, because it reads the pane *before* the
save. Only answering the dialog, and failing the row when it cannot be
answered, closes the gap.
"""
import json
import os
import sys
import threading
import unittest
import urllib.request

from playwright.sync_api import sync_playwright

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("CCWORKS_SKIP_BROWSER_BOOTSTRAP", "1")

from mock_concur_server import MockConcurServer  # noqa: E402
from ccworks.browser_client import ConcurBrowserClient  # noqa: E402

PORT = 8098
BASE_URL = f"http://127.0.0.1:{PORT}"

DEPT = "(25605) ORF-Technical Support"
FUND = "(A0001) General Fund"


class SaveDialogTestCase(unittest.TestCase):
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

    def tx(self, report_name, index):
        with urllib.request.urlopen(f"{BASE_URL}/api/reports") as resp:
            for r in json.loads(resp.read().decode("utf-8")):
                if r["name"] == report_name:
                    return (r.get("transactions") or [])[index - 1]
        self.fail(f"report {report_name!r} not found")


class TestRequiredFieldRejection(SaveDialogTestCase):
    REPORT = "RECEIPTS REQTYPE rejection"

    def setUp(self):
        self.client.create_draft_report(self.REPORT, "save dialogs", "", headless=True)

    def test_rejected_save_fails_the_row_with_concurs_reason(self):
        res = self.client.apply_json_updates(
            report_name=self.REPORT,
            expenses=[{"index": 1, "vendor": "ESHIPGLOBAL INC", "amount": "$23.28",
                       "business_purpose": "should not persist",
                       "comment": "should not persist"}],
            headless=True)
        row = res["results"][0]
        self.assertFalse(row["success"],
                         "a save Concur refused must not be reported as written")
        self.assertIn("save rejected by Concur", row["error"])
        self.assertIn("Expense Type", row["error"],
                      "the row should carry Concur's own reason, not a generic failure")

        self.assertNotEqual("should not persist",
                            self.tx(self.REPORT, 1).get("business_purpose"),
                            "the rejected value must not be on the record")

    def test_supplying_the_missing_field_lets_the_save_through(self):
        res = self.client.apply_json_updates(
            report_name=self.REPORT,
            expenses=[{"index": 2, "vendor": "ESHIPGLOBAL INC", "amount": "$23.28",
                       "expense_type": "Lodging",
                       "business_purpose": "now valid", "comment": "now valid"}],
            headless=True)
        self.assertTrue(res["results"][0]["success"], res["results"][0])
        self.assertEqual("now valid", self.tx(self.REPORT, 2).get("business_purpose"))


class TestUpdateOtherItemsDialog(SaveDialogTestCase):
    REPORT = "RECEIPTS update-other-items"

    def setUp(self):
        self.client.create_draft_report(self.REPORT, "save dialogs", "", headless=True)

    def test_edit_persists_on_an_allocated_expense(self):
        """The live failure: an allocated expense silently kept its old purpose.

        Adding an allocation makes Concur ask whether to propagate the change.
        Until that is answered the save does not commit.
        """
        add = self.client.add_transaction_allocation(
            self.REPORT, 0, DEPT, FUND, None, headless=True)
        self.assertTrue(add["success"], add.get("error"))

        res = self.client.apply_json_updates(
            report_name=self.REPORT,
            expenses=[{"index": 1, "vendor": "ESHIPGLOBAL INC", "amount": "$23.28",
                       "business_purpose": "after allocation",
                       "comment": "after allocation"}],
            headless=True)
        self.assertTrue(res["results"][0]["success"], res["results"][0])
        self.assertEqual("after allocation",
                         self.tx(self.REPORT, 1).get("business_purpose"),
                         "the edit must survive the 'Update Other Items?' dialog")

    def test_unallocated_expense_needs_no_dialog(self):
        """Control: the dialog only appears on allocated expenses."""
        res = self.client.apply_json_updates(
            report_name=self.REPORT,
            expenses=[{"index": 3, "vendor": "ESHIPGLOBAL INC", "amount": "$23.28",
                       "business_purpose": "no allocation here",
                       "comment": "no allocation here"}],
            headless=True)
        self.assertTrue(res["results"][0]["success"], res["results"][0])
        self.assertEqual("no allocation here",
                         self.tx(self.REPORT, 3).get("business_purpose"))


class TestAllocationSaveAcknowledgment(unittest.TestCase):
    """Model the live informational dialog, including asynchronously added actions."""

    def test_delayed_acknowledgment_commits_without_changing_allocations(self):
        client = ConcurBrowserClient.__new__(ConcurBrowserClient)
        with sync_playwright() as playwright, playwright.chromium.launch() as browser:
            page = browser.new_page()
            page.set_content("""
                <input data-nuiexp="field-expenseType" value="Software">
                <button data-nuiexp="exp-save-expense" onclick="showDialog()">Save Expense</button>
                <script>
                window.persisted = false;
                window.allocations = ['existing allocation'];
                function showDialog() {
                    const dialog = document.createElement('div');
                    dialog.setAttribute('role', 'alertdialog');
                    dialog.innerHTML = `<h2>Update Other Items</h2>
                        You've updated the following fields: Business Purpose Comment
                        These changes will not overwrite previously modified allocations.
                        Update them manually.`;
                    document.body.appendChild(dialog);
                    setTimeout(() => {
                        const action = document.createElement('button');
                        action.textContent = 'Acknowledge';
                        action.onclick = () => {
                            window.persisted = true;
                            dialog.remove();
                            document.querySelector('input').remove();
                        };
                        dialog.appendChild(action);
                    }, 250);
                }
                </script>
            """)
            saved, error = client._click_save_expense(page)
            self.assertTrue(saved, error)
            self.assertTrue(page.evaluate('window.persisted'), 'acknowledgment must commit')
            self.assertEqual(['existing allocation'], page.evaluate('window.allocations'))

    def test_unrecognized_acknowledgment_does_not_authorize_propagation(self):
        client = ConcurBrowserClient.__new__(ConcurBrowserClient)
        with sync_playwright() as playwright, playwright.chromium.launch() as browser:
            page = browser.new_page()
            page.set_content("""
                <input data-nuiexp="field-expenseType" value="Software">
                <button data-nuiexp="exp-save-expense" onclick="showDialog()">Save Expense</button>
                <script>
                window.propagated = false;
                function showDialog() {
                    const dialog = document.createElement('div');
                    dialog.setAttribute('role', 'alertdialog');
                    dialog.innerHTML = `<h2>Update Other Items</h2>
                        Apply changes to all allocations?
                        <button onclick="window.propagated = true">Acknowledge</button>`;
                    document.body.appendChild(dialog);
                }
                </script>
            """)
            saved, error = client._click_save_expense(page)
            self.assertFalse(saved, 'an unknown dialog must not be reported as saved')
            self.assertTrue(error, 'an explicit failure reason is required')
            self.assertFalse(page.evaluate('window.propagated'), 'do not guess what acknowledgment does')


if __name__ == "__main__":
    unittest.main()
