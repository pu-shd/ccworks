# ccworks

A Python CLI and test suite for SAP Concur expense reports. It works in two modes:

1. **API**: Concur REST APIs with OAuth 2.0 client credentials. Requires a Web
   Services license, an App Center registration (client ID and secret) with the
   `EXPRPT` scope, and a Concur login ID.
2. **Browser**: Playwright Chromium automates the Concur web UI after you sign in
   with SSO/MFA. Use this when API access is unavailable.

For the managed Windows and macOS desktop pilot, see the
[desktop setup guide](docs/desktop-setup.md) and the
[IT pilot and packaging runbook](docs/desktop-pilot.md).

## Install

```bash
brew tap pu-orfe/tap && brew install ccworks          # macOS
```

The first browser command offers to download Playwright Chromium (~180 MB).
Session state is stored in `~/Library/Application Support/ccworks` (macOS) or
`$XDG_STATE_HOME/ccworks` (Linux); override with `CCWORKS_STATE_DIR`.

From source:

```bash
git clone https://github.com/pu-shd/ccworks.git && cd ccworks
./ccworks setup       # creates .venv, installs the package editable and Chromium
```

API mode reads OAuth credentials from `.env`; copy `.env.example` to start.

## Sign in

```bash
ccworks session login     # headed Chromium: complete SSO/MFA, then press ENTER
ccworks session status    # is the saved session still valid?
```

The new session is verified in two fresh headless contexts before it replaces
the saved one; a failed or cancelled login keeps the old session. Later commands
run headlessly. Concur sessions last about 60 minutes from login and are not
extended by use.

Chromium is the supported login browser. Safari login is incomplete and
experimental; see [Experimental Safari MCP login](docs/safari-login.md).

## Commands

Commands are grouped as `<group> <subcommand>`. An installed `ccworks` and the
checkout's `./ccworks` launcher accept the same arguments; the launcher tracks
your working tree. If a command is rejected with `invalid choice`, your installed
version is too old: check `ccworks -V` and run `brew upgrade ccworks`.

| Group | Command | Notes |
| :--- | :--- | :--- |
| **report** | `report list [--historical] [--view F]` | List draft or historical reports. |
| | `report show NAME [--deep] [--view F]` | Line items. `--deep` opens each transaction (slow, complete). |
| | `report create [--name N] [--purpose P] [--comment C] [--headed]` | Create a draft. |
| | `report update NAME [--name --purpose --comment --justification]` | Update header fields. |
| | `report reconcile NAME [--rules PATH] [--submit]` | Apply merchant rules; review-only unless `--submit`. |
| | `report submit NAME` | Submit for approval. |
| | `report delete NAME` / `report delete --all-drafts` | Delete one draft or all drafts. |
| | `report apply-json PATH` | Apply an edited `report show` JSON back to Concur. |
| **txn** | `txn update NAME IDX... [--type --purpose --comment --justification]` | Update transactions by 1-based index. |
| | `txn allocations NAME [--view F]` | List chartstring allocations. |
| | `txn allocate NAME IDX --dept D --fund F [--prog P]` | Add a chartstring. A second one splits the expense. |
| | `txn allocate-bulk NAME (--all / --indices N...) --dept D --fund F [--prog P] [--dry-run]` | Set one 100% allocation on many expenses. |
| | `txn unallocate NAME IDX` | Clear all allocations on a transaction. |
| | `txn attach-receipt NAME --merchant M --file PATH` | Attach a receipt file. |
| **card** | `card list [--view F]` / `card show MERCHANT_OR_ID` | Card transactions. |
| **receipt** | `receipt delete --all` | Delete every available receipt. |
| **delegate** | `delegate add WHO [--can prepare submit approve]` / `delegate remove WHO` | Manage expense delegates. |
| **session** | `session login` | Supported default: headed Chromium login for SSO/MFA. |
| | `session login --browser safari [--no-fallback]` | Incomplete, experimental Safari MCP login; falls back to Chromium. |
| | `session status` | Check the saved session. |
| **api** | `api test` | Run the API client tests (needs `.env`). |
| — | `nuke` | Delete all drafts **and** all receipts. |

Global flags: `-V/--version`, `-v/--verbose`, `--output {json,text}` (default
`json`). Run `ccworks <group> <sub> --help` for details.

- **stdout is data, stderr is diagnostics**, so `2>/dev/null | jq` is safe.
- Write commands exit `1` if any row fails. JSON reports `success`, `updated`,
  `skipped`, `failed` and per-row results; review partial batches before retrying.
- Concur's "expense has been saved" warning means a saved draft, not a failure:
  `success` can be `true` while `ready_to_submit` is `false`.

## Monthly reconciliation

Work in this order; each step avoids a problem the next would hit.

1. **Capture.** `ccworks report show "Statement Report 06/16 - 07/31" > report.json`.
   Check `extraction.complete` before trusting the data. Indices are 1-based and
   shared by `report show`, `txn update`, `txn allocate` and `report apply-json`
   (`txn allocations` uses `section_number` instead). They are positional, so
   re-capture before reusing them.
2. **Receipts, types and text.** Edit the JSON and apply it in one pass with
   `report apply-json`. Each row's `vendor` and `amount` guard against writing to
   the wrong expense. Omit a field to keep it; `""` clears it. Use full
   expense-type names; an `Undefined` type must be set in the same row as text.

   ```json
   { "report_name": "Statement Report 06/16 - 07/31",
     "expenses": [
       { "index": 1, "vendor": "ANTHROPIC* CLAUDE TEAM", "amount": "$500.00",
         "receipt_file_path": "/path/to/001.pdf", "business_purpose": "...",
         "comment": "..." } ] }
   ```

3. **Allocate last.** Editing an allocated expense adds a confirmation round trip
   to every later write. `txn allocate` adds a split; run `txn unallocate` first
   to replace a chartstring. For a common chartstring, use `txn allocate-bulk`
   with `--dry-run` first. It skips expenses already allocated correctly, stops
   on conflicts before writing, verifies in a fresh context, and is safe to
   rerun after an interruption. After a timeout, re-read allocations before
   retrying, since a retry can create duplicate splits.
4. **Verify and submit.** `report show NAME --deep` reads every field back;
   `report submit NAME` cannot be silently undone.

Budget the session: text and receipts take ~10 s per transaction, allocations
~100 s. A 16-row statement takes ~30 minutes with allocations. `apply-json`
refuses a payload that will not fit the remaining session and stops cleanly with
`remaining_indices` if time runs short (`--ignore-session-budget` overrides).

`report reconcile --rules rules.json` applies rules keyed by case-insensitive
merchant substring (first match wins, with `expense_type`, `business_purpose`,
`allocation_code` and `receipt_path`). Unmatched rows fail the command. Prefer
`apply-json` when a merchant repeats.

## AI agent integrations

- **Codex and ChatGPT skill.** [`plugins/ccworks/skills/ccworks`](plugins/ccworks/skills/ccworks/SKILL.md)
  guides receipt matching, justifications, bulk allocations and approved
  submission using your installed `ccworks`. It is an unpublished development
  package. Install it from this checkout with
  `codex plugin marketplace add .` and `codex plugin add ccworks@ccworks-local`,
  or through the ChatGPT desktop Plugins Directory. It needs local shell access,
  Chromium and a signed-in session.
- **Pi extension.** [Pi](https://pi.dev) loads `.pi/extensions/concur.ts`
  automatically in this repository (`/reload` to refresh). It wraps the CLI as
  `concur_*` tools for reports, card transactions, receipts, delegates,
  reconciliation and session checks. `concur_update_transaction` takes a
  0-based index.

## Development

| Command | Purpose |
| :--- | :--- |
| `./ccworks setup` | Create `.venv` and install Chromium. |
| `./ccworks test-local` | Mock unit tests in `.venv`. |
| `./ccworks test-docker` | Mock unit tests in Docker (no credentials). |
| `./ccworks test-browser-smoke` | Browser tests against the local mock server. |
| `./ccworks test-reports-live` / `test-receipts-live` | Smoke tests against your real Concur account. |

```bash
docker-compose build unit-tests extension-tests
docker-compose run --rm unit-tests          # full Python suite
docker-compose run --rm extension-tests     # Pi extension lint, types, tests
.venv/bin/ruff check src tests && .venv/bin/mypy
```

CI (`.github/workflows/ci.yml`) runs host unit tests, the container suite in four
shards (`python -m tests.run_shard --index N --count 4`), browser smoke tests,
Windows desktop builds and packaging checks on every push and pull request to
`main`. CI has no macOS runners: run `desktop/build-macos.zsh` and
`desktop/test-macos.zsh` locally before merging macOS desktop changes.

Releases require live validation and owner approval; see the
[release gate](docs/live-validation-2026-12-01.md) and the open
[resilience follow-ups](docs/client-resilience-followups.md).
