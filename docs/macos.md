# macOS — usable path + packaging

## Double-click (your Mac)

1. Install **Python ≥3.10** (`brew install python@3.11`). Apple CLT 3.9 cannot install `lancedb>=0.34`.
2. Open the repo folder in Finder.
3. If Gatekeeper blocks launchers: right-click **`Start QP Helper.command`** → **Open**, or:
   ```bash
   xattr -dr com.apple.quarantine .
   chmod +x "Start QP Helper.command" "Start RAG Service.command" scripts/*.sh
   ```
4. Double-click **`Start QP Helper.command`** (or **`Start QP Helper.app`**).
   - Recreates `.venv` if missing / &lt;3.10 / pip-broken.
   - Copies example → `qp_email_helper.config.json` when needed.
   - Starts GUI; RAG auto-starts on `127.0.0.1:8765`.
5. Settings → paste API key → Save → ask on Home.
   - Defaults: DeepSeek base URL + `deepseek-v4-pro`.

Optional RAG-only window: double-click **`Start RAG Service.command`**.

```bash
./scripts/setup_macos.sh
./scripts/run.sh
./scripts/smoke_macos.sh          # CI/Linux-safe offline checks
./scripts/smoke_macos.sh --live   # live port + optional /ask if key present
```

Outlook fetch (optional): `.venv/bin/playwright install chromium`.

Default shared root: `~/Documents/QP Helper/Shared`. Prefer the `.command` launcher so macOS does not sudden-terminate a background Python GUI.

## Build `.app` / `.dmg` (on a Mac)

```bash
./packaging/macos/build_macos.sh
```

Outputs `dist/QP Helper.app` and `dist/QP-Helper-macOS.dmg`. Optional: `packaging/macos/AppIcon.icns`.

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

## Still needs your Mac (cannot do from cloud / blocked CLI login)

| Item | Why |
|------|-----|
| Double-click GUI / Dock / Gatekeeper | PyQt + Finder on your machine |
| Developer ID signing + notarization | Your Apple cert / notary secrets |
| Optional AppIcon.icns | Brand asset |
| Playwright Outlook login | Real browser session |
| Live `/ask` with your API key | Key stays on your machine |
