@echo off
setlocal
cd /d "%~dp0"
if not exist venv\Scripts\python.exe (
  echo [ERROR] venv missing. Run install.bat first.
  pause
  exit /b 1
)
venv\Scripts\python.exe -c "from kdp_checker.scheduler import _run_checks_once; _run_checks_once()"
pause
