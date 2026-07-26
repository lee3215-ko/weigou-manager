@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYEXE="
if exist "%LocalAppData%\Programs\Python\Python313\python.exe" set "PYEXE=%LocalAppData%\Programs\Python\Python313\python.exe"
if not defined PYEXE if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PYEXE=%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PYEXE if exist "%LocalAppData%\Programs\Python\Python311\python.exe" set "PYEXE=%LocalAppData%\Programs\Python\Python311\python.exe"

if not defined PYEXE (
  where py >nul 2>nul
  if not errorlevel 1 (
    for /f "delims=" %%I in ('py -3 -c "import sys; print(sys.executable)"') do set "PYEXE=%%I"
  )
)

if not defined PYEXE (
  where python >nul 2>nul
  if not errorlevel 1 (
    for /f "delims=" %%I in ('where python') do (
      if not defined PYEXE set "PYEXE=%%I"
    )
  )
)

if not defined PYEXE (
  echo.
  echo [ERROR] Python not found.
  echo Install from https://www.python.org/downloads/
  echo Enable: Add python.exe to PATH
  echo.
  pause
  exit /b 1
)

echo [manager] using: %PYEXE%
"%PYEXE%" -c "import sys; print(sys.version)"
if errorlevel 1 (
  echo [ERROR] Python failed to start.
  pause
  exit /b 1
)

echo [manager] installing packages...
"%PYEXE%" -m pip install -q -r "%~dp0requirements.txt"
if errorlevel 1 (
  echo [ERROR] pip install failed.
  pause
  exit /b 1
)

echo [manager] ensuring Chromium for Google Lens...
"%PYEXE%" -m playwright install chromium
if errorlevel 1 (
  echo [WARN] playwright chromium install failed. Google image search may not work.
)

echo [manager] starting app...
"%PYEXE%" "%~dp0manager_app.py"
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" (
  echo.
  echo [ERROR] exit code %ERR%
  pause
)
endlocal & exit /b %ERR%
