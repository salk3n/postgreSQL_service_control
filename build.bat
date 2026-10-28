@echo off
setlocal

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found on PATH.
    pause & exit /b 1
)

echo Installing / verifying build dependencies...
python -m pip install --upgrade pyinstaller pystray Pillow
if errorlevel 1 (
    echo ERROR: Dependency install failed.
    pause & exit /b 1
)

pushd "%~dp0"

set ICON=
if exist icon.ico set ICON=--icon icon.ico

echo.
echo Building standalone executable...
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name PgAdminServiceControl ^
    --collect-submodules pystray ^
    --hidden-import PIL._tkinter_finder ^
    %ICON% ^
    pgadmin_service_control.py

if errorlevel 1 (
    echo ERROR: Build failed.
    popd & pause & exit /b 1
)

echo.
echo ============================================================
echo  Build complete: dist\PgAdminServiceControl.exe
echo.
echo  * config.json is created next to the exe on first run.
echo    If that folder is not writable it falls back to
echo    %%LOCALAPPDATA%%\PgAdminServiceControl\
echo  * Logs go to the same directory: service_control.log
echo  * The exe does NOT force UAC. Right-click -^> "Run as
echo    administrator" when you need to control the service.
echo ============================================================
popd
pause