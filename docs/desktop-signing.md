# Desktop signing and updates

## macOS signing on a build Mac

Run `desktop/sign-macos.zsh` to build and sign the app with the Developer ID Application identity already available in Keychain. The script selects it automatically when exactly one is available. Set `CCWORKS_MAC_SIGN_IDENTITY` to the approved identity name or fingerprint when you need a specific identity. The script verifies the app signature after signing the embedded code. `--dry-run` prints the action without signing.

Apple notarization uses separate credentials. On the build Mac, run `xcrun notarytool store-credentials ccworks` and follow the secure prompts to register an approved Apple ID with an app-specific password or an App Store Connect API key. For a different Keychain profile, set `CCWORKS_NOTARY_PROFILE`. Run `desktop/notarize-macos.zsh` to sign the disk image, submit it, staple the ticket and verify Gatekeeper acceptance. The build and signing scripts keep credentials in Keychain. [Apple notarization workflow](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow)

## Sparkle for macOS self-service

Catalog builds use IT's update channel. For a GitHub self-service build, set `CCWORKS_SPARKLE_PUBLIC_KEY` to the macOS Ed25519 public key before signing. The build derives the GitHub Release appcast URL for its architecture. Use `CCWORKS_SPARKLE_FEED_URL` only for an explicit alternate feed. The app enables **Check for Updates…** in its application menu. Update checks are user-initiated and the updater verifies the archive signature before extraction. The feed should carry only releases approved for that audience. Use a separate feed if you ship a distinct Intel build. [Sparkle publishing guide](https://sparkle-project.org/documentation/publishing/) · [Sparkle settings](https://sparkle-project.org/documentation/customization/)

The macOS build downloads and verifies the pinned Sparkle release under `build/sparkle`. The upstream release also contains the signing tools. Use the `io.github.pu-shd.ccworks.sparkle.macos` Keychain account for the Mac update key. The tool keeps the private key in Keychain and `generate_keys --account ACCOUNT -p` prints its public key. Keep the Developer ID identity and update key consistent across consecutive releases. Validate an upgrade between two signed builds in a user-writable application directory before enabling the production feed. [Sparkle release workflow](https://sparkle-project.org/documentation/)

## Windows Authenticode

Use a Windows build host with the Windows SDK SignTool and Inno Setup 6.7.4. Set `CCWORKS_SIGNTOOL` and `CCWORKS_INNO_COMPILER` to the respective executable paths. `desktop/build-windows.ps1` signs unsigned packaged PE files and uses Inno Setup's SignTool integration for both the installer and its uninstaller. The build verifies the packaged PE signatures and the final installer. [Inno Setup signing](https://jrsoftware.org/inno-setup/documentation/topic_setup_signtool.htm)

- For a local certificate, set `CCWORKS_SIGN_THUMBPRINT` to its SHA-1 thumbprint in the signing user's Windows certificate store and `CCWORKS_TIMESTAMP_URL` to the approved timestamp service.
- For Azure Artifact Signing, confirm the institutional subscription is eligible and that IT can authorize use of the institution's verified publisher identity. A paid subscription is required. Free, trial, and sponsored subscriptions are ineligible. Use a Public Trust certificate profile for ordinary Windows distribution. Ask IT to assign the signing account's build identity the **Artifact Signing Certificate Profile Signer** role at the profile scope. [Azure prerequisites and setup](https://learn.microsoft.com/azure/artifact-signing/quickstart) · [Azure subscription eligibility](https://learn.microsoft.com/azure/artifact-signing/faq)
- Install the corresponding Artifact Signing Client Tools on the Windows build host. Set `CCWORKS_ARTIFACT_SIGNING_DLIB` to the x64 `Azure.CodeSigning.Dlib.dll` and `CCWORKS_ARTIFACT_SIGNING_METADATA` to the client metadata JSON containing the endpoint, account and certificate profile. SignTool uses Azure credentials from the build environment. Use IT's approved identity or an authorized federated build identity. The script defaults to Microsoft's Artifact Signing timestamp service for this mode. [Microsoft SignTool integration](https://learn.microsoft.com/azure/artifact-signing/how-to-signing-integrations)

### Build and verify in autowin

Use a dedicated Windows 11 Arm64 guest on your Apple Silicon Mac. The current ccworks installer targets x64, which Windows 11 Arm64 runs under emulation. Install x64 Python 3.12 with Tcl/Tk in the guest. Set `CCWORKS_WINDOWS_BUILD_PYTHON` to that Python executable before creating the project's packaging environment. Use the x64 Windows SDK SignTool and matching x64 Azure Client Tools DLL. The managed-device pilot still needs a standard-user check on the organization's target Windows hardware. [autowin instructions](https://github.com/pubino/autowin) · [Windows on Arm emulation](https://learn.microsoft.com/windows/arm/apps-on-arm-x86-emulation)

1. Create or start a dedicated guest with autowin. From this checkout, run `desktop/autowin-stage.zsh --dry-run ccworks-build`, then run it without `--dry-run` when the guest is ready. Set `CCWORKS_AUTOWIN_SOURCE` if your autowin checkout is outside `build/autowin-source`. The script sends an allowlisted source ZIP, verifies its hash in the guest, and prints the build directory.
2. In that directory, set `CCWORKS_INNO_COMPILER` to the pinned Inno Setup 6.7.4 compiler. Use `desktop/build-windows.ps1 -UnsignedInstaller` and `desktop/test-windows-installer.ps1` to verify install, reinstall, removal and the setup window using a fresh guest user. Run the test with approved Chrome installed.
3. Ask the Azure owner to authorize the Windows signing identity on the existing Public Trust profile. Sign in in the guest under that identity. Use a disposable run from a clean VM snapshot for sessions with organizational credentials. Pass the local metadata path as `CCWORKS_AUTOWIN_SIGNING_METADATA` when staging the source, then point `CCWORKS_ARTIFACT_SIGNING_METADATA` at the staged `artifact-signing-metadata.json`. The metadata contains the endpoint, account and profile. Azure authentication stays in the guest's signing session.
4. Run `desktop/build-windows.ps1` without build flags to sign the binaries, installer and uninstaller. Run the installer smoke test with the signed build. Copy the installer and its SHA-256 digest back through `utmctl file pull`. Verify the received digest and the publisher before offering the signed build to the pilot.

### WinSparkle for Windows self-service

A GitHub self-service build gets **Check for Updates…** when `CCWORKS_WINSPARKLE_PUBLIC_KEY` is set at build time. The build uses the Windows x64 GitHub Release appcast by default. Use `CCWORKS_WINSPARKLE_FEED_URL` for an explicit alternate feed. The Windows update key is kept in the `io.github.pu-shd.ccworks.sparkle.windows` Keychain account on the release Mac. The build verifies the pinned WinSparkle archive before bundling it. The updater checks only on request, verifies update signatures, and closes the setup window before Inno Setup replaces the existing installation. Catalog builds use IT's update channel. [WinSparkle integration and signing](https://winsparkle.org/guides/getting-started/)

Sign the final Authenticode installer with the WinSparkle Ed25519 release key, then publish the installer and its appcast entry with `sparkle:edSignature`, `sparkle:os="windows-x64"`, the version, and the byte length. The Ed25519 key and the Azure certificate have separate roles. Use `/SILENT /SP- /NORESTART` as installer arguments to retain visible installation progress. Verify a two-version upgrade through the app and confirm an invalid update signature is rejected before publishing the feed. [WinSparkle publishing](https://winsparkle.org/guides/publishing-updates/)

## GitHub Release update feeds

The GitHub Releases build has a stable feed per platform. The Apple Silicon build reads `appcast-macos-arm64.xml` and the Windows x64 build reads `appcast-windows-x64.xml` from `https://github.com/pu-shd/ccworks/releases/latest/download/`. Each feed points at a versioned installer asset on the tag that contains the file. Use `CCWORKS_UPDATE_REPOSITORY=OWNER/REPO` for an explicitly selected GitHub fork. A build with neither an update key nor a feed override omits the self-service updater. [GitHub Release asset URLs](https://docs.github.com/en/repositories/releasing-projects-on-github/linking-to-releases)

Prepare public artifacts according to the [pilot and release gate](desktop-pilot.md). For the Mac build, set the public key in the release shell, then notarize and staple the DMG.

```zsh
export CCWORKS_SPARKLE_PUBLIC_KEY="$(build/sparkle/bin/generate_keys \
  --account io.github.pu-shd.ccworks.sparkle.macos -p)"
desktop/notarize-macos.zsh
```

For Windows, use the corresponding `generate_keys --account io.github.pu-shd.ccworks.sparkle.windows -p` public key as `CCWORKS_WINSPARKLE_PUBLIC_KEY` in the autowin build session. Complete Authenticode signing in Windows before staging the update. Use the final artifact bytes for the appcast. Apple stapling or Authenticode signing after calculating the Ed25519 signature would invalidate that signature.

Run the staging command on the release Mac for each final asset. For Windows substitute the signed `.exe` and the `.windows` Keychain account. The same `sign_update` tool signs both archive formats for Sparkle and WinSparkle.

```zsh
asset=dist/ccworks-desktop-0.8.0-arm64.dmg
update_account=io.github.pu-shd.ccworks.sparkle.macos
signature="$(build/sparkle/bin/sign_update --account "$update_account" -p "$asset")"
build/sparkle/bin/sign_update --account "$update_account" --verify "$asset" "$signature"
.venv/bin/python desktop/stage-github-update.py "$asset" \
  --signature "$signature" --output build/github-release
```

After the version tag passes release validation, upload the final binaries, checksums and appcasts to a draft GitHub Release. Carry forward the last published appcast for any existing channel whose installer is unchanged. Its enclosure remains pinned to the older tag. This keeps Windows updating during a Mac-only release and vice versa. Make the completed release **Latest** only after the assets are present. Check that both applicable latest-download URLs resolve and that each appcast enclosure downloads from its declared version tag. [GitHub Releases](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository) · [WinSparkle appcasts](https://winsparkle.org/guides/publishing-updates/)

## Grant the Windows signing role

Use the Microsoft Entra identity that will actually authenticate to Azure in the Windows build session. For manual signing, sign in to Azure CLI in the guest as that user, select the tenant that owns the signing account, and run `az ad signed-in-user show --query id -o tsv` to obtain the user's object ID. For a federated build identity, use the service principal's object ID from the same tenant. [Microsoft role assignment tutorial](https://learn.microsoft.com/azure/artifact-signing/tutorial-assign-roles)

An Azure administrator with role-assignment permissions runs the following on the Mac or in Azure Cloud Shell. Select the subscription that contains the signing account. Substitute the account and profile names from `build/ccworks-artifact-signing-metadata.json` and the object ID from the previous step.

```zsh
signing_account='ACCOUNT_FROM_METADATA'
certificate_profile='PROFILE_FROM_METADATA'
signer_object_id='WINDOWS_SIGNER_OBJECT_ID'
account_scope="$(az resource list --name "$signing_account" \
  --resource-type Microsoft.CodeSigning/codeSigningAccounts \
  --query '[0].id' -o tsv)"
if [[ -n "$account_scope" ]]; then
  profile_scope="$account_scope/certificateProfiles/$certificate_profile"
  az role assignment create \
    --assignee-object-id "$signer_object_id" --assignee-principal-type User \
    --role 'Artifact Signing Certificate Profile Signer' --scope "$profile_scope"
  az role assignment list --scope "$profile_scope" \
    --assignee "$signer_object_id" --include-inherited -o table
else
  print -u2 'Select the signing subscription first.'
fi
```

Specify `ServicePrincipal` in place of `User` for automation. Use the certificate-profile scope to grant permission for this publisher only. If the assignment is denied, ask the subscription owner or access administrator to run it. After the grant, refresh the guest's Azure CLI sign-in, then use the existing metadata with SignTool and run the signed installer build. [Profile-level access control](https://learn.microsoft.com/azure/artifact-signing/tutorial-assign-roles) · [SignTool authentication](https://learn.microsoft.com/azure/artifact-signing/how-to-signing-integrations)
