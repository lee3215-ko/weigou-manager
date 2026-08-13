@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
)

call ".venv\Scripts\activate.bat"
REM skip pip self-upgrade (often locks broken ~ip leftovers)
pip install -q -r requirements.txt
if errorlevel 1 exit /b 1
pip install -q pyinstaller pillow certifi
if errorlevel 1 exit /b 1

set "PYI_WORK=%TEMP%\weigou-pyi-build"
set "PYI_DIST=%TEMP%\weigou-pyi-dist"
if exist "%PYI_WORK%" rmdir /s /q "%PYI_WORK%" 2>nul
if exist "%PYI_DIST%" rmdir /s /q "%PYI_DIST%" 2>nul

pyinstaller --noconfirm --workpath "%PYI_WORK%" --distpath "%PYI_DIST%" build.spec
if errorlevel 1 exit /b 1

REM Fresh folder each time so Cursor/Explorer locks on release-out cannot block deploy
set RELEASE=dist-out\WeigouManager
if exist dist-out rmdir /s /q dist-out 2>nul
mkdir "%RELEASE%"
robocopy "%PYI_DIST%\WeigouManager" "%RELEASE%" /E /IS /IT /NFL /NDL /NJH /NJS /R:2 /W:1
if %ERRORLEVEL% GEQ 8 exit /b 1

if not exist "%RELEASE%\data" mkdir "%RELEASE%\data"
if not exist "%RELEASE%\bundled" mkdir "%RELEASE%\bundled"
if exist "data\sync_settings.example.json" copy /Y "data\sync_settings.example.json" "%RELEASE%\data\sync_settings.example.json" >nul
REM Cloud credentials: fresh install seed + update bootstrap (robocopy keeps data/)
if exist "data\mall_cloud.json" (
  copy /Y "data\mall_cloud.json" "%RELEASE%\data\mall_cloud.json" >nul
  copy /Y "data\mall_cloud.json" "%RELEASE%\bundled\mall_cloud.json" >nul
)
if exist "data\mall_cloud.example.json" copy /Y "data\mall_cloud.example.json" "%RELEASE%\data\mall_cloud.example.json" >nul
if not exist "%RELEASE%\data\sync_settings.json" (
  >"%RELEASE%\data\sync_settings.json" echo {
  >>"%RELEASE%\data\sync_settings.json" echo   "enabled": true,
  >>"%RELEASE%\data\sync_settings.json" echo   "backend": "supabase",
  >>"%RELEASE%\data\sync_settings.json" echo   "interval_sec": 2,
  >>"%RELEASE%\data\sync_settings.json" echo   "device_name": "",
  >>"%RELEASE%\data\sync_settings.json" echo   "role": "full",
  >>"%RELEASE%\data\sync_settings.json" echo   "prefer_table_sync": true
  >>"%RELEASE%\data\sync_settings.json" echo }
)

echo @echo off> "%RELEASE%\run.bat"
echo cd /d "%%~dp0">> "%RELEASE%\run.bat"
echo start "" "WeigouManager.exe">> "%RELEASE%\run.bat"

endlocal
