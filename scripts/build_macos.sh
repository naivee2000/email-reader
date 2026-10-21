#!/bin/zsh
set -euo pipefail

project_dir="${0:A:h:h}"
cd "$project_dir"

ensure_application_shortcut() {
  local shortcut="/Applications/Email Reader.app"
  local target="$project_dir/dist/Email Reader.app"
  if [[ -L "$shortcut" ]]; then
    if [[ "$(readlink "$shortcut")" != "$target" ]]; then
      rm "$shortcut"
      ln -s "$target" "$shortcut"
    fi
  elif [[ ! -e "$shortcut" ]]; then
    ln -s "$target" "$shortcut"
  else
    echo "Kept existing non-shortcut app at $shortcut"
    return
  fi
  echo "Applications shortcut: $shortcut -> $target"
}

case "${1:-}" in
  ""|--stage) ;;
  *) echo "Usage: scripts/build_macos.sh [--stage]"; exit 2 ;;
esac

if [[ ! -x .venv/bin/python ]]; then
  echo "Create .venv and install the project before building. See README.md."
  exit 2
fi

if ! .venv/bin/python -c 'import sqlcipher3' >/dev/null 2>&1; then
  echo "SQLCipher support is mandatory for a production build."
  echo "Install it with the Homebrew flags shown in README.md."
  exit 3
fi

# Keep the app requirement stable so the immutable Keychain helper can
# authenticate rebuilt clients. Never fall back to ad-hoc production signing.
signing_identity="${EMAIL_READER_CODESIGN_IDENTITY:-Email Reader Local Signing}"
if ! security find-identity -v -p codesigning | grep -Fq "\"$signing_identity\""; then
  echo "The stable signing identity is required for private-data builds."
  exit 4
fi

mkdir -p build/packaging-work build/packaging-spec build/package-backups dist
.venv/bin/python scripts/prepare_keychain_helper.py
candidate_root="$(mktemp -d "$project_dir/build/package-candidate.XXXXXX")"
trap 'rm -rf "$candidate_root"' EXIT
.venv/bin/pyinstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "Email Reader" \
  --icon "$project_dir/assets/EmailReader.icns" \
  --osx-bundle-identifier "com.privateemailreader.app" \
  --osx-entitlements-file "packaging/entitlements.plist" \
  --add-data "$project_dir/src/email_reader/qml:email_reader/qml" \
  --add-data "$project_dir/build/keychain-helper.json:email_reader" \
  --hidden-import "sqlcipher3.dbapi2" \
  --hidden-import "keyring.backends.macOS" \
  --collect-all "googleapiclient" \
  --collect-submodules "msal" \
  --workpath "build/packaging-work" \
  --specpath "build/packaging-spec" \
  --distpath "$candidate_root" \
  src/email_reader/app.py

/usr/libexec/PlistBuddy -c \
  'Add :NSAppleEventsUsageDescription string Email Reader uses Apple Mail to import messages and save drafts at your request.' \
  "$candidate_root/Email Reader.app/Contents/Info.plist"
codesign --force --deep --sign "$signing_identity" --entitlements packaging/entitlements.plist \
  "$candidate_root/Email Reader.app"
codesign --verify --deep --strict "$candidate_root/Email Reader.app"

# Never move the bundle/resources out from under an open editor. A staged
# candidate can be installed once the user has saved their unsaved drafts.
if [[ "${1:-}" == --stage ]] || pgrep -x 'Email Reader' >/dev/null; then
  mkdir -p build/staged
  staged_root="$(mktemp -d "$project_dir/build/staged/candidate.XXXXXX")"
  mv "$candidate_root/Email Reader.app" "$staged_root/Email Reader.app"
  ensure_application_shortcut
  echo "Staged (running app unchanged): $staged_root/Email Reader.app"
  exit 0
fi

# The running version remains untouched until a complete, verified candidate
# exists. Moving it to a timestamped backup keeps installation recoverable.
if [[ -d 'dist/Email Reader.app' ]]; then
  backup_stamp="$(date +%Y%m%d-%H%M%S)"
  backup_path="build/package-backups/Email Reader-$backup_stamp.app"
  mv 'dist/Email Reader.app' "$backup_path"
fi
mv "$candidate_root/Email Reader.app" 'dist/Email Reader.app'
ensure_application_shortcut

# A spare .app is discoverable by Launchpad/Spotlight and can look like a
# second installation. Keep the rollback copy in a verified ZIP instead.
if [[ -n "${backup_path:-}" ]]; then
  archive_path="${backup_path%.app}.zip"
  ditto -c -k --keepParent "$backup_path" "$archive_path"
  unzip -tq "$archive_path" >/dev/null
  rm -rf -- "$backup_path"
  echo "Recoverable previous build: $archive_path"
fi

echo "Built: $project_dir/dist/Email Reader.app (signed with $signing_identity)"
