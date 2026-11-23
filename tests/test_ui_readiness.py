"""Live UI readiness failures reproduced with synthetic browser documents."""

import os
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from playwright.sync_api import sync_playwright

from ccworks.browser_client import ConcurBrowserClient

os.environ.setdefault("CCWORKS_SKIP_BROWSER_BOOTSTRAP", "1")


class TestUIReadiness(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.playwright = stack.enter_context(sync_playwright())
        self.browser = stack.enter_context(self.playwright.chromium.launch())
        self.page = self.browser.new_page()
        self.client = ConcurBrowserClient.__new__(ConcurBrowserClient)

    def test_native_editor_close_does_not_save(self):
        self.page.set_content("""
            <section class="ere__dynamic-main-content">
              <input data-nuiexp="field-expenseType" value="Software">
              <button data-nuiexp="close-full-screen-panel"
                onclick="this.closest('section').remove()">Exit expense</button>
              <button onclick="window.saved=true">Save Expense</button>
            </section><script>window.saved=false</script>
        """)
        self.assertTrue(self.client._close_expense_editor(self.page))
        self.assertEqual(0, self.page.locator("input").count())
        self.assertFalse(self.page.evaluate("window.saved"))

    def test_unknown_business_dialog_is_never_dismissed_or_removed(self):
        self.page.set_content("""
            <div role="dialog" aria-modal="true" class="sapcnqr-dialog">
              <p>Business confirmation</p>
              <button onclick="this.parentNode.remove()">Close</button>
            </div>
        """)
        self.client._dismiss_modals(self.page)
        self.assertEqual(1, self.page.get_by_role("dialog").count())

    def test_onboarding_close_is_scoped_and_preserves_other_dialogs(self):
        self.page.set_content("""
            <div role="dialog" aria-modal="true" id="business" class="sapcnqr-dialog">
              <button onclick="this.parentNode.remove()">Close</button>
            </div>
            <div data-nuiexp="timelineModal" role="dialog" aria-modal="true">
              <button onclick="this.parentNode.remove()">Close</button>
            </div>
        """)
        self.client._dismiss_modals(self.page)
        self.assertEqual(1, self.page.locator("#business").count())
        self.assertEqual(0, self.page.locator('[data-nuiexp="timelineModal"]').count())

    def test_report_name_hydration_is_required_before_opening(self):
        self.page.set_content("""
            <div class="report-card" onclick="window.opened=true">Loading</div>
            <script>
              window.opened=false;
              setTimeout(()=>document.querySelector('.report-card').textContent='Synthetic report',250);
            </script>
        """)
        self.client._wait_for_report_view = Mock()
        self.client._open_report_by_name(self.page, "Synthetic report", settle=False)
        self.assertTrue(self.page.evaluate("window.opened"))

    def test_multiple_known_overlays_close_without_skipping_a_shifted_index(self):
        self.page.set_content("""
            <div data-nuiexp="timelineModal">
              <button onclick="this.parentNode.remove()">Close</button>
            </div>
            <div class="vip-widgets__text-app-onboarding-dialog">
              <button onclick="this.parentNode.remove()">Close</button>
            </div>
        """)
        self.client._dismiss_modals(self.page)
        self.assertEqual(0, self.page.get_by_role("button", name="Close").count())

    def test_report_tile_is_preferred_over_its_matching_list_container(self):
        self.page.set_content("""
            <div class="sapMLIB" style="position:relative;width:400px;height:400px"
                 onclick="window.containerClicked=true">
              <div class="report-tile" style="position:absolute;top:0;left:0;width:60px;height:40px"
                   onclick="window.cardClicked=true;event.stopPropagation()">Synthetic report</div>
            </div>
            <script>window.cardClicked=false;window.containerClicked=false</script>
        """)
        self.client._wait_for_report_view = Mock()
        self.client._open_report_by_name(self.page, "Synthetic report", settle=False)
        self.assertTrue(self.page.evaluate("window.cardClicked"))
        self.assertFalse(self.page.evaluate("window.containerClicked"))


class TestLateOverlay(unittest.TestCase):
    def test_late_cookie_overlay_closes_without_changing_preferences(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary) / "session.json"
            session.write_text('{"cookies": [], "origins": []}')
            client = ConcurBrowserClient(
                session_file=str(session), base_url="http://example.invalid"
            )
            with client._browser_page() as page:
                page.set_content("""
                    <button id="target" onclick="window.clicked=true">Target</button>
                    <script>
                      window.clicked=false;window.preferencesChanged=false;window.overlayCreated=false;
                      setTimeout(()=>{
                        const overlay=document.createElement('div');
                        overlay.id='trustarc-banner-overlay';
                        overlay.style='position:fixed;inset:0;z-index:1000;background:#888';
                        overlay.innerHTML='<button id="truste-consent-close" onclick="this.parentNode.remove()">Close</button>';
                        document.body.appendChild(overlay);
                        window.overlayCreated=true;
                      },50);
                    </script>
                """)
                page.wait_for_function("window.overlayCreated")
                page.locator("#target").click(timeout=2000)
                self.assertTrue(page.evaluate("window.clicked"))
                self.assertFalse(page.evaluate("window.preferencesChanged"))
