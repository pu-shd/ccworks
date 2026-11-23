# Commands and payloads

Examples use synthetic names and codes. Substitute the user's actual report,
fresh indices and requested values. Run commands through an argument-vector
execution API when available; never interpolate report names, paths or text
into shell syntax. If the host accepts only shell text, quote every argument
with its shell's proper escaping. Global flags precede the resource group.
In a desktop Work session, replace the `ccworks` shown below with the absolute
helper path from the skill and use `desktop check` for readiness.

## Inspect and authenticate

```zsh
ccworks --version
ccworks --output json session status
ccworks session login --browser chromium
ccworks --output json report list
ccworks --output json report show "Statement Report" --deep
ccworks --output json txn allocations "Statement Report"
```

The two `session` commands above are for developers. Keep the interactive
developer login process open until the user sees the dashboard and presses
Enter. Then verify with a separate `session status` invocation. Desktop users
complete sign-in or recovery in **ccworks Desktop** from Start or Finder. After
they see **Ready**, rerun `desktop check`. Sign-in confirmation does not authorize
report submission.

`report show` uses 1-based expense indices. `txn allocations` uses section numbers;
correlate using expense identity rather than assuming these numbers coincide.
On an expired noninteractive command, initiate login separately; after login,
read back before deciding whether to retry a write.

## Update type and both justification fields

```zsh
ccworks --output json txn update "Statement Report" 1 --type "Software (OIT use only)" --justification "Requested for research. Software used for research."
```

The full expense type is an example, not a universal classification. Use the
organization's valid option and the user's instructions.

For several fields or receipts, write a minimal private JSON file and use
`apply-json`. Do not feed an entire capture back unchanged, since unrequested
empty fields can clear data. This example deliberately leaves receipts and
allocations untouched:

```json
{
  "report_name": "Statement Report",
  "expenses": [
    {
      "index": 1,
      "vendor": "Example Software",
      "amount": "$12.34",
      "expense_type": "Software (OIT use only)",
      "business_purpose": "Requested for research. Software used for research.",
      "comment": "Requested for research. Software used for research."
    }
  ]
}
```

```zsh
ccworks --output json report apply-json "/private/local/reconciliation.json"
```

For a verified new attachment, add `receipt_file_path` to the identified expense
in that payload. The alternative below is only appropriate when the merchant
uniquely identifies the target; it must not replace receipt inspection:

```zsh
ccworks --output json txn attach-receipt "Statement Report" --merchant "Example Software" --file "/private/local/receipt.pdf"
```

## Allocate last

Use the same selection and codes for preflight and apply. These are synthetic
chartstrings, not suggested defaults:

```zsh
ccworks --output json txn allocate-bulk "Statement Report" --indices 1 3 --dept 12345 --fund A0001 --dry-run
ccworks --output json txn allocate-bulk "Statement Report" --indices 1 3 --dept 12345 --fund A0001
```

Use `--all` only for an explicit all-expenses request. Use `--prog` only when the
user requests a program change. The bulk path preserves other fields and skips
verified identical allocations; conflicting/split allocations require a separate
plan. After uncertain writes, check readback and `remaining_indices` before
resuming. Do not clear existing allocations as a routine preparatory step.

## Approved submission and independent status

After this report's reconciliation is verified and the user has authorized its
submission:

```zsh
ccworks --output json report submit "Statement Report"
ccworks --output json report list --historical --view "All Reports"
ccworks --output json report show "Statement Report" --view "All Reports"
```

Read the actual approval status, including when the submit command reports an
unknown result. A saved draft or “Not Paid” alone does not prove submission.
Do not submit again merely because the first confirmation was unreadable.
