# Development candidate and live release gate

The next report is expected on **December 1, 2026**. Keep the candidate on
`dev/reliability` until that report has been reconciled and submitted successfully
with the development client, its saved state independently verified, and the
owner has approved a release. CI success alone is not production validation.

The installed Homebrew release and `main` remain at 0.7.1 during development.
Do not create a new version tag, publish a release, merge this candidate to
`main`, or update the Homebrew formula before the gate passes. Do not create a
fabricated production report or modify the already submitted November report.

## Scope and evidence

The first candidate implements the observed submission and recovery failures:

- Handle Submit Report, the native report-summary **Proceed**, and the
  certification **Accept & Submit** as distinct stages. Keep the older two-stage
  flow covered as well.
- Wait for named report cards, status labels, and enabled confirmation controls.
- Read status in a separate browser context after a submission attempt. Treat a
  closed dialog without persisted submission as a failure, and an unreadable
  status as unknown. A repeated invocation checks saved status before acting.
- Write private, atomic submission checkpoints and emit allowlisted events.
  Legacy browser diagnostics exclude expense text and exception tracebacks.
- Use the native editor-close control and recognized onboarding/cookie close
  controls. Preserve unknown business dialogs and consent preferences.

Remaining changes stay separate so they can have their own failing regressions,
focused commits, and live acceptance criteria:

- Durable field/receipt/allocation checkpoints for mixed reconciliation, with
  readback before resuming an uncertain write.
- In-place replacement of an existing 100% allocation, preserving unrequested
  chartstring fields. Keep split/ambiguous allocations protected.
- Skip verified unchanged fields and receipts; reduce report reloads only after
  editor and receipt ownership are proven. Retain the fresh-navigation fallback.
- Extend structured events and operation budgets to every reconciliation phase,
  including a process watchdog for unresponsive renderer calls.

Do not describe those remaining changes as implemented or live validated. Freeze
the complete intended candidate before the December test; changes to code,
dependency pins, or runtime configuration afterward require revalidation.

## Before December 1

1. Commit each focused change to the development branch. Reproduce bugs in tests
   before fixing them. Use synthetic reports and mock browser fixtures only.
2. Run lint, format and type checks, unit tests, the container suite with
   `docker-compose`, and browser regressions. The fixtures must cover delayed
   controls, dropped saves/submissions, lost acknowledgments, and repeat runs.
3. Keep a draft PR for review. Green checks mean eligible for live testing, not
   eligible for release. Keep `release/live-validation.json` in `pending` state.
4. Use the existing pinned dependencies. No tag or Homebrew release is needed
   to test a development checkout.

## December 1 live validation

This is a supervised test with the real report and the owner's actual receipt,
allocation, and justification instructions. It is not a scheduled unattended
submission. The November submission approval does not authorize December's
report submission.

1. Use the development checkout's own `.venv`; keep the installed Homebrew
   command available as a fallback. Install the locked dependencies into that
   environment and invoke its explicit executable path:

   ```zsh
   git switch dev/reliability
   git status --short
   git rev-parse HEAD
   .venv/bin/python -m pip install -r requirements-dev.lock -e .
   .venv/bin/ccworks --version
   ```

   Record the exact clean commit SHA and executable path in private validation
   evidence. The development package can still report 0.7.1; the commit SHA
   identifies the candidate. Do not test one commit and ship another code tree.

2. Check session expiry. If needed, use the development client's Chromium login
   and have the owner complete authentication. Verify saved-session reuse in a
   fresh headless context before any expense writes.
3. Capture a private baseline. Match expenses uniquely by merchant, amount and
   date. Check receipt contents, requested chartstrings and fields. Review the
   dry-run plan where supported; preserve every unmentioned field and expense.
4. Apply the owner's requested reconciliation. Checkpoint each phase. After any
   ambiguous acknowledgment, read saved state before deciding what remains.
   Never test write failures by deliberately interrupting production mutations.
5. Independently read back every requested field, receipt and 100% allocation.
   Repeating the same plan must skip completed work without duplicate writes,
   replacement receipt uploads or extra allocations. Exercise fault injection
   in mocks, not against the real report.
6. Obtain the owner's approval for this report before invoking `report submit`.
   Observe all supported stages, including Proceed and Accept & Submit. Read
   the report's actual approval status in a fresh browser context. Modal closure
   or a successful click alone does not pass this check.
7. Check events and failures for sensitive data. Keep raw report/receipt evidence
   in private local storage, outside git and public CI/PR logs. Record only the
   redacted checklist and exact candidate commit in the release attestation.

## Promotion after the live test

1. All relevant checks must pass against the same candidate. If a fix is needed,
   return to the development branch, reproduce it in mocks, run CI, and repeat
   the affected live validation before promotion. Use the stable client to
   complete financial work if necessary; that does not validate the candidate.
2. With explicit owner approval, fill `release/live-validation.json` with the
   tested commit, timezone-aware execution timestamp, passed checks and approval.
   Do not include names, report IDs, amounts, receipt paths, cookies or tokens.
3. Commit the attestation and version-only promotion. The gate rejects code,
   dependency, or other packaging changes after the tested commit. Run:

   ```zsh
   .venv/bin/python -m ccworks.release_validation --require-live
   ```

4. Merge the validated candidate with a merge commit or fast-forward that
   preserves the tested commit's ancestry; do not squash or rebase it during
   promotion. Run final CI, and only then tag/publish the new
   version. The tag workflow verifies the attestation and package-version match.
   This repository has no automatic publishing job; maintainers must also run
   the gate before manually creating a tag or GitHub release.
5. For the first release after 0.7.1, remove Intel **macOS** support from the
   Homebrew packaging/CI in a separate reviewed change. Preserve Apple Silicon
   macOS and Linux support. Build/test the supported bottles, publish the formula,
   and verify the installed release locally. Version 0.7.1 retains its Intel
   bottle and support policy.
