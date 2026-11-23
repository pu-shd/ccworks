"""Bulk CLI selection, output, and failure status without real authentication."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from ccworks import cli


class TestBulkAllocationCLI(unittest.TestCase):
    def invoke(self, selection, result):
        out, err = io.StringIO(), io.StringIO()
        argv = [
            "ccworks",
            "--output",
            "json",
            "txn",
            "allocate-bulk",
            "Report",
            *selection,
            "--dept",
            "25605",
            "--fund",
            "A0001",
        ]
        with (
            patch("sys.argv", argv),
            patch.object(cli, "ConcurBrowserClient") as client,
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            client.return_value.set_report_allocations.return_value = result
            try:
                cli.main()
                status = 0
            except SystemExit as error:
                status = error.code
        return status, out.getvalue(), err.getvalue(), client

    def test_explicit_all_and_dry_run_are_forwarded(self):
        status, out, _, client = self.invoke(
            ["--all", "--dry-run"], {"success": True, "planned_indices": [1, 2]}
        )
        self.assertEqual(0, status)
        self.assertEqual([1, 2], json.loads(out)["planned_indices"])
        self.assertIsNone(
            client.return_value.set_report_allocations.call_args.kwargs[
                "transaction_indices"
            ]
        )
        self.assertTrue(
            client.return_value.set_report_allocations.call_args.kwargs["dry_run"]
        )

    def test_selected_indices_convert_to_zero_based(self):
        status, _, _, client = self.invoke(["--indices", "1", "3"], {"success": True})
        self.assertEqual(0, status)
        self.assertEqual(
            [0, 2],
            client.return_value.set_report_allocations.call_args.kwargs[
                "transaction_indices"
            ],
        )

    def test_failures_are_nonzero_with_machine_readable_results(self):
        status, out, _, _ = self.invoke(
            ["--all"], {"success": False, "remaining_indices": [2]}
        )
        self.assertEqual(1, status)
        self.assertEqual([2], json.loads(out)["remaining_indices"])

    def test_missing_ambiguous_and_invalid_selection_never_opens_a_browser(self):
        for selection in (
            [],
            ["--all", "--indices", "1"],
            ["--indices", "0"],
            ["--indices", "1", "1"],
        ):
            with self.subTest(selection=selection):
                status, _, _, client = self.invoke(selection, {"success": True})
                self.assertEqual(2, status)
                client.assert_not_called()
