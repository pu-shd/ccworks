# SAP Concur API & Browser Automation Test Suite

This project provides a unified Python integration client and test suite to verify connectivity and support programmatically creating draft expense reports in SAP Concur.

It supports two modes of interaction:
1. **API Integration (Direct)**: Uses SAP Concur REST APIs with OAuth 2.0 Client Credentials authentication (requires API permissions and administrative licensing).
2. **Browser Automation (Playwright)**: Automates a browser session to perform UI clicks (useful if your organization doesn't have Web Services API access or if direct API keys are unavailable).

---

## 🛠️ Prerequisites

### For API-Based Access
1. **Client Web Services License**: A valid license to enable API access.
2. **App Registration**: A registered application in the SAP Concur App Center to obtain a `Client ID` and `Client Secret`.
3. **Scopes**: Your application must have the `EXPRPT` (Expense Report) scope enabled.
4. **Target User Account**: A valid SAP Concur Login ID.

### For Browser-Based Automation
1. **Login Credentials**: Standard Concur username/password or SSO login.
2. **Playwright Setup**: Playwright must be installed locally along with chromium binaries (handled automatically by `./ccworks setup`).

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
| | `txn unallocate NAME IDX` | Clear every allocation on a transaction, returning it to the report's default. Use before `txn allocate` to replace a chartstring rather than split it. |
| | `txn attach-receipt NAME --merchant M --file PATH` | Attach a local receipt file to a transaction row. |
| **card** | `card list [--view F]` | List credit-card transactions. |
| | `card show MERCHANT_OR_ID [--view F]` | Details for one card transaction. |
| **receipt** | `receipt delete --all` | Delete every available receipt. `--all` is required. |
| **delegate** | `delegate add WHO [--can prepare submit approve]` | Add an expense delegate (default: `prepare`). |
| | `delegate remove WHO` | Remove an expense delegate. |
| **session** | `session login` | Headed browser for manual SSO; saves session state. |
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

Unmatched transactions are skipped with a warning on stderr — check those rather
than assuming full coverage. Merchant matching cannot distinguish rows that
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
13. **`concur_update_transaction(report_name, transaction_index, type, purpose, comment)`**: Updates fields (type, business purpose, comment) of a specific transaction inside an expense report.

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
4. Your authenticated session token, cookies, and local storage are saved into `concur_session.json`.
5. Subsequent automated actions will load this file and run headlessly without requiring login or prompt parameters.

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
