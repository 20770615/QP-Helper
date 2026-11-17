# QP Helper (UI)

Cross-platform PyQt6 desktop UI for QP Helper.

```bash
# macOS / Linux
../.venv/bin/python qp_email_helper.py
# or from repo root:
./scripts/run.sh

# Windows
..\.venv\Scripts\python.exe qp_email_helper.py
```

`platform_compat.py` centralizes OS differences (venv paths, file open,
fonts, macOS Qt tweaks).
