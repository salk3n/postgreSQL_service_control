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
echo Build complete: dist\PgAdminServiceControl.exe
popd
pause