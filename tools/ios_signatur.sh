#!/bin/bash
# Switch the app target of the generated Xcode project to manual signing.
#
#     tools/ios_signatur.sh <project.pbxproj> <team ID> <profile name>
#
# **Why here and not as a parameter to xcodebuild.** A build setting on the
# command line applies to *every* target, including the Swift packages of
# Capacitor and the plugins. They do not know provisioning profiles, and
# Xcode aborts with "does not support provisioning profiles". Only the app
# target is touched here.
#
# The template sets `CODE_SIGN_STYLE = Automatic;` exactly twice - Debug and
# Release of the app target. If the count is off, the template has changed,
# and it is better to abort than to sign wrongly somewhere.
set -euo pipefail

PBXPROJ="${1:?project.pbxproj missing}"
TEAM="${2:?team ID missing}"
PROFILE="${3:?profile name missing}"

COUNT="$(grep -c 'CODE_SIGN_STYLE = Automatic;' "$PBXPROJ" || true)"
if [ "$COUNT" != "2" ]; then
  echo "Expected: two occurrences of 'CODE_SIGN_STYLE = Automatic;', found: $COUNT." >&2
  echo "The Capacitor template has changed - adjust tools/ios_signatur.sh." >&2
  exit 1
fi

# Characters that would mean something in the replacement do not belong in
# the profile name. Apple allows spaces and hyphens; nothing more is needed
# here.
case "$PROFILE" in
  *[\"\\\&\|/]*) echo "Profile name contains an invalid character: $PROFILE" >&2; exit 1 ;;
esac
case "$TEAM" in
  *[!A-Z0-9]*) echo "Team ID may only consist of capital letters and digits: $TEAM" >&2; exit 1 ;;
esac

# BSD sed (macOS) requires an argument after -i, GNU sed forbids it. With a
# backup file both work.
sed -i.bak "s|CODE_SIGN_STYLE = Automatic;|CODE_SIGN_STYLE = Manual; DEVELOPMENT_TEAM = $TEAM; CODE_SIGN_IDENTITY = \"Apple Distribution\"; \"CODE_SIGN_IDENTITY[sdk=iphoneos*]\" = \"Apple Distribution\"; PROVISIONING_PROFILE_SPECIFIER = \"$PROFILE\";|" "$PBXPROJ"
rm -f "$PBXPROJ.bak"

echo "Signing set in the app target: team $TEAM, profile \"$PROFILE\"."
