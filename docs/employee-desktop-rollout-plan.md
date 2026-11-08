# Employee desktop and ChatGPT rollout plan

Status: proposed implementation plan, November 7, 2026. This document authorizes
no deployment or report submission and makes no claim of Windows compatibility
or completed ChatGPT desktop validation.

## Outcome

Employees on macOS and Windows can reconcile their own Concur statements using
plain language and guided screens, without using Terminal, PowerShell, WSL,
Homebrew, Python installation, or editing configuration files.

Reuse the existing reconciliation engine and
[ccworks skill](../plugins/ccworks/skills/ccworks/SKILL.md). Provide a small local
desktop companion for installation/setup, sign-in, receipt selection, review,
progress, and recovery. ChatGPT supplies an optional conversational interface;
the companion must support the essential workflow independently.

Engineering priority remains macOS, Linux, web, WSL, native Windows, Android.
Preserve Linux support in the shared engine and container tests. Both macOS and
native Windows must meet employee pilot acceptance criteria; WSL is not an
employee prerequisite. Android delivery is outside this plan. Preserve 0.7.1's
Intel macOS support; subsequent macOS packaging targets Apple Silicon as already
agreed. Select supported Windows versions and architectures during discovery.

## Existing foundation and gaps

- The skill and portable plugin provide workflow instructions and CLI examples.
  They depend on a local executable and provide no remote Concur connector.
- Chromium is the supported authentication and automation path. Safari remains
  experimental and is not an employee installation dependency.
- The source launcher uses zsh. Session locking currently imports `fcntl`, and
  state paths lack a Windows-specific default. These require portability work.
- Mixed reconciliation does not yet provide complete durable automatic resume.
  Saved values must be read before an uncertain write is retried.
- Earlier measurements found a roughly 60-minute Concur JWT lifetime that
  persistence did not extend. Read actual remaining lifetime rather than assume
  every employee or tenant has that policy. Track remaining work in
  [Issue #3](https://github.com/pu-shd/ccworks/issues/3).

## Employee workflow

1. Install a signed package from the organization's software distribution portal
   or a guided installer. Setup checks browser availability and permissions.
2. Open the companion and sign in to Concur through headed Chromium using the
   employee's existing SSO/MFA. Verify reuse in a fresh browser context.
3. Select the statement and receipt folder using native file pickers. In
   ChatGPT, an employee can instead ask to reconcile a statement and provide
   allocation and justification instructions.
4. Review matched receipts, exact purpose/comment text, expense categories,
   chartstrings, preserved fields, and unresolved or ambiguous matches.
5. Approve the proposed edits. Show operation progress and verified results.
   Handle reauthentication with a clear browser sign-in action and resume only
   unfinished work after readback.
6. Present a separate submission action after reconciliation is verified.
   Require approval for this statement and independently verify submitted status.

## Phase 1: deployment and ChatGPT feasibility

Confirm whether employee accounts are individual Plus subscriptions or belong
to a managed Business, Edu, or Enterprise workspace. Validate current local Work,
skill/plugin installation, sharing, and administration capabilities on actual
macOS and Windows accounts. Do not assume that Plus grants managed distribution
or that browser chat can access local receipts and sessions.

Prototype the existing skill on each desktop platform: select it through the UI,
execute a synthetic inspection through the local client, and complete a mock
authentication handoff. Document the supported host/account combinations and
any workspace policy prerequisites.

Decide the companion UI framework after a small macOS/Windows prototype proves
file pickers, browser launching, progress delivery, packaging, accessibility and
the intended ChatGPT integration. Prefer a shared interface where practical;
use native platform adapters for installation, storage and authentication.

Decide whether local Work can safely invoke the packaged client directly or
whether a narrowly scoped local tool bridge is necessary. Define any bridge's
transport and authorization before building it. A skill alone does not provide
that connection. A hosted/web-only connector is a separate project requiring
its own per-user authentication, hosting, and Concur API feasibility assessment.

Exit evidence: platform/account compatibility matrix, a working synthetic demo
on both operating systems, and a short architecture decision record. If ChatGPT
integration is unavailable on one platform, record the limitation and validate
the companion's independent workflow before choosing pilot scope.

## Phase 2: portable local engine

Introduce platform adapters for state paths, locking, process management and
browser discovery. Use the Windows per-user application-data directory and
appropriate ACLs; preserve existing macOS/Linux paths and explicit overrides.
Replace Unix-only locking with a tested cross-platform implementation that
serializes concurrent authentication, state replacement and reconciliation.

Use atomic session-state replacement and protect the previous working session
when login fails. Evaluate native credential storage for encryption keys where
needed; never share session files between employees or send session exports to
ChatGPT. Define migration and recovery behavior for existing installations.

Expose structured operations for inspection, planning, applying approved edits,
progress, cancellation, verification and submission. Invoke the engine through
validated argument vectors or typed calls. Never turn receipt text, report names
or model output into arbitrary shell commands. Keep business logic shared across
the CLI, companion and any ChatGPT adapter.

Add durable operation identity and per-stage checkpoints for mixed changes.
After cancellation, expiry, process termination or an unreadable acknowledgment,
perform fresh readback before resuming. Preserve partial successes, skip verified
unchanged work, and prevent duplicate receipt uploads or allocation splits.
Bound operations and distinguish failed, saved, verified and unknown outcomes.

Exit evidence: passing portability, concurrency, interruption and recovery tests
on macOS, Linux and native Windows, using synthetic data only.

## Phase 3: companion and installation

Implement onboarding, receipt selection, proposed-change review, progress,
session expiry and actionable failure screens. Keep underlying commands and
technical identifiers in diagnostic views. Support keyboard navigation and
accessible labels. Clearly show which employee, report and installation are
active before account changes.

Package ccworks, its runtime and the matching Chromium dependencies. Verify
Playwright/browser redistribution and installation requirements before choosing
to bundle binaries or download verified versions during setup. Use locked
dependencies and fail visibly on incomplete or invalid setup.

Provide signed macOS installation with notarization and signed Windows
installation suitable for the organization's deployment tooling. Confirm
standard-user installation or document the managed-install requirement. Add
verified updates, rollback, repair, uninstall and private-data retention choices.
Use native Windows packaging tools; employee installation must not require WSL.

Provide macOS zsh-compatible build, run and test scripts; if the selected UI uses
Swift/SwiftUI, also provide the required notarization scripts and a Tahoe-style
icon filling the squircle. Pin any Docker images used for builds or tests and
keep build output, signing material and session data out of version control.

Exit evidence: repeatable clean installation, upgrade, rollback and uninstall
on supported macOS and Windows versions, including managed standard-user hosts.

## Phase 4: skill distribution and integration

Adapt the skill to guide employees through the companion's approved operations.
Preserve the current CLI path for technical users. Add account-appropriate GUI
installation instructions and conversational examples. Installation must not
silently switch an employee to an unvalidated development client.

Require each employee to authenticate individually. Keep local execution scoped
to the selected files and that employee's Concur session. If a tool bridge is
needed, expose specific validated operations, bind it to the intended local host,
authenticate callers, and prevent other local or web origins from invoking it.
Do not expose an unrestricted command executor.

Test explicit skill selection and ordinary task requests. Check that ambiguous
matches trigger clarification, receipt contents cannot authorize extra actions,
an approved plan cannot expand during execution, and an old statement's approval
does not authorize submission of a new statement.

Exit evidence: repeatable installation and complete synthetic workflows through
ChatGPT on both platforms, plus the same workflows through the companion alone.

## Testing and release gates

Write failing regressions before bug fixes. Keep full mock tests runnable with
`docker-compose`; include schema/input validation, plan identity, session expiry,
lost acknowledgments, allocation conflicts, duplicate receipts, concurrent
writers, cancellation and crash recovery. Every test run must report explicit
results and fail for missing or empty required test groups.

Run lint, format, type and unit checks in CI. Retain macOS and Linux coverage and
add native Windows tests for paths, locking, subprocesses, browser lifecycle and
GUI integration. Container tests complement native tests; Linux containers cannot
validate Windows/macOS installer behavior, ACLs, OS dialogs or signing. Use mocks
where enterprise deployment cannot be reproduced, and clearly label the limits.

Test paths with spaces and Unicode, restricted permissions, missing Chromium,
interrupted downloads, proxy/network failures, expired/revoked sessions, browser
crashes and unavailable ChatGPT integration. Exercise installer/update failure
and rollback on clean hosts, with sanitized diagnostic output.

Keep the existing [December live-validation gate](live-validation-2026-12-01.md)
and explicit release approval. Do not alter the pending attestation or equate
mock success with production validation. The owner-authorized merge of PR #11
did not authorize publishing a new release.

After that gate passes, pilot with a small group representing both operating
systems, including managed Windows and standard-user accounts. Obtain each
participant's actual instructions and approval for their own statement; verify
saved fields, receipts, allocations and submitted status independently. Do not
inject write failures into production. Keep detailed financial evidence private
and commit only redacted validation summaries.

General availability requires successful native installation and recovery tests,
successful supervised pilot workflows on both platforms, documented support
boundaries and an owner-approved release. Any unsupported ChatGPT combination
must have a clearly documented companion workflow.

## Delivery boundaries

Implement each phase in focused PRs with its own acceptance evidence. Preserve
existing CLI functionality and Linux container tests throughout. Monitor every
initiated GitHub workflow and address failures before marking a phase complete.
No hosted service, public plugin publication, employee rollout, live account
changes or new release is included in committing this plan.

Reference current platform guidance during feasibility testing:
[OpenAI skill format](https://learn.chatgpt.com/docs/build-skills),
[plugin installation and use](https://learn.chatgpt.com/docs/plugins), and
[plan coverage](https://learn.chatgpt.com/docs/pricing).
