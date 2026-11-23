"""Safari login transport and session-safety tests, runnable without macOS."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from ccworks.safari_login import (
    MCPClient,
    SafariLoginError,
    save_state_atomically,
    state_from_capture,
)


FAKE_SERVER = r"""
import json, sys
for line in sys.stdin:
    msg=json.loads(line)
    if 'id' not in msg: continue
    method=msg['method']
    if method=='initialize':
        result={'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'serverInfo':{'name':'fake','version':'1'}}
    elif method=='tools/list':
        result={'tools':[{'name':'echo','inputSchema':{'type':'object'}}]}
    elif method=='tools/call':
        if msg['params']['arguments'].get('fail'):
            result={'isError':True,'content':[{'type':'text','text':'SECRET-TOKEN must not be logged'}]}
        else:
            result={'structuredContent':msg['params']['arguments']}
    else:
        result={}
    print(json.dumps({'jsonrpc':'2.0','method':'notifications/message','params':{'data':'SECRET-TOKEN'}}),flush=True)
    print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':result}),flush=True)
"""


class TestSafariMCPTransport(unittest.TestCase):
    def test_initialize_tool_calls_and_cleanup(self):
        with MCPClient([sys.executable, "-u", "-c", FAKE_SERVER], timeout=2) as client:
            self.assertIn("echo", client.tools)
            self.assertEqual(
                {"hello": "world"}, client.call_tool("echo", {"hello": "world"})
            )
            process = client.process
        self.assertIsNotNone(process.poll(), "owned MCP process must be reaped")

    def test_tool_errors_do_not_expose_server_payloads_or_secrets(self):
        output, diagnostics = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(diagnostics):
            with MCPClient(
                [sys.executable, "-u", "-c", FAKE_SERVER], timeout=2
            ) as client:
                with self.assertRaises(SafariLoginError) as caught:
                    client.call_tool("echo", {"fail": True})
        self.assertNotIn(
            "SECRET-TOKEN",
            str(caught.exception) + output.getvalue() + diagnostics.getvalue(),
        )

    def test_hung_server_times_out_and_is_reaped(self):
        client = MCPClient(
            [sys.executable, "-u", "-c", "import time; time.sleep(30)"], timeout=0.1
        )
        with self.assertRaises(SafariLoginError):
            with client:
                self.fail("unresponsive server must not initialize")
        self.assertIsNotNone(client.process.poll())

    def test_unavailable_executable_has_a_sanitized_error(self):
        with self.assertRaises(SafariLoginError):
            with MCPClient(["/missing/safaridriver"], timeout=0.1):
                self.fail("missing executable must fail")


class TestAtomicSessionStorage(unittest.TestCase):
    def test_private_state_replaces_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            path.write_text("old session")
            state = {"cookies": [], "origins": []}
            save_state_atomically(path, state)
            self.assertEqual(state, json.loads(path.read_text()))
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertEqual([path], list(Path(directory).iterdir()))

    def test_failed_serialization_preserves_existing_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            path.write_text("existing working session")
            with self.assertRaises(TypeError):
                save_state_atomically(path, {"cookies": object()})
            self.assertEqual("existing working session", path.read_text())
            self.assertEqual([path], list(Path(directory).iterdir()))


class TestSafariCookieConversion(unittest.TestCase):
    def capture(self, headers, **kwargs):
        return state_from_capture(
            "https://www.concursolutions.com",
            {
                "origin": "https://us2.concursolutions.com",
                "cookies": "language=en",
                "localStorage": [{"name": "preference", "value": "test"}],
            },
            [
                {
                    "url": "https://us2.concursolutions.com/login",
                    "responseHeaders": headers,
                }
            ],
            **kwargs,
        )

    def test_http_only_cookie_metadata_survives_conversion(self):
        state = self.capture(
            {
                "Set-Cookie": "JWT=SECRET; Domain=.concursolutions.com; Path=/; "
                "Max-Age=3600; HttpOnly; Secure; SameSite=Lax"
            }
        )
        cookie = next(c for c in state["cookies"] if c["name"] == "JWT")
        self.assertEqual("SECRET", cookie["value"])
        self.assertTrue(cookie["httpOnly"])
        self.assertTrue(cookie["secure"])
        self.assertEqual(".concursolutions.com", cookie["domain"])
        self.assertGreater(cookie["expires"], time.time())
        self.assertEqual("Lax", cookie["sameSite"])
        self.assertEqual(
            "https://us2.concursolutions.com", state["origins"][0]["origin"]
        )

    def test_multiple_set_cookie_headers_preserve_paths_and_expiry_commas(self):
        state = self.capture(
            {
                "Set-Cookie": [
                    "JWT=SECRET; Path=/; Max-Age=3600; HttpOnly; Secure",
                    "csrf=one; Path=/nui/expense; HttpOnly; Secure",
                    "csrf=two; Path=/nui/signin; HttpOnly; Secure",
                    "expired=gone; Expires=Wed, 01 Jan 2020 00:00:00 GMT; Path=/",
                ]
            }
        )
        csrf = [c for c in state["cookies"] if c["name"] == "csrf"]
        self.assertEqual({"/nui/expense", "/nui/signin"}, {c["path"] for c in csrf})
        self.assertFalse(any(c["name"] == "expired" for c in state["cookies"]))

    def test_identity_provider_and_foreign_domain_cookies_are_not_imported(self):
        with self.assertRaises(SafariLoginError):
            self.capture(
                {"Set-Cookie": "JWT=SECRET; Domain=.unrelated.example; Max-Age=3600"}
            )
        with self.assertRaises(SafariLoginError):
            state_from_capture(
                "https://www.concursolutions.com",
                {"origin": "https://idp.example", "cookies": "JWT=SECRET"},
                [],
            )

    def test_document_cookie_alone_is_not_an_authenticated_handoff(self):
        with self.assertRaises(SafariLoginError):
            state_from_capture(
                "https://www.concursolutions.com",
                {"origin": "https://www.concursolutions.com", "cookies": "JWT=SECRET"},
                [],
            )

    def test_max_age_is_anchored_to_capture_time_and_never_extended(self):
        page = {"origin": "https://www.concursolutions.com", "localStorage": []}
        network = [
            {
                "url": "https://www.concursolutions.com/auth",
                "receivedAt": 900,
                "responseHeaders": {
                    "Set-Cookie": "JWT=SECRET; Path=/; Max-Age=200; HttpOnly"
                },
            }
        ]
        state = state_from_capture(
            "https://www.concursolutions.com", page, network, now=1000
        )
        self.assertEqual(1100, state["cookies"][0]["expires"])
        with self.assertRaises(SafariLoginError):
            state_from_capture(
                "https://www.concursolutions.com", page, network, now=1200
            )

    def test_request_cookie_cannot_replace_missing_redirect_response_metadata(self):
        with self.assertRaises(SafariLoginError) as caught:
            state_from_capture(
                "https://www.concursolutions.com",
                {"origin": "https://www.concursolutions.com", "cookies": ""},
                [
                    {
                        "url": "https://www.concursolutions.com/nui/expense",
                        "requestHeaders": {"Cookie": "JWT=SECRET"},
                        "responseHeaders": {"Content-Type": "text/html"},
                    }
                ],
            )
        self.assertNotIn("SECRET", str(caught.exception))

    def test_expired_auth_cookie_is_rejected_without_leaking_its_value(self):
        with self.assertRaises(SafariLoginError) as caught:
            self.capture({"Set-Cookie": "JWT=SECRET; Max-Age=0; HttpOnly"})
        self.assertNotIn("SECRET", str(caught.exception))


class TestLoginHandoff(unittest.TestCase):
    def setUp(self):
        from contextlib import nullcontext
        from unittest.mock import Mock

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "session.json"
        self.path.write_text("previous session")
        self.client = Mock(
            session_file=str(self.path), base_url="https://www.concursolutions.com"
        )
        self.client._session_lock.side_effect = nullcontext
        self.candidate = Mock()
        self.candidate.check_session_validity.return_value = {
            "success": True,
            "authenticated": True,
        }
        self.candidate.verify_session_reuse.return_value = []
        self.state = {
            "cookies": [
                {
                    "name": "JWT",
                    "value": "SECRET",
                    "domain": ".concursolutions.com",
                    "path": "/",
                    "expires": time.time() + 3600,
                }
            ],
            "origins": [],
        }
        self.events = []

        def factory(**kwargs):
            self.candidate.session_file = kwargs["session_file"]
            return self.candidate

        self.factory = factory

    def run_login(self, **kwargs):
        from ccworks.safari_login import run_login

        return run_login(
            self.client,
            browser="safari",
            client_factory=self.factory,
            safari_capture=lambda base: self.state,
            **kwargs,
        )

    def test_promotes_only_after_two_independent_browser_checks(self):
        def validity(**kwargs):
            self.assertEqual("previous session", self.path.read_text())
            self.assertNotEqual(str(self.path), self.candidate.session_file)
            self.events.append("authenticated")
            return {"success": True, "authenticated": True}

        self.candidate.check_session_validity.side_effect = validity
        self.candidate.verify_session_reuse.side_effect = lambda **kwargs: (
            self.events.append("reused") or []
        )
        result = self.run_login()
        self.assertEqual(["authenticated", "reused"], self.events)
        self.assertEqual(self.state, json.loads(self.path.read_text()))
        self.assertEqual("safari", result["login_browser"])
        self.assertFalse(result["fallback_used"])
        self.assertFalse(Path(self.candidate.session_file).exists())

    def test_invalid_or_revoked_candidate_preserves_saved_session(self):
        from ccworks.safari_login import SafariLoginError

        self.candidate.check_session_validity.return_value = {
            "success": True,
            "authenticated": False,
        }
        with self.assertRaises(SafariLoginError):
            self.run_login(fallback=False)
        self.assertEqual("previous session", self.path.read_text())
        self.candidate.verify_session_reuse.assert_not_called()

    def test_failed_reuse_preserves_saved_session_and_redacts_error(self):
        self.candidate.verify_session_reuse.side_effect = RuntimeError("SECRET")
        with self.assertRaises(SafariLoginError) as caught:
            self.run_login(fallback=False)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual("previous session", self.path.read_text())

    def test_expired_candidate_never_reaches_browser(self):
        self.state["cookies"][0]["expires"] = time.time() - 1
        with self.assertRaises(SafariLoginError):
            self.run_login(fallback=False)
        self.candidate.check_session_validity.assert_not_called()
        self.assertEqual("previous session", self.path.read_text())

    def test_cancel_does_not_trigger_fallback(self):
        from unittest.mock import Mock
        from ccworks.safari_login import run_login, LoginCancelled

        capture = Mock(side_effect=LoginCancelled("Login cancelled"))
        with self.assertRaises(LoginCancelled):
            run_login(
                self.client,
                browser="safari",
                safari_capture=capture,
                client_factory=self.factory,
            )
        self.candidate.run_headed_login.assert_not_called()
        self.assertEqual("previous session", self.path.read_text())

    def test_unavailable_safari_falls_back_to_staged_chromium(self):
        from unittest.mock import Mock
        from ccworks.safari_login import run_login

        def headed():
            save_state_atomically(Path(self.candidate.session_file), self.state)

        self.candidate.run_headed_login.side_effect = headed
        result = run_login(
            self.client,
            browser="auto",
            client_factory=self.factory,
            safari_capture=Mock(side_effect=SafariLoginError("Unavailable")),
        )
        self.assertEqual("chromium", result["login_browser"])
        self.assertTrue(result["fallback_used"])
        self.candidate.run_headed_login.assert_called_once()
        self.assertEqual(self.state, json.loads(self.path.read_text()))

    def test_reauthentication_replaces_only_verified_candidate(self):
        self.run_login()
        self.state["cookies"][0]["value"] = "REAUTHENTICATED"
        self.run_login()
        self.assertEqual(
            "REAUTHENTICATED", json.loads(self.path.read_text())["cookies"][0]["value"]
        )

    def test_export_timeout_identifies_stage_and_tool(self):
        from unittest.mock import Mock
        from ccworks.safari_login import run_login

        failure = SafariLoginError("Safari MCP request timed out")
        failure.tool = "get_network_request"
        with self.assertRaises(SafariLoginError) as caught:
            run_login(
                self.client,
                browser="safari",
                fallback=False,
                client_factory=self.factory,
                safari_capture=Mock(side_effect=failure),
            )
        self.assertIn("Safari session export", str(caught.exception))
        self.assertIn("get_network_request", str(caught.exception))
        self.assertIn("timed out", str(caught.exception))
        self.assertEqual("previous session", self.path.read_text())

    def test_unknown_export_error_text_is_redacted(self):
        from unittest.mock import Mock
        from ccworks.safari_login import run_login

        with self.assertRaises(SafariLoginError) as caught:
            run_login(
                self.client,
                browser="safari",
                fallback=False,
                client_factory=self.factory,
                safari_capture=Mock(side_effect=SafariLoginError("SECRET")),
            )
        self.assertIn("Safari session export", str(caught.exception))
        self.assertNotIn("SECRET", str(caught.exception))

    def test_invalid_browser_is_rejected_without_starting_browser(self):
        from ccworks.safari_login import run_login

        with self.assertRaises(ValueError):
            run_login(self.client, browser="unknown", client_factory=self.factory)
        self.candidate.run_headed_login.assert_not_called()

    def test_ctrl_c_returns_a_failure_exit_status(self):
        import signal
        from ccworks.cli import signal_handler

        with self.assertRaises(SystemExit) as caught:
            signal_handler(signal.SIGINT, None)
        self.assertEqual(130, caught.exception.code)

    def test_default_login_uses_chromium_without_starting_safari(self):
        from unittest.mock import Mock
        from ccworks.safari_login import run_login

        def headed():
            save_state_atomically(Path(self.candidate.session_file), self.state)

        self.candidate.run_headed_login.side_effect = headed
        capture = Mock()
        result = run_login(
            self.client, client_factory=self.factory, safari_capture=capture
        )
        capture.assert_not_called()
        self.candidate.run_headed_login.assert_called_once()
        self.assertEqual("chromium", result["login_browser"])
        self.assertFalse(result["fallback_used"])

    def test_safari_opt_in_reports_incomplete_experimental_status(self):
        diagnostics = StringIO()
        with redirect_stderr(diagnostics):
            self.run_login()
        text = diagnostics.getvalue().lower()
        for word in ("experimental", "incomplete", "redirect", "chromium"):
            self.assertIn(word, text)
        self.assertNotIn("SECRET", diagnostics.getvalue())

    def test_cli_help_labels_safari_experimental_and_incomplete(self):
        from ccworks.cli import build_parser

        parser, _ = build_parser()
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            parser.parse_args(["session", "login", "--help"])
        self.assertEqual(0, caught.exception.code)
        self.assertIn("experimental", output.getvalue().lower())
        self.assertIn("incomplete", output.getvalue().lower())

    def test_cli_options(self):
        from ccworks.cli import build_parser

        parser, _ = build_parser()
        args = parser.parse_args(
            ["session", "login", "--browser", "safari", "--no-fallback"]
        )
        self.assertEqual("safari", args.login_browser)
        self.assertTrue(args.no_fallback)


class TestMCPProcessTree(unittest.TestCase):
    @unittest.skipUnless(sys.platform != "win32", "POSIX process groups")
    def test_inherited_stdout_does_not_block_shutdown(self):
        server = (
            'import subprocess\nsubprocess.Popen([__import__("sys").executable, "-c", "import time; time.sleep(3)"])\n'
            + FAKE_SERVER
        )
        with MCPClient([sys.executable, "-u", "-c", server], timeout=2) as client:
            started = time.monotonic()
        self.assertLess(time.monotonic() - started, 1.7)
        self.assertFalse(
            client._reader.is_alive(),
            "MCP reader must terminate with its owned process tree",
        )


class TestSafariNativeAdapter(unittest.TestCase):
    def fake_client(self):
        from unittest.mock import MagicMock

        client = MagicMock()
        client.__enter__.return_value = client
        client.tools = {
            name: {"inputSchema": {"properties": props, "required": list(props)}}
            for name, props in {
                "create_tab": {"url": {}},
                "close_tab": {"tab_uuid": {}},
                "navigate_to_url": {"url": {}},
                "evaluate_javascript": {"expression": {}},
                "list_network_requests": {},
                "get_network_request": {"requestId": {}},
            }.items()
        }

        def tool(name, args):
            return {
                "create_tab": {"handle": "owned-fixture-tab"},
                "close_tab": {},
                "navigate_to_url": {},
                "evaluate_javascript": {
                    "origin": "https://www.concursolutions.com",
                    "localStorage": [],
                },
                "list_network_requests": {
                    "requests": [
                        {
                            "requestId": "1",
                            "url": "https://www.concursolutions.com/auth",
                        },
                        {"requestId": "2", "url": "https://idp.example/auth"},
                    ]
                },
                "get_network_request": {
                    "response": {
                        "headers": {
                            "Set-Cookie": "JWT=SECRET; Path=/; Secure; HttpOnly; Max-Age=3600"
                        }
                    }
                },
            }[name]

        client.call_tool.side_effect = tool
        return client

    def test_manual_handoff_uses_advertised_schema_and_excludes_idp(self):
        from unittest.mock import patch
        from ccworks.safari_login import capture_safari_state

        client = self.fake_client()
        with (
            patch("sys.platform", "darwin"),
            patch("ccworks.safari_login.MCPClient", return_value=client),
            patch("ccworks.safari_login.wait_for_login") as handoff,
        ):
            state = capture_safari_state("https://www.concursolutions.com")
        handoff.assert_called_once()
        self.assertTrue(state["cookies"][0]["httpOnly"])
        details = [
            call
            for call in client.call_tool.call_args_list
            if call.args[0] == "get_network_request"
        ]
        self.assertEqual(1, len(details))
        self.assertEqual({"requestId": "1"}, details[0].args[1])
        client.__exit__.assert_called_once()

    def test_native_safari_contract_arms_capture_before_navigation_and_returns_js(self):
        from unittest.mock import patch
        from ccworks.safari_login import capture_safari_state

        client = self.fake_client()
        client.tools["create_tab"] = {
            "inputSchema": {"properties": {"url": {}}, "required": []}
        }
        client.tools["get_network_request"]["inputSchema"] = {
            "properties": {"request_id": {}},
            "required": ["request_id"],
        }
        calls = []

        def native_tool(name, args):
            calls.append(name)
            if name == "create_tab":
                return {"handle": "owned-fixture-tab"}
            if name == "close_tab":
                self.assertEqual({"tab_uuid": "owned-fixture-tab"}, args)
                return {}
            if name == "navigate_to_url":
                self.assertIn(
                    "list_network_requests",
                    calls[:-1],
                    "network capture must be armed before authentication",
                )
                return {"url": "https://www.concursolutions.com"}
            if name == "evaluate_javascript":
                self.assertTrue(
                    args["expression"].lstrip().startswith("return "),
                    "native Safari evaluates a function body",
                )
                return {"origin": "https://www.concursolutions.com", "localStorage": []}
            if name == "list_network_requests":
                return (
                    {
                        "count": 1,
                        "requests": [
                            {
                                "request_id": "1",
                                "url": "https://www.concursolutions.com/auth",
                            }
                        ],
                    }
                    if "navigate_to_url" in calls
                    else {"count": 0, "requests": []}
                )
            if name == "get_network_request":
                self.assertEqual({"request_id": "1"}, args)
                return {
                    "request": {
                        "response_headers": {
                            "Set-Cookie": "JWT=SECRET; Path=/; Secure; HttpOnly; Max-Age=3600"
                        }
                    }
                }
            self.fail("unexpected native tool")

        client.call_tool.side_effect = native_tool
        with (
            patch("sys.platform", "darwin"),
            patch("ccworks.safari_login.MCPClient", return_value=client),
            patch("ccworks.safari_login.wait_for_login"),
        ):
            state = capture_safari_state("https://www.concursolutions.com")
        self.assertEqual("create_tab", calls[0])
        self.assertEqual("close_tab", calls[-1])
        self.assertTrue(state["cookies"][0]["httpOnly"])
        self.assertEqual("SECRET", state["cookies"][0]["value"])

    def test_auth_response_is_prioritized_without_fetching_static_asset_bodies(self):
        from unittest.mock import patch
        from ccworks.safari_login import capture_safari_state

        client = self.fake_client()
        original = client.call_tool.side_effect
        details = []

        def tool(name, args):
            if name == "list_network_requests":
                return {
                    "requests": [
                        {
                            "requestId": str(i),
                            "url": f"https://www.concursolutions.com/static/{i}.js",
                            "mime_type": "application/javascript",
                            "status": 200,
                        }
                        for i in range(200)
                    ]
                    + [
                        {
                            "requestId": "auth",
                            "url": "https://www.concursolutions.com/nui/expense",
                            "mime_type": "text/html",
                            "status": 200,
                        }
                    ]
                }
            if name == "get_network_request":
                details.append(args["requestId"])
                if args["requestId"] != "auth":
                    raise SafariLoginError(
                        "a static body lookup would exhaust the export budget"
                    )
            return original(name, args)

        client.call_tool.side_effect = tool
        with (
            patch("sys.platform", "darwin"),
            patch("ccworks.safari_login.MCPClient", return_value=client),
            patch("ccworks.safari_login.wait_for_login"),
        ):
            state = capture_safari_state("https://www.concursolutions.com")
        self.assertEqual(["auth"], details)
        self.assertTrue(state["cookies"][0]["httpOnly"])

    def test_unknown_required_schema_fails_before_manual_login(self):
        from unittest.mock import patch
        from ccworks.safari_login import capture_safari_state

        client = self.fake_client()
        client.tools["navigate_to_url"]["inputSchema"]["required"].append("unsupported")
        with (
            patch("sys.platform", "darwin"),
            patch("ccworks.safari_login.MCPClient", return_value=client),
            patch("ccworks.safari_login.wait_for_login") as handoff,
        ):
            with self.assertRaises(SafariLoginError):
                capture_safari_state("https://www.concursolutions.com")
        handoff.assert_not_called()
        client.__exit__.assert_called_once()

    def test_cancel_closes_only_the_owned_login_tab(self):
        from unittest.mock import patch, call
        from ccworks.safari_login import capture_safari_state, LoginCancelled

        client = self.fake_client()
        with (
            patch("sys.platform", "darwin"),
            patch("ccworks.safari_login.MCPClient", return_value=client),
            patch(
                "ccworks.safari_login.wait_for_login",
                side_effect=LoginCancelled("cancelled"),
            ),
        ):
            with self.assertRaises(LoginCancelled):
                capture_safari_state("https://www.concursolutions.com")
        self.assertEqual(
            call("close_tab", {"tab_uuid": "owned-fixture-tab"}),
            client.call_tool.call_args_list[-1],
        )
        self.assertFalse(
            any(
                c.args[0] == "evaluate_javascript"
                for c in client.call_tool.call_args_list
            )
        )

    def test_manual_eof_cancels_without_export(self):
        from unittest.mock import patch
        from ccworks.safari_login import wait_for_login, LoginCancelled

        with patch("sys.stdin", StringIO("")), redirect_stderr(StringIO()):
            with self.assertRaises(LoginCancelled):
                wait_for_login()


class TestAuthenticationDocumentation(unittest.TestCase):
    def test_command_reference_keeps_chromium_default_and_safari_experimental(self):
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
        rows = [line for line in readme.splitlines() if line.startswith("|")]
        default = [line for line in rows if "| `session login` |" in line]
        experimental = [
            line for line in rows if "session login --browser safari" in line
        ]
        self.assertEqual(1, len(default), "document the normal login command once")
        self.assertIn("chromium", default[0].lower())
        self.assertIn("default", default[0].lower())
        self.assertEqual(1, len(experimental), "list the experimental login separately")
        self.assertIn("experimental", experimental[0].lower())
        self.assertIn("incomplete", experimental[0].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
