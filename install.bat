@echo off
REM Install dependencies + Playwright Chromium into a local venv.
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python is not on PATH. Install Python 3.10+ from python.org.
  pause
  exit /b 1
)

if not exist venv (
  echo Creating virtual environment...
  python -m venv venv
)

call venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

echo.
echo Installing Playwright Chromium (one-time, ~150MB)...
python -m playwright install chromium

if not exist .env (
  copy .env.example .env >nul
  echo Created .env — edit it to add Stripe keys etc.
)

echo.
echo Done.
echo   run-web.bat                    - start dashboard
echo   run-cli.bat B0ABCDE123         - one-off CLI check
echo   run-monitor.bat                - trigger scheduled monitor now
pause
