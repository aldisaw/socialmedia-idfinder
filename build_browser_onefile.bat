@echo off
setlocal
cd /d "%~dp0"

set "SIF_EDITION=browser"
set "SIF_BUILD_MODE=onefile"
if exist "build_v1.1.0\brw_onefile" rmdir /s /q "build_v1.1.0\brw_onefile"
if exist "dist_v1.1.0\SocialIDFinder_Browser.exe" del /f /q "dist_v1.1.0\SocialIDFinder_Browser.exe"

".venv\Scripts\pyinstaller.exe" --noconfirm --clean --workpath "build_v1.1.0\brw_onefile" --distpath "dist_v1.1.0" "SocialIDFinder.spec"
if errorlevel 1 exit /b 1
echo [OK] Browser ONEFILE: dist_v1.1.0\SocialIDFinder_Browser.exe
endlocal
