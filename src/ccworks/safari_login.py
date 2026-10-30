"""Optional Safari MCP login with bounded transport and private session files.

MCP payloads can contain credentials. Never echo server messages or tool errors
into exceptions, logs, or the CLI's data channel.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
import math
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any, Sequence
from urllib.parse import urlsplit

from ccworks import __version__


class SafariLoginError(RuntimeError):
    """A sanitized Safari startup, protocol, export, or validation failure."""

    tool: str | None = None


class MCPClient:
    """Small stdio MCP client. One request at a time; no server logging."""

    MAX_MESSAGE = 8 * 1024 * 1024

    def __init__(self, command: Sequence[str], timeout: float = 30):
        if not command or not 0 < timeout <= 120:
            raise ValueError(
                "A command and a timeout between zero and 120 seconds are required"
            )
        self.command = list(command)
        self.timeout = timeout
        self.process: subprocess.Popen[str] | None = None
        self.tools: dict[str, dict[str, Any]] = {}
        self._responses: queue.Queue[Any] = queue.Queue(maxsize=128)
        self._write_lock = threading.Lock()
        self._request_id = 0
        self._reader: threading.Thread | None = None

    def __enter__(self):
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                start_new_session=os.name == "posix",
            )
            self._reader = threading.Thread(target=self._read, daemon=True)
            self._reader.start()
            initialized = self._request(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "ccworks", "version": __version__},
                },
            )
            if not isinstance(initialized, dict) or initialized.get(
                "protocolVersion"
            ) not in ("2024-11-05", "2025-03-26", "2025-06-18"):
                raise SafariLoginError("Safari MCP negotiated an unsupported protocol")
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            listing = self._request("tools/list", {})
            if not isinstance(listing, dict) or not isinstance(
                listing.get("tools"), list
            ):
                raise SafariLoginError("Safari MCP returned invalid capabilities")
            self.tools = {
                t["name"]: t
                for t in listing["tools"]
                if isinstance(t, dict) and isinstance(t.get("name"), str)
            }
            return self
        except BaseException as exc:
            self.close()
            if isinstance(exc, (KeyboardInterrupt, SystemExit, SafariLoginError)):
                raise
            raise SafariLoginError(
                "Safari MCP could not start; check Safari 27 and driver authorization"
            ) from None

    def _send(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise SafariLoginError("Safari MCP is not running")
        try:
            with self._write_lock:
                self.process.stdin.write(json.dumps(message) + "\n")
                self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            raise SafariLoginError("Safari MCP connection closed") from None

    def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                line = self.process.stdout.readline(self.MAX_MESSAGE + 1)
                if not line or len(line) > self.MAX_MESSAGE:
                    raise SafariLoginError(
                        "Safari MCP connection closed or exceeded its message limit"
                    )
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise SafariLoginError(
                        "Safari MCP sent an invalid protocol message"
                    )
                if "method" in message:
                    if "id" in message:
                        reply = {"jsonrpc": "2.0", "id": message["id"]}
                        if message["method"] == "ping":
                            reply["result"] = {}
                        else:
                            reply["error"] = {
                                "code": -32601,
                                "message": "Unsupported client request",
                            }
                        self._send(reply)
                    # Logging notifications can contain credentials. Discard them.
                    continue
                self._responses.put_nowait(message)
        except Exception:
            try:
                self._responses.put_nowait(
                    SafariLoginError("Safari MCP connection closed or protocol failed")
                )
            except queue.Full:
                pass

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        self._request_id += 1
        identifier = self._request_id
        self._send(
            {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
        )
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SafariLoginError("Safari MCP request timed out")
            try:
                message = self._responses.get(timeout=remaining)
            except queue.Empty:
                raise SafariLoginError("Safari MCP request timed out") from None
            if isinstance(message, Exception):
                raise message
            if message.get("id") != identifier:
                continue
            if "error" in message or "result" not in message:
                raise SafariLoginError("Safari MCP request failed")
            return message["result"]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in self.tools:
            raise SafariLoginError("Required Safari MCP capability is unavailable")
        try:
            result = self._request("tools/call", {"name": name, "arguments": arguments})
        except SafariLoginError as error:
            error.tool = name
            raise
        if not isinstance(result, dict) or result.get("isError"):
            failure = SafariLoginError("Safari MCP tool call failed")
            failure.tool = name
            raise failure
        if "structuredContent" in result:
            return result["structuredContent"]
        content = result.get("content", [])
        if not isinstance(content, list):
            raise SafariLoginError("Safari MCP returned an invalid tool response")
        texts = [
            c.get("text", "")
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        text = "\n".join(texts)
        try:
            return json.loads(text)
        except ValueError:
            return text

    def close(self) -> None:
        process = self.process
        if process is not None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            # A descendant can keep stdout open after the driver exits. Kill
            # only this owned process group, never Safari or unrelated drivers.
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if self._reader is not None:
                self._reader.join(timeout=1)
            if process.stdout is not None and (
                self._reader is None or not self._reader.is_alive()
            ):
                process.stdout.close()

    def __exit__(self, *args):
        self.close()


def save_state_atomically(path: Path, state: dict[str, Any]) -> None:
    """Replace a session only after the caller has validated it."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    candidate = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".session-",
            suffix=".json",
            dir=path.parent,
            delete=False,
        ) as stream:
            candidate = Path(stream.name)
            os.chmod(candidate, 0o600)
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(candidate, path)
    finally:
        if candidate is not None:
            candidate.unlink(missing_ok=True)


def _trusted_host(base_url: str, hostname: str | None) -> bool:
    base = urlsplit(base_url).hostname
    host = (hostname or "").lower().rstrip(".")
    if not base or not host:
        return False
    base = base.lower().rstrip(".")
    if base == "concursolutions.com" or base.endswith(".concursolutions.com"):
        return host == "concursolutions.com" or host.endswith(".concursolutions.com")
    return host == base


def _header_values(headers: Any, name: str) -> list[str]:
    if isinstance(headers, dict):
        pairs = list(headers.items())
    elif isinstance(headers, list):
        pairs = [
            (h.get("name"), h.get("value")) for h in headers if isinstance(h, dict)
        ]
    else:
        return []
    values = []
    for key, value in pairs:
        if isinstance(key, str) and key.lower() == name.lower():
            candidates = value if isinstance(value, list) else [value]
            if any(not isinstance(v, str) for v in candidates):
                raise SafariLoginError(
                    "Safari returned invalid response-header metadata"
                )
            values.extend(candidates)
    return values


def state_from_capture(
    base_url: str,
    page: dict[str, Any],
    network: list[dict[str, Any]],
    now: float | None = None,
) -> dict[str, Any]:
    """Convert first-party Set-Cookie headers and origin storage to Playwright state.

    document.cookie cannot export HttpOnly authentication. Require an auth
    cookie from actual first-party response headers, preserving its scope.
    A browser acceptance check is still required before promoting this state.
    """
    timestamp = time.time() if now is None else now
    origin = page.get("origin")
    if not isinstance(origin, str):
        raise SafariLoginError("Safari did not return an authenticated service origin")
    parsed_origin = urlsplit(origin)
    if (
        parsed_origin.scheme not in ("https", "http")
        or parsed_origin.username
        or not _trusted_host(base_url, parsed_origin.hostname)
    ):
        raise SafariLoginError("Safari is not on the expected service origin")
    jar: dict[tuple[str, str, str], dict[str, Any]] = {}
    try:
        for record in network:
            url = record.get("url")
            if not isinstance(url, str):
                continue
            parsed = urlsplit(url)
            if parsed.scheme not in ("https", "http") or not _trusted_host(
                base_url, parsed.hostname
            ):
                continue
            hostname = parsed.hostname or ""
            received_at = record.get("receivedAt", timestamp)
            if (
                not isinstance(received_at, (int, float))
                or not math.isfinite(received_at)
                or received_at > timestamp
            ):
                raise SafariLoginError(
                    "Safari returned invalid response timing metadata"
                )
            for value in _header_values(record.get("responseHeaders"), "set-cookie"):
                # An Expires date contains a comma. Split only at a new cookie
                # assignment, not at the day/date separator.
                lines = re.split(r"\r?\n|,(?=\s*[!#$%&'*+.^_`|~\w-]+=)", value)
                for line in lines:
                    parsed_cookie: SimpleCookie = SimpleCookie()
                    parsed_cookie.load(line)
                    for name, morsel in parsed_cookie.items():
                        domain_attr = morsel["domain"].lower().lstrip(".").rstrip(".")
                        if domain_attr and (
                            not _trusted_host(base_url, domain_attr)
                            or not (
                                hostname == domain_attr
                                or hostname.endswith("." + domain_attr)
                            )
                        ):
                            raise SafariLoginError(
                                "Safari response contained an out-of-scope cookie"
                            )
                        domain = "." + domain_attr if domain_attr else hostname
                        request_path = parsed.path or "/"
                        default_path = request_path.rsplit("/", 1)[0] or "/"
                        path = (
                            morsel["path"]
                            if morsel["path"].startswith("/")
                            else default_path
                        )
                        expires = -1.0
                        if morsel["max-age"]:
                            expires = received_at + int(morsel["max-age"])
                        elif morsel["expires"]:
                            expires = parsedate_to_datetime(
                                morsel["expires"]
                            ).timestamp()
                        if not math.isfinite(expires):
                            raise SafariLoginError(
                                "Safari cookie has invalid expiry metadata"
                            )
                        key = name, domain, path
                        if expires != -1 and expires <= timestamp:
                            jar.pop(key, None)
                            continue
                        same_site = morsel["samesite"].capitalize() or "Lax"
                        if same_site not in ("Strict", "Lax", "None"):
                            raise SafariLoginError(
                                "Safari cookie has unsupported SameSite metadata"
                            )
                        secure = bool(morsel["secure"])
                        if same_site == "None" and not secure:
                            raise SafariLoginError(
                                "Safari cookie has invalid security metadata"
                            )
                        jar[key] = {
                            "name": name,
                            "value": morsel.value,
                            "domain": domain,
                            "path": path,
                            "expires": expires,
                            "httpOnly": bool(morsel["httponly"]),
                            "secure": secure,
                            "sameSite": same_site,
                        }
    except SafariLoginError:
        raise
    except Exception:
        raise SafariLoginError(
            "Safari response cookie metadata could not be converted"
        ) from None
    if not any(c["name"] == "JWT" for c in jar.values()):
        raise SafariLoginError(
            "Safari did not expose a usable authentication cookie; session was not replaced"
        )

    # Readable preferences are optional. Never replace authoritative HttpOnly,
    # domain, or path metadata with document.cookie's incomplete information.
    readable = page.get("cookies", "")
    if isinstance(readable, str):
        known_names = {c["name"] for c in jar.values()}
        for pair in readable.split(";"):
            name, separator, value = pair.strip().partition("=")
            if (
                separator
                and name not in known_names
                and re.fullmatch(r"[!#$%&'*+.^_`|~\w-]+", name)
            ):
                jar[name, parsed_origin.hostname or "", "/"] = {
                    "name": name,
                    "value": value,
                    "domain": parsed_origin.hostname,
                    "path": "/",
                    "expires": -1,
                    "httpOnly": False,
                    "secure": parsed_origin.scheme == "https",
                    "sameSite": "Lax",
                }
    storage = page.get("localStorage", [])
    if not isinstance(storage, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("name"), str)
        or not isinstance(item.get("value"), str)
        for item in storage
    ):
        raise SafariLoginError("Safari returned invalid origin storage")
    return {
        "cookies": list(jar.values()),
        "origins": [{"origin": origin, "localStorage": storage}],
    }


class LoginCancelled(RuntimeError):
    """Manual authentication was cancelled; do not start another browser."""


def wait_for_login() -> None:
    import sys

    sys.stderr.write(
        "\nComplete Concur sign-in and MFA in Safari. Once the expense dashboard "
        "is visible, return here and press ENTER (Ctrl-C cancels): "
    )
    sys.stderr.flush()
    if not sys.stdin.readline():
        raise LoginCancelled("Login cancelled; the saved session was preserved")


def capture_safari_state(base_url: str) -> dict[str, Any]:
    """Experimental native adapter; unsupported or redacted exports fail closed.

    Safari advertises its tool schemas at runtime. Only recognized arguments
    are supplied. Unknown required arguments trigger the Chromium fallback.
    """
    import sys

    if sys.platform != "darwin":
        raise SafariLoginError("Safari MCP authentication requires macOS and Safari 27")
    with MCPClient(["/usr/bin/safaridriver", "--mcp"]) as mcp, ExitStack() as cleanup:
        tab = None

        def invoke(name: str, values: dict[str, Any]) -> Any:
            tool = mcp.tools.get(name, {})
            schema = tool.get("inputSchema", {})
            properties = schema.get("properties", {})
            aliases = {
                "url": ("url",),
                "script": ("script", "expression", "javascript", "code", "function"),
                "request": ("requestId", "request_id", "id", "requestIdentifier"),
                "tab": (
                    "tabId",
                    "tab_id",
                    "tabHandle",
                    "tab_handle",
                    "tab_uuid",
                    "handle",
                ),
            }
            arguments = {}
            for semantic, value in values.items():
                for key in aliases[semantic]:
                    if key in properties:
                        arguments[key] = value
                        break
                else:
                    raise SafariLoginError("Safari MCP tool schema is unsupported")
            for key in aliases["tab"]:
                if key in properties and tab is not None:
                    arguments[key] = tab
            if any(key not in arguments for key in schema.get("required", [])):
                raise SafariLoginError("Safari MCP tool schema is unsupported")
            return mcp.call_tool(name, arguments)

        required = {
            "create_tab",
            "navigate_to_url",
            "evaluate_javascript",
            "list_network_requests",
            "get_network_request",
        }
        if not required.issubset(mcp.tools):
            raise SafariLoginError(
                "Safari MCP lacks required session-export capabilities"
            )
        capture_started = time.time()
        created = invoke("create_tab", {"url": "about:blank"})
        tab = created.get("handle") if isinstance(created, dict) else None
        if not isinstance(tab, str) or not tab:
            raise SafariLoginError("Safari MCP did not identify the owned login tab")

        def close_owned_tab() -> None:
            try:
                if "close_tab" in mcp.tools:
                    invoke("close_tab", {"tab": tab})
            except Exception:
                # Cleanup must not expose payloads or obscure the login result.
                pass

        cleanup.callback(close_owned_tab)
        # Safari starts recording when the network tool is first used. Arm it
        # before authentication so the initial HttpOnly Set-Cookie is retained.
        invoke("list_network_requests", {})
        navigation = invoke("navigate_to_url", {"url": base_url})
        if isinstance(navigation, dict):
            tab = navigation.get("tabId", navigation.get("tabHandle", tab))
        wait_for_login()
        export_deadline = time.monotonic() + 60
        host = urlsplit(base_url).hostname or ""
        root = (
            "concursolutions.com"
            if _trusted_host("https://concursolutions.com", host)
            else host
        )
        # Check the origin before touching storage: never export IdP storage.
        script = """return (() => {
          const host = location.hostname.toLowerCase();
          const root = ROOT;
          if (!(host === root || (root === 'concursolutions.com' && host.endsWith('.' + root))))
            return {origin: location.origin};
          return {origin: location.origin, cookies: document.cookie,
            localStorage: Object.keys(localStorage).map(name => ({name, value: localStorage.getItem(name)}))};
        })()""".replace("ROOT", json.dumps(root))
        page = invoke("evaluate_javascript", {"script": script})
        if isinstance(page, dict) and isinstance(page.get("result"), dict):
            page = page["result"]
        if not isinstance(page, dict) or "origin" not in page:
            raise SafariLoginError(
                "Safari MCP did not expose structured origin storage"
            )
        listing = invoke("list_network_requests", {})
        requests = listing.get("requests") if isinstance(listing, dict) else listing
        if isinstance(listing, dict) and (
            listing.get("hasMore") or listing.get("nextCursor")
        ):
            raise SafariLoginError("Safari MCP returned an incomplete network capture")
        if not isinstance(requests, list) or len(requests) > 1000:
            raise SafariLoginError(
                "Safari MCP did not expose bounded structured network metadata"
            )
        candidates = []
        for index, request in enumerate(requests):
            if not isinstance(request, dict):
                continue
            url = request.get("url")
            if not isinstance(url, str) or not _trusted_host(
                base_url, urlsplit(url).hostname
            ):
                continue
            status = request.get("status")
            if isinstance(status, (int, float)) and (status <= 0 or status >= 400):
                continue
            mime = str(request.get("mime_type", "")).split(";", 1)[0].lower()
            if mime.startswith(("image/", "font/", "audio/", "video/")) or mime in {
                "application/javascript",
                "text/javascript",
                "text/css",
                "application/wasm",
            }:
                continue
            priority = (
                0
                if mime == "text/html"
                else 1
                if isinstance(status, (int, float)) and 300 <= status < 400
                else 2
                if "json" in mime
                else 3
            )
            candidates.append((priority, -index, request))
        # Native detail calls can each wait five seconds for a response body.
        # Prefer recent documents/auth redirects over hundreds of static assets.
        candidates.sort(key=lambda item: (item[0], item[1]))
        captured: dict[int, dict[str, Any]] = {}
        for _, reverse_index, request in candidates:
            if time.monotonic() > export_deadline:
                raise SafariLoginError("Safari session export exceeded its time limit")
            url = request["url"]
            identifier = request.get(
                "requestId", request.get("request_id", request.get("id"))
            )
            if identifier is None:
                raise SafariLoginError("Safari MCP network identifiers are unavailable")
            details = invoke("get_network_request", {"request": identifier})
            if not isinstance(details, dict):
                raise SafariLoginError(
                    "Safari MCP response-header metadata is unavailable"
                )
            response = details.get("response", {})
            headers = details.get("responseHeaders")
            if headers is None and isinstance(response, dict):
                headers = response.get("headers")
            native_request = details.get("request", {})
            if headers is None and isinstance(native_request, dict):
                headers = native_request.get("response_headers")
            # Without an authoritative response timestamp, the beginning of
            # this login is a conservative bound. Never restart a cookie TTL
            # after the human authentication delay.
            captured[-reverse_index] = {
                "url": url,
                "responseHeaders": headers,
                "receivedAt": capture_started,
            }
            # Cookie updates must be replayed in their original chronology,
            # regardless of which response was retrieved first.
            network = [captured[i] for i in sorted(captured)]
            if any(
                re.search(r"(?:^|[\r\n,])\s*JWT\s*=", value)
                for value in _header_values(headers, "set-cookie")
            ):
                # Stop before unrelated bodies exhaust the export budget. The
                # resulting minimal state still must pass two Chromium checks.
                return state_from_capture(base_url, page, network)
        return state_from_capture(
            base_url, page, [captured[i] for i in sorted(captured)]
        )


def _require_auth_state(state: Any, base_url: str) -> None:
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        raise SafariLoginError("Login did not produce a valid saved session")
    for cookie in state["cookies"]:
        if not isinstance(cookie, dict) or cookie.get("name") != "JWT":
            continue
        domain = cookie.get("domain")
        expires = cookie.get("expires", -1)
        if (
            isinstance(domain, str)
            and _trusted_host(base_url, domain.lstrip("."))
            and isinstance(cookie.get("value"), str)
            and cookie["value"]
            and isinstance(expires, (int, float))
            and math.isfinite(expires)
            and (expires == -1 or expires > time.time())
        ):
            return
    raise SafariLoginError(
        "Login did not capture a usable service authentication cookie"
    )


def run_login(
    client: Any,
    browser: str = "chromium",
    fallback: bool = True,
    *,
    client_factory: Any = None,
    safari_capture: Any = None,
) -> dict[str, Any]:
    """Capture privately, prove headless reuse, then atomically promote.

    Both capture backends use the same transaction. No candidate failure or
    cancellation changes the canonical state. Runtime clients still use Chromium.
    """
    import sys

    if browser not in ("chromium", "safari", "auto"):
        raise ValueError("Login browser must be chromium, safari, or auto")
    if browser in ("safari", "auto"):
        print(
            "Safari login is experimental and incomplete for Concur SSO: redirect cookie "
            "headers may be missing. Chromium remains the supported default.",
            file=sys.stderr,
        )
    if client_factory is None:
        from ccworks.browser_client import ConcurBrowserClient

        client_factory = ConcurBrowserClient
    capture = safari_capture or capture_safari_state
    destination = Path(client.session_file)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backends = ["chromium"] if browser == "chromium" else ["safari"]
    if browser != "chromium" and fallback:
        backends.append("chromium")
    for backend in backends:
        stage = "Safari session export" if backend == "safari" else "Chromium login"
        try:
            with tempfile.TemporaryDirectory(
                prefix=".login-", dir=destination.parent
            ) as directory:
                candidate_path = Path(directory) / "state.json"
                staged = client_factory(
                    session_file=str(candidate_path), base_url=client.base_url
                )
                if backend == "safari":
                    state = capture(client.base_url)
                    _require_auth_state(state, client.base_url)
                    save_state_atomically(candidate_path, state)
                else:
                    staged.run_headed_login()
                stage = "candidate validation"
                state = json.loads(candidate_path.read_text(encoding="utf-8"))
                _require_auth_state(state, client.base_url)
                stage = "headless authentication"
                valid = staged.check_session_validity(headless=True)
                if (
                    valid.get("success") is not True
                    or valid.get("authenticated") is not True
                ):
                    raise SafariLoginError(
                        "The candidate session failed headless authentication"
                    )
                # Each method opens and closes its own fresh browser context.
                stage = "headless session reuse"
                staged.verify_session_reuse(headless=True)
                state = json.loads(candidate_path.read_text(encoding="utf-8"))
                _require_auth_state(state, client.base_url)
                stage = "saved session promotion"
                with client._session_lock():
                    save_state_atomically(destination, state)
            return {
                "login_browser": backend,
                "verification_browser": "chromium",
                "fallback_used": backend == "chromium" and browser != "chromium",
            }
        except (EOFError, LoginCancelled):
            raise LoginCancelled(
                "Login cancelled; the saved session was preserved"
            ) from None
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            # Only known messages and capability names may cross the CLI
            # boundary; arbitrary exceptions can embed browser credentials.
            safe_reasons = {
                "Safari MCP request timed out",
                "Safari MCP connection closed or protocol failed",
                "Safari MCP tool call failed",
                "Safari MCP request failed",
                "The candidate session failed headless authentication",
                "Safari session export exceeded its time limit",
            }
            detail = (
                str(error)
                if isinstance(error, SafariLoginError) and str(error) in safe_reasons
                else "verification failed"
            )
            tool = getattr(error, "tool", None)
            if tool in {
                "create_tab",
                "navigate_to_url",
                "evaluate_javascript",
                "list_network_requests",
                "get_network_request",
            }:
                detail += f" ({tool})"
            if backend == "safari" and fallback:
                print(
                    "Safari authentication or session export failed; opening Playwright Chromium. "
                    "The saved session has been preserved.",
                    file=sys.stderr,
                )
                continue
            raise SafariLoginError(
                f"Login failed during {stage}: {detail}; the saved session was preserved"
            ) from None
    raise SafariLoginError("No authentication backend completed")
