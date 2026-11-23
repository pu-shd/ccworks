#!/bin/zsh
set -euo pipefail
cd "${0:A:h:h}"
if (( $# )); then
  print -u2 'Usage: desktop/run-macos.zsh'
  exit 2
fi
open 'dist/ccworks Desktop.app'
print -r 'Opened ccworks Desktop.'
