# macOS — usable path + packaging

## Run (dev)

1. Install **Python ≥3.10** (Homebrew / python.org / micromamba). Apple CLT 3.9 cannot install `lancedb>=0.34`.
2. Double-click **`Start QP Helper.command`** (or `Start QP Helper.app`).
   - Recreates `.venv` if missing or still on 3.9.
   - Copies `qp_email_helper.config.example.json` → `qp_email_helper.config.json` when needed.
   - Starts the GUI; RAG auto-starts on `127.0.0.1:8765`.
3. Settings → set API key (defaults: DeepSeek base URL + `deepseek-v4-pro`) → Save → ask on Home.

```bash
./scripts/setup_macos.sh
./scripts/run.sh
./scripts/smoke_macos.sh    # venv version, /health, model normalize; /ask only if key present
```

Optional Outlook fetch: `.venv/bin/playwright install chromium`.

Default shared root: `~/Documents/QP Helper/Shared`.

If a background Python GUI vanishes with no traceback, use the `.command` launcher (Terminal-owned session) so macOS does not sudden-terminate an accessory process.

## Build `.app` / `.dmg` (on a Mac)

```bash
./packaging/macos/build_macos.sh
```

Outputs `dist/QP Helper.app` and `dist/QP-Helper-macOS.dmg`.
See `packaging/macos/` for Info.plist, entitlements, and the PyInstaller spec.
Optional: add `packaging/macos/AppIcon.icns`.

## Codesign + notarize (your Apple certs — never commit secrets)

```bash
codesign --deep --force --options runtime \
  --entitlements packaging/macos/entitlements.plist \
  --sign "Developer ID Application: YOUR NAME (TEAMID)" \
  "dist/QP Helper.app"

xcrun notarytool submit "dist/QP-Helper-macOS.dmg" \
  --apple-id "you@example.com" --team-id "TEAMID" \
  --password "app-specific-password" --wait

xcrun stapler staple "dist/QP Helper.app"
xcrun stapler staple "dist/QP-Helper-macOS.dmg"
spctl --assess --type execute -vv "dist/QP Helper.app"
```

## Still needs a real Mac

| Item | Why |
|------|-----|
| GUI / Dock / Gatekeeper click-through | Cloud agents cannot exercise PyQt + Finder |
| Developer ID signing + notarization | Your Apple cert / notary secrets |
| Optional AppIcon.icns | Brand asset |
| Playwright Outlook login | Real browser session |
