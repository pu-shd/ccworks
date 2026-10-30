"""Native, restartable bulk allocations with independent persisted readback."""

from decimal import Decimal, InvalidOperation
import re
from typing import Any

from playwright.sync_api import expect


ROOT = "[data-nuiexp='sal-dialog-allocations']"
FORM = "[data-nuiexp='Ut-modal'], #add-allocation-form"
OTHER_KEYS = ("custom8", "custom9", "custom10", "custom11", "custom12")


def contains_code(text: str, code: str) -> bool:
    return bool(re.search(r"(?<![\w])" + re.escape(code) + r"(?![\w])", text, re.I))


def field_code(value: str) -> str:
    match = re.search(r"\(([A-Za-z0-9_-]+)\)", value)
    return (
        match.group(1) if match else value.strip().split()[0] if value.strip() else ""
    )


def is_full_allocation(rows, codes, percent_mode):
    if len(rows) != 1 or not percent_mode:
        return False
    row = rows[0]
    try:
        full = Decimal(row.get("allocation_input", "").replace(",", "")) == 100
    except InvalidOperation:
        return False
    return full and all(contains_code(row["raw_text"], code) for code in codes)


class BulkAllocator:
    def __init__(
        self,
        client: Any,
        report_name,
        department,
        fund,
        program,
        indices,
        dry_run,
        headless,
    ):
        self.client = client
        self.name = report_name
        self.department = department
        self.fund = fund
        self.program = program
        self.indices = indices
        self.dry_run = dry_run
        self.headless = headless
        self.identities: list[str] = []
        self.before: dict[int, dict[str, str]] = {}
        self.pending: list[int] = []
        self.skipped: list[int] = []
        self.groups: dict[tuple[tuple[str, str], ...], list[int]] = {}
        self.attempted = False
        self.warnings: list[str] = []
        self.codes = [department, fund] + ([program] if program is not None else [])

    def _rows(self, page):
        rows, _ = self.client._collect_expense_rows(page, self.name)
        identities = [" ".join((row.text_content() or "").split()) for row in rows]
        if self.identities and identities != self.identities:
            raise ValueError(
                "Expense rows changed; refresh the selection before retrying"
            )
        return rows, identities

    def _root(self, page):
        root = page.locator(ROOT).filter(visible=True)
        expect(root).to_have_count(1, timeout=10000)
        self.client._wait_for_allocations_ready(page)
        return root

    def _cancel(self, root):
        cancel = root.locator("[data-nuiexp='sal-btn-allocation-modal-cancel']")
        if cancel.count() != 1:
            cancel = root.get_by_role("button", name="Cancel", exact=True)
        expect(cancel).to_have_count(1)
        cancel.click()
        root.wait_for(state="hidden", timeout=10000)

    def _percent_mode(self, root):
        selected = root.locator("[role='option'][aria-selected='true']")
        return selected.count() == 1 and selected.inner_text().strip().lower() in (
            "percent",
            "percentage",
        )

    def _form(self, page, root):
        add = root.locator(self.client.ALLOCATION_ADD_BUTTON).filter(visible=True)
        expect(add).to_have_count(1)
        add.click()
        form = page.locator(FORM).filter(visible=True)
        expect(form).to_have_count(1)
        form.locator("[data-nuiexp='field-custom6']").wait_for(
            state="visible", timeout=10000
        )
        return form

    def _other(self, form):
        values = {}
        for key in OTHER_KEYS:
            if key == "custom8" and self.program is not None:
                continue
            if key == "custom11":
                field = form.locator("input[data-nuiexp='field-custom11']")
                values[key] = field.input_value() if field.count() else ""
            else:
                trigger = form.locator(f"[data-nuiexp='field-{key}__trigger']")
                if trigger.count():
                    values[key] = " ".join(trigger.inner_text().split())
                else:
                    field = form.locator(f"[data-nuiexp='field-{key}']")
                    label = field.locator(".sapcnqr-form-field__label")
                    text = label.inner_text() if label.count() else ""
                    values[key] = self.client._read_select_field(form, key, text) or ""
        return values

    def _close_form(self, form):
        cancel = form.locator("[data-nuiexp='At-cancel-btn']")
        if not cancel.count():
            cancel = form.get_by_role("button", name="Cancel", exact=True)
        expect(cancel).to_have_count(1)
        cancel.click()
        form.wait_for(state="hidden", timeout=10000)

    def _preflight(self, page):
        self.client._load_type_report(page, self.name)
        rows, self.identities = self._rows(page)
        indices = list(range(len(rows))) if self.indices is None else self.indices
        if not indices or any(
            type(i) is not int or i < 0 or i >= len(rows) for i in indices
        ):
            raise ValueError(
                "Choose existing zero-based expense indices, or omit indices for all expenses"
            )
        if len(set(indices)) != len(indices):
            raise ValueError("Expense indices must be unique")
        self.indices = sorted(indices)
        conflicts = []
        for index in self.indices:
            self._rows(page)
            self.client._open_allocations_modal(page, self.name, index)
            root = self._root(page)
            allocations = self.client._read_allocations_from_modal(page)
            if allocations:
                if is_full_allocation(
                    allocations, self.codes, self._percent_mode(root)
                ):
                    self.skipped.append(index)
                else:
                    conflicts.append(index + 1)
            else:
                form = self._form(page, root)
                other = self._other(form)
                self._close_form(form)
                key = tuple(sorted(other.items()))
                self.groups.setdefault(key, []).append(index)
                self.before[index] = other
                self.pending.append(index)
            self._cancel(root)
        return conflicts

    def _write_group(self, page, indices, other):
        left = self.client._session_minutes_left()
        if left is not None and left < 2:
            raise ValueError(
                "Session is too short for a verified bulk write; reauthenticate and rerun"
            )
        rows, _ = self._rows(page)
        for index, row in enumerate(rows):
            box = row.get_by_role("checkbox")
            expect(box).to_have_count(1)
            box.set_checked(index in indices)
        assert all(
            row.get_by_role("checkbox").is_checked() == (i in indices)
            for i, row in enumerate(rows)
        ), "Bulk selection could not be confirmed"
        page.get_by_role("button", name="Allocate", exact=True).click()
        root = self._root(page)
        if self.client._read_allocations_from_modal(page):
            raise ValueError(
                "Selected expenses gained allocations; refresh before retrying"
            )
        if not self._percent_mode(root):
            raise ValueError(
                "Select Percent allocation mode before using bulk allocations"
            )
        form = self._form(page, root)
        if self._other(form) != other:
            raise ValueError(
                "Bulk form defaults differ from the individual chartstrings"
            )
        wanted = [
            ("custom6", self.department, "Department"),
            ("custom7", self.fund, "Fund"),
        ]
        if self.program is not None:
            wanted.append(("custom8", self.program, "Program"))
        for key, code, label in wanted:
            current = self.client._read_select_field(form, key, label) or ""
            if not contains_code(current, code):
                error = self.client._set_select_field(page, form, key, code, label)
                if error:
                    raise ValueError(
                        f"{label} picker did not confirm the requested code"
                    )
            if not contains_code(
                self.client._read_select_field(form, key, label) or "", code
            ):
                raise ValueError(f"{label} did not retain the requested code")
        if self._other(form) != other:
            raise ValueError("An unrequested chartstring field changed")
        confirm = form.locator(self.client.ALLOCATION_ADD_CONFIRM).filter(visible=True)
        expect(confirm).to_have_count(1)
        expect(confirm).to_be_enabled()
        # This click may persist immediately. Never retry after it without readback.
        self.attempted = True
        confirm.click()
        form.wait_for(state="hidden", timeout=10000)
        self.client._wait_for_allocations_ready(page)
        value = root.locator("input[data-nuiexp='spend-common__amount-input']").filter(
            visible=True
        )
        expect(value).to_have_count(1)
        if not self._percent_mode(root):
            raise ValueError("Allocation percentage mode could not be confirmed")
        if Decimal(value.input_value().replace(",", "")) != 100:
            value.fill("100")
            value.press("Tab")
        if not is_full_allocation(
            self.client._read_allocations_from_modal(page), self.codes, True
        ):
            raise ValueError(
                "The bulk draft does not contain one requested 100 percent allocation"
            )
        save = root.locator(self.client.ALLOCATION_SAVE_BUTTON).filter(visible=True)
        expect(save).to_have_count(1)
        expect(save).to_be_enabled()
        save.click()
        root.wait_for(state="hidden", timeout=10000)

    def _verify(self):
        results = []
        # A separate browser context reads server state, never the draft modal.
        with self.client._browser_page(
            headless=self.headless, viewport_height=1000
        ) as page:
            self.client._load_type_report(page, self.name)
            self._rows(page)
            for index in self.indices:
                self.client._open_allocations_modal(page, self.name, index)
                root = self._root(page)
                allocations = self.client._read_allocations_from_modal(page)
                other_codes = [
                    field_code(v) for v in self.before.get(index, {}).values() if v
                ]
                verified = is_full_allocation(
                    allocations,
                    self.codes + other_codes,
                    self._percent_mode(root),
                )
                results.append(
                    {
                        "index": index + 1,
                        "success": verified,
                        "verified": verified,
                        "status": "skipped"
                        if index in self.skipped
                        else "updated"
                        if verified
                        else "failed",
                    }
                )
                self._cancel(root)
        return results

    def run(self):
        base = {"dry_run": self.dry_run, "bulk_groups": 0, "warnings": self.warnings}
        try:
            if not isinstance(self.name, str) or not self.name.strip():
                raise ValueError("A report name is required")
            for code in self.codes:
                if not isinstance(code, str) or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", code
                ):
                    raise ValueError(
                        "Chartstring codes must be nonempty codes, not descriptions"
                    )
            if self.indices is not None and not isinstance(self.indices, (list, tuple)):
                raise ValueError("Expense indices must be a list")
            with self.client._browser_page(
                headless=self.headless, viewport_height=1000
            ) as page:
                conflicts = self._preflight(page)
                if conflicts:
                    return {
                        **base,
                        "success": False,
                        "updated": 0,
                        "skipped": len(self.skipped),
                        "conflicting_indices": conflicts,
                        "error": "Existing allocations differ or contain splits; no changes were made",
                    }
                base["bulk_groups"] = len(self.groups)
                if self.dry_run:
                    return {
                        **base,
                        "success": True,
                        "updated": 0,
                        "skipped": len(self.skipped),
                        "planned_indices": [i + 1 for i in self.pending],
                    }
                for other, indices in self.groups.items():
                    self._write_group(page, indices, dict(other))
        except Exception as error:
            # Error details can contain expense data or browser state; expose only
            # our own validation messages and the exception class.
            if not self.attempted:
                message = (
                    str(error)
                    if isinstance(error, ValueError)
                    else type(error).__name__
                )
                return {**base, "success": False, "updated": 0, "error": message}
            self.warnings.append(
                "Write acknowledgement interrupted; saved state was checked before any retry"
            )
        try:
            results = self._verify()
        except Exception:
            return {
                **base,
                "success": False,
                "updated": 0,
                "failed": len(self.indices),
                "remaining_indices": [i + 1 for i in self.indices],
                "error": "Persisted state could not be verified; rerun to read state before writing",
            }
        summary = self.client._update_summary(results)
        return {
            **base,
            **summary,
            "warnings": self.warnings,
            "remaining_indices": [r["index"] for r in results if not r["success"]],
        }
