"""Known submission stages; only independently persisted status proves success."""

from pathlib import Path
import re
from typing import Any

from playwright.sync_api import expect

from ccworks.browser_client import ConcurSessionExpiredError
from ccworks.operation_events import OperationEvents, ProgressCheckpoint

CARDS = ".report-tile, .report-card, .cnqr-report-card, .sapMCard"
SUMMARY = "[data-nuiexp='erp-dlg-report-summary']"
AGREEMENT = "[data-nuiexp='exp-dlg-confirmation-agreement-modal']"
LEGACY = "[data-nuiexp='report-totals-modal'], .report-totals-modal"
STATUS = "[class*='status'], [class*='Status'], [data-nuiexp*='status'], [data-nuiexp*='Status']"


def approval_state(values: list[str]) -> str:
    states = set()
    for value in values:
        text = re.sub(r"[^a-z]", "", value.lower())
        text = text.removeprefix("approvalstatus").removesuffix("information")
        if text in {"draft", "notsubmitted", "unsubmitted", "returned", "sentback"}:
            states.add("draft")
        elif text.startswith(
            ("submitted", "approved", "pendingapproval", "sentforapproval")
        ) or text in {"paid", "processingpayment"}:
            states.add("submitted")
    return next(iter(states)) if len(states) == 1 else "unknown"


class ReportSubmission:
    def __init__(self, client: Any, name: str, headless: bool):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("A report name is required")
        self.client, self.name, self.headless = client, name, headless
        self.events = OperationEvents("submission")
        # Explicit session overrides keep test checkpoints isolated as well.
        self.checkpoint = ProgressCheckpoint(
            "submission",
            client.base_url + "\0" + name,
            directory=Path(client.session_file).parent / ".ccworks-state",
        )
        self.attempted = False
        self.final_attempted = False
        self.confirmed = False
        self.reauthentication_required = False
        self.checkpoint_available = True

    def _record(self, phase, outcome):
        self.events.emit(phase, outcome)
        if self.checkpoint_available:
            try:
                self.checkpoint.write(phase, outcome, attempted=self.final_attempted)
            except OSError:
                self.checkpoint_available = False
                self.events.emit("checkpoint", "failed")

    def _require_checkpoint(self):
        if not self.checkpoint_available:
            raise RuntimeError("Private checkpoint unavailable; submission stopped")

    def _card(self, page):
        page.goto(self.client.base_url + "/nui/expense", timeout=30000)
        self.client._wait_for_dashboard(page)
        cards = self.client._named_report_cards(page, self.name)
        expect(cards).to_have_count(1, timeout=15000)
        for selector in (
            ".report-tile__header__text",
            ".report-name",
            ".cnqr-report-name",
            ".sapMObjLTitle",
            "h3",
            "strong",
        ):
            title = cards.locator(selector).first
            if title.count():
                expect(title).to_have_text(self.name, timeout=5000)
                return cards
        if cards.inner_text().strip() != self.name:
            raise RuntimeError("Exact report title could not be verified")
        return cards

    def _state(self, page, card):
        try:
            expect(card.locator(STATUS).first).to_contain_text(
                re.compile(r"submitted|draft|approved|pending|returned|paid", re.I),
                timeout=5000,
            )
        except AssertionError:
            # Older report cards omit status; use the identity-checked header.
            pass
        state = approval_state(card.locator(STATUS).all_text_contents())
        if state != "unknown":
            return state
        card.click(timeout=10000)
        self.client._wait_for_report_view(page)
        header = self.client._read_report_header(page, self.name)
        return approval_state([(header or {}).get("approval_status", "")])

    def _verify(self):
        self._record("verification", "started")
        # No write retries. A fresh context also covers a lost final acknowledgment.
        with self.client._browser_page(headless=self.headless) as page:
            state = self._state(page, self._card(page))
        self._record(
            "verification",
            "verified"
            if state == "submitted"
            else "not_submitted"
            if state == "draft"
            else "unknown",
        )
        return state

    def _result(self, state, skipped=False):
        success = state == "submitted"
        outcome = (
            "skipped"
            if skipped
            else "verified"
            if success
            else "not_submitted"
            if state == "draft"
            else "unknown"
        )
        self._record("completed", outcome)
        return {
            "success": success,
            "verified": success,
            "status": outcome,
            "attempted": self.attempted,
            "final_attempted": self.final_attempted,
            "confirmed": self.confirmed,
            "approval_state": state,
            "reauthentication_required": self.reauthentication_required and not success,
            "checkpoint_available": self.checkpoint_available,
            "warnings": []
            if self.checkpoint_available
            else [
                "Private checkpoint could not be updated; use fresh saved-state verification"
            ],
            "message": "Report submission verified"
            if success
            else "Report remains unsubmitted"
            if state == "draft"
            else "Session expired; reauthenticate and read saved status before retrying"
            if self.reauthentication_required
            else "Submission outcome is unknown; read saved status before retrying",
            "events": self.events.events,
        }

    def run(self):
        self._record("started", "started")
        left = self.client._session_minutes_left()
        self.events.emit("preflight", "started", session_minutes=left)
        if left is not None and left < 2:
            self._record("completed", "failed")
            raise RuntimeError(
                "Session budget is too short; reauthenticate before submitting"
            )
        try:
            with self.client._browser_page(headless=self.headless) as page:
                card = self._card(page)
                before = self._state(page, card)
                if before == "submitted":
                    return self._result(before, skipped=True)
                if before != "draft":
                    return self._result("unknown")
                self._require_checkpoint()
                # _state may have opened a header to establish the status.
                if (
                    page.locator(CARDS)
                    .filter(has_text=self.name)
                    .filter(visible=True)
                    .count()
                ):
                    card.click(timeout=10000)
                    self.client._wait_for_report_view(page)
                toolbar = page.locator(
                    "[data-nuiexp='reportActionButtons.submitButton'], "
                    "#submit-entire-report-btn, button:has-text('Submit Report')"
                ).filter(visible=True)
                expect(toolbar).to_have_count(1, timeout=15000)
                try:
                    expect(toolbar).to_be_enabled(timeout=5000)
                except AssertionError as error:
                    raise RuntimeError(
                        "Submit Report is disabled; resolve report alerts"
                    ) from error
                self._record("preflight", "attempted")
                self._require_checkpoint()
                self.attempted = True
                toolbar.click(timeout=10000)
                for _ in range(3):
                    dialog = page.locator(
                        SUMMARY + ", " + AGREEMENT + ", " + LEGACY
                    ).filter(visible=True)
                    expect(dialog).to_have_count(1, timeout=15000)
                    key = dialog.get_attribute("data-nuiexp")
                    if key == "erp-dlg-report-summary":
                        phase = "summary"
                        action = dialog.locator(
                            "[data-nuiexp='erp-btn-summary-footer-submit']"
                        )
                        label = "Proceed"
                    elif key == "exp-dlg-confirmation-agreement-modal":
                        phase = "certification"
                        action = dialog.locator(
                            "[data-nuiexp='accept-and-create-button']"
                        )
                        label = "Accept & Submit"
                    else:
                        phase = "confirmation"
                        action = dialog.get_by_role(
                            "button", name="Submit Report", exact=True
                        )
                        label = "Submit Report"
                    expect(action).to_have_count(1, timeout=15000)
                    expect(action).to_have_text(label, timeout=15000)
                    expect(action).to_be_enabled(timeout=15000)
                    self.final_attempted = phase != "summary"
                    self._record(phase, "attempted")
                    self._require_checkpoint()
                    node = dialog.element_handle()
                    action.click(timeout=10000)
                    # The union locator can immediately resolve to the next
                    # stage. Wait for the original node, not that new dialog.
                    page.wait_for_function(
                        "el => !el.isConnected || !el.getClientRects().length",
                        arg=node,
                        timeout=15000,
                    )
                    self._record(phase, "acknowledged")
                    if self.final_attempted:
                        self.confirmed = True
                        break
        except Exception as error:
            if isinstance(error, ConcurSessionExpiredError):
                self.reauthentication_required = True
                self._record("session", "failed")
            if not self.attempted:
                self._record("completed", "failed")
                if self.reauthentication_required:
                    raise ConcurSessionExpiredError(
                        "Session expired; reauthenticate with ccworks session login"
                    ) from None
                if isinstance(error, RuntimeError) and str(error).startswith(
                    ("Submit Report is disabled", "Private checkpoint unavailable")
                ):
                    raise
                raise RuntimeError(
                    "Submission preflight could not verify the report and controls"
                ) from None
            self._record("confirmation", "unknown")
        try:
            return self._result(self._verify())
        except Exception as error:
            if isinstance(error, ConcurSessionExpiredError):
                self.reauthentication_required = True
                self._record("session", "failed")
            return self._result("unknown")
