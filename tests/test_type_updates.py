"""Type-only batch updates against a browser and a local Concur mock."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import urllib.request

from playwright.sync_api import Page

from ccworks.browser_client import ConcurBrowserClient
from tests.mock_concur_server import MockConcurServer

os.environ.setdefault("CCWORKS_SKIP_BROWSER_BOOTSTRAP", "1")


class TestTypeUpdates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = MockConcurServer(host="127.0.0.1", port=8101)
        cls.server.start()
        cls.base_url = "http://127.0.0.1:8101"

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
        self.report_name = "BATCHTYPES " + self._testMethodName
        self.navigations = []
        self.browser_page = self.client._browser_page

        @contextmanager
        def tracked_page(*args, **kwargs):
            with self.browser_page(*args, **kwargs) as page:
                page.on(
                    "framenavigated",
                    lambda frame: (
                        self.navigations.append(frame.url)
                        if frame == page.main_frame
                        and frame.url.endswith("/nui/expense")
                        else None
                    ),
                )
                yield page

        self.client._browser_page = tracked_page

    def post(self, route, data):
        req = urllib.request.Request(
            self.base_url + route,
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as response:
            return json.load(response)

    def create(self, **options):
        self.post("/api/reports", {"name": self.report_name, "mock_options": options})

    def rows(self):
        with urllib.request.urlopen(self.base_url + "/api/reports") as response:
            reports = json.load(response)
        return next(r["transactions"] for r in reports if r["name"] == self.report_name)

    def payload(self, expense_type="Meal", indices=range(1, 7)):
        return [
            {
                "index": i,
                "vendor": f"Vendor {i}",
                "amount": f"${10 + i}.00",
                "expense_type": expense_type,
            }
            for i in indices
        ]

    def assert_types(self, expected):
        rows = self.rows()
        self.assertEqual([expected] * 6, [r["expense_type"] for r in rows])
        self.assertTrue(
            all(
                r["business_purpose"] == "Keep purpose"
                and r["comment"] == "Keep comment"
                for r in rows
            ),
            rows,
        )

    def test_batch_reuses_report_and_verifies_once(self):
        self.create()
        result = self.client.apply_json_updates(self.report_name, self.payload())
        self.assertTrue(result["success"], result)
        self.assertEqual(
            (6, 0, 0), (result["updated"], result["skipped"], result["failed"])
        )
        self.assertEqual(
            2, len(self.navigations), "one initial load and one persisted verification"
        )
        self.assertTrue(all(r["verified"] for r in result["results"]), result)
        self.assert_types("Meal")

    def test_repeat_skips_all_rows_without_saving(self):
        self.create()
        result = self.client.apply_json_updates(
            self.report_name, self.payload("Lodging")
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(
            (0, 6, 0), (result["updated"], result["skipped"], result["failed"])
        )
        self.assertEqual(0, sum(r.get("save_count", 0) for r in self.rows()))
        self.assertEqual(
            1,
            len(self.navigations),
            "the initial fresh report is sufficient for a no-op",
        )

    def test_saved_draft_warning_is_successful_but_not_ready_to_submit(self):
        self.create(saved_warning=True)
        result = self.client.apply_json_updates(
            self.report_name, self.payload(indices=[1])
        )
        self.assertTrue(result["success"], result)
        self.assertFalse(result["ready_to_submit"], result)
        self.assertTrue(result["results"][0]["saved"], result)
        self.assertTrue(result["results"][0]["verified"], result)
        self.assertIn("has been saved", " ".join(result["warnings"]))
        self.assertEqual("Meal", self.rows()[0]["expense_type"])

    def test_stale_editor_reloads_before_writing_the_next_expense(self):
        self.create(sticky_editor=True)
        result = self.client.apply_json_updates(
            self.report_name, self.payload(indices=[1, 2])
        )
        self.assertTrue(result["success"], result)
        rows = self.rows()
        self.assertEqual(
            ["Meal", "Meal", "Lodging", "Lodging", "Lodging", "Lodging"],
            [r["expense_type"] for r in rows],
        )
        self.assertEqual([1, 1, 0, 0, 0, 0], [r.get("save_count", 0) for r in rows])
        self.assertEqual(
            3, len(self.navigations), "one fallback reload plus the normal two loads"
        )

    def test_silent_save_loss_fails_persisted_verification(self):
        self.create(drop_save=True)
        result = self.client.apply_json_updates(
            self.report_name, self.payload(indices=[1])
        )
        self.assertFalse(result["success"], result)
        self.assertEqual(1, result["failed"], result)
        self.assertIn("persist", result["results"][0]["error"])
        self.assertEqual("Lodging", self.rows()[0]["expense_type"])

    def test_delayed_save_warning_uses_state_waits_without_fixed_sleeps(self):
        self.create(saved_warning=True, save_delay_ms=2700)
        original = self.client._click_save_expense

        def no_sleep_save(*args, **kwargs):
            with patch.object(
                Page, "wait_for_timeout", side_effect=AssertionError("fixed save delay")
            ):
                return original(*args, **kwargs)

        self.client._click_save_expense = no_sleep_save
        result = self.client.apply_json_updates(
            self.report_name, self.payload(indices=[1])
        )
        self.assertTrue(result["success"], result)
        self.assertFalse(result["ready_to_submit"], result)
        self.assertEqual("Meal", self.rows()[0]["expense_type"])

    def test_direct_txn_type_update_uses_the_same_batch_path(self):
        self.create()
        result = self.client.update_report_transaction(
            self.report_name, [1, 2], expense_type="Meal"
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(2, result["updated"], result)
        self.assertEqual(2, len(self.navigations))

    def test_identity_mismatch_never_writes(self):
        self.create()
        expense = self.payload(indices=[1])[0]
        expense["amount"] = "$999.00"
        result = self.client.apply_json_updates(self.report_name, [expense])
        self.assertFalse(result["success"], result)
        self.assertEqual(0, sum(r.get("save_count", 0) for r in self.rows()))

    def test_picker_and_save_have_no_fixed_waits(self):
        self.create(picker_delay_ms=1500)
        with patch.object(
            Page, "wait_for_timeout", side_effect=AssertionError("fixed UI delay")
        ):
            result = self.client.apply_json_updates(
                self.report_name, self.payload(indices=[1])
            )
        self.assertTrue(result["success"], result)
        self.assertEqual("Meal", self.rows()[0]["expense_type"])

    def test_mixed_batch_saves_only_changed_rows(self):
        self.create()
        payload = self.payload()
        for exp in payload[1::2]:
            exp["expense_type"] = "Lodging"
        result = self.client.apply_json_updates(self.report_name, payload)
        self.assertTrue(result["success"], result)
        self.assertEqual(
            (3, 3, 0), (result["updated"], result["skipped"], result["failed"])
        )
        self.assertEqual(
            [1, 0, 1, 0, 1, 0], [r.get("save_count", 0) for r in self.rows()]
        )

    def test_actual_rejection_still_fails_without_saving(self):
        self.create(reject_save=True)
        result = self.client.apply_json_updates(
            self.report_name, self.payload(indices=[1])
        )
        self.assertFalse(result["success"], result)
        self.assertEqual(1, result["failed"], result)
        self.assertIn("save rejected", result["results"][0]["error"])
        self.assertEqual(0, self.rows()[0].get("save_count", 0))

    def test_duplicate_identities_reload_to_avoid_stale_editor_confusion(self):
        self.create(sticky_editor=True, duplicate_identity=True)
        payload = self.payload(indices=[1, 2])
        payload[1].update(vendor="Vendor 1", amount="$11.00")
        result = self.client.apply_json_updates(self.report_name, payload)
        self.assertTrue(result["success"], result)
        self.assertEqual(2, result["updated"], result)
        self.assertEqual([1, 1], [r.get("save_count", 0) for r in self.rows()[:2]])
        self.assertEqual(3, len(self.navigations))

    def test_warning_after_itemization_confirmation_is_still_a_saved_draft(self):
        self.create(saved_warning=True)
        self.post(
            "/api/reports/allocate",
            {
                "report_name": self.report_name,
                "index": 0,
                "allocations": [{"dept": "test", "fund": "test"}],
            },
        )
        expense = self.payload(indices=[1])[0]
        expense["business_purpose"] = "Updated purpose"
        result = self.client.apply_json_updates(self.report_name, [expense])
        self.assertTrue(result["success"], result)
        self.assertFalse(result["ready_to_submit"], result)
        self.assertTrue(result["results"][0]["saved"], result)
        self.assertEqual("Updated purpose", self.rows()[0]["business_purpose"])
        self.assertEqual("Meal", self.rows()[0]["expense_type"])

    def test_short_session_skips_existing_types_but_refuses_new_writes(self):
        self.create()
        self.client._session_minutes_left = lambda: 0.2
        result = self.client.apply_json_updates(
            self.report_name, self.payload("Lodging")
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(6, result["skipped"], result)
        refused = self.client.apply_json_updates(
            self.report_name, self.payload(indices=[1, 2])
        )
        self.assertFalse(refused["success"], refused)
        self.assertEqual(2, refused["failed"], refused)
        self.assertEqual(0, sum(r.get("save_count", 0) for r in self.rows()))

    def test_ready_dashboard_is_not_blocked_by_a_decorative_busy_wrapper(self):
        self.create()
        with self.client._browser_page() as page:
            page.goto(self.base_url + "/nui/expense")
            page.locator(".report-card").first.wait_for(state="visible")
            page.evaluate("""() => {
                const el = document.createElement('div');
                el.className = 'spndpkg-full-page-busyIndicator-wrapper';
                el.style.cssText = 'width:16px;height:16px;pointer-events:none';
                el.setAttribute('aria-hidden', 'true');
                document.body.appendChild(el);
            }""")
            started = time.monotonic()
            self.client._wait_for_dashboard(page)
            self.assertLess(
                time.monotonic() - started,
                5,
                "an idle wrapper must not consume the 30-second loading timeout",
            )


class TestTypeUpdateInputs(unittest.TestCase):
    def test_invalid_plans_fail_before_a_browser_is_opened(self):
        for expenses in (
            [],
            [{"index": 0, "expense_type": "Meal"}],
            [{"index": True, "expense_type": "Meal"}],
            [{"index": "1", "expense_type": "Meal"}],
            [{"index": 1, "expense_type": "Meal"}] * 2,
            [{"index": 1, "expense_type": " "}],
            [{"index": 1, "expense_type": "Meal", "amount": 11}],
        ):
            with self.subTest(expenses=expenses):
                client = ConcurBrowserClient.__new__(ConcurBrowserClient)
                client._browser_page = Mock()
                with self.assertRaises(ValueError):
                    client._apply_type_updates("Example", expenses)
                client._browser_page.assert_not_called()

    def test_other_mutations_do_not_enter_the_type_only_path(self):
        base = {
            "index": 1,
            "vendor": "Example",
            "amount": "$11.00",
            "expense_type": "Meal",
        }
        self.assertTrue(ConcurBrowserClient._type_only_payload([base]))
        for key, value in (
            ("comment", ""),
            ("business_purpose", ""),
            ("allocation", None),
            ("receipt_file_path", ""),
            ("receipt_file", "new.pdf"),
        ):
            with self.subTest(key=key):
                self.assertFalse(
                    ConcurBrowserClient._type_only_payload([{**base, key: value}])
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
