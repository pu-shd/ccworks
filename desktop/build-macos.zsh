#!/bin/zsh
set -euo pipefail
cd "${0:A:h:h}"

if [[ "${1:-}" == --dry-run && $# == 1 ]]; then
  print -r 'Build the SwiftUI app and bundled command helper in dist/.'
  exit 0
fi
if (( $# )) || [[ "$(uname -s)" != Darwin ]]; then
  print -u2 'Usage on macOS: desktop/build-macos.zsh [--dry-run]'
  exit 2
fi
python_tool="${CCWORKS_BUILD_PYTHON:-python3.12}"
venv_dir=".venv/desktop"
if [[ ! -x "$venv_dir/bin/python" ]]; then
  "$python_tool" -m venv "$venv_dir"
fi
"$venv_dir/bin/python" -m pip install -r requirements-desktop.lock -e .
"$venv_dir/bin/python" desktop/macos/bundle_config.py --validate
mkdir -p build/ccworks.iconset
swift desktop/make-icon.swift build/ccworks.iconset
iconutil -c icns build/ccworks.iconset -o build/ccworks.icns

sparkle_version="$(sed -n '1p' desktop/macos/sparkle.lock)"
sparkle_checksum="$(sed -n '2p' desktop/macos/sparkle.lock)"
sparkle_dir='build/sparkle'
sparkle_archive="$sparkle_dir/Sparkle-$sparkle_version.tar.xz"
mkdir -p "$sparkle_dir"
if [[ ! -f "$sparkle_archive" ]]; then
  curl --fail --location --silent --show-error \
    "https://github.com/sparkle-project/Sparkle/releases/download/$sparkle_version/Sparkle-$sparkle_version.tar.xz" \
    -o "$sparkle_archive.part"
  mv -- "$sparkle_archive.part" "$sparkle_archive"
fi
printf '%s  %s\n' "$sparkle_checksum" "$sparkle_archive" | shasum -a 256 -c
tar -xJf "$sparkle_archive" -C "$sparkle_dir" Sparkle.framework LICENSE

"$venv_dir/bin/python" -m PyInstaller --noconfirm --clean desktop/ccworks.spec
app='dist/ccworks Desktop.app'
"$venv_dir/bin/python" desktop/macos/bundle_config.py "$app/Contents/Info.plist"
framework="$app/Contents/Frameworks/Sparkle.framework"
ditto "$sparkle_dir/Sparkle.framework" "$framework"
cp "$sparkle_dir/LICENSE" "$app/Contents/Resources/Sparkle-License.txt"
xcrun swiftc -target "$(uname -m)-apple-macos13.0" \
  -F "$sparkle_dir" -framework Sparkle \
  -Xlinker -rpath -Xlinker '@executable_path/../Frameworks' \
  desktop/macos/{SetupApp,HelperProtocol}.swift -o "$app/Contents/MacOS/ccworks-desktop"

if [[ -n "${CCWORKS_MAC_SIGN_IDENTITY:-}" ]]; then
  sparkle_code="$framework/Versions/B"
  codesign --force --sign "$CCWORKS_MAC_SIGN_IDENTITY" --timestamp --options runtime \
    --entitlements desktop/macos/playwright-node.entitlements \
    "$app/Contents/Frameworks/playwright/driver/node"
  codesign --force --sign "$CCWORKS_MAC_SIGN_IDENTITY" --timestamp --options runtime \
    --preserve-metadata=entitlements "$sparkle_code/XPCServices/Downloader.xpc"
  for code in "$sparkle_code/XPCServices/Installer.xpc" "$sparkle_code/Updater.app" \
      "$sparkle_code/Autoupdate" "$framework" "$app/Contents/MacOS/ccworks"; do
    codesign --force --sign "$CCWORKS_MAC_SIGN_IDENTITY" --timestamp --options runtime "$code"
  done
  codesign --force --sign "$CCWORKS_MAC_SIGN_IDENTITY" --timestamp --options runtime "$app"
else
  codesign --force --sign - "$app/Contents/MacOS/ccworks"
  codesign --force --sign - "$app"
fi
codesign --verify --deep --strict "$app"
"$app/Contents/MacOS/ccworks" --version
print -r "Built $app"
