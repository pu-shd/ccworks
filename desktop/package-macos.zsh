#!/bin/zsh
set -euo pipefail
cd "${0:A:h:h}"

if [[ "${1:-}" == --dry-run && $# == 1 ]]; then
  print -r 'Build the Developer ID-signed app and disk image for local verification.'
  exit 0
fi
if (( $# > 1 )) || [[ "$(uname -s)" != Darwin ]]; then
  print -u2 'Usage on macOS: desktop/package-macos.zsh [output-directory | --dry-run]'
  exit 2
fi
source desktop/macos/signing.zsh
identity="$(ccworks_signing_identity)"
export CCWORKS_MAC_SIGN_IDENTITY="$identity"
output="${1:-build/macos-release-candidate}"
mkdir -p -- "$output"
desktop/sign-macos.zsh
version="$(.venv/desktop/bin/python -c 'from ccworks import __version__; print(__version__)')"
arch="$(uname -m)"
image="${output:A}/ccworks-desktop-${version}-${arch}.dmg"
stage="$(mktemp -d)"
trap 'rm -rf -- "$stage"' EXIT
ditto 'dist/ccworks Desktop.app' "$stage/ccworks Desktop.app"
cp docs/desktop-setup.md "$stage/Setup Guide.txt"
hdiutil create -ov -volname 'ccworks Desktop' -srcfolder "$stage" -format UDZO "$image"
codesign --sign "$identity" --timestamp "$image"
codesign --verify --strict \
  -R='anchor apple generic and certificate leaf[field.1.2.840.113635.100.6.1.13] exists' "$image"
hdiutil verify "$image"
print -r "Verified the signed disk image at $image"
