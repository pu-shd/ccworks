"""Regression coverage for failed expense writes; no real records are touched."""

from contextlib import nullcontext, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from ccworks import cli
from ccworks.browser_client import ConcurBrowserClient


def browser_fixture():
    """A report with one editable row and a working save operation."""
    client = ConcurBrowserClient.__new__(ConcurBrowserClient)
    client.base_url = "https://example.invalid"
    locator = MagicMock()
    locator.first = locator
    locator.filter.return_value = locator
    locator.locator.return_value = locator
    locator.count.return_value = 1
    locator.is_enabled.return_value = True
    locator.is_visible.return_value = True
    locator.text_content.return_value = "Uber"
    page = MagicMock()
    absent = MagicMock()
    absent.first = absent
    absent.filter.return_value = absent
    absent.count.return_value = 0
    page.locator.side_effect = lambda selector: (
        absent if ".sapMMessageBox" in selector else locator
    )
    row = MagicMock()
    row.locator.return_value = locator
    row.text_content.return_value = "07/01/2026 Uber $24.50 Select expense"
    client._browser_page = Mock(return_value=nullcontext(page))
    for name in (
        "_wait_for_dashboard",
        "_wait_for_report_view",
        "_take_screenshot",
        "_dismiss_modals",
        "_open_report_by_name",
    ):
        setattr(client, name, Mock())
    client._collect_expense_rows = Mock(return_value=([row], {}))
    client._session_minutes_left = Mock(return_value=None)
    client._write_expense_fields = Mock(return_value=[])
    client._click_save_expense = Mock(return_value=(True, None))
    client._clear_allocations_on_page = Mock(return_value=(1, None))
    client._add_allocation_on_page = Mock(return_value=(["25605", "A0001"], None))
    return client, page, locator, row


class TestExpenseWrites(unittest.TestCase):
    def test_valid_chartstring_is_cleared_replaced_and_reported_successful(self):
        client, _, _, _ = browser_fixture()
        result = client.apply_json_updates(
            "Statement",
            [
                {
                    "index": 1,
                    "vendor": "Uber",
                    "amount": "$24.50",
                    "allocation": {"dept": "25605", "fund": "A0001"},
                }
            ],
        )
        client._clear_allocations_on_page.assert_called_once()
        client._add_allocation_on_page.assert_called_once()
        self.assertTrue(result["results"][0]["success"], result)

    def test_valid_transaction_edit_is_saved(self):
        client, _, _, _ = browser_fixture()
        result = client.update_report_transaction(
            "Statement", transaction_indices=[1], comment="valid edit"
        )
        client._click_save_expense.assert_called_once()
        self.assertTrue(result["results"][0]["success"], result)

    def test_incomplete_chartstring_does_not_clear_existing_allocations(self):
        client, _, _, _ = browser_fixture()
        result = client.apply_json_updates(
            "Statement",
            [
                {
                    "index": 1,
                    "vendor": "Uber",
                    "amount": "$24.50",
                    "allocation": {"dept": "25605"},  # accidental omission of fund
                }
            ],
        )
        client._clear_allocations_on_page.assert_not_called()
        client._browser_page.assert_not_called()
        self.assertFalse(result["success"], result)

    def test_invalid_allocation_in_later_row_prevents_all_writes(self):
        for allocation in (
            {"fund": "A0001"},
            {"dept": "25605", "fund": " "},
            "25605",
            [],
            False,
            {"dept": 25605, "fund": "A0001"},
            {"dept": "25605", "fund": "A0001", "prog": 123},
        ):
            with self.subTest(allocation=allocation):
                client, _, _, _ = browser_fixture()
                result = client.apply_json_updates(
                    "Statement",
                    [
                        {"index": 1, "vendor": "Uber", "comment": "first edit"},
                        {"index": 2, "vendor": "Uber", "allocation": allocation},
                    ],
                )
                self.assertFalse(result["success"], result)
                client._browser_page.assert_not_called()

    def test_empty_and_null_allocations_still_clear(self):
        for allocation in ({}, None):
            with self.subTest(allocation=allocation):
                client, _, _, _ = browser_fixture()
                result = client.apply_json_updates(
                    "Statement",
                    [
                        {
                            "index": 1,
                            "vendor": "Uber",
                            "allocation": allocation,
                        }
                    ],
                )
                self.assertTrue(result["success"], result)
                client._clear_allocations_on_page.assert_called_once()
                client._add_allocation_on_page.assert_not_called()

    def test_failed_row_makes_the_batch_unsuccessful(self):
        client, _, _, _ = browser_fixture()
        result = client.apply_json_updates(
            "Statement",
            [
                {
                    "index": 1,
                    "vendor": "Uber",
                    "amount": "$99.00",
                    "comment": "must not write to a different expense",
                }
            ],
        )
        self.assertFalse(result["results"][0]["success"])
        self.assertFalse(result["success"], result)

    def test_field_failure_keeps_the_real_error_and_saves_other_valid_edits(self):
        client, _, _, _ = browser_fixture()
        client._write_expense_fields.return_value = ["Expense Type not offered"]
        result = client.update_report_transaction(
            "Statement",
            transaction_indices=[1],
            expense_type="Missing",
            comment="valid edit",
        )
        self.assertFalse(result["results"][0]["success"])
        self.assertIn("Expense Type not offered", str(result["results"][0]))
        client._click_save_expense.assert_called_once()

    def test_reconciliation_reports_failed_saves(self):
        client, page, locator, row = browser_fixture()
        locator.all.return_value = [row]
        client._click_save_expense.return_value = (False, "required field missing")
        result = client.reconcile_report("Statement", {"Uber": {"comment": "test"}})
        self.assertFalse(result["success"], result)

    def test_save_failure_keeps_the_real_reason(self):
        client, _, _, _ = browser_fixture()
        client._click_save_expense.return_value = (False, "required field missing")
        result = client.update_report_transaction("Statement", [1], comment="edit")
        self.assertFalse(result["success"], result)
        self.assertIn("required field missing", str(result["results"][0]))

    def test_sparse_failed_index_does_not_corrupt_a_previous_result(self):
        client, _, _, row = browser_fixture()
        client._collect_expense_rows.return_value = ([row] * 3, {})
        client._write_expense_fields.side_effect = [[], ["comment did not take"]]
        result = client.update_report_transaction("Statement", [3, 1], comment="edit")
        self.assertEqual([3, 1], [r["index"] for r in result["results"]])
        self.assertTrue(result["results"][0]["success"], result)
        self.assertFalse(result["results"][1]["success"], result)
        self.assertIn("comment did not take", str(result["results"][1]))
        self.assertFalse(result["success"], result)

    def test_reconciliation_never_submits_failed_or_unmatched_rows(self):
        for rules, errors, save in (
            ({"Uber": {"comment": "edit"}}, [], False),
            ({"Uber": {"comment": "edit"}}, ["bad field"], True),
            ({"Other": {"comment": "edit"}}, [], True),
        ):
            with self.subTest(rules=rules, errors=errors, save=save):
                client, _, locator, row = browser_fixture()
                locator.all.return_value = [row]
                client._write_expense_fields.return_value = errors
                client._click_save_expense.return_value = (
                    save,
                    None if save else "save refused",
                )
                client.submit_report = Mock()
                result = client.reconcile_report("Statement", rules, submit=True)
                self.assertFalse(result["success"], result)
                self.assertFalse(result["submitted"], result)
                client.submit_report.assert_not_called()

    def test_reconciliation_uses_verified_submission_and_propagates_failure(self):
        for success in (False, True):
            with self.subTest(success=success):
                client, _, locator, row = browser_fixture()
                locator.all.return_value = [row]
                client.submit_report = Mock(
                    return_value={"success": success, "confirmed": success}
                )
                result = client.reconcile_report(
                    "Statement", {"Uber": {"comment": "edit"}}, submit=True
                )
                client.submit_report.assert_called_once_with(
                    report_name="Statement", headless=True
                )
                self.assertEqual(success, result["success"], result)
                self.assertEqual(success, result["submitted"], result)

    def test_saved_draft_warnings_prevent_reconciliation_submission(self):
        client, _, locator, row = browser_fixture()
        locator.all.return_value = [row]

        def saved_with_warning(page, ctx, warnings):
            warnings.append(
                "Expense has been saved; missing information for submission"
            )
            return True, None

        client._click_save_expense.side_effect = saved_with_warning
        client.submit_report = Mock()
        result = client.reconcile_report(
            "Statement", {"Uber": {"comment": "edit"}}, submit=True
        )
        self.assertFalse(result["success"], result)
        self.assertFalse(result["submitted"], result)
        self.assertFalse(result["ready_to_submit"], result)
        client.submit_report.assert_not_called()


class TestCliFailures(unittest.TestCase):
    def run_cli(self, argv, client):
        output = StringIO()
        with (
            patch.object(cli.sys, "argv", ["ccworks", *argv]),
            patch.object(cli, "ConcurBrowserClient", return_value=client),
            patch.object(cli, "Spinner", return_value=nullcontext()),
            redirect_stdout(output),
            redirect_stderr(StringIO()),
        ):
            code = 0
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code
        return code, output.getvalue()

    def test_apply_json_failure_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "payload.json"
            payload.write_text(json.dumps({"report_name": "Statement", "expenses": []}))
            fake = Mock()
            fake.apply_json_updates.return_value = {
                "success": False,
                "error": "session budget exceeded",
                "results": [],
            }
            output = StringIO()
            with (
                patch.object(
                    cli.sys, "argv", ["ccworks", "report", "apply-json", str(payload)]
                ),
                patch.object(cli, "ConcurBrowserClient", return_value=fake),
                patch.object(cli, "Spinner", return_value=nullcontext()),
                redirect_stdout(output),
                redirect_stderr(StringIO()),
            ):
                code = 0
                try:
                    cli.main()
                except SystemExit as exc:
                    code = exc.code
            self.assertFalse(json.loads(output.getvalue())["success"])
            self.assertNotEqual(
                0, code, "failed writes must fail shell/agent automation"
            )

    def test_all_write_commands_propagate_failure_in_both_output_formats(self):
        commands = [
            (["txn", "update", "Statement", "1"], "update_report_transaction"),
            (["report", "update", "Statement", "--purpose", "edit"], "update_report"),
            (["report", "reconcile", "Statement"], "reconcile_report"),
            (
                [
                    "txn",
                    "allocate",
                    "Statement",
                    "1",
                    "--dept",
                    "25605",
                    "--fund",
                    "A0001",
                ],
                "add_transaction_allocation",
            ),
            (["txn", "unallocate", "Statement", "1"], "remove_transaction_allocations"),
            (["txn", "detach-receipt", "Statement", "1"], "detach_transaction_receipt"),
        ]
        for argv, method in commands:
            for fmt in ("text", "json"):
                with self.subTest(command=argv, fmt=fmt):
                    client = Mock()
                    getattr(client, method).return_value = {
                        "success": False,
                        "error": "write refused",
                    }
                    code, output = self.run_cli([*argv, "--output", fmt], client)
                    self.assertEqual(1, code, output)
                    self.assertNotIn("[SUCCESS]", output)
                    self.assertIn("write refused", output)

    def test_apply_json_failed_rows_are_visible_in_text_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "payload.json"
            payload.write_text(json.dumps({"report_name": "Statement", "expenses": []}))
            client = Mock()
            client.apply_json_updates.return_value = {
                "success": False,
                "results": [{"index": 1, "success": False, "error": "save refused"}],
            }
            code, output = self.run_cli(
                ["report", "apply-json", str(payload), "--output", "text"], client
            )
            self.assertEqual(1, code, output)
            self.assertNotIn("[SUCCESS]", output)
            self.assertIn("save refused", output)

    def test_allocation_and_receipt_exceptions_exit_nonzero(self):
        commands = [
            (["txn", "allocations", "Statement"], "get_report_allocations"),
            (["txn", "unallocate", "Statement", "1"], "remove_transaction_allocations"),
            (["txn", "detach-receipt", "Statement", "1"], "detach_transaction_receipt"),
            (
                [
                    "txn",
                    "allocate",
                    "Statement",
                    "1",
                    "--dept",
                    "25605",
                    "--fund",
                    "A0001",
                ],
                "add_transaction_allocation",
            ),
        ]
        for argv, method in commands:
            with self.subTest(command=argv):
                client = Mock()
                getattr(client, method).side_effect = RuntimeError(
                    "browser unavailable"
                )
                code, output = self.run_cli(argv, client)
                self.assertEqual(1, code, output)
                self.assertEqual("error", json.loads(output)["status"])

    def test_saved_draft_warning_reports_counts_without_failing_the_command(self):
        client = Mock()
        client.update_report_transaction.return_value = {
            "success": True,
            "updated": 1,
            "skipped": 5,
            "failed": 0,
            "warnings": ["Expense saved; information required before submission"],
            "ready_to_submit": False,
            "results": [
                {
                    "index": i,
                    "success": True,
                    "status": "updated" if i == 1 else "skipped",
                }
                for i in range(1, 7)
            ],
        }
        code, output = self.run_cli(
            [
                "txn",
                "update",
                "Statement",
                "1",
                "2",
                "3",
                "4",
                "5",
                "6",
                "--type",
                "Meal",
                "--output",
                "text",
            ],
            client,
        )
        self.assertEqual(0, code, output)
        self.assertIn("Updated: 1; unchanged: 5; failed: 0", output)
        self.assertIn("[WARNING] Expense saved", output)
        self.assertNotIn("Updated 6/6", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
