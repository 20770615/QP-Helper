# macOS setup, packaging, and notarization

QP Helper is a **PyQt6** desktop app. The Windows portable layout used
`.venv\Scripts\python.exe` and `G:\` shared-drive defaults. This repo adds
macOS-native paths, menus, Finder integration, and packaging hooks.

## Dev run (Mac)

```bash
./scripts/setup_macos.sh
cp qp_email_helper.config.example.json qp_email_helper.config.json
# edit API key / shared root in the config file
./scripts/run.sh
```

Default shared root on macOS: `~/Documents/QP Helper/Shared`.

Outlook email fetch still needs Playwright browsers:

```bash
.venv/bin/playwright install chromium
```

## Build `.app` / `.dmg` (must run on a Mac)

```bash
./packaging/macos/build_macos.sh
```

Outputs:

- `dist/QP Helper.app`
- `dist/QP-Helper-macOS.dmg` (via `hdiutil`)

Files involved:

- `packaging/macos/qp-helper.spec` — PyInstaller bundle
- `packaging/macos/Info.plist` — bundle metadata
- `packaging/macos/entitlements.plist` — Hardened Runtime entitlements

Optional: add `packaging/macos/AppIcon.icns` and set `icon=` in the spec.

## Codesign + notarize (manual — needs Apple Developer certs)

These steps require a physical Mac, an Apple Developer Program membership,
and secrets that should **never** be committed:

1. Import your **Developer ID Application** certificate into the login keychain.
2. Create an app-specific password / API key for notarytool.
3. Sign the app (replace identity name):

```bash
codesign --deep --force --options runtime \
  --entitlements packaging/macos/entitlements.plist \
  --sign "Developer ID Application: YOUR NAME (TEAMID)" \
  "dist/QP Helper.app"
```

4. Notarize:

```bash
xcrun notarytool submit "dist/QP-Helper-macOS.dmg" \
  --apple-id "you@example.com" \
  --team-id "TEAMID" \
  --password "app-specific-password" \
  --wait
```

5. Staple:

```bash
xcrun stapler staple "dist/QP Helper.app"
xcrun stapler staple "dist/QP-Helper-macOS.dmg"
```

6. Gatekeeper check:

```bash
spctl --assess --type execute -vv "dist/QP Helper.app"
```

## What still needs a real Mac

| Item | Why |
|------|-----|
| GUI smoke test | Cloud agents cannot exercise PyQt/Finder/Dock |
| Developer ID signing | Requires your Apple cert + private key |
| Notarization | Requires Apple ID / API key secrets |
| App icon `.icns` | Optional brand asset |
| Playwright Outlook login | Browser automation against real Edge/Chrome |

## Windows / Linux behavior

Windows launchers (`scripts/run.bat`, `scripts/run_qdrant.bat`) and Windows
venv path discovery remain intact. Linux uses the same `run.sh` path helpers
as macOS (`xdg-open` for files).
