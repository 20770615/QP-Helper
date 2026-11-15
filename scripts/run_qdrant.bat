@echo off
REM Launch QP Helper with the disk-backed LanceDB vector store (Windows).
setlocal
set "RAG_VECTOR_BACKEND=lancedb"
call "%~dp0run.bat" %*
