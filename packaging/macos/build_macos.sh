#!/usr/bin/env bash
# Build QP Helper.app (and optional .dmg) on macOS with PyInstaller.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script must run on macOS." >&2
  exit 1
fi

if [[ ! -x "$ROOT/.venv/bin/pyinstaller" ]]; then
  echo "Missing .venv with PyInstaller. Run ./scripts/setup_macos.sh first." >&2
  exit 1
fi

# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"

rm -rf "$ROOT/dist/QP Helper" "$ROOT/dist/QP Helper.app" "$ROOT/build"
pyinstaller --noconfirm "$ROOT/packaging/macos/qp-helper.spec"

APP="$ROOT/dist/QP Helper.app"
if [[ ! -d "$APP" ]]; then
  echo "Build failed: $APP not found" >&2
  exit 1
fi

echo "Built: $APP"

# Optional ad-hoc sign for local smoke tests (not notarized).
if command -v codesign >/dev/null 2>&1; then
  codesign --force --deep --sign - \
    --entitlements "$ROOT/packaging/macos/entitlements.plist" \
    "$APP" || true
  echo "Ad-hoc codesign applied (local only)."
fi

# Optional DMG if create-dmg or hdiutil is available.
DMG="$ROOT/dist/QP-Helper-macOS.dmg"
if command -v hdiutil >/dev/null 2>&1; then
  rm -f "$DMG"
  STAGE="$ROOT/dist/dmg-stage"
  rm -rf "$STAGE"
  mkdir -p "$STAGE"
  cp -R "$APP" "$STAGE/"
  ln -sf /Applications "$STAGE/Applications"
  hdiutil create -volname "QP Helper" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
  rm -rf "$STAGE"
  echo "DMG: $DMG"
fi

echo
echo "Next (Developer ID signing + notarization): see docs/macos.md"
