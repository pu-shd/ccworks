"""Real Chromium handoff checks against an isolated server; no Safari or Concur.

Run in the shipped Docker image with unittest -v. Synthetic credentials only.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ccworks.browser_client import ConcurBrowserClient
from ccworks.safari_login import SafariLoginError, run_login, state_from_capture


class TestSavedSessionHandoff(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.accepted_token = "test-first-token"
        cls.accepted_requests = 0

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                cookie = self.headers.get("Cookie", "")
                if self.path.startswith("/nui/expense"):
                    if f"JWT={cls.accepted_token}" not in cookie:
                        self.send_response(302)
                        self.send_header("Location", "/login")
                        self.end_headers()
                        return
                    cls.accepted_requests += 1
                    body = b'<html><title>Expenses</title><div class="no-reports">No reports found</div></html>'
                else:
                    body = b"<html><title>Login</title>Please sign in</html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        # Chromium resolves *.localhost to loopback without DNS configuration.
        cls.base = f"http://ccworks.concursolutions.localhost:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "session.json"
        self.path.write_text("previous saved session")
        self.client = ConcurBrowserClient(
            session_file=str(self.path), base_url=self.base
        )
        type(self).accepted_token = "test-first-token"
        type(self).accepted_requests = 0

    def capture(self, token=None, expires=3600):
        token = token or type(self).accepted_token
        return state_from_capture(
            self.base,
            {"origin": self.base, "localStorage": []},
            [
                {
                    "url": self.base + "/auth",
                    "responseHeaders": {
                        "Set-Cookie": f"JWT={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={expires}"
                    },
                }
            ],
        )

    def test_handoff_and_reuse_by_an_independent_process(self):
        result = run_login(
            self.client,
            browser="safari",
            fallback=False,
            safari_capture=lambda base: self.capture(),
        )
        self.assertEqual("safari", result["login_browser"])
        self.assertGreaterEqual(
            type(self).accepted_requests,
            2,
            "two fresh browser contexts must authenticate",
        )
        state = json.loads(self.path.read_text())
        self.assertTrue(state["cookies"][0]["httpOnly"])
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        program = """import sys
from ccworks.browser_client import ConcurBrowserClient
c=ConcurBrowserClient(session_file=sys.argv[1], base_url=sys.argv[2])
assert c.check_session_validity()['authenticated'], 'saved session did not authenticate'
c.verify_session_reuse()
print('PASS: saved session reused in a new process')
"""
        completed = subprocess.run(
            [sys.executable, "-c", program, str(self.path), self.base],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("PASS: saved session reused", completed.stdout)
        self.assertGreaterEqual(type(self).accepted_requests, 4)

    def test_revocation_then_reauthentication(self):
        run_login(
            self.client,
            browser="safari",
            fallback=False,
            safari_capture=lambda base: self.capture(),
        )
        type(self).accepted_token = "test-renewed-token"
        self.assertFalse(self.client.check_session_validity()["authenticated"])
        before = self.path.read_bytes()
        with self.assertRaises(SafariLoginError):
            run_login(
                self.client,
                browser="safari",
                fallback=False,
                safari_capture=lambda base: self.capture(token="revoked-token"),
            )
        self.assertEqual(
            before,
            self.path.read_bytes(),
            "failed reauthentication must preserve the saved state",
        )
        run_login(
            self.client,
            browser="safari",
            fallback=False,
            safari_capture=lambda base: self.capture(),
        )
        self.assertNotEqual(before, self.path.read_bytes())
        self.assertTrue(self.client.check_session_validity()["authenticated"])
        self.client.verify_session_reuse()

    def test_rejected_handoff_preserves_original_file(self):
        previous = self.path.read_bytes()
        with self.assertRaises(SafariLoginError):
            run_login(
                self.client,
                browser="safari",
                fallback=False,
                safari_capture=lambda base: self.capture(token="revoked-token"),
            )
        self.assertEqual(previous, self.path.read_bytes())

    def test_expired_export_preserves_original_file(self):
        previous = self.path.read_bytes()
        with self.assertRaises(SafariLoginError):
            run_login(
                self.client,
                browser="safari",
                fallback=False,
                safari_capture=lambda base: self.capture(expires=-1),
            )
        self.assertEqual(previous, self.path.read_bytes())

    def test_fallback_uses_real_headless_verification(self):
        # Manual Chromium input is mocked; export, browser validation, and disk
        # promotion are real. Safari is never started in this suite.
        def fake_headed(client):
            Path(client.session_file).write_text(json.dumps(self.capture()))

        with patch.object(ConcurBrowserClient, "run_headed_login", fake_headed):
            result = run_login(
                self.client,
                browser="safari",
                safari_capture=lambda base: (_ for _ in ()).throw(
                    SafariLoginError("Unavailable")
                ),
            )
        self.assertTrue(result["fallback_used"])
        self.assertEqual("chromium", result["login_browser"])
        self.assertTrue(self.client.check_session_validity()["authenticated"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
