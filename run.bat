@echo off
REM Start the Audit PDF Volume & Annexure Manager on http://127.0.0.1:8000
cd /d "%~dp0"
set "URL=http://127.0.0.1:8000"

REM Already running (e.g. started twice)? Just open the browser.
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing '%URL%/api/health' -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 (
  echo The app is already running - opening %URL%
  start "" "%URL%"
  goto :eof
)

REM Python 3.10 or newer is required.
where python >nul 2>&1
if errorlevel 1 goto :nopython
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto :oldpython

if not exist .venv\Scripts\python.exe (
  echo Creating the Python environment - first start only...
  python -m venv .venv || goto :error
)

REM Install / repair the required libraries if any is missing (needs internet the first time).
.venv\Scripts\python -c "import pymupdf, pypdf, PIL, fastapi, uvicorn, multipart, psutil" >nul 2>&1
if errorlevel 1 (
  echo Installing the required libraries - first start only, this can take a few minutes...
  .venv\Scripts\python -m pip install --disable-pip-version-check -r requirements.txt || goto :error
)

echo.
echo  Audit PDF Volume ^& Annexure Manager is starting...
echo  The browser opens automatically at %URL%
echo  Keep this window open while you use the app. Close it (or press Ctrl+C) to stop.
echo.
REM Open the browser as soon as the app answers (instead of too early).
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "for ($i = 0; $i -lt 90; $i++) { try { Invoke-WebRequest -UseBasicParsing '%URL%/api/health' -TimeoutSec 2 | Out-Null; Start-Process '%URL%'; break } catch { Start-Sleep -Seconds 1 } }"
.venv\Scripts\python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
if errorlevel 1 (
  echo.
  echo The app stopped with an error. If it says the port is already in use, another program is using
  echo port 8000 - close it, or restart the computer, and run this file again.
  pause
)
goto :eof

:nopython
echo Python is not installed (or not on PATH).
echo Install Python 3.10 or newer from https://www.python.org/downloads/
echo IMPORTANT: on the first installer screen tick "Add python.exe to PATH". Then run this file again.
pause
goto :eof

:oldpython
echo Your Python is too old. Install Python 3.10 or newer from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH"), then run this file again.
pause
goto :eof

:error
echo.
echo Setup failed. Check the internet connection (needed only for the first start) and try again.
pause
