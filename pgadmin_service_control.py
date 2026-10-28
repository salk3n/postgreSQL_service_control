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
  * config.json contains NO secrets. This is a design constraint, not an oversight.
  * Persistent SCM write operations are gated behind "advanced_enabled" (production).
  * Only one instance can run (named mutex).
  * UI language (en/ru) is fixed at startup; change applies after restart.
    The file audit log is ALWAYS English (tr_en), regardless of UI language.
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
from dataclasses import dataclass
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
# Localization
# ---------------------------------------------------------------------------

LANGUAGES = ("en", "ru")
LANG_DISPLAY = (("en", "English"), ("ru", "Русский"))

STRINGS_EN = {
    # App / status bar
    "app_title": "PostgreSQL Service Control",
    "status_ready": "Ready.",
    "status_busy": "Busy - please wait for the current operation to finish.",
    "status_done": "Done.",
    "status_done_errors": "Completed with errors - see output.",
    "status_checking": "checking\u2026",
    "status_minimized": "Minimised to tray.",
    "word_unknown": "unknown",
    "word_defaults": "defaults",

    # Menu
    "menu_service": "Service",
    "menu_tools": "Tools",
    "menu_file": "File",
    "menu_start": "Start Service",
    "menu_stop": "Stop Service",
    "menu_restart": "Restart Service",
    "menu_refresh": "Refresh Status",
    "menu_pgadmin": "Open pgAdmin 4",
    "menu_config": "Configuration\u2026",
    "menu_reload": "Reload config",
    "menu_exit": "Exit",

    # Frames / labels
    "frame_service": "Service",
    "frame_output": "Output",
    "lbl_name": "Name:",
    "lbl_health": "Health:",
    "lbl_detail": "Detail:",

    # Buttons / options
    "btn_start": "Start",
    "btn_stop": "Stop",
    "btn_restart": "Restart",
    "btn_refresh": "Refresh",
    "btn_pgadmin": "Open pgAdmin 4",
    "btn_configuration": "Configuration\u2026",
    "btn_reload_config": "Reload config",
    "btn_relaunch": "Relaunch as Admin",
    "chk_auto_refresh": "Auto-refresh",
    "chk_health_probe": "Health probe",

    # Admin banner / elevation
    "banner_no_admin": "\u26a0  NOT RUNNING AS ADMINISTRATOR\nStart / Stop / Restart / recovery config will fail with 'Access denied'.\nStatus checks, health probe and pgAdmin launch still work.",
    "msg_already_elevated_title": "Already elevated",
    "msg_already_elevated": "This instance is already running as administrator.",
    "msg_elevation_cancelled_title": "Elevation cancelled",
    "msg_elevation_cancelled": "The UAC prompt was declined or failed.\n\nStatus checks, health probe and pgAdmin launch still work; service control requires elevation.",

    # Dispatch status lines
    "disp_start": "Starting service\u2026",
    "disp_stop": "Stopping service\u2026",
    "disp_restart": "Restarting service\u2026",
    "disp_refresh": "Querying status\u2026",
    "disp_pgadmin": "Launching pgAdmin\u2026",

    # Action names (toasts / output headers)
    "action_start": "START",
    "action_stop": "STOP",
    "action_restart": "RESTART",
    "action_refresh": "REFRESH",
    "action_pgadmin": "PGADMIN",

    # Output panel lines
    "log_writing_to": "[log] Writing to {path}",
    "log_path_ok": "[check] pgAdmin path OK: {path}",
    "log_path_empty": "[config] pgadmin_path is empty - the pgAdmin button will not work.",
    "log_path_missing": "[config] WARNING: pgAdmin executable NOT found at: {path}\n          Fix it under Configuration\u2026 > pgAdmin > Browse. The pgAdmin button will fail until corrected.",
    "log_tray_missing": "[tray] pystray/Pillow unavailable ({err}). Tray icon and toast notifications disabled. Install with: pip install pystray Pillow",
    "log_advanced_unlocked": "[config] advanced_enabled=true - SCM write operations UNLOCKED.",
    "notify_enabled": "[notify] Notifications enabled.",
    "notify_disabled": "[notify] Notifications disabled.",
    "log_result_header": "== {action} @ {time} ==",

    # Config warnings (rendered at display time)
    "w_parse_failed": "Could not parse {path}: {err}. Using defaults.",
    "w_root_not_object": "config.json root must be a JSON object. Using defaults.",
    "w_not_found": "config.json not found; defaults will be written on first save.",
    "w_int_default": "'{key}' must be an integer; kept default.",
    "w_str_default": "'{key}' must be a string; kept default.",
    "w_unknown_theme": "Unknown theme '{theme}'; using 'light'.",
    "w_unknown_lang": "Unknown language '{lang}'; using 'en'.",
    "w_invalid_port": "Invalid port '{port}'; using 5432.",
    "w_unknown_keys": "Ignoring unknown config keys: {keys}",
    "w_saved_elsewhere": "Could not write {path}; saved to {other} instead. The portable config.json takes precedence at next launch - delete one of them to avoid confusion.",
    "w_save_failed": "Could not write config.json to any known location.",
    "w_log_open_failed": "Could not open log file {path}: {err}. Logging disabled.",

    # Service controller
    "err_cmd_not_found": "Command not found: {cmd}",
    "err_timeout": "Command timed out after {sec}s: {cmdline}",
    "err_exec_failed": "Could not execute {cmd}: {err}",
    "err_svc_missing": "Service '{name}' does not exist. Check configuration.",
    "err_access_denied": "Access denied. Run this tool as administrator.",
    "sc_query_failed": "sc query exited with code {rc}",
    "status_state": "Service state: {state}",
    "status_raw_title": "Service status (raw):",
    "verb_start": "Start",
    "verb_stop": "Stop",
    "ok_reached": "Service is {state}.",
    "ok_reached_detail": "(net {verb} produced output but the service reached {state}.)",
    "fail_access": "Access denied - administrator rights required.",
    "fail_not_found": "Service '{name}' not found. Check configuration.",
    "fail_op": "{verb} failed (exit code {rc}). Current state: {state}.",
    "ok_already_running": "Service is already RUNNING - nothing to do.",
    "ok_already_stopped": "Service is already STOPPED - nothing to do.",
    "restart_stop_line": "STOP  : {summary}",
    "restart_skip_line": "STOP  : skipped (state was {state})",
    "restart_start_line": "START : {summary}",
    "restart_aborted": "Restart aborted - stop failed.\n{summary}",
    "restart_complete": "Restart complete.\n{lines}",
    "ok_recovery": "SCM recovery configured: restart after 5s / 15s / 60s, counter resets daily.",
    "fail_sc_failure": "sc failure failed (exit code {rc}).",
    "invalid_start_type": "Invalid start type: {st}",
    "ok_start_type": "Start type set to '{st}'.",
    "fail_sc_config": "sc config failed (exit code {rc}).",

    # SCM state tokens (display forms)
    "st_STOPPED": "STOPPED",
    "st_RUNNING": "RUNNING",
    "st_START_PENDING": "START PENDING",
    "st_STOP_PENDING": "STOP PENDING",
    "st_CONTINUE_PENDING": "CONTINUE PENDING",
    "st_PAUSE_PENDING": "PAUSE PENDING",
    "st_PAUSED": "PAUSED",

    # Probe
    "probe_no_host": "probe skipped (no host configured)",
    "probe_bad_port": "probe skipped (invalid port {port})",
    "probe_alive": "postmaster responsive on {host}:{port} (SSL reply {reply})",
    "probe_closed": "connection closed by {host}:{port} without reply",
    "probe_unexpected": "unexpected protocol reply from {host}:{port}: {reply}",
    "probe_timeout": "timeout after {sec}s contacting {host}:{port}",
    "probe_refused": "connection refused by {host}:{port} (nothing listening)",
    "probe_error": "probe error on {host}:{port}: {kind}: {err}",

    # Health classification
    "h_error_label": "ERROR",
    "h_unknown_label": "UNKNOWN",
    "h_unknown_detail": "Could not parse sc.exe output.",
    "h_probe_off_suffix": " (probe off)",
    "h_scm_only_detail": "Health reflects SCM state only; protocol probe disabled.",
    "h_pending_detail": "Service is transitioning.",
    "h_healthy_label": "HEALTHY",
    "h_healthy_detail": "SCM reports RUNNING and postmaster answers the wire protocol.",
    "h_wedged_label": "RUNNING / NO RESPONSE",
    "h_wedged_detail": "SCM reports RUNNING but nothing answered the PostgreSQL protocol. Possible wedged postmaster, wrong host/port, or firewall.",
    "h_down_label": "STOPPED",
    "h_down_detail": "Service stopped and port not listening.",
    "h_port_busy_label": "STOPPED / PORT BUSY",
    "h_port_busy_detail": "Service is stopped but something else answers on that port.",

    # pgAdmin launcher
    "pgadmin_no_path": "pgadmin_path is not set in configuration.",
    "pgadmin_not_found": "pgAdmin executable not found:\n{path}",
    "pgadmin_launch_failed": "Failed to launch pgAdmin: {err}",
    "pgadmin_launched": "Launched pgAdmin:\n{path}",

    # Toasts
    "toast_op_complete": "Operation complete",
    "toast_op_failed": "Operation failed",
    "toast_transition_title": "PostgreSQL status changed",
    "toast_transition_body": "{svc}: {label}",
    "toast_body": "{action}: {summary}",

    # Tray
    "tray_show_hide": "Show / Hide",
    "tray_notifications": "Notifications",
    "tray_quit": "Quit",

    # Config dialog
    "dlg_title": "Configuration",
    "lbl_service_name": "Service name:",
    "btn_scan": "Scan",
    "lbl_start_type": "Start type:",
    "st_query": "(query)",
    "btn_apply_start_type": "Apply start type",
    "btn_recovery": "Configure SCM recovery",
    "advanced_locked_hint": "SCM write operations are locked (production safety). Set \"advanced_enabled\": true in config.json to unlock.",
    "frame_probe": "Health probe (PostgreSQL wire protocol, no credentials)",
    "chk_enable_probe": "Enable probe",
    "lbl_host": "Host:",
    "lbl_port": "Port:",
    "lbl_probe_timeout": "Timeout (s):",
    "frame_pgadmin": "pgAdmin",
    "btn_browse": "Browse\u2026",
    "frame_behaviour": "Behaviour",
    "chk_auto_refresh_status": "Auto-refresh status",
    "chk_minimize_to_tray": "Minimize to tray",
    "chk_system_tray": "System tray icon",
    "chk_toast": "Toast notifications",
    "chk_write_log": "Write log file",
    "lbl_language": "Language:",
    "lbl_theme": "Theme:",
    "lbl_refresh_s": "Refresh (s):",
    "lbl_op_timeout": "Op timeout (s):",
    "lbl_log_mb": "Log MB:",
    "lbl_backups": "Backups:",
    "tray_missing_dlg": "pystray / Pillow not available ({err}).\nTray icon and toast notifications are disabled. Install with: pip install pystray Pillow",
    "btn_reset_defaults": "Reset to defaults",
    "btn_open_config": "Open config.json",
    "btn_cancel": "Cancel",
    "btn_apply": "Apply",

    # Config dialog messages
    "dlg_status_scanning": "Scanning registry for PostgreSQL services\u2026",
    "scan_failed_title": "Scan failed",
    "scan_none_title": "Scan",
    "scan_none": "No PostgreSQL-like services found in the registry.",
    "scan_done_title": "Scan complete",
    "scan_done_body": "Found {n} service(s):\n\n{listing}\n\nSelect one from the dropdown.\nNote: the registry also lists leftovers from uninstalled versions.",
    "locked_title": "Locked",
    "locked_body": "SCM write operations are locked (production safety).\nSet \"advanced_enabled\": true in config.json, then Reload config.",
    "title_start_type": "Start type",
    "title_recovery": "Recovery",
    "msg_svc_empty": "Service name is empty.",
    "recovery_confirm_title": "Configure SCM recovery",
    "recovery_confirm_body": "Configure Windows to auto-restart '{svc}' on failure?\n\n  1st failure: restart after 5s\n  2nd failure: restart after 15s\n  later failures: restart after 60s\n  failure count resets after 24h\n\nThis changes the service configuration and requires administrator rights.",
    "dlg_status_set_st": "Setting start type to '{st}'\u2026",
    "dlg_status_query_st": "Querying start type\u2026",
    "dlg_status_recovery": "Configuring SCM recovery\u2026",
    "start_type_read_failed": "Could not read start type: {err}",
    "start_type_current": "Current start type: {name}",
    "start_type_reg_detail": "Registry Start value = {v}",
    "stt_boot": "boot",
    "stt_system": "system",
    "stt_auto": "auto",
    "stt_demand": "demand",
    "stt_disabled": "disabled",
    "dlg_success_title": "Success",
    "dlg_failed_title": "Failed",
    "reset_confirm_title": "Reset",
    "reset_confirm_body": "Reset all settings to built-in defaults and save?\n\nThis also re-locks SCM write operations.",
    "title_config": "Config",
    "open_config_missing": "Could not locate or create config.json.",
    "open_config_err": "Could not open:\n{path}\n{err}",
    "v_svc_empty": "Service name must not be empty.",
    "v_int": "{label} must be a whole number.",
    "v_range": "{label} must be between {lo} and {hi}.",
    "v_theme": "Theme must be one of {themes}.",
    "v_language": "Language must be English or \u0420\u0443\u0441\u0441\u043a\u0438\u0439.",
    "v_tray_missing": "System tray requested but pystray/Pillow are not installed.",
    "invalid_config_title": "Invalid configuration",
    "save_failed_title": "Save failed",
    "save_failed_body": "Could not write config.json to any known location.\n\n{warnings}",
    "dlg_open_failed": "Could not open dialog:\n{err}",
    "reload_status": "Config reloaded from {path}.",
    "lang_restart_title": "Language changed",
    "lang_restart_body": "Language will be applied after the application is restarted.",
}

STRINGS_RU = {
    # App / status bar
    "app_title": "Управление службой PostgreSQL",
    "status_ready": "Готово.",
    "status_busy": "Занято — дождитесь завершения текущей операции.",
    "status_done": "Выполнено.",
    "status_done_errors": "Завершено с ошибками — см. панель вывода.",
    "status_checking": "проверка…",
    "status_minimized": "Свёрнуто в трей.",
    "word_unknown": "неизвестно",
    "word_defaults": "значения по умолчанию",

    # Menu
    "menu_service": "Служба",
    "menu_tools": "Инструменты",
    "menu_file": "Файл",
    "menu_start": "Запустить службу",
    "menu_stop": "Остановить службу",
    "menu_restart": "Перезапустить службу",
    "menu_refresh": "Обновить статус",
    "menu_pgadmin": "Открыть pgAdmin 4",
    "menu_config": "Настройки…",
    "menu_reload": "Перечитать конфигурацию",
    "menu_exit": "Выход",

    # Frames / labels
    "frame_service": "Служба",
    "frame_output": "Вывод",
    "lbl_name": "Имя:",
    "lbl_health": "Состояние:",
    "lbl_detail": "Детали:",

    # Buttons / options
    "btn_start": "Пуск",
    "btn_stop": "Стоп",
    "btn_restart": "Перезапуск",
    "btn_refresh": "Обновить",
    "btn_pgadmin": "Открыть pgAdmin 4",
    "btn_configuration": "Настройки…",
    "btn_reload_config": "Перечитать конфиг",
    "btn_relaunch": "Перезапустить от администратора",
    "chk_auto_refresh": "Автообновление",
    "chk_health_probe": "Проверка здоровья",

    # Admin banner / elevation
    "banner_no_admin": "\u26a0  ЗАПУЩЕНО НЕ ОТ ИМЕНИ АДМИНИСТРАТОРА\nПуск / Стоп / Перезапуск / настройка восстановления завершатся ошибкой «Отказано в доступе».\nПроверка статуса, здоровья и запуск pgAdmin продолжают работать.",
    "msg_already_elevated_title": "Права администратора",
    "msg_already_elevated": "Этот экземпляр уже запущен с правами администратора.",
    "msg_elevation_cancelled_title": "Повышение прав отменено",
    "msg_elevation_cancelled": "Запрос UAC был отклонён или не выполнен.\n\nПроверка статуса, здоровья и запуск pgAdmin работают; управление службой требует повышения прав.",

    # Dispatch status lines
    "disp_start": "Запуск службы…",
    "disp_stop": "Остановка службы…",
    "disp_restart": "Перезапуск службы…",
    "disp_refresh": "Запрос статуса…",
    "disp_pgadmin": "Запуск pgAdmin…",

    # Action names (toasts / output headers)
    "action_start": "ПУСК",
    "action_stop": "СТОП",
    "action_restart": "ПЕРЕЗАПУСК",
    "action_refresh": "ОБНОВЛЕНИЕ",
    "action_pgadmin": "PGADMIN",

    # Output panel lines
    "log_writing_to": "[журнал] Запись в {path}",
    "log_path_ok": "[проверка] Путь к pgAdmin корректен: {path}",
    "log_path_empty": "[конфиг] pgadmin_path пуст — кнопка pgAdmin не будет работать.",
    "log_path_missing": "[конфиг] ВНИМАНИЕ: исполняемый файл pgAdmin НЕ найден: {path}\n          Исправьте в Настройки… > pgAdmin > Обзор. Кнопка pgAdmin не будет работать.",
    "log_tray_missing": "[трей] pystray/Pillow недоступны ({err}). Значок в трее и уведомления отключены. Установка: pip install pystray Pillow",
    "log_advanced_unlocked": "[конфиг] advanced_enabled=true — операции записи в SCM РАЗБЛОКИРОВАНЫ.",
    "notify_enabled": "[уведомления] Уведомления включены.",
    "notify_disabled": "[уведомления] Уведомления отключены.",
    "log_result_header": "== {action} @ {time} ==",

    # Config warnings
    "w_parse_failed": "Не удалось разобрать {path}: {err}. Используются значения по умолчанию.",
    "w_root_not_object": "Корень config.json должен быть объектом JSON. Используются значения по умолчанию.",
    "w_not_found": "config.json не найден; значения по умолчанию будут записаны при первом сохранении.",
    "w_int_default": "'{key}' должно быть целым числом; оставлено значение по умолчанию.",
    "w_str_default": "'{key}' должно быть строкой; оставлено значение по умолчанию.",
    "w_unknown_theme": "Неизвестная тема '{theme}'; используется 'light'.",
    "w_unknown_lang": "Неизвестный язык '{lang}'; используется 'en'.",
    "w_invalid_port": "Некорректный порт '{port}'; используется 5432.",
    "w_unknown_keys": "Игнорируются неизвестные ключи конфигурации: {keys}",
    "w_saved_elsewhere": "Не удалось записать {path}; сохранено в {other}. Переносимый config.json имеет приоритет при следующем запуске — удалите один из файлов во избежание путаницы.",
    "w_save_failed": "Не удалось записать config.json ни в одно известное расположение.",
    "w_log_open_failed": "Не удалось открыть файл журнала {path}: {err}. Журналирование отключено.",

    # Service controller
    "err_cmd_not_found": "Команда не найдена: {cmd}",
    "err_timeout": "Тайм-аут команды после {sec} с: {cmdline}",
    "err_exec_failed": "Не удалось выполнить {cmd}: {err}",
    "err_svc_missing": "Служба '{name}' не существует. Проверьте конфигурацию.",
    "err_access_denied": "Отказано в доступе. Запустите программу от имени администратора.",
    "sc_query_failed": "sc query завершилась с кодом {rc}",
    "status_state": "Состояние службы: {state}",
    "status_raw_title": "Статус службы (сырой вывод):",
    "verb_start": "Пуск",
    "verb_stop": "Стоп",
    "ok_reached": "Служба {state}.",
    "ok_reached_detail": "(net {verb} выдала вывод, но служба перешла в состояние {state}.)",
    "fail_access": "Отказано в доступе — требуются права администратора.",
    "fail_not_found": "Служба '{name}' не найдена. Проверьте конфигурацию.",
    "fail_op": "{verb}: ошибка (код выхода {rc}). Текущее состояние: {state}.",
    "ok_already_running": "Служба уже ЗАПУЩЕНА — делать нечего.",
    "ok_already_stopped": "Служба уже ОСТАНОВЛЕНА — делать нечего.",
    "restart_stop_line": "СТОП  : {summary}",
    "restart_skip_line": "СТОП  : пропущено (состояние было {state})",
    "restart_start_line": "ПУСК  : {summary}",
    "restart_aborted": "Перезапуск прерван — остановка не удалась.\n{summary}",
    "restart_complete": "Перезапуск завершён.\n{lines}",
    "ok_recovery": "Восстановление SCM настроено: перезапуск через 5 с / 15 с / 60 с, счётчик сбрасывается ежедневно.",
    "fail_sc_failure": "sc failure завершилась с ошибкой (код {rc}).",
    "invalid_start_type": "Некорректный тип запуска: {st}",
    "ok_start_type": "Тип запуска установлен в '{st}'.",
    "fail_sc_config": "sc config завершилась с ошибкой (код {rc}).",

    # SCM state tokens (display forms)
    "st_STOPPED": "ОСТАНОВЛЕНА",
    "st_RUNNING": "ЗАПУЩЕНА",
    "st_START_PENDING": "ЗАПУСКАЕТСЯ",
    "st_STOP_PENDING": "ОСТАНАВЛИВАЕТСЯ",
    "st_CONTINUE_PENDING": "ВОЗОБНОВЛЯЕТСЯ",
    "st_PAUSE_PENDING": "ПРИОСТАНАВЛИВАЕТСЯ",
    "st_PAUSED": "НА ПАУЗЕ",

    # Probe
    "probe_no_host": "проверка пропущена (хост не настроен)",
    "probe_bad_port": "проверка пропущена (некорректный порт {port})",
    "probe_alive": "postmaster отвечает на {host}:{port} (ответ SSL {reply})",
    "probe_closed": "соединение закрыто {host}:{port} без ответа",
    "probe_unexpected": "неожиданный ответ протокола от {host}:{port}: {reply}",
    "probe_timeout": "тайм-аут после {sec} с при обращении к {host}:{port}",
    "probe_refused": "соединение отклонено {host}:{port} (ничего не слушает)",
    "probe_error": "ошибка проверки на {host}:{port}: {kind}: {err}",

    # Health classification
    "h_error_label": "ОШИБКА",
    "h_unknown_label": "НЕИЗВЕСТНО",
    "h_unknown_detail": "Не удалось разобрать вывод sc.exe.",
    "h_probe_off_suffix": " (проверка выкл.)",
    "h_scm_only_detail": "Состояние отражает только данные SCM; проверка по протоколу отключена.",
    "h_pending_detail": "Служба в процессе перехода.",
    "h_healthy_label": "ИСПРАВНА",
    "h_healthy_detail": "SCM сообщает ЗАПУЩЕНА, и postmaster отвечает на запрос по протоколу.",
    "h_wedged_label": "ЗАПУЩЕНА / НЕТ ОТВЕТА",
    "h_wedged_detail": "SCM сообщает ЗАПУЩЕНА, но никто не ответил на запрос протокола PostgreSQL. Возможен зависший postmaster, неверный хост/порт или файрвол.",
    "h_down_label": "ОСТАНОВЛЕНА",
    "h_down_detail": "Служба остановлена, порт не слушает.",
    "h_port_busy_label": "ОСТАНОВЛЕНА / ПОРТ ЗАНЯТ",
    "h_port_busy_detail": "Служба остановлена, но на порту отвечает другой процесс.",

    # pgAdmin launcher
    "pgadmin_no_path": "pgadmin_path не задан в конфигурации.",
    "pgadmin_not_found": "Исполняемый файл pgAdmin не найден:\n{path}",
    "pgadmin_launch_failed": "Не удалось запустить pgAdmin: {err}",
    "pgadmin_launched": "Запущен pgAdmin:\n{path}",

    # Toasts
    "toast_op_complete": "Операция выполнена",
    "toast_op_failed": "Ошибка операции",
    "toast_transition_title": "Статус PostgreSQL изменился",
    "toast_transition_body": "{svc}: {label}",
    "toast_body": "{action}: {summary}",

    # Tray
    "tray_show_hide": "Показать / Скрыть",
    "tray_notifications": "Уведомления",
    "tray_quit": "Выход",

    # Config dialog
    "dlg_title": "Настройки",
    "lbl_service_name": "Имя службы:",
    "btn_scan": "Поиск",
    "lbl_start_type": "Тип запуска:",
    "st_query": "(запрос)",
    "btn_apply_start_type": "Применить тип запуска",
    "btn_recovery": "Настроить восстановление SCM",
    "advanced_locked_hint": "Операции записи в SCM заблокированы (защита продакшена). Чтобы разблокировать, задайте \"advanced_enabled\": true в config.json.",
    "frame_probe": "Проверка здоровья (протокол PostgreSQL, без учётных данных)",
    "chk_enable_probe": "Включить проверку",
    "lbl_host": "Хост:",
    "lbl_port": "Порт:",
    "lbl_probe_timeout": "Тайм-аут (с):",
    "frame_pgadmin": "pgAdmin",
    "btn_browse": "Обзор…",
    "frame_behaviour": "Поведение",
    "chk_auto_refresh_status": "Автообновление статуса",
    "chk_minimize_to_tray": "Сворачивать в трей",
    "chk_system_tray": "Значок в системном трее",
    "chk_toast": "Всплывающие уведомления",
    "chk_write_log": "Писать файл журнала",
    "lbl_language": "Язык:",
    "lbl_theme": "Тема:",
    "lbl_refresh_s": "Обновление (с):",
    "lbl_op_timeout": "Тайм-аут операции (с):",
    "lbl_log_mb": "Журнал (МБ):",
    "lbl_backups": "Резервные копии:",
    "tray_missing_dlg": "pystray / Pillow недоступны ({err}).\nЗначок в трее и уведомления отключены. Установка: pip install pystray Pillow",
    "btn_reset_defaults": "Сбросить по умолчанию",
    "btn_open_config": "Открыть config.json",
    "btn_cancel": "Отмена",
    "btn_apply": "Применить",

    # Config dialog messages
    "dlg_status_scanning": "Поиск служб PostgreSQL в реестре…",
    "scan_failed_title": "Поиск не удался",
    "scan_none_title": "Поиск",
    "scan_none": "Службы, похожие на PostgreSQL, в реестре не найдены.",
    "scan_done_title": "Поиск завершён",
    "scan_done_body": "Найдено служб: {n}\n\n{listing}\n\nВыберите одну из списка.\nПримечание: реестр также содержит остатки от удалённых версий.",
    "locked_title": "Заблокировано",
    "locked_body": "Операции записи в SCM заблокированы (защита продакшена).\nЗадайте \"advanced_enabled\": true в config.json, затем «Перечитать конфигурацию».",
    "title_start_type": "Тип запуска",
    "title_recovery": "Восстановление",
    "msg_svc_empty": "Имя службы не задано.",
    "recovery_confirm_title": "Настроить восстановление SCM",
    "recovery_confirm_body": "Настроить автоматический перезапуск '{svc}' средствами Windows при сбое?\n\n  1-й сбой: перезапуск через 5 с\n  2-й сбой: перезапуск через 15 с\n  последующие сбои: перезапуск через 60 с\n  счётчик сбоев сбрасывается через 24 ч\n\nЭто изменяет конфигурацию службы и требует прав администратора.",
    "dlg_status_set_st": "Установка типа запуска '{st}'…",
    "dlg_status_query_st": "Запрос типа запуска…",
    "dlg_status_recovery": "Настройка восстановления SCM…",
    "start_type_read_failed": "Не удалось прочитать тип запуска: {err}",
    "start_type_current": "Текущий тип запуска: {name}",
    "start_type_reg_detail": "Значение Start в реестре = {v}",
    "stt_boot": "загрузочный",
    "stt_system": "системный",
    "stt_auto": "авто",
    "stt_demand": "вручную",
    "stt_disabled": "отключено",
    "dlg_success_title": "Успешно",
    "dlg_failed_title": "Ошибка",
    "reset_confirm_title": "Сброс",
    "reset_confirm_body": "Сбросить все настройки к значениям по умолчанию и сохранить?\n\nЭто также заново заблокирует операции записи в SCM.",
    "title_config": "Конфигурация",
    "open_config_missing": "Не удалось найти или создать config.json.",
    "open_config_err": "Не удалось открыть:\n{path}\n{err}",
    "v_svc_empty": "Имя службы не должно быть пустым.",
    "v_int": "{label} должно быть целым числом.",
    "v_range": "{label} должно быть в диапазоне от {lo} до {hi}.",
    "v_theme": "Тема должна быть одной из {themes}.",
    "v_language": "Язык должен быть English или Русский.",
    "v_tray_missing": "Запрошен системный трей, но pystray/Pillow не установлены.",
    "invalid_config_title": "Некорректная конфигурация",
    "save_failed_title": "Ошибка сохранения",
    "save_failed_body": "Не удалось записать config.json ни в одно известное расположение.\n\n{warnings}",
    "dlg_open_failed": "Не удалось открыть диалог:\n{err}",
    "reload_status": "Конфигурация перечитана из {path}.",
    "lang_restart_title": "Язык изменён",
    "lang_restart_body": "Язык будет применён после перезапуска приложения.",
}

STRINGS = {"en": STRINGS_EN, "ru": STRINGS_RU}

_LANG = "en"


def set_language(code):
    """Fix the UI language for the process lifetime. Called once at startup."""
    global _LANG
    _LANG = code if code in LANGUAGES else "en"


def _render(table_key, key, params):
    s = STRINGS.get(table_key, {}).get(key)
    if s is None:
        s = STRINGS["en"].get(key, key)
    try:
        return s.format(**params) if params else s
    except (KeyError, IndexError):
        return s


def tr(key, **params):
    """Translate for the active UI language; falls back en -> key."""
    return _render(_LANG, key, params)


def tr_en(key, **params):
    """English rendering regardless of UI language - for the audit log."""
    return _render("en", key, params)


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
    "operation_timeout_seconds": 300,   # production shutdown checkpoints can exceed 60s
    "theme": "light",
    "language": "en",
    "tray_enabled": True,
    "minimize_to_tray": True,
    "toast_enabled": True,
    "log_enabled": True,
    "log_max_mb": 2,
    "log_backups": 5,
    "advanced_enabled": False,          # production safety gate for SCM writes
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
    """config.json with validation, portable-first resolution, writable fallback.

    NOTE: warnings are stored as (key, params) tuples, NOT rendered strings,
    because they are produced before the UI language is known. Render with
    tr()/tr_en() at display time.
    """

    def __init__(self):
        self.path = None
        self.data_dir = None
        self.warnings = []
        self.data = dict(DEFAULT_CONFIG)
        self.load()

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
                    self.warnings.append(("w_parse_failed", {"path": cand, "err": e}))
                    raw = {}
                if isinstance(raw, dict):
                    self._merge(raw)
                else:
                    self.warnings.append(("w_root_not_object", {}))
                break
        else:
            self.warnings.append(("w_not_found", {}))

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
                    self.warnings.append(("w_int_default", {"key": key}))
            elif isinstance(default, str):
                if isinstance(val, str):
                    self.data[key] = val.strip()
                else:
                    self.warnings.append(("w_str_default", {"key": key}))

        theme = self.data.get("theme")
        if theme not in VALID_THEMES:
            self.warnings.append(("w_unknown_theme", {"theme": theme}))
            self.data["theme"] = "light"

        lang = self.data.get("language")
        if lang not in LANGUAGES:
            self.warnings.append(("w_unknown_lang", {"lang": lang}))
            self.data["language"] = "en"

        port = self.data.get("port")
        if not isinstance(port, int) or not (1 <= port <= 65535):
            self.warnings.append(("w_invalid_port", {"port": port}))
            self.data["port"] = 5432

        unknown = set(raw) - set(DEFAULT_CONFIG)
        if unknown:
            self.warnings.append(("w_unknown_keys", {"keys": ", ".join(sorted(unknown))}))

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
                    json.dump(self.data, f, indent=2, ensure_ascii=False)
                    f.write("\n")
                if self.path and self.path != cand:
                    self.warnings.append(
                        ("w_saved_elsewhere", {"path": self.path, "other": cand}))
                self.path = cand
                self.data_dir = resolve_data_dir(os.path.dirname(cand))
                return True
            except OSError:
                continue

        self.warnings.append(("w_save_failed", {}))
        return False

    def reset_to_defaults(self):
        self.data = dict(DEFAULT_CONFIG)   # re-locks advanced_enabled deliberately
        self.warnings = []
        return self.save()

    def get(self, key, default=None):
        return self.data.get(key, default)


# ---------------------------------------------------------------------------
# Logging (file audit log is ALWAYS English)
# ---------------------------------------------------------------------------

def setup_logging(cfg):
    """(Re)configure the rotating file logger. Safe to call repeatedly."""
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
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
        cfg.warnings.append(("w_log_open_failed", {"path": path, "err": e}))
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
    Returns (alive: bool|None, detail: str).
    """
    if not host:
        return None, tr("probe_no_host")
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None, tr("probe_bad_port", port=port)

    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(struct.pack("!ii", 8, PG_SSL_REQUEST_CODE))
            resp = s.recv(1)
            if resp in (b"S", b"N"):
                return True, tr("probe_alive", host=host, port=port, reply=repr(resp))
            if not resp:
                return False, tr("probe_closed", host=host, port=port)
            return False, tr("probe_unexpected", host=host, port=port, reply=repr(resp))
    except socket.timeout:
        return False, tr("probe_timeout", sec=timeout, host=host, port=port)
    except ConnectionRefusedError:
        return False, tr("probe_refused", host=host, port=port)
    except OSError as e:
        return False, tr("probe_error", host=host, port=port,
                         kind=type(e).__name__, err=e)


# ---------------------------------------------------------------------------
# Service discovery (registry - stdlib, instant, no console)
# ---------------------------------------------------------------------------

def discover_postgres_services(hints=("postgres", "postgresql", "pgsql")):
    """Enumerate installed services that look like PostgreSQL.

    Reads HKLM\\SYSTEM\\CurrentControlSet\\Services (no admin required).
    CAVEAT: the registry also lists leftovers from uninstalled versions.
    Callers must validate with sc query before use.
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
            raise ServiceError(tr("err_cmd_not_found", cmd=args[0]))
        except subprocess.TimeoutExpired:
            raise ServiceError(tr("err_timeout", sec=self.timeout, cmdline=" ".join(args)))
        except OSError as e:
            raise ServiceError(tr("err_exec_failed", cmd=args[0], err=e))
        return proc.returncode, _decode(proc.stdout), _decode(proc.stderr)

    def query_raw(self):
        rc, out, err = self._run(["sc", "query", self.service_name])
        return rc, (out + "\n" + err).strip()

    def query_state(self):
        """Returns (state_token|None, raw). Raises ServiceError if unusable.
        The state token stays English (functional identifier); translation is
        a display-time concern."""
        rc, raw = self.query_raw()
        if rc == 0:
            m = _STATE_RE.search(raw)
            if m:
                code = int(m.group(1))
                return STATE_NAMES.get(code, f"UNKNOWN({code})"), raw
            return None, raw
        if _MISSING_RE.search(raw) or rc == 1060:
            raise ServiceError(tr("err_svc_missing", name=self.service_name))
        if _ACCESS_RE.search(raw) or rc == 5:
            raise ServiceError(tr("err_access_denied"))
        return None, raw or tr("sc_query_failed", rc=rc)

    def status(self):
        state, raw = self.query_state()
        if state:
            return OpResult(True, tr("status_state", state=state_label(state)), raw)
        return OpResult(True, tr("status_raw_title"), raw)

    def _new_state(self):
        try:
            return self.query_state()[0]
        except ServiceError:
            return None

    def _classify_failure(self, rc, detail, new_state, action, wanted):
        if new_state == wanted:
            return OpResult(
                True, tr("ok_reached", state=state_label(wanted)),
                detail or tr("ok_reached_detail", verb=action, wanted=state_label(wanted)))
        if _ACCESS_RE.search(detail) or rc == 5:
            return OpResult(False, tr("fail_access"), detail)
        if _MISSING_RE.search(detail) or rc == 1060:
            return OpResult(False, tr("fail_not_found", name=self.service_name), detail)
        return OpResult(
            False,
            tr("fail_op", verb=tr("verb_" + action), rc=rc,
               state=state_label(new_state) if new_state else tr("word_unknown")),
            detail,
        )

    def start(self):
        state, _ = self.query_state()
        if state == "RUNNING":
            return OpResult(True, tr("ok_already_running"))
        rc, out, err = self._run(["net", "start", self.service_name])
        return self._classify_failure(rc, (out + "\n" + err).strip(), self._new_state(), "start", "RUNNING")

    def stop(self):
        state, _ = self.query_state()
        if state == "STOPPED":
            return OpResult(True, tr("ok_already_stopped"))
        rc, out, err = self._run(["net", "stop", self.service_name])
        return self._classify_failure(rc, (out + "\n" + err).strip(), self._new_state(), "stop", "STOPPED")

    def restart(self):
        state, _ = self.query_state()
        lines = []
        if state == "RUNNING":
            r = self.stop()
            lines.append(tr("restart_stop_line", summary=r.summary))
            if not r.ok:
                return OpResult(False, tr("restart_aborted", summary=r.summary), r.detail)
        else:
            lines.append(tr("restart_skip_line",
                            state=state_label(state) if state else tr("word_unknown")))
        r = self.start()
        lines.append(tr("restart_start_line", summary=r.summary))
        return OpResult(r.ok, tr("restart_complete", lines="\n".join(lines)), r.detail)

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
            return OpResult(True, tr("ok_recovery"), detail)
        if _ACCESS_RE.search(detail) or rc == 5:
            return OpResult(False, tr("fail_access"), detail)
        return OpResult(False, tr("fail_sc_failure", rc=rc), detail)

    def set_start_type(self, start_type):
        """start_type: 'auto' | 'demand' | 'disabled' (sc.exe tokens, never localized)"""
        if start_type not in ("auto", "demand", "disabled"):
            return OpResult(False, tr("invalid_start_type", st=start_type))
        rc, out, err = self._run([
            "sc", "config", self.service_name, "start=", start_type,
        ])
        detail = (out + "\n" + err).strip()
        if rc == 0:
            return OpResult(True, tr("ok_start_type", st=start_type), detail)
        if _ACCESS_RE.search(detail) or rc == 5:
            return OpResult(False, tr("fail_access"), detail)
        return OpResult(False, tr("fail_sc_config", rc=rc), detail)


# ---------------------------------------------------------------------------
# Health classification (SCM x probe)
# ---------------------------------------------------------------------------

@dataclass
class Health:
    key: str          # language-stable identifier, used for transition detection
    label: str        # translated display label
    tone: str         # palette key: ok / warn / err / muted
    scm_state: str = ""
    probe_alive: object = None
    detail: str = ""
    error: str = ""

    @property
    def ok(self):
        return self.tone == "ok"


def state_label(state):
    """Translate an SCM state token for display; unknown tokens pass through."""
    if not state:
        return ""
    key = "st_" + state
    s = tr(key)
    return state if s == key else s


def classify_health(scm_state, probe_alive, probe_enabled, scm_error=""):
    if scm_error:
        return Health("error", tr("h_error_label"), "err", scm_state or "", probe_alive, "", scm_error)

    if not scm_state:
        return Health("unknown", tr("h_unknown_label"), "muted", "", probe_alive,
                      tr("h_unknown_detail"), "")

    pending = scm_state.endswith("_PENDING")

    if not probe_enabled or probe_alive is None:
        tone = "muted" if pending else ("ok" if scm_state == "RUNNING" else "err")
        key = scm_state + ("" if probe_enabled else "/probe-off")
        label = state_label(scm_state) + ("" if probe_enabled else tr("h_probe_off_suffix"))
        return Health(key, label, tone, scm_state, probe_alive, tr("h_scm_only_detail"), "")

    if pending:
        return Health("pending", state_label(scm_state), "warn",
                      scm_state, probe_alive, tr("h_pending_detail"), "")

    if scm_state == "RUNNING" and probe_alive:
        return Health("healthy", tr("h_healthy_label"), "ok", scm_state, probe_alive,
                      tr("h_healthy_detail"), "")
    if scm_state == "RUNNING" and not probe_alive:
        return Health("wedged", tr("h_wedged_label"), "warn", scm_state, probe_alive,
                      tr("h_wedged_detail"), "")
    if scm_state == "STOPPED" and not probe_alive:
        return Health("down", tr("h_down_label"), "err", scm_state, probe_alive,
                      tr("h_down_detail"), "")
    if scm_state == "STOPPED" and probe_alive:
        return Health("port_busy", tr("h_port_busy_label"), "warn", scm_state, probe_alive,
                      tr("h_port_busy_detail"), "")
    return Health(scm_state.lower(), state_label(scm_state), "warn",
                  scm_state, probe_alive, "", "")


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
        return OpResult(False, tr("pgadmin_no_path"))
    expanded = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isfile(expanded):
        return OpResult(False, tr("pgadmin_not_found", path=expanded))
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
        return OpResult(False, tr("pgadmin_launch_failed", err=e))
    return OpResult(True, tr("pgadmin_launched", path=expanded))


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
# Single-instance enforcement
# ---------------------------------------------------------------------------

_INSTANCE_MUTEX = None   # module global on purpose - see acquire_single_instance


def acquire_single_instance():
    """Acquire a named mutex so only one instance can run.

    Returns the handle, or None if another instance already holds it.
    The handle is stored in a module global deliberately: if it is garbage
    collected, CPython closes it and the mutex silently vanishes.

    ctypes notes (both are classic silent-corruption bugs on 64-bit Windows):
      * HANDLE is pointer-sized - without restype=c_void_p the default c_int
        truncates 64-bit handles.
      * Without argtypes, str arguments are marshalled as ANSI, which would
        feed garbage to W-suffixed APIs.
    """
    global _INSTANCE_MUTEX
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.GetLastError.restype = ctypes.c_uint
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    handle = kernel32.CreateMutexW(None, False, APP_DIR_NAME + ".SingleInstance")
    if not handle:
        return None
    if kernel32.GetLastError() == 183:          # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return None
    _INSTANCE_MUTEX = handle
    return handle


def notify_already_running():
    """Deliberately English: fires before config (and thus language) is read."""
    user32 = ctypes.windll.user32
    user32.MessageBoxW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                   ctypes.c_wchar_p, ctypes.c_uint]
    user32.MessageBoxW.restype = ctypes.c_int
    user32.MessageBoxW(
        None,
        "PostgreSQL Service Control is already running.\n\n"
        "If you cannot see its window, check the system tray for its icon.",
        "PostgreSQL Service Control",
        0x40,                                    # MB_ICONINFORMATION
    )


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
    """In-app editor for config.json. Modal; writes through Config.save().

    advanced_enabled is deliberately NOT editable here: the config file is
    the only unlock path for SCM write operations (production safety).
    Language changes apply after restart (fixed for process lifetime).
    """

    def __init__(self, master, cfg):
        super().__init__(master)
        self.cfg = cfg
        self.saved = False
        self.title(tr("dlg_title"))
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
        svc = ttk.LabelFrame(outer, text=tr("frame_service"))
        svc.pack(fill="x", pady=(0, 8))

        row = ttk.Frame(svc, padding=(8, 6))
        row.pack(fill="x")
        ttk.Label(row, text=tr("lbl_service_name"), width=18, anchor="w").pack(side="left")
        self.vars["service_name"] = tk.StringVar(value=self.cfg.get("service_name"))
        self.svc_combo = ttk.Combobox(row, textvariable=self.vars["service_name"], width=42)
        self.svc_combo.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text=tr("btn_scan"), width=8, command=self._scan).pack(side="left", padx=(6, 0))

        row = ttk.Frame(svc, padding=(8, 0, 8, 4))
        row.pack(fill="x")
        ttk.Label(row, text=tr("lbl_start_type"), width=18, anchor="w").pack(side="left")
        self.start_type_var = tk.StringVar(value=tr("st_query"))
        # Querying start type is READ-only and stays always enabled.
        # The functional values stay English sc.exe tokens - never localized.
        self.st_combo = ttk.Combobox(
            row, textvariable=self.start_type_var, width=14, state="readonly",
            values=[tr("st_query"), "auto", "demand", "disabled"],
        )
        self.st_combo.pack(side="left")

        # WRITE operations are gated behind advanced_enabled.
        self.st_apply_btn = ttk.Button(row, text=tr("btn_apply_start_type"),
                                       command=self._apply_start_type)
        self.st_apply_btn.pack(side="left", padx=6)
        self.recovery_btn = ttk.Button(row, text=tr("btn_recovery"),
                                       command=self._recovery)
        self.recovery_btn.pack(side="left")

        if not bool(self.cfg.get("advanced_enabled")):
            self.st_apply_btn.state(["disabled"])
            self.recovery_btn.state(["disabled"])
            ttk.Label(
                svc, text=tr("advanced_locked_hint"),
                font=("Segoe UI", 8), foreground=self.master.pal["muted"],
                wraplength=560, justify="left",
            ).pack(fill="x", padx=8, pady=(0, 6))

        # --- Probe --------------------------------------------------------
        pr = ttk.LabelFrame(outer, text=tr("frame_probe"))
        pr.pack(fill="x", pady=(0, 8))

        row = ttk.Frame(pr, padding=(8, 6))
        row.pack(fill="x")
        self.vars["probe_enabled"] = tk.BooleanVar(value=bool(self.cfg.get("probe_enabled")))
        ttk.Checkbutton(row, text=tr("chk_enable_probe"), variable=self.vars["probe_enabled"]).pack(side="left")
        ttk.Label(row, text=tr("lbl_host")).pack(side="left", padx=(16, 4))
        self.vars["host"] = tk.StringVar(value=str(self.cfg.get("host") or ""))
        ttk.Entry(row, textvariable=self.vars["host"], width=18).pack(side="left")
        ttk.Label(row, text=tr("lbl_port")).pack(side="left", padx=(12, 4))
        self.vars["port"] = tk.StringVar(value=str(self.cfg.get("port")))
        ttk.Entry(row, textvariable=self.vars["port"], width=7).pack(side="left")
        ttk.Label(row, text=tr("lbl_probe_timeout")).pack(side="left", padx=(12, 4))
        self.vars["probe_timeout_seconds"] = tk.StringVar(value=str(self.cfg.get("probe_timeout_seconds")))
        ttk.Entry(row, textvariable=self.vars["probe_timeout_seconds"], width=5).pack(side="left")

        # --- pgAdmin ------------------------------------------------------
        pa = ttk.LabelFrame(outer, text=tr("frame_pgadmin"))
        pa.pack(fill="x", pady=(0, 8))
        row = ttk.Frame(pa, padding=(8, 6))
        row.pack(fill="x")
        self.vars["pgadmin_path"] = tk.StringVar(value=self.cfg.get("pgadmin_path"))
        ttk.Entry(row, textvariable=self.vars["pgadmin_path"]).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text=tr("btn_browse"), width=10, command=self._browse).pack(side="left", padx=(6, 0))

        # --- Behaviour ----------------------------------------------------
        bh = ttk.LabelFrame(outer, text=tr("frame_behaviour"))
        bh.pack(fill="x", pady=(0, 8))

        grid = ttk.Frame(bh, padding=(8, 6))
        grid.pack(fill="x")

        self.vars["auto_refresh"] = tk.BooleanVar(value=bool(self.cfg.get("auto_refresh")))
        self.vars["minimize_to_tray"] = tk.BooleanVar(value=bool(self.cfg.get("minimize_to_tray")))
        self.vars["toast_enabled"] = tk.BooleanVar(value=bool(self.cfg.get("toast_enabled")))
        self.vars["log_enabled"] = tk.BooleanVar(value=bool(self.cfg.get("log_enabled")))

        ttk.Checkbutton(grid, text=tr("chk_auto_refresh_status"),
                        variable=self.vars["auto_refresh"]).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Checkbutton(grid, text=tr("chk_minimize_to_tray"),
                        variable=self.vars["minimize_to_tray"]).grid(row=0, column=1, sticky="w", pady=2, padx=(20, 0))

        self.vars["tray_enabled"] = tk.BooleanVar(value=bool(self.cfg.get("tray_enabled")))
        self.tray_chk = ttk.Checkbutton(grid, text=tr("chk_system_tray"), variable=self.vars["tray_enabled"])
        self.tray_chk.grid(row=1, column=0, sticky="w", pady=2)
        if not TRAY_AVAILABLE:
            self.tray_chk.state(["disabled"])
            self.vars["tray_enabled"].set(False)

        ttk.Checkbutton(grid, text=tr("chk_toast"),
                        variable=self.vars["toast_enabled"]).grid(row=1, column=1, sticky="w", pady=2, padx=(20, 0))

        ttk.Checkbutton(grid, text=tr("chk_write_log"),
                        variable=self.vars["log_enabled"]).grid(row=2, column=0, sticky="w", pady=2)

        ttk.Label(grid, text=tr("lbl_theme")).grid(row=2, column=1, sticky="e", padx=(20, 4), pady=2)
        self.vars["theme"] = tk.StringVar(value=self.cfg.get("theme"))
        ttk.Combobox(grid, textvariable=self.vars["theme"], width=8, state="readonly",
                     values=list(VALID_THEMES)).grid(row=2, column=2, sticky="w", pady=2)

        ttk.Label(grid, text=tr("lbl_language")).grid(row=3, column=1, sticky="e", padx=(20, 4), pady=2)
        self.vars["language_display"] = tk.StringVar(
            value=dict(LANG_DISPLAY).get(self.cfg.get("language"), "English"))
        ttk.Combobox(grid, textvariable=self.vars["language_display"], width=8, state="readonly",
                     values=[d for _, d in LANG_DISPLAY]).grid(row=3, column=2, sticky="w", pady=2)

        row = ttk.Frame(bh, padding=(8, 0, 8, 6))
        row.pack(fill="x")
        for label_key, key, width in (
            ("lbl_refresh_s", "refresh_interval_seconds", 5),
            ("lbl_op_timeout", "operation_timeout_seconds", 6),
            ("lbl_log_mb", "log_max_mb", 5),
            ("lbl_backups", "log_backups", 5),
        ):
            ttk.Label(row, text=tr(label_key)).pack(side="left", padx=(0 if key == "refresh_interval_seconds" else 12, 4))
            self.vars[key] = tk.StringVar(value=str(self.cfg.get(key)))
            ttk.Entry(row, textvariable=self.vars[key], width=width).pack(side="left")

        if not TRAY_AVAILABLE:
            tk.Label(
                outer, anchor="w", justify="left", wraplength=560,
                text=tr("tray_missing_dlg", err=TRAY_IMPORT_ERROR),
                font=("Segoe UI", 8),
            ).pack(fill="x", pady=(0, 6))

        # --- Buttons ------------------------------------------------------
        btns = ttk.Frame(outer)
        btns.pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text=tr("btn_reset_defaults"), command=self._reset).pack(side="left")
        ttk.Button(btns, text=tr("btn_open_config"), command=self._open_file).pack(side="left", padx=6)
        ttk.Button(btns, text=tr("btn_cancel"), command=self._cancel).pack(side="right")
        ttk.Button(btns, text=tr("btn_apply"), command=self._apply).pack(side="right", padx=6)

    # -- actions ----------------------------------------------------------
    def _scan(self):
        self.master._set_status(tr("dlg_status_scanning"))
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
        self.master._set_status(tr("status_ready"))
        if err:
            messagebox.showerror(tr("scan_failed_title"), err, parent=self)
            return
        if not services:
            messagebox.showinfo(tr("scan_none_title"), tr("scan_none"), parent=self)
            return
        names = [n for n, _ in services]
        self.svc_combo.configure(values=names)
        listing = "\n".join(f"{n}  -  {d}" for n, d in services)
        log.info("Service discovery found %d candidate(s): %s", len(services), ", ".join(names))
        messagebox.showinfo(
            tr("scan_done_title"),
            tr("scan_done_body", n=len(services), listing=listing),
            parent=self,
        )

    def _browse(self):
        initial = self.vars["pgadmin_path"].get() or "C:\\Program Files"
        path = filedialog.askopenfilename(
            parent=self, title=tr("frame_pgadmin"),
            initialdir=os.path.dirname(initial) if os.path.isdir(os.path.dirname(initial)) else "C:\\",
            filetypes=[("pgAdmin", "pgAdmin4.exe"), ("Executables", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self.vars["pgadmin_path"].set(path)

    def _gate_or_warn(self):
        """Defense in depth: re-check advanced_enabled even though the
        write buttons are disabled when locked."""
        if not bool(self.cfg.get("advanced_enabled")):
            messagebox.showwarning(tr("locked_title"), tr("locked_body"), parent=self)
            return False
        return True

    def _apply_start_type(self):
        if not self._gate_or_warn():
            return
        st = self.start_type_var.get()
        # Anything that is not a real sc.exe token means "(query)".
        if st not in ("auto", "demand", "disabled"):
            self._query_start_type()
            return
        svc = self.vars["service_name"].get().strip()
        if not svc:
            messagebox.showwarning(tr("title_start_type"), tr("msg_svc_empty"), parent=self)
            return
        self.master._set_status(tr("dlg_status_set_st", st=st))
        threading.Thread(target=self._op_worker, args=("set_start_type", svc, st), daemon=True).start()

    def _query_start_type(self):
        svc = self.vars["service_name"].get().strip()
        if not svc:
            return
        self.master._set_status(tr("dlg_status_query_st"))
        threading.Thread(target=self._op_worker, args=("query_start_type", svc, None), daemon=True).start()

    def _recovery(self):
        if not self._gate_or_warn():
            return
        svc = self.vars["service_name"].get().strip()
        if not svc:
            messagebox.showwarning(tr("title_recovery"), tr("msg_svc_empty"), parent=self)
            return
        if not messagebox.askyesno(
            tr("recovery_confirm_title"),
            tr("recovery_confirm_body", svc=svc),
            parent=self,
        ):
            return
        self.master._set_status(tr("dlg_status_recovery"))
        threading.Thread(target=self._op_worker, args=("recovery", svc, None), daemon=True).start()

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
            return OpResult(False, tr("start_type_read_failed", err=e))
        names = {0: "boot", 1: "system", 2: "auto", 3: "demand", 4: "disabled"}
        token = names.get(start, f"unknown({start})")
        display = tr("stt_" + token) if token in names else token
        return OpResult(True, tr("start_type_current", name=display),
                        tr("start_type_reg_detail", v=start))

    def _op_done(self, kind, res):
        self.master._set_status(tr("status_ready"))
        log.info("config-dialog %s -> ok=%s : %s", kind, res.ok, res.summary)
        if kind == "query_start_type" and res.ok:
            m = re.search(r"=\s*(\d+)", res.detail)
            if m:
                names = {"0": "boot", "1": "system", "2": "auto", "3": "demand", "4": "disabled"}
                token = names.get(m.group(1))
                self.start_type_var.set(tr("stt_" + token) if token else tr("st_query"))
        if res.ok:
            messagebox.showinfo(tr("dlg_success_title"),
                                res.summary + ("\n\n" + res.detail if res.detail else ""), parent=self)
        else:
            messagebox.showerror(tr("dlg_failed_title"),
                                 res.summary + ("\n\n" + res.detail if res.detail else ""), parent=self)

    def _reset(self):
        if not messagebox.askyesno(tr("reset_confirm_title"), tr("reset_confirm_body"), parent=self):
            return
        self.cfg.reset_to_defaults()
        self.saved = True
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

    def _open_file(self):
        if not self.cfg.path:
            self.cfg.save()
        if not self.cfg.path or not os.path.isfile(self.cfg.path):
            messagebox.showerror(tr("title_config"), tr("open_config_missing"), parent=self)
            return
        try:
            os.startfile(self.cfg.path)
        except OSError as e:
            messagebox.showerror(tr("title_config"),
                                 tr("open_config_err", path=self.cfg.path, err=e), parent=self)

    def _collect(self):
        """Validate dialog fields into a config dict. Raises ValueError on bad input.

        NOTE: advanced_enabled is copied through untouched - it cannot be
        changed from this dialog by design.
        """
        out = dict(self.cfg.data)
        svc = self.vars["service_name"].get().strip()
        if not svc:
            raise ValueError(tr("v_svc_empty"))
        out["service_name"] = svc
        out["pgadmin_path"] = self.vars["pgadmin_path"].get().strip()
        out["host"] = self.vars["host"].get().strip()

        for key, label_key, lo, hi in (
            ("port", "lbl_port", 1, 65535),
            ("probe_timeout_seconds", "lbl_probe_timeout", 1, 60),
            ("refresh_interval_seconds", "lbl_refresh_s", 1, 3600),
            ("operation_timeout_seconds", "lbl_op_timeout", 5, 600),
            ("log_max_mb", "lbl_log_mb", 1, 512),
            ("log_backups", "lbl_backups", 0, 99),
        ):
            label = tr(label_key).rstrip(":")
            try:
                v = int(str(self.vars[key].get()).strip())
            except (TypeError, ValueError):
                raise ValueError(tr("v_int", label=label))
            if not (lo <= v <= hi):
                raise ValueError(tr("v_range", label=label, lo=lo, hi=hi))
            out[key] = v

        for key in ("probe_enabled", "auto_refresh", "tray_enabled",
                    "minimize_to_tray", "toast_enabled", "log_enabled"):
            out[key] = bool(self.vars[key].get())

        out["theme"] = self.vars["theme"].get()
        if out["theme"] not in VALID_THEMES:
            raise ValueError(tr("v_theme", themes="/".join(VALID_THEMES)))

        lang_map = {d: c for c, d in LANG_DISPLAY}
        lang = lang_map.get(self.vars["language_display"].get())
        if lang not in LANGUAGES:
            raise ValueError(tr("v_language"))
        out["language"] = lang

        if out["tray_enabled"] and not TRAY_AVAILABLE:
            raise ValueError(tr("v_tray_missing"))
        return out

    def _apply(self):
        try:
            new = self._collect()
        except ValueError as e:
            messagebox.showerror(tr("invalid_config_title"), str(e), parent=self)
            return
        old = dict(self.cfg.data)
        self.cfg.data = new
        self.cfg.warnings = []
        if not self.cfg.save():
            rendered = "\n".join(tr(k, **p) for k, p in self.cfg.warnings)
            messagebox.showerror(tr("save_failed_title"),
                                 tr("save_failed_body", warnings=rendered), parent=self)
            self.cfg.data = old
            return
        self.saved = True
        log.info("Configuration updated via dialog; saved to %s", self.cfg.path)
        if new["language"] != old.get("language"):
            log.info("Language changed %s -> %s (applies after restart)",
                     old.get("language"), new["language"])
            messagebox.showinfo(tr("lang_restart_title"), tr("lang_restart_body"), parent=self)
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

    def _cancel(self):
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()

        self.cfg = Config()
        set_language(self.cfg.get("language"))     # fixed for process lifetime
        self.log_path = setup_logging(self.cfg)
        log.info("=" * 70)
        log.info("Startup | admin=%s | frozen=%s | python=%s | lang=%s | config=%s",
                 is_admin(), getattr(sys, "frozen", False),
                 sys.version.split()[0], _LANG, self.cfg.path)
        if not TRAY_AVAILABLE:
            log.warning("pystray/Pillow unavailable (%s) - tray and toasts disabled.",
                        TRAY_IMPORT_ERROR)

        self.title(tr("app_title"))
        self.geometry("680x620")
        self.minsize(600, 540)

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
        self._build_menu()
        self._bind_accelerators()
        self._build_widgets()
        self._apply_theme()
        self._log_startup_warnings()
        self._check_pgadmin_path()
        self._update_admin_banner()
        self._build_tray()

        self.after(100, self._drain_queues)
        self._schedule_refresh(immediate=True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Unmap>", self._on_unmap)

    # -- style / theme ----------------------------------------------------
    def _build_style(self):
        self.style = ttk.Style(self)

    def _build_menu(self):
        """Menu bar. Accelerator labels are display-only; the actual key
        bindings live in _bind_accelerators."""
        self.menubar = tk.Menu(self)

        m_op = tk.Menu(self.menubar, tearoff=0)
        m_op.add_command(label=tr("menu_start"), accelerator="Alt+1",
                         command=lambda: self._dispatch("start"))
        m_op.add_command(label=tr("menu_stop"), accelerator="Alt+2",
                         command=lambda: self._dispatch("stop"))
        m_op.add_command(label=tr("menu_restart"), accelerator="Alt+3",
                         command=lambda: self._dispatch("restart"))
        m_op.add_command(label=tr("menu_refresh"), accelerator="Alt+4",
                         command=lambda: self._dispatch("refresh"))
        m_op.add_separator()
        m_op.add_command(label=tr("menu_pgadmin"), accelerator="Alt+5",
                         command=lambda: self._dispatch("pgadmin"))
        self.menubar.add_cascade(label=tr("menu_service"), menu=m_op)

        m_tools = tk.Menu(self.menubar, tearoff=0)
        m_tools.add_command(label=tr("menu_config"), accelerator="Alt+C",
                            command=self._open_config_dialog)
        m_tools.add_command(label=tr("menu_reload"), command=self._reload_config)
        self.menubar.add_cascade(label=tr("menu_tools"), menu=m_tools)

        m_exit = tk.Menu(self.menubar, tearoff=0)
        m_exit.add_command(label=tr("menu_exit"), accelerator="Esc", command=self._on_close)
        self.menubar.add_cascade(label=tr("menu_file"), menu=m_exit)

        self.config(menu=self.menubar)

    def _bind_accelerators(self):
        """Alt chords are bound on the typed CHARACTER, so Alt+C works on
        Latin layouts and Alt+С (Cyrillic es, same physical key) works on
        Russian layouts. Digits are layout-stable.

        While any grab is active (modal dialog, open Combobox popup), action
        shortcuts are suppressed so they never act behind a dialog's back.
        """
        self.bind_all("<Alt-KeyPress>", self._accel_alt_key, add="+")
        self.bind_all("<Escape>", self._accel_escape, add="+")

    def _accel_alt_key(self, event):
        if self.grab_current() is not None:
            return
        ch = (event.char or "").lower()
        action = {
            "1": "start", "2": "stop", "3": "restart", "4": "refresh", "5": "pgadmin",
            "c": "config", "\u0441": "config",   # Latin c + Cyrillic с
        }.get(ch)
        if action is None:
            return
        if action == "config":
            self._open_config_dialog()
        else:
            self._dispatch(action)

    def _accel_escape(self, event):
        if self.grab_current() is not None:
            return
        self._on_close()

    def _apply_theme(self):
        pal = self.pal
        dark = self.theme == "dark"
        try:
            self.style.theme_use("clam" if dark else "vista")
        except tk.TclError:
            try:
                self.style.theme_use("clam")
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

        # Menu bar (tk.Menu is not themed by ttk)
        try:
            self.menubar.config(bg=pal["card"], fg=pal["fg"], activebackground=pal["accent"],
                                activeforeground=pal["card_fg"], relief="flat", bd=0)
            for i in range(self.menubar.index("end") + 1):
                sub = self.menubar.entrycget(i, "menu")
                if sub:
                    self.nametowidget(sub).config(
                        bg=pal["card"], fg=pal["fg"], activebackground=pal["accent"],
                        activeforeground=pal["card_fg"])
        except (tk.TclError, AttributeError):
            pass

    def _set_theme(self, name):
        if name not in VALID_THEMES or name == self.theme:
            return
        self.theme = name
        self.pal = DARK if name == "dark" else LIGHT
        self._apply_theme()
        log.info("Theme switched to '%s'", name)

    # -- widgets ----------------------------------------------------------
    def _build_widgets(self):
        self.statusbar = tk.Label(self, text=tr("status_ready"), anchor="w", padx=6, pady=3)
        self.statusbar.pack(fill="x", side="bottom")

        self.body = ttk.Frame(self)
        self.body.pack(side="top", fill="both", expand=True)

        # Service info
        self.info_frame = ttk.LabelFrame(self.body, text=tr("frame_service"))
        self.info_frame.pack(fill="x", padx=10, pady=(8, 4))

        row1 = ttk.Frame(self.info_frame)
        row1.pack(fill="x", padx=8, pady=4)
        ttk.Label(row1, text=tr("lbl_name"), width=9, anchor="w").pack(side="left")
        self.service_name_var = tk.StringVar(value=self.cfg.get("service_name"))
        ttk.Label(row1, textvariable=self.service_name_var,
                  font=("Consolas", 10, "bold")).pack(side="left")

        row2 = ttk.Frame(self.info_frame)
        row2.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Label(row2, text=tr("lbl_health"), width=9, anchor="w").pack(side="left")
        self.status_indicator = tk.Label(row2, text=" \u25cf ", font=("Segoe UI", 14, "bold"), fg="#888888")
        self.status_indicator.pack(side="left", padx=(0, 4))
        self.status_var = tk.StringVar(value=tr("status_checking"))
        ttk.Label(row2, textvariable=self.status_var,
                  font=("Segoe UI", 10, "bold")).pack(side="left")

        row3 = ttk.Frame(self.info_frame)
        row3.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(row3, text=tr("lbl_detail"), width=9, anchor="w").pack(side="left")
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
        self.elevate_btn = ttk.Button(self.admin_frame, text=tr("btn_relaunch"),
                                      command=self._relaunch_as_admin, width=22)
        self.elevate_btn.pack(side="right", padx=8)

        # Buttons
        btns = ttk.Frame(self.body)
        btns.pack(fill="x", padx=10, pady=4)
        self.btn_start = ttk.Button(btns, text=tr("btn_start"), command=lambda: self._dispatch("start"))
        self.btn_stop = ttk.Button(btns, text=tr("btn_stop"), command=lambda: self._dispatch("stop"))
        self.btn_restart = ttk.Button(btns, text=tr("btn_restart"), command=lambda: self._dispatch("restart"))
        self.btn_refresh = ttk.Button(btns, text=tr("btn_refresh"), command=lambda: self._dispatch("refresh"))
        self.btn_pgadmin = ttk.Button(btns, text=tr("btn_pgadmin"), command=lambda: self._dispatch("pgadmin"))
        for b in (self.btn_start, self.btn_stop, self.btn_restart, self.btn_refresh, self.btn_pgadmin):
            b.pack(side="left", expand=True, fill="x", padx=3)

        # Options row
        opts = ttk.Frame(self.body)
        opts.pack(fill="x", padx=10, pady=(0, 2))
        self.auto_var = tk.BooleanVar(value=bool(self.cfg.get("auto_refresh")))
        ttk.Checkbutton(opts, text=tr("chk_auto_refresh"), variable=self.auto_var,
                        command=self._on_auto_toggle).pack(side="left")
        self.probe_var = tk.BooleanVar(value=bool(self.cfg.get("probe_enabled")))
        ttk.Checkbutton(opts, text=tr("chk_health_probe"), variable=self.probe_var,
                        command=self._on_probe_toggle).pack(side="left", padx=(12, 0))
        ttk.Button(opts, text=tr("btn_configuration"), command=self._open_config_dialog).pack(side="right")
        ttk.Button(opts, text=tr("btn_reload_config"), command=self._reload_config).pack(side="right", padx=4)

        # Output (tk.Text + ttk.Scrollbar so it themes correctly)
        logf = ttk.LabelFrame(self.body, text=tr("frame_output"))
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
        self.admin_label.config(text=tr("banner_no_admin"))
        self.admin_frame.pack(side="top", fill="x", padx=10, pady=(8, 0),
                              before=self.info_frame)

    # -- startup checks -----------------------------------------------------
    def _log_startup_warnings(self):
        for key, params in self.cfg.warnings:
            text = tr(key, **params)
            self._append_log(f"[config] {text}", "err")
            log.warning("config: %s", tr_en(key, **params))   # audit log stays English
        if self.log_path:
            self._append_log(tr("log_writing_to", path=self.log_path), "hdr")
        if not TRAY_AVAILABLE:
            self._append_log(tr("log_tray_missing", err=TRAY_IMPORT_ERROR), "warn")
        if self.cfg.get("advanced_enabled"):
            self._append_log(tr("log_advanced_unlocked"), "warn")
            log.warning("advanced_enabled is TRUE - SCM write operations unlocked.")

    def _check_pgadmin_path(self):
        """One-time validation of pgadmin_path.

        Catches the common failure mode where a PostgreSQL/pgAdmin minor
        update moves the exe and the button silently breaks until needed.
        """
        raw = self.cfg.get("pgadmin_path") or ""
        if not raw.strip():
            self._append_log(tr("log_path_empty"), "warn")
            log.warning("pgadmin_path is empty.")
            return
        expanded = os.path.expandvars(os.path.expanduser(raw.strip()))
        if os.path.isfile(expanded):
            self._append_log(tr("log_path_ok", path=expanded), "hdr")
            log.info("pgAdmin path verified: %s", expanded)
        else:
            self._append_log(tr("log_path_missing", path=expanded), "warn")
            log.warning("pgAdmin executable not found at configured path: %s", expanded)

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
                pystray.MenuItem(tr("tray_show_hide"), lambda i, it: self._cmds.put("toggle"), default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(tr("menu_start"), lambda i, it: self._cmds.put("start")),
                pystray.MenuItem(tr("menu_stop"), lambda i, it: self._cmds.put("stop")),
                pystray.MenuItem(tr("menu_restart"), lambda i, it: self._cmds.put("restart")),
                pystray.MenuItem(tr("menu_refresh"), lambda i, it: self._cmds.put("refresh")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(tr("menu_pgadmin"), lambda i, it: self._cmds.put("pgadmin")),
                pystray.MenuItem(tr("chk_auto_refresh"),
                                 lambda i, it: self._cmds.put("toggle_auto"),
                                 checked=lambda it: bool(self.auto_var.get())),
                pystray.MenuItem(tr("tray_notifications"),
                                 lambda i, it: self._cmds.put("toggle_toast"),
                                 checked=lambda it: bool(self.cfg.get("toast_enabled"))),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(tr("tray_quit"), lambda i, it: self._cmds.put("quit")),
            )
            self.icon = pystray.Icon(APP_DIR_NAME, self._tray_icon_image("muted"),
                                     tr("app_title"), menu)
            self.icon.run_detached()
            log.info("System tray icon started.")
        except Exception as e:
            self.icon = None
            log.error("Failed to start tray icon: %s: %s", type(e).__name__, e)
            self._append_log(f"[tray] {type(e).__name__}: {e}", "err")

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

    # -- notifications ------------------------------------------------------
    def _toast(self, title, message):
        """Windows toast via the tray icon (Shell_NotifyIcon NIF_INFO).

        Hard requirement: an ACTIVE tray icon. Windows has no stdlib toast
        channel that bypasses the tray. Every decision is logged so silent
        failures are diagnosable in service_control.log.
        """
        log.info("NOTIFY | %s | %s", title, message.replace("\n", " "))

        if not self.cfg.get("toast_enabled"):
            log.info("Toast skipped: toast_enabled=false.")
            self._set_status(f"{title}: {message.splitlines()[0] if message else ''}")
            return

        if self.icon is None:
            log.warning("Toast skipped: no tray icon active. "
                        "Toasts require pystray+Pillow and tray_enabled=true.")
            self._beep()
            self._set_status(f"{title}: {message.splitlines()[0] if message else ''}")
            return

        try:
            self.icon.notify(message, title)
            log.info("Toast dispatched to tray icon.")
            return
        except Exception as e:
            log.warning("icon.notify failed: %s: %s - falling back to beep.",
                        type(e).__name__, e)
            self._beep()
            self._set_status(f"{title}: {message.splitlines()[0] if message else ''}")

    def _beep(self):
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass

    def _toggle_toast(self):
        # Guard: if the Configuration dialog is open (modal grab), ignore the
        # tray toggle - otherwise the dialog's Apply would silently overwrite it.
        if self.grab_current() is not None:
            return
        new = not bool(self.cfg.get("toast_enabled"))
        self.cfg.data["toast_enabled"] = new
        if not self.cfg.save():
            self._append_log(tr("w_save_failed"), "err")
        self._append_log(tr("notify_enabled") if new else tr("notify_disabled"), "hdr")
        log.info("toast_enabled=%s (toggled from tray)", new)

    # -- dispatch / workers ----------------------------------------------
    def _dispatch(self, action):
        if self.busy:
            self._set_status(tr("status_busy"))
            return
        self.busy = True
        self._set_buttons("disabled")
        self._set_status(tr("disp_" + action))
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
                                     self.cfg.get("operation_timeout_seconds", 300))
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
                self._handle_cmd(self._cmds.get_nowait())
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

                # Toast on every manual operation result (success AND failure).
                # Refresh stays silent by design.
                if outcome.action in ("start", "stop", "restart", "pgadmin"):
                    body = tr("toast_body", action=tr("action_" + outcome.action),
                              summary=outcome.result.summary)
                    if outcome.result.ok:
                        self._toast(tr("toast_op_complete"), body)
                    else:
                        self._toast(tr("toast_op_failed"), body)

                self._set_status(tr("status_done") if outcome.result.ok
                                 else tr("status_done_errors"))
                processed = True
        except queue.Empty:
            pass

        self.after(100 if processed else 150, self._drain_queues)

    def _handle_cmd(self, cmd):
        if cmd == "quit":
            self._on_close(force=True)
        elif cmd == "toggle":
            self._toggle_visibility()
        elif cmd == "toggle_auto":
            self.auto_var.set(not self.auto_var.get())
            self._on_auto_toggle()
        elif cmd == "toggle_toast":
            self._toggle_toast()
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
                self._toast(tr("toast_transition_title"),
                            tr("toast_transition_body",
                               svc=self.cfg.get("service_name"), label=h.label))
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
        header = tr("log_result_header", action=tr("action_" + action),
                    time=time.strftime("%H:%M:%S"))
        self._append_log(header, "hdr")
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
            messagebox.showerror(tr("dlg_title"), tr("dlg_open_failed", err=f"{type(e).__name__}: {e}"))
            return
        self.wait_window(dlg)
        if dlg.saved:
            self._after_config_change(reinit_logging=True)

    def _reload_config(self):
        self.cfg.load()
        setup_logging(self.cfg)
        self._after_config_change(reinit_logging=False)
        self._set_status(tr("reload_status", path=self.cfg.path or tr("word_defaults")))

    def _after_config_change(self, reinit_logging=True):
        if reinit_logging:
            setup_logging(self.cfg)
            log.info("Logging reinitialised after config change.")

        self.service_name_var.set(self.cfg.get("service_name"))
        self.auto_var.set(bool(self.cfg.get("auto_refresh")))
        self.probe_var.set(bool(self.cfg.get("probe_enabled")))
        self._set_theme(self.cfg.get("theme", "light"))
        # NOTE: language is intentionally NOT applied here - restart required.

        self._log_startup_warnings()
        self._check_pgadmin_path()
        self._build_tray()
        self._health_seeded = False
        self._last_health_key = None
        self._schedule_refresh(immediate=True)

    def _relaunch_as_admin(self):
        if is_admin():
            messagebox.showinfo(tr("msg_already_elevated_title"), tr("msg_already_elevated"))
            return
        log.info("Requesting elevation via UAC.")
        if request_elevation():
            self._closing = True
            self.destroy()
        else:
            messagebox.showwarning(tr("msg_elevation_cancelled_title"),
                                   tr("msg_elevation_cancelled"))

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
            self._set_status(tr("status_minimized"))
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
    # Single instance: refuse a second copy before creating any window.
    # (The already-running message is deliberately English: it fires before
    # config - and therefore the language - is read.)
    if acquire_single_instance() is None:
        notify_already_running()
        return 0

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