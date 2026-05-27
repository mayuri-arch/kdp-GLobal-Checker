@echo off
setlocal
cd /d "%~dp0"
if not exist venv\Scripts\python.exe (
  echo [ERROR] venv missing. Run install.bat first.
  pause
  exit /b 1
)
start "" http://127.0.0.1:5000
venv\Scripts\python.exe run.py web
