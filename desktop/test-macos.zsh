#!/bin/zsh
set -euo pipefail
cd "${0:A:h:h}"

if (( $# )); then
  print -u2 'Usage: desktop/test-macos.zsh'
  exit 2
fi
app='dist/ccworks Desktop.app'
"$app/Contents/Frameworks/playwright/driver/node" -e 'console.log("PASS: bundled Playwright Node executes JavaScript.")'
swiftlint lint --strict desktop/macos
mkdir -p build
xcrun swiftc desktop/macos/{HelperProtocol,ProtocolTests}.swift -o build/ccworks-macos-tests
build/ccworks-macos-tests
.venv/desktop/bin/python -m unittest -v tests.test_desktop tests.test_desktop_bridge
.venv/desktop/bin/python desktop/smoke.py "$app/Contents/MacOS/ccworks"
