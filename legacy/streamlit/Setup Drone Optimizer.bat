@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Could not find .venv in this PyThrust folder.
  echo Expected: %CD%\.venv\Scripts\python.exe
  pause
  exit /b 1
)

echo Installing/updating Drone Optimizer UI dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements_drone_optimizer.txt
if errorlevel 1 (
  echo.
  echo Setup failed.
  pause
  exit /b 1
)

echo.
echo Setup complete. Double-click "Launch Drone Optimizer.bat".
pause
