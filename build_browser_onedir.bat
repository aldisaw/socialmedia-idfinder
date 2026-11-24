@echo off
setlocal
cd /d "%~dp0"

set "SIF_EDITION=browser"
set "SIF_BUILD_MODE=onedir"
if exist "build_v1.1.0\brw_onedir" rmdir /s /q "build_v1.1.0\brw_onedir"
if exist "dist_v1.1.0\SocialIDFinder_Browser" rmdir /s /q "dist_v1.1.0\SocialIDFinder_Browser"

".venv\Scripts\pyinstaller.exe" --noconfirm --clean --workpath "build_v1.1.0\brw_onedir" --distpath "dist_v1.1.0" "SocialIDFinder.spec"
if errorlevel 1 exit /b 1
echo [OK] Browser ONEDIR: dist_v1.1.0\SocialIDFinder_Browser\SocialIDFinder.exe
endlocal
