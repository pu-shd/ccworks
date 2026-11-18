# Sourced by the macOS app and disk-image builders.
ccworks_signing_identity() {
  if [[ -n "${CCWORKS_MAC_SIGN_IDENTITY:-}" ]]; then
    print -r -- "$CCWORKS_MAC_SIGN_IDENTITY"
    return
  fi
  local identities
  identities="$(security find-identity -v -p codesigning | sed -nE 's/^ +[0-9]+\) ([[:xdigit:]]{40}) "Developer ID Application: .*"$/\1/p')"
  if [[ ! "$identities" =~ '^[A-Fa-f0-9]{40}$' ]]; then
    print -u2 'Set CCWORKS_MAC_SIGN_IDENTITY to the intended Developer ID Application identity.'
    return 2
  fi
  print -r -- "$identities"
}
