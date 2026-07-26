@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
)

call .venv\Scripts\activate.bat
python -m pip install -q --upgrade pip
pip install -q -r requirements.txt
pip install -q pyinstaller pillow certifi

pyinstaller --noconfirm --clean build.spec
if errorlevel 1 exit /b 1

set RELEASE=release\WeigouManager
if exist release rmdir /s /q release
mkdir "%RELEASE%"
xcopy /E /I /Y "dist\WeigouManager\*" "%RELEASE%\" >nul

if not exist "%RELEASE%\data" mkdir "%RELEASE%\data"
if exist "data\sync_settings.example.json" copy /Y "data\sync_settings.example.json" "%RELEASE%\data\sync_settings.example.json" >nul

echo @echo off> "%RELEASE%\run.bat"
echo cd /d "%%~dp0">> "%RELEASE%\run.bat"
echo start "" "WeigouManager.exe">> "%RELEASE%\run.bat"

endlocal
