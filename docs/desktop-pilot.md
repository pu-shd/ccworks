# ccworks Desktop pilot runbook

The workspace owner manages private plugin access and rollback. Endpoint IT owns browser validation, signing, catalog distribution, and removal. Record the person responsible for each role and the candidate plugin release ID and helper version before inviting users. Share [the setup guide](desktop-setup.md) with pilot members.

## Deployment proof

Use one ordinary member and one managed, standard-user device for each supported OS. Use **Only those invited** for the existing private workspace plugin. Confirm an uninvited ordinary member cannot open the plugin. Verify the invite and installation in the ChatGPT desktop UI and confirm that the member can start a local Work session. Verify each member sees only the reports available to that member's own Concur account. Record whether the workspace can install the plugin by default and how an admin removes a policy-installed plugin.

Have Work launch the packaged CLI at the catalog path with arguments `desktop check`. Separately verify that the user can reach the setup window in their desktop session. Try the managed Work launch path for the GUI. If that route is blocked, verify the Start or Finder route as the supported workflow. Record the actual browser version, OS, architecture, security controls and outcomes. Confirm that the managed browser policy permits both headless and headed Playwright launches. Ask IT to confirm catalog install, update and uninstall under the enrolled user's account.

Open the private link on the web and check a desktop thread without local execution. Record the actual UI and verify that neither context performs a Concur operation. ChatGPT controls the experience when it blocks invocation. [OpenAI desktop-only plugin documentation](https://developers.openai.com/plugins/deploy/desktop-only)

## Build candidates and release packages

Build on the target OS and architecture with Python 3.12. macOS builds use SwiftUI and Xcode tools. Windows builds use the Tk runtime included with the Windows Python installer. Runtime, build and Playwright driver versions are pinned in the project lockfiles. The helper uses installed Chrome through Playwright's `chrome` channel. It never downloads a browser. For a custom Chromium build, validate the exact approved build in the pilot and provision the same environment for the GUI and Work process.

```dotenv
CCWORKS_BROWSER=chromium
CCWORKS_CHROMIUM_EXECUTABLE=/absolute/path/to/approved/chromium
CCWORKS_CHROMIUM_VERSION=156.0.8078.4
```

Use the exact version reported by the validated executable. On Windows, use a fully qualified path to the approved `.exe`. Use the default configuration for Chrome. The packaged helper uses the standard state directory. Developer installs can use `CCWORKS_STATE_DIR` and default to Playwright Chromium.

- On macOS 13 or later run `desktop/build-macos.zsh`, `desktop/run-macos.zsh`, and `desktop/test-macos.zsh`. Set `CCWORKS_BUILD_PYTHON` when Python 3.12 is installed outside PATH. Build natively on Apple Silicon and separately on Intel if IT supports Intel. Catalog installs use `/Applications/ccworks Desktop.app`. Self-service uses `~/Applications/ccworks Desktop.app`.
- On Windows x64 run `desktop/build-windows.ps1 -BundleOnly` for an unsigned smoke-test bundle. The per-user install path is `%LOCALAPPDATA%\Programs\ccworks`. Run any silent uninstaller that purges data in the installed user's context with `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /PURGECCWORKSDATA`. Silent uninstall keeps state by default.

See [Desktop signing and updates](desktop-signing.md) for the macOS signing bootstrap, the optional Sparkle feed and Windows Authenticode signing.

The skills-only plugin and the helper ship independently. Plugin 0.2.x accepts helper protocol 1 and versions in the range `>=0.8.0,<1.0.0`. Keep protocol and behavior compatible throughout that range. The CLI named `ccworks` on macOS or `ccworks.exe` on Windows supports the ordinary command surface. The helper check emits one JSON object with `protocol`, `version`, `browser` and `sign_in`. Expect exit 0 only for a ready browser and ready sign-in. Exit 1 carries the same JSON schema and the reason category. The check leaves saved state untouched and opens no report.

## Acceptance before expanding access

Record **Pass**, **Fail** or **Pending**, with the platform/build and a brief description of what happened. Keep credentials, cookies, financial data, and receipt contents out of the evidence.

| Gate | Windows | macOS |
| --- | --- | --- |
| Invited member installs the private plugin and the catalog helper with standard-user permissions | Pending | Pending |
| Work launches the installed helper and the user opens the sign-in window | Pending | Pending |
| Approved browser opens in both modes. Missing, blocked and unsupported cases give the expected instructions | Pending | Pending |
| Sign-in completes with SSO and MFA. Cancellation and invalid candidates preserve a usable prior session | Pending | Pending |
| A read-only report inspection succeeds. Expired sign-in can be recovered from Start or Finder | Pending | Pending |
| An authorized test report's receipt attachment and allocations are saved and independently read back | Pending | Pending |
| Old helper, concurrent runs and interrupted writes are handled. Recovery starts with a fresh read | Pending | Pending |
| Upgrade and rollback preserve the session and maintain plugin compatibility | Pending | Pending |
| Standard-user uninstall and cleanup leave no session. Personal browser and files are intact | Pending | Pending |
| Web and desktop without local execution perform no Concur operation | Pending | Pending |

Test steps on each architecture IT approves. No pilot step should require Terminal, elevation, a Python install or a browser-driver install for the end user. Submission still requires explicit report-specific approval and a separate status read. Follow the existing live validation gate for release.

## Expand or roll back

After acceptance, the workspace owner can extend access to approved groups or the workspace directory and document admin removal. Keep the plugin private throughout the pilot. Endpoint IT retains the previous signed installer or app bundle in the catalog and tests rollback before broad deployment. Roll back the helper through the catalog. Restore a compatible prior plugin release through workspace controls or remove access if that release is unavailable. To verify cleanup, check that `concur_session.json` and owned temporary state are gone under `%LOCALAPPDATA%\ccworks` or `~/Library/Application Support/ccworks`. The zero-length lock rendezvous file can remain and contains no session data.

## Release assets

Use the same version and commit for the notarized Apple Silicon DMG, Authenticode-signed Windows installer and their SHA-256 checksums. Host private pilot builds in the organization’s software catalog or an approved internal download location. The GitHub tag workflow checks live approval and candidate identity. A public GitHub Release requires a separate owner-approved publication of the accepted artifacts. Keep that publication and the private ChatGPT plugin rollout independent.
