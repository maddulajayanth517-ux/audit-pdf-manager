@echo off
REM Start the Audit PDF Volume & Annexure Manager on http://127.0.0.1:8000
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Creating virtual environment...
  python -m venv .venv || goto :error
)
REM Install / repair dependencies if any required package is missing.
.venv\Scripts\python -c "import pymupdf, pypdf, PIL, fastapi, uvicorn, multipart, psutil" 2>nul
if errorlevel 1 (
  echo Installing dependencies - this can take a few minutes...
  .venv\Scripts\python -m pip install -r requirements.txt || goto :error
)
start "" http://127.0.0.1:8000
.venv\Scripts\python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
goto :eof
:error
echo Setup failed. Make sure Python 3.10+ is installed and on PATH, and that you are online.
pause
