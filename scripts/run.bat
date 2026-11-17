@echo off
REM Launch QP Helper on Windows from this repo.
setlocal
set "ROOT=%~dp0.."
cd /d "%ROOT%"
set "RAG_VECTOR_BACKEND=lancedb"
if exist "%ROOT%\.venv\Scripts\python.exe" (
  "%ROOT%\.venv\Scripts\python.exe" "%ROOT%\src\qp_email_helper.py" %*
) else (
  python "%ROOT%\src\qp_email_helper.py" %*
)
