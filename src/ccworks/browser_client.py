import sys
import os
import json
from contextlib import contextmanager
import time
from datetime import datetime
import logging
import contextlib
import re
from typing import Any, Dict, List, Optional, Union
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from ccworks import paths
from ccworks.browser_bootstrap import ensure_chromium

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ConcurBrowserClient")


class ConcurSessionExpiredError(RuntimeError):
    """Exception raised when the Concur session has expired and requires re-login."""
    pass


class ConcurBrowserClient:
    """Browser automation client for SAP Concur using Playwright."""

    def __init__(
        self,
        session_file: Optional[str] = None,
        base_url: str = "https://www.concursolutions.com"
    ):
        self.session_file = session_file if session_file else str(paths.session_file())
        self.base_url = base_url.rstrip("/")
        self.screenshot_dir = str(paths.screenshot_dir())
        ensure_chromium()

    def _take_screenshot(self, page: Any, label: str) -> None:
        # Include PID to prevent interference between concurrent runs
        pid = os.getpid()
        path = os.path.join(self.screenshot_dir, f"{label}_{pid}.png")
        page.screenshot(path=path)
        logger.info(f"Captured screenshot: {path}")

    # Selectors for expense line-item rows inside a report. Deliberately broad:
    # Concur's markup varies between editable, read-only, and historical reports.
    EXPENSE_ROW_SELECTORS = [
        # Current Concur grid markup. This was only in _get_transaction_rows'
        # private copy of the list, so the other paths could miss rows entirely.
        ".sapcnqr-data-grid-list__row",
        ".detail-row",
        ".sapMListUl .sapMLIB",
        "[class*='expense-item']",
        "[class*='expense-row']",
        ".sapMCustomListItem",
        "[role='row']",
        "[role='listitem']",
        ".sapMTable tr",
        "tr.sapMLIB",
    ]

    # Proof that an expense's detail pane is actually open and editable.
    # Deliberately narrow: .sapMInputBaseInner and .recon-type also match inputs
    # in the list grid, so including them lets a merely-selected row read as
    # open, which then skips every field write while reporting success.
    PANE_READY_SELECTOR = "[data-nuiexp*='field'], input[id*='type']"

    def _collect_expense_rows(self, page, report_name: str = "", report_num: str = "Unknown"):
        """Return (rows, diagnostics) for a report's expense line items.

        Every command must address rows through this helper. Previously each of
        get_report_details / update_report_transaction / apply_json_updates had
        its own copy of the selector list *and* a slightly different filter, and
        get_report_details reported a row's position among the raw selector
        matches while the write paths indexed into their own filtered lists. The
        index in one command therefore did not denote the same row as the same
        index in another, so a `report show` -> edit -> `report apply-json`
        round-trip could write to the wrong expense.

        The returned list is in document order. A row's 1-based index is its
        position in this list plus one, and that is the only index ccworks
        exposes or accepts.
        """
        candidates = page.locator(", ".join(self.EXPENSE_ROW_SELECTORS)).all()
        diagnostics = {"candidates_seen": len(candidates), "skipped": []}
        logger.info(f"Discovered {len(candidates)} potential expense line item(s) using broad selectors.")

        rows = []
        for position, row in enumerate(candidates):
            def skip(reason):
                diagnostics["skipped"].append({"candidate_position": position, "reason": reason})

            try:
                text = row.text_content()
            except Exception as e:
                skip(f"unreadable: {e}")
                continue
            if not text:
                skip("empty")
                continue

            text = " ".join(text.split()).strip()
            if len(text) < 15:
                skip("too short to be an expense row")
                continue

            lower_text = text.lower()
            if "expense type" in lower_text and "vendor details" in lower_text:
                skip("column header row")
                continue
            if "select all rows" in lower_text:
                skip("select-all header row")
                continue
            if "no expenses" in lower_text or "add an expense" in lower_text:
                skip("empty-state placeholder")
                continue
            if report_name and report_name.lower() in lower_text:
                if len(text) < len(report_name) + 25:
                    skip("report title header")
                    continue
            if report_num and report_num != "Unknown" and report_num.lower() in lower_text:
                if len(text) < len(report_num) + 25:
                    skip("report number header")
                    continue

            # An expense row carries the row-select control, or (on read-only and
            # historical reports that lack it) labelled Date/Type/Amount fields,
            # or failing that a bare date-and-amount pair.
            #
            # That last fallback used to be unreachable: it called re.search in a
            # function whose body also had a local `import re` further down, so
            # `re` was function-local and the call raised UnboundLocalError,
            # which the enclosing `except Exception: continue` swallowed --
            # silently dropping the row. Here `re` is the module-level import.
            is_expense = (
                "Select expense" in text
                or ("Date:" in text and "Type:" in text and "Amount:" in text)
                or bool(re.search(r"\d{2}/\d{2}/\d{4}.*?\$\d+", text))
            )
            if not is_expense:
                skip("no expense markers (Select expense / Date+Type+Amount)")
                continue

            rows.append(row)

        if diagnostics["skipped"]:
            logger.info(
                f"Filtered {len(diagnostics['skipped'])} non-expense candidate(s); "
                f"{len(rows)} expense row(s) remain."
            )
        return rows, diagnostics

    def _row_identity_mismatch(self, row, expense: Dict[str, Any]) -> Optional[str]:
        """Return a reason string if `row` is not the expense described, else None.

        A write keyed only on a positional index is trustworthy only while the
        report is unchanged. If a line item was added, removed, or re-sorted in
        Concur after `report show` produced the JSON, the index still resolves --
        to the wrong expense. Amount is the discriminator (a currency string that
        must appear in the row's text); vendor is checked when present but is
        matched loosely because Concur truncates and decorates merchant names.
        """
        try:
            text = " ".join((row.text_content() or "").split()).strip()
        except Exception as e:
            return f"row text unreadable: {e}"
        if not text:
            return "row has no text"

        amount = (expense.get("amount") or "").strip()
        if amount and amount not in text:
            return f"expected amount {amount!r} not present in row"

        vendor = (expense.get("vendor") or "").strip()
        if vendor and vendor.lower() not in ("", "unknown"):
            # Compare on the leading token: "ANTHROPIC* CLAUDE TEAM" in the JSON
            # may render as "ANTHROPIC*" or with a trailing location in the row.
            head = vendor.split()[0].rstrip("*").lower()
            if head and head not in text.lower():
                return f"expected vendor {vendor!r} not present in row"

        if not amount and not vendor:
            return "expense has neither amount nor vendor to verify against"
        return None

    # Concur's receipt controls, discovered by probing the live DOM. There is no
    # <input type="file"> anywhere on the page, so set_input_files has nothing to
    # bind to -- the attach button opens a native file chooser instead, which is
    # why every set_input_files-based path silently attached nothing.

    # Receipt controls live in the expense's DETAIL PANE, not the grid row.
    # The file inputs are hidden (aria-hidden, display:none) and Playwright can
    # set files on them directly, which avoids driving the OS file picker.
    RECEIPT_TAB_SELECTOR = "[data-nuiexp='receipt-tabs'] li[role='option']"
    # Hidden file inputs. data-nuiexp differs between the empty and attached
    # states ("upload-receipt"/"upload-file" vs "erc-inp-upload-file"), but the
    # id is "upload-file" in both. Playwright can set files on hidden inputs, so
    # the OS picker is never involved.
    RECEIPT_UPLOAD_INPUT = ("#upload-file, input[data-nuiexp='erc-inp-upload-file'], "
                            "input[data-nuiexp='upload-file'], "
                            "input[data-nuiexp='upload-receipt']")
    # Present only once a receipt is attached: Concur swaps the drop zone for a
    # viewer carrying Remove/Add/Open and a filename.
    RECEIPT_VIEWER_METADATA = "[data-nuiexp='receipt-viewer-metadata']"
    RECEIPT_REMOVE_SELECTOR = "[data-nuiexp='receipt-viewer__detach']"

    # The receipt area renders as an aria-live skeleton and hydrates
    # asynchronously. Checking for controls before it settles is a race: the
    # upload input may not exist yet, and the viewer metadata that confirms an
    # attachment appears seconds late. Both states end in one of these.
    RECEIPT_AREA_READY = ("[data-nuiexp='receipt-tabs'], [data-nuiexp='drag-drop-file'], "
                          "[data-nuiexp='receipt-body'], "
                          "[data-nuiexp='receipt-viewer-metadata'], #upload-receipt-button")

    # Submitting is two clicks. The toolbar button opens a report-totals
    # confirmation rendered as a full-screen overlay; the report is not
    # submitted until the dialog's own button is clicked. The toolbar button
    # stays in the DOM behind the overlay and still matches
    # button:has-text('Submit Report'), so a page-wide locator taking .first
    # resolves to the covered button and times out against the overlay
    # ("subtree intercepts pointer events") while the report stays in draft.
    # The confirmation must be scoped to inside the dialog.
    SUBMIT_CONFIRM_DIALOG = ("[data-nuiexp='report-totals-modal'], "
                             ".report-totals-modal, "
                             "[role='dialog'][aria-modal='true']")
    SUBMIT_CONFIRM_BUTTON = ("button:has-text('Submit Report'), "
                             ".sapMBtn:has-text('Submit Report'), "
                             "button:has-text('Accept & Submit'), "
                             "button:has-text('Submit')")

    # One definition of the editable fields on an expense, shared by every write
    # path. These were duplicated per-caller, and the copies drifted: one grew a
    # read-back check and a re-focus after clearing, the others did not, so the
    # same edit succeeded through one command and silently vanished through
    # another.
    # Expense type. The old list matched on `[data-nuiexp*='type']`, a wildcard
    # that hits 33 elements in a live report -- every `expense-type-cell` in the
    # grid plus the `expense-type-quicktips` help panel -- and none of them are
    # the editor. `.first` therefore typed into a table cell and read a tooltip
    # back. These are exact.
    # Scope options to the picker's own listbox: the receipt Receipt/Card Receipt
    # toggle is also role=option, and a page-wide search matches it too.
    SELECT_POPUP = ".sapcnqr-selection-list__list-box"
    # A native <select> is what the mock renders; Concur renders the combobox.
    NATIVE_TYPE_SELECT = ("select.recon-type, select[data-nuiexp='field-type'], "
                          "select[data-nuiexp='field-expenseType'], select[id*='type']")
    PURPOSE_FIELD_SELECTORS = ("[data-nuiexp='field-businessPurpose'], input#businessPurpose, "
                               "input.recon-purpose, input[id*='usinessPurpose']")
    COMMENT_FIELD_SELECTORS = ("[data-nuiexp='field-comment'], textarea#comment, "
                               "textarea.recon-comment, textarea[id*='omment']")

    SAVE_BUTTON_SELECTORS = (
        "[data-nuiexp='exp-save-expense']",
        "button[data-nuiexp='exp-save-expense']",
        "button.recon-save-btn",
        "button:has-text('Save Expense')",
        "button.sapcnqr-button:has-text('Save Expense')",
        "button.sapMBtn:has-text('Save')",
        "button:has-text('Save')",
        "button[data-nuiexp='save-button']",
    )

    def _click_save_expense(self, page, ctx=None):
        """Save the open expense. Returns (saved, error).

        Only one of the three copies this replaces checked for the validation
        dialog Concur raises when it rejects a save; the others treated the click
        itself as proof and reported a rejected save as a successful one.
        """
        scope = ctx if ctx is not None else page
        for sel in self.SAVE_BUTTON_SELECTORS:
            try:
                btn = scope.locator(sel).first
                if btn.count() == 0 or not btn.is_visible():
                    continue
                try:
                    btn.wait_for_element_state("enabled", timeout=3000)
                except Exception:
                    pass
                btn.click(force=True)
                page.wait_for_timeout(2500)

                # Concur's dialogs are .sapcnqr-message-dialog with
                # role="alertdialog". The old locator looked for .sapMDialog /
                # [role='dialog'], matched neither, and so reported a save that
                # Concur had refused as a successful one.
                dialog = page.locator(
                    ".sapcnqr-message-dialog, .sapcnqr-dialog, [role='alertdialog'], "
                    ".sapMDialog, .sapMMessageBox, [role='dialog']"
                ).filter(visible=True).first
                if dialog.count() > 0:
                    text = " ".join((dialog.text_content() or "").split())

                    # "Update Other Items?" asks whether to propagate the change
                    # into this expense's itemizations and allocations. Until it
                    # is answered the save does not commit, so leaving it on
                    # screen silently discarded the edit. "Do Not Update" saves
                    # the expense-level change and leaves allocations' own text
                    # alone, which is the narrower of the two.
                    if "Update Other Items" in text:
                        choice = dialog.locator(
                            "button:has-text('Do Not Update')").filter(visible=True).first
                        if choice.count() == 0:
                            return False, ("save raised 'Update Other Items?' but no "
                                           "'Do Not Update' button was found")
                        choice.click()
                        page.wait_for_timeout(2500)
                        logger.info(f"  Saved via {sel} (declined itemization update)")
                        return True, None

                    if re.search(r"Error|provide valid information|must provide", text, re.I):
                        msg = text[:180]
                        close = dialog.locator(
                            "button:has-text('Close'), button:has-text('OK')").first
                        if close.count() > 0:
                            close.click()
                        else:
                            page.keyboard.press("Escape")
                        return False, f"save rejected by Concur: {msg}"

                logger.info(f"  Saved via {sel}")
                return True, None
            except Exception:
                continue
        return False, "Could not find Save button"

    def _read_select_field(self, ctx, field_key, label, native_extra=""):
        """The value a Concur select/combobox field currently shows, or None.

        `field_key` is the data-nuiexp suffix, e.g. "expenseType" or "custom6".
        """
        native = f"select[data-nuiexp='field-{field_key}'], select#{field_key}"
        if native_extra:
            native += ", " + native_extra
        el = ctx.locator(native).first
        if el.count() > 0:
            try:
                return (el.input_value() or "").strip() or None
            except Exception:
                pass
        field = ctx.locator(f"[data-nuiexp='field-{field_key}']").first
        if field.count() == 0:
            return None
        text = " ".join((field.text_content() or "").split())
        # The container carries its own label and required marker, e.g.
        # "Expense Type*Computer Peripherals (OIT use only)".
        for lead in (f"{label} *", f"{label}*", label):
            if lead and text.lower().startswith(lead.lower()):
                text = text[len(lead):]
                break
        return text.strip() or None

    def _set_select_field(self, page, ctx, field_key, value, label, native_extra=""):
        """Set a Concur select/combobox field and confirm it took.

        Returns an error string, or None. Concur renders these as a role=combobox
        with no input of its own; the search box exists only while the picker is
        open, and option labels carry the value followed by its description, so
        matching is by prefix. Options are scoped to the picker's own listbox
        because other widgets on the page also use role=option.
        """
        native_sel = f"select[data-nuiexp='field-{field_key}'], select#{field_key}"
        if native_extra:
            native_sel += ", " + native_extra
        native = ctx.locator(native_sel).first
        if native.count() > 0:
            try:
                native.select_option(label=value)
                page.wait_for_timeout(500)
            except Exception as e:
                return f"failed to select {label} {value!r}: {e}"
        else:
            field = ctx.locator(f"[data-nuiexp='field-{field_key}']").first
            if field.count() == 0:
                return f"{label} field not found (wanted {value!r})"
            try:
                trigger = page.locator(f"[data-nuiexp='field-{field_key}__trigger']").first
                (trigger if trigger.count() > 0 else field).click(force=True)
                page.wait_for_timeout(900)

                search = page.locator(f"[data-nuiexp='field-{field_key}__input']").first
                if search.count() == 0:
                    return f"{label} picker did not open for {value!r}"
                search.click()
                search.fill(value)
                page.wait_for_timeout(1200)

                # Option labels carry the value followed by its description
                # ("Computer Peripherals (OIT use only)Accessories like..."), so
                # an anchored exact match never matches. Prefer a prefix match;
                # fall back to a substring so a bare code like "25605" still
                # finds "(25605) ORF-Technical Support". Ambiguity is refused
                # rather than guessed -- this writes to a financial record.
                options = page.locator(f"{self.SELECT_POPUP} [role='option']")
                labels = []
                for i in range(options.count()):
                    labels.append((i, " ".join((options.nth(i).text_content() or "").split())))

                exact = [i for i, t in labels if t.startswith(value)]
                loose = [i for i, t in labels if value.lower() in t.lower()]
                # Judge ambiguity on distinct labels: the picker renders each
                # option more than once, so counting elements reports a single
                # unambiguous choice as a conflict.
                distinct = {labels[i][1] for i in loose}
                distinct_exact = {labels[i][1] for i in exact}
                if len(distinct_exact) > 1:
                    # A prefix can match more than one option ("Software" also
                    # prefixes "Software Maintenance"). Picking the first would
                    # silently write a type nobody asked for.
                    page.keyboard.press("Escape")
                    matched = ", ".join(repr(t) for t in sorted(distinct_exact)[:4])
                    return (f"{label} {value!r} is ambiguous -- it prefixes "
                            f"{len(distinct_exact)} options ({matched})")
                if exact:
                    chosen = exact[0]
                elif len(distinct) == 1:
                    chosen = loose[0]
                elif len(distinct) > 1:
                    page.keyboard.press("Escape")
                    matched = ", ".join(repr(t) for t in sorted(distinct)[:4])
                    return (f"{label} {value!r} is ambiguous -- it matches "
                            f"{len(distinct)} options ({matched})")
                else:
                    page.keyboard.press("Escape")
                    return f"{label} {value!r} was not offered by the picker"
                target = options.nth(chosen)
                target.click(force=True)
                page.wait_for_timeout(1500)
            except Exception as e:
                return f"failed to set {label} {value!r}: {e}"

        got = self._read_select_field(ctx, field_key, label, native_extra)
        if not got or value.lower() not in got.lower():
            return f"{label} did not take: wanted {value!r}, field shows {got!r}"
        return None


    def _read_expense_type(self, ctx):
        """The expense type currently shown, or None."""
        return self._read_select_field(ctx, "expenseType", "Expense Type",
                                       self.NATIVE_TYPE_SELECT)

    def _set_expense_type(self, page, ctx, expense_type):
        """Set the expense type on the open pane. Returns an error string, or None."""
        return self._set_select_field(page, ctx, "expenseType", expense_type,
                                      "Expense Type", self.NATIVE_TYPE_SELECT)

    def _read_text_field(self, ctx, selectors):
        """Value of a text field, or "" if absent.

        Uses the same exact selectors as the writer. The read paths used their own
        `[data-nuiexp*='...']` wildcards, which are page-wide and resolve in DOM
        order, so they could return an unrelated widget's text instead of the
        field -- the comment read returned empty for a comment that was set.
        """
        field = ctx.locator(selectors).filter(visible=True).first
        if field.count() == 0:
            return ""
        try:
            val = field.input_value()
        except Exception:
            val = field.text_content() or ""
        return " ".join((val or "").split()).strip()

    def _fill_text_field(self, ctx, selectors, value, label):
        """Set one text field, replacing its contents. Returns an error, or None."""
        field = ctx.locator(selectors).filter(visible=True).first
        if field.count() == 0:
            return f"{label} field not found"
        try:
            field.fill("")
            field.fill(value)
        except Exception as e:
            return f"failed to set {label}: {e}"

        # Read back, like every other write. Without it a field that silently
        # refused the value still reported success.
        try:
            got = (field.input_value() or "").strip()
        except Exception:
            return None  # not an input; nothing reliable to compare against
        if got != value.strip():
            return f"{label} did not take: wanted {value!r}, field shows {got!r}"
        return None

    def _write_expense_fields(self, page, ctx, expense_type=None, purpose=None, comment=None):
        """Apply the field edits to the open pane. Returns a list of error strings.

        Absent (None) means leave the field alone; "" means clear it.
        """
        errors = []
        if expense_type is not None and expense_type != "":
            err = self._set_expense_type(page, ctx, expense_type)
            if err:
                errors.append(err)
        if purpose is not None:
            err = self._fill_text_field(ctx, self.PURPOSE_FIELD_SELECTORS, purpose,
                                        "business purpose")
            if err:
                errors.append(err)
        if comment is not None:
            err = self._fill_text_field(ctx, self.COMMENT_FIELD_SELECTORS, comment, "comment")
            if err:
                errors.append(err)
        return errors

    def _wait_for_receipt_area(self, page, timeout=20000):
        """Block until the receipt panel has hydrated past its loading skeleton."""
        try:
            page.wait_for_selector(self.RECEIPT_AREA_READY, timeout=timeout)
            page.wait_for_timeout(500)
            return True
        except Exception:
            return False

    def _select_receipt_tab(self, page, ctx):
        """Focus the 'Receipt' tab, not 'Card Receipt'. Returns True if the Receipt
        tab is selected (or there is no toggle at all), False if it could not be.

        On a card transaction Concur defaults this toggle to 'Card Receipt', whose
        pane carries its own file input. Uploading there is silently discarded, so
        selecting the right tab is a correctness requirement, not cosmetic. The
        toggle is not always inside the detail-pane container, so both scopes are
        searched -- looking only at the pane found zero tabs and did nothing.
        """
        for scope in (ctx, page):
            try:
                tabs = scope.locator(self.RECEIPT_TAB_SELECTOR)
                count = tabs.count()
            except Exception:
                continue
            if count == 0:
                continue
            for i in range(count):
                tab = tabs.nth(i)
                try:
                    if (tab.text_content() or "").strip() != "Receipt":
                        continue
                    if tab.get_attribute("aria-selected") == "true":
                        return True
                    tab.click()
                    page.wait_for_timeout(1500)
                    # Switching tabs re-renders the panel, so wait for the empty
                    # state's own controls before anything reads an input.
                    self._wait_for_receipt_area(page)
                    return tab.get_attribute("aria-selected") == "true"
                except Exception:
                    continue
            return False
        # No toggle on this expense: the receipt panel is the only one.
        return True

    def _attached_receipt_name(self, page, ctx):
        """Filename of the receipt currently attached, or None if there is none.

        Checks the pane first, then the whole page: the receipt viewer is not
        always rooted inside the detail-pane container.
        """
        for scope in (ctx, page):
            try:
                meta = scope.locator(f"{self.RECEIPT_VIEWER_METADATA} .filename").first
                if meta.count() > 0:
                    name = (meta.text_content() or "").strip()
                    if name:
                        return name
            except Exception:
                continue
        return None

    def _attach_receipt_in_pane(self, page, ctx, idx, receipt_path):
        """Attach receipt_path to the expense whose detail pane is currently open,
        replacing any receipt already there. Returns an error string, or None.

        Addressed by the open pane rather than by merchant text: this report has
        six rows sharing a merchant and two that are byte-identical, so no text
        match can target them.
        """
        import os.path as _osp
        want = _osp.basename(receipt_path)
        try:
            if not self._wait_for_receipt_area(page):
                return (f"row {idx}: the receipt panel never finished loading, so "
                        f"no upload was attempted")
            if not self._select_receipt_tab(page, ctx):
                return (f"row {idx}: could not switch from the 'Card Receipt' tab to "
                        f"'Receipt'; uploading there would be discarded")

            # Replace, don't append. A file input is present in BOTH states, so
            # setting files while a receipt is attached would add a second one --
            # Concur exposes that separately as "Add" (receipt-viewer__append).
            # Remove the existing receipt first so the upload is a true overwrite.
            # Only replace something we can actually name. A viewer with an empty
            # filename is not an uploaded receipt -- on card transactions Concur
            # renders a card e-receipt shell there -- and removing on that signal
            # destroys a real receipt while attaching nothing in its place.
            existing = self._attached_receipt_name(page, ctx)
            if existing is not None:
                err = self._remove_receipt_in_pane(page, ctx, idx, existing)
                if err:
                    return err
                # Removing swaps the viewer back to the drop zone, which
                # re-renders through the skeleton again. Without waiting, the
                # input located next is the detached one from the previous
                # render and the upload silently goes nowhere -- leaving the row
                # with no receipt at all, having just deleted the old one.
                if not self._wait_for_receipt_area(page):
                    return (f"row {idx}: removed the existing receipt but the upload "
                            f"panel never re-appeared; the row now has no receipt")
                self._select_receipt_tab(page, ctx)

            inp = None
            for scope in (ctx, page):
                loc = scope.locator(self.RECEIPT_UPLOAD_INPUT).first
                if loc.count() > 0:
                    inp = loc
                    break
            if inp is None:
                return f"row {idx}: no receipt upload input found in the detail pane"

            inp.set_input_files(receipt_path)
            page.wait_for_timeout(3000)
            self._dismiss_modals(page)

            # Verify by filename rather than trusting the click. This also catches
            # an accidental append, where the viewer would show the wrong name.
            deadline = 30000
            waited = 0
            got = None
            while waited < deadline:
                got = self._attached_receipt_name(page, ctx)
                if got == want:
                    logger.info(f"  Attached receipt to row {idx}: {want}")
                    return None
                page.wait_for_timeout(1000)
                waited += 1000
            if got is None:
                return (f"row {idx}: upload did not register -- no receipt is shown "
                        f"after uploading {want}")
            return (f"row {idx}: expected receipt '{want}' but the expense shows "
                    f"'{got}'")
        except Exception as e:
            return f"receipt attach failed on row {idx}: {e}"

    def _detach_receipt_in_pane(self, page, ctx, idx):
        """Remove whatever receipt is attached to the open expense.

        Returns an error string, or None. A row with no receipt is a no-op rather
        than a failure, so clearing a whole report is idempotent.
        """
        if not self._wait_for_receipt_area(page):
            return f"row {idx}: the receipt panel never finished loading"
        if not self._select_receipt_tab(page, ctx):
            return (f"row {idx}: could not switch to the 'Receipt' tab, so any receipt "
                    f"there was left in place")
        existing = self._attached_receipt_name(page, ctx)
        if existing is None:
            return None
        err = self._remove_receipt_in_pane(page, ctx, idx, existing)
        if err:
            return err
        still = self._attached_receipt_name(page, ctx)
        if still is not None:
            return f"row {idx}: receipt {still!r} still attached after removal"
        return None

    def detach_transaction_receipt(self, report_name: str, transaction_index: int,
                                  headless: bool = True) -> Dict[str, Any]:
        """Remove the receipt from a transaction. `transaction_index` is 1-based
        here, matching the row index the CLI exposes."""
        logger.info(f"Detaching receipt from row {transaction_index} in "
                    f"'{report_name}'...")
        with self._browser_page(headless=headless, viewport_height=900) as page:
            try:
                page.goto(f"{self.base_url}/nui/expense", timeout=45000)
                self._wait_for_dashboard(page)
                self._open_report_by_name(page, report_name)
                self._dismiss_modals(page)

                rows, _ = self._collect_expense_rows(page, report_name)
                if transaction_index < 1 or transaction_index > len(rows):
                    return {"success": False,
                            "error": (f"Transaction index {transaction_index} out of bounds "
                                      f"(found {len(rows)} rows).")}
                row = rows[transaction_index - 1]
                row.scroll_into_view_if_needed()
                row.click(force=True)
                page.wait_for_timeout(600)
                edit = page.locator("[data-nuiexp='edit-button'], button:has-text('Edit'), "
                                    "#edit-transaction-btn").filter(visible=True).first
                if edit.count() > 0 and edit.is_enabled():
                    edit.click(force=True)
                page.wait_for_selector(self.PANE_READY_SELECTOR, timeout=10000)

                pane = page.locator(
                    "#sapcnqr-layout-side-panel-elements, "
                    ".sapcnqr-layout-side-panel__elements, "
                    ".ere__dynamic-main-content").filter(visible=True).first
                ctx = pane if pane.count() > 0 else page

                err = self._detach_receipt_in_pane(page, ctx, transaction_index)
                if err:
                    return {"success": False, "error": err}
                saved, save_error = self._click_save_expense(page, ctx)
                if not saved:
                    return {"success": False, "error": save_error}
                return {"success": True}
            except Exception as e:
                logger.error(f"Failed to detach receipt: {e}")
                return {"success": False, "error": str(e)}

    def _remove_receipt_in_pane(self, page, ctx, idx, existing_name=None):
        """Remove the receipt attached to the open expense. Returns an error string,
        or None on success.

        Fails explicitly rather than falling through: a row must never be reported
        as replaced while the original receipt is still attached.
        """
        try:
            btn = ctx.locator(self.RECEIPT_REMOVE_SELECTOR).filter(visible=True).first
            if btn.count() == 0:
                btn = page.locator(self.RECEIPT_REMOVE_SELECTOR).filter(visible=True).first
            if btn.count() == 0:
                return (f"row {idx} already has a receipt "
                        f"{'(' + existing_name + ') ' if existing_name else ''}"
                        f"and no Remove control was found to replace it")
            btn.click(force=True)
            page.wait_for_timeout(1200)

            confirm = page.locator(
                "[role='alertdialog'] button:has-text('Remove'), "
                "[role='alertdialog'] button:has-text('Delete'), "
                "[role='alertdialog'] button:has-text('Yes'), "
                "[role='dialog'] button:has-text('Remove'), "
                "[role='dialog'] button:has-text('Delete'), "
                "[role='dialog'] button:has-text('Confirm')"
            ).filter(visible=True).first
            if confirm.count() > 0:
                confirm.click()
                page.wait_for_timeout(1500)
            self._dismiss_modals(page)

            # Confirm it actually went, so a failed removal cannot masquerade as a
            # successful replace.
            waited = 0
            while waited < 10000:
                if self._attached_receipt_name(page, ctx) is None:
                    logger.info(f"  Removed existing receipt from row {idx}.")
                    return None
                page.wait_for_timeout(1000)
                waited += 1000
            return (f"row {idx}: receipt still attached after Remove "
                    f"({self._attached_receipt_name(page, ctx)})")
        except Exception as e:
            return f"receipt removal failed on row {idx}: {e}"

    def _dismiss_modals(self, page):
        """Aggressively dismisses common SAP Concur overlays."""
        # 1. Timeline Modal / What's New
        modals = page.locator("[data-nuiexp='timelineModal'], .sapcnqr-dialog__fade--in, [role='dialog'][aria-modal='true']").filter(visible=True)
        if modals.count() > 0:
            logger.info(f"Detected {modals.count()} visible modal(s). Attempting dismissal...")
            
            # Try Escape key first
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            
            # Try finding a Close/X button
            close_buttons = [
                "button:has-text('Close')", 
                ".sapMBtn:has-text('Close')", 
                "button[aria-label='Close']",
                ".sapcnqr-icon--close"
            ]
            for selector in close_buttons:
                btn = page.locator(selector).filter(visible=True).first
                if btn.count() > 0:
                    try:
                        btn.click(force=True, timeout=2000)
                        page.wait_for_timeout(1000)
                        if modals.count() == 0:
                            return
                    except Exception:
                        pass

            # 2. Nuclear Option: Remove from DOM if still present
            logger.info("Nuclear dismissal: removing modals from DOM via evaluate...")
            page.evaluate("""
                document.querySelectorAll("[data-nuiexp='timelineModal'], .sapcnqr-dialog, [role='dialog'][aria-modal='true']").forEach(el => {
                    el.style.display = 'none';
                    el.remove();
                });
                document.querySelectorAll(".sapMDialog").forEach(el => el.remove());
                document.body.classList.remove("sapMDialog-Open");
            """)
            page.wait_for_timeout(1000)

    @contextlib.contextmanager
    def _session_lock(self):
        """Simple file-based lock for concurrency safety."""
        lock_file = f"{self.session_file}.lock"
        with open(lock_file, "w") as f:
            try:
                # Exclusive lock, non-blocking if possible (but we'll wait)
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
                try:
                    os.remove(lock_file)
                except Exception:
                    pass

    def _check_session(self, page: Any) -> None:
        """Checks if the current page is a login/signin page, indicating an expired session."""
        url = page.url.lower()
        if "signin" in url or "login" in url:
            title = page.title().lower()
            if "sign in" in title or "login" in title:
                logger.error("Session expired detected via URL/Title redirection.")
                raise ConcurSessionExpiredError(
                    "Your SAP Concur session has expired. Please re-run the login command:\n"
                    "  ccworks session login"
                )

    @contextmanager
    def _browser_page(self, headless: bool = True, viewport_height: int = 800):
        """Chromium with the saved session, yielding a page and closing after.

        Twenty-one call sites repeated this launch/new_context/new_page preamble
        and its `finally: browser.close()`. One definition means session handling
        (and anything that has to change about it) lives in one place.

        Deliberately not used by run_headed_login, which must start with no
        stored state and writes the session file itself.
        """
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context(
                storage_state=self.session_file,
                viewport={"width": 1280, "height": viewport_height},
            )
            page = context.new_page()
            try:
                yield page
            except ConcurSessionExpiredError:
                # Do not persist here: the context now holds a redirect-to-login
                # state, and saving it would clobber a session that may still be
                # good for other work.
                raise
            else:
                # Concur's JWT carries roughly a 60-minute TTL and is refreshed
                # in-browser during use. Every run used to load the session file
                # and never write it back, so the refreshed cookie was discarded
                # at close and the next invocation re-presented the original --
                # which expires 60 minutes after login regardless of how much
                # activity happened in between. Persisting the state means each
                # run inherits the freshest cookies.
                try:
                    with self._session_lock():
                        context.storage_state(path=self.session_file)
                    logger.debug("Refreshed session state written back.")
                except Exception as exc:
                    logger.debug(f"_browser_page: could not persist session state: {exc!r}")
            finally:
                browser.close()

    def _wait_for_dashboard(self, page: Any) -> None:
        """Helper to wait for Concur's dynamic SPA dashboard elements to load."""
        self._check_session(page)
        logger.info("Waiting for Concur dashboard to render (handling loading spinners)...")
        try:
            page.wait_for_load_state("load", timeout=15000)
        except Exception:
            pass

        # Smart wait loop: wait until all Concur busy indicators (blue loading dots) disappear
        try:
            logger.info("Waiting for all Concur busy indicators to clear...")
            indicator = page.locator(".sapcnqr-busy-indicator, .spndpkg-full-page-busyIndicator-wrapper")
            start_time = time.time()
            page.wait_for_timeout(500)
            
            while time.time() - start_time < 30:
                visible_count = 0
                count = indicator.count()
                for i in range(count):
                    try:
                        if indicator.nth(i).is_visible():
                            visible_count += 1
                    except Exception:
                        continue
                if visible_count == 0:
                    break
                page.wait_for_timeout(500)
            logger.info("Concur busy indicators cleared.")
        except Exception as e:
            logger.warning(f"Proceeding after busy indicator wait timeout: {str(e)}")

        combined_selectors = [
            "#create-report-btn",
            "button:has-text('Create New Report')",
            "button:has-text('Create Report')",
            "button:has-text('Create Expense Report')",
            "span:has-text('Create Expense Report')",
            ".no-reports",
            ".report-card",
            ".report-tile",
            ".cnqr-report-card",
            ".sapMCard",
            "h2:has-text('Available Receipts')"
        ]
        combined_str = ", ".join(combined_selectors)

        try:
            page.locator(combined_str).first.wait_for(state="visible", timeout=15000)
            logger.info("Dashboard components loaded and visible.")
        except Exception as e:
            logger.warning(f"Proceeding after dashboard load timeout: {str(e)}")

        # Clear overlays before any caller tries to interact with the grid.
        # Concur shows onboarding / what's-new dialogs (e.g.
        # .vip-widgets__text-app-onboarding-dialog) over the dashboard on first
        # visit after a feature release. They are modal and swallow pointer
        # events, so a click on a report tile retries for its full timeout and
        # fails with "<div ...dialog__body> subtree intercepts pointer events"
        # even though the tile resolved correctly. Callers previously dismissed
        # only *after* the click that the dialog was blocking.
        self._dismiss_modals(page)

    def _wait_for_report_view(self, page: Any) -> None:
        """Helper to wait for the inside of a report to load."""
        logger.info("Waiting for report details view to render...")
        combined_selectors = [
            "button:has-text('Submit Report')",
            "button:has-text('Report Details')",
            ".expense-list",
            "[class*='report-header']",
            ".sapMLIB"
        ]
        combined_str = ", ".join(combined_selectors)
        try:
            page.locator(combined_str).first.wait_for(state="visible", timeout=15000)
            logger.info("Report details view loaded.")
        except Exception as e:
            logger.warning(f"Proceeding after report view load timeout: {str(e)}")

    def _saved_jwt_expiry(self):
        """(iso_expiry, minutes_remaining) for the saved Concur JWT, or None.

        Reads the session file directly -- no browser -- so it is cheap enough to
        run before an interactive login. Only cookie metadata is touched; values
        are never read or logged.
        """
        try:
            if not os.path.exists(self.session_file):
                return None
            with open(self.session_file) as f:
                data = json.load(f)
            for c in data.get("cookies", []):
                if c.get("name") == "JWT" and "concursolutions" in (c.get("domain") or ""):
                    exp = c.get("expires") or -1
                    if exp and exp > 0:
                        return (datetime.fromtimestamp(exp).isoformat(timespec="seconds"),
                                (exp - time.time()) / 60.0)
                    return ("no fixed expiry", None)
        except Exception as exc:
            logger.debug(f"_saved_jwt_expiry: {exc!r}")
        return None

    def run_headed_login(self) -> None:
        """
        Launches a headed browser instance to let the user log in manually
        and handle MFA/2FA or SSO. Once logged in, it saves the session state.
        """
        logger.info("Starting headed browser for login...")
        import sys
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()

            logger.info(f"Navigating to login page: {self.base_url}")
            page.goto(self.base_url)

            # Write all interactive prompts to stderr so they show up even when stdout is redirected
            sys.stderr.write("\n" + "=" * 80 + "\n")
            # This command deliberately starts with an empty cookie jar, so the
            # Concur login screen appears even when the saved session is fine.
            # Without saying so, that looks like the existing session failing.
            saved = self._saved_jwt_expiry()
            if saved:
                when, mins = saved
                if mins is None:
                    sys.stderr.write(f" NOTE: a saved session already exists (expiry {when}).\n")
                elif mins > 0:
                    sys.stderr.write(f" NOTE: a saved session already exists and is still "
                                     f"usable for {mins:.0f} more minute(s) (until {when}).\n")
                else:
                    sys.stderr.write(f" NOTE: the saved session expired at {when}.\n")
                sys.stderr.write(" This command always begins a fresh login with no stored\n")
                sys.stderr.write(" cookies, so you will see the Concur login screen either way.\n")
                sys.stderr.write(" Press Ctrl-C now to keep the existing session instead.\n")
                sys.stderr.write("-" * 80 + "\n")
            sys.stderr.write(" ACTION REQUIRED:\n")
            sys.stderr.write(" 1. In the opened browser window, log in to SAP Concur.\n")
            sys.stderr.write(" 2. Complete any MFA/2FA, Single Sign-On (SSO), or Captchas if prompted.\n")
            sys.stderr.write(" 3. Once you see the Concur Homepage / Dashboard (fully logged in),\n")
            sys.stderr.write("    return to this terminal and press ENTER to save your session.\n")
            sys.stderr.write("=" * 80 + "\n\n")
            sys.stderr.flush()

            sys.stderr.write("Press ENTER here after you have logged in and see the Concur home page... ")
            sys.stderr.flush()
            # Read from stdin
            sys.stdin.readline()

            # Save authenticated state with lock
            with self._session_lock():
                context.storage_state(path=self.session_file)
            logger.info(f"Session state successfully saved to {self.session_file}")
            browser.close()
            logger.info("Browser closed.")

            # Saving state is not the same as capturing a usable session. If the
            # dashboard had not finished loading when ENTER was pressed, Concur
            # has not issued its JWT yet and the file is saved without one --
            # which looked like a successful login, reported "complete", and then
            # failed on the next command. Say so instead.
            saved = self._saved_jwt_expiry()
            if saved is None:
                raise RuntimeError(
                    "Login did not capture a Concur session: no JWT cookie was "
                    "saved. This usually means ENTER was pressed before the Concur "
                    "dashboard finished loading. Run `ccworks session login` again "
                    "and wait until the expense dashboard is fully visible before "
                    "pressing ENTER."
                )
            when, mins = saved
            if mins is not None and mins <= 0:
                raise RuntimeError(
                    f"Login saved an already-expired Concur JWT (expired {when}). "
                    f"Run `ccworks session login` again."
                )
            if mins is not None:
                logger.info(f"Concur JWT captured; valid for {mins:.0f} more minute(s) "
                            f"(until {when}).")

    def check_session_validity(self, headless: bool = True) -> Dict[str, Any]:
        """
        Checks whether the currently saved session file exists and is valid (not expired/redirected to login).
        Returns a dictionary indicating status: {"success": True, "authenticated": True/False, "reason": str}
        """
        if not os.path.exists(self.session_file):
            return {
                "success": True,
                "authenticated": False,
                "reason": f"Session file '{self.session_file}' does not exist on disk."
            }

        logger.info(f"Checking session validity using session file: {self.session_file}...")
        with self._browser_page(headless=headless) as page:
            try:
                dashboard_url = f"{self.base_url}/nui/expense"
                page.goto(dashboard_url, timeout=15000)
                
                # Check for login redirection
                current_url = page.url
                if "login" in current_url.lower() or "signin" in current_url.lower():
                    return {
                        "success": True,
                        "authenticated": False,
                        "reason": "Session has expired or credentials have been invalidated (redirected to login page)."
                    }
                
                # Not being redirected is necessary but not sufficient: Concur
                # served a dashboard seven minutes after the JWT had expired,
                # so this reported "active and valid" and the next real
                # operation failed with SESSION EXPIRED. The JWT's own lifetime
                # is what governs, so check it and say how long is left.
                jwt = None
                try:
                    for c in page.context.cookies():
                        if c.get("name") == "JWT" and "concursolutions" in (c.get("domain") or ""):
                            jwt = c
                            break
                except Exception as exc:
                    logger.debug(f"check_session_validity: could not read cookies: {exc!r}")

                if jwt is None:
                    return {
                        "success": True,
                        "authenticated": False,
                        "reason": ("No live Concur JWT cookie. The saved session has "
                                   "lapsed even though the dashboard still rendered; "
                                   "re-run `ccworks session login`."),
                    }

                expires = jwt.get("expires") or -1
                if expires and expires > 0:
                    remaining = (expires - time.time()) / 60.0
                    expires_at = datetime.fromtimestamp(expires).isoformat(timespec="seconds")
                    if remaining <= 0:
                        return {
                            "success": True,
                            "authenticated": False,
                            "reason": (f"Concur JWT expired at {expires_at}; "
                                       f"re-run `ccworks session login`."),
                            "jwt_expires_at": expires_at,
                            "expires_in_minutes": round(remaining, 1),
                        }
                    return {
                        "success": True,
                        "authenticated": True,
                        "reason": (f"Session is active; the Concur JWT expires at "
                                   f"{expires_at} ({remaining:.0f} min from now)."),
                        "jwt_expires_at": expires_at,
                        "expires_in_minutes": round(remaining, 1),
                    }

                return {
                    "success": True,
                    "authenticated": True,
                    "reason": "Session is active (JWT present with no fixed expiry).",
                }
            except Exception as e:
                return {
                    "success": False,
                    "authenticated": False,
                    "reason": f"Network or browser error while checking status: {str(e)}"
                }
    def create_draft_report(
        self,
        name: str,
        purpose: Optional[str] = None,
        comment: Optional[str] = None,
        headless: bool = True
    ) -> Dict[str, Any]:
        """
        Loads the saved session and attempts to create a draft expense report.
        Captures screenshots at each step for verification and debugging.
        """
        if not os.path.exists(self.session_file):
            raise FileNotFoundError(
                f"Session file '{self.session_file}' not found. "
                "Please run login configuration first using: ccworks session login"
            )

        logger.info(f"Launching browser (headless={headless}) using session from {self.session_file}...")
        
        with self._browser_page(headless=headless) as page:
            try:
                dashboard_url = f"{self.base_url}/nui/expense"
                logger.info(f"Navigating to Concur Expense page: {dashboard_url}")
                page.goto(dashboard_url, timeout=30000)
                
                # Check for login redirection
                current_url = page.url
                if "login" in current_url.lower() or "signin" in current_url.lower():
                    self._take_screenshot(page, "session_expired_error")
                    raise RuntimeError("Session appears to have expired. Re-run: ccworks session login")

                # Wait for SPA widgets to load
                self._wait_for_dashboard(page)
                self._take_screenshot(page, "01_expense_dashboard")

                # Step 1: Click "Create New Report"
                logger.info("Locating 'Create New Report' button...")
                create_button = None
                selectors = [
                    # Real Concur selector strategies
                    lambda p: p.get_by_text("Create Expense Report", exact=False),
                    lambda p: p.get_by_role("button", name="Create Expense Report", exact=False),
                    lambda p: p.locator("text=Create Expense Report"),
                    lambda p: p.locator("button:has-text('Create Expense Report')"),
                    
                    # Alternative selector fallbacks
                    lambda p: p.get_by_role("button", name="Create New Report", exact=False),
                    lambda p: p.get_by_role("button", name="Create Report", exact=False),
                    lambda p: p.locator("button:has-text('Create New Report')"),
                    lambda p: p.locator("button:has-text('Create Report')"),
                    lambda p: p.locator("a:has-text('Create New Report')"),
                    lambda p: p.locator("a:has-text('Create Report')"),
                    lambda p: p.locator("#create-report-btn"),
                    lambda p: p.locator(".sapMBtnContent:has-text('Create New Report')")
                ]

                for idx, get_sel in enumerate(selectors):
                    try:
                        loc = get_sel(page)
                        if loc.is_visible(timeout=2000):
                            create_button = loc
                            logger.info(f"Found 'Create New Report' using selector strategy {idx+1}.")
                            break
                    except Exception:
                        continue

                if not create_button:
                    self._take_screenshot(page, "create_button_not_found")
                    raise RuntimeError("Could not locate 'Create New Report' button.")

                create_button.click()
                logger.info("Clicked 'Create New Report' button.")
                page.wait_for_timeout(2000)
                self._take_screenshot(page, "02_create_report_dialog")

                # Step 2: Fill in the Report Header Form
                logger.info("Filling out the report header form...")

                # Name input selector
                name_input = page.get_by_role("textbox", name="Report Name", exact=False)
                if not name_input.is_visible(timeout=2000):
                    name_input = page.locator("#reportname, input[id*='reportname'], input[id*='ReportName'], input[name*='name']")
                
                if name_input.is_visible(timeout=2000):
                    name_input.fill(name)
                    logger.info(f"Filled Report Name: {name}")
                else:
                    raise RuntimeError("Could not find standard Report Name input field.")

                # Purpose input
                if purpose:
                    purpose_input = page.get_by_role("textbox", name="Purpose", exact=False)
                    if not purpose_input.is_visible(timeout=1000):
                        purpose_input = page.locator("#purpose, textarea[id*='purpose'], input[id*='purpose']")
                    
                    if purpose_input.is_visible(timeout=1000):
                        purpose_input.fill(purpose)
                        logger.info("Filled Purpose field.")

                # Comment input
                if comment:
                    comment_input = page.get_by_role("textbox", name="Comment", exact=False)
                    if not comment_input.is_visible(timeout=1000):
                        comment_input = page.locator("#comment, textarea[id*='comment'], input[id*='comment']")
                    
                    if comment_input.is_visible(timeout=1000):
                        comment_input.fill(comment)
                        logger.info("Filled Comment field.")

                self._take_screenshot(page, "03_filled_form")

                # Step 3: Click "Create Report" / "Next" / "Save"
                logger.info("Submitting the report form...")
                submit_button = None
                submit_selectors = [
                    lambda p: p.get_by_role("button", name="Create Report", exact=False),
                    lambda p: p.get_by_role("button", name="Create", exact=True),
                    lambda p: p.get_by_role("button", name="Next", exact=True),
                    lambda p: p.get_by_role("button", name="Save", exact=True),
                    lambda p: p.locator("#submit-report-btn"),
                    lambda p: p.locator("button:has-text('Create Report')")
                ]

                for idx, get_sel in enumerate(submit_selectors):
                    try:
                        loc = get_sel(page)
                        if loc.is_visible(timeout=2000):
                            submit_button = loc
                            logger.info(f"Found submit button using selector strategy {idx+1}.")
                            break
                    except Exception:
                        continue

                if not submit_button:
                    self._take_screenshot(page, "submit_button_not_found")
                    raise RuntimeError("Could not locate Create/Next/Save button in report form.")

                submit_button.click()
                logger.info("Clicked form submission button.")
                page.wait_for_timeout(3000)

                self._take_screenshot(page, "04_after_creation_completed")
                logger.info("Report creation completed!")

                return {
                    "success": True,
                    "report_name": name,
                    "screenshot_folder": os.path.abspath(self.screenshot_dir),
                    "notes": f"Verify details in {os.path.join(self.screenshot_dir, '04_after_creation_completed.png')}"
                }

            except PlaywrightTimeoutError as e:
                self._take_screenshot(page, "timeout_error")
                raise RuntimeError(f"Playwright operation timed out: {str(e)}")
            except Exception as e:
                self._take_screenshot(page, "unexpected_browser_error")
                raise e
    def list_reports(self, filter_view: Optional[str] = None, headless: bool = True) -> List[Dict[str, Any]]:
        """
        [READ] Navigates to the Expense page and retrieves all visible reports.
        Optionally selects a different filter view (e.g. 'Last 90 Days', 'All Reports') first.
        """
        logger.info(f"Listing expense reports via browser (headless={headless}, filter={filter_view})...")
        with self._browser_page(headless=headless) as page:
            reports = []
            try:
                page.goto(f"{self.base_url}/nui/expense", timeout=30000)
                self._wait_for_dashboard(page)
                self._take_screenshot(page, "list_reports_dashboard")

                if filter_view:
                    logger.info(f"Selecting report filter view: {filter_view}...")
                    view_btn = None
                    view_selectors = [
                        lambda p: p.locator("#report-view-select"),
                        lambda p: p.get_by_role("combobox", name="View", exact=False),
                        lambda p: p.locator("select[id*='view']"),
                        lambda p: p.locator(".sapMSelect, [class*='select']").filter(has_text="Reports").first,
                        lambda p: p.get_by_text("Active Reports", exact=True),
                        lambda p: p.locator("button:has-text('Active Reports')")
                    ]
                    for idx, get_sel in enumerate(view_selectors):
                        try:
                            loc = get_sel(page)
                            if loc.is_visible(timeout=2000):
                                view_btn = loc
                                logger.info(f"Found View selector using strategy {idx+1}.")
                                break
                        except Exception:
                            continue

                    if view_btn:
                        tag_name = view_btn.evaluate("el => el.tagName.toLowerCase()")
                        if tag_name == "select":
                            view_btn.select_option(label=filter_view)
                        else:
                            view_btn.click()
                            page.wait_for_timeout(1000)
                            option = page.get_by_role("option", name=filter_view, exact=False)
                            if not option.is_visible(timeout=1000):
                                option = page.locator(f".sapMSelectListItem:has-text('{filter_view}')")
                            if not option.is_visible(timeout=1000):
                                option = page.locator(f"text={filter_view}").last
                            option.click()
                        
                        logger.info(f"Successfully selected filter view: {filter_view}")
                        page.wait_for_timeout(3000)
                        self._wait_for_dashboard(page)
                        self._take_screenshot(page, "list_reports_post_filter")

                # Handle empty state
                if page.locator(".no-reports").filter(has_text="No reports found").is_visible(timeout=2000):
                    logger.info("No reports found on dashboard.")
                    return []

                # Selector options to locate report containers (supports Mock UI and standard Concur UIs)
                card_selectors = [".report-tile", ".report-card", ".cnqr-report-card", ".sapMCard"]
                cards = None
                for selector in card_selectors:
                    loc = page.locator(selector)
                    if loc.count() > 0:
                        cards = loc
                        logger.info(f"Found report cards using selector '{selector}'.")
                        break

                if not cards:
                    cards = page.locator(".sapMListUl .sapMLIB")
                
                count = cards.count()
                logger.info(f"Discovered {count} report item(s) on page.")

                for i in range(count):
                    card = cards.nth(i)
                    
                    # Extract Name
                    name_selectors = [
                        ".report-tile__header__text",
                        ".report-name",
                        ".cnqr-report-name",
                        ".sapMObjLTitle",
                        "h3",
                        "strong"
                    ]
                    name = "Unknown Report"
                    for ns in name_selectors:
                        sub = card.locator(ns)
                        if sub.count() > 0:
                            name = sub.first.text_content().strip()
                            break

                    # Extract Purpose / Info
                    purpose_selectors = [
                        ".report-purpose",
                        ".sapMObjLDescription",
                        "p"
                    ]
                    purpose = ""
                    for ps in purpose_selectors:
                        sub = card.locator(ps)
                        if sub.count() > 0:
                            purpose = sub.first.text_content().strip()
                            break

                    reports.append({
                        "index": i,
                        "name": name,
                        "purpose": purpose
                    })
                    logger.info(f"  Report {i+1}: {name} ({purpose})")

            except Exception as e:
                logger.error(f"Error listing reports: {str(e)}")
                raise e
            return reports

    def update_report(
        self,
        old_name: str,
        new_name: str,
        new_purpose: Optional[str] = None,
        new_comment: Optional[str] = None,
        headless: bool = True
    ) -> Dict[str, Any]:
        """
        [UPDATE] Locates an expense report by its current name, enters edit mode,
        modifies its headers, and saves it.
        """
        logger.info(f"Updating report '{old_name}' -> '{new_name}' via browser (headless={headless})...")
        with self._browser_page(headless=headless) as page:
            try:
                page.goto(f"{self.base_url}/nui/expense", timeout=30000)
                self._wait_for_dashboard(page)
                self._take_screenshot(page, "update_report_pre")

                # Find the card containing the old name
                card = page.locator(".report-tile, .report-card").filter(has_text=old_name)
                if card.count() == 0:
                    card = page.locator(".sapMCard, .sapMLIB").filter(has_text=old_name)
                
                if card.count() == 0:
                    raise FileNotFoundError(f"No report named '{old_name}' found to edit.")

                # Click "Edit" or open the report
                edit_btn = None
                edit_selectors = [
                    lambda c: c.get_by_role("button", name="Edit", exact=False),
                    lambda c: c.get_by_role("button", name="Modify", exact=False),
                    lambda c: c.locator("button:has-text('Edit')"),
                    lambda c: c.locator("button:has-text('Modify')")
                ]
                for get_sel in edit_selectors:
                    try:
                        loc = get_sel(card)
                        if loc.is_visible(timeout=2000):
                            edit_btn = loc
                            break
                    except Exception:
                        continue

                if edit_btn:
                    edit_btn.click()
                else:
                    card.first.click()
                    page.wait_for_timeout(2000)
                    
                    # Open 'Report Details' dropdown menu
                    details_btn = page.get_by_role("button", name="Report Details", exact=False)
                    if details_btn.is_visible(timeout=2000):
                        details_btn.click()
                        page.wait_for_timeout(1000)
                    
                    # If a dialog is already visible (e.g. from a direct click), we might not need the menu
                    dialog_selector = "#report-dialog, [role='dialog'][aria-modal='true'], .sapMDialog"
                    if page.locator(dialog_selector).filter(visible=True).count() == 0:
                        # Locate and click 'Report Header' (or 'Edit Report Info' on legacy UIs)
                        menu_item = None
                        menu_selectors = [
                            lambda p: p.get_by_role("menuitem", name="Report Header", exact=False),
                            lambda p: p.get_by_role("menuitem", name="Edit Report Info", exact=False),
                            lambda p: p.locator("text=Report Header"),
                            lambda p: p.locator("text=Edit Report Info"),
                            lambda p: p.get_by_text("Report Header", exact=False),
                            lambda p: p.get_by_text("Edit Report Info", exact=False)
                        ]
                        for idx, get_sel in enumerate(menu_selectors):
                            try:
                                loc = get_sel(page)
                                if loc.is_visible(timeout=2000):
                                    menu_item = loc
                                    break
                            except Exception:
                                continue

                        if menu_item:
                            menu_item.click()
                        else:
                            # If we still can't find it but a dialog is NOT open, we are stuck
                            if page.locator(dialog_selector).filter(visible=True).count() == 0:
                                self._take_screenshot(page, "report_header_menuitem_not_found")
                                logger.warning("Could not locate 'Report Header' dropdown item, but continuing to see if dialog appeared.")

                page.wait_for_timeout(2000)
                self._take_screenshot(page, "update_report_dialog")

                # Refill form fields
                name_input = page.get_by_role("textbox", name="Report Name", exact=False)
                if not name_input.is_visible(timeout=2000):
                    name_input = page.locator("#reportname, input[id*='reportname'], input[id*='ReportName'], input[name*='name']")
                
                if name_input.is_visible(timeout=2000):
                    name_input.fill(new_name)
                    logger.info(f"Filled Report Name: {new_name}")
                else:
                    raise RuntimeError("Could not find standard Report Name input field.")

                if new_purpose:
                    purpose_input = page.get_by_role("textbox", name="Purpose", exact=False)
                    if not purpose_input.is_visible(timeout=2000):
                        purpose_input = page.locator("#purpose, textarea[id*='purpose'], input[id*='purpose']")
                    if purpose_input.is_visible(timeout=2000):
                        purpose_input.fill(new_purpose)
                        logger.info("Filled Purpose field.")

                if new_comment:
                    comment_input = page.get_by_role("textbox", name="Comment", exact=False)
                    if not comment_input.is_visible(timeout=2000):
                        comment_input = page.locator("#comment, textarea[id*='comment'], input[id*='comment']")
                    if comment_input.is_visible(timeout=2000):
                        comment_input.fill(new_comment)
                        logger.info("Filled Comment field.")

                self._take_screenshot(page, "update_report_form_filled")

                # Save changes
                save_btn = page.get_by_role("button", name="Save", exact=True)
                if not save_btn.is_visible(timeout=2000):
                    save_btn = page.locator("#submit-report-btn, button:has-text('Save')")
                save_btn.click()
                
                page.wait_for_timeout(2000)
                self._take_screenshot(page, "update_report_post")
                logger.info(f"Report '{old_name}' successfully updated to '{new_name}'!")
                return {"success": True, "name": new_name}

            except Exception as e:
                self._take_screenshot(page, "update_error")
                raise e
    def update_report_transaction(
        self,
        report_name: str,
        transaction_indices: Optional[Union[int, List[int]]] = None,
        expense_type: Optional[str] = None,
        business_purpose: Optional[str] = None,
        comment: Optional[str] = None,
        headless: bool = True,
        transaction_index: Optional[int] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Updates the fields of one or more transactions inside an expense report.
        transaction_indices can be a single integer or a list of integers (1-based).
        To remove/clear a field, pass an empty string "".
        """
        # Handle transaction_index (0-based) vs transaction_indices (1-based)
        if transaction_indices is None:
            if transaction_index is not None:
                transaction_indices = transaction_index + 1
            elif "transaction_index" in kwargs:
                transaction_indices = kwargs["transaction_index"] + 1
            else:
                raise ValueError("Must provide either transaction_indices or transaction_index.")

        # If they passed transaction_index positionally as transaction_indices (e.g. 0), map it to 1-based indexing
        if isinstance(transaction_indices, int) and transaction_indices == 0:
            transaction_indices = 1
        elif isinstance(transaction_indices, list) and 0 in transaction_indices:
            transaction_indices = [idx + 1 if idx == 0 else idx for idx in transaction_indices]

        indices = [transaction_indices] if isinstance(transaction_indices, int) else transaction_indices
        logger.info(f"Updating transactions at indices {indices} in report '{report_name}' (headless={headless})...")
        with self._browser_page(headless=headless) as page:
            try:
                page.goto(f"{self.base_url}/nui/expense", timeout=30000)
                self._wait_for_dashboard(page)
                self._take_screenshot(page, "update_transaction_start")

                # Locate and open the report
                card_selectors = [".report-tile", ".report-card", ".sapMCard", ".sapMLIB", ".cnqr-report-card"]
                card = None
                for selector in card_selectors:
                    loc = page.locator(selector).filter(has_text=report_name)
                    if loc.count() > 0:
                        card = loc.first
                        break
                
                if not card:
                    raise FileNotFoundError(f"Could not find report '{report_name}'.")

                card.click()
                page.wait_for_timeout(3000)
                self._wait_for_report_view(page)
                self._take_screenshot(page, "update_transaction_report_opened")

                # Same row definition as the read and write paths, so index N
                # here is the same expense as index N in `report show`. The
                # comment above this block used to claim it filtered "exactly
                # like get_report_details"; it did not.
                valid_rows, _ = self._collect_expense_rows(page, report_name)

                if not valid_rows:
                    self._take_screenshot(page, "no_rows_found_debug")
                    # Collect page text for better error messaging
                    page_text = page.locator("body").text_content() or ""
                    snippet = " ".join(page_text.split()[:50])
                    raise RuntimeError(f"No valid transaction rows found in report. Page content snippet: {snippet}")
                
                logger.info(f"Discovered {len(valid_rows)} valid transaction row(s).")

                results = []
                for current_idx in indices:
                    try:
                        logger.info(f"Processing transaction index {current_idx}...")
                        if current_idx < 1 or current_idx > len(valid_rows):
                            logger.error(f"Transaction index {current_idx} is out of bounds (found {len(valid_rows)} rows).")
                            results.append({"index": current_idx, "success": False, "error": "Index out of bounds"})
                            continue

                        row = valid_rows[current_idx - 1]
                        
                        selection_successful = False
                        for attempt in range(4):
                            logger.info(f"  [{current_idx}] Selection attempt {attempt + 1}...")
                            
                            if attempt == 2:
                                logger.info(f"  [{current_idx}] UI seems stuck, reloading page...")
                                page.reload()
                                page.wait_for_timeout(5000)
                                self._dismiss_modals(page)
                                # Re-locate the row after reload

                            # 1. Re-identify rows to avoid staleness
                            current_valid_rows, _ = self._collect_expense_rows(page, report_name)
                            
                            if current_idx > len(current_valid_rows):
                                logger.warning(f"  [{current_idx}] Index out of range in this attempt (found {len(current_valid_rows)}).")
                                continue
                            
                            row = current_valid_rows[current_idx - 1]

                            # 2. Select target row
                            try:
                                # Scroll into view
                                row.scroll_into_view_if_needed()
                                cb = row.locator(".sapMCb, [type='checkbox']").first
                                if cb.count() > 0:
                                    cb.click(force=True)
                                else:
                                    row.click(force=True)
                                page.wait_for_timeout(2000)
                                
                                # Check if detail pane opened directly from row click (common in Fiori/Mock)
                                if page.locator("[data-nuiexp*='field'], input[id*='type'], .sapMInputBaseInner, .recon-type").filter(visible=True).count() > 0:
                                    logger.info(f"  [{current_idx}] Detail pane opened directly from row click.")
                                    selection_successful = True
                                    break
                            except Exception as exc:
                                logger.debug(f"update_report_transaction: ignoring {exc!r}")
                            
                            # 3. Final verification of 'Edit' button or Detail pane
                            edit_btn_selectors = [
                                "[data-nuiexp='edit-button']",
                                "button:has-text('Edit')",
                                ".sapMBtn:has-text('Edit')",
                                "button[title='Edit']"
                            ]
                            
                            for sel in edit_btn_selectors:
                                btn = page.locator(sel).first
                                if btn.count() > 0 and btn.is_visible():
                                    try:
                                        # Wait for it to be enabled
                                        btn.wait_for_element_state("enabled", timeout=3000)
                                        logger.info(f"  [{current_idx}] 'Edit' button enabled, clicking.")
                                        btn.click(force=True)
                                        # Wait for pane to appear
                                        try:
                                            page.wait_for_selector("[data-nuiexp*='field'], input[id*='type'], .sapMInputBaseInner", timeout=5000)
                                            selection_successful = True
                                            break
                                        except Exception as exc:
                                            logger.debug(f"update_report_transaction: ignoring {exc!r}")
                                    except Exception as exc:
                                        logger.debug(f"update_report_transaction: ignoring {exc!r}")
                            
                            if selection_successful:
                                break
                                
                            # Fallback: Use "Actions" kebab menu
                            logger.info(f"  [{current_idx}] Falling back to 'Actions' kebab menu...")
                            try:
                                actions_btn = row.locator("button[aria-label='Actions'], .entries-list-actions-button").first
                                if actions_btn.count() > 0:
                                    actions_btn.click(force=True)
                                    menu_item = page.locator(".sapMMenuItemText:has-text('Edit'), .sapMMenuItemText:has-text('Open'), [role='menuitem']:has-text('Edit')").first
                                    if menu_item.count() > 0:
                                        menu_item.click()
                                        try:
                                            page.wait_for_selector("[data-nuiexp*='field']", timeout=5000)
                                            selection_successful = True
                                            break
                                        except Exception as exc:
                                            logger.debug(f"update_report_transaction: ignoring {exc!r}")
                            except Exception as exc:
                                logger.debug(f"update_report_transaction: ignoring {exc!r}")

                            # Fallback: Double click the row
                            logger.info(f"  [{current_idx}] Falling back to double-click on row...")
                            try:
                                row.dblclick(force=True)
                                try:
                                    page.wait_for_selector("[data-nuiexp*='field'], input[id*='type']", timeout=5000)
                                    selection_successful = True
                                    break
                                except Exception as exc:
                                    logger.debug(f"update_report_transaction: ignoring {exc!r}")
                            except Exception as exc:
                                logger.debug(f"update_report_transaction: ignoring {exc!r}")
                                
                        if not selection_successful:
                            raise Exception(f"Failed to open transaction detail pane for index {current_idx}")
                        
                        # Extra wait for stability
                        page.wait_for_timeout(2000)
                        self._take_screenshot(page, f"transaction_{current_idx}_details_opened")

                        # Now verify if we have inputs. If not, we might need to wait more.
                        # The fields might be in the row (inline) or in a detail pane (right side)

                        # Focus on the detail pane/side panel
                        detail_pane = page.locator("#sapcnqr-layout-side-panel-elements, .sapcnqr-layout-side-panel__elements, .ere__dynamic-main-content").filter(visible=True).first
                        if detail_pane.count() == 0:
                            # Fallback to whole page if specific pane ID not found
                            detail_pane = page
                        # Tracking successes
                        updates_attempted = 0
                        updates_found = 0
                        
                        # Use a more robust approach to find inputs in the detail pane
                        input_context = detail_pane if detail_pane.count() > 0 else page
                        
                        # Same writer the JSON path uses. These blocks were
                        # duplicated and had drifted: only this copy verified the
                        # expense type, so the other silently dropped it.
                        field_errors = self._write_expense_fields(
                            page, input_context, expense_type=expense_type,
                            purpose=business_purpose, comment=comment)
                        updates_attempted = sum(
                            1 for v in (expense_type, business_purpose, comment)
                            if v is not None)
                        updates_found = updates_attempted - len(field_errors)
                        for err in field_errors:
                            logger.warning(f"  [{current_idx}] {err}")
                            results[current_idx-1]["success"] = False
                            results[current_idx-1]["validation_error"] = err


                        # Save the changes
                        saved, save_error = self._click_save_expense(page)
                        if not saved:
                            # This used to press Enter and then set saved = True
                            # regardless ("assume success if we reached here"),
                            # which reported an unsaved edit as saved. A save that
                            # could not be performed is a failure.
                            logger.error(f"  [{current_idx}] {save_error}")
                            results[current_idx-1]["success"] = False
                            results[current_idx-1]["validation_error"] = save_error

                        if saved:
                            logger.info(f"  [{current_idx}] Changes saved.")
                            
                            # Check for a validation/error modal
                            modal_msg = None
                            try:
                                modal = page.locator(".sapMDialog, .sapMMessageBox, .sapcnqr-modal, [role='dialog']").filter(has_text=re.compile(r"Error|Alert|Warning|Missing", re.I)).first
                                if modal.count() > 0 and modal.is_visible(timeout=2000):
                                    modal_msg = modal.text_content() or ""
                                    logger.warning(f"  [{current_idx}] Validation warning detected: {modal_msg.strip()[:100]}...")
                                    self._dismiss_modals(page)
                            except Exception as exc:
                                logger.debug(f"update_report_transaction: ignoring {exc!r}")

                            overall_success = (updates_found == updates_attempted)
                            results.append({
                                "index": current_idx, 
                                "success": overall_success, 
                                "partial_success": not overall_success and updates_found > 0,
                                "validation_error": modal_msg
                            })
                        else:
                            logger.warning(f"  [{current_idx}] Could not find a visible 'Save' button.")
                            # Final attempt: dispatch enter key on the comment field
                            try:
                                inp_comment.press("Enter")
                                logger.info(f"  [{current_idx}] Attempted Enter key on comment field.")
                                results.append({"index": current_idx, "success": True, "note": "Used Enter key instead of Save button"})
                            except Exception:
                                results.append({"index": current_idx, "success": False, "error": "Save button not found and Enter key failed"})
                        
                        page.wait_for_timeout(2000)

                    except Exception as sub_e:
                        logger.error(f"  [{current_idx}] Failed: {str(sub_e)}")
                        results.append({"index": current_idx, "success": False, "error": str(sub_e)})

                self._take_screenshot(page, "update_transactions_final")

                return {
                    "success": any(r["success"] for r in results),
                    "report_name": report_name,
                    "results": results
                }

            except Exception as e:
                logger.error(f"Failed to update report transactions: {str(e)}")
                self._take_screenshot(page, "update_transaction_error")
                return {"success": False, "error": str(e)}
    def get_transaction_allocations(self, report_name: str, transaction_index: int, headless: bool = True) -> Dict[str, Any]:
        """
        Opens the Allocations modal for a transaction and reads the current allocations.
        transaction_index is 0-based.
        """
        logger.info(f"Getting allocations for transaction {transaction_index} in report '{report_name}'...")
        with self._browser_page(headless=headless) as page:
            try:
                page.goto(f"{self.base_url}/nui/expense", timeout=30000)
                self._wait_for_dashboard(page)
                
                # Navigate to report
                self._open_report_by_name(page, report_name)
                
                self._open_allocations_modal(page, report_name, transaction_index)
                allocations = self._read_allocations_from_modal(page)
                return {"success": True, "allocations": allocations}

            except Exception as e:
                logger.error(f"Failed to get allocations: {str(e)}")
                self._take_screenshot(page, "get_allocations_error")
                return {"success": False, "error": str(e)}
    def _open_allocations_modal(self, page, report_name, transaction_index):
        """Open the Allocations modal for a 0-based transaction index.

        Shared by the read and write paths so they cannot drift apart.
        """
        valid_rows = self._get_transaction_rows(page)
        if transaction_index < 0 or transaction_index >= len(valid_rows):
            raise IndexError(f"Transaction index {transaction_index} out of bounds "
                             f"(found {len(valid_rows)} rows).")
        row = valid_rows[transaction_index]

        ALLOCATE_ITEM = (".menu-item:has-text('Allocate'), "
                         ".sapMMenuItemText:has-text('Allocate'), "
                         "[role='menuitem']:has-text('Allocate')")

        # Retry the kebab and *wait* for the menu item rather than checking once
        # after a fixed delay. Called straight after saving an expense pane the
        # grid is still re-rendering, so a single 1s wait found no menu and gave
        # up -- which failed every row of a combined write while the standalone
        # command, which opens the report fresh, worked fine.
        last_err = "Could not find 'Actions' button for transaction."
        for attempt in range(3):
            rows_now, _ = self._collect_expense_rows(page, report_name)
            if transaction_index >= len(rows_now):
                break
            row = rows_now[transaction_index]
            actions_btn = row.locator("[data-nui-widgets='menu-button-trigger'], "
                                      ".entries-list-actions-button, "
                                      "[aria-label='Actions']").first
            if actions_btn.count() == 0:
                page.wait_for_timeout(1500)
                continue
            actions_btn.click(force=True)
            try:
                # Wait on the first *visible* match, not the first match. Every
                # row's menu can exist in the DOM, so `wait_for_selector` settles
                # on another row's hidden item and times out even once this row's
                # menu has rendered.
                item = page.locator(ALLOCATE_ITEM).filter(visible=True).first
                item.wait_for(state="visible", timeout=4000)
            except Exception as exc:
                last_err = f"Could not find a visible 'Allocate' menu item ({exc!r})"
                page.keyboard.press("Escape")
                page.wait_for_timeout(1200)
                continue
            item.click()
            page.wait_for_timeout(2000)
            return row
        raise RuntimeError(last_err)

    def _read_allocations_from_modal(self, page):
        """Text of each allocation row in the open modal.

        Deliberately excludes the "Default Allocation" block. That block shows the
        expense's inherited chartstring, so counting it as an allocation makes an
        expense that has none look allocated -- and makes a failed write verify
        against the very code it failed to change.
        """
        allocations = []
        container = page.locator(".allocation-grid-container, #allocations-list, "
                                 ".sapMListUl").filter(visible=True).first
        if container.count() == 0:
            return allocations
        body = (container.text_content() or "")
        if "No Allocations" in body or "No allocations found" in body:
            return allocations
        for r in container.locator(".allocation-row, .sapMLIB, [role='row']").all():
            text = " ".join((r.text_content() or "").split())
            if not text or "Default Allocation" in text:
                continue
            # Skip the column-header row: it carries the sort affordances and the
            # select-all checkbox, and counting it as an allocation both inflates
            # the count and makes a cleared expense look allocated.
            if "Sort column" in text or text.startswith("Select all rows"):
                continue
            allocations.append({"raw_text": text})
        if not allocations:
            codes = container.locator(".allocation-code")
            for i in range(codes.count()):
                text = " ".join((codes.nth(i).text_content() or "").split())
                if text:
                    allocations.append({"raw_text": text})
        return allocations

    def _verify_allocation(self, page, report_name, transaction_index, values):
        """Which of `values` are absent from the transaction's allocations.

        Returns a list of missing values; empty means everything is present.
        Reloads the report first so the check reads persisted state rather than
        the modal that is still on screen.
        """
        try:
            page.goto(f"{self.base_url}/nui/expense", timeout=30000)
            self._wait_for_dashboard(page)
            self._open_report_by_name(page, report_name)
            self._dismiss_modals(page)
            self._open_allocations_modal(page, report_name, transaction_index)
            rows = self._read_allocations_from_modal(page)
        except Exception as e:
            logger.warning(f"Could not re-read allocations to verify: {e}")
            return [v for v in values]
        for row in rows:
            text = row["raw_text"].lower()
            if all(v.lower() in text for v in values):
                return []
        blob = " ".join(r["raw_text"] for r in rows).lower()
        return [v for v in values if v.lower() not in blob] or list(values)

    def _clear_allocations_on_page(self, page, report_name, transaction_index):
        """Clear a transaction's allocations using an already-open page.

        Page-level so `apply-json` can allocate inside the same browser session
        as the field and receipt writes. Returns (removed_count, error).
        """
        self._open_allocations_modal(page, report_name, transaction_index)
        before = self._read_allocations_from_modal(page)
        if not before:
            return 0, None

        container = page.locator(".allocation-grid-container, #allocations-list, "
                                 ".sapMListUl").filter(visible=True).first
        boxes = container.locator("input[type='checkbox']")
        if boxes.count() == 0:
            return 0, "no row checkboxes found in the allocations grid"
        # The first checkbox is the header's select-all; fall back to ticking
        # each row if it does not take.
        boxes.nth(0).click(force=True)
        page.wait_for_timeout(600)

        remove = page.locator("[data-nuiexp='allocations-removeBtn']").filter(visible=True).first
        if remove.count() == 0:
            return 0, "Remove button not found in the allocations modal"
        if not remove.is_enabled():
            for i in range(1, boxes.count()):
                boxes.nth(i).click(force=True)
            page.wait_for_timeout(600)
        remove.click(force=True)
        page.wait_for_timeout(1200)

        confirm = page.locator(
            "[role='alertdialog'] button:has-text('Remove'), "
            "[role='alertdialog'] button:has-text('Delete'), "
            "[role='alertdialog'] button:has-text('Yes'), "
            ".sapcnqr-message-dialog button:has-text('Remove'), "
            ".sapcnqr-message-dialog button:has-text('Yes')"
        ).filter(visible=True).first
        if confirm.count() > 0:
            confirm.click()
            page.wait_for_timeout(1200)

        save = page.locator("[data-nuiexp='allocation-modal-save']").filter(visible=True).first
        if save.count() == 0:
            save = page.locator("button:has-text('Save')").filter(visible=True).last
        if save.count() == 0:
            return 0, "Save button not found in the allocations modal"
        save.click(force=True)
        page.wait_for_timeout(3000)
        self._dismiss_modals(page)

        remaining = self._verify_allocations_cleared(page, report_name, transaction_index)
        if remaining:
            return 0, (f"{len(remaining)} allocation(s) still present after clearing: "
                       f"{remaining}")
        return len(before), None

    def _add_allocation_on_page(self, page, report_name, transaction_index,
                                department, fund, program=None):
        """Add one allocation using an already-open page. Returns (verified, error)."""
        self._open_allocations_modal(page, report_name, transaction_index)

        # Exact selector only. A comma-list including button:has-text('Add')
        # resolves in DOM order and matches the page's "Add Expense" toolbar
        # button, which the modal overlay then blocks.
        add_btn = page.locator("[data-nuiexp='allocations-addBtn']").filter(visible=True).first
        if add_btn.count() == 0:
            return [], ("Could not find the Allocations modal 'Add' button "
                        "([data-nuiexp='allocations-addBtn']).")
        add_btn.click(force=True)
        page.wait_for_timeout(2000)

        # Chartstring fields are the same combobox widget as the expense type, so
        # they go through the same verified writer.
        wanted = [("custom6", department, "Department"), ("custom7", fund, "Fund")]
        if program:
            wanted.append(("custom8", program, "Program"))
        field_errors = [err for err in
                        (self._set_select_field(page, page, key, val, label)
                         for key, val, label in wanted) if err]
        if field_errors:
            self._take_screenshot(page, "add_allocation_field_error")
            return [], "; ".join(field_errors)

        save_add_btn = page.locator("[data-nuiexp='Ct-add-btn']").filter(visible=True).first
        if save_add_btn.count() == 0:
            save_add_btn = page.locator("button:has-text('Save')").filter(visible=True).last
        if save_add_btn.count() == 0:
            return [], "Could not find 'Save' button in Add Allocation modal."
        save_add_btn.click()
        page.wait_for_timeout(2000)

        save_alloc_btn = page.locator("[data-nuiexp='allocation-modal-save']").filter(
            visible=True).first
        if save_alloc_btn.count() == 0:
            save_alloc_btn = page.locator("button:has-text('Save')").filter(visible=True).last
        save_alloc_btn.click()
        page.wait_for_timeout(3000)

        values = [v for _, v, _ in wanted]
        missing = self._verify_allocation(page, report_name, transaction_index, values)
        if missing:
            return [], ("allocation did not persist; the transaction's allocations do "
                        f"not mention {', '.join(missing)}")
        return values, None

    def remove_transaction_allocations(self, report_name: str, transaction_index: int,
                                       headless: bool = True) -> Dict[str, Any]:
        """Clear every allocation on a transaction, returning it to the report's
        default allocation. `transaction_index` is 0-based.

        `txn allocate` adds rather than replaces -- Concur's modal exposes Add,
        and a second allocation splits the expense by percentage rather than
        superseding the first. Replacing a chartstring therefore means clearing
        and re-adding.
        """
        logger.info(f"Clearing allocations on transaction {transaction_index} "
                    f"in report '{report_name}'...")
        with self._browser_page(headless=headless, viewport_height=900) as page:
            try:
                page.goto(f"{self.base_url}/nui/expense", timeout=30000)
                self._wait_for_dashboard(page)
                self._open_report_by_name(page, report_name)
                self._dismiss_modals(page)
                removed, err = self._clear_allocations_on_page(
                    page, report_name, transaction_index)
                if err:
                    return {"success": False, "error": err}
                logger.info(f"  Cleared {removed} allocation(s).")
                return {"success": True, "removed": removed}
            except Exception as e:
                logger.error(f"Failed to clear allocations: {e}")
                self._take_screenshot(page, "remove_allocations_error")
                return {"success": False, "error": str(e)}

    def _verify_allocations_cleared(self, page, report_name, transaction_index):
        """Allocation rows still on the transaction after a clear; [] means clear."""
        try:
            page.goto(f"{self.base_url}/nui/expense", timeout=30000)
            self._wait_for_dashboard(page)
            self._open_report_by_name(page, report_name)
            self._dismiss_modals(page)
            self._open_allocations_modal(page, report_name, transaction_index)
            return [a["raw_text"][:80] for a in self._read_allocations_from_modal(page)]
        except Exception as e:
            logger.warning(f"Could not re-read allocations to verify the clear: {e}")
            return ["verification failed: " + str(e)[:80]]

    def add_transaction_allocation(
        self,
        report_name: str,
        transaction_index: int,
        department: str,
        fund: str,
        program: Optional[str] = None,
        headless: bool = True,
    ) -> Dict[str, Any]:
        """Add an allocation to a transaction. `transaction_index` is 0-based.

        Adds; does not replace. Clear first with remove_transaction_allocations to
        supersede an existing chartstring rather than split the expense.
        """
        logger.info(f"Adding allocation to transaction {transaction_index} in report "
                    f"'{report_name}': Dept={department}, Fund={fund}, Prog={program}...")
        with self._browser_page(headless=headless) as page:
            try:
                page.goto(f"{self.base_url}/nui/expense", timeout=30000)
                self._wait_for_dashboard(page)
                self._open_report_by_name(page, report_name)
                self._dismiss_modals(page)
                verified, err = self._add_allocation_on_page(
                    page, report_name, transaction_index, department, fund, program)
                if err:
                    return {"success": False, "error": err}
                return {"success": True, "verified": verified}
            except Exception as e:
                logger.error(f"Failed to add allocation: {e}")
                self._take_screenshot(page, "add_allocation_error")
                return {"success": False, "error": str(e)}

    def delete_report(self, name: str, headless: bool = True) -> Dict[str, Any]:
        """
        Deletes a report by name.
        """
        logger.info(f"Deleting report '{name}' via browser (headless={headless})...")
        with self._browser_page(headless=headless) as page:
            try:
                page.goto(f"{self.base_url}/nui/expense", timeout=30000)
                self._wait_for_dashboard(page)
                self._take_screenshot(page, "delete_report_pre")

                # Find target report card
                card = page.locator(".report-tile, .report-card").filter(has_text=name)
                if card.count() == 0:
                    card = page.locator(".sapMCard, .sapMLIB").filter(has_text=name)

                if card.count() == 0:
                    raise FileNotFoundError(f"No report named '{name}' found to delete.")

                # Set up listener for dialog popups (like window.confirm prompt)
                page.on("dialog", lambda dialog: dialog.accept())

                # Click Delete
                delete_btn = card.get_by_role("button", name="Delete", exact=False)
                if delete_btn.is_visible(timeout=2000):
                    delete_btn.click()
                    logger.info("Clicked delete button on card.")
                else:
                    # In real Concur Fiori: open the report, click three-dot menu, click Delete Report
                    logger.info("Opening report details page to delete...")
                    card.first.click()
                    page.wait_for_timeout(3000)
                    
                    # Locate and click the '...' (More Options) button next to 'Submit Report'
                    more_btn = None
                    more_selectors = [
                        lambda p: p.get_by_role("button", name="Report Actions", exact=False),
                        lambda p: p.get_by_role("button", name="More Actions", exact=False),
                        lambda p: p.get_by_role("button", name="More Options", exact=False),
                        lambda p: p.get_by_role("button", name="More", exact=False),
                        lambda p: p.locator("button:has-text('...')"),
                        lambda p: p.locator("[class*='more']"),
                        lambda p: p.locator(".sapMBtnContent:has-text('...')")
                    ]
                    for get_sel in more_selectors:
                        try:
                            loc = get_sel(page)
                            if loc.is_visible(timeout=2000):
                                more_btn = loc
                                break
                        except Exception:
                            continue
                            
                    if not more_btn:
                        self._take_screenshot(page, "more_options_button_not_found")
                        raise RuntimeError("Could not locate three-dot (More Actions) button inside report.")
                        
                    more_btn.click()
                    page.wait_for_timeout(1000)
                    self._take_screenshot(page, "more_options_menu_open")
                    
                    # Click 'Delete Report' or 'Delete'
                    delete_item = None
                    delete_item_selectors = [
                        lambda p: p.get_by_role("menuitem", name="Delete Report", exact=False),
                        lambda p: p.get_by_role("menuitem", name="Delete", exact=False),
                        lambda p: p.locator("text=Delete Report"),
                        lambda p: p.locator("text=Delete")
                    ]
                    for get_sel in delete_item_selectors:
                        try:
                            loc = get_sel(page)
                            if loc.is_visible(timeout=2000):
                                delete_item = loc
                                break
                        except Exception:
                            continue
                            
                    if not delete_item:
                        raise RuntimeError("Could not locate 'Delete Report' menu item.")
                        
                    delete_item.click()
                    logger.info("Clicked 'Delete Report' menu item.")
                    
                    # Confirm popup if not handled automatically
                    try:
                        confirm_selectors = [
                            lambda p: p.get_by_role("button", name="Delete Report", exact=True),
                            lambda p: p.get_by_role("button", name="Delete", exact=True),
                            lambda p: p.get_by_role("button", name="Yes, Delete", exact=False),
                            lambda p: p.locator(".sapcnqr-button--primary:has-text('Delete Report')"),
                            lambda p: p.locator("button:has-text('Delete Report')").last
                        ]
                        confirm_btn = None
                        for get_sel in confirm_selectors:
                            try:
                                loc = get_sel(page)
                                if loc.is_visible(timeout=2000):
                                    confirm_btn = loc
                                    break
                            except Exception:
                                continue

                        if confirm_btn:
                            confirm_btn.click()
                            logger.info("Clicked confirmation button.")
                        else:
                            logger.warning("No confirmation button matched/visible.")
                    except Exception as ce:
                        logger.warning(f"Error handling confirmation: {str(ce)}")

                page.wait_for_timeout(3000)
                self._take_screenshot(page, "delete_report_post")
                logger.info(f"Report '{name}' successfully deleted!")
                return {"success": True}

            except Exception as e:
                self._take_screenshot(page, "delete_error")
                raise e
    def list_available_receipts(self, headless: bool = True) -> List[str]:
        """
        [READ RECEIPTS] Navigates to the Expense page and lists names of available receipts.
        """
        logger.info(f"Listing available receipts via browser (headless={headless})...")
        with self._browser_page(headless=headless) as page:
            receipts = []
            try:
                page.goto(f"{self.base_url}/nui/expense", timeout=30000)
                self._wait_for_dashboard(page)
                self._take_screenshot(page, "list_receipts_dashboard")

                # Locate specific receipt grid items to avoid instructions and loading skeletons
                items = page.locator(".receipt-grid-item")
                count = items.count()
                
                # Fallback to general thumbnails if specific classes are not found (e.g. in mock server)
                if count == 0:
                    items = page.locator(".available-receipt-thumbnail")
                    count = items.count()

                logger.info(f"Discovered {count} available receipt card(s) on page.")

                for i in range(count):
                    item = items.nth(i)
                    
                    # Try to extract the title/name of the receipt
                    name = ""
                    name_selectors = [
                        ".receipt-grid-item__header__text.receipt-grid-item__header--bold",
                        ".receipt-grid-item__header__text",
                        ".receipt-name"
                    ]
                    for ns in name_selectors:
                        sub = item.locator(ns)
                        if sub.count() > 0:
                            name = sub.first.text_content().strip()
                            break
                    
                    if not name:
                        # Fallback to stripping the text content of the item directly
                        name = item.text_content().strip()
                        if "\n" in name:
                            name = name.split("\n")[-1].strip()

                    # Clean up layout text and skeletons
                    if name:
                        name = name.replace("\n", " ").strip()
                    
                    if (name and 
                        "loading" not in name.lower() and 
                        "drag and drop" not in name.lower() and 
                        "valid file types" not in name.lower() and
                        "available receipts" not in name.lower() and
                        "upload new receipt" not in name.lower()):
                        receipts.append(name)
                        logger.info(f"  Receipt {i+1}: {name}")

            except Exception as e:
                logger.error(f"Error listing receipts: {str(e)}")
                raise e
            return list(set(receipts))

    def delete_available_receipt(self, receipt_name: str, headless: bool = True) -> Dict[str, Any]:
        """
        [DELETE RECEIPT] Navigates to the Expense page, locates the receipt thumbnail
        in the 'Available Receipts' section, opens it, clicks delete, and confirms.
        """
        logger.info(f"Deleting available receipt '{receipt_name}' via browser (headless={headless})...")
        with self._browser_page(headless=headless) as page:
            try:
                page.goto(f"{self.base_url}/nui/expense", timeout=30000)
                self._wait_for_dashboard(page)
                self._take_screenshot(page, "delete_receipt_dashboard_pre")

                # Find the receipt thumbnail
                thumb_selectors = [
                    lambda p: p.locator(".receipt-grid-item").filter(has_text=receipt_name),
                    lambda p: p.locator(".available-receipt-thumbnail").filter(has_text=receipt_name),
                    lambda p: p.locator("[class*='receipt']").filter(has_text=receipt_name)
                ]

                thumbnail = None
                for get_sel in thumb_selectors:
                    try:
                        loc = get_sel(page)
                        if loc.count() > 0:
                            thumbnail = loc.first
                            logger.info("Found receipt thumbnail to delete.")
                            break
                    except Exception:
                        continue

                if not thumbnail:
                    raise FileNotFoundError(f"No available receipt named '{receipt_name}' found.")

                page.on("dialog", lambda dialog: dialog.accept())

                thumbnail.click()
                page.wait_for_timeout(2000)
                self._take_screenshot(page, "delete_receipt_viewer_open")

                # Click the Delete button inside the viewer
                delete_btn = None
                delete_selectors = [
                    lambda p: p.get_by_role("button", name="Delete Receipt", exact=False),
                    lambda p: p.get_by_role("button", name="Delete", exact=False),
                    lambda p: p.locator("#delete-receipt-btn"),
                    lambda p: p.locator("button:has-text('Delete')")
                ]

                for idx, get_sel in enumerate(delete_selectors):
                    try:
                        loc = get_sel(page)
                        if loc.is_visible(timeout=2000):
                            delete_btn = loc
                            logger.info(f"Found receipt delete button using strategy {idx+1}.")
                            break
                    except Exception:
                        continue

                if not delete_btn:
                    raise RuntimeError("Could not locate Delete button in receipt viewer.")

                delete_btn.click()
                logger.info("Clicked delete button in receipt viewer.")
                
                try:
                    confirm_btn = page.get_by_role("button", name="Yes, Delete", exact=False)
                    if confirm_btn.is_visible(timeout=1000):
                        confirm_btn.click()
                        logger.info("Clicked confirmation confirmation button.")
                except Exception:
                    pass

                page.wait_for_timeout(3000)
                self._take_screenshot(page, "delete_receipt_post")
                logger.info(f"Receipt '{receipt_name}' successfully deleted!")
                return {"success": True}

            except Exception as e:
                self._take_screenshot(page, "delete_receipt_error")
                raise e
    REPORT_HEADER_BUTTON = ("button[aria-label*='activate to open report header details'], "
                            "[data-nuiexp='reportMenu.reportDetails']")
    REPORT_NAME_FIELD = "[data-nuiexp='field-name'], input#name"
    REPORT_PURPOSE_FIELD = ("[data-nuiexp='field-businessPurpose'], "
                            "textarea#businessPurpose")

    def _read_report_header(self, page, report_name):
        """Open the report header pane and read the report's own fields.

        Returns a dict, or None if the pane could not be opened or does not
        belong to this report. The header's Business Purpose field is
        `field-businessPurpose` / id `businessPurpose` -- exactly the same
        identifiers the per-expense field uses -- so the pane's identity is
        checked against the report name before anything is read out of it.
        Reading the wrong one would attribute an expense's purpose to the report.
        """
        try:
            btn = page.locator(self.REPORT_HEADER_BUTTON).filter(visible=True).first
            if btn.count() == 0:
                logger.debug("Report header button not found; leaving header fields unset.")
                return None
            btn.click(force=True)
            page.wait_for_timeout(2500)

            name_field = page.locator(self.REPORT_NAME_FIELD).filter(visible=True).first
            if name_field.count() == 0:
                logger.debug("Report header pane did not render its name field.")
                return None
            try:
                got_name = (name_field.input_value() or "").strip()
            except Exception:
                got_name = ""
            if report_name.strip() and report_name.strip() not in got_name:
                logger.warning(f"Report header pane shows {got_name!r} while reading "
                               f"{report_name!r}; not attributing its fields.")
                return None

            header = {
                "purpose": self._read_text_field(page, self.REPORT_PURPOSE_FIELD),
                # No Comment field exists in the header; report-level comments are
                # a separate thread, so this is empty rather than "Unknown".
                "comment": "",
                "report_id": self._read_text_field(page, "[data-nuiexp='field-reportId'], input#reportId"),
                "approval_status": self._read_text_field(
                    page, "[data-nuiexp='field-approvalStatus'], input#approvalStatus"),
                "payment_status": self._read_text_field(
                    page, "[data-nuiexp='field-paymentStatus'], input#paymentStatus"),
            }
            for sel in ("button:has-text('Cancel')", "button:has-text('Close')"):
                close = page.locator(sel).filter(visible=True).first
                if close.count() > 0:
                    close.click(force=True)
                    page.wait_for_timeout(1200)
                    break
            else:
                page.keyboard.press("Escape")
                page.wait_for_timeout(800)
            self._dismiss_modals(page)
            return header
        except Exception as e:
            logger.warning(f"Could not read the report header: {e}")
            return None

    def get_report_details(self, name: str, filter_view: Optional[str] = None, deep: bool = False, headless: bool = True) -> Dict[str, Any]:
        """
        Navigates to the Expense page, locates a report by name, clicks it to open detail view,
        and extracts report metadata and line-item expenses.
        Optionally selects a different filter view (e.g. 'Last 90 Days') first.
        """
        logger.info(f"Getting details for report '{name}' via browser (headless={headless}, filter={filter_view})...")
        with self._browser_page(headless=headless) as page:
            try:
                page.goto(f"{self.base_url}/nui/expense", timeout=30000)
                self._wait_for_dashboard(page)
                self._take_screenshot(page, "get_report_details_pre")

                if filter_view:
                    logger.info(f"Selecting report filter view: {filter_view}...")
                    view_btn = None
                    view_selectors = [
                        lambda p: p.locator("#report-view-select"),
                        lambda p: p.get_by_role("combobox", name="View", exact=False),
                        lambda p: p.locator("select[id*='view']"),
                        lambda p: p.locator(".sapMSelect, [class*='select']").filter(has_text="Reports").first,
                        lambda p: p.get_by_text("Active Reports", exact=True),
                        lambda p: p.locator("button:has-text('Active Reports')")
                    ]
                    for idx, get_sel in enumerate(view_selectors):
                        try:
                            loc = get_sel(page)
                            if loc.is_visible(timeout=2000):
                                view_btn = loc
                                break
                        except Exception:
                            continue

                    if view_btn:
                        tag_name = view_btn.evaluate("el => el.tagName.toLowerCase()")
                        if tag_name == "select":
                            view_btn.select_option(label=filter_view)
                        else:
                            view_btn.click()
                            page.wait_for_timeout(1000)
                            option = page.get_by_role("option", name=filter_view, exact=False)
                            if not option.is_visible(timeout=1000):
                                option = page.locator(f".sapMSelectListItem:has-text('{filter_view}')")
                            if not option.is_visible(timeout=1000):
                                option = page.locator(f"text={filter_view}").last
                            option.click()
                        page.wait_for_timeout(3000)
                        self._wait_for_dashboard(page)
                        self._take_screenshot(page, "get_report_details_post_filter")

                # Locate the card and click it to navigate into detail view
                card_selectors = [".report-tile", ".report-card", ".sapMCard", ".sapMLIB", ".cnqr-report-card"]
                card = None
                
                # Strategy 1: Substring match via has_text (case-insensitive)
                for selector in card_selectors:
                    loc = page.locator(selector).filter(has_text=name)
                    if loc.count() > 0:
                        card = loc.first
                        logger.info(f"Found report card using selector '{selector}' and has_text.")
                        break
                
                if not card:
                    # Strategy 2: More flexible whitespace-insensitive match
                    normalized_name = " ".join(name.split()).lower()
                    all_cards_loc = page.locator(", ".join(card_selectors))
                    count = all_cards_loc.count()
                    for i in range(count):
                        c = all_cards_loc.nth(i)
                        card_text = c.text_content() or ""
                        if normalized_name in " ".join(card_text.split()).lower():
                            card = c
                            logger.info(f"Found report card using flexible text matching at index {i}.")
                            break

                if not card:
                    self._take_screenshot(page, "report_not_found_debug")
                    # Collect names of what IS visible for better error message
                    visible_reports = []
                    try:
                        all_cards_loc = page.locator(", ".join(card_selectors))
                        for i in range(all_cards_loc.count()):
                            txt = all_cards_loc.nth(i).text_content()
                            if txt:
                                # Try to find the name specifically
                                first_line = txt.strip().split('\n')[0].strip()
                                visible_reports.append(first_line)
                    except Exception:
                        pass
                    
                    err_msg = f"No report named '{name}' found."
                    if visible_reports:
                        err_msg += f" Found these reports on page: {visible_reports}"
                    else:
                        err_msg += " No report cards visible on page."
                    
                    if filter_view:
                        err_msg += f" (Checked in filter: '{filter_view}')"
                    else:
                        err_msg += " (Checked in default view)"
                        
                    raise FileNotFoundError(err_msg)

                card.click()
                page.wait_for_timeout(3000)
                self._wait_for_report_view(page)
                self._dismiss_modals(page)
                self._take_screenshot(page, "get_report_details_opened")

                # Extract Report Details Header info
                report_num = "Unknown"
                purpose = "Unknown"
                comment = "Unknown"

                # Standard Fiori selectors for report header info
                # Report Number
                selectors_num = [
                    lambda p: p.locator("#detail-report-id"),
                    lambda p: p.locator("[class*='report-number']"),
                    lambda p: p.locator("text=Report Number:").locator(".."),
                    lambda p: p.get_by_role("button", name="Report Number", exact=False)
                ]
                for get_sel in selectors_num:
                    try:
                        loc = get_sel(page)
                        if loc.count() > 0:
                            raw = loc.first.text_content()
                            # Clean up prefix case-insensitively
                            import re
                            report_num = re.sub(r'(?i)^Report Number:?', '', raw).strip()
                            break
                    except Exception:
                        continue

                # Purpose
                selectors_purpose = [
                    lambda p: p.locator("#detail-purpose"),
                    lambda p: p.locator("text=Purpose:").locator(".."),
                    lambda p: p.locator("[class*='purpose']")
                ]
                for get_sel in selectors_purpose:
                    try:
                        loc = get_sel(page)
                        if loc.count() > 0:
                            raw = loc.first.text_content()
                            import re
                            purpose = re.sub(r'(?i)^Purpose:?', '', raw).strip()
                            break
                    except Exception:
                        continue

                # Comment
                selectors_comment = [
                    lambda p: p.locator("#detail-comment"),
                    lambda p: p.locator("text=Comment:").locator("..")
                ]
                for get_sel in selectors_comment:
                    try:
                        loc = get_sel(page)
                        if loc.count() > 0:
                            raw = loc.first.text_content()
                            import re
                            comment = re.sub(r'(?i)^Comment:?', '', raw).strip()
                            break
                    except Exception:
                        continue

                # The report's own Business Purpose lives in the header pane, and
                # its field is `field-businessPurpose` with id `businessPurpose`
                # -- byte-identical to the per-expense field. A page-wide wildcard
                # therefore could not tell them apart, and this scrape returned
                # "Unknown" for every report. Read it from the header pane, and
                # verify the pane is this report's before trusting it.
                header = self._read_report_header(page, name)
                if header is not None:
                    purpose = header.get("purpose", "")
                    # Concur's report header has no Comment field; report-level
                    # comments are a separate thread. Reporting "Unknown" implied a
                    # field that does not exist.
                    comment = header.get("comment", "")

                # Wait for line items to load specifically
                try:
                    self._dismiss_modals(page)

                    # Broaden wait selectors
                    page.locator(".sapMLIB, [class*='expense-item'], [class*='expense-row'], [role='row'], [role='listitem'], tr").first.wait_for(state="visible", timeout=10000)
                    logger.info("Line items detected in report details.")
                except Exception:
                    logger.warning("Timed out waiting for line items to appear using standard selectors.")

                # List expenses line items
                expenses = []
                # One shared definition of what an expense row is, so a row's
                # index means the same thing here as in the write paths.
                expense_rows, row_diagnostics = self._collect_expense_rows(page, name, report_num)

                for position, row in enumerate(expense_rows):
                    # Dense and 1-based: the only index ccworks exposes or accepts.
                    idx = position + 1
                    try:
                        text = " ".join((row.text_content() or "").split()).strip()
                        
                        # Initialize fields
                        date_str = ""
                        exp_type = "Unknown"
                        vendor = "Unknown"
                        amount = ""
                        payment_type = "Unknown"
                        
                        # Strategy: Many Concur rows follow: "Select expense, Type, Amount, date, Date Vendor Details..."
                        # Or they are just concatenated.
                        
                        # Try to find a date (MM/DD/YYYY or YYYY-MM-DD)
                        date_match = re.search(r'(\d{2}/\d{2}/\d{4})|(\d{4}-\d{2}-\d{2})', text)
                        if date_match:
                            date_str = date_match.group(0)
                            
                        # Try to find an amount ($X.XX)
                        amount_match = re.search(r'(\$\d{1,3}(?:,\d{3})*\.\d{2})', text)
                        if amount_match:
                            amount = amount_match.group(1)
                        
                        # Try parsing via anchors to handle commas in Type
                        # Structure: "Select expense, [Type], $[Amount], date, [Date] ..."
                        anchor_match = re.search(r'Select expense,\s*(.*?),\s*\$\d{1,3}(?:,\d{3})*\.\d{2},\s*date,', text)
                        if anchor_match:
                            exp_type = anchor_match.group(1)
                        elif "Type:" in text:
                            type_match = re.search(r'Type:\s*(.*?)(?:\||$)', text)
                            if type_match:
                                exp_type = type_match.group(1).strip()
                        else:
                            # Fallback to comma split if anchor fails
                            parts = [p.strip() for p in text.split(",")]
                            if len(parts) >= 4 and "Select expense" in parts[0]:
                                exp_type = parts[1]

                        # If we have a vendor/merchant, it's usually between the date and payment type
                        # This is tricky with raw text, so we'll do our best
                        if "Merchant:" in text:
                            merchant_match = re.search(r'Merchant:\s*(.*?)(?:\||$)', text)
                            if merchant_match:
                                vendor = merchant_match.group(1).strip()
                        elif date_str and amount:
                            # Try to find vendor between Date and Payment Type or Amount
                            # Example: "06/30/2026Computer Peripherals (OIT use only)ANTHROPIC* CLAUDE TEAMDepartmental Purchasing Card$400.00"
                            pattern = rf'{date_str}.*?{re.escape(exp_type)}?(.*?)(?:Departmental|Corporate|Personal|Cash|{re.escape(amount)})'
                            vendor_match = re.search(pattern, text)
                            if vendor_match:
                                vendor = vendor_match.group(1).strip()
                                if not vendor: vendor = "Unknown"

                        # Try to read fields from active inputs/selects or static labels if they exist in the row
                        business_purpose = ""
                        comment_field = ""
                        
                        try:
                            # Same reader as every other path. Only overrides the
                            # value parsed from the row text when the row actually
                            # carries an editable type control.
                            val = self._read_expense_type(row)
                            if val:
                                exp_type = val
                        except Exception as e:
                            logger.debug(f"Could not read type from input/select: {e}")

                        try:
                            # Check for input.recon-purpose
                            purpose_el = row.locator("input.recon-purpose, input[id*='purpose']").first
                            if purpose_el.count() > 0:
                                business_purpose = purpose_el.input_value()
                            else:
                                # Check for static text element
                                purpose_text_el = row.locator(".recon-purpose, [class*='purpose']").first
                                if purpose_text_el.count() > 0:
                                    business_purpose = purpose_text_el.text_content().strip()
                        except Exception as e:
                            logger.debug(f"Could not read business purpose from input/select: {e}")

                        try:
                            # Check for input.recon-comment
                            comment_el = row.locator("input.recon-comment, input[id*='comment']").first
                            if comment_el.count() > 0:
                                comment_field = comment_el.input_value()
                            else:
                                # Check for static text element
                                comment_text_el = row.locator(".recon-comment, [class*='comment']").first
                                if comment_text_el.count() > 0:
                                    comment_field = comment_text_el.text_content().strip()
                        except Exception as e:
                            logger.debug(f"Could not read comment from input/select: {e}")

                        # Broaden field extraction to ARIA labels and titles (common in Fiori)
                        full_context = text
                        try:
                            aria_label = row.get_attribute("aria-label") or ""
                            title_attr = row.get_attribute("title") or ""
                            full_context += f" | ARIA: {aria_label} | TITLE: {title_attr}"
                        except Exception:
                            pass

                        # If still Unknown, try extracting from full context (text + attributes)
                        if business_purpose == "Unknown" or not business_purpose:
                            purpose_match = re.search(r'(?i)business purpose:?\s*([^|]+)', full_context)
                            if purpose_match:
                                business_purpose = purpose_match.group(1).strip()
                            else:
                                business_purpose = ""

                        if comment_field == "Unknown" or not comment_field:
                            comment_match = re.search(r'(?i)comment:?\s*([^|]+)', full_context)
                            if comment_match:
                                comment_field = comment_match.group(1).strip()
                            else:
                                # Stricter icon detection to avoid false positives
                                try:
                                    # Look for buttons or icons that represent the comment bubble
                                    comment_btn = row.locator("button[class*='comment'], .sapMBtn[title*='Comment'], .sapUiIcon[title*='Comment'], i[class*='comment']").filter(visible=True).first
                                    if comment_btn.count() > 0:
                                        icon_text = comment_btn.get_attribute("title") or comment_btn.get_attribute("aria-label") or ""
                                        # Only accept it if it contains actual user text
                                        if icon_text and icon_text.strip().lower() not in ["", "comment", "comments", "view comment", "show comments", "add comment"]:
                                            comment_field = icon_text.strip()
                                except Exception:
                                    pass
                                if not comment_field:
                                    comment_field = ""

                        # Final fallback for Business Purpose from icons if not in text
                        if not business_purpose:
                            try:
                                purpose_icon = row.locator(".sapcnqr-icon--notes, [class*='purpose-icon'], .sapUiIcon[title*='Purpose']").first
                                if purpose_icon.count() > 0:
                                    icon_text = purpose_icon.get_attribute("title") or purpose_icon.get_attribute("aria-label") or ""
                                    if icon_text and "Purpose" not in icon_text:
                                        business_purpose = icon_text.strip()
                            except Exception:
                                pass

                        # Final object construction
                        exp_obj = {
                            "index": idx,
                            "date": date_str,
                            "expense_type": exp_type,
                            "type": exp_type,
                            "vendor": vendor,
                            "amount": amount,
                            "business_purpose": business_purpose if business_purpose.lower() not in ["", "unknown"] else "",
                            "comment": comment_field if comment_field.lower() not in ["", "unknown", "show comments", "comment", "comments"] else "",
                            "raw_text": text
                        }
                        
                        expenses.append(exp_obj)
                    except Exception:
                        continue

                # Deep scan: open each transaction to get full details
                # Rows whose detail pane will not open keep their shallow fields;
                # the failure is recorded so the caller can tell partial data from
                # complete data instead of trusting "success": true.
                deep_failures = []
                if deep:
                    # We determine the count first
                    total_to_scan = len(expenses)
                    logger.info(f"Performing deep scan on {total_to_scan} transactions...")
                    
                    for i in range(total_to_scan):
                        idx = i + 1
                        try:
                            logger.info(f"  Scanning transaction {idx} of {total_to_scan}...")

                            # Reload the report before every row after the first.
                            # Cancelling the detail pane does not reliably tear it
                            # down, so the next row opened onto the previous
                            # expense's pane and its business purpose, comment and
                            # type were attributed to the wrong expense.
                            if i > 0:
                                page.goto(f"{self.base_url}/nui/expense", timeout=45000)
                                self._wait_for_dashboard(page)
                                self._open_report_by_name(page, name)

                            # 1. Clear modals and wait for list
                            self._dismiss_modals(page)
                            try:
                                page.wait_for_selector(", ".join(self.EXPENSE_ROW_SELECTORS), timeout=20000, state="visible")
                            except Exception as e:
                                logger.warning(f"  Transaction list not immediately visible after back/cancel: {e}")
                                # Try one more wait or refresh
                                page.wait_for_timeout(2000)
                                if page.locator(", ".join(self.EXPENSE_ROW_SELECTORS)).count() == 0:
                                    logger.error("  List still not found. Attempting to scroll.")
                                    page.mouse.wheel(0, 500)
                                    page.wait_for_timeout(1000)
                            
                            # 2. Re-identify valid rows to avoid staleness, through the
                            # same helper the shallow pass used so `i` still refers to
                            # the same expense.
                            current_valid_rows, _ = self._collect_expense_rows(page, name, report_num)
                            
                            if i >= len(current_valid_rows):
                                logger.warning(f"  Transaction {idx} not found in current view. Skipping.")
                                continue
                            
                            row = current_valid_rows[i]
                            
                            row.scroll_into_view_if_needed()
                            
                            # 3. Open details (using robust logic with re-identification and fallbacks)
                            selection_successful = False
                            for attempt in range(3):
                                # Re-identify through the same helper as everything
                                # else. This used its own hardcoded selector list,
                                # which omitted the current grid class and did none
                                # of the header/placeholder filtering -- so `i`
                                # indexed a different list here than it did above,
                                # and every row opened its predecessor's pane.
                                rows, _ = self._collect_expense_rows(page, name, report_num)
                                if i >= len(rows): break
                                current_row = rows[i]
                                
                                # 1. Select the row
                                current_row.click(force=True)
                                page.wait_for_timeout(500)
                                
                                # 2. Try Edit button in toolbar
                                edit_btn = page.locator("button:has-text('Edit'), #edit-transaction-btn").filter(visible=True).first
                                if edit_btn.count() > 0 and edit_btn.is_enabled():
                                    edit_btn.click()
                                else:
                                    # 3. Fallback to Kebab menu
                                    try:
                                        actions_btn = current_row.locator("button[aria-label='Actions'], .entries-list-actions-button").first
                                        if actions_btn.count() > 0:
                                            actions_btn.click(force=True)
                                            menu_item = page.locator(".sapMMenuItemText:has-text('Edit'), .sapMMenuItemText:has-text('Open'), [role='menuitem']:has-text('Edit')").first
                                            if menu_item.count() > 0:
                                                menu_item.click()
                                    except Exception as exc:
                                        logger.debug(f"get_report_details: ignoring {exc!r}")
                                    
                                # 4. Fallback to double click
                                if page.locator("[data-nuiexp*='field'], input[id*='type']").count() == 0:
                                    current_row.dblclick(force=True)
                                    
                                # Check if opened
                                try:
                                    page.wait_for_selector("[data-nuiexp*='field'], input[id*='type']", timeout=3000)
                                    selection_successful = True
                                    break
                                except Exception as exc:
                                    logger.debug(f"get_report_details: ignoring {exc!r}")
                                
                            if not selection_successful:
                                logger.warning(f"  Failed to open detail pane for transaction {i+1}")
                                deep_failures.append({
                                    "index": i + 1,
                                    "vendor": expenses[i].get("vendor"),
                                    "amount": expenses[i].get("amount"),
                                    "reason": "detail pane did not open; shallow fields only",
                                })
                                continue
                            
                            self._dismiss_modals(page)
                            page.wait_for_timeout(1000)
                            self._take_screenshot(page, f"get_report_details_opened")

                            # Confirm the open pane really belongs to this row
                            # before reading anything out of it. Closing a pane
                            # does not always tear it down, so the previous
                            # expense's pane can still be on screen -- which
                            # attributed one expense's business purpose, comment
                            # and type to the next one, silently and plausibly.
                            pane_vendor = ""
                            try:
                                v_field = page.locator(
                                    "[data-nuiexp='field-vendorName'], input#vendorName").first
                                if v_field.count() > 0:
                                    pane_vendor = (v_field.input_value() or "").strip()
                            except Exception:
                                pane_vendor = ""
                            row_vendor = (expenses[i].get("vendor") or "").strip()
                            if pane_vendor and row_vendor:
                                a = pane_vendor.split()[0].rstrip("*").lower()
                                b = row_vendor.split()[0].rstrip("*").lower()
                                if a != b:
                                    logger.warning(
                                        f"  Detail pane shows {pane_vendor!r} while scanning "
                                        f"{row_vendor!r}; refusing to attribute its fields.")
                                    deep_failures.append({
                                        "index": i + 1,
                                        "vendor": expenses[i].get("vendor"),
                                        "amount": expenses[i].get("amount"),
                                        "reason": (f"detail pane belonged to {pane_vendor!r}, "
                                                   f"not this expense; shallow fields only"),
                                    })
                                    continue

                            # 5. Extract fields (using precise Fiori selectors)
                            # Business Purpose
                            try:
                                expenses[i]["business_purpose"] = self._read_text_field(
                                    page, self.PURPOSE_FIELD_SELECTORS)
                            except Exception as exc:
                                logger.debug(f"get_report_details: ignoring {exc!r}")
                            
                            # Expense Type. Shares one reader with the write paths:
                            # this copy searched `[data-nuiexp*='type']`, which in a
                            # live report matches grid cells and the quick-tips
                            # help panel rather than the field.
                            try:
                                val = self._read_expense_type(page)
                                if val:
                                    expenses[i]["expense_type"] = val
                                    expenses[i]["type"] = val
                            except Exception as exc:
                                logger.debug(f"get_report_details: ignoring {exc!r}")
                            
                            # Comment
                            try:
                                val = self._read_text_field(page, self.COMMENT_FIELD_SELECTORS)
                                if val.lower() not in ("", "comment", "comments", "show comments"):
                                    expenses[i]["comment"] = val
                            except Exception as exc:
                                logger.debug(f"get_report_details: ignoring {exc!r}")
                            
                            # 6. Back to list
                            clicked_back = False
                            
                            # Prioritize clicking back/cancel INSIDE the side panel or detail pane
                            detail_pane_sel = "#sapcnqr-layout-side-panel-elements, .sapcnqr-layout-side-panel__elements, .ere__dynamic-main-content, [data-nuiexp*='panel'], [class*='side-panel'], [class*='detail-pane'], [class*='details-pane']"
                            detail_pane = page.locator(detail_pane_sel).filter(visible=True).first
                            if detail_pane.count() > 0:
                                pane_back_selectors = [
                                    "button:has-text('Cancel')",
                                    "button:has-text('Back')",
                                    ".sapMBtn:has-text('Cancel')",
                                    ".sapMBtn:has-text('Back')",
                                    "[data-nuiexp*='cancel']",
                                    "[data-nuiexp*='back']",
                                    ".sapcnqr-icon--nav-back",
                                    "button:has-text('Close')",
                                    ".sapMBtn:has-text('Close')",
                                    "button[title*='Close']",
                                    "button[aria-label*='Close']",
                                    "[class*='close']",
                                    "[class*='cancel']"
                                ]
                                for sel in pane_back_selectors:
                                    btn = detail_pane.locator(sel).first
                                    if btn.count() > 0 and btn.is_visible():
                                        logger.info(f"  Clicking back/cancel button INSIDE detail pane: {sel}")
                                        self._dismiss_modals(page)
                                        btn.click(force=True)
                                        clicked_back = True
                                        break
                                
                                if not clicked_back:
                                    logger.warning("  Detail pane is visible but no back/cancel button found inside. Attempting Escape...")
                                    self._dismiss_modals(page)
                                    page.keyboard.press("Escape")
                                    page.wait_for_timeout(2000)
                                    # If detail pane is now gone, consider back navigation successful
                                    if page.locator(detail_pane_sel).filter(visible=True).count() == 0:
                                        clicked_back = True
                                        
                            if not clicked_back:
                                # Fallback to page-level selectors ONLY if detail pane is not visible
                                if page.locator(detail_pane_sel).filter(visible=True).count() == 0:
                                    page_back_selectors = [
                                        ".sapcnqr-icon--nav-back",
                                        "[data-nuiexp='exit-full-screen-button']",
                                        ".sapMBtnBack",
                                        "button[title*='Back']",
                                        "button[aria-label*='Back']",
                                        "button[id*='back']",
                                        "button:has-text('Cancel')",
                                        "button:has-text('Back')"
                                    ]
                                    for sel in page_back_selectors:
                                        btn = page.locator(sel).first
                                        if btn.count() > 0 and btn.is_visible():
                                            logger.info(f"  Clicking back/cancel button using page-level selector: {sel}")
                                            self._dismiss_modals(page)
                                            btn.click(force=True)
                                            clicked_back = True
                                            break

                            # Wait and VERIFY we are back in the list, NOT the dashboard
                            if clicked_back:
                                page.wait_for_timeout(2000)
                                if page.locator(".report-tile").count() > 0 and page.locator(", ".join(self.EXPENSE_ROW_SELECTORS)).count() == 0:
                                    logger.warning("  Oops! Went back to dashboard. Re-opening report...")
                                    report_card = page.locator(".report-tile").filter(has_text=name).first
                                    if report_card.count() > 0:
                                        report_card.click()
                                        page.wait_for_timeout(3000)
                            else:
                                # Check if detail pane is still visible
                                if page.locator("#sapcnqr-layout-side-panel-elements").filter(visible=True).count() > 0:
                                    logger.warning("  Detail pane still visible. Trying Escape key.")
                                    page.keyboard.press("Escape")
                                    page.wait_for_timeout(2000)
                            
                        except Exception as e:
                            logger.error(f"  Failed to deep scan transaction {idx}: {e}")
                            deep_failures.append({
                                "index": idx,
                                "vendor": expenses[idx - 1].get("vendor") if idx - 1 < len(expenses) else None,
                                "amount": expenses[idx - 1].get("amount") if idx - 1 < len(expenses) else None,
                                "reason": f"deep scan error: {e}",
                            })
                            # Try to recover by reloading
                            page.reload()
                            page.wait_for_timeout(5000)
                    
                    # Add discovered types to result
                    if 'available_types' in locals():
                        res_data = locals().get('res_data', {}) # This might be outside scope, better use return dict
                        # I'll just rely on returning it later
                
                # Rows are no longer deduplicated by text. They come from a single
                # querySelectorAll pass, which yields each element exactly once, so
                # two entries with identical text are two distinct rows -- e.g. two
                # shipments booked the same day for the same amount, which is
                # ordinary on a purchasing-card statement. Dropping the second was
                # silent data loss on a reconciliation tool. Identical rows are
                # reported instead, so a genuine scraping duplicate is still visible.
                text_counts = {}
                for exp in expenses:
                    text_counts[exp["raw_text"]] = text_counts.get(exp["raw_text"], 0) + 1
                identical_groups = [
                    {"raw_text": text, "count": count}
                    for text, count in text_counts.items() if count > 1
                ]
                if identical_groups:
                    logger.info(
                        f"{len(identical_groups)} group(s) of line items are textually identical "
                        f"and were all kept; verify against Concur if unexpected."
                    )

                if not expenses:
                    logger.warning("No expenses found. Capturing diagnostic screenshot and page text.")
                    self._take_screenshot(page, "empty_report_details_debug")
                    # Log all text elements that are visible for debugging
                    all_text = page.locator("body").text_content()
                    logger.info(f"Page text content snippet: {all_text[:1000]}...")

                # What the scrape did *not* capture belongs in the payload, not
                # only in -v logs. "success": true previously coexisted with
                # dropped rows and failed deep scans with no way to tell.
                extraction = {
                    "candidates_seen": row_diagnostics["candidates_seen"],
                    "expenses_returned": len(expenses),
                    "skipped_candidates": row_diagnostics["skipped"],
                    "identical_line_items": identical_groups,
                }
                if deep:
                    extraction["deep_scan_failures"] = deep_failures
                    extraction["complete"] = not deep_failures
                else:
                    extraction["complete"] = True

                return {
                    "success": True,
                    "report_name": name,
                    "report_number": report_num,
                    "purpose": purpose,
                    "comment": comment,
                    "index_base": "1-based, dense, matches `txn update` and `report apply-json`",
                    "extraction": extraction,
                    "expenses": expenses
                }

            except Exception as e:
                self._take_screenshot(page, "get_report_details_error")
                raise e
    def get_report_allocations(self, report_name: str, filter_view: Optional[str] = None, headless: bool = True) -> Dict[str, Any]:
        """
        Navigates to report details, opens the '*Princeton Detailed Report CBS' print view,
        and parses the detailed text for allocations and chartstrings.
        """
        logger.info(f"Querying detailed allocations for report '{report_name}' via Print/Share menu...")
        with self._browser_page(headless=headless) as page:
            try:
                page.goto(f"{self.base_url}/nui/expense", timeout=30000)
                self._wait_for_dashboard(page)
                if filter_view:
                    logger.info(f"Selecting report filter view: {filter_view}...")
                    view_btn = None
                    view_selectors = [
                        lambda p: p.locator("#report-view-select"),
                        lambda p: p.get_by_role("combobox", name="View", exact=False),
                        lambda p: p.locator("select[id*='view']"),
                        lambda p: p.locator(".sapMSelect, [class*='select']").filter(has_text="Reports").first,
                        lambda p: p.get_by_text("Active Reports", exact=True),
                        lambda p: p.locator("button:has-text('Active Reports')")
                    ]
                    for idx, get_sel in enumerate(view_selectors):
                        try:
                            loc = get_sel(page)
                            if loc.is_visible(timeout=2000):
                                view_btn = loc
                                break
                        except Exception:
                            continue

                    if view_btn:
                        tag_name = view_btn.evaluate("el => el.tagName.toLowerCase()")
                        if tag_name == "select":
                            view_btn.select_option(label=filter_view)
                        else:
                            view_btn.click()
                            page.wait_for_timeout(1000)
                            option = page.get_by_role("option", name=filter_view, exact=False)
                            if not option.is_visible(timeout=1000):
                                option = page.locator(f".sapMSelectListItem:has-text('{filter_view}')")
                            if not option.is_visible(timeout=1000):
                                option = page.locator(f"text={filter_view}").last
                            option.click()
                        page.wait_for_timeout(3000)
                        self._wait_for_dashboard(page)

                # Extra wait for dynamic cards to populate
                page.wait_for_timeout(5000)

                # 1. Locate and open the report using robust logic
                card_selectors = [".report-tile", ".report-card", ".sapMCard", ".sapMLIB", ".cnqr-report-card"]
                card = None
                
                # Strategy 1: Substring match via has_text (case-insensitive)
                for selector in card_selectors:
                    loc = page.locator(selector).filter(has_text=report_name)
                    if loc.count() > 0:
                        card = loc.first
                        logger.info(f"Found report card using selector '{selector}' and has_text.")
                        break
                
                if not card:
                    # Strategy 2: More flexible whitespace-insensitive match
                    normalized_name = " ".join(report_name.split()).lower()
                    all_cards_loc = page.locator(", ".join(card_selectors))
                    count = all_cards_loc.count()
                    print(f"DEBUG: Checking {count} total potential cards for a match...", file=sys.stderr)
                    for i in range(count):
                        c = all_cards_loc.nth(i)
                        card_text = c.text_content() or ""
                        clean_text = " ".join(card_text.split())
                        print(f"DEBUG:   Card {i} text: '{clean_text}'", file=sys.stderr)
                        if normalized_name in clean_text.lower():
                            card = c
                            logger.info(f"Found report card using flexible text matching at index {i}.")
                            break

                if not card:
                    print(f"DEBUG: Current URL: {page.url}", file=sys.stderr)
                    print(f"DEBUG: Page Title: {page.title()}", file=sys.stderr)
                    self._take_screenshot(page, "allocations_report_not_found")
                    # Try to find ANY text that looks like reports
                    try:
                        all_text = page.locator("body").text_content()
                        print(f"DEBUG: Page Text (first 1000 chars): {all_text[:1000]}", file=sys.stderr)
                    except Exception:
                        pass
                    raise FileNotFoundError(f"Could not find report '{report_name}'.")

                card.click()
                page.wait_for_timeout(3000)
                self._wait_for_report_view(page)
                self._take_screenshot(page, "allocations_report_opened")

                # 1. Click Print/Share
                try:
                    print_btn = page.locator("button:has-text('Print/Share'), .sapMBtn:has-text('Print/Share')").first
                    print_btn.click()
                    page.wait_for_timeout(1000)
                    self._take_screenshot(page, "print_menu_open")
                except Exception as e:
                    logger.warning(f"Failed to find Print/Share button: {str(e)}")
                    # Fallback to existing logic if Print/Share fails
                    return {"success": False, "error": "Could not find Print/Share menu"}

                # 2. Click '*Princeton Detailed Report CBS' and catch popup
                try:
                    # Look for the menu item with retries and multiple selector strategies
                    menu_item_selectors = [
                        "text='*Princeton Detailed Report CBS'",
                        "[role='menuitem']:has-text('Princeton Detailed Report CBS')",
                        ".sapMSelectListItem:has-text('Princeton Detailed Report CBS')",
                        "button:has-text('Princeton Detailed Report CBS')",
                        "li:has-text('Princeton Detailed Report CBS')"
                    ]
                    
                    target_item = None
                    for attempt in range(3):
                        for selector in menu_item_selectors:
                            loc = page.locator(selector).first
                            if loc.is_visible():
                                target_item = loc
                                break
                        if target_item: break
                        page.wait_for_timeout(1500)
                        # Re-click Print/Share if menu didn't appear
                        if attempt > 0:
                            print_btn.click()
                    
                    if not target_item:
                        # Final attempt: search by text globally
                        target_item = page.get_by_text("Princeton Detailed Report CBS", exact=False).first

                    logger.info(f"Attempting to click menu item: '{target_item.text_content().strip()}'")
                    # Debug: log the tag and classes
                    tag_name = target_item.evaluate("el => el.tagName")
                    logger.info(f"Target element tag: {tag_name}")

                    # Attempt to trigger the report view
                    # We'll try to handle both popups and in-page modals
                    print_page = None
                    full_text = None

                    try:
                        # Try to catch a popup first (older Concur style)
                        with page.expect_popup(timeout=5000) as popup_info:
                            try:
                                target_item.click(timeout=2000)
                            except Exception:
                                page.evaluate("el => el.click()", target_item.element_handle())
                        print_page = popup_info.value
                        print_page.wait_for_load_state("networkidle")
                        full_text = print_page.locator("body").text_content()
                        self._take_screenshot(print_page, "detailed_report_popup_view")
                        print_page.close()
                    except Exception:
                        # If no popup, it's likely an in-page modal (newer Concur style)
                        logger.info("No popup detected, checking for in-page modal...")
                        # The click might have already happened in the try block above, 
                        # but let's ensure it's clicked if we didn't get a popup.
                        dialog_selector = "div[role='dialog'], .print-report-dialog"
                        if not page.locator(dialog_selector).is_visible():
                            try:
                                target_item.click(force=True)
                            except Exception:
                                page.evaluate("el => el.click()", target_item.element_handle())
                        
                        # Wait for dialog to appear
                        page.locator(dialog_selector).first.wait_for(state="visible", timeout=15000)
                        self._take_screenshot(page, "detailed_report_modal_view")
                        
                        # Extract text from the dialog body
                        dialog_body = page.locator(".print-report-dialog__body, .sapcnqr-dialog__body").first
                        full_text = dialog_body.text_content()
                        
                        # Close the modal to clean up
                        close_btn = page.locator("button:has-text('Close'), .sapMBtn:has-text('Close')").last
                        if close_btn.is_visible():
                            close_btn.click()

                    if not full_text:
                        raise RuntimeError("Failed to capture detailed report text from either popup or modal.")
                except Exception as e:
                    logger.warning(f"Failed to open detailed report popup: {str(e)}")
                    # Capture current page state for debugging
                    self._take_screenshot(page, "print_menu_failure_debug")
                    return {"success": False, "error": f"Failed to open detailed report: {str(e)}"}

                # 3. Parse the detailed text
                import re
                allocations = []
                
                # Normalize text: replace non-breaking spaces and multi-spaces
                clean_text = full_text.replace('\u00a0', ' ')
                
                # Pattern for an expense row followed by allocations
                # 1. Find all dates as anchors
                date_matches = list(re.finditer(r'(\d{2}/\d{2}/\d{4})', clean_text))
                
                for idx, match in enumerate(date_matches):
                    start = match.start()
                    end = date_matches[idx+1].start() if idx + 1 < len(date_matches) else len(clean_text)
                    section = clean_text[start:end]
                    
                    # Inside this section, look for "Allocations :"
                    if "Allocations :" in section:
                        date = match.group(1)
                        # Extract amount (e.g., $400.00)
                        amount_match = re.search(r'\$(\d{1,3}(?:,\d{3})*(?:\.\d{2}))', section)
                        amount = amount_match.group(0) if amount_match else "Unknown"
                        
                        # Extract chartstring (e.g., 25605-A0006)
                        # Pattern: 5 digits - 1 letter + 4 digits
                        chartstring_match = re.search(r'(\d{5}-[A-Z]\d{4})', section)
                        chartstring = chartstring_match.group(1) if chartstring_match else "Unknown"
                        
                        # Guess the expense type (right after the date)
                        type_match = re.search(r'\d{2}/\d{2}/\d{4}\s+([^\$]+?)\s+', section)
                        exp_type = type_match.group(1).strip() if type_match else "Unknown"
                        
                        allocations.append({
                            # NOT the shared row index. This is derived from the
                            # position of date matches in the printed report, so
                            # any date that is not an expense row shifts it -- it
                            # read one higher than `report show` for every row.
                            # Named distinctly so it cannot be passed to
                            # `txn allocate`, which would allocate a different
                            # expense than the caller was looking at. Correlate on
                            # date + amount instead.
                            "section_number": idx + 1,
                            "index": None,
                            "date": date,
                            "type": exp_type,
                            "amount": amount,
                            "chartstring": chartstring,
                            "raw_section": section[:200].strip() + "..." if len(section) > 200 else section.strip()
                        })

                return {
                    "success": True,
                    "report_name": report_name,
                    "allocations": allocations,
                    "raw_text_summary": clean_text[:1000] + "..." if len(clean_text) > 1000 else clean_text
                }

            except Exception as e:
                self._take_screenshot(page, "allocations_error")
                raise e
    def list_card_transactions(self, card_type_filter: str = "All Corporate and Personal Cards", headless: bool = True) -> List[Dict[str, Any]]:
        """
        Navigates to the Expense page, locates the Available Expenses section,
        selects the card type activity view (e.g. All Corporate and Personal Cards, All Purchasing Cards),
        and lists available credit card transactions.
        """
        logger.info(f"Listing card transactions with filter '{card_type_filter}' via browser (headless={headless})...")
        with self._browser_page(headless=headless) as page:
            transactions = []
            try:
                page.goto(f"{self.base_url}/nui/expense", timeout=30000)
                self._wait_for_dashboard(page)
                self._take_screenshot(page, "list_card_transactions_pre")

                # Filter dropdown selector for available expenses/card transactions
                filter_btn = None
                filter_selectors = [
                    lambda p: p.locator("#card-view-select"),
                    lambda p: p.get_by_role("combobox", name="Activity", exact=False),
                    lambda p: p.locator("select[id*='card']"),
                    lambda p: p.locator("button:has-text('All Corporate and Personal Cards')"),
                    lambda p: p.locator("button:has-text('All Purchasing Cards')")
                ]
                for idx, get_sel in enumerate(filter_selectors):
                    try:
                        loc = get_sel(page)
                        if loc.is_visible(timeout=2000):
                            filter_btn = loc
                            logger.info(f"Found card filter view dropdown using strategy {idx+1}.")
                            break
                    except Exception:
                        continue

                if filter_btn:
                    tag_name = filter_btn.evaluate("el => el.tagName.toLowerCase()")
                    if tag_name == "select":
                        filter_btn.select_option(label=card_type_filter)
                    else:
                        filter_btn.click()
                        page.wait_for_timeout(1000)
                        
                        # Select option
                        option = page.get_by_role("option", name=card_type_filter, exact=False)
                        if not option.is_visible(timeout=1000):
                            option = page.locator(f"text={card_type_filter}").last
                        option.click()
                    
                    logger.info(f"Selected card filter view: {card_type_filter}")
                    page.wait_for_timeout(3000)
                    self._wait_for_dashboard(page)
                    self._take_screenshot(page, "list_card_transactions_post_filter")

                # Extract list of transactions
                rows = page.locator(".card-transaction-row, .card-transaction-item, [class*='transaction'], [class*='card-view']").all()
                logger.info(f"Discovered {len(rows)} potential transaction item(s) on page.")

                for idx, row in enumerate(rows):
                    try:
                        text = row.text_content().strip()
                        # Deduplicate instructions/headers
                        if text and ("uber" in text.lower() or "office" in text.lower() or "starbucks" in text.lower() or "amount" in text.lower() or "amazon" in text.lower() or "$" in text.lower()):
                            transactions.append({
                                "index": idx,
                                "raw_text": text
                            })
                            logger.info(f"  Transaction {idx+1}: {text}")
                    except Exception:
                        continue

            except Exception as e:
                logger.error(f"Error listing card transactions: {str(e)}")
                raise e
            return transactions

    def get_card_transaction_details(self, merchant_or_id: str, card_type_filter: Optional[str] = None, headless: bool = True) -> Dict[str, Any]:
        """
        Navigates to the Expense page, locates the card transaction row matching merchant_or_id,
        clicks it to open the transaction details dialog, and extracts full details.
        Optionally selects a different card type filter first.
        """
        logger.info(f"Getting details for transaction matching '{merchant_or_id}' via browser (headless={headless}, filter={card_type_filter})...")
        with self._browser_page(headless=headless) as page:
            try:
                page.goto(f"{self.base_url}/nui/expense", timeout=30000)
                self._wait_for_dashboard(page)
                self._take_screenshot(page, "get_transaction_details_pre")

                if card_type_filter:
                    logger.info(f"Selecting card filter view: {card_type_filter}...")
                    filter_btn = None
                    filter_selectors = [
                        lambda p: p.locator("#card-view-select"),
                        lambda p: p.get_by_role("combobox", name="Activity", exact=False),
                        lambda p: p.locator("select[id*='card']"),
                        lambda p: p.locator("button:has-text('All Corporate and Personal Cards')"),
                        lambda p: p.locator("button:has-text('All Purchasing Cards')")
                    ]
                    for idx, get_sel in enumerate(filter_selectors):
                        try:
                            loc = get_sel(page)
                            if loc.is_visible(timeout=2000):
                                filter_btn = loc
                                break
                        except Exception:
                            continue

                    if filter_btn:
                        tag_name = filter_btn.evaluate("el => el.tagName.toLowerCase()")
                        if tag_name == "select":
                            filter_btn.select_option(label=card_type_filter)
                        else:
                            filter_btn.click()
                            page.wait_for_timeout(1000)
                            option = page.get_by_role("option", name=card_type_filter, exact=False)
                            if not option.is_visible(timeout=1000):
                                option = page.locator(f"text={card_type_filter}").last
                            option.click()
                        page.wait_for_timeout(3000)
                        self._wait_for_dashboard(page)
                        self._take_screenshot(page, "get_transaction_details_post_filter")

                # Find the row containing merchant_or_id
                row = page.locator(".card-transaction-row, .card-transaction-item, [class*='transaction']").filter(has_text=merchant_or_id).first
                if row.count() == 0:
                    row = page.locator("*:has-text('" + merchant_or_id + "')").last

                row.click()
                page.wait_for_timeout(3000)
                self._take_screenshot(page, "get_transaction_details_open")

                # Extract details from modal
                merchant = "Unknown"
                date = "Unknown"
                amount = "Unknown"
                tx_id = "Unknown"
                card_prog = "Unknown"

                selectors_merchant = [lambda p: p.locator("#tx-merchant"), lambda p: p.locator("text=Merchant:").locator("..")]
                for get_sel in selectors_merchant:
                    try:
                        loc = get_sel(page)
                        if loc.count() > 0:
                            merchant = loc.first.text_content().replace("Merchant:", "").strip()
                            break
                    except Exception:
                        continue

                selectors_date = [lambda p: p.locator("#tx-date"), lambda p: p.locator("text=Date:").locator("..")]
                for get_sel in selectors_date:
                    try:
                        loc = get_sel(page)
                        if loc.count() > 0:
                            date = loc.first.text_content().replace("Date:", "").strip()
                            break
                    except Exception:
                        continue

                selectors_amount = [lambda p: p.locator("#tx-amount"), lambda p: p.locator("text=Amount:").locator("..")]
                for get_sel in selectors_amount:
                    try:
                        loc = get_sel(page)
                        if loc.count() > 0:
                            amount = loc.first.text_content().replace("Amount:", "").strip()
                            break
                    except Exception:
                        continue

                selectors_id = [lambda p: p.locator("#tx-id"), lambda p: p.locator("text=Transaction ID:").locator("..")]
                for get_sel in selectors_id:
                    try:
                        loc = get_sel(page)
                        if loc.count() > 0:
                            tx_id = loc.first.text_content().replace("Transaction ID:", "").strip()
                            break
                    except Exception:
                        continue

                selectors_prog = [lambda p: p.locator("#tx-program"), lambda p: p.locator("text=Card Program:").locator("..")]
                for get_sel in selectors_prog:
                    try:
                        loc = get_sel(page)
                        if loc.count() > 0:
                            card_prog = loc.first.text_content().replace("Card Program:", "").strip()
                            break
                    except Exception:
                        continue

                return {
                    "success": True,
                    "merchant": merchant,
                    "date": date,
                    "amount": amount,
                    "transaction_id": tx_id,
                    "card_program": card_prog
                }

            except Exception as e:
                self._take_screenshot(page, "get_transaction_details_error")
                raise e
    def add_expense_delegate(self, name_or_email: str, permissions: Optional[List[str]] = None, headless: bool = True) -> Dict[str, Any]:
        """
        Navigates to the Expense Delegates settings page, adds a delegate by name or email,
        sets their checkboxes based on permissions list, and saves the settings.
        """
        logger.info(f"Adding delegate '{name_or_email}' with permissions {permissions} via browser (headless={headless})...")
        if not permissions:
            permissions = ["prepare"] # Default permission

        with self._browser_page(headless=headless) as page:
            try:
                # Direct navigation to the edit delegates page
                delegates_url = f"{self.base_url}/profile/editdelegates.asp?ObjectType=1"
                logger.info(f"Navigating to Concur Expense Delegates: {delegates_url}")
                page.goto(delegates_url, timeout=30000)
                page.wait_for_load_state("load")
                page.wait_for_timeout(3000)
                self._take_screenshot(page, "add_delegate_pre")

                # Step 1: Click 'Add' or search delegate button
                add_btn = None
                add_selectors = [
                    lambda p: p.locator("#add-delegate-btn"),
                    lambda p: p.get_by_role("button", name="Add", exact=True),
                    lambda p: p.get_by_role("button", name="Add Delegate", exact=False),
                    lambda p: p.locator("button:has-text('Add')")
                ]
                for idx, get_sel in enumerate(add_selectors):
                    try:
                        loc = get_sel(page)
                        if loc.is_visible(timeout=2000):
                            add_btn = loc
                            break
                    except Exception:
                        continue

                if not add_btn:
                    raise RuntimeError("Could not locate 'Add' delegate button.")

                add_btn.click()
                page.wait_for_timeout(1000)

                # Step 2: Fill in the search input
                search_input = None
                search_selectors = [
                    lambda p: p.locator("#delegate-search-input"),
                    lambda p: p.get_by_role("textbox", name="search", exact=False),
                    lambda p: p.locator("input[placeholder*='name']"),
                    lambda p: p.locator("input[placeholder*='delegate']")
                ]
                for idx, get_sel in enumerate(search_selectors):
                    try:
                        loc = get_sel(page)
                        if loc.is_visible(timeout=2000):
                            search_input = loc
                            break
                    except Exception:
                        continue

                if not search_input:
                    raise RuntimeError("Could not locate delegate search input field.")

                search_input.fill(name_or_email)
                page.wait_for_timeout(2000)
                self._take_screenshot(page, "add_delegate_searching")

                # Click the matched suggestion item
                suggestion = None
                suggestion_selectors = [
                    lambda p: p.locator("#suggestion-john") if "john" in name_or_email.lower() else p.locator("#suggestion-jane"),
                    lambda p: p.locator(".suggestion-item").first,
                    lambda p: p.locator("[class*='suggestion']").first,
                    lambda p: page.get_by_role("listitem").first
                ]
                for get_sel in suggestion_selectors:
                    try:
                        loc = get_sel(page)
                        if loc.is_visible(timeout=2000):
                            suggestion = loc
                            break
                    except Exception:
                        continue

                if not suggestion:
                    raise RuntimeError(f"Could not locate autocomplete suggestion for '{name_or_email}'.")

                suggestion.click()
                page.wait_for_timeout(2000)
                self._take_screenshot(page, "add_delegate_added_to_table")

                # Step 3: Find delegate row and set permission checkboxes
                row = page.locator(".delegate-row, tr").filter(has_text=name_or_email).first
                if row.count() == 0:
                    row = page.locator("tr:has-text('" + name_or_email + "')").first

                if row.count() == 0:
                    raise RuntimeError(f"Could not find delegate row for '{name_or_email}' in table.")

                # Set checkboxes
                # Usually there are columns: Prepare, Submit, Approve, Receives Emails
                if "prepare" in permissions:
                    chk_prepare = row.locator(".perm-prepare, input[type='checkbox']").nth(1) # nth(0) is row selection
                    if not chk_prepare.is_checked():
                        chk_prepare.check()
                        logger.info("Checked 'Can Prepare' permission.")
                
                if "submit" in permissions:
                    chk_submit = row.locator(".perm-submit, input[type='checkbox']").nth(2)
                    if not chk_submit.is_checked():
                        chk_submit.check()
                        logger.info("Checked 'Can Submit Reports' permission.")

                if "approve" in permissions:
                    chk_approve = row.locator(".perm-approve, input[type='checkbox']").nth(3)
                    if not chk_approve.is_checked():
                        chk_approve.check()
                        logger.info("Checked 'Can Approve' permission.")

                self._take_screenshot(page, "add_delegate_permissions_checked")

                # Click Save settings button
                save_btn = None
                save_selectors = [
                    lambda p: p.locator("#save-delegates-btn"),
                    lambda p: p.get_by_role("button", name="Save", exact=True),
                    lambda p: p.locator("button:has-text('Save')")
                ]
                for get_sel in save_selectors:
                    try:
                        loc = get_sel(page)
                        if loc.is_visible(timeout=2000):
                            save_btn = loc
                            break
                    except Exception:
                        continue

                if not save_btn:
                    raise RuntimeError("Could not locate Save settings button on Delegates page.")

                page.on("dialog", lambda dialog: dialog.accept())
                save_btn.click()
                page.wait_for_timeout(3000)
                self._take_screenshot(page, "add_delegate_saved")

                logger.info(f"Successfully added delegate '{name_or_email}'!")
                return {"success": True}

            except Exception as e:
                self._take_screenshot(page, "add_delegate_error")
                raise e
    def remove_expense_delegate(self, name_or_email: str, headless: bool = True) -> Dict[str, Any]:
        """
        Navigates to the Expense Delegates settings page, locates a delegate by name or email,
        selects them, clicks the Delete button, and saves the settings.
        """
        logger.info(f"Removing delegate '{name_or_email}' via browser (headless={headless})...")
        with self._browser_page(headless=headless) as page:
            try:
                delegates_url = f"{self.base_url}/profile/editdelegates.asp?ObjectType=1"
                logger.info(f"Navigating to Concur Expense Delegates: {delegates_url}")
                page.goto(delegates_url, timeout=30000)
                page.wait_for_load_state("load")
                page.wait_for_timeout(3000)
                self._take_screenshot(page, "remove_delegate_pre")

                # Step 1: Find the delegate row
                row = page.locator(".delegate-row, tr").filter(has_text=name_or_email).first
                if row.count() == 0:
                    row = page.locator("tr:has-text('" + name_or_email + "')").first

                if row.count() == 0:
                    raise FileNotFoundError(f"No delegate named '{name_or_email}' found to remove.")

                # Step 2: Check the row selection checkbox (first column)
                select_chk = row.locator(".delegate-select-chk, input[type='checkbox']").first
                select_chk.check()
                logger.info(f"Checked delegate selection checkbox for '{name_or_email}'.")
                page.wait_for_timeout(1000)
                self._take_screenshot(page, "remove_delegate_selected")

                # Step 3: Click 'Delete' button
                delete_btn = None
                delete_selectors = [
                    lambda p: p.locator("#delete-delegate-btn"),
                    lambda p: p.get_by_role("button", name="Delete", exact=True),
                    lambda p: p.locator("button:has-text('Delete')")
                ]
                for get_sel in delete_selectors:
                    try:
                        loc = get_sel(page)
                        if loc.is_visible(timeout=2000):
                            delete_btn = loc
                            break
                    except Exception:
                        continue

                if not delete_btn:
                    raise RuntimeError("Could not locate 'Delete' delegate button.")

                page.on("dialog", lambda dialog: dialog.accept())
                delete_btn.click()
                page.wait_for_timeout(2000)
                self._take_screenshot(page, "remove_delegate_clicked_delete")

                # Step 4: Click 'Save' to apply deletion
                save_btn = None
                save_selectors = [
                    lambda p: p.locator("#save-delegates-btn"),
                    lambda p: p.get_by_role("button", name="Save", exact=True),
                    lambda p: p.locator("button:has-text('Save')")
                ]
                for get_sel in save_selectors:
                    try:
                        loc = get_sel(page)
                        if loc.is_visible(timeout=2000):
                            save_btn = loc
                            break
                    except Exception:
                        continue

                if not save_btn:
                    raise RuntimeError("Could not locate Save settings button.")

                save_btn.click()
                page.wait_for_timeout(3000)
                self._take_screenshot(page, "remove_delegate_saved")

                logger.info(f"Successfully removed delegate '{name_or_email}'!")
                return {"success": True}

            except Exception as e:
                self._take_screenshot(page, "remove_delegate_error")
                raise e
    def reconcile_report(self, report_name: str, reconciliation_rules: Dict[str, Dict[str, str]], headless: bool = True, submit: bool = False) -> Dict[str, Any]:
        """
        Automates month-end reconciliation: opens the report details view,
        iterates over all transaction rows, matches them with reconciliation rules,
        inputs Expense Type, Business Purpose, Comment, and Allocation Codes,
        saves each row, and optionally submits the entire report when all are reconciled.
        """
        logger.info(f"Starting month-end reconciliation for report '{report_name}' via browser (headless={headless}, submit={submit})...")
        with self._browser_page(headless=headless) as page:
            try:
                page.goto(f"{self.base_url}/nui/expense", timeout=30000)
                self._wait_for_dashboard(page)
                self._take_screenshot(page, "reconcile_start")

                # Locate and open the report
                card = page.locator(".report-tile, .report-card").filter(has_text=report_name).first
                if card.count() == 0:
                    card = page.locator(".sapMCard, .sapMLIB").filter(has_text=report_name).first
                if card.count() == 0:
                    raise FileNotFoundError(f"Could not find report '{report_name}'.")

                card.click()
                page.wait_for_timeout(3000)
                self._wait_for_report_view(page)
                self._take_screenshot(page, "reconcile_opened_report")

                # Iterate through reconciliation rows
                rows = page.locator(".transaction-recon-row, .detail-row").all()
                logger.info(f"Discovered {len(rows)} line item(s) to reconcile.")

                # These selectors do not exist in current Concur markup -- only in
                # the mock -- so against a live report this list comes back empty,
                # the loop below never runs, and the function used to return
                # {"success": True} having reconciled nothing. A reconcile that
                # matched no rows is a failure, not a silent no-op.
                if not rows:
                    return {"success": False, "submitted": False,
                            "error": ("no reconcilable line items found. This path's row "
                                      "selectors (.transaction-recon-row, .detail-row) do "
                                      "not match current Concur markup; use "
                                      "`report apply-json`, which addresses rows by index "
                                      "through the shared row helper.")}

                for idx, row in enumerate(rows):
                    merchant_elem = row.locator(".recon-merchant, strong").first
                    if merchant_elem.count() == 0:
                        continue
                    
                    raw_text = merchant_elem.text_content().strip()
                    logger.info(f"Checking line item {idx+1}: '{raw_text}'...")

                    # Match with rule key (case insensitive)
                    matched_rule = None
                    for key, rule in reconciliation_rules.items():
                        if key.lower() in raw_text.lower():
                            matched_rule = rule
                            break

                    if not matched_rule:
                        logger.warning(f"No reconciliation rule matched for '{raw_text}'. Skipping.")
                        continue

                    logger.info(f"Reconciling item '{raw_text}' using rule: {matched_rule}")
                    
                    # If inputs are not found in the row, it might be because we need to click the row to open a side panel
                    has_inline_inputs = row.locator("select.recon-type, input.recon-purpose").first.count() > 0
                    if not has_inline_inputs:
                        logger.info(f"  Inputs not found in row. Clicking row to open detail pane...")
                        row.click()
                        page.wait_for_timeout(2000)
                    
                    # Target inputs - check both row and page (for side panel)
                    input_context = page if not has_inline_inputs else row
                    
                    # Same writer the other two paths use. This copy had no
                    # read-back at all, so a rule that matched but never applied
                    # still counted as reconciled.
                    field_errors = self._write_expense_fields(
                        page, input_context,
                        expense_type=matched_rule.get("expense_type"),
                        purpose=matched_rule.get("business_purpose"),
                        comment=matched_rule.get("comment"))
                    if field_errors:
                        logger.warning(f"  Transaction '{raw_text}': " + "; ".join(field_errors))

                    # Allocation Code
                    inp_alloc = input_context.locator("input.recon-allocation, input[id*='allocation']").first
                    if inp_alloc.count() > 0:
                        inp_alloc.fill(matched_rule.get("allocation_code", ""))
                    
                    # Optional receipt attachment
                    receipt_path = matched_rule.get("receipt_path") or matched_rule.get("receipt")
                    if receipt_path:
                        import os
                        if os.path.exists(receipt_path):
                            inp_receipt = input_context.locator("input.recon-receipt-file, input[type='file'], input[id*='receipt']").first
                            if inp_receipt.count() > 0:
                                inp_receipt.set_input_files(receipt_path)
                                page.wait_for_timeout(2000)
                                logger.info(f"  Attached receipt '{receipt_path}' to transaction '{raw_text}'.")
                            else:
                                logger.warning(f"  Could not find receipt upload input for '{raw_text}'.")
                    
                    # Save this transaction
                    saved, save_error = self._click_save_expense(page, input_context)
                    if not saved:
                        logger.warning(f"  Transaction '{raw_text}': {save_error}")
                    
                    if not saved and not has_inline_inputs:
                        # Try closing the pane if we can't save
                        logger.warning(f"  Could not save transaction '{raw_text}'. Closing pane.")
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(1000)

                self._take_screenshot(page, "reconcile_all_saved")

                if not submit:
                    logger.info("Report reconciliation completed. Skipping submission as requested (submit=False).")
                    return {"success": True, "submitted": False}

                # Click Submit Report
                submit_btn = page.locator("#submit-entire-report-btn").first
                if submit_btn.count() > 0 and submit_btn.is_enabled():
                    # Register dialog accept handler
                    page.on("dialog", lambda dialog: dialog.accept())
                    submit_btn.click()
                    page.wait_for_timeout(3000)
                    self._take_screenshot(page, "reconcile_submitted")
                    logger.info("Report successfully submitted!")
                    return {"success": True, "submitted": True}
                else:
                    raise RuntimeError("Submit Report button is missing or not enabled. Check if all transactions are reconciled.")

            except Exception as e:
                self._take_screenshot(page, "reconcile_error")
                raise e
    def attach_receipt_to_transaction(self, report_name: str, merchant_or_id: str, receipt_file_path: str, headless: bool = True) -> Dict[str, Any]:
        """
        Navigates to the report details view, locates the transaction row matching merchant_or_id,
        and uploads a local receipt file (PDF/image) to match/associate it with the transaction.
        """
        logger.info(f"Attaching receipt '{receipt_file_path}' to transaction matching '{merchant_or_id}' in report '{report_name}'...")
        with self._browser_page(headless=headless) as page:
            try:
                page.goto(f"{self.base_url}/nui/expense", timeout=30000)
                self._wait_for_dashboard(page)
                self._take_screenshot(page, "attach_receipt_start")

                # Locate and open the report
                card = page.locator(".report-tile, .report-card").filter(has_text=report_name).first
                if card.count() == 0:
                    card = page.locator(".sapMCard, .sapMLIB").filter(has_text=report_name).first
                if card.count() == 0:
                    raise FileNotFoundError(f"Could not find report '{report_name}'.")

                card.click()
                page.wait_for_timeout(3000)
                self._wait_for_report_view(page)
                self._take_screenshot(page, "attach_receipt_report_opened")

                # Find the transaction row matching merchant_or_id
                row_selectors = [".transaction-recon-row", ".detail-row", ".sapMLIB", "tr[role='row']"]
                row = page.locator(", ".join(row_selectors)).filter(has_text=merchant_or_id).first
                if row.count() == 0:
                    # Log what IS there for debugging
                    all_rows = page.locator(", ".join(row_selectors)).all()
                    row_texts = [r.text_content().strip().split('\n')[0] for r in all_rows[:5]]
                    raise FileNotFoundError(f"Could not find transaction matching '{merchant_or_id}'. Visible rows: {row_texts}")

                logger.info(f"  Found transaction row for '{merchant_or_id}'.")

                # Locate file input element - check row first, then click row and check page (side panel)
                input_file = row.locator("input.recon-receipt-file, input[type='file']").first
                if input_file.count() == 0:
                    logger.info("  Receipt input not found in row. Clicking row to open detail pane...")
                    
                    # Try clicking multiple times or different ways to ensure panel opens
                    row.click(force=True)
                    page.wait_for_timeout(2000)
                    
                    # Check if panel is visible, if not, try clicking the merchant text specifically
                    panel_selectors = [
                        "#sapcnqr-layout-side-panel-elements",
                        ".sapcnqr-layout-side-panel__elements",
                        "[data-nuiexp*='panel']",
                        ".ere__dynamic-main-content",
                        ".sapMResponsivePopover"
                    ]
                    
                    panel = None
                    for _ in range(2): # Try twice
                        for sel in panel_selectors:
                            p_loc = page.locator(sel).filter(visible=True).first
                            if p_loc.count() > 0:
                                panel = p_loc
                                break
                        if panel: break
                        
                        logger.info("  Side panel not detected yet. Retrying click on merchant text...")
                        merchant_elem = row.locator(".recon-merchant, strong, b").first
                        if merchant_elem.count() > 0:
                            merchant_elem.click(force=True)
                        else:
                            row.click(force=True)
                        page.wait_for_timeout(2000)

                    if panel:
                        logger.info(f"  Detected side panel.")
                        input_file = panel.locator("input.recon-receipt-file, input[type='file'], input[id*='receipt']").first
                    else:
                        logger.warning("  Side panel could not be detected. Searching whole page for any file input.")
                        input_file = page.locator("input.recon-receipt-file, input[type='file'], input[id*='receipt']").first

                if input_file.count() == 0:
                    self._take_screenshot(page, "attach_receipt_input_not_found_debug")
                    # Final attempt: look for ANY input that might be the one
                    all_inputs = page.locator("input").all()
                    input_details = []
                    for inp in all_inputs[:10]:
                        try:
                            input_details.append(f"{inp.get_attribute('type') or 'text'}:{inp.get_attribute('class') or ''}:{inp.get_attribute('id') or ''}")
                        except Exception as exc:
                            logger.debug(f"attach_receipt_to_transaction: ignoring {exc!r}")
                    raise RuntimeError(f"Could not find file input for receipt upload in transaction '{merchant_or_id}'. Found inputs: {input_details}")

                input_file.set_input_files(receipt_file_path)
                page.wait_for_timeout(3000)

                self._take_screenshot(page, "attach_receipt_completed")
                logger.info(f"Successfully attached receipt '{receipt_file_path}' to transaction!")
                return {"success": True}

            except Exception as e:
                self._take_screenshot(page, "attach_receipt_error")
                raise e
    def submit_report(self, report_name: str, headless: bool = True) -> Dict[str, Any]:
        """
        Locates an expense report by name, opens it, and clicks the 'Submit Report' button.
        Handles the confirmation dialog that typically follows.
        """
        logger.info(f"Submitting report '{report_name}' via browser (headless={headless})...")
        with self._browser_page(headless=headless) as page:
            try:
                page.goto(f"{self.base_url}/nui/expense", timeout=30000)
                self._wait_for_dashboard(page)
                self._take_screenshot(page, "submit_report_start")

                # Locate and open the report
                card = page.locator(".report-tile, .report-card").filter(has_text=report_name).first
                if card.count() == 0:
                    card = page.locator(".sapMCard, .sapMLIB").filter(has_text=report_name).first
                
                if card.count() == 0:
                    raise FileNotFoundError(f"Could not find report '{report_name}'.")

                card.click()
                page.wait_for_timeout(3000)
                self._wait_for_report_view(page)
                self._take_screenshot(page, "submit_report_opened")

                # A leftover timeline / What's New overlay intercepts pointer
                # events on the report view, so clear it before reaching for
                # the toolbar button.
                self._dismiss_modals(page)

                # Click Submit Report
                # The button ID #submit-entire-report-btn is often used in the modern UI
                submit_btn = page.locator("#submit-entire-report-btn, button:has-text('Submit Report')").filter(visible=True).first

                if submit_btn.count() == 0:
                    raise RuntimeError("Submit Report button not found on this page.")
                if not submit_btn.is_enabled():
                    raise RuntimeError("Submit Report button is disabled. Ensure all alerts are resolved and justifications are filled.")

                # Register dialog accept handler for native confirm()/alert()
                page.on("dialog", lambda dialog: dialog.accept())

                submit_btn.click()
                logger.info("Clicked 'Submit Report' button.")

                # The report-totals confirmation is where submission actually
                # happens. Scope the click to inside the dialog: the toolbar
                # button behind the overlay still matches the same text and is
                # unclickable, so a page-wide .first would time out here and
                # leave the report in draft.
                confirmed = False
                dialog = page.locator(self.SUBMIT_CONFIRM_DIALOG).filter(visible=True).first
                try:
                    dialog.wait_for(state="visible", timeout=15000)
                except PlaywrightTimeoutError:
                    logger.info("No report-totals confirmation appeared; treating the first click as the submit.")
                else:
                    self._take_screenshot(page, "submit_report_totals_dialog")
                    confirm_btn = dialog.locator(self.SUBMIT_CONFIRM_BUTTON).filter(visible=True).first
                    try:
                        confirm_btn.wait_for(state="visible", timeout=10000)
                        confirm_btn.click()
                        confirmed = True
                        logger.info("Clicked 'Submit Report' inside the report-totals confirmation.")
                    except PlaywrightTimeoutError as exc:
                        raise RuntimeError(
                            "Report-totals confirmation dialog opened but no submit control "
                            "could be found inside it; the report was NOT submitted."
                        ) from exc

                page.wait_for_timeout(5000)
                self._take_screenshot(page, "submit_report_final")

                # An unanswered confirmation still on screen means the report
                # is still in draft. Say so instead of reporting success.
                if page.locator(self.SUBMIT_CONFIRM_DIALOG).filter(visible=True).count() > 0:
                    raise RuntimeError(
                        "Report-totals confirmation dialog is still open after the confirm "
                        "click; the report was NOT submitted."
                    )

                # Verify. page.url is not evidence on its own -- the dashboard
                # URL is the same one we started from.
                if page.locator("text=Report Successfully Submitted").count() > 0:
                    logger.info("Report successfully submitted!")
                    return {"success": True, "message": "Report successfully submitted", "confirmed": confirmed}
                if confirmed:
                    logger.info("Confirmation dialog answered and dismissed; report submitted.")
                    return {"success": True, "message": "Report successfully submitted", "confirmed": True}

                logger.warning("Submit clicked, but submission could not be verified. Check the report in Concur.")
                return {"success": False, "message": "Submit clicked, but submission could not be verified", "confirmed": False}

            except Exception as e:
                self._take_screenshot(page, "submit_report_error")
                logger.error(f"Failed to submit report: {str(e)}")
                raise e
    # --- Private Helpers ---

    def _open_report_by_name(self, page: Any, name: str) -> None:
        """Helper to find and open a report card by name."""
        logger.info(f"Opening report '{name}'...")
        
        # Wait for content to load
        try:
            page.locator(".report-tile, .report-card, .sapMCard, .sapMLIB, .cnqr-report-card, .no-reports").first.wait_for(state="visible", timeout=5000)
        except Exception:
            pass

        card_selectors = [".report-tile", ".report-card", ".sapMCard", ".sapMLIB", ".cnqr-report-card"]
        card = None
        for selector in card_selectors:
            loc = page.locator(selector).filter(has_text=name)
            if loc.count() > 0:
                card = loc.first
                break
        
        if not card:
            if page.locator(".no-reports").is_visible():
                raise FileNotFoundError(f"Dashboard is empty (No reports found). Could not find '{name}'.")
            raise FileNotFoundError(f"Could not find report '{name}'.")

        card.click()
        page.wait_for_timeout(3000)
        self._wait_for_report_view(page)

    def _get_transaction_rows(self, page: Any) -> List[Any]:
        """All expense rows in the current report view, in document order.

        Thin wrapper over _collect_expense_rows so the allocation paths share one
        definition of a row with the read and write paths. Callers of this helper
        index it 0-based; the CLI converts from the 1-based index it exposes.
        """
        rows, _ = self._collect_expense_rows(page)
        return rows

    # Measured against a live 16-row statement: text + receipt + save is about
    # 10s, and an allocation (clear and add, each verified against a fresh read)
    # about 103s. Rows without an allocation are therefore far cheaper, so the
    # estimate is per-row rather than a flat figure.
    SECONDS_PER_ROW = 12
    SECONDS_PER_ALLOCATION = 105

    def _estimate_seconds(self, expenses):
        """Rough wall-clock estimate for an apply-json payload."""
        total = 0
        for exp in expenses:
            total += self.SECONDS_PER_ROW
            if "allocation" in exp:
                total += self.SECONDS_PER_ALLOCATION
        return total

    def _session_minutes_left(self):
        """Minutes until the saved Concur JWT expires, or None if unknown."""
        saved = self._saved_jwt_expiry()
        if not saved:
            return None
        _, mins = saved
        return mins

    def apply_json_updates(self, report_name: str, expenses: list, headless: bool = True,
                           ignore_session_budget: bool = False) -> dict:
        """
        Applies a list of custom transaction updates (e.g. from an edited JSON file)
        to a draft report in a single, high-performance browser session.
        """
        logger.info(f"Applying custom JSON updates to report '{report_name}' (headless={headless})...")

        # Concur's JWT lasts about 60 minutes from login and is not refreshed by
        # use, so a long payload can outlive it. Refuse up front rather than
        # spending twenty minutes to die partway.
        estimate = self._estimate_seconds(expenses)
        minutes_left = self._session_minutes_left()
        if (minutes_left is not None and not ignore_session_budget
                and estimate / 60.0 > minutes_left):
            fits = 0
            spent = 0
            for exp in expenses:
                cost = self.SECONDS_PER_ROW + (
                    self.SECONDS_PER_ALLOCATION if "allocation" in exp else 0)
                if (spent + cost) / 60.0 > minutes_left:
                    break
                spent += cost
                fits += 1
            return {
                "success": False,
                "error": (
                    f"This payload needs about {estimate/60:.0f} min and the Concur "
                    f"session has {minutes_left:.0f} min left. Roughly {fits} of "
                    f"{len(expenses)} row(s) would finish. Re-run "
                    f"`ccworks session login` first, split the payload, or pass "
                    f"--ignore-session-budget to try anyway."
                ),
                "estimated_minutes": round(estimate / 60.0, 1),
                "session_minutes_left": round(minutes_left, 1),
                "rows_that_would_fit": fits,
                "results": [],
            }
        results = []
        with self._browser_page(headless=headless) as page:
            try:
                page.goto(f"{self.base_url}/nui/expense", timeout=45000)
                self._wait_for_dashboard(page)
                
                # Locate and open the report
                card_selectors = [".report-tile", ".report-card", ".sapMCard", ".sapMLIB", ".cnqr-report-card"]
                card = None
                for selector in card_selectors:
                    loc = page.locator(selector).filter(has_text=report_name)
                    if loc.count() > 0:
                        card = loc.first
                        break
                
                if not card:
                    raise FileNotFoundError(f"Could not find report '{report_name}'.")
                    
                card.click()
                page.wait_for_timeout(3000)
                self._wait_for_report_view(page)
                self._dismiss_modals(page)
                
                valid_rows, _ = self._collect_expense_rows(page, report_name)
                logger.info(f"Discovered {len(valid_rows)} transaction row(s) in Concur.")

                for row_ordinal, exp in enumerate(
                        sorted(expenses, key=lambda e: e.get("index", 0))):
                    # Reload the report before every row after the first. Closing
                    # the detail pane does not tear down the receipt viewer, so
                    # the next row opens with the previous row's receipt still in
                    # the DOM -- every later row then reads that filename as its
                    # own attachment and tries to replace a receipt it does not
                    # have. Re-navigating is the only reliable reset.
                    if row_ordinal > 0:
                        page.goto(f"{self.base_url}/nui/expense", timeout=45000)
                        self._wait_for_dashboard(page)
                        self._open_report_by_name(page, report_name)
                        self._dismiss_modals(page)
                        valid_rows, _ = self._collect_expense_rows(page, report_name)

                    # 1-based and dense, the same space `report show` emits and
                    # `txn update` accepts. This previously read the index as a
                    # 0-based offset into a differently-filtered list, so a
                    # `report show` -> edit -> `apply-json` round-trip could write
                    # each change to the wrong expense.
                    # Stop cleanly while there is still session left, rather than
                    # failing mid-row on an expiry. The caller gets the rows that
                    # did land plus exactly what remains to re-run.
                    if not ignore_session_budget:
                        left = self._session_minutes_left()
                        cost = self.SECONDS_PER_ROW + (
                            self.SECONDS_PER_ALLOCATION if "allocation" in exp else 0)
                        if left is not None and left * 60 < cost + 60:
                            remaining = [e.get("index") for e in
                                         sorted(expenses, key=lambda e: e.get("index", 0))
                                         [row_ordinal:]]
                            logger.warning(
                                f"Stopping before row {exp.get('index')}: about "
                                f"{left:.0f} min of session left, this row needs "
                                f"{cost/60:.1f} min.")
                            return {
                                "success": False,
                                "stopped_early": True,
                                "reason": (f"Concur session has about {left:.0f} min left, "
                                           f"too little for the next row. Re-run "
                                           f"`ccworks session login`, then apply a payload "
                                           f"containing only the remaining rows."),
                                "remaining_indices": remaining,
                                "results": results,
                            }

                    idx = exp.get("index")
                    if idx is None or idx < 1 or idx > len(valid_rows):
                        logger.warning(f"Index {idx} is out of bounds. Skipping.")
                        results.append({"index": idx, "success": False, "error": "Index out of bounds"})
                        continue

                    expense_type = exp.get("expense_type") or exp.get("type")
                    # No "" default: an absent key must mean "leave this field
                    # alone", and "" must mean "clear it". These previously
                    # defaulted to "", which is not None, so the `is not None`
                    # guards below always fired and every omitted field was
                    # overwritten with empty -- wiping a business purpose or
                    # comment that the caller never mentioned. That is especially
                    # dangerous for a row listed in extraction.deep_scan_failures,
                    # whose fields read as "" because the detail pane never
                    # opened, not because Concur holds them empty.
                    purpose = exp.get("business_purpose")
                    comment = exp.get("comment")
                    vendor = exp.get("vendor", "Unknown")
                    amount = exp.get("amount", "")

                    # Refuse to edit a row that is not the expense the caller
                    # described. Index alignment can still drift if the report
                    # changed in Concur since the JSON was produced, and a silent
                    # mis-write to a financial record is far worse than a refusal.
                    mismatch = self._row_identity_mismatch(valid_rows[idx - 1], exp)
                    if mismatch:
                        logger.error(f"Row {idx} does not match the supplied expense: {mismatch}")
                        results.append({
                            "index": idx, "success": False,
                            "error": f"Row does not match supplied expense ({mismatch}). "
                                     f"Re-run `report show` to get current indices.",
                        })
                        continue

                    logger.info(f"Updating Row {idx}: {vendor} - {amount}")
                    
                    # Selection loop (up to 3 attempts)
                    selection_successful = False
                    for attempt in range(3):
                        # Re-identify rows to prevent staleness
                        current_valid_rows, _ = self._collect_expense_rows(page, report_name)

                        if idx > len(current_valid_rows):
                            break

                        row = current_valid_rows[idx - 1]
                        row.scroll_into_view_if_needed()

                        # Click the row BODY, never its checkbox. Every row carries a
                        # "Select expense" checkbox, and clicking that only toggles
                        # bulk selection -- it never opens the detail pane, so this
                        # loop used to exhaust all three attempts and fail every row
                        # with "Failed to open row". This is the escalation the deep
                        # scan uses (see get_report_details), which does open panes.
                        try:
                            row.click(force=True)
                            page.wait_for_timeout(500)
                        except Exception as exc:
                            logger.debug(f"apply_json_updates: ignoring {exc!r}")

                        # Escalate: toolbar Edit -> row kebab -> double click. These
                        # run unconditionally after the row click, because a click
                        # that merely selects still needs a real open action.
                        try:
                            edit_btn = page.locator(
                                "[data-nuiexp='edit-button'], button:has-text('Edit'), "
                                "#edit-transaction-btn, .sapMBtn:has-text('Edit')"
                            ).filter(visible=True).first
                            if edit_btn.count() > 0 and edit_btn.is_enabled():
                                edit_btn.click(force=True)
                        except Exception as exc:
                            logger.debug(f"apply_json_updates: ignoring {exc!r}")

                        if page.locator(self.PANE_READY_SELECTOR).count() == 0:
                            try:
                                actions_btn = row.locator(
                                    "[data-nui-widgets='menu-button-trigger'], "
                                    ".entries-list-actions-button, button[aria-label='Actions']"
                                ).first
                                if actions_btn.count() > 0:
                                    actions_btn.click(force=True)
                                    menu_item = page.locator(
                                        ".sapMMenuItemText:has-text('Edit'), "
                                        ".sapMMenuItemText:has-text('Open'), "
                                        "[role='menuitem']:has-text('Edit')"
                                    ).first
                                    if menu_item.count() > 0:
                                        menu_item.click()
                            except Exception as exc:
                                logger.debug(f"apply_json_updates: ignoring {exc!r}")

                        if page.locator(self.PANE_READY_SELECTOR).count() == 0:
                            try:
                                row.dblclick(force=True)
                            except Exception as exc:
                                logger.debug(f"apply_json_updates: ignoring {exc!r}")

                        # Confirm the pane is actually open before claiming success.
                        # The old code set selection_successful right after clicking
                        # Edit without waiting, so it could "succeed" into a missing
                        # pane and then silently skip every field write. The readiness
                        # selector is deliberately narrow: .sapMInputBaseInner and
                        # .recon-type also match the list grid, so a merely-selected
                        # row would read as open.
                        try:
                            page.wait_for_selector(self.PANE_READY_SELECTOR, timeout=3000)
                            selection_successful = True
                            break
                        except Exception as exc:
                            logger.debug(f"apply_json_updates: ignoring {exc!r}")

                    if not selection_successful:
                        logger.error(f"Failed to open transaction row {idx}")
                        results.append({"index": idx, "success": False, "error": "Failed to open row"})
                        continue
                        
                    # Detail Pane
                    detail_pane = page.locator("#sapcnqr-layout-side-panel-elements, .sapcnqr-layout-side-panel__elements, .ere__dynamic-main-content").filter(visible=True).first
                    input_context = detail_pane if detail_pane.count() > 0 else page
                    
                    # One shared writer for every field, with read-back built in.
                    # These edits were previously inlined here and in
                    # update_report_transaction, and only that copy verified the
                    # result -- so a type that silently reverted was invisible.
                    field_errors = self._write_expense_fields(
                        page, input_context, expense_type=expense_type,
                        purpose=purpose, comment=comment)
                    if field_errors:
                        logger.warning(f"Row {idx}: " + "; ".join(field_errors))

                    # Receipt upload happens here, while the detail pane is open:
                    # the upload controls are rendered inside the pane, and the
                    # file inputs are hidden, so files are set on them directly
                    # rather than by driving the OS picker.
                    # Same convention as the text fields: omitting the key leaves
                    # the receipt alone, and passing "" clears it.
                    has_receipt_key = ("receipt_file_path" in exp) or ("receipt_file" in exp)
                    receipt_path = exp.get("receipt_file_path") or exp.get("receipt_file")
                    receipt_error = None
                    if has_receipt_key and not receipt_path:
                        receipt_error = self._detach_receipt_in_pane(page, input_context, idx)
                    elif receipt_path:
                        if not os.path.exists(receipt_path):
                            receipt_error = f"receipt file not found locally: {receipt_path}"
                            logger.warning(f"  {receipt_error}")
                        else:
                            receipt_error = self._attach_receipt_in_pane(
                                page, input_context, idx, receipt_path)


                    # Save
                    saved, save_error = self._click_save_expense(page, input_context)

                    # Allocation last, and only once the pane is closed: the
                    # allocations modal is reached from the row's kebab, not the
                    # expense pane. Same convention again -- omit the key to leave
                    # allocations alone, pass an empty object or null to clear.
                    alloc_error = None
                    alloc_verified = None
                    if saved and "allocation" in exp:
                        spec = exp.get("allocation") or {}
                        try:
                            removed, err = self._clear_allocations_on_page(
                                page, report_name, idx - 1)
                            if err:
                                alloc_error = err
                            elif spec.get("dept") and spec.get("fund"):
                                alloc_verified, err = self._add_allocation_on_page(
                                    page, report_name, idx - 1,
                                    spec["dept"], spec["fund"], spec.get("prog"))
                                if err:
                                    alloc_error = err
                            else:
                                alloc_verified = []
                        except Exception as e:
                            alloc_error = f"allocation failed on row {idx}: {e}"

                    if not saved:
                        logger.warning(f"Row {idx}: {save_error}. Closing pane.")
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(1000)
                        results.append({"index": idx, "success": False, "error": save_error})
                    elif alloc_error:
                        logger.warning(f"Row {idx}: {alloc_error}")
                        results.append({"index": idx, "success": False, "error": alloc_error})
                    elif field_errors:
                        # A field that did not take must fail the row. Reporting
                        # success here is how a silently reverted expense type
                        # went unnoticed on a submitted report.
                        results.append({"index": idx, "success": False,
                                        "error": "; ".join(field_errors)})
                    elif receipt_error:
                        # Field edits saved, but the receipt did not attach. Report
                        # the row as failed: a caller uploading receipts cares that
                        # the receipt landed, and a bare success here would be a
                        # silent omission on a financial record.
                        logger.warning(f"Saved Row {idx}, but receipt did not attach.")
                        results.append({"index": idx, "success": False, "error": receipt_error})
                    else:
                        logger.info(f"Successfully updated and saved Row {idx}.")
                        row_result = {"index": idx, "success": True}
                        if alloc_verified is not None:
                            row_result["allocation_verified"] = alloc_verified
                        results.append(row_result)
                
                return {"success": True, "results": results}
                
            except Exception as e:
                logger.error(f"Error applying JSON updates: {e}")
                raise
