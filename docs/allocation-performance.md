# Allocation performance — work plan

Replacing an allocation is about 91% of the cost of a reconciliation pass. It is
why a teardown plus a re-apply cannot fit in one Concur session (#3). This plan
reduces that cost without weakening verification.

Nothing here is implemented. Refreshed November 2026 against the code at
`fd00ec5`; the original plan was written in August 2026.

## Scope

`txn allocate-bulk` (`src/ccworks/bulk_allocations.py`) now handles a common
chartstring across fresh expenses. It skips expenses that already match and
stops on any conflicting allocation, so it never replaces one.

This plan covers the remaining slow path: **replacing** an allocation on an
expense, which happens in:

- `report apply-json` rows with an `allocation` key, which clear and then add in
  one browser session (`apply_json_updates` in `src/ccworks/browser_client.py`);
- `txn unallocate` followed by `txn allocate`, two separate commands.

## Where the time goes

Measured against a live 16-row statement in August 2026, per transaction:

| Phase | Cost | Share |
| :--- | ---: | ---: |
| Business purpose, comment, receipt upload, save | ~10s | 9% |
| Allocation — clear, then add | ~103s | 91% |
| **Total** | **~113s** | |

A 16-row statement takes ~30 min with allocations and ~3 min without. The
allocation-only re-run measured 103 s per row across 15 consecutive rows. Allocation
code has changed since (`cbbbf0c`, `476839d`), so re-measure before starting.

The dominant cost is still **two full report reloads per row**, and the code
paths are unchanged:

- `_clear_allocations_on_page` verifies through `_verify_allocations_cleared`,
  which navigates to `/nui/expense`, waits for the dashboard, reopens the
  report and reopens the allocations modal.
- `_add_allocation_on_page` reopens the modal itself, then verifies through
  `_verify_allocation`, which does the same full reload again.

## Why it is built that way

`txn allocate` **adds** rather than replaces. Allocating an already-allocated
expense splits it: a live row ended up 50/50 across two chartstrings while both
writes reported success. The clear step's verification guarantees the old
chartstring is gone before the new one is added.

The final check is weaker than it looks. `_verify_allocation` passes when any
one allocation row contains each requested value as a substring. It does not
check the row count or the percentage, and fund `A0001` would also match
`A00012`. Only the clear step's separate check catches a leftover split.

Any change here changes verification on a financial write. That is the bar.

## Proposed work, in order

### 1. Replace in one modal session and verify once (largest win)

When a replacement runs as one operation:

1. open the allocations modal once;
2. replace the existing allocation. Prefer editing the existing 100% row in
   place, so an interruption cannot leave the expense unallocated. Whether
   Concur's current SAL dialog supports an in-place edit is not yet known and must
   be checked live. Otherwise remove and add in the same modal session;
3. save once, then verify once in a fresh read of the report.

The single verification must assert the end state with the bulk path's
`is_full_allocation` rule: **exactly one** allocation, at **100%** in percent
mode, containing every requested code as a whole code, plus any preserved
chartstring defaults. This is strictly stronger than today's final check.

Expected: about half the per-row cost (~103 s → ~55 s; a 16-row pass from
~30 min to ~16 min). Confirm live.

`txn unallocate` on its own keeps its independent verification; no later add
confirms its end state.

This item supersedes the "in-place, verified replacement" item in
[client resilience follow-ups](client-resilience-followups.md).

### 2. Reduce redundant row collection

`_collect_expense_rows` runs several times per row: after each per-row report
reload in `apply_json_updates`, inside `_open_allocations_modal`, and again in
each verification. Each call is a full DOM scan. Reuse the result within a row
while the page has demonstrably not navigated. The gain is small but compounds.

### 3. Verify allocations in one sweep per report

`apply-json` must reload the report before every row after the first. Closing
the detail pane leaves the previous row's receipt viewer in the DOM, so a stale
row would otherwise claim the wrong receipt. That reload stays.

Allocation verification can be amortised instead. `BulkAllocator._verify` already
re-reads every selected expense in one fresh browser context after the writes.
Apply the same pattern to `apply-json`: write allocations per row, then verify
them all in one sweep and report each row's result. Attempt this only after
item 1 is measured, and keep the identity guards and `remaining_indices`
checkpoints.

## Constraints

- **Verification cannot be weakened.** Assert the end state against a fresh read
  of what Concur holds, never the modal left on screen. After a full pass,
  `report show --deep` must still report `complete: true` and `failures: 0`.
- **Report partial failure loudly.** If the old allocation is removed but the new
  one fails, the expense is now unallocated, which is worse than before. Fail the
  row and say so.
- **Re-read before retrying.** A child Add Allocation save can persist before the
  parent dialog closes. After a timeout or interrupted dialog, read the
  allocations before retrying, or a retry can create a duplicate split.
- **The mock cannot validate the gain.** Its timings are unrelated to Concur's.
  Correctness is testable in the suite; speed only live.

## How to verify a change here

1. The suite passes in Docker (`docker-compose run --rm unit-tests`), including
   `tests/test_allocations_crud.py`, with new regressions for a leftover split,
   a non-100% allocation, a code that is a prefix of another, and a removal that
   succeeds while the add fails.
2. Compare one row live, before and after, using the `Updating Row N` timestamps
   in a `-v` log.
3. Run a full pass on a 16-row statement, then `report show --deep`. Assert
   `complete: true`, `failures: 0` and the expected chartstring on every row.
4. Update the per-row figures in `README.md` ("Monthly reconciliation") and in
   this plan if they change.

## Related

- Session lifetime (#3, open). The Concur JWT lasts ~60 minutes from login and is
  not extended by use: across a 24-minute run the session file's modification
  time advanced while the JWT expiry did not. Halving allocation cost is what
  would let a teardown and a re-apply fit in one session.
- [Client resilience follow-ups](client-resilience-followups.md): phase
  deadlines, checkpoints and structured events also apply to this work.
