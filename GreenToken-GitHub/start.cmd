@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please follow the installation steps in README.md first.
  echo Expected Python environment: .venv
  pause
  exit /b 1
)
echo Open http://127.0.0.1:8501 in your browser.
echo Keep this window open. Press Ctrl+C to stop.
".venv\Scripts\python.exe" -m streamlit run showcase.py --server.port 8501
if errorlevel 1 pause
