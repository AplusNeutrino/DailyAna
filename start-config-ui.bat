@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" tools\config_ui.py
  goto :end
)

if exist ".config-ui-venv\Scripts\python.exe" (
  ".config-ui-venv\Scripts\python.exe" tools\config_ui.py
  goto :end
)

where py >nul 2>nul
if %errorlevel%==0 (
  py tools\config_ui.py
  goto :end
)

where python >nul 2>nul
if %errorlevel%==0 (
  python tools\config_ui.py
  goto :end
)

echo Python was not found.
echo Install Python 3.12 or run the project setup script first.
pause

:end
endlocal
