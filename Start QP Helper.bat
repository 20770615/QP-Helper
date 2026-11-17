@echo off
REM Double-click launcher for Windows.
setlocal EnableExtensions
cd /d "%~dp0"
echo QP Helper
echo Repo: %CD%
echo.

if not exist ".venv\Scripts\python.exe" (
  echo No .venv found — creating venv and installing requirements...
  where python >nul 2>&1
  if errorlevel 1 (
    echo ERROR: python not found on PATH. Install Python 3.11+ and retry.
    pause
    exit /b 1
  )
  python -m venv .venv
  if errorlevel 1 (
    echo ERROR: failed to create .venv
    pause
    exit /b 1
  )
  call ".venv\Scripts\python.exe" -m pip install --upgrade pip
  call ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo ERROR: pip install failed
    pause
    exit /b 1
  )
)

if not exist "qp_email_helper.config.json" (
  if exist "qp_email_helper.config.example.json" (
    copy /Y "qp_email_helper.config.example.json" "qp_email_helper.config.json" >nul
    echo Created qp_email_helper.config.json from example.
  )
)

set "RAG_VECTOR_BACKEND=lancedb"
echo Launching QP Helper...
call "scripts\run.bat" %*
set "EC=%ERRORLEVEL%"
if not "%EC%"=="0" (
  echo.
  echo QP Helper exited with code %EC%.
  pause
)
exit /b %EC%
