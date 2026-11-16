# QP Helper

QP Helper is an integrated research portal for equity analysis. It helps
investment teams unify emails, documents, meeting notes, and portfolio data,
retrieve relevant context through search and RAG, and turn mixed sources into
a decision-making workflow.

This repository is the **cross-platform PyQt6 desktop app**, with first-class
**macOS** support (paths, menus, Finder, packaging) while keeping Windows and
Linux launch paths working.

UI source lives in [`src/`](src/). Upstream Windows-oriented UI also exists in
[`QP-Email-Helper`](https://github.com/20770615/QP-Email-Helper).

## Quick start

### macOS

**Double-click** `Start QP Helper.command` (or `Start QP Helper.app`) in Finder.
First launch creates `.venv` and a config file if needed; Terminal stays open
on errors so you can read them.

Or from a shell:

```bash
./scripts/setup_macos.sh
cp qp_email_helper.config.example.json qp_email_helper.config.json
./scripts/run.sh
```

See [docs/macos.md](docs/macos.md) for `.app` / `.dmg` builds, codesigning,
and notarization.

### Windows

**Double-click** `Start QP Helper.bat`, or:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy qp_email_helper.config.example.json qp_email_helper.config.json
.\scripts\run.bat
```

### Linux

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp qp_email_helper.config.example.json qp_email_helper.config.json
./scripts/run.sh
```

## Config

Copy `qp_email_helper.config.example.json` → `qp_email_helper.config.json`
and fill in local settings. Do not commit API keys.

On macOS the default shared root is `~/Documents/QP Helper/Shared`. On Windows
leave `shared_root` empty (or point it at your `G:\` team share) until configured
in **Settings**.

## Packaging (macOS)

```bash
./packaging/macos/build_macos.sh
```

Produces `dist/QP Helper.app` and `dist/QP-Helper-macOS.dmg`. Signing and
Apple notarization require your Developer ID certificate — see
[docs/macos.md](docs/macos.md).

## Layout

```
src/                  PyQt6 application
scripts/              run / setup helpers (sh + bat)
packaging/macos/      Info.plist, entitlements, PyInstaller spec
docs/macos.md         Mac build + notarization notes
```
