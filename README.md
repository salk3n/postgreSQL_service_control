# PostgreSQL Service Control

GUI replacement for `pgadmin_service_control.bat`. Manage the PostgreSQL
Windows service and launch pgAdmin from a single window.

## Running from source

    python pgadmin_service_control.py

Requires Python 3.9+ on Windows. No external dependencies (uses only
stdlib + tkinter, which ships with the Windows Python installer).

## Building a standalone .exe

    build.bat

Requires PyInstaller (`pip install pyinstaller`). Output:
`dist/PgAdminServiceControl.exe`. Place `icon.ico` next to build.bat to
customize the exe icon.

## Configuration

On first run, `config.json` is created next to the exe (or script). If
the directory is not writable (e.g., Program Files without elevation),
it falls back to `%LOCALAPPDATA%\PgAdminServiceControl\config.json`.

```json
{
  "service_name": "postgresql-x64-18",
  "pgadmin_path": "C:\\Program Files\\PostgreSQL\\18\\pgAdmin 4\\runtime\\pgAdmin4.exe",
  "auto_refresh": true,
  "refresh_interval_seconds": 5,
  "operation_timeout_seconds": 60
}