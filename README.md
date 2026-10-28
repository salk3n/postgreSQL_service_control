# PostgreSQL Service Control

GUI replacement for `pgadmin_service_control.bat`. Manage the PostgreSQL
Windows service and launch pgAdmin from one window.

## Shortcuts

| Key | Action |
|---|---|
| Alt+1 | Start service |
| Alt+2 | Stop service |
| Alt+3 | Restart service |
| Alt+4 | Refresh status |
| Alt+5 | Open pgAdmin 4 |
| Alt+C (Alt+С on Cyrillic layout) | Configuration dialog |
| Esc | Exit (minimises to tray if tray is enabled) |

Keys are bound on the typed character, so Alt+C works on Latin layouts and
Alt+С on Russian layouts (same physical key). Digits are layout-independent.
Shortcuts are ignored while the Configuration dialog is open.
Right-click the tray icon for the one-click **Notifications** toggle.

## Language

- `"language": "en"` or `"ru"` in config.json; also selectable in
  Configuration… -> Behaviour -> Language.
- Changes apply **after restart** (language is fixed for the process lifetime).
- The file audit log (`service_control.log`) is ALWAYS English regardless of
  UI language - keep it that way for grep-ability. The on-screen Output panel
  follows the UI language.
- Raw `sc.exe` / `net.exe` output appears in whatever language Windows uses
  and cannot be translated. Theme values and start-type tokens stay English
  (they are sc.exe arguments).

## When do toasts fire?

- Manual operations (Start / Stop / Restart / Open pgAdmin) on success AND failure.
- Health state transitions (e.g. HEALTHY -> RUNNING / NO RESPONSE), never on
  the first status read.
- Refresh stays silent by design.

## Toasts not showing? Work this list in order.

1. Tray icon must be active (pystray + Pillow installed, tray_enabled=true).
   Windows has no toast channel without a tray icon.
2. Perform a manual Start/Stop - operations toast on success AND failure.
   - Toast appears -> mechanism works; check toast_enabled in config.json
     or the Notifications toggle.
   - No toast -> continue below.
3. Check `Settings -> System -> Notifications` for this app, and make sure
   Focus / Do-not-disturb is OFF.
4. Check the overflow area (^) - Windows hides new tray icons there.
5. Open `service_control.log` and search for `NOTIFY`. Every toast decision
   is logged: `Toast dispatched`, `Toast skipped: ...`, or `icon.notify failed`.

## Elevation

Service start/stop/restart require administrator rights. Without them a
yellow banner warns you; status, health probe and pgAdmin still work.
The exe does NOT force UAC - right-click -> Run as administrator.

## Configuration

`config.json` is created next to the exe on first run; if that directory
is not writable it falls back to
`%LOCALAPPDATA%\PgAdminServiceControl\config.json`.

**Production safety:** SCM write operations (Apply start type, Configure
SCM recovery) are locked unless `"advanced_enabled": true` is set
manually in config.json. This flag cannot be toggled from the UI, and
"Reset to defaults" re-locks it. This is a friction gate against
accidental changes, not a security control.

## Design constraints (do not remove)

- config.json contains NO secrets - never add credentials.
- No query execution, no in-app scheduler/watchdog, no auto-updater.
- Supervision belongs to Windows (SCM recovery), not to this GUI.

## Runbook: health shows "RUNNING / NO RESPONSE"

SCM says running, but nothing answered the PostgreSQL protocol.

1. Check the newest file in `C:\Program Files\PostgreSQL\18\data\log\`
   (requires administrator) for repeated FATAL/PANIC lines.
2. Check free disk space on that volume - a full disk stops WAL writes
   while the postmaster process stays alive: exactly this amber state.
3. Confirm `host`/`port` in config.json match `listen_addresses`/`port`
   in postgresql.conf.
4. Only then consider a controlled restart from this tool.

Every operation is written to `service_control.log` with timestamp and
elevation state - check it before and after any incident.
