# SAP Concur API & Browser Automation Test Suite

This project provides a unified Python integration client and test suite to verify connectivity and support programmatically creating draft expense reports in SAP Concur.

It supports two modes of interaction:
1. **API Integration (Direct)**: Uses SAP Concur REST APIs with OAuth 2.0 Client Credentials authentication (requires API permissions and administrative licensing).
2. **Browser Automation (Playwright Chromium)**: Automates a browser session to perform UI clicks (useful if your organization doesn't have Web Services API access or if direct API keys are unavailable). Chromium is the supported default for manual authentication, saved-session verification, and headless operations. Safari MCP login is an incomplete, experimental development option.

---

## 🛠️ Prerequisites

### For API-Based Access
1. **Client Web Services License**: A valid license to enable API access.
2. **App Registration**: A registered application in the SAP Concur App Center to obtain a `Client ID` and `Client Secret`.
3. **Scopes**: Your application must have the `EXPRPT` (Expense Report) scope enabled.
4. **Target User Account**: A valid SAP Concur Login ID.

### For Browser-Based Automation
1. **Login Credentials**: Standard Concur username/password or SSO login.
2. **Playwright Setup**: Playwright must be installed locally along with Chromium binaries (handled automatically by `./ccworks setup`). Chromium is still required when experimenting with Safari login because session verification and subsequent operations use Playwright Chromium.

---

## 🚀 Getting Started

### Install with Homebrew (recommended on macOS)

```bash
brew tap pu-orfe/tap
brew install ccworks
```

This gives you a `ccworks` command anywhere on your PATH. The first time you
run a browser-based command (e.g. `ccworks session login`), ccworks will prompt to
download Playwright's chromium browser (~180 MB) into
`~/Library/Caches/ms-playwright` — a one-time step.

Session state (login cookies, screenshots) is written to
`~/Library/Application Support/ccworks` (macOS) or
`$XDG_STATE_HOME/ccworks` (Linux). Override with `CCWORKS_STATE_DIR=/some/path`.

### Install from source (developers)

```bash
git clone https://github.com/pu-shd/ccworks.git
cd ccworks
./ccworks setup       # creates .venv, `pip install -e .`, installs chromium
```

### One command surface

`ccworks` exposes a single set of commands, grouped by resource as
`<group> <subcommand>` — the convention used by `gh`, `kubectl`, and modern
`docker`:

```bash
ccworks report list
ccworks report show "Q1 Travel" --deep
ccworks card show "Office Depot"
ccworks session status
```

The two entry points accept identical arguments, **provided the installed
package is new enough to have this surface**:

| | `ccworks <group> <sub>` | `./ccworks <group> <sub>` |
| :--- | :--- | :--- |
| Source | Entry point from Homebrew / `pip install` | The zsh launcher in a repo checkout |
| Behaviour | The CLI itself | Manages `.venv`, then forwards **verbatim** to the CLI |
| Tracks | The released version you installed | Your working tree, always current |

An installation predating the noun-verb surface rejects these commands with
`invalid choice: 'report'` and lists the old flat names instead. That is a stale
binary, not a bug — check with `ccworks --help`, and either upgrade
(`brew upgrade ccworks`) or use `./ccworks` from the checkout, which always
reflects your working tree.

The launcher owns only the checkout-only chores that cannot work from an
installed package — `setup`, `test-local`, `test-docker`,
`test-browser-smoke`, `test-reports-live`, `test-receipts-live`. Everything else
it passes straight through, so the two forms behave identically.

Run `ccworks` for the grouped reference, or `ccworks <group> <sub> --help` for a
command's flags.

## 📂 Command Reference

### Dev-checkout tasks (`./ccworks` only)

| Command | Scope / Notes |
| :--- | :--- |
| `./ccworks setup` | Create `.venv`, install the package editable, install chromium. |
| `./ccworks test-local` | Run mock unit tests locally using `.venv`. |
| `./ccworks test-docker` | Run mock unit tests in Docker (offline, no credentials needed). |
| `./ccworks test-browser-smoke` | Playwright browser CRUD smoke tests against the local mock server. |
| `./ccworks test-reports-live` | Playwright reports CRUD smoke test against your real Concur account. |
| `./ccworks test-receipts-live` | Playwright receipts smoke test against your real Concur account. |

### Commands

| Group | Command | Scope / Notes |
| :--- | :--- | :--- |
| **report** | `report list [--historical] [--view F]` | List draft reports, or historical ones with `--historical`. |
| | `report show NAME [--deep] [--view F]` | Line-item details. `--deep` opens each transaction (slower, fully accurate). |
| | `report create [--name N] [--purpose P] [--comment C] [--headed]` | Create a draft report. |
| | `report update NAME [--name --purpose --comment --justification]` | Update header fields. |
| | `report reconcile NAME [--rules PATH] [--submit]` | Reconcile transactions; review-only unless `--submit`. |
| | `report submit NAME` | Submit for approval. |
| | `report delete NAME` / `report delete --all-drafts` | Delete one report, or every draft. |
| | `report apply-json PATH` | Apply an edited `report show` JSON back to Concur. |
| **txn** | `txn update NAME IDX... [--type --purpose --comment --justification]` | Update one or more transactions by 1-based index. |
| | `txn allocations NAME [--view F]` | List chartstring allocations (Dept, Fund, Program). |
| | `txn allocate NAME IDX --dept D --fund F [--prog P]` | Add a chartstring to a transaction. Adds; it does not replace — a second allocation splits the expense by percentage. |
| | `txn allocate-bulk NAME (--all / --indices N...) --dept D --fund F [--prog P] [--dry-run]` | Set a common 100% allocation with native bulk selection; skip verified matches. |
| | `txn unallocate NAME IDX` | Clear every allocation on a transaction, returning it to the report's default. Use before `txn allocate` to replace a chartstring rather than split it. |
| | `txn attach-receipt NAME --merchant M --file PATH` | Attach a local receipt file to a transaction row. |
| **card** | `card list [--view F]` | List credit-card transactions. |
| | `card show MERCHANT_OR_ID [--view F]` | Details for one card transaction. |
| **receipt** | `receipt delete --all` | Delete every available receipt. `--all` is required. |
| **delegate** | `delegate add WHO [--can prepare submit approve]` | Add an expense delegate (default: `prepare`). |
| | `delegate remove WHO` | Remove an expense delegate. |
| **session** | `session login` | Supported default: headed Chromium for manual SSO; verifies and saves session state. |
| | `session login --browser safari [--no-fallback]` | Incomplete, experimental Safari MCP login for development; falls back to Chromium unless disabled. |
| | `session status` | Check whether the saved session is still valid. |
| **api** | `api test` | Run the API client test suite (needs `.env` OAuth creds). |
| — | `nuke` | Delete ALL draft reports **and** all available receipts. |

Global flags work anywhere in the argument list: `-V/--version`,
`-v/--verbose` (logs to stderr), `--output {json,text}` (default `json`).

`-V` reports the version of the entry point you invoked, which is worth checking
first when a command is rejected as unrecognized — `./ccworks` tracks your
working tree while an installed `ccworks` tracks whatever release you installed:

```console
$ ccworks --version
ccworks 0.3.3
```

**stdout is data, stderr is diagnostics.** Query commands print JSON on stdout
while logs, spinners, and session warnings go to stderr, so `2>/dev/null` is
safe when piping to `jq`.

Write commands exit `1` when an operation or any requested row fails. JSON
includes the overall `success` flag and individual row results; text output
also shows failure details. A partially successful batch must be reviewed
before retrying.

Type-only `txn update` and `report apply-json` batches skip expenses whose type
already matches exactly. Changed rows reuse the report page after checking the
editor's vendor, amount, and date; an unverified editor or indistinguishable
duplicate expenses trigger a fresh navigation. A single final report reload
verifies saved types. Receipt, text, and allocation updates retain their existing
navigation and verification paths.

Results include `updated`, `skipped`, and `failed` counts, per-row `saved` and
`verified` flags for type-only batches, and submission `warnings`. Concur's
“expense has been saved” warning is a saved draft, not a failed write:
`success` can be `true` while `ready_to_submit` is `false`. A `null` readiness
means readiness was not determined. A repeated type-only batch performs no
saves when every type already matches.

---

## 🔍 Monthly reconciliation workflow

A statement period is reconciled in the order below. The order matters: each
step avoids a trap the next one would otherwise hit.

**1. Capture the report and check the capture.**

```bash
ccworks report list
ccworks report show "Statement Report 06/16 - 07/31" > report.json
```

Read `extraction.complete` before trusting anything downstream. `false` means
rows were skipped or a detail pane never opened, and the reasons are listed in
`extraction`. Byte-identical line items are **kept** — two shipments the same
day for the same amount are two expenses, not a duplicate.

Indices are **1-based and dense**, and the same number addresses the same
expense in `report show`, `txn update`, `txn allocate`, and `report apply-json`.
They are positional, so re-capture before acting on numbers from an earlier run.
(`txn allocations` is the exception: it reports `section_number`, not the shared
index. Correlate on date + amount.)

**2. Attach receipts.** One `apply-json` pass, one browser session:

```json
{ "report_name": "Statement Report 06/16 - 07/31",
  "expenses": [
    { "index": 1, "vendor": "ANTHROPIC* CLAUDE TEAM", "amount": "$500.00",
      "receipt_file_path": "/path/to/001.pdf" }
  ] }
```

`vendor` and `amount` are the identity guard: `apply-json` refuses a row whose
amount and vendor do not match what you described, rather than writing to a
different expense. Omit a field to leave it alone; pass `""` to clear it.

An `allocation` object must include nonempty string `dept` and `fund` values,
with an optional `prog`. Use `allocation: {}` or `allocation: null` to clear
allocations. Malformed allocation objects are rejected before any updates are applied.

**3. Correct expense types where they are wrong.** Usually only a few rows.
Supply the full option text — a bare prefix that matches more than one option is
refused rather than guessed. An expense typed `Undefined` cannot be saved at
all, so set its type in the same row as its text or neither will persist.

**4. Fill Business Purpose and Comment for every transaction.** Batch all of
them into one `apply-json` payload; `--justification` on `txn update` sets both
fields to the same text for a single row.

**5. Allocate chartstrings last.**

```bash
ccworks txn allocate "Statement Report 06/16 - 07/31" 6 \
    --dept 25604 --fund A0002 --prog FC631
ccworks txn unallocate "Statement Report 06/16 - 07/31" 6   # clear, to replace
```

`txn allocate` **adds**; it does not replace. A second allocation splits the
expense by percentage rather than superseding the first, and both writes report
success. Clear first with `txn unallocate` to replace a chartstring.

Allocate last because editing any field on an allocated expense makes Concur ask
"Update Other Items?" before it will commit — ccworks answers it, but every
later write then pays for the extra round trip.

For a common chartstring across several expenses, use the native bulk path:

```bash
ccworks txn allocate-bulk "Statement Report" --all --dept 25605 --fund A0001 --dry-run
ccworks txn allocate-bulk "Statement Report" --all --dept 25605 --fund A0001
ccworks txn allocate-bulk "Statement Report" --indices 1 3 6 --dept 25605 --fund A0001
```

An explicit `--all` or `--indices` selection is required. `--dry-run` reads and
validates the selection without saving. Codes must be bare codes, such as
`25605` and `A0001`. The native SAL allocation dialog must be in Percent mode.
The per-transaction commands remain available for the legacy UI.

The command checks every selected expense before writing. Existing matching
100% allocations are skipped; conflicting allocations and splits stop the whole
preflight without changes. It preserves unrequested chartstring fields, groups
expenses by those fields, and applies one common allocation per compatible
group. Omit `--prog` to retain each expense's inherited program.

A fresh browser context verifies saved allocations, percentages, and unchanged
expense rows. Failed or uncertain writes return a nonzero exit status unless
readback proves every selected allocation saved correctly. JSON includes
`updated`, `skipped`, `results`, and `remaining_indices` (all reported indices
are 1-based). Rerun the same command after reauthentication or interruption:
it reads state first and skips completed rows, avoiding duplicate splits.

A live six-expense run confirmed that the child Add Allocation Save can persist
changes before the parent dialog closes. Canceling the parent does not establish
that the change was rolled back. After a timeout or interrupted dialog, reload
and read each expense before retrying; adding again can create duplicate splits.
Allocation readback supports both legacy and SAL controls, waits for data or an
explicit empty state, and reads visible cells in `display: contents` grid rows.

### How long a pass takes

Measured against a live 16-row statement, per transaction:

| Work | Cost |
|---|---|
| Business purpose, comment, receipt, save | ~10s |
| Allocation (clear then add, each verified) | ~103s |
| **Full treatment** | **~113s** |

So a 16-row statement is roughly **30 minutes** for everything, or about **3
minutes** if you are only writing text and receipts. Allocation dominates
because clearing and adding each verify against a fresh reload of the report.

That matters because Concur's session lasts about 60 minutes from login and is
**not** extended by use. One full pass fits; a teardown *and* a re-apply does
not. `report apply-json` estimates the payload against `session status`'s
`expires_in_minutes` and refuses up front rather than dying partway, and stops
cleanly with `remaining_indices` if the session runs short mid-run. Pass
`--ignore-session-budget` to override.

**6. Verify, then submit.**

```bash
ccworks report show "Statement Report 06/16 - 07/31" --deep
ccworks report submit "Statement Report 06/16 - 07/31"
```

`--deep` opens every transaction, so it is the only way to read business
purpose, comment and type back — and it is slow. Submission is outward-facing
and not silently undoable.

### Reconcile rules (optional)

`report reconcile` is review-only unless given `--submit`. Rules are keyed by
**merchant substring**, matched case-insensitively, first match wins:

```json
{ "United Airlines": {
    "expense_type": "Airfare",
    "business_purpose": "INFORMS 2026 travel",
    "allocation_code": "(25605) ORF-Technical Support",
    "receipt_path": "/Users/you/receipts/united.pdf" } }
```

Unmatched transactions and failed writes are included in the results, fail the
command, and prevent submission. Merchant matching cannot distinguish rows that
share a vendor, so prefer `apply-json` with explicit indices where a statement
repeats a merchant.

### Other commands

```bash
ccworks card list                       # corporate card transactions
ccworks card show "Office Depot"
ccworks delegate add "John Doe" --can prepare submit
ccworks report delete --all-drafts      # destructive
```

## 🤖 Integration with Pi Coding Agent (pi.dev)

This project includes a project-local extension for the **Pi** coding agent (an open-source terminal-based AI coding assistant at [pi.dev](https://pi.dev)).

The extension is written in TypeScript and is saved at `.pi/extensions/concur.ts`. It registers custom tools that allow the Pi agent to interact directly with your SAP Concur session.

### Registered Tools

1. **`concur_list_reports(filter_view, is_old)`**: Queries and lists active or historical expense reports.
2. **`concur_report_details(report_name, filter_view)`**: Fetches line-item details of a report.
3. **`concur_list_card_transactions(filter_view)`**: Lists card transactions from Available Expenses.
4. **`concur_reconcile_report(report_name, rules, submit)`**: Automatically reconciles transactions using JSON rules, and optionally submits the report (default: `submit` is false, leaving it in draft mode for review).
5. **`concur_attach_receipt(report_name, merchant, receipt_path)`**: Uploads and attaches a local receipt file to an expense.
6. **`concur_create_report(name, purpose, comment)`**: Creates a new draft expense report headlessly.
7. **`concur_delete_report(report_name)`**: Deletes a draft expense report by name.
8. **`concur_card_transaction_details(merchant_or_id, filter_view)`**: Fetches details of a specific credit card transaction by merchant or ID.
9. **`concur_add_delegate(name_or_email, permissions)`**: Adds a new expense delegate in settings with specified permissions.
10. **`concur_remove_delegate(name_or_email)`**: Removes an expense delegate from settings by name or email.
11. **`concur_nuke_drafts_and_receipts()`**: Deletes all draft reports and available receipts inside Concur (intended for testing cleanup).
12. **`concur_check_session()`**: Checks whether the currently saved browser session state is active and valid (returns true if authenticated, false if expired or missing).
13. **`concur_update_transaction(report_name, transaction_index, type, purpose, comment)`**: Updates fields (type, business purpose, comment) of a specific transaction using a zero-based index. The extension converts it to the CLI's one-based index.

### Development checks

Run the browser and unit suite in Docker, and run extension lint, format, type,
and argument-forwarding checks in its own container:

```bash
docker-compose build unit-tests extension-tests
docker-compose run --rm unit-tests
docker-compose run --rm extension-tests
```

For local Python checks, install `requirements-dev.lock` into `.venv`, then run:

```bash
.venv/bin/ruff check src tests
.venv/bin/mypy
.venv/bin/ruff format --check \
  tests/test_write_failures.py tests/test_extension.py tests/test_type_updates.py \
  src/ccworks/safari_login.py tests/test_safari_login.py \
  tests/test_session_handoff_browser.py tests/test_native_safari_handoff.py tests/test_allocation_resilience.py src/ccworks/bulk_allocations.py tests/test_bulk_allocations.py tests/test_bulk_allocation_cli.py
```

Use `npm ci`, `npm run check`, and `npm test` for local extension checks.

### How to Enable

If you use Pi within this repository, it will automatically discover the extension located in the `.pi/extensions/` folder. You can also manually load it or reload your active session by running `/reload` inside the Pi terminal client.

> **Note:** `concur_check_session()` returns exit code **0** (authenticated) or **2** (invalid/expired session). It catches a non-zero exit and reports `false` instead of raising.

---

## 🔮 Recommended Future Features & Integrations

1. ~~Receipt-to-Report Attachment~~ *(Already implemented — see `txn attach-receipt` / `concur_attach_receipt`).*
2. **Expense Itemization Automation:**
   * *Description:* Parse lodging/hotel folios or receipt text (using OCR/LLM) and programmatically itemize room rates, room taxes, parking, and meals.
   * *Value:* Eliminates tedious manual breakdowns of hotel checkout bills.
3. **Approval workflows for Managers:**
   * *Description:* Scan pending approval reports, display total summaries, and click approve or send back to employees with custom comments.
   * *Value:* Streamlines managers' review process via CLI/Slack commands.
4. **Export to ERP/Accounting Formats:**
   * *Description:* Export queried reports and transactions directly to CSV, JSON, or standard ERP formats (SAP, NetSuite, QuickBooks).
   * *Value:* Syncs Concur expense data directly into business accounting books.

---

### Client resilience and event reporting follow-ups

Observed during live reconciliation. The client now waits for actions on the
supported “Update Other Items” confirmations and accepts “Acknowledge” only for
the known message that previously modified allocations must be updated manually.
Browser regressions cover delayed actions and refusing an unknown acknowledgment.
The broader follow-up work remains:

- Extend confirmation coverage to partially hydrated dialog bodies and nested
  dialog footers. Keep supported actions scoped to the active confirmation and
  refuse ambiguous choices.
- Support the native `close-full-screen-panel` control and verify that the
  editor actually closes. Wait for the selected expense's Edit action and
  identity fields to hydrate before deciding it cannot open. Keep inspection
  read-only.
- Wait for the requested report card's name, not just the first dashboard
  placeholder. Wait for allocation data and its toolbar controls together;
  an empty grid can appear before its Add button. When removing the last
  allocation closes the modal automatically, read persisted allocations before
  reporting failure or retrying removal.
- Handle late TrustArc cookie overlays through their normal close controls,
  without changing consent preferences or removing unrelated dialogs.
- Track save attempts, acknowledgements, persisted state, and independent
  verification separately. Read saved values before retrying an interrupted or
  unrecognized save; retain identity guards and resumable checkpoints. Preserve
  partial completion when fields and a receipt save but allocation replacement
  fails; resume only the unfinished stage after reading it back.
- Prefer an in-place, verified replacement of an existing 100% allocation so an
  interruption cannot leave a gap between clearing and adding. For bulk
  replacements, group only expenses with the same requested codes and preserved
  chartstring defaults, and checkpoint each verified group.
- Emit structured, redacted events for operation, phase, expense index, retry,
  elapsed time, overlay/confirmation handling, session budget, and outcome.
  Distinguish saved-with-warning, unknown, failed, and verified outcomes. Do not
  log credentials, tokens, expense text, personal data, or receipt contents.
- Bound every phase with a deadline and report stalled browser/renderer calls.
  Restart read-only verification in a fresh context from its last completed
  checkpoint. After an interrupted write, establish persisted state before
  deciding whether a retry is safe; refresh authentication before the remaining
  work exceeds the session budget.
- Add mock-browser regressions for delayed confirmation buttons, alternate
  action labels, native close controls, late cookie overlays, stale receipt
  viewers, delayed report-card and allocation-toolbar hydration, automatic
  allocation-modal closure, interrupted acknowledgements, and expiry during a
  batch. Run these through the existing Docker test suite and make failures
  explicit.

---

## ⚙️ CI/CD Pipeline

This project includes a fully automated **GitHub Actions CI/CD Pipeline** defined in `.github/workflows/ci.yml`. On every push and pull request to the `main` branch, it runs:

1. **Host-Based Unit Tests**: Runs mock API tests directly on the runner.
2. **Containerized Unit Tests**: Builds and executes mock unit tests inside a Docker container using `docker-compose`.
3. **End-to-End Browser Smoke & Regression Tests**: Launches a stateful mock server and runs headless Playwright tests, including full CRUD and justification/classification regression suites.

---

## 🔒 Handling Multi-Factor Authentication (MFA) & SSO in Browser Mode

Modern enterprise security often requires MFA or SSO login screens that standard automation cannot programmatically bypass. This project handles this using a **Session State Preservation** strategy:

1. Run the manual session setup:
   ```bash
   ./ccworks session login
   ```
2. A headed Chromium window will open. Enter your email/password, solve SSO if prompted, and complete the MFA authentication.
3. Once logged in and redirected to the SAP Concur dashboard page, return to your terminal and press **ENTER**.
4. The candidate session is checked in two fresh headless Chromium contexts before it atomically replaces your saved session. On macOS the default location is `~/Library/Application Support/ccworks/concur_session.json`. Failed or cancelled login attempts preserve the existing saved session.
5. Subsequent automated actions will load this file and run headlessly without requiring login or prompt parameters.

**Chromium is the supported default for login and reauthentication.**
Its live Concur login, headless authentication, and saved-session reuse from a
separate process have passed validation. Keep `CCWORKS_LOGIN_BROWSER=chromium`
for routine use.

### Experimental Safari MCP login (incomplete)

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

---

## 📂 Project Directory Structure

```
├── .env.example                          # Environment variables configuration template
├── .pi/
│   └── extensions/
│       └── concur.ts                     # Pi coding agent extension (13 tools)
├── Dockerfile                            # Docker container definition
├── docker-compose.yml                    # Service orchestration for testing
├── ccworks                                   # Zsh shell helper script (CLI entry point)
├── requirements.txt                      # Third-party Python dependencies
├── src/
│   ├── __init__.py
│   ├── browser_client.py                 # Playwright Browser Automation Client
│   ├── client.py                         # SAP Concur REST API integration (OAuth2)    
│   └── cli.py                            # Argument parsing, signal handling, command routing
├── tests/
│   ├── __init__.py
│   ├── mock_concur_server.py             # Stateful local mock SAP Concur Server
│   ├── smoke_test_reports.py             # Live reports CRUD smoke test (Playwright)
│   ├── smoke_test_receipts.py            # Live receipts list/delete smoke test
│   ├── test_allocations_crud.py          # Allocations read/write regression tests
│   ├── test_browser_smoke.py             # E2E local browser smoke tests against mock server
│   ├── test_client.py                    # Unit tests using requests mocks
│   ├── test_justification.py             # Justification & classification regression tests
│   └── test_transaction_fields_crud.py   # Transaction field (type/purpose/comment) CRUD tests
└── .github/
    └── workflows/
        └── ci.yml                        # GitHub Actions CI/CD workflow configuration
```
