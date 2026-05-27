@echo off
setlocal
cd /d "%~dp0"
if not exist venv\Scripts\python.exe (
  echo [ERROR] venv missing. Run install.bat first.
  pause
  exit /b 1
)
venv\Scripts\python.exe run.py cli %*
