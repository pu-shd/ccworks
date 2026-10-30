"""Opt-in native Safari-to-Chromium test using synthetic local credentials only.

CCWORKS_TEST_NATIVE_SAFARI=1 .venv/bin/python -m unittest -v tests.test_native_safari_handoff
Requires macOS, authorized Safari 27, and installed Playwright Chromium.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ccworks.safari_login import capture_safari_state, save_state_atomically


@unittest.skipUnless(
    sys.platform == "darwin" and os.environ.get("CCWORKS_TEST_NATIVE_SAFARI") == "1",
    "native Safari requires macOS and CCWORKS_TEST_NATIVE_SAFARI=1; portable tests use mocks",
)
class TestNativeSafariHandoff(unittest.TestCase):
    def test_native_http_only_export_authenticates_in_new_chromium_process(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                protected = self.path.startswith("/protected")
                accepted = "JWT=ccworks-synthetic-only" in self.headers.get(
                    "Cookie", ""
                )
                body = b"<html><title>Local handoff test</title><p>Synthetic fixture</p></html>"
                self.send_response(200 if not protected or accepted else 401)
                self.send_header("Content-Type", "text/html")
                if not protected:
                    self.send_header(
                        "Set-Cookie",
                        "JWT=ccworks-synthetic-only; Path=/; HttpOnly; SameSite=Lax; Max-Age=600",
                    )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            with patch("ccworks.safari_login.wait_for_login"):
                state = capture_safari_state(origin)
            jwt = next(cookie for cookie in state["cookies"] if cookie["name"] == "JWT")
            self.assertTrue(jwt["httpOnly"])
            self.assertGreater(jwt["expires"], time.time())
            with tempfile.TemporaryDirectory(
                prefix="ccworks-native-handoff-"
            ) as directory:
                saved = Path(directory) / "state.json"
                save_state_atomically(saved, state)
                program = """import sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    for i in range(2):
        browser=p.chromium.launch(headless=True)
        try:
            context=browser.new_context(storage_state=sys.argv[1])
            response=context.new_page().goto(sys.argv[2]+'/protected')
            assert response is not None and response.status==200, 'native session did not authenticate'
            print('PASS: native Safari session reused in Chromium context', i+1)
        finally:
            browser.close()
"""
                result = subprocess.run(
                    [sys.executable, "-c", program, str(saved), origin],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(
                    2, result.stdout.count("PASS: native Safari session reused")
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
