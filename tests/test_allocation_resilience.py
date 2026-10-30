"""Allocation control compatibility against a real browser and local backend."""

import json
from pathlib import Path
import tempfile
import unittest
import urllib.request

from ccworks.browser_client import ConcurBrowserClient
from tests.mock_concur_server import MockConcurServer


class TestSALAllocationControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = MockConcurServer(host="127.0.0.1", port=8102)
        cls.server.start()
        cls.base_url = "http://127.0.0.1:8102"

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        session = Path(self.tmp.name) / "session.json"
        session.write_text('{"cookies": [], "origins": []}')
        self.client = ConcurBrowserClient(
            session_file=str(session), base_url=self.base_url
        )
        self.name = "SAL allocation " + self._testMethodName
        request = urllib.request.Request(
            self.base_url + "/api/reports",
            data=json.dumps(
                {"name": self.name, "mock_options": {"sal_allocations": True}}
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(201, response.status)

    def allocations(self):
        with urllib.request.urlopen(self.base_url + "/api/reports") as response:
            reports = json.load(response)
        report = next(report for report in reports if report["name"] == self.name)
        return report["transactions"][0].get("allocations", [])

    def test_sal_reader_uses_its_dialog_and_ignores_headers_defaults_and_hidden_empty_state(
        self,
    ):
        with self.client._browser_page(headless=True) as page:
            page.set_content("""
              <div class="sapMListUl"><div role="row">Unrelated background content</div></div>
              <div role="dialog" data-nuiexp="sal-dialog-allocations">
                <section>Default Allocation: 25605 A0006 100%</section>
                <div style="display:none">No Allocations</div>
                <div class="sapcnqr-data-grid-list__row" role="row">
                  <div role="columnheader">Department</div><div role="columnheader">Fund</div>
                </div>
                <div class="sapcnqr-data-grid-list__row" role="row" style="display:contents">
                  <div>25605</div><div>A0001</div>
                  <input data-nuiexp="spend-common__amount-input" value="100.0000" aria-label="Percentage">
                </div>
              </div>
            """)
            rows = self.client._read_allocations_from_modal(page)
        self.assertEqual(1, len(rows))
        self.assertIn("25605", rows[0]["raw_text"])
        self.assertIn("A0001", rows[0]["raw_text"])
        self.assertNotIn("A0006", rows[0]["raw_text"])
        self.assertEqual("100.0000", rows[0]["allocation_input"])

    def test_reader_waits_for_sal_data_instead_of_treating_loading_as_empty(self):
        with self.client._browser_page(headless=True) as page:
            page.set_content("""
              <div data-nuiexp="sal-dialog-allocations">
                <button data-nuiexp="sal-btn-buttonbar-add">Add</button>
                <span data-nuiexp="it-amount">Expense summary</span>
                <div id="async-rows"></div>
              </div>
              <script>
                setTimeout(() => {
                  document.getElementById('async-rows').innerHTML =
                    '<div role="row">25605 A0001<input data-nuiexp="spend-common__amount-input" value="100"></div>';
                }, 300);
              </script>
            """)
            rows = self.client._read_allocations_from_modal(page)
        self.assertEqual(1, len(rows), "loading must not be reported as empty")
        self.assertIn("A0001", rows[0]["raw_text"])

    def test_sal_add_and_clear_persist_and_verify_the_requested_codes(self):
        added = self.client.add_transaction_allocation(
            self.name, 0, "25605", "A0001", "P999", headless=True
        )
        self.assertTrue(added["success"], added)
        rows = self.allocations()
        self.assertEqual(1, len(rows))
        self.assertIn("25605", rows[0]["custom6"])
        self.assertIn("A0001", rows[0]["custom7"])
        self.assertIn("P999", rows[0]["custom8"])
        cleared = self.client.remove_transaction_allocations(
            self.name, 0, headless=True
        )
        self.assertTrue(cleared["success"], cleared)
        self.assertEqual(1, cleared["removed"])
        self.assertEqual([], self.allocations())
