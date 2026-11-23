# Experimental Safari MCP login

Safari 27 on macOS has an **incomplete, experimental** interactive login path
retained for development. It is not ready for routine Concur SSO. Reevaluate it
when Safari MCP exposes redirect cookie headers and the live Concur handoff
passes authentication and saved-session reuse checks:

```bash
./ccworks session login --browser safari
./ccworks session login --browser safari --no-fallback
```

`--browser auto` also tries the experimental Safari path first; it is an explicit
development opt-in. Developers can set `CCWORKS_LOGIN_BROWSER=safari` (or `auto`)
to opt in for both login and interactive reauthentication after session expiry. An explicit
`--browser` overrides the environment variable; invalid values fail immediately.
All report and expense operations continue through headless Playwright Chromium.

Enable remote automation and external agents in Safari's developer settings, and
complete any `/usr/bin/safaridriver --enable` authorization locally if macOS
requires it. ccworks never requests or handles your administrator password.
On standard macOS accounts, inspect the authorization right with
`security authorizationdb read com.apple.safaridriver.allow`. Its rules accept
administrators or members of `_webdeveloper`. A correct password can be rejected
when the current account belongs to neither group. An administrator can grant
`_webdeveloper` membership to the account that will run Safari; running `--enable`
as root alone does not verify that the standard account has that permission.

The manual handoff waits for you to complete SSO/MFA and press ENTER. EOF or
Ctrl-C cancels without launching the fallback browser. Safari startup and MCP
requests have timeouts, and owned driver processes are cleaned up on failure.

Safari must expose structured first-party `Set-Cookie` response headers, including
HttpOnly cookies and their scope, to produce a transferable session. A readable
`document.cookie` or request `Cookie` header alone is insufficient. Identity-provider
cookies and storage are excluded from the Safari export. If Safari is unavailable,
hangs, exposes an unsupported tool schema, redacts authentication headers, or
produces state Chromium cannot reuse, ccworks opens the existing headed Playwright
login. `--no-fallback` instead fails and preserves the saved session.

Native Safari 27 has been validated against a local synthetic server: the adapter
exports an HttpOnly session cookie and Chromium reuses it in fresh contexts.
Live Concur SSO/MFA handoff did not yield a usable JWT. A controlled Safari 27.0
redirect test showed that MCP exposed the final response but omitted the redirect's
issuing `Set-Cookie` header. SSO flows that issue authentication cookies on redirects
may therefore require the Chromium fallback. Missing cookie scope and expiry are
never reconstructed from request headers. Portable tests cover MCP transport,
cross-process reuse, revocation, reauthentication, cancellation, and fallback;
these checks do not establish that Concur issues transferable cookies in Safari.

Run the portable tests (Safari is never launched):

```bash
.venv/bin/python -m unittest -v tests.test_safari_login
docker-compose build unit-tests
docker-compose run --rm unit-tests python -m unittest -v tests.test_safari_login tests.test_session_handoff_browser
```

On an authorized macOS host, opt in to the native test. It opens an owned Safari
fixture tab, exports a synthetic HttpOnly cookie, and verifies it in a separate
Chromium process with two fresh contexts. It never loads Concur credentials:

```bash
CCWORKS_TEST_NATIVE_SAFARI=1 .venv/bin/python -m unittest -v tests.test_native_safari_handoff
```
