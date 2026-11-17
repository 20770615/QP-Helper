# QP Helper

Cross-platform **PyQt6** research desktop app (emails, docs, RAG chat).
UI source: [`src/`](src/). Upstream Windows UI: [`QP-Email-Helper`](https://github.com/20770615/QP-Email-Helper).

## macOS — double-click path (do this on your Mac)

Needs **Python ≥3.10** first (`brew install python@3.11` — Apple CLT `python3` is 3.9 and will fail).

1. Clone this repo (branch `cursor/macos-compatibility-f446` / PR #1) and open the folder in Finder.
2. If macOS blocks the file: right-click **`Start QP Helper.command`** → **Open** (or run once in Terminal: `xattr -dr com.apple.quarantine .`).
3. Double-click **`Start QP Helper.command`** (or **`Start QP Helper.app`**).
   - First run creates `.venv`, installs deps, copies config, starts the GUI.
   - The app **auto-starts RAG** on `127.0.0.1:8765`.
4. **Settings** → paste your DeepSeek API key → **Save** (defaults: `https://api.deepseek.com/v1`, model `deepseek-v4-pro`).
5. On **Home**, ask the knowledge base.

Optional: double-click **`Start RAG Service.command`** for RAG logs alone.

Shell / CI check (no API key required):

```bash
./scripts/setup_macos.sh
./scripts/smoke_macos.sh          # offline: imports, normalize, TestClient /health
./scripts/smoke_macos.sh --live   # also curl live /health; /ask only if key set
```

Shared docs default: `~/Documents/QP Helper/Shared`. Packaging / notarization: [docs/macos.md](docs/macos.md).

## Windows

Double-click `Start QP Helper.bat`, or create a venv, `pip install -r requirements.txt`, copy the example config, run `scripts\run.bat`.

## Linux

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-rag.txt
cp qp_email_helper.config.example.json qp_email_helper.config.json
./scripts/run.sh
./scripts/smoke_macos.sh
```

## Config

`qp_email_helper.config.example.json` → `qp_email_helper.config.json` (gitignored). Never commit API keys. DeepSeek model ids are normalized (`deepseek-V4-Pro` → `deepseek-v4-pro`).

## Layout

```
src/                  PyQt6 application
rag/ + rag_service/   local LanceDB RAG API (default :8765)
scripts/              setup / run / smoke helpers
packaging/macos/      Info.plist, entitlements, PyInstaller
docs/macos.md         Mac build + notarization
```
