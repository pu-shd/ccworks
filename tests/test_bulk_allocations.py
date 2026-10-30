"""Bulk allocations exercised through Chromium and a persisted local backend."""

import json
from pathlib import Path
import tempfile
import unittest
import urllib.request
from unittest.mock import patch

from ccworks.browser_client import ConcurBrowserClient
from tests.mock_concur_server import MockConcurServer


class TestBulkAllocations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = MockConcurServer(host="127.0.0.1", port=8103)
        cls.server.start()
        cls.url = "http://127.0.0.1:8103"

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        session = Path(self.tmp.name) / "session.json"
        session.write_text('{"cookies": [], "origins": []}')
        self.client = ConcurBrowserClient(session_file=str(session), base_url=self.url)
        self.name = "BATCHTYPES " + self._testMethodName

    def seed(self, options=None, transactions=None):
        data = {"name": self.name, "mock_options": {"sal_allocations": True}}
        data["mock_options"].update(options or {})
        if transactions is not None:
            data["transactions"] = transactions
        request = urllib.request.Request(
            self.url + "/api/reports",
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(201, response.status)

    def report(self):
        with urllib.request.urlopen(self.url + "/api/reports") as response:
            return next(r for r in json.load(response) if r["name"] == self.name)

    def apply(self, **kwargs):
        return self.client.set_report_allocations(self.name, "25605", "A0001", **kwargs)

    def test_bulk_saves_six_once_and_rerun_is_a_verified_noop(self):
        self.seed()
        before = self.report()["transactions"]
        result = self.apply()
        self.assertTrue(result["success"], result)
        self.assertEqual(6, result["updated"])
        self.assertEqual(1, result["bulk_groups"])
        after = self.report()
        self.assertEqual(1, after["allocation_write_count"])
        for old, new in zip(before, after["transactions"]):
            allocation = new["allocations"]
            self.assertEqual(1, len(allocation))
            self.assertIn("25605", allocation[0]["custom6"])
            self.assertIn("A0001", allocation[0]["custom7"])
            self.assertEqual("100", allocation[0]["percentage"])
            self.assertEqual(old, {k: v for k, v in new.items() if k != "allocations"})
        repeated = self.apply()
        self.assertTrue(repeated["success"], repeated)
        self.assertEqual(0, repeated["updated"])
        self.assertEqual(6, repeated["skipped"])
        self.assertEqual(1, self.report()["allocation_write_count"])

    def test_dry_run_and_selected_indices_do_not_broaden_selection(self):
        self.seed()
        dry = self.apply(transaction_indices=[0, 2], dry_run=True)
        self.assertTrue(dry["success"], dry)
        self.assertEqual([1, 3], dry["planned_indices"])
        self.assertFalse(self.report().get("allocation_write_count", 0))
        result = self.apply(transaction_indices=[0, 2])
        self.assertTrue(result["success"], result)
        self.assertEqual(2, result["updated"])
        txs = self.report()["transactions"]
        self.assertEqual(
            [True, False, True, False, False, False],
            [bool(t.get("allocations")) for t in txs],
        )

    def test_conflicting_or_split_allocations_abort_before_any_write(self):
        self.seed(options={"seed_conflicting_allocation": True})
        result = self.apply()
        self.assertFalse(result["success"], result)
        self.assertEqual([6], result["conflicting_indices"])
        self.assertFalse(self.report().get("allocation_write_count", 0))
        self.assertTrue(
            all(not t.get("allocations") for t in self.report()["transactions"][:5])
        )

    def test_different_inherited_programs_are_grouped_and_preserved(self):
        self.seed(options={"different_programs": True})
        result = self.apply()
        self.assertTrue(result["success"], result)
        self.assertEqual(2, result["bulk_groups"])
        txs = self.report()["transactions"]
        self.assertEqual(
            ["P999"] * 3 + ["FC631"] * 3, [t["allocations"][0]["custom8"] for t in txs]
        )

    def test_persisted_child_save_survives_lost_parent_acknowledgement(self):
        self.seed(options={"child_save_persists": True})
        from ccworks.bulk_allocations import BulkAllocator

        original = BulkAllocator._write_group

        def interrupted(allocator, *args):
            original(allocator, *args)
            raise TimeoutError("simulated lost acknowledgement")

        with patch.object(BulkAllocator, "_write_group", interrupted):
            result = self.apply()
        self.assertTrue(result["success"], result)
        self.assertEqual(6, result["updated"])
        self.assertTrue(result["warnings"])
        self.assertEqual(1, self.report()["allocation_write_count"])

    def test_partial_run_can_resume_without_duplicate_allocations(self):
        self.seed(options={"different_programs": True, "child_save_persists": True})
        from ccworks.bulk_allocations import BulkAllocator

        original = BulkAllocator._write_group

        def interrupt(allocator, *args):
            original(allocator, *args)
            raise TimeoutError("connection interrupted after the first group")

        with patch.object(BulkAllocator, "_write_group", interrupt):
            partial = self.apply()
        self.assertFalse(partial["success"], partial)
        self.assertEqual([4, 5, 6], partial["remaining_indices"])
        resumed = self.apply()
        self.assertTrue(resumed["success"], resumed)
        self.assertEqual(3, resumed["updated"])
        self.assertEqual(3, resumed["skipped"])
        self.assertEqual(2, self.report()["allocation_write_count"])
        self.assertTrue(
            all(len(t["allocations"]) == 1 for t in self.report()["transactions"])
        )

    def test_child_save_persists_even_when_parent_save_disappears(self):
        self.seed(options={"child_save_persists": True, "missing_parent_save": True})
        result = self.apply()
        self.assertTrue(result["success"], result)
        self.assertTrue(result["warnings"])
        self.assertEqual(1, self.report()["allocation_write_count"])

    def test_explicit_program_and_short_session_protection(self):
        self.seed()
        with patch.object(self.client, "_session_minutes_left", return_value=0.5):
            blocked = self.apply(program="P999")
        self.assertFalse(blocked["success"], blocked)
        self.assertFalse(self.report().get("allocation_write_count", 0))
        result = self.apply(program="P999")
        self.assertTrue(result["success"], result)
        self.assertTrue(
            all(
                "P999" in t["allocations"][0]["custom8"]
                for t in self.report()["transactions"]
            )
        )

    def test_amount_mode_is_never_treated_as_a_matching_percentage(self):
        self.seed(options={"amount_mode": True, "seed_matching_allocation": True})
        result = self.apply()
        self.assertFalse(result["success"], result)
        self.assertEqual([6], result["conflicting_indices"])
        self.assertFalse(self.report().get("allocation_write_count", 0))

    def test_bulk_selection_controls_preserve_row_body_editor_clicks(self):
        self.seed(
            transactions=[
                {
                    "merchant": "Vendor 1",
                    "amount": "$11.00",
                    "expense_type": "Lodging",
                    "business_purpose": "Keep purpose",
                    "comment": "Keep comment",
                }
            ]
        )
        result = self.client.reconcile_report(
            self.name,
            {
                "Vendor 1": {
                    "expense_type": "Ground Transportation",
                    "business_purpose": "Keep purpose",
                }
            },
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(
            "Ground Transportation", self.report()["transactions"][0]["expense_type"]
        )

    def test_dropped_save_never_reports_success(self):
        self.seed(options={"drop_allocation_save": True})
        result = self.apply()
        self.assertFalse(result["success"], result)
        self.assertEqual(6, result["failed"])
        self.assertEqual(list(range(1, 7)), result["remaining_indices"])
        self.assertEqual(1, self.report()["allocation_write_count"])

    def test_bad_indices_fail_without_writing(self):
        self.seed()
        for indices in ([0, 0], [-1], [6], [], [True]):
            with self.subTest(indices=indices):
                result = self.apply(transaction_indices=indices)
                self.assertFalse(result["success"], result)
        self.assertFalse(self.report().get("allocation_write_count", 0))
