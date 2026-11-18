#!/bin/zsh
set -euo pipefail
cd "${0:A:h:h}"

if [[ "${1:-}" == --dry-run && $# == 1 ]]; then
  print -r 'Build and sign the app, sign its disk image, submit it to Apple, staple and verify the ticket.'
  exit 0
fi
if (( $# )) || [[ "$(uname -s)" != Darwin ]]; then
  print -u2 'Usage on macOS: desktop/notarize-macos.zsh [--dry-run]'
  exit 2
fi
profile="${CCWORKS_NOTARY_PROFILE:-ccworks}"
xcrun notarytool history --keychain-profile "$profile" >/dev/null
desktop/package-macos.zsh "$PWD/dist"
version="$(.venv/desktop/bin/python -c 'from ccworks import __version__; print(__version__)')"
arch="$(uname -m)"
image="$PWD/dist/ccworks-desktop-${version}-${arch}.dmg"
xcrun notarytool submit "$image" --keychain-profile "$profile" --wait
xcrun stapler staple "$image"
xcrun stapler validate "$image"
spctl --assess --type open --context context:primary-signature --verbose "$image"
print -r "Signed and notarized $image"
