@echo off
REM Start the local RAG FastAPI service on Windows with visible logs.
setlocal
set "ROOT=%~dp0.."
cd /d "%ROOT%"
if not exist "%ROOT%\.venv\Scripts\python.exe" (
  echo No .venv. Create one and pip install -r requirements.txt -r requirements-rag.txt
  exit /b 1
)
if not exist "%ROOT%\rag_service\main.py" (
  echo ERROR: rag_service\main.py missing
  exit /b 1
)
set "RAG_VECTOR_BACKEND=lancedb"
set "RAG_CONFIG_PATH=%ROOT%\qp_email_helper.config.json"
if not exist "%RAG_CONFIG_PATH%" if exist "%ROOT%\qp_email_helper.config.example.json" (
  copy /Y "%ROOT%\qp_email_helper.config.example.json" "%RAG_CONFIG_PATH%" >nul
)
if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"
echo Starting rag_service on 127.0.0.1:8765
"%ROOT%\.venv\Scripts\python.exe" "%ROOT%\rag_service\main.py" --host 127.0.0.1 --port 8765 %*
