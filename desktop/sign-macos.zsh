#!/bin/zsh
set -euo pipefail
cd "${0:A:h:h}"

if [[ "${1:-}" == --dry-run && $# == 1 ]]; then
  print -r 'Find the local Developer ID Application identity, sign the app and verify its signature.'
  exit 0
fi
if (( $# )) || [[ "$(uname -s)" != Darwin ]]; then
  print -u2 'Usage on macOS: desktop/sign-macos.zsh [--dry-run]'
  exit 2
fi
source desktop/macos/signing.zsh
identity="$(ccworks_signing_identity)"
export CCWORKS_MAC_SIGN_IDENTITY="$identity"
desktop/build-macos.zsh
app='dist/ccworks Desktop.app'
codesign --verify --deep --strict \
  -R='anchor apple generic and certificate leaf[field.1.2.840.113635.100.6.1.13] exists' "$app"
print -r 'Verified the Developer ID Application signature.'
