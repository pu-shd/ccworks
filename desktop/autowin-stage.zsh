#!/bin/zsh
set -euo pipefail
cd "${0:A:h:h}"

dry_run=0
if [[ "${1:-}" == --dry-run ]]; then
  dry_run=1
  shift
fi
if (( $# < 1 || $# > 2 )); then
  print -u2 'Usage: desktop/autowin-stage.zsh [--dry-run] VM [guest-user]'
  exit 2
fi
vm="$1"
guest_user="${2:-tester}"
if [[ ! "$vm" =~ '^[A-Za-z0-9._-]{1,40}$' || ! "$guest_user" =~ '^[A-Za-z0-9_-]{1,20}$' ]]; then
  print -u2 'Use a valid autowin VM name and local Windows user.'
  exit 2
fi
if (( dry_run )); then
  print -r "Stage allowlisted ccworks source in an isolated build folder for $guest_user on $vm."
  exit 0
fi

autowin="${CCWORKS_AUTOWIN_SOURCE:-$PWD/build/autowin-source}"
guest_agent="${UTMCTL:-/Applications/UTM.app/Contents/MacOS/utmctl}"
if [[ "$(uname -s)" != Darwin || ! -x "$guest_agent" || ! -x "$autowin/examples/zsh/vm-ps.sh" ]]; then
  print -u2 'Set CCWORKS_AUTOWIN_SOURCE to the autowin checkout and install UTM on your Mac.'
  exit 2
fi

runner="$autowin/examples/zsh/vm-ps.sh"
export UTMCTL="$guest_agent"
guest_profile="$("$runner" --user "$guest_user" "$vm" 'Write-Output $env:USERPROFILE')"
guest_profile="${guest_profile%%$'\n'*}"
if [[ "$guest_profile" != 'C:\Users\'"$guest_user" ]]; then
  print -u2 'The logged-in guest must use the expected local Windows profile.'
  exit 2
fi
archive="$PWD/build/autowin/ccworks-windows-source.zip"
if [[ -n "${CCWORKS_AUTOWIN_SIGNING_METADATA:-}" ]]; then
  .venv/bin/python desktop/windows-build-payload.py "$archive" --metadata "$CCWORKS_AUTOWIN_SIGNING_METADATA" >&2
else
  .venv/bin/python desktop/windows-build-payload.py "$archive" >&2
fi
digest="$(shasum -a 256 "$archive" | cut -d ' ' -f 1)"
[[ "$digest" =~ '^[a-f0-9]{64}$' ]]
guest_root="$guest_profile"'\ccworks-build'
guest_archive="$guest_root"'\'"${digest}.zip"
guest_target="$guest_root"'\source-'"${digest[1,24]}"
"$runner" --user "$guest_user" "$vm" "New-Item -Force -ItemType Directory -Path '$guest_root' | Out-Null" >/dev/null
if ! {
  perl -e 'alarm shift; exec @ARGV' 15 "$guest_agent" file push --hide "$vm" "$guest_archive" < "$archive" &&
  "$runner" --user "$guest_user" "$vm" "if ((Get-FileHash -LiteralPath '$guest_archive').Hash -ne '$digest') { throw 'Upload did not match the source archive.' }"
} >/dev/null 2>&1; then
  print -ru2 'UTM file upload unavailable. Sending the verified source in bounded guest-agent messages.'
  guest_archive="$guest_root"'\'"${digest}-$RANDOM.zip"
  "$runner" --user "$guest_user" "$vm" "[IO.File]::WriteAllBytes('$guest_archive', [byte[]]@())" >/dev/null
  base64 < "$archive" | tr -d '\n' | fold -w 6000 | while IFS= read -r chunk || [[ -n "$chunk" ]]; do
    "$runner" "$vm" "
      \$ErrorActionPreference = 'Stop'
      \$bytes = [Convert]::FromBase64String('$chunk')
      \$stream = [IO.File]::Open('$guest_archive', [IO.FileMode]::Append)
      try { \$stream.Write(\$bytes, 0, \$bytes.Length) } finally { \$stream.Dispose() }
    " >/dev/null
  done
fi
"$runner" --user "$guest_user" "$vm" "
  \$ErrorActionPreference = 'Stop'
  \$archive = '$guest_archive'
  if ((Get-FileHash -LiteralPath \$archive -Algorithm SHA256).Hash -ne '$digest') { throw 'Source archive hash differs.' }
  \$target = '$guest_target'
  Expand-Archive -LiteralPath \$archive -DestinationPath \$target -Force
" >/dev/null
print -r -- "$guest_target"
