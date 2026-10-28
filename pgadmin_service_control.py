#!/usr/bin/env python3
"""
PostgreSQL Service Control - GUI edition.
Python port of pgadmin_service_control.bat (by salk3n).

Core invariants:
  * ALL tkinter widget mutation happens on the main thread. Worker threads and
    tray callbacks communicate exclusively through queues.
  * Service state is ground-truth verified: sc query is re-run after start/stop,
    and cross-checked with a credential-free PostgreSQL protocol probe.
  * Subprocesses use CREATE_NO_WINDOW + stdin=DEVNULL (no console flash, no hangs).
  * Optional dependencies degrade gracefully; the app never hard-fails on them.
"""

import ctypes
import json
import logging
import os
import queue
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from tkinter import filedialog, messagebox, ttk

if sys.platform != "win32":
    sys.stderr.write("This tool is Windows-only.\n")
    sys.exit(2)

# --- Optional dependency: tray + toast ------------------------------------
try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
    TRAY_IMPORT_ERROR = None
except ImportError as e:                      # pragma: no cover
    pystray = None
    TRAY_AVAILABLE = False
    TRAY_IMPORT_ERROR = str(e)

APP_USER_MODEL_ID = "salk3n.PgAdminServiceControl"
APP_DIR_NAME = "PgAdminServiceControl"

log = logging.getLogger("pgsvc")

# ---------------------------------------------------------------------------
# Platform constants
# ---------------------------------------------------------------------------

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008

STATE_NAMES = {
    1: "STOPPED", 2: "START_PENDING", 3: "STOP_PENDING", 4: "RUNNING",
    5: "CONTINUE_PENDING", 6: "PAUSE_PENDING", 7: "PAUSED",
}

_STATE_RE = re.compile(r"^\s*STATE\s*:\s*(\d+)", re.IGNORECASE | re.MULTILINE)
_MISSING_RE = re.compile(r"\b1060\b|does not exist", re.IGNORECASE)
_ACCESS_RE = re.compile(r"access is denied|access denied|\berror 5\b", re.IGNORECASE)

PG_SSL_REQUEST_CODE = 80877103


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "service_name": "postgresql-x64-18",
    "pgadmin_path": "C:\\Program Files\\PostgreSQL\\18\\pgAdmin 4\\runtime\\pgAdmin4.exe",
    "host": "127.0.0.1",
    "port": 5432,
    "probe_enabled": True,
    "probe_timeout_seconds": 3,
    "auto_refresh": True,
    "refresh_interval_seconds": 5,
    "operation_timeout_seconds": 60,
    "theme": "light",
    "tray_enabled": True,
    "minimize_to_tray": True,
    "toast_enabled": True,
    "log_enabled": True,
    "log_max_mb": 2,
    "log_backups": 5,
}

VALID_THEMES = ("light", "dark")


def _app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _localappdata_dir():
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return os.path.join(local, APP_DIR_NAME)
    return None


def _config_candidates():
    yield os.path.join(_app_dir(), "config.json")
    lad = _localappdata_dir()
    if lad:
        yield os.path.join(lad, "config.json")


def _is_writable_dir(path):
    """True if path exists (or can be created) and accepts a probe write."""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return False
    probe = os.path.join(path, ".write_test")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("x")
        os.remove(probe)
        return True
    except OSError:
        return False


def resolve_data_dir(preferred=None):
    """First writable directory for logs / fallback config."""
    cands = []
    if preferred:
        cands.append(preferred)
    cands.append(_app_dir())
    lad = _localappdata_dir()
    if lad:
        cands.append(lad)
    cands.append(os.path.join(os.environ.get("TEMP", _app_dir()), APP_DIR_NAME))
    seen = set()
    for c in cands:
        if not c or c in seen:
            continue
        seen.add(c)
        if _is_writable_dir(c):
            return c
    return None


class Config:
    """config.json with validation, portable-first resolution, writable fallback."""

    def __init__(self):
        self.path = None
        self.data_dir = None
        self.warnings = []
        self.data = dict(DEFAULT_CONFIG)
        self.load()

    # -- loading ----------------------------------------------------------
    def load(self):
        self.warnings = []
        self.data = dict(DEFAULT_CONFIG)
        self.path = None

        for cand in _config_candidates():
            if os.path.isfile(cand):
                self.path = cand
                try:
                    with open(cand, "r", encoding="utf-8-sig") as f:
                        raw = json.load(f)
                except (OSError, json.JSONDecodeError) as e:
                    self.warnings.append(f"Could not parse {cand}: {e}. Using defaults.")
                    raw = {}
                if isinstance(raw, dict):
                    self._merge(raw)
                else:
                    self.warnings.append("config.json root must be a JSON object. Using defaults.")
                break
        else:
            self.warnings.append("config.json not found; defaults will be written on first save.")

        self.data_dir = resolve_data_dir(
            os.path.dirname(self.path) if self.path else None
        )
        if self.path is None:
            self.save()

    def _merge(self, raw):
        for key, default in DEFAULT_CONFIG.items():
            if key not in raw:
                continue
            val = raw[key]
            # bool MUST be checked before int (bool is a subclass of int)
            if isinstance(default, bool):
                self.data[key] = bool(val)
            elif isinstance(default, int):
                try:
                    self.data[key] = max(1, int(val))
                except (TypeError, ValueError):
                    self.warnings.append(f"'{key}' must be an integer; kept default.")
            elif isinstance(default, str):
                if isinstance(val, str):
                    self.data[key] = val.strip()
                else:
                    self.warnings.append(f"'{key}' must be a string; kept default.")

        theme = self.data.get("theme")
        if theme not in VALID_THEMES:
            self.warnings.append(f"Unknown theme '{theme}'; using 'light'.")
            self.data["theme"] = "light"

        port = self.data.get("port")
        if not isinstance(port, int) or not (1 <= port <= 65535):
            self.warnings.append(f"Invalid port '{port}'; using 5432.")
            self.data["port"] = 5432

        unknown = set(raw) - set(DEFAULT_CONFIG)
        if unknown:
            self.warnings.append("Ignoring unknown config keys: " + ", ".join(sorted(unknown)))

    # -- saving -----------------------------------------------------------
    def save(self):
        targets = []
        if self.path:
            targets.append(self.path)
        targets.extend(c for c in _config_candidates() if c not in targets)

        for cand in targets:
            try:
                parent = os.path.dirname(cand)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(cand, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, indent=2)
                    f.write("\n")
                if self.path and self.path != cand:
                    self.warnings.append(
                        f"Could not write {self.path}; saved to {cand} instead. "
                        "The portable config.json takes precedence at next launch - "
                        "delete one of them to avoid confusion."
                    )
                self.path = cand
                self.data_dir = resolve_data_dir(os.path.dirname(cand))
                return True
            except OSError:
                continue

        self.warnings.append("Could not write config.json to any known location.")
        return False

    def reset_to_defaults(self):
        self.data = dict(DEFAULT_CONFIG)
        self.warnings = []
        return self.save()

    def get(self, key, default=None):
        return self.data.get(key, default)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(cfg):
    """(Re)configure the rotating file logger. Safe to call repeatedly."""
    log.setLevel(logging.DEBUG)
    log.handlers.clear()          # prevents duplicate handlers on config reload
    log.propagate = False

    if not cfg.get("log_enabled"):
        log.addHandler(logging.NullHandler())
        return None

    data_dir = cfg.data_dir or resolve_data_dir()
    if not data_dir:
        log.addHandler(logging.NullHandler())
        return None

    path = os.path.join(data_dir, "service_control.log")
    try:
        handler = RotatingFileHandler(
            path,
            maxBytes=max(1, int(cfg.get("log_max_mb", 2))) * 1024 * 1024,
            backupCount=max(0, int(cfg.get("log_backups", 5))),
            encoding="utf-8",
        )
    except OSError as e:
        cfg.warnings.append(f"Could not open log file {path}: {e}. Logging disabled.")
        log.addHandler(logging.NullHandler())
        return None

    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S"
    ))
    handler.setLevel(logging.INFO)
    log.addHandler(handler)
    return path


# ---------------------------------------------------------------------------
# Health probe (credential-free PostgreSQL wire protocol check)
# ---------------------------------------------------------------------------

def probe_postgres(host, port, timeout=3.0):
    """Send an SSLRequest packet; a live postmaster replies b'S' or b'N'.

    Requires no credentials and performs no authentication.
    Returns (alive: bool|None, detail: str). alive is None if probing is
    impossible (no host configured).
    """
    if not host:
        return None, "probe skipped (no host configured)"
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None, f"probe skipped (invalid port {port!r})"

    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(struct.pack("!ii", 8, PG_SSL_REQUEST_CODE))
            resp = s.recv(1)
            if resp in (b"S", b"N"):
                return True, f"postmaster responsive on {host}:{port} (SSL reply {resp!r})"
            if not resp:
                return False, f"connection closed by {host}:{port} without reply"
            return False, f"unexpected protocol reply from {host}:{port}: {resp!r}"
    except socket.timeout:
        return False, f"timeout after {timeout}s contacting {host}:{port}"
    except ConnectionRefusedError:
        return False, f"connection refused by {host}:{port} (nothing listening)"
    except OSError as e:
        return False, f"probe error on {host}:{port}: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Service discovery (registry - stdlib, instant, no console)
# ---------------------------------------------------------------------------

def discover_postgres_services(hints=("postgres", "postgresql", "pgsql")):
    """Enumerate installed services that look like PostgreSQL.

    Reads HKLM\\SYSTEM\\CurrentControlSet\\Services (no admin required).
    Returns [(service_name, display_name), ...] sorted by name.

    CAVEAT: the registry lists *installed* services, including leftovers from
    uninstalled versions. Callers must validate with sc query before use.
    """
    found = {}
    root_path = r"SYSTEM\CurrentControlSet\Services"
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root_path)
    except OSError as e:
        log.warning("Service discovery failed to open registry: %s", e)
        return []

    try:
        i = 0
        while True:
            try:
                svc = winreg.EnumKey(root, i)
                i += 1
            except OSError:
                break

            disp, image = svc, ""
            try:
                k = winreg.OpenKey(root, svc)
                try:
                    disp = winreg.QueryValueEx(k, "DisplayName")[0] or svc
                except OSError:
                    pass
                try:
                    image = str(winreg.QueryValueEx(k, "ImagePath")[0] or "")
                except OSError:
                    pass
                finally:
                    k.Close()
            except OSError:
                pass

            hay = f"{svc} {disp} {image}".lower()
            if any(h in hay for h in hints):
                found[svc] = disp
    finally:
        root.Close()

    return sorted(found.items(), key=lambda kv: kv[0].lower())


# ---------------------------------------------------------------------------
# Service control
# ---------------------------------------------------------------------------

class ServiceError(Exception):
    """Raised when a service operation cannot be attempted at all."""


@dataclass
class OpResult:
    ok: bool
    summary: str
    detail: str = ""


def _decode(raw):
    """sc/net emit OEM codepage (cp437/cp850), not UTF-8."""
    if not raw:
        return ""
    for enc in ("utf-8", "cp437", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


class ServiceController:
    def __init__(self, service_name, timeout=60):
        self.service_name = service_name
        self.timeout = timeout

    def _run(self, args):
        try:
            proc = subprocess.run(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=self.timeout,
            )
        except FileNotFoundError:
            raise ServiceError(f"Command not found: {args[0]}")
        except subprocess.TimeoutExpired:
            raise ServiceError(f"Command timed out after {self.timeout}s: {' '.join(args)}")
        except OSError as e:
            raise ServiceError(f"Could not execute {args[0]}: {e}")
        return proc.returncode, _decode(proc.stdout), _decode(proc.stderr)

    def query_raw(self):
        rc, out, err = self._run(["sc", "query", self.service_name])
        return rc, (out + "\n" + err).strip()

    def query_state(self):
        """Returns (state_name|None, raw). Raises ServiceError if unusable."""
        rc, raw = self.query_raw()
        if rc == 0:
            m = _STATE_RE.search(raw)
            if m:
                code = int(m.group(1))
                return STATE_NAMES.get(code, f"UNKNOWN({code})"), raw
            return None, raw
        if _MISSING_RE.search(raw) or rc == 1060:
            raise ServiceError(
                f"Service '{self.service_name}' does not exist. Check configuration."
            )
        if _ACCESS_RE.search(raw) or rc == 5:
            raise ServiceError("Access denied. Run this tool as administrator.")
        return None, raw or f"sc query exited with code {rc}"

    def status(self):
        state, raw = self.query_state()
        if state:
            return OpResult(True, f"Service state: {state}", raw)
        return OpResult(True, "Service status (raw):", raw)

    def _classify_failure(self, rc, detail, new_state, verb, wanted):
        if new_state == wanted:
            return OpResult(True, f"Service is {wanted}.", detail or f"(net {verb} produced output but the service reached {wanted}.)")
        if _ACCESS_RE.search(detail) or rc == 5:
            return OpResult(False, "Access denied - administrator rights required.", detail)
        if _MISSING_RE.search(detail) or rc == 1060:
            return OpResult(False, f"Service '{self.service_name}' not found. Check configuration.", detail)
        return OpResult(
            False,
            f"{verb.title()} failed (exit code {rc}). Current state: {new_state or 'unknown'}.",
            detail,
        )

    def _new_state(self):
        try:
            return self.query_state()[0]
        except ServiceError:
            return None

    def start(self):
        state, _ = self.query_state()
        if state == "RUNNING":
            return OpResult(True, "Service is already RUNNING - nothing to do.")
        rc, out, err = self._run(["net", "start", self.service_name])
        return self._classify_failure(rc, (out + "\n" + err).strip(), self._new_state(), "start", "RUNNING")

    def stop(self):
        state, _ = self.query_state()
        if state == "STOPPED":
            return OpResult(True, "Service is already STOPPED - nothing to do.")
        rc, out, err = self._run(["net", "stop", self.service_name])
        return self._classify_failure(rc, (out + "\n" + err).strip(), self._new_state(), "stop", "STOPPED")

    def restart(self):
        state, _ = self.query_state()
        lines = []
        if state == "RUNNING":
            r = self.stop()
            lines.append(f"STOP  : {r.summary}")
            if not r.ok:
                return OpResult(False, "Restart aborted - stop failed.\n" + r.summary, r.detail)
        else:
            lines.append(f"STOP  : skipped (state was {state or 'unknown'})")
        r = self.start()
        lines.append(f"START : {r.summary}")
        return OpResult(r.ok, "Restart complete.\n" + "\n".join(lines), r.detail)

    def configure_recovery(self):
        """Native SCM recovery. Survives reboots; runs without this app.
        NOTE: sc.exe requires the literal space after 'actions='."""
        rc, out, err = self._run([
            "sc", "failure", self.service_name,
            "reset=", "86400",
            "actions=", "restart/5000/restart/15000/restart/60000",
        ])
        detail = (out + "\n" + err).strip()
        if rc == 0:
            return OpResult(True, "SCM recovery configured: restart after 5s / 15s / 60s, counter resets daily.", detail)
        if _ACCESS_RE.search(detail) or rc == 5:
            return OpResult(False, "Access denied - administrator rights required.", detail)
        return OpResult(False, f"sc failure failed (exit code {rc}).", detail)

    def set_start_type(self, start_type):
        """start_type: 'auto' | 'demand' | 'disabled'"""
        if start_type not in ("auto", "demand", "disabled"):
            return OpResult(False, f"Invalid start type: {start_type}")
        rc, out, err = self._run([
            "sc", "config", self.service_name, "start=", start_type,
        ])
        detail = (out + "\n" + err).strip()
        if rc == 0:
            return OpResult(True, f"Start type set to '{start_type}'.", detail)
        if _ACCESS_RE.search(detail) or rc == 5:
            return OpResult(False, "Access denied - administrator rights required.", detail)
        return OpResult(False, f"sc config failed (exit code {rc}).", detail)


# ---------------------------------------------------------------------------
# Health classification (SCM x probe)
# ---------------------------------------------------------------------------

@dataclass
class Health:
    key: str
    label: str
    tone: str          # palette key: ok / warn / err / muted
    scm_state: str = ""
    probe_alive: object = None
    detail: str = ""
    error: str = ""

    @property
    def ok(self):
        return self.tone == "ok"


def classify_health(scm_state, probe_alive, probe_enabled, scm_error=""):
    if scm_error:
        return Health("error", "ERROR", "err", scm_state or "", probe_alive, "", scm_error)

    if not scm_state:
        return Health("unknown", "UNKNOWN", "muted", "", probe_alive,
                      "Could not parse sc.exe output.", "")

    pending = scm_state.endswith("_PENDING")

    if not probe_enabled or probe_alive is None:
        tone = "muted" if pending else ("ok" if scm_state == "RUNNING" else "err")
        suffix = "" if probe_enabled else " (probe off)"
        return Health(
            scm_state + suffix,
            scm_state.replace("_", " ").title() + suffix,
            tone, scm_state, probe_alive,
            "Health reflects SCM state only; protocol probe disabled.",
        )

    if pending:
        return Health("pending", scm_state.replace("_", " ").title(), "warn",
                      scm_state, probe_alive, "Service is transitioning.", "")

    if scm_state == "RUNNING" and probe_alive:
        return Health("healthy", "HEALTHY", "ok", scm_state, probe_alive,
                      "SCM reports RUNNING and postmaster answers the wire protocol.", "")
    if scm_state == "RUNNING" and not probe_alive:
        return Health("wedged", "RUNNING / NO RESPONSE", "warn", scm_state, probe_alive,
                      "SCM reports RUNNING but nothing answered the PostgreSQL protocol. "
                      "Possible wedged postmaster, wrong host/port, or firewall.", "")
    if scm_state == "STOPPED" and not probe_alive:
        return Health("down", "STOPPED", "err", scm_state, probe_alive,
                      "Service stopped and port not listening.", "")
    if scm_state == "STOPPED" and probe_alive:
        return Health("port_busy", "STOPPED / PORT BUSY", "warn", scm_state, probe_alive,
                      "Service is stopped but something else answers on that port.", "")
    # PAUSED and other combinations
    return Health(scm_state.lower(), scm_state.replace("_", " ").title(), "warn",
                  scm_state, probe_alive, "", "")


# ---------------------------------------------------------------------------
# Snapshot worker payload
# ---------------------------------------------------------------------------

@dataclass
class Snapshot:
    health: Health
    raw: str = ""


@dataclass
class OpOutcome:
    action: str
    result: OpResult
    snapshot: object = None


# ---------------------------------------------------------------------------
# pgAdmin launcher
# ---------------------------------------------------------------------------

def launch_pgadmin(path):
    if not path:
        return OpResult(False, "pgadmin_path is not set in configuration.")
    expanded = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isfile(expanded):
        return OpResult(False, f"pgAdmin executable not found:\n{expanded}")
    try:
        subprocess.Popen(
            [expanded],
            cwd=os.path.dirname(expanded),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
        )
    except OSError as e:
        return OpResult(False, f"Failed to launch pgAdmin: {e}")
    return OpResult(True, f"Launched pgAdmin:\n{expanded}")


# ---------------------------------------------------------------------------
# Elevation
# ---------------------------------------------------------------------------

def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def request_elevation():
    if getattr(sys, "frozen", False):
        target, params = sys.executable, ""
    else:
        target = sys.executable
        params = '"' + os.path.abspath(__file__) + '"'
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", target, params, None, 1)
    except OSError:
        return False
    return bool(rc) and rc > 32


# ---------------------------------------------------------------------------
# Themes
# ---------------------------------------------------------------------------

LIGHT = {
    "bg": "#f3f3f3", "fg": "#1a1a1a", "card": "#ffffff", "card_fg": "#1a1a1a",
    "accent": "#0067c0", "entry": "#ffffff", "entry_fg": "#1a1a1a",
    "border": "#c8c8c8", "muted": "#8a8a8a", "sel": "#cce4f7",
    "ok": "#1a7f37", "warn": "#b26a00", "err": "#b3261e",
    "log_bg": "#ffffff", "log_fg": "#1a1a1a",
    "banner_bg": "#fff3cd", "banner_fg": "#663c00",
    "raw": "#4a4a4a",
}

DARK = {
    "bg": "#1e1e1e", "fg": "#e8e8e8", "card": "#2b2b2b", "card_fg": "#e8e8e8",
    "accent": "#4cc2ff", "entry": "#333333", "entry_fg": "#e8e8e8",
    "border": "#3f3f3f", "muted": "#9a9a9a", "sel": "#264f78",
    "ok": "#4ec96a", "warn": "#e2a336", "err": "#f2555a",
    "log_bg": "#181818", "log_fg": "#dcdcdc",
    "banner_bg": "#4a3b12", "banner_fg": "#ffd97a",
    "raw": "#b8b8b8",
}


# ---------------------------------------------------------------------------
# Config dialog
# ---------------------------------------------------------------------------

class ConfigDialog(tk.Toplevel):
    """In-app editor for config.json. Modal; writes through Config.save()."""

    def __init__(self, master, cfg):
        super().__init__(master)
        self.cfg = cfg
        self.saved = False
        self.title("Configuration")
        self.resizable(False, False)
        self.transient(master)

        self.vars = {}
        self._build()
        self._center(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _center(self, master):
        self.update_idletasks()
        try:
            x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
            y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 2
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except tk.TclError:
            pass

    def _build(self):
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        # --- Service ------------------------------------------------------
        svc = ttk.LabelFrame(outer, text="Service")
        svc.pack(fill="x", pady=(0, 8))

        row = ttk.Frame(svc, padding=(8, 6))
        row.pack(fill="x")
        ttk.Label(row, text="Service name:", width=18, anchor="w").pack(side="left")
        self.vars["service_name"] = tk.StringVar(value=self.cfg.get("service_name"))
        self.svc_combo = ttk.Combobox(row, textvariable=self.vars["service_name"], width=42)
        self.svc_combo.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Scan", width=8, command=self._scan).pack(side="left", padx=(6, 0))

        row = ttk.Frame(svc, padding=(8, 0, 8, 4))
        row.pack(fill="x")
        ttk.Label(row, text="Start type:", width=18, anchor="w").pack(side="left")
        self.start_type_var = tk.StringVar(value="(query)")
        self.st_combo = ttk.Combobox(
            row, textvariable=self.start_type_var, width=14, state="readonly",
            values=["(query)", "auto", "demand", "disabled"],
        )
        self.st_combo.pack(side="left")
        ttk.Button(row, text="Apply start type", command=self._apply_start_type).pack(side="left", padx=6)
        ttk.Button(row, text="Configure SCM recovery", command=self._recovery).pack(side="left")

        # --- Probe --------------------------------------------------------
        pr = ttk.LabelFrame(outer, text="Health probe (PostgreSQL wire protocol, no credentials)")
        pr.pack(fill="x", pady=(0, 8))

        row = ttk.Frame(pr, padding=(8, 6))
        row.pack(fill="x")
        self.vars["probe_enabled"] = tk.BooleanVar(value=bool(self.cfg.get("probe_enabled")))
        ttk.Checkbutton(row, text="Enable probe", variable=self.vars["probe_enabled"]).pack(side="left")
        ttk.Label(row, text="Host:").pack(side="left", padx=(16, 4))
        self.vars["host"] = tk.StringVar(value=str(self.cfg.get("host") or ""))
        ttk.Entry(row, textvariable=self.vars["host"], width=18).pack(side="left")
        ttk.Label(row, text="Port:").pack(side="left", padx=(12, 4))
        self.vars["port"] = tk.StringVar(value=str(self.cfg.get("port")))
        ttk.Entry(row, textvariable=self.vars["port"], width=7).pack(side="left")
        ttk.Label(row, text="Timeout (s):").pack(side="left", padx=(12, 4))
        self.vars["probe_timeout_seconds"] = tk.StringVar(value=str(self.cfg.get("probe_timeout_seconds")))
        ttk.Entry(row, textvariable=self.vars["probe_timeout_seconds"], width=5).pack(side="left")

        # --- pgAdmin ------------------------------------------------------
        pa = ttk.LabelFrame(outer, text="pgAdmin")
        pa.pack(fill="x", pady=(0, 8))
        row = ttk.Frame(pa, padding=(8, 6))
        row.pack(fill="x")
        self.vars["pgadmin_path"] = tk.StringVar(value=self.cfg.get("pgadmin_path"))
        ttk.Entry(row, textvariable=self.vars["pgadmin_path"]).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse\u2026", width=10, command=self._browse).pack(side="left", padx=(6, 0))

        # --- Behaviour ----------------------------------------------------
        bh = ttk.LabelFrame(outer, text="Behaviour")
        bh.pack(fill="x", pady=(0, 8))

        grid = ttk.Frame(bh, padding=(8, 6))
        grid.pack(fill="x")

        self.vars["auto_refresh"] = tk.BooleanVar(value=bool(self.cfg.get("auto_refresh")))
        self.vars["minimize_to_tray"] = tk.BooleanVar(value=bool(self.cfg.get("minimize_to_tray")))
        self.vars["toast_enabled"] = tk.BooleanVar(value=bool(self.cfg.get("toast_enabled")))
        self.vars["log_enabled"] = tk.BooleanVar(value=bool(self.cfg.get("log_enabled")))

        ttk.Checkbutton(grid, text="Auto-refresh status", variable=self.vars["auto_refresh"]).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Checkbutton(grid, text="Minimize to tray", variable=self.vars["minimize_to_tray"]).grid(row=0, column=1, sticky="w", pady=2, padx=(20, 0))

        self.vars["tray_enabled"] = tk.BooleanVar(value=bool(self.cfg.get("tray_enabled")))
        self.tray_chk = ttk.Checkbutton(grid, text="System tray icon", variable=self.vars["tray_enabled"])
        self.tray_chk.grid(row=1, column=0, sticky="w", pady=2)
        if not TRAY_AVAILABLE:
            self.tray_chk.state(["disabled"])
            self.vars["tray_enabled"].set(False)

        ttk.Label(grid, text="Theme:").grid(row=1, column=1, sticky="e", padx=(20, 4), pady=2)
        self.vars["theme"] = tk.StringVar(value=self.cfg.get("theme"))
        ttk.Combobox(grid, textvariable=self.vars["theme"], width=8, state="readonly",
                     values=list(VALID_THEMES)).grid(row=1, column=2, sticky="w", pady=2)

        row = ttk.Frame(bh, padding=(8, 0, 8, 6))
        row.pack(fill="x")
        for label, key, width in (
            ("Refresh (s):", "refresh_interval_seconds", 5),
            ("Op timeout (s):", "operation_timeout_seconds", 6),
            ("Log MB:", "log_max_mb", 5),
            ("Backups:", "log_backups", 5),
        ):
            ttk.Label(row, text=label).pack(side="left", padx=(0 if label.startswith("Refresh") else 12, 4))
            self.vars[key] = tk.StringVar(value=str(self.cfg.get(key)))
            ttk.Entry(row, textvariable=self.vars[key], width=width).pack(side="left")

        if not TRAY_AVAILABLE:
            warn = tk.Label(
                outer, anchor="w", justify="left", wraplength=560,
                text=f"pystray / Pillow not available ({TRAY_IMPORT_ERROR}).\n"
                     "Tray icon and toast notifications are disabled. "
                     "Install with: pip install pystray Pillow",
                font=("Segoe UI", 8),
            )
            warn.pack(fill="x", pady=(0, 6))

        # --- Buttons ------------------------------------------------------
        btns = ttk.Frame(outer)
        btns.pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="Reset to defaults", command=self._reset).pack(side="left")
        ttk.Button(btns, text="Open config.json", command=self._open_file).pack(side="left", padx=6)
        ttk.Button(btns, text="Cancel", command=self._cancel).pack(side="right")
        ttk.Button(btns, text="Apply", command=self._apply).pack(side="right", padx=6)

    # -- actions ----------------------------------------------------------
    def _scan(self):
        self._set_busy("Scanning registry for PostgreSQL services\u2026")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        try:
            services = discover_postgres_services()
        except Exception as e:
            services = []
            err = f"{type(e).__name__}: {e}"
        else:
            err = None
        self.after(0, lambda: self._scan_done(services, err))

    def _scan_done(self, services, err):
        self._set_busy("")
        if err:
            messagebox.showerror("Scan failed", err, parent=self)
            return
        if not services:
            messagebox.showinfo(
                "Scan", "No PostgreSQL-like services found in the registry.", parent=self
            )
            return
        names = [n for n, _ in services]
        self.svc_combo.configure(values=names)
        listing = "\n".join(f"{n}  -  {d}" for n, d in services)
        log.info("Service discovery found %d candidate(s): %s", len(services), ", ".join(names))
        messagebox.showinfo(
            "Scan complete",
            f"Found {len(services)} service(s):\n\n{listing}\n\n"
            "Select one from the dropdown.\n"
            "Note: the registry also lists leftovers from uninstalled versions.",
            parent=self,
        )

    def _browse(self):
        initial = self.vars["pgadmin_path"].get() or "C:\\Program Files"
        path = filedialog.askopenfilename(
            parent=self, title="Select pgAdmin4.exe",
            initialdir=os.path.dirname(initial) if os.path.isdir(os.path.dirname(initial)) else "C:\\",
            filetypes=[("pgAdmin", "pgAdmin4.exe"), ("Executables", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self.vars["pgadmin_path"].set(path)

    def _apply_start_type(self):
        st = self.start_type_var.get()
        if st == "(query)":
            self._query_start_type()
            return
        svc = self.vars["service_name"].get().strip()
        if not svc:
            messagebox.showwarning("Start type", "Service name is empty.", parent=self)
            return
        threading.Thread(target=self._op_worker,
                         args=("set_start_type", svc, st), daemon=True).start()
        self._set_busy(f"Setting start type to '{st}'\u2026")

    def _query_start_type(self):
        svc = self.vars["service_name"].get().strip()
        if not svc:
            return
        threading.Thread(target=self._op_worker,
                         args=("query_start_type", svc, None), daemon=True).start()
        self._set_busy("Querying start type\u2026")

    def _recovery(self):
        svc = self.vars["service_name"].get().strip()
        if not svc:
            messagebox.showwarning("Recovery", "Service name is empty.", parent=self)
            return
        if not messagebox.askyesno(
            "Configure SCM recovery",
            f"Configure Windows to auto-restart '{svc}' on failure?\n\n"
            "  1st failure: restart after 5s\n"
            "  2nd failure: restart after 15s\n"
            "  later failures: restart after 60s\n"
            "  failure count resets after 24h\n\n"
            "This changes the service configuration and requires administrator rights.",
            parent=self,
        ):
            return
        threading.Thread(target=self._op_worker,
                         args=("recovery", svc, None), daemon=True).start()
        self._set_busy("Configuring SCM recovery\u2026")

    def _op_worker(self, kind, svc, arg):
        ctrl = ServiceController(svc, 30)
        try:
            if kind == "set_start_type":
                res = ctrl.set_start_type(arg)
            elif kind == "recovery":
                res = ctrl.configure_recovery()
            else:
                res = self._read_start_type(svc)
        except ServiceError as e:
            res = OpResult(False, str(e))
        except Exception as e:
            res = OpResult(False, f"{type(e).__name__}: {e}")
        self.after(0, lambda: self._op_done(kind, res))

    def _read_start_type(self, svc):
        try:
            k = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                rf"SYSTEM\CurrentControlSet\Services\{svc}",
            )
            try:
                start = int(winreg.QueryValueEx(k, "Start")[0])
            finally:
                k.Close()
        except OSError as e:
            return OpResult(False, f"Could not read start type: {e}")
        names = {0: "boot", 1: "system", 2: "auto", 3: "demand", 4: "disabled"}
        name = names.get(start, f"unknown({start})")
        return OpResult(True, f"Current start type: {name}", f"Registry Start value = {start}")

    def _op_done(self, kind, res):
        self._set_busy("")
        log.info("config-dialog %s -> ok=%s : %s", kind, res.ok, res.summary)
        if kind == "query_start_type" and res.ok:
            raw = res.detail
            m = re.search(r"=\s*(\d+)", raw)
            if m:
                names = {"0": "boot", "1": "system", "2": "auto", "3": "demand", "4": "disabled"}
                self.start_type_var.set(names.get(m.group(1), "(query)"))
        if res.ok:
            messagebox.showinfo("Success", res.summary + ("\n\n" + res.detail if res.detail else ""), parent=self)
        else:
            messagebox.showerror("Failed", res.summary + ("\n\n" + res.detail if res.detail else ""), parent=self)

    def _set_busy(self, text):
        self.master._set_status(text) if text else None

    def _reset(self):
        if not messagebox.askyesno(
            "Reset", "Reset all settings to built-in defaults and save?", parent=self
        ):
            return
        self.cfg.reset_to_defaults()
        self.saved = True
        self.destroy()

    def _open_file(self):
        if not self.cfg.path:
            self.cfg.save()
        if not self.cfg.path or not os.path.isfile(self.cfg.path):
            messagebox.showerror("Config", "Could not locate or create config.json.", parent=self)
            return
        try:
            os.startfile(self.cfg.path)
        except OSError as e:
            messagebox.showerror("Config", f"Could not open:\n{self.cfg.path}\n{e}", parent=self)

    def _collect(self):
        """Validate dialog fields into a config dict. Raises ValueError on bad input."""
        out = dict(self.cfg.data)
        svc = self.vars["service_name"].get().strip()
        if not svc:
            raise ValueError("Service name must not be empty.")
        out["service_name"] = svc
        out["pgadmin_path"] = self.vars["pgadmin_path"].get().strip()
        out["host"] = self.vars["host"].get().strip()

        for key, label, lo, hi in (
            ("port", "Port", 1, 65535),
            ("probe_timeout_seconds", "Probe timeout", 1, 60),
            ("refresh_interval_seconds", "Refresh interval", 1, 3600),
            ("operation_timeout_seconds", "Operation timeout", 5, 600),
            ("log_max_mb", "Log size MB", 1, 512),
            ("log_backups", "Log backups", 0, 99),
        ):
            try:
                v = int(str(self.vars[key].get()).strip())
            except (TypeError, ValueError):
                raise ValueError(f"{label} must be a whole number.")
            if not (lo <= v <= hi):
                raise ValueError(f"{label} must be between {lo} and {hi}.")
            out[key] = v

        for key in ("probe_enabled", "auto_refresh", "tray_enabled",
                    "minimize_to_tray", "toast_enabled", "log_enabled"):
            out[key] = bool(self.vars[key].get())

        out["theme"] = self.vars["theme"].get()
        if out["theme"] not in VALID_THEMES:
            raise ValueError(f"Theme must be one of {VALID_THEMES}.")

        if out["tray_enabled"] and not TRAY_AVAILABLE:
            raise ValueError("System tray requested but pystray/Pillow are not installed.")
        return out

    def _apply(self):
        try:
            new = self._collect()
        except ValueError as e:
            messagebox.showerror("Invalid configuration", str(e), parent=self)
            return
        old = dict(self.cfg.data)
        self.cfg.data = new
        self.cfg.warnings = []
        if not self.cfg.save():
            messagebox.showerror(
                "Save failed",
                "Could not write config.json to any known location.\n\n"
                + "\n".join(self.cfg.warnings),
                parent=self,
            )
            self.cfg.data = old
            return
        self.saved = True
        log.info("Configuration updated via dialog; saved to %s", self.cfg.path)
        self.destroy()

    def _cancel(self):
        self.grab_release()
        self.destroy()


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PostgreSQL Service Control")
        self.geometry("680x600")
        self.minsize(600, 520)

        self.cfg = Config()
        self.log_path = setup_logging(self.cfg)
        log.info("=" * 70)
        log.info("Startup | admin=%s | frozen=%s | python=%s | config=%s",
                 is_admin(), getattr(sys, "frozen", False),
                 sys.version.split()[0], self.cfg.path)
        if not TRAY_AVAILABLE:
            log.warning("pystray/Pillow unavailable (%s) - tray and toasts disabled.",
                        TRAY_IMPORT_ERROR)

        self.theme = self.cfg.get("theme", "light")
        self.pal = DARK if self.theme == "dark" else LIGHT

        self.busy = False
        self._polling = False
        self._results = queue.Queue()
        self._states = queue.Queue()
        self._cmds = queue.Queue()
        self._refresh_job = None
        self._last_health_key = None
        self._health_seeded = False
        self._hidden = False
        self._closing = False

        self.icon = None
        self.icon_color = None

        self._build_style()
        self._build_widgets()
        self._apply_theme()
        self._log_startup_warnings()
        self._update_admin_banner()
        self._build_tray()

        self.after(100, self._drain_queues)
        self._schedule_refresh(immediate=True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Unmap>", self._on_unmap)

    # -- style / theme ----------------------------------------------------
    def _build_style(self):
        self.style = ttk.Style(self)

    def _apply_theme(self):
        pal = self.pal
        dark = self.theme == "dark"
        base = "clam" if dark else "vista"
        try:
            self.style.theme_use(base)
        except tk.TclError:
            try:
                self.style.theme_use("clam")
                dark = True
            except tk.TclError:
                pass

        self.style.configure("TButton", padding=(8, 4))
        self.style.configure("TLabelframe.Label", font=("Segoe UI", 9, "bold"))

        if dark or self.style.theme_use() == "clam":
            self.style.configure(".", background=pal["bg"], foreground=pal["fg"],
                                 fieldbackground=pal["entry"], bordercolor=pal["border"],
                                 lightcolor=pal["card"], darkcolor=pal["card"],
                                 selectbackground=pal["sel"], selectforeground=pal["fg"])
            self.style.configure("TFrame", background=pal["bg"])
            self.style.configure("TLabel", background=pal["bg"], foreground=pal["fg"])
            self.style.configure("TLabelframe", background=pal["bg"], bordercolor=pal["border"])
            self.style.configure("TLabelframe.Label", background=pal["bg"], foreground=pal["fg"])
            self.style.configure("TButton", background=pal["card"], foreground=pal["card_fg"],
                                 bordercolor=pal["border"], focuscolor=pal["accent"])
            self.style.map("TButton",
                           background=[("active", pal["accent"]), ("disabled", pal["card"])],
                           foreground=[("disabled", pal["muted"])])
            self.style.configure("TCheckbutton", background=pal["bg"], foreground=pal["fg"],
                                 focuscolor=pal["accent"])
            self.style.map("TCheckbutton", background=[("active", pal["bg"])],
                           indicatorcolor=[("selected", pal["accent"]), ("!selected", pal["entry"])])
            self.style.configure("TCombobox", fieldbackground=pal["entry"], background=pal["card"],
                                 foreground=pal["entry_fg"], arrowcolor=pal["fg"],
                                 bordercolor=pal["border"])
            self.style.map("TCombobox",
                           fieldbackground=[("readonly", pal["entry"])],
                           foreground=[("readonly", pal["entry_fg"])],
                           selectbackground=[("readonly", pal["entry"])],
                           selectforeground=[("readonly", pal["entry_fg"])])
            self.option_add("*TCombobox*Listbox.background", pal["entry"])
            self.option_add("*TCombobox*Listbox.foreground", pal["entry_fg"])
            self.option_add("*TCombobox*Listbox.selectBackground", pal["sel"])
            self.style.configure("Vertical.TScrollbar", background=pal["card"],
                                 troughcolor=pal["bg"], bordercolor=pal["border"],
                                 arrowcolor=pal["fg"])
            self.style.map("Vertical.TScrollbar", background=[("active", pal["accent"])])

        # tk (non-ttk) widgets always need explicit colors
        self.configure(bg=pal["bg"])
        self.statusbar.config(bg=pal["card"], fg=pal["fg"], relief="sunken")
        self.status_indicator.config(bg=pal["bg"])
        self.admin_label.config(bg=pal["banner_bg"], fg=pal["banner_fg"])
        self.log.config(bg=pal["log_bg"], fg=pal["log_fg"],
                        insertbackground=pal["fg"], selectbackground=pal["sel"],
                        selectforeground=pal["fg"])
        self.log.tag_configure("hdr", foreground=pal["muted"], font=("Consolas", 9, "bold"))
        self.log.tag_configure("ok", foreground=pal["ok"])
        self.log.tag_configure("err", foreground=pal["err"])
        self.log.tag_configure("warn", foreground=pal["warn"])
        self.log.tag_configure("raw", foreground=pal["raw"])

    def _set_theme(self, name):
        if name not in VALID_THEMES or name == self.theme:
            return
        self.theme = name
        self.pal = DARK if name == "dark" else LIGHT
        self._apply_theme()
        log.info("Theme switched to '%s'", name)

    # -- widgets ----------------------------------------------------------
    def _build_widgets(self):
        self.statusbar = tk.Label(self, text="Ready.", anchor="w", padx=6, pady=3)
        self.statusbar.pack(fill="x", side="bottom")

        self.body = ttk.Frame(self)
        self.body.pack(side="top", fill="both", expand=True)

        # Service info
        self.info_frame = ttk.LabelFrame(self.body, text="Service")
        self.info_frame.pack(fill="x", padx=10, pady=(8, 4))

        row1 = ttk.Frame(self.info_frame)
        row1.pack(fill="x", padx=8, pady=4)
        ttk.Label(row1, text="Name:", width=9, anchor="w").pack(side="left")
        self.service_name_var = tk.StringVar(value=self.cfg.get("service_name"))
        ttk.Label(row1, textvariable=self.service_name_var,
                  font=("Consolas", 10, "bold")).pack(side="left")

        row2 = ttk.Frame(self.info_frame)
        row2.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Label(row2, text="Health:", width=9, anchor="w").pack(side="left")
        self.status_indicator = tk.Label(row2, text=" \u25cf ", font=("Segoe UI", 14, "bold"), fg="#888888")
        self.status_indicator.pack(side="left", padx=(0, 4))
        self.status_var = tk.StringVar(value="checking\u2026")
        ttk.Label(row2, textvariable=self.status_var,
                  font=("Segoe UI", 10, "bold")).pack(side="left")

        row3 = ttk.Frame(self.info_frame)
        row3.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(row3, text="Detail:", width=9, anchor="w").pack(side="left")
        self.detail_var = tk.StringVar(value="")
        ttk.Label(row3, textvariable=self.detail_var, wraplength=520,
                  justify="left", font=("Segoe UI", 8)).pack(side="left", fill="x", expand=True)

        # Admin banner (packed with 'before' so it stays on top when re-shown)
        self.admin_frame = ttk.Frame(self.body)
        self.admin_label = tk.Label(
            self.admin_frame, text="", anchor="w", justify="left", wraplength=440,
            font=("Segoe UI", 9, "bold"), padx=10, pady=8,
        )
        self.admin_label.pack(side="left", fill="x", expand=True)
        self.elevate_btn = ttk.Button(self.admin_frame, text="Relaunch as Admin",
                                      command=self._relaunch_as_admin, width=18)
        self.elevate_btn.pack(side="right", padx=8)

        # Buttons
        btns = ttk.Frame(self.body)
        btns.pack(fill="x", padx=10, pady=4)
        self.btn_start = ttk.Button(btns, text="Start", command=lambda: self._dispatch("start"))
        self.btn_stop = ttk.Button(btns, text="Stop", command=lambda: self._dispatch("stop"))
        self.btn_restart = ttk.Button(btns, text="Restart", command=lambda: self._dispatch("restart"))
        self.btn_refresh = ttk.Button(btns, text="Refresh", command=lambda: self._dispatch("refresh"))
        self.btn_pgadmin = ttk.Button(btns, text="Open pgAdmin 4", command=lambda: self._dispatch("pgadmin"))
        for b in (self.btn_start, self.btn_stop, self.btn_restart, self.btn_refresh, self.btn_pgadmin):
            b.pack(side="left", expand=True, fill="x", padx=3)

        # Options row
        opts = ttk.Frame(self.body)
        opts.pack(fill="x", padx=10, pady=(0, 2))
        self.auto_var = tk.BooleanVar(value=bool(self.cfg.get("auto_refresh")))
        ttk.Checkbutton(opts, text="Auto-refresh", variable=self.auto_var,
                        command=self._on_auto_toggle).pack(side="left")
        self.probe_var = tk.BooleanVar(value=bool(self.cfg.get("probe_enabled")))
        ttk.Checkbutton(opts, text="Health probe", variable=self.probe_var,
                        command=self._on_probe_toggle).pack(side="left", padx=(12, 0))
        ttk.Button(opts, text="Configuration\u2026", command=self._open_config_dialog).pack(side="right")
        ttk.Button(opts, text="Reload config", command=self._reload_config).pack(side="right", padx=4)

        # Output (tk.Text + ttk.Scrollbar so it themes correctly)
        logf = ttk.LabelFrame(self.body, text="Output")
        logf.pack(fill="both", expand=True, padx=10, pady=(6, 10))
        inner = ttk.Frame(logf)
        inner.pack(fill="both", expand=True, padx=4, pady=4)
        self.log_vsb = ttk.Scrollbar(inner, orient="vertical")
        self.log = tk.Text(inner, height=12, state="disabled", wrap="word",
                           font=("Consolas", 9), bd=0, padx=6, pady=4,
                           yscrollcommand=self.log_vsb.set)
        self.log_vsb.config(command=self.log.yview)
        self.log_vsb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

    def _update_admin_banner(self):
        if is_admin():
            self.admin_frame.pack_forget()
            return
        self.admin_label.config(
            text=("\u26a0  NOT RUNNING AS ADMINISTRATOR\n"
                  "Start / Stop / Restart / recovery config will fail with 'Access denied'.\n"
                  "Status checks, health probe and pgAdmin launch still work."))
        self.admin_frame.pack(side="top", fill="x", padx=10, pady=(8, 0),
                              before=self.info_frame)

    # -- startup warnings -------------------------------------------------
    def _log_startup_warnings(self):
        for w in self.cfg.warnings:
            self._append_log(f"[config] {w}", "err")
            log.warning("config: %s", w)
        if self.log_path:
            self._append_log(f"[log] Writing to {self.log_path}", "hdr")
        if not TRAY_AVAILABLE:
            self._append_log(
                f"[tray] pystray/Pillow unavailable ({TRAY_IMPORT_ERROR}). "
                "Tray icon and toast notifications disabled. "
                "Install with: pip install pystray Pillow", "warn")

    # -- tray -------------------------------------------------------------
    def _tray_icon_image(self, tone):
        color = {
            "ok": (35, 160, 60, 255),
            "warn": (226, 163, 54, 255),
            "err": (200, 50, 55, 255),
            "muted": (140, 140, 140, 255),
        }.get(tone, (140, 140, 140, 255))
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((4, 4, 60, 60), fill=color, outline=(255, 255, 255, 235), width=3)
        d.text((24, 18), "P", fill=(255, 255, 255, 255))
        return img

    def _build_tray(self):
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None

        if not self.cfg.get("tray_enabled") or not TRAY_AVAILABLE:
            return

        try:
            menu = pystray.Menu(
                pystray.MenuItem("Show / Hide", lambda i, it: self._cmds.put("toggle"), default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Start service", lambda i, it: self._cmds.put("start")),
                pystray.MenuItem("Stop service", lambda i, it: self._cmds.put("stop")),
                pystray.MenuItem("Restart service", lambda i, it: self._cmds.put("restart")),
                pystray.MenuItem("Refresh status", lambda i, it: self._cmds.put("refresh")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Open pgAdmin 4", lambda i, it: self._cmds.put("pgadmin")),
                pystray.MenuItem("Auto-refresh",
                                 lambda i, it: self._cmds.put("toggle_auto"),
                                 checked=lambda it: bool(self.auto_var.get())),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", lambda i, it: self._cmds.put("quit")),
            )
            self.icon = pystray.Icon(APP_DIR_NAME, self._tray_icon_image("muted"),
                                     "PostgreSQL Service Control", menu)
            self.icon.run_detached()
            log.info("System tray icon started.")
        except Exception as e:
            self.icon = None
            log.error("Failed to start tray icon: %s: %s", type(e).__name__, e)
            self._append_log(f"[tray] Failed to start: {type(e).__name__}: {e}", "err")

    def _update_tray_icon(self, health):
        if self.icon is None:
            return
        try:
            if health.tone != self.icon_color:
                self.icon.icon = self._tray_icon_image(health.tone)
                self.icon_color = health.tone
            self.icon.title = f"PostgreSQL: {health.label}"[:127]
        except Exception as e:
            log.debug("Tray icon update failed: %s", e)

    def _toast(self, title, message):
        """Windows toast via the tray icon (Shell_NotifyIcon NIIF_INFO).
        Degrades to beep + status bar if tray/toast unavailable."""
        log.info("NOTIFY | %s | %s", title, message.replace("\n", " "))
        if self.cfg.get("toast_enabled") and self.icon is not None:
            try:
                self.icon.notify(message, title)
                return
            except Exception as e:
                log.debug("Toast failed (%s); falling back.", e)
        if self.cfg.get("toast_enabled"):
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                pass
        self._set_status(f"{title}: {message.splitlines()[0] if message else ''}")

    # -- dispatch / workers ----------------------------------------------
    def _dispatch(self, action):
        if self.busy:
            self._set_status("Busy - please wait for the current operation to finish.")
            return
        self.busy = True
        self._set_buttons("disabled")
        label = {
            "start": "Starting service", "stop": "Stopping service",
            "restart": "Restarting service", "refresh": "Querying status",
            "pgadmin": "Launching pgAdmin",
        }.get(action, action)
        self._set_status(label + "\u2026")
        log.info("OP BEGIN | %s | admin=%s | service=%s", action, is_admin(),
                 self.cfg.get("service_name"))
        threading.Thread(target=self._worker, args=(action,), daemon=True).start()

    def _snapshot(self, ctrl):
        """Build a Health snapshot. Never raises."""
        scm_error, state, raw = "", None, ""
        try:
            state, raw = ctrl.query_state()
        except ServiceError as e:
            scm_error = str(e)
        except Exception as e:
            scm_error = f"{type(e).__name__}: {e}"

        alive, pdetail = None, ""
        if self.cfg.get("probe_enabled"):
            alive, pdetail = probe_postgres(
                self.cfg.get("host"), self.cfg.get("port"),
                float(self.cfg.get("probe_timeout_seconds", 3)),
            )

        health = classify_health(state, alive, bool(self.cfg.get("probe_enabled")), scm_error)
        if pdetail:
            health.detail = (health.detail + "\n" if health.detail else "") + pdetail
        return Snapshot(health=health, raw=raw)

    def _worker(self, action):
        try:
            ctrl = ServiceController(self.cfg.get("service_name"),
                                     self.cfg.get("operation_timeout_seconds", 60))
            if action == "start":
                res = ctrl.start()
            elif action == "stop":
                res = ctrl.stop()
            elif action == "restart":
                res = ctrl.restart()
            elif action == "refresh":
                res = ctrl.status()
            elif action == "pgadmin":
                res = launch_pgadmin(self.cfg.get("pgadmin_path"))
            else:
                res = OpResult(False, f"Unknown action: {action}")
        except ServiceError as e:
            res = OpResult(False, str(e))
        except Exception as e:
            res = OpResult(False, f"Unexpected error ({type(e).__name__}): {e}")

        snap = None
        if action != "pgadmin":
            try:
                snap = self._snapshot(ServiceController(self.cfg.get("service_name"), 15))
            except Exception as e:
                log.debug("Post-op snapshot failed: %s", e)

        log.info("OP END | %s | ok=%s | %s", action, res.ok, res.summary.replace("\n", " | "))
        self._results.put(OpOutcome(action, res, snap))

    def _silent_refresh(self):
        if self.busy or self._polling:
            return
        self._polling = True
        threading.Thread(target=self._silent_worker, daemon=True).start()

    def _silent_worker(self):
        try:
            ctrl = ServiceController(self.cfg.get("service_name"), 15)
            snap = self._snapshot(ctrl)
            self._states.put(snap)
        except Exception as e:
            log.debug("Silent refresh failed: %s: %s", type(e).__name__, e)
        finally:
            self._polling = False

    # -- queue draining (main thread only) --------------------------------
    def _drain_queues(self):
        processed = False

        try:
            while True:
                cmd = self._cmds.get_nowait()
                self._handle_cmd(cmd)
                processed = True
        except queue.Empty:
            pass

        latest = None
        try:
            while True:
                latest = self._states.get_nowait()
                processed = True
        except queue.Empty:
            pass
        if latest is not None:
            self._apply_snapshot(latest, notify_transitions=True)

        try:
            while True:
                outcome = self._results.get_nowait()
                self.busy = False
                self._set_buttons("normal")
                self._log_result(outcome.action, outcome.result)
                if outcome.snapshot is not None:
                    self._apply_snapshot(outcome.snapshot, notify_transitions=False)
                if not outcome.result.ok:
                    self._toast("Operation failed",
                                f"{outcome.action.upper()}: {outcome.result.summary}")
                self._set_status("Done." if outcome.result.ok
                                 else "Completed with errors - see output.")
                processed = True
        except queue.Empty:
            pass

        self.after(100 if processed else 150, self._drain_queues)

    def _handle_cmd(self, cmd):
        if cmd == "quit":
            self._closing = True
            self._on_close(force=True)
        elif cmd == "toggle":
            self._toggle_visibility()
        elif cmd == "toggle_auto":
            self.auto_var.set(not self.auto_var.get())
            self._on_auto_toggle()
        elif cmd in ("start", "stop", "restart", "refresh", "pgadmin"):
            self._show_window()
            self._dispatch(cmd)

    def _apply_snapshot(self, snap, notify_transitions):
        h = snap.health
        color = self.pal.get(h.tone, self.pal["muted"])
        self.status_var.set(h.label)
        self.status_indicator.config(fg=color)
        self.detail_var.set((h.error or h.detail or "").replace("\n", "  ")[:300])
        self._update_tray_icon(h)

        if notify_transitions:
            if self._health_seeded and h.key != self._last_health_key:
                log.info("HEALTH TRANSITION | %s -> %s", self._last_health_key, h.key)
                self._toast("PostgreSQL status changed",
                            f"{self.cfg.get('service_name')}: {h.label}")
            self._health_seeded = True
        self._last_health_key = h.key

    # -- refresh scheduling ----------------------------------------------
    def _on_auto_toggle(self):
        self._schedule_refresh(immediate=self.auto_var.get())

    def _on_probe_toggle(self):
        self._silent_refresh()

    def _schedule_refresh(self, immediate=False):
        if self._refresh_job:
            self.after_cancel(self._refresh_job)
            self._refresh_job = None
        if not self.auto_var.get():
            return
        interval = max(1, int(self.cfg.get("refresh_interval_seconds", 5))) * 1000
        if immediate:
            self._silent_refresh()
        self._refresh_job = self.after(interval, self._auto_refresh_tick)

    def _auto_refresh_tick(self):
        self._silent_refresh()
        self._schedule_refresh()

    # -- window visibility ------------------------------------------------
    def _toggle_visibility(self):
        if self._hidden:
            self._show_window()
        else:
            self._hide_window()

    def _hide_window(self):
        self._hidden = True
        self.withdraw()

    def _show_window(self):
        if not self._hidden:
            return
        self._hidden = False
        self.deiconify()
        self.state("normal")
        self.lift()
        self.focus_force()

    def _on_unmap(self, event):
        if event.widget is not self:
            return
        if not self.cfg.get("minimize_to_tray") or self.icon is None:
            return
        try:
            if self.state() == "iconic":
                self._hide_window()
        except tk.TclError:
            pass

    # -- logging widgets --------------------------------------------------
    def _append_log(self, text, tag=None):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n", tag)
        try:
            lines = int(self.log.index("end-1c").split(".")[0])
            if lines > 4000:
                self.log.delete("1.0", f"{lines - 3000}.0")
        except (ValueError, tk.TclError):
            pass
        self.log.config(state="disabled")
        self.log.see("end")

    def _log_result(self, action, res):
        self._append_log(f"== {action.upper()} @ {time.strftime('%H:%M:%S')} ==", "hdr")
        self._append_log(res.summary, "ok" if res.ok else "err")
        if res.detail:
            for line in res.detail.splitlines():
                self._append_log(line, "raw")
        self._append_log("")

    # -- config actions ---------------------------------------------------
    def _open_config_dialog(self):
        try:
            dlg = ConfigDialog(self, self.cfg)
        except Exception as e:
            messagebox.showerror("Configuration", f"Could not open dialog:\n{type(e).__name__}: {e}")
            return
        self.wait_window(dlg)
        if dlg.saved:
            self._after_config_change(reinit_logging=True)

    def _reload_config(self):
        self.cfg.load()
        setup_logging(self.cfg)
        self._after_config_change(reinit_logging=False)
        self._set_status(f"Config reloaded from {self.cfg.path or 'defaults'}.")

    def _after_config_change(self, reinit_logging=True):
        if reinit_logging:
            setup_logging(self.cfg)
            log.info("Logging reinitialised after config change.")

        self.service_name_var.set(self.cfg.get("service_name"))
        self.auto_var.set(bool(self.cfg.get("auto_refresh")))
        self.probe_var.set(bool(self.cfg.get("probe_enabled")))
        self._set_theme(self.cfg.get("theme", "light"))

        self._log_startup_warnings()
        self._build_tray()
        self._health_seeded = False
        self._last_health_key = None
        self._schedule_refresh(immediate=True)

    def _relaunch_as_admin(self):
        if is_admin():
            messagebox.showinfo("Already elevated",
                                "This instance is already running as administrator.")
            return
        log.info("Requesting elevation via UAC.")
        if request_elevation():
            self._closing = True
            self.destroy()
        else:
            messagebox.showwarning(
                "Elevation cancelled",
                "The UAC prompt was declined or failed.\n\n"
                "Status checks, health probe and pgAdmin launch still work; "
                "service control requires elevation.",
            )

    # -- misc -------------------------------------------------------------
    def _set_status(self, text):
        self.statusbar.config(text=text)

    def _set_buttons(self, state):
        for b in (self.btn_start, self.btn_stop, self.btn_restart,
                  self.btn_refresh, self.btn_pgadmin):
            b.config(state=state)

    def _on_close(self, force=False):
        if not force and self.icon is not None and self.cfg.get("minimize_to_tray"):
            self._hide_window()
            self._set_status("Minimised to tray.")
            return
        self._closing = True
        if self._refresh_job:
            try:
                self.after_cancel(self._refresh_job)
            except Exception:
                pass
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None
        log.info("Shutdown.")
        logging.shutdown()
        self.destroy()


def main():
    for fn in (lambda: ctypes.windll.shcore.SetProcessDpiAwareness(1),
               lambda: ctypes.windll.user32.SetProcessDPIAware()):
        try:
            fn()
            break
        except Exception:
            continue

    # Without an explicit AppUserModelID, Win10/11 may attribute toasts to a
    # generic publisher or suppress them entirely.
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass

    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())