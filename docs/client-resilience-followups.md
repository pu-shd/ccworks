# Client resilience and event reporting follow-ups

Development candidate and live-validation policy:
[Live validation and release gate](live-validation-2026-12-01.md).
Development CI is separate from the live test and owner approval required before
the next version and Homebrew update can ship.

Observed during live reconciliation. The client now waits for actions on the
supported “Update Other Items” confirmations and accepts “Acknowledge” only for
the known message that previously modified allocations must be updated manually.
Browser regressions cover delayed actions and refusing an unknown acknowledgment.
The checklist below tracks the broader next-release work; the development
runbook distinguishes implemented candidate changes from remaining work and
required live validation:

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
