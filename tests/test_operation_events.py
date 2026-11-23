"""Event/checkpoint schemas must never become an expense-data logging channel."""

import json
import logging
from pathlib import Path
import tempfile
import unittest

from ccworks.operation_events import OperationEvents, ProgressCheckpoint
from ccworks.browser_client import logger as browser_logger
from ccworks.submission import approval_state


class TestOperationEvents(unittest.TestCase):
    def test_legacy_browser_diagnostics_cannot_leak_messages_or_tracebacks(self):
        sentinel = "synthetic-private@example.invalid"
        with self.assertLogs("ConcurBrowserClient", logging.WARNING) as captured:
            try:
                raise RuntimeError(sentinel)
            except RuntimeError:
                browser_logger.warning("Report %s", sentinel, exc_info=True)
        self.assertNotIn(sentinel, "\n".join(captured.output))
        self.assertIsNone(captured.records[0].exc_info)
        self.assertEqual((), captured.records[0].args)

    def test_events_allow_only_typed_metadata(self):
        events = OperationEvents("submission")
        with self.assertLogs("ccworks.events", logging.INFO) as captured:
            event = events.emit("verification", "verified", index=2, session_minutes=12)
        self.assertEqual("verified", event["outcome"])
        self.assertEqual(2, event["index"])
        self.assertEqual(1, len(captured.records))
        with self.assertRaises(TypeError):
            events.emit("verification", "verified", receipt_path="private.pdf")
        for invalid in (float("nan"), float("inf"), "private-value"):
            with (
                self.subTest(invalid=type(invalid).__name__),
                self.assertRaises(ValueError),
            ):
                events.emit("session", "started", session_minutes=invalid)

    def test_private_checkpoint_does_not_store_the_report_identity(self):
        identity = "synthetic-private-report@example.invalid"
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = ProgressCheckpoint("submission", identity, Path(temporary))
            checkpoint.write("certification", "attempted", attempted=True)
            text = checkpoint.path.read_text()
            self.assertNotIn(identity, text + checkpoint.path.name)
            self.assertEqual(0o600, checkpoint.path.stat().st_mode & 0o777)
            self.assertEqual("attempted", json.loads(text)["outcome"])

    def test_status_parser_rejects_conflicting_and_negative_evidence(self):
        for values, expected in (
            (["Not SubmittedInformation"], "draft"),
            (["Not Paid"], "unknown"),
            (["Submitted & Pending ApprovalInformation"], "submitted"),
            (["Submitted", "Draft"], "unknown"),
            ([], "unknown"),
            (["Submitted", "Not Paid"], "submitted"),
        ):
            with self.subTest(values=values):
                self.assertEqual(expected, approval_state(values))
