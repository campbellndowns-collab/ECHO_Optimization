@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: PyThrust virtual environment not found.
  echo Expected: %CD%\.venv\Scripts\python.exe
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -c "import streamlit, pandas" >nul 2>&1
if errorlevel 1 (
  echo Drone Optimizer UI dependencies are not installed.
  echo Running one-time setup...
  ".venv\Scripts\python.exe" -m pip install -r requirements_drone_optimizer.txt
  if errorlevel 1 (
    echo.
    echo Setup failed.
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" drone_optimizer\launcher.py

if errorlevel 1 (
  echo.
  echo Drone Optimizer exited with an error.
  pause
)
