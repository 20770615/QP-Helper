# QP Helper

Cross-platform **PyQt6** research desktop app (emails, docs, RAG chat).
UI source: [`src/`](src/). Upstream Windows UI: [`QP-Email-Helper`](https://github.com/20770615/QP-Email-Helper).

## macOS — usable path

Needs **Python ≥3.10** (Apple CLT `python3` is 3.9.x — too old for LanceDB).

1. Install Python 3.11+ (Homebrew `brew install python@3.11`, python.org, or micromamba).
2. Double-click **`Start QP Helper.command`** (or `Start QP Helper.app`).
   - Creates `.venv`, installs deps, copies config if missing, starts the GUI.
   - App **auto-starts** the local RAG service on port `8765`.
3. Open **Settings** → paste your DeepSeek (or compatible) **API key** → Save.
   - Defaults: `https://api.deepseek.com/v1` + model `deepseek-v4-pro`.
4. Ask a question on Home (knowledge base / chat).

Shell equivalent:

```bash
./scripts/setup_macos.sh
./scripts/run.sh                 # GUI + auto RAG
# or: ./scripts/run_rag_service.sh   / double-click Start RAG Service.command
./scripts/smoke_macos.sh         # venv /health / normalize; /ask only if key set
```

Shared docs default: `~/Documents/QP Helper/Shared`. Details / `.app` build / notarization: [docs/macos.md](docs/macos.md).

## Windows

Double-click `Start QP Helper.bat`, or:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy qp_email_helper.config.example.json qp_email_helper.config.json
.\scripts\run.bat
```

## Linux

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-rag.txt
cp qp_email_helper.config.example.json qp_email_helper.config.json
./scripts/run.sh
```

## Config

`qp_email_helper.config.example.json` → `qp_email_helper.config.json` (gitignored).
Do not commit API keys. Empty / wrong-cased DeepSeek model ids are normalized to
`deepseek-v4-pro` / `deepseek-flash`.

## Layout

```
src/                  PyQt6 application
rag/ + rag_service/   local LanceDB RAG API (default :8765)
scripts/              setup / run / smoke helpers
packaging/macos/      Info.plist, entitlements, PyInstaller
docs/macos.md         Mac build + notarization
```
