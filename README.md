# PostgreSQL Service Control

> GUI replacement for `pgadmin_service_control.bat` — manage the PostgreSQL
> Windows service, watch its real health, and launch pgAdmin from one window.

**[Русская версия](README.ru.md)**

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-lightgrey)
![License](https://img.shields.io/badge/License-MIT-green)

---

## What it does

| Feature | Description |
|---|---|
| Service control | Start / Stop / Restart via `net` + `sc`, with ground-truth verification after every operation |
| Real health check | Credential-free PostgreSQL wire-protocol probe — detects a "running but wedged" postmaster that `sc query` alone cannot see |
| System tray | Color-coded icon (green/amber/red), one-click operations, notifications toggle |
| Toast notifications | On operation results and health state transitions |
| Audit log | Rotating file log of every operation, transition and notification decision — always in English |
| Config | Single `config.json`, portable-first, editable in-app |
| Languages | English and Russian |
| Dark theme | Light / Dark switchable in-app |

### Health states

The tool cross-checks the Windows service state against a TCP probe of the
PostgreSQL protocol (an `SSLRequest` packet — **no credentials ever touch disk**):

| SCM state | Probe | Health | Meaning |
|---|---|---|---|
| RUNNING | answers | 🟢 HEALTHY | Normal |
| RUNNING | silent | 🟠 RUNNING / NO RESPONSE | Wedged postmaster, wrong port, or firewall |
| STOPPED | silent | 🔴 STOPPED | Down |
| STOPPED | answers | 🟠 STOPPED / PORT BUSY | Something else holds the port |

---

## Screenshots

<!-- Add screenshots here: <img width="643" height="617" alt="PgAdminServiceControl_AFw7tBL8Fd" src="https://github.com/user-attachments/assets/38c07eca-5cef-49f8-90c5-e261f5e1056f" /> <img width="682" height="684" alt="PgAdminServiceControl_FlaVHUsEJ6" src="https://github.com/user-attachments/assets/3f06a700-a203-4502-8a1b-bd32bc824517" /> -->

---

## Requirements

- Windows 10 / 11
- PostgreSQL installed as a Windows service
- Python 3.9+ (only if running from source)

Optional (for tray icon and toasts):

```
pip install pystray Pillow
```

Without them the app still runs — tray and toasts degrade gracefully with a
warning.

---

## Install

### Option A — prebuilt exe (GitHub Release)

1. Download `PgAdminServiceControl.exe` from the [latest release](../../releases).
2. Place it anywhere. `config.json` and `service_control.log` are created
   next to the exe on first run (or in `%LOCALAPPDATA%\PgAdminServiceControl\`
   if the exe folder is not writable).
3. Right-click → **Run as administrator** when you need to control the service.

### Option B — from source

```bat
git clone https://github.com/salk3n/PgAdminServiceControl.git
cd PgAdminServiceControl
python pgadmin_service_control.py
```

### Building your own exe

```bat
build.bat
```

Produces `dist\PgAdminServiceControl.exe` (PyInstaller, no console window).
Drop an `icon.ico` next to `build.bat` to customize the icon.

> **Note:** unsigned PyInstaller executables may trigger SmartScreen / some AV
> engines on first run. That is expected for any unsigned exe that manages
> services.

---

## Keyboard shortcuts

| Key | Action |
|---|---|
| `Alt+1` | Start service |
| `Alt+2` | Stop service |
| `Alt+3` | Restart service |
| `Alt+4` | Refresh status |
| `Alt+5` | Open pgAdmin 4 |
| `Alt+C` | Configuration dialog — also works as `Alt+С` on Cyrillic layouts |
| `Esc` | Exit (minimizes to tray if tray is enabled) |

Shortcuts work regardless of which widget has focus and are intentionally
ignored while the Configuration dialog is open.

---

## Configuration

`config.json` is created automatically next to the executable:

| Key | Default | Description |
|---|---|---|
| `service_name` | `postgresql-x64-18` | Windows service name. Use **Scan** in the dialog to discover installed PostgreSQL services |
| `pgadmin_path` | *(PG 18 default)* | Path to `pgAdmin4.exe`; verified at every startup |
| `host` / `port` | `127.0.0.1` / `5432` | Health probe target — must match `listen_addresses` / `port` in `postgresql.conf` |
| `probe_enabled` | `true` | Enable the protocol health probe |
| `probe_timeout_seconds` | `3` | Probe timeout |
| `auto_refresh` | `true` | Background polling |
| `refresh_interval_seconds` | `5` | Poll interval |
| `operation_timeout_seconds` | `300` | Subprocess timeout. High default: production shutdown checkpoints can take minutes |
| `theme` | `light` | `light` / `dark` |
| `language` | `en` | `en` / `ru` — applies after restart |
| `tray_enabled` | `true` | System tray icon |
| `minimize_to_tray` | `true` | Minimize/close hides to tray |
| `toast_enabled` | `true` | Toast notifications |
| `log_enabled` | `true` | File audit log |
| `log_max_mb` / `log_backups` | `2` / `5` | Log rotation |
| `advanced_enabled` | `false` | **Unlocks SCM write operations** (start type, recovery config) — see Production safety |

---

## Production safety

This tool touches real databases, so it ships deliberately conservative:

- **SCM write operations** (Apply start type, Configure SCM recovery) are
  **locked** until you manually set `"advanced_enabled": true` in
  `config.json`. The flag cannot be toggled from the UI; *Reset to defaults*
  re-locks it. This is a friction gate against accidents, not a security control.
- **No credentials anywhere.** The health probe uses the unauthenticated
  PostgreSQL `SSLRequest` handshake. `config.json` must never contain secrets.
- **No built-in watchdog.** Supervision belongs to Windows itself — the
  *Configure SCM recovery* button sets native service recovery (restart after
  5s / 15s / 60s), which survives reboots and works without this app running.
- **Single instance.** A named mutex prevents double-launch races.

---

## Languages

- Select **Configuration → Behaviour → Language** or set `"language"` in
  `config.json`. Applies after restart.
- The **file audit log is always English** regardless of UI language — by
  design, for grep-ability.
- Raw `sc.exe` / `net.exe` output appears in whatever language Windows uses
  and cannot be translated.

---

## Toasts not showing?

1. A **tray icon must be active** — Windows has no toast channel without one.
   Requires `pystray` + `Pillow` and `tray_enabled: true`.
2. Perform a manual Start/Stop — operations toast on success **and** failure.
3. Check *Settings → System → Notifications* for this app; make sure
   Focus / Do-not-disturb is OFF (Windows 11 silently swallows toasts otherwise).
4. Check the tray overflow area (`^`) — Windows hides new icons there.
5. Search `service_control.log` for `NOTIFY` — every toast decision is logged
   (`Toast dispatched` / `Toast skipped: ...` / `icon.notify failed`).

---

## Runbook: health shows 🟠 RUNNING / NO RESPONSE

SCM says the service is running, but nothing answered the PostgreSQL protocol:

1. Check the newest file in `C:\Program Files\PostgreSQL\18\data\log\`
   (requires administrator) for repeated `FATAL` / `PANIC` lines.
2. Check free disk space on that volume — a full disk stops WAL writes while
   the postmaster stays alive: exactly this amber state.
3. Confirm `host` / `port` in `config.json` match `postgresql.conf`.
4. Only then consider a controlled restart from this tool.

Every operation is recorded in `service_control.log` with a timestamp and
elevation state — check it before and after any incident.

---

## Project structure

```
PgAdminServiceControl/
├── pgadmin_service_control.py   # the entire application
├── pgadmin_service_control.bat  # the original batch script (origin)
├── config.json                  # created on first run
├── requirements.txt
├── build.bat                    # PyInstaller build script
├── CHANGELOG.md
├── README.md
└── README.ru.md
```

---

## Design constraints (won't be added)

- No database credentials, ever
- No query execution / DB browsing (that is pgAdmin's job)
- No in-app scheduler or watchdog (use SCM recovery / Task Scheduler)
- No auto-updater (an elevated auto-updater is a privilege-escalation channel)
- No force-kill of the postmaster (crash semantics + WAL recovery risk)

---

## Credits

Originally a batch script by **salk3n**; ported to Python with health
verification, tray integration and i18n.

## License

[MIT](LICENSE) © salk3n
