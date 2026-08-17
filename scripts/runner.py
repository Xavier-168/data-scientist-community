import errno
import json
import mimetypes
import os
import pathlib
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import glob
import zipfile
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pandas as pd

# Ensure scripts directory is on sys.path for sibling imports.
_SELF_DIR = os.path.dirname(os.path.abspath(__file__))
if _SELF_DIR not in sys.path:
    sys.path.insert(0, _SELF_DIR)

from analytics_engine import archive_snapshot_from_excel, compute_dashboard, list_recent_runs
from client_license import LICENSE_VERIFY_CACHE_TTL_SECONDS, LicenseManager
from feedback_manager import send_feedback
from package_identity import verify_package_manifest
from runtime_paths import read_package_id, resolve_auth_dir, resolve_downloads_dir, resolve_state_dir, seed_state_from_bundle
from runner_io import (
    ensure_parent_dir as _ensure_parent_dir,
    load_json_dict as _load_json_dict,
    path_within_root as _path_within_root,
    read_json_file,
    remove_file_if_exists as _remove_file_if_exists,
    remove_tree_if_exists as _remove_tree_if_exists,
    write_json_file_atomically as _write_json_file_atomically,
)
from runner_platforms import (
    PLATFORM_LABELS,
    PLATFORM_PROFILE_PREFIX,
    VALID_PLATFORM_IDS,
    normalize_platform_ids as _normalize_platform_ids,
    platform_label as _platform_label,
)
from runner_process import (
    clear_profile_lock_files as _clear_profile_lock_files,
    command_uses_profile as _command_uses_profile,
    pid_alive as _pid_alive,
    resolve_default_node_bin,
    sort_existing_paths as _sort_existing_paths,
    terminate_profile_browsers as _terminate_profile_browsers,
)
from orchestration.run_lease import LeaseToken, RunLeaseStore
from orchestration.subprocess_supervisor import run_supervised
from orchestration.collection_scheduler import PlatformResult, run_bounded
from orchestration.run_artifacts import ArtifactValidationError, RunWorkspace
from update_manager import (
    check_for_update,
    current_package_info,
    download_update,
    get_download_progress,
    install_downloaded_update,
    reveal_path,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.abspath(os.environ.get("YIRENGONGIS_BASE_DIR", os.path.join(SCRIPT_DIR, "..")))
STATE_DIR = seed_state_from_bundle(BASE_DIR)
PACKAGE_ID = read_package_id(BASE_DIR)
DOWNLOADS_DIR = resolve_downloads_dir(BASE_DIR, STATE_DIR)
AUTH_DIR = resolve_auth_dir(BASE_DIR, STATE_DIR)

_PACKAGE_IDENTITY_STATUS = verify_package_manifest(BASE_DIR)
if not _PACKAGE_IDENTITY_STATUS.get("ok"):
    raise RuntimeError(f"package identity verification failed: {_PACKAGE_IDENTITY_STATUS.get('error')}")


def _default_playwright_browsers_dir() -> str:
    configured = str(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "") or "").strip()
    if configured:
        return configured
    bundled = os.path.join(BASE_DIR, "runtime", "playwright-browsers")
    if os.path.isdir(bundled):
        return bundled
    state_browsers = os.path.join(STATE_DIR, ".playwright-browsers")
    if os.path.isdir(state_browsers):
        return state_browsers
    if os.name == "nt":
        # Playwright 每用户默认注册表（npx playwright install 的默认落点）。
        local_app_data = str(os.environ.get("LOCALAPPDATA", "") or "").strip()
        if local_app_data:
            default_registry = os.path.join(local_app_data, "ms-playwright")
            if os.path.isdir(default_registry):
                return default_registry
    return state_browsers

RUNNER_HOST = os.environ.get("YIRENGONGIS_RUNNER_HOST", "127.0.0.1")
RUNNER_PORT = int(os.environ.get("YIRENGONGIS_RUNNER_PORT", "8811"))
SCRIPT_EXT = ".cmd" if os.name == "nt" else ".sh"
RUN_SCRIPT = os.path.join(BASE_DIR, "scripts", f"run_export{SCRIPT_EXT}")
RUN_XHS_SCRIPT = os.path.join(BASE_DIR, "scripts", f"run_xhs_export{SCRIPT_EXT}")
RUN_BILI_SCRIPT = os.path.join(BASE_DIR, "scripts", f"run_bili_export{SCRIPT_EXT}")
RUN_KS_SCRIPT = os.path.join(BASE_DIR, "scripts", f"run_ks_export{SCRIPT_EXT}")
PROFILE_SEED_SCRIPT = os.path.join(BASE_DIR, "scripts", "seed_browser_profile.mjs")
LOCK_FILE = os.path.join(DOWNLOADS_DIR, "runner.lock")
AUTH_LOCK_DIR = os.path.join(DOWNLOADS_DIR, "auth_locks")
LOG_FILE = os.path.join(DOWNLOADS_DIR, "runner.log")
DATA_FILE = os.path.join(DOWNLOADS_DIR, "all_videos.xlsx")
XHS_DATA_FILE = os.path.join(DOWNLOADS_DIR, "xiaohongshu_all_videos.xlsx")
BILI_DATA_FILE = os.path.join(DOWNLOADS_DIR, "bilibili_all_videos.xlsx")
KS_DATA_FILE = os.path.join(DOWNLOADS_DIR, "kuaishou_all_videos.xlsx")
ALL_DATA_FILE = os.path.join(DOWNLOADS_DIR, "all_channels_videos.xlsx")
DOUYIN_PROGRESS_FILE = os.path.join(DOWNLOADS_DIR, "douyin_progress.json")
XHS_PROGRESS_FILE = os.path.join(DOWNLOADS_DIR, "xiaohongshu_progress.json")
BILI_PROGRESS_FILE = os.path.join(DOWNLOADS_DIR, "bilibili_progress.json")
KS_PROGRESS_FILE = os.path.join(DOWNLOADS_DIR, "kuaishou_progress.json")
MONITOR_HTML = os.path.join(BASE_DIR, "frontend", "progress.html")
MERGE_CHANNELS_SCRIPT = os.path.join(BASE_DIR, "scripts", "merge_channels.py")
BUILD_EXCEL_SCRIPT = os.path.join(BASE_DIR, "scripts", "build_excel_export.py")
FEISHU_PREPARE_SCRIPT = os.path.join(BASE_DIR, "scripts", "prepare_feishu_bitable_sync_v2.py")
FEISHU_SYNC_SCRIPT = os.path.join(BASE_DIR, "scripts", "sync_feishu_bitable_openapi.py")
FEISHU_SYNC_TIMEOUT_APP = 180
FEISHU_SYNC_TIMEOUT_CLI = 900
FEISHU_TEST_TIMEOUT_APP = 120
FEISHU_TEST_TIMEOUT_CLI = 180
ENRICHED_ALL_DATA_FILE = os.path.join(DOWNLOADS_DIR, "all_channels_enriched.xlsx")
ENRICHED_PLATFORM_FILES = {
    "douyin": os.path.join(DOWNLOADS_DIR, "douyin_enriched.xlsx"),
    "xiaohongshu": os.path.join(DOWNLOADS_DIR, "xiaohongshu_enriched.xlsx"),
    "bilibili": os.path.join(DOWNLOADS_DIR, "bilibili_enriched.xlsx"),
    "kuaishou": os.path.join(DOWNLOADS_DIR, "kuaishou_enriched.xlsx"),
}
PYTHON_BIN = os.environ.get("PYTHON_BIN", sys.executable or "python3")
DB_PATH = os.path.join(DOWNLOADS_DIR, "analytics.db")
RUN_HISTORY_FILE = os.path.join(DOWNLOADS_DIR, "run_history.json")
FEISHU_SYNC_BASELINE_FILE = os.path.join(DOWNLOADS_DIR, "feishu_sync_baseline_v2.json")
LARK_CLI_DIR = os.path.join(AUTH_DIR, "lark-cli")
LARK_CLI_HOME = os.path.join(AUTH_DIR, "lark-cli-home")
GLOBAL_LARK_CLI_STATE_DIR = os.path.expanduser("~/.lark-cli")
CONFIG_FILE = os.path.join(AUTH_DIR, "customer_config.json")
SECRET_CONFIG_FILE = os.path.join(AUTH_DIR, "customer_secrets.json")
AUTH_STATE_FILE = os.path.join(AUTH_DIR, "auth_state.json")
AUTH_HEALTH_FILE = os.path.join(AUTH_DIR, "auth_health.json")
AUTH_HEALTH_PROBE_DIR = os.path.join(DOWNLOADS_DIR, "auth_health")
AUTH_PROFILE_BACKUP_DIR = os.path.join(AUTH_DIR, "profile_backups")
PLAYWRIGHT_BROWSERS_DIR = _default_playwright_browsers_dir()
SESSION_TOKEN = str(os.environ.pop("YIRENGONGIS_SESSION_TOKEN", "") or "").strip()
SESSION_HEADER_NAME = "X-YRG-Session"
SESSION_QUERY_PARAM = "session"
SUPERVISED_HEALTH_PATH = "/supervised/health"
RUNNER_READY_PREFIX = "YRG_SIDECAR_READY "


_EXCEL_SAVE_DIALOG_SCRIPT = r"""
on run argv
    set defaultName to item 1 of argv
    set selectedFile to choose file name with prompt "选择 Excel 保存位置" default location (path to downloads folder) default name defaultName
    return POSIX path of selectedFile
end run
"""


def _run_excel_save_dialog(default_filename: str) -> dict:
    """Open the native macOS Save As panel without interpolating user text into AppleScript."""
    safe_name = os.path.basename(str(default_filename or "数据汇总.xlsx").strip()) or "数据汇总.xlsx"
    try:
        completed = subprocess.run(
            ["/usr/bin/osascript", "-e", _EXCEL_SAVE_DIALOG_SCRIPT, safe_name],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "cancelled": False,
            "error": "excel_save_dialog_failed",
            "message": "打开 Excel 保存窗口失败，请稍后重试。",
            "detail": str(exc),
        }

    stderr = str(completed.stderr or "").strip()
    if completed.returncode != 0:
        cancelled = "-128" in stderr or "user canceled" in stderr.lower() or "用户取消" in stderr
        if cancelled:
            return {"ok": True, "cancelled": True, "message": "已取消保存"}
        return {
            "ok": False,
            "cancelled": False,
            "error": "excel_save_dialog_failed",
            "message": "打开 Excel 保存窗口失败，请稍后重试。",
            "detail": stderr,
        }

    selected_path = str(completed.stdout or "").strip()
    if not selected_path or "\x00" in selected_path:
        return {
            "ok": False,
            "cancelled": False,
            "error": "excel_save_path_invalid",
            "message": "没有获得有效的 Excel 保存位置，请重新选择。",
        }
    return {"ok": True, "cancelled": False, "path": selected_path}


def _is_valid_xlsx_file(file_path: str) -> bool:
    try:
        if not zipfile.is_zipfile(file_path):
            return False
        with zipfile.ZipFile(file_path, "r") as workbook:
            members = set(workbook.namelist())
            return {"[Content_Types].xml", "xl/workbook.xml"}.issubset(members)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return False


def _save_excel_to_selected_path(source_path: str, selected_path: str) -> str:
    """Copy a verified XLSX to the user-selected destination using atomic replacement."""
    source = os.path.abspath(str(source_path or ""))
    destination = os.path.abspath(os.path.expanduser(str(selected_path or "").strip()))
    if not destination.lower().endswith(".xlsx"):
        destination += ".xlsx"
    if not os.path.isfile(source):
        raise FileNotFoundError("excel_source_missing")
    if not _is_valid_xlsx_file(source):
        raise ValueError("excel_source_invalid")
    parent = os.path.dirname(destination)
    if not parent or not os.path.isdir(parent):
        raise FileNotFoundError("excel_destination_directory_missing")
    if os.path.isdir(destination):
        raise IsADirectoryError("excel_destination_is_directory")

    descriptor, temp_path = tempfile.mkstemp(prefix=".excel-export-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(descriptor, "wb") as target, open(source, "rb") as source_file:
            shutil.copyfileobj(source_file, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_path, destination)
    except Exception:
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass
        raise
    return destination


def _is_tauri_supervised() -> bool:
    return str(os.environ.get("YIRENGONGIS_SUPERVISED_BY_TAURI") or "").strip() == "1"


def _is_loopback_host(host: str) -> bool:
    value = str(host or "").strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return value.lower() == "localhost" or value == "127.0.0.1"

LOCK_STALE_SECONDS = 1800  # 30 minutes (was 8 hours)
RUN_LEASE_TTL_SECONDS = 120
LOG_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
LOG_KEEP_BYTES = 1 * 1024 * 1024  # keep last 1 MB after truncation
SCRIPT_TIMEOUT = 600  # 10 minutes per platform
PLATFORM_INACTIVITY_TIMEOUT = max(
    int(os.environ.get("YIRENGONGIS_PLATFORM_INACTIVITY_TIMEOUT", "720")),
    30,
)
RUN_MUTEX = threading.Lock()
AUTH_STATE_WRITE_LOCK = threading.RLock()
LOG_WRITE_LOCK = threading.Lock()
AUTH_HEALTH_STATE_LOCK = threading.RLock()
AUTH_HEALTH_THREAD_LOCK = threading.Lock()
AUTH_HEALTH_ENABLED = str(os.environ.get("YIRENGONGIS_AUTH_HEALTH_ENABLED", "1") or "1").strip().lower() not in {"0", "false", "no", "off"}


def _positive_env_seconds(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(int(os.environ.get(name, str(default))), minimum)
    except (TypeError, ValueError):
        return max(default, minimum)


AUTH_HEALTH_STARTUP_DELAY_SECONDS = _positive_env_seconds("YIRENGONGIS_AUTH_HEALTH_STARTUP_DELAY", 30, 0)
AUTH_HEALTH_INTERVAL_SECONDS = _positive_env_seconds("YIRENGONGIS_AUTH_HEALTH_INTERVAL", 6 * 60 * 60, 60)
AUTH_HEALTH_FAILURE_RETRY_SECONDS = _positive_env_seconds("YIRENGONGIS_AUTH_HEALTH_FAILURE_RETRY", 30 * 60, 60)
AUTH_HEALTH_BUSY_RETRY_SECONDS = _positive_env_seconds("YIRENGONGIS_AUTH_HEALTH_BUSY_RETRY", 5 * 60, 30)
AUTH_HEALTH_PROBE_TIMEOUT_SECONDS = _positive_env_seconds("YIRENGONGIS_AUTH_HEALTH_PROBE_TIMEOUT", 45, 15)
AUTH_HEALTH_SCAN_WAIT_MS = _positive_env_seconds("YIRENGONGIS_AUTH_HEALTH_SCAN_WAIT_MS", 10000, 5000)
AUTH_HEALTH_TICK_SECONDS = _positive_env_seconds("YIRENGONGIS_AUTH_HEALTH_TICK", 60, 5)
AUTH_HEALTH_STARTUP_SPACING_SECONDS = _positive_env_seconds("YIRENGONGIS_AUTH_HEALTH_STARTUP_SPACING", 5, 0)
COLLECTION_DOUYIN_RETRY_DELAY_SECONDS = _positive_env_seconds(
    "YIRENGONGIS_DOUYIN_RETRY_DELAY",
    20,
    0,
)
_AUTH_HEALTH_STOP_EVENT = threading.Event()
_AUTH_HEALTH_THREAD = None
_AUTH_HEALTH_ACTIVE_PLATFORM = ""
_AUTH_HEALTH_PROCESS_LOCK = threading.Lock()
_AUTH_HEALTH_PROCESS = None
_RUN_LEASE_STORE = RunLeaseStore(
    LOCK_FILE,
    ttl_seconds=RUN_LEASE_TTL_SECONDS,
    pid_alive=_pid_alive,
)
try:
    COLLECTION_MAX_WORKERS = max(
        1,
        min(int(os.environ.get("YIRENGONGIS_COLLECTION_MAX_WORKERS", "2")), 2),
    )
except ValueError:
    COLLECTION_MAX_WORKERS = 2
VALID_BROWSER_CHANNELS = {"chrome", "msedge", "chromium"}
FORCED_BROWSER_CHANNEL = "chrome"
# Change detection must cover every business table. Detail-only baselines would
# miss regrouping or chart/increment changes when raw platform rows stay the same.
FEISHU_BUSINESS_TABLE_NAMES = ("平台明细V2", "作品总表V2", "作品图表表", "作品增量表")
FEISHU_BASELINE_RUNTIME_ONLY_FIELDS = {"日期", "最近更新时间"}
FEISHU_DETAIL_RUNTIME_ONLY_FIELDS = FEISHU_BASELINE_RUNTIME_ONLY_FIELDS

# ============================================================
# Lark CLI (飞书 CLI) integration state
# ============================================================
_LARK_CLI_STATE: dict = {
    "phase": "idle",  # idle | init_running | init_done | auth_waiting | auth_done | creating_base | ready | error
    "error": "",
    "message": "",
    "app_id": "",
    "app_secret": "",
    "verification_url": "",
    "user_code": "",
    "device_code": "",
    "app_token": "",
    "auth_mode": "",
    "trigger_reason": "",
    "effective": {},
}
_LARK_CLI_LOCK = threading.Lock()
_LARK_CLI_EFFECTIVE_CACHE: dict = {"key": None, "ts": 0.0, "value": {}}
_LARK_CLI_STATUS_CACHE_LOCK = threading.Lock()
_LARK_CLI_STATUS_CACHE: dict = {}
LARK_CLI_STATUS_CACHE_TTL_SECONDS = 3.0
LARK_CLI_NPX_PACKAGE = os.getenv("YIRENGONGIS_LARK_CLI_PACKAGE", "@larksuite/cli@1.0.43")
FEISHU_USER_BASE_SCOPES = [
    "base:app:create",
    "base:table:create",
    "base:table:read",
    "base:field:create",
    "base:field:read",
    "base:field:update",
    "base:field:delete",
    "base:record:create",
    "base:record:delete",
    "base:record:update",
    "base:record:read",
    "base:view:write_only",
]

# ============================================================
# 许可证管理
# ============================================================
LICENSE_MGR = LicenseManager(base_dir=BASE_DIR)
COMMUNITY_EDITION_ENABLED = os.path.isfile(os.path.join(BASE_DIR, "COMMUNITY_EDITION"))
LICENSE_BYPASS_ENABLED = COMMUNITY_EDITION_ENABLED or str(os.environ.get("YIRENGONGIS_LICENSE_BYPASS", "")).strip().lower() in {"1", "true", "yes", "on"}

_SUPERVISED_LICENSE_STATE = threading.Condition()
_SUPERVISED_LICENSE_OPERATION_LOCK = threading.Lock()
_SUPERVISED_LICENSE_PHASE = "idle"
_SUPERVISED_LICENSE_RESULT = None
_SUPERVISED_LICENSE_GENERATION = 0
_SUPERVISED_LICENSE_FINISHED_AT = 0.0
_SUPERVISED_LICENSE_RESULT_TTL_SECONDS = float(LICENSE_VERIFY_CACHE_TTL_SECONDS)


def _normalize_license_result(ok, info):
    normalized = dict(info or {})
    normalized.setdefault("access_mode", "license" if ok else "none")
    return bool(ok), normalized


def _license_pending_result():
    return False, {
        "error": "license_check_pending",
        "message": "正在后台验证许可证，请稍候。",
        "access_mode": "checking",
        "checking": True,
    }


def _begin_supervised_license_operation(*, preserve_result=False):
    global _SUPERVISED_LICENSE_GENERATION
    global _SUPERVISED_LICENSE_PHASE
    global _SUPERVISED_LICENSE_RESULT
    with _SUPERVISED_LICENSE_STATE:
        if _SUPERVISED_LICENSE_PHASE in {"running", "refreshing"}:
            return None
        _SUPERVISED_LICENSE_GENERATION += 1
        if preserve_result and _SUPERVISED_LICENSE_RESULT is not None:
            _SUPERVISED_LICENSE_PHASE = "refreshing"
        else:
            _SUPERVISED_LICENSE_PHASE = "running"
            _SUPERVISED_LICENSE_RESULT = None
        _SUPERVISED_LICENSE_STATE.notify_all()
        return _SUPERVISED_LICENSE_GENERATION


def _finish_supervised_license_operation(generation, ok, info):
    global _SUPERVISED_LICENSE_FINISHED_AT
    global _SUPERVISED_LICENSE_PHASE
    global _SUPERVISED_LICENSE_RESULT
    normalized = _normalize_license_result(ok, info)
    with _SUPERVISED_LICENSE_STATE:
        if generation != _SUPERVISED_LICENSE_GENERATION:
            return
        _SUPERVISED_LICENSE_RESULT = normalized
        _SUPERVISED_LICENSE_PHASE = "done"
        _SUPERVISED_LICENSE_FINISHED_AT = time.monotonic()
        _SUPERVISED_LICENSE_STATE.notify_all()


def _supervised_license_snapshot():
    with _SUPERVISED_LICENSE_STATE:
        phase = _SUPERVISED_LICENSE_PHASE
        result = _SUPERVISED_LICENSE_RESULT
    if phase != "done" or result is None:
        ok, info = _license_pending_result()
        if phase == "refreshing" and result is not None:
            last_ok, last_info = result
            info["last_known_valid"] = bool(last_ok)
            info["last_known_access_mode"] = (last_info or {}).get("access_mode", "none")
        return phase, ok, info
    ok, info = result
    return phase, bool(ok), dict(info or {})


def _reset_supervised_license_state_for_test():
    global _SUPERVISED_LICENSE_FINISHED_AT
    global _SUPERVISED_LICENSE_GENERATION
    global _SUPERVISED_LICENSE_PHASE
    global _SUPERVISED_LICENSE_RESULT
    with _SUPERVISED_LICENSE_STATE:
        _SUPERVISED_LICENSE_GENERATION += 1
        _SUPERVISED_LICENSE_PHASE = "idle"
        _SUPERVISED_LICENSE_RESULT = None
        _SUPERVISED_LICENSE_FINISHED_AT = 0.0
        _SUPERVISED_LICENSE_STATE.notify_all()


def _wait_for_supervised_license_terminal_for_test(timeout_seconds):
    deadline = time.monotonic() + max(float(timeout_seconds), 0.0)
    with _SUPERVISED_LICENSE_STATE:
        while _SUPERVISED_LICENSE_PHASE != "done":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("supervised_license_terminal_timeout")
            _SUPERVISED_LICENSE_STATE.wait(remaining)
    return _supervised_license_snapshot()


def _auto_activate_seeded_license():
    """Auto-activate the pre-seeded license for this machine on first launch.

    Strict rules:
    - Only activates if the stored license key matches the package manifest's
      license_key_sha256 (same package, same key).
    - If the stored key does NOT match the manifest (stale .auth from a
      different package), the old license.json and session.json are cleaned
      so the user is forced back to the activation flow.
    """
    from package_identity import package_license_allowed

    if not LICENSE_MGR.is_activated():
        return

    key = LICENSE_MGR.get_license_key()
    if not key:
        return

    # ── Guard: license must belong to THIS package ──────────────────────
    allowed, _err = package_license_allowed(BASE_DIR, key)
    if not allowed:
        # Stale .auth from a different package → clean up, force re-activation
        print(f"[license] stored key does not match this package — clearing stale auth")
        auth_dir = AUTH_DIR
        for stale_file in ("license.json", "session.json"):
            path = os.path.join(auth_dir, stale_file)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        LICENSE_MGR.reload()
        return

    ok, info = LICENSE_MGR.verify()
    if ok:
        return  # already valid on this machine

    # Same package, same key, but wrong machine → re-activate
    try:
        success, msg = LICENSE_MGR.activate(key)
        if success:
            print(f"[license] auto-activated for this machine")
        else:
            print(f"[license] auto-activation failed: {msg}")
    except Exception as exc:
        print(f"[license] auto-activation error: {exc}")


def _run_supervised_seeded_license_operation(manager):
    from package_identity import package_license_allowed

    if LICENSE_BYPASS_ENABLED:
        return True, {
            "status": "community",
            "access_mode": "community",
            "message": "社区版本地访问已启用",
        }

    if not manager.is_activated():
        return _normalize_license_result(*manager.verify_access())

    key = str(manager.get_license_key() or "").strip()
    if not key:
        return _normalize_license_result(*manager.verify_access())

    allowed, _error = package_license_allowed(BASE_DIR, key)
    if not allowed:
        print("[license] stored key does not match this package — clearing stale auth")
        for stale_file in ("license.json", "session.json"):
            path = os.path.join(AUTH_DIR, stale_file)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        manager.reload()
        return _normalize_license_result(*manager.verify_access())

    ok, info = _normalize_license_result(*manager.verify_access())
    if ok:
        return ok, info

    success, message = manager.activate(key)
    if not success:
        return False, {
            "error": "license_auto_activation_failed",
            "message": str(message or "许可证自动激活失败"),
            "access_mode": "none",
        }
    print("[license] auto-activated for this machine")
    return _normalize_license_result(*manager.verify_access())


def _initialize_seeded_license_activation() -> None:
    if LICENSE_BYPASS_ENABLED:
        return
    if not _is_tauri_supervised():
        _auto_activate_seeded_license()


def _start_supervised_license_background() -> None:
    if not _is_tauri_supervised():
        return
    generation = _begin_supervised_license_operation()
    if generation is None:
        return
    manager = LICENSE_MGR

    def _run() -> None:
        ok, info = False, {
            "error": "license_check_failed",
            "message": "许可证验证失败",
            "access_mode": "none",
        }
        try:
            with _SUPERVISED_LICENSE_OPERATION_LOCK:
                ok, info = _run_supervised_seeded_license_operation(manager)
        except BaseException as exc:
            info["message"] = str(exc or info["message"])
        finally:
            _finish_supervised_license_operation(generation, ok, info)

    worker = threading.Thread(
        target=_run,
        daemon=True,
        name="supervised_license_activation",
    )
    try:
        worker.start()
    except Exception as exc:
        _finish_supervised_license_operation(generation, False, {
            "error": "license_check_failed",
            "message": str(exc or "许可证验证失败"),
            "access_mode": "none",
        })


def _supervised_license_result_expired():
    with _SUPERVISED_LICENSE_STATE:
        return (
            _SUPERVISED_LICENSE_PHASE == "done"
            and _SUPERVISED_LICENSE_RESULT is not None
            and time.monotonic() - _SUPERVISED_LICENSE_FINISHED_AT
            >= max(_SUPERVISED_LICENSE_RESULT_TTL_SECONDS, 0.0)
        )


def _start_supervised_license_refresh() -> None:
    if not _is_tauri_supervised():
        return
    generation = _begin_supervised_license_operation(preserve_result=True)
    if generation is None:
        return
    manager = LICENSE_MGR

    def _run() -> None:
        ok, info = False, {
            "error": "license_check_failed",
            "message": "许可证验证失败",
            "access_mode": "none",
        }
        try:
            with _SUPERVISED_LICENSE_OPERATION_LOCK:
                ok, info = _normalize_license_result(*manager.verify_access())
        except BaseException as exc:
            info["message"] = str(exc or info["message"])
        finally:
            _finish_supervised_license_operation(generation, ok, info)

    worker = threading.Thread(
        target=_run,
        daemon=True,
        name="supervised_license_refresh",
    )
    try:
        worker.start()
    except Exception as exc:
        _finish_supervised_license_operation(generation, False, {
            "error": "license_check_failed",
            "message": str(exc or "许可证验证失败"),
            "access_mode": "none",
        })


def _license_access_for_request():
    if _is_tauri_supervised():
        if _supervised_license_result_expired():
            _start_supervised_license_refresh()
        return _supervised_license_snapshot()
    ok, info = _normalize_license_result(*LICENSE_MGR.verify_access())
    return "done", ok, info


def _activate_license_key_for_request(key):
    if not _is_tauri_supervised():
        ok, message = LICENSE_MGR.activate(key)
        return "done", ok, message

    generation = _begin_supervised_license_operation()
    if generation is None:
        return "pending", False, "license_check_pending"

    manager = LICENSE_MGR
    with _SUPERVISED_LICENSE_OPERATION_LOCK:
        try:
            activated, message = manager.activate(key)
            if activated:
                access_ok, info = _normalize_license_result(*manager.verify_access())
            else:
                access_ok, info = False, {
                    "error": "license_activation_failed",
                    "message": str(message or "许可证激活失败"),
                    "access_mode": "none",
                }
        except Exception as exc:
            activated = False
            message = str(exc or "许可证激活失败")
            access_ok, info = False, {
                "error": "license_activation_failed",
                "message": message,
                "access_mode": "none",
            }
    _finish_supervised_license_operation(generation, access_ok, info)
    return "done", bool(activated), message


_initialize_seeded_license_activation()


def _browser_channel_paths(channel: str) -> list[str]:
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if os.name == "nt":
        channel_paths = {
            "chrome": [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.join(local_app_data, "Google", "Chrome", "Application", "chrome.exe"),
            ],
            "msedge": [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                os.path.join(local_app_data, "Microsoft", "Edge", "Application", "msedge.exe"),
            ],
        }
    else:
        channel_paths = {
            "chrome": [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/usr/bin/google-chrome-stable",
                "/usr/bin/google-chrome",
            ],
            "msedge": [
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/usr/bin/microsoft-edge",
            ],
        }
    return channel_paths.get(channel, [])


def _browser_channel_available(channel: str) -> bool:
    for p in _browser_channel_paths(channel):
        if p and os.path.exists(p):
            return True
    return False


def _playwright_browser_binary_names() -> tuple[str, ...]:
    if os.name == "nt":
        return ("chrome.exe", "Chromium.exe")
    if sys.platform == "darwin":
        return ("Google Chrome for Testing", "Chromium")
    return ("chrome", "chromium")


def _find_playwright_browser_executable(browser_root: str) -> str:
    if not browser_root or not os.path.isdir(browser_root):
        return ""
    binary_names = set(_playwright_browser_binary_names())
    try:
        candidates = sorted(
            [
                os.path.join(browser_root, name)
                for name in os.listdir(browser_root)
                if name.startswith("chromium-")
            ],
            reverse=True,
        )
    except OSError:
        return ""
    for candidate_root in candidates:
        for current_root, dirnames, filenames in os.walk(candidate_root):
            dirnames.sort(reverse=True)
            for filename in sorted(filenames, reverse=True):
                if filename in binary_names:
                    candidate = os.path.join(current_root, filename)
                    if os.path.isfile(candidate):
                        return candidate
    return ""


def _resolve_browser_executable(channel: str) -> str:
    normalized = _sanitize_browser_channel(channel) or FORCED_BROWSER_CHANNEL
    if normalized == "chrome":
        bundled_browser = _find_playwright_browser_executable(PLAYWRIGHT_BROWSERS_DIR)
        if sys.platform == "darwin" and bundled_browser:
            return bundled_browser
        system_browser = next(
            (path for path in _browser_channel_paths("chrome") if path and os.path.exists(path)),
            "",
        )
        if system_browser:
            return system_browser
        return bundled_browser
    if normalized == "chromium":
        return _find_playwright_browser_executable(PLAYWRIGHT_BROWSERS_DIR)
    return next(
        (path for path in _browser_channel_paths(normalized) if path and os.path.exists(path)),
        "",
    )


def _detect_browser_channel() -> str:
    return FORCED_BROWSER_CHANNEL


def _detect_default_browser_channel() -> str:
    return _detect_browser_channel()


def _sanitize_browser_channel(value: str) -> str:
    channel = str(value or "").strip().lower()
    if not channel:
        return ""
    return FORCED_BROWSER_CHANNEL if channel in VALID_BROWSER_CHANNELS else ""


def _sanitize_workspace_name(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if all(ch in {"?", "？"} or ch.isspace() for ch in text):
        return DEFAULT_CONFIG["workspace_name"]
    return text

DEFAULT_CONFIG = {
    "customer_name": "",
    "workspace_name": "本地数据工作台",
    "min_publish_date": "2026-01-01",
    "browser_channel": _detect_default_browser_channel(),
    "enabled_platforms": [],
    "onboarding_completed": False,
    "feishu_enabled": False,
    "feishu_auto_sync": False,
    "feishu_cli_use_global_home": False,
    "feishu_initial_seed_pending": False,
    "feishu_bitable_owner_identity": "",
    "feishu_app_token": "",
    "feishu_app_id": "",
    "feishu_app_secret": "",
}

LEGACY_DEFAULT_MIN_PUBLISH_DATE = "2026-03-05"
MIN_PUBLISH_DATE_CUSTOMIZED_KEY = "min_publish_date_customized"

STRING_CONFIG_FIELDS = {
    "customer_name",
    "workspace_name",
    "min_publish_date",
    "browser_channel",
    "feishu_bitable_owner_identity",
}

SECRET_CONFIG_FIELDS = {
    "feishu_app_token",
    "feishu_app_id",
    "feishu_app_secret",
}

BOOL_CONFIG_FIELDS = {
    "onboarding_completed",
    "feishu_enabled",
    "feishu_auto_sync",
    "feishu_cli_mode",
    "feishu_cli_use_global_home",
    "feishu_initial_seed_pending",
}

LIST_CONFIG_FIELDS = {
    "enabled_platforms",
}

def _enabled_platform_scope(config: dict | None) -> list[str]:
    if not isinstance(config, dict):
        return []
    return _normalize_platform_ids(config.get("enabled_platforms") or [])


def resolve_requested_targets(query: dict | None, enabled_platforms: list[str]) -> list[str]:
    payload = query if isinstance(query, dict) else {}
    if "platforms" not in payload:
        requested = _normalize_platform_ids(enabled_platforms or [])
    else:
        raw_values = payload.get("platforms")
        values = raw_values if isinstance(raw_values, (list, tuple, set)) else [raw_values]
        tokens = []
        for value in values:
            tokens.extend(
                token.strip()
                for token in str(value or "").split(",")
                if token.strip()
            )
        invalid = [token for token in tokens if token not in VALID_PLATFORM_IDS]
        if invalid:
            raise ValueError(f"invalid_platform:{invalid[0]}")
        requested_set = set(tokens)
        requested = [platform_id for platform_id in VALID_PLATFORM_IDS if platform_id in requested_set]
    if not requested:
        raise ValueError("no_platform_selected")
    return requested


def _resolved_platform_scope(query: dict | None = None, config: dict | None = None) -> list[str]:
    payload = query if isinstance(query, dict) else {}
    enabled = _enabled_platform_scope(config)
    if "platforms" in payload:
        return resolve_requested_targets(payload, enabled)
    return enabled


def _feishu_skip_result(message: str, *, platforms=None) -> dict:
    prepare = {}
    normalized_platforms = _normalize_platform_ids(platforms or [])
    if normalized_platforms:
        prepare["platforms"] = normalized_platforms
    return {
        "ok": True,
        "attempted": False,
        "message": str(message or "").strip(),
        "prepare": prepare,
        "sync": {},
    }

PLATFORM_PROGRESS_FILES = {
    "douyin": DOUYIN_PROGRESS_FILE,
    "xiaohongshu": XHS_PROGRESS_FILE,
    "bilibili": BILI_PROGRESS_FILE,
    "kuaishou": KS_PROGRESS_FILE,
}

PLATFORM_EXPORT_FILES = {
    "douyin": DATA_FILE,
    "xiaohongshu": XHS_DATA_FILE,
    "bilibili": BILI_DATA_FILE,
    "kuaishou": KS_DATA_FILE,
}

EMPTY_PLATFORM_EXPORT_COLUMNS = [
    "作品ID",
    "标题",
    "发布日期",
    "曝光量",
    "播放量",
    "阅读量",
    "点赞量",
    "收藏量",
    "评论量",
    "分享量",
    "涨粉量",
    "投币量",
    "弹幕量",
    "时长",
    "链接",
    "内容类型",
]


def _platform_artifact_contract(
    platform_id: str,
    workspace: RunWorkspace,
) -> tuple[dict[str, str], dict[pathlib.Path, pathlib.Path]]:
    rows_targets = {
        "xiaohongshu": pathlib.Path(DOWNLOADS_DIR, "xiaohongshu_rows.json"),
        "bilibili": pathlib.Path(DOWNLOADS_DIR, "bilibili_rows.json"),
        "kuaishou": pathlib.Path(DOWNLOADS_DIR, "kuaishou_rows.json"),
    }
    if platform_id == "douyin":
        master = workspace.stage_path("all_videos.xlsx")
        summary = workspace.stage_path("summary.csv")
        state = workspace.stage_path("processed_ids.json")
        return (
            {
                "DOWNLOAD_DIR": str(workspace.root),
                "SUMMARY_PATH": str(summary),
                "MASTER_PATH": str(master),
                "STATE_PATH": str(state),
            },
            {
                master: pathlib.Path(DATA_FILE),
                summary: pathlib.Path(DOWNLOADS_DIR, "summary.csv"),
                state: pathlib.Path(DOWNLOADS_DIR, "processed_ids.json"),
            },
        )
    if platform_id == "xiaohongshu":
        output = workspace.stage_path("xiaohongshu_all_videos.xlsx")
        rows = workspace.stage_path("xiaohongshu_rows.json")
        return (
            {
                "XHS_OUTPUT_PATH": str(output),
                "XHS_TEMP_ROWS_PATH": str(rows),
                "XHS_DETAIL_EXPORT_DIR": str(workspace.stage_path("xiaohongshu_detail_exports")),
            },
            {output: pathlib.Path(XHS_DATA_FILE), rows: rows_targets[platform_id]},
        )
    if platform_id == "bilibili":
        output = workspace.stage_path("bilibili_all_videos.xlsx")
        rows = workspace.stage_path("bilibili_rows.json")
        return (
            {
                "BILI_OUTPUT_PATH": str(output),
                "BILI_TEMP_ROWS_PATH": str(rows),
                "BILI_OFFICIAL_DOWNLOAD_DIR": str(workspace.stage_path("bilibili_official_exports")),
            },
            {output: pathlib.Path(BILI_DATA_FILE), rows: rows_targets[platform_id]},
        )
    if platform_id == "kuaishou":
        output = workspace.stage_path("kuaishou_all_videos.xlsx")
        rows = workspace.stage_path("kuaishou_rows.json")
        return (
            {
                "KS_OUTPUT_PATH": str(output),
                "KS_TEMP_ROWS_PATH": str(rows),
                "KS_DETAIL_EXPORT_DIR": str(workspace.stage_path("kuaishou_detail_exports")),
            },
            {output: pathlib.Path(KS_DATA_FILE), rows: rows_targets[platform_id]},
        )
    raise ValueError(f"invalid_platform:{platform_id}")


def _seed_douyin_workspace(workspace: RunWorkspace, downloads_dir: str | os.PathLike) -> None:
    source_root = pathlib.Path(downloads_dir)
    for name in ("processed_ids.json", "summary.csv"):
        source = source_root / name
        target = workspace.stage_path(name)
        if source.is_file() and not target.exists():
            shutil.copy2(source, target)
    for source in source_root.glob("merged-*.xls*"):
        target = workspace.stage_path(source.name)
        if source.is_file() and not target.exists():
            shutil.copy2(source, target)


def _stage_completed_empty_artifacts(
    platform_id: str,
    mapping: dict[pathlib.Path, pathlib.Path],
) -> None:
    for source in mapping:
        source.parent.mkdir(parents=True, exist_ok=True)
        suffix = source.suffix.lower()
        if suffix in {".xlsx", ".xls"}:
            pd.DataFrame(columns=EMPTY_PLATFORM_EXPORT_COLUMNS).to_excel(source, index=False)
        elif source.name == "processed_ids.json":
            if not source.exists():
                source.write_text(
                    json.dumps({"processed_ids": []}, ensure_ascii=False),
                    encoding="utf-8",
                )
        elif suffix == ".json":
            source.write_text("[]", encoding="utf-8")
        elif source.name == "summary.csv" and not source.exists():
            source.write_text(
                "work_id,title,publish_date,merged_file,raw_files\n",
                encoding="utf-8",
            )

    _validate_basic_platform_artifacts(platform_id, mapping)


def _validate_basic_platform_artifacts(
    platform_id: str,
    mapping: dict[pathlib.Path, pathlib.Path],
) -> None:
    for source in mapping:
        if not source.is_file():
            raise ArtifactValidationError(f"missing_artifact:{platform_id}:{source.name}")
        if source.stat().st_size <= 0:
            raise ArtifactValidationError(f"empty_artifact:{platform_id}:{source.name}")
        if source.suffix.lower() == ".json":
            try:
                payload = json.loads(source.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ArtifactValidationError(
                    f"invalid_json_artifact:{platform_id}:{source.name}"
                ) from exc
            if not isinstance(payload, (dict, list)):
                raise ArtifactValidationError(
                    f"invalid_json_shape:{platform_id}:{source.name}"
                )


def _promote_platform_artifacts(
    platform_id: str,
    workspace: RunWorkspace,
    mapping: dict[pathlib.Path, pathlib.Path],
) -> bool:
    resolved_mapping = dict(mapping)
    if platform_id == "douyin":
        for staged in workspace.root.glob("merged-*.xls*"):
            if staged.is_file():
                resolved_mapping[staged] = pathlib.Path(DOWNLOADS_DIR, staged.name)
    workspace.promote(
        resolved_mapping,
        validator=lambda: _validate_basic_platform_artifacts(platform_id, resolved_mapping),
    )
    return True


def _platform_completed_without_output(progress_path: str) -> bool:
    progress = read_json_file(progress_path, {})
    return bool(
        str(progress.get("status") or "").strip().lower() == "completed"
        and int(progress.get("totalWorks") or 0) == 0
        and int(progress.get("successWorks") or 0) == 0
        and int(progress.get("failedWorks") or 0) == 0
    )


def _is_promotable_douyin_partial_failure(progress_path: str) -> bool:
    progress = read_json_file(progress_path, {})
    try:
        success_works = int(progress.get("successWorks") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        str(progress.get("status") or "").strip().lower() == "failed"
        and str(progress.get("error") or "").strip().lower() == "partial_failure"
        and success_works > 0
    )


AUTH_SINGLE_PLATFORM_MAP = {
    "douyin": (RUN_SCRIPT, DOUYIN_PROGRESS_FILE),
    "xiaohongshu": (RUN_XHS_SCRIPT, XHS_PROGRESS_FILE),
    "bilibili": (RUN_BILI_SCRIPT, BILI_PROGRESS_FILE),
    "kuaishou": (RUN_KS_SCRIPT, KS_PROGRESS_FILE),
}

RUN_ALL_PLATFORM_PROGRESS_STEPS = [
    ("douyin", DOUYIN_PROGRESS_FILE),
    ("xiaohongshu", XHS_PROGRESS_FILE),
    ("bilibili", BILI_PROGRESS_FILE),
    ("kuaishou", KS_PROGRESS_FILE),
]

LIVE_AUTH_PRECHECK_PLATFORMS = frozenset()

RUN_ROUTE_PLATFORM_MAP = {
    "/run": "douyin",
    "/run_xhs": "xiaohongshu",
    "/run_bili": "bilibili",
    "/run_ks": "kuaishou",
}

def ensure_runtime_dirs() -> None:
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    os.makedirs(AUTH_LOCK_DIR, exist_ok=True)
    os.makedirs(AUTH_DIR, exist_ok=True)
    os.makedirs(os.path.join(AUTH_DIR, "profiles"), exist_ok=True)
    os.makedirs(AUTH_PROFILE_BACKUP_DIR, exist_ok=True)
    os.makedirs(AUTH_HEALTH_PROBE_DIR, exist_ok=True)
    os.makedirs(PLAYWRIGHT_BROWSERS_DIR, exist_ok=True)


def _to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_date_text(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    ts = pd.to_datetime(text, errors="coerce")
    if pd.isna(ts):
        return ""
    return ts.strftime("%Y-%m-%d")


def _apply_config_migrations(config: dict, *, raw_payload=None) -> dict:
    migrated = dict(config or {})
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    min_date_customized = _to_bool(
        payload.get(MIN_PUBLISH_DATE_CUSTOMIZED_KEY)
        if MIN_PUBLISH_DATE_CUSTOMIZED_KEY in payload
        else migrated.get(MIN_PUBLISH_DATE_CUSTOMIZED_KEY)
    )
    normalized_min_date = _normalize_date_text(migrated.get("min_publish_date"))
    if not normalized_min_date:
        migrated["min_publish_date"] = DEFAULT_CONFIG["min_publish_date"]
    elif not min_date_customized and normalized_min_date == LEGACY_DEFAULT_MIN_PUBLISH_DATE:
        migrated["min_publish_date"] = DEFAULT_CONFIG["min_publish_date"]
    else:
        migrated["min_publish_date"] = normalized_min_date

    if min_date_customized:
        migrated[MIN_PUBLISH_DATE_CUSTOMIZED_KEY] = True
    else:
        migrated.pop(MIN_PUBLISH_DATE_CUSTOMIZED_KEY, None)
    return migrated


def _sanitize_config(payload, *, include_defaults: bool = True) -> dict:
    config = _public_config_defaults() if include_defaults else {}
    if not isinstance(payload, dict):
        return config

    for key in STRING_CONFIG_FIELDS:
        if key in payload:
            config[key] = str(payload.get(key) or "").strip()
    if "workspace_name" in config:
        config["workspace_name"] = _sanitize_workspace_name(config.get("workspace_name"))
    if "browser_channel" in config:
        config["browser_channel"] = _sanitize_browser_channel(config.get("browser_channel"))

    for key in BOOL_CONFIG_FIELDS:
        if key in payload:
            config[key] = _to_bool(payload.get(key))

    for key in LIST_CONFIG_FIELDS:
        if key in payload:
            raw = payload.get(key) or []
            if isinstance(raw, str):
                raw = [item.strip() for item in raw.split(",") if item.strip()]
            if not isinstance(raw, list):
                raw = []
            dedup = []
            seen = set()
            for item in raw:
                value = str(item or "").strip()
                if value in VALID_PLATFORM_IDS and value not in seen:
                    dedup.append(value)
                    seen.add(value)
            config[key] = dedup

    if "browser_channel" in config and not config["browser_channel"]:
        config["browser_channel"] = DEFAULT_CONFIG["browser_channel"]
    if "min_publish_date" in config and not config["min_publish_date"]:
        config["min_publish_date"] = DEFAULT_CONFIG["min_publish_date"]
    if MIN_PUBLISH_DATE_CUSTOMIZED_KEY in payload:
        config[MIN_PUBLISH_DATE_CUSTOMIZED_KEY] = _to_bool(payload.get(MIN_PUBLISH_DATE_CUSTOMIZED_KEY))
    return config


def merge_config_patch(existing: dict, patch: dict) -> dict:
    normalized_patch = dict(patch or {})
    merged = _public_config_only(existing or _public_config_defaults())
    merged.update(_sanitize_config(normalized_patch, include_defaults=False))
    return _apply_config_migrations(merged, raw_payload=normalized_patch)


def _sanitize_secret_config(payload) -> dict:
    secrets = {}
    if not isinstance(payload, dict):
        return secrets
    for key in SECRET_CONFIG_FIELDS:
        if key in payload:
            secrets[key] = str(payload.get(key) or "").strip()
    return secrets


def _public_config_defaults() -> dict:
    return {key: value for key, value in DEFAULT_CONFIG.items() if key not in SECRET_CONFIG_FIELDS}


def _public_config_only(config: dict) -> dict:
    return {key: value for key, value in dict(config or {}).items() if key not in SECRET_CONFIG_FIELDS}


def load_saved_secrets() -> dict:
    return _sanitize_secret_config(_load_json_dict(SECRET_CONFIG_FILE))


def _write_secret_config(payload: dict) -> None:
    _write_json_file_atomically(SECRET_CONFIG_FILE, payload)
    if os.name != "nt":
        try:
            os.chmod(SECRET_CONFIG_FILE, 0o600)
        except OSError:
            pass


def _merge_public_and_secret_config(public_config: dict, secrets: dict | None = None) -> dict:
    merged = dict(DEFAULT_CONFIG)
    merged.update(public_config or {})
    merged.update(secrets or {})
    return merged


def _mask_value(value: str, *, prefix: int = 6, suffix: int = 0) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if suffix <= 0:
        kept = min(prefix, len(text))
        return f"{text[:kept]}…"
    if len(text) <= prefix + suffix:
        return text
    return f"{text[:prefix]}…{text[-suffix:]}"


def public_config_payload(config: dict) -> dict:
    public = _public_config_only(config)
    public["feishu_credentials_saved"] = bool(
        config.get("feishu_app_token") or config.get("feishu_app_id") or config.get("feishu_app_secret")
    )
    public["feishu_app_id_masked"] = _mask_value(config.get("feishu_app_id", ""), prefix=8)
    public["feishu_app_token_masked"] = _mask_value(config.get("feishu_app_token", ""), prefix=6)
    return public


def load_saved_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return _merge_public_and_secret_config(_public_config_defaults(), load_saved_secrets())

    payload = _load_json_dict(CONFIG_FILE)
    if not payload:
        return _merge_public_and_secret_config(_public_config_defaults(), load_saved_secrets())

    migrated_secret_fields = any(key in payload for key in SECRET_CONFIG_FIELDS)
    if migrated_secret_fields:
        migrated_secrets = {key: value for key, value in _sanitize_secret_config(payload).items() if value}
        existing_secrets = load_saved_secrets()
        existing_secrets.update(migrated_secrets)
        _write_secret_config(existing_secrets)
        for key in SECRET_CONFIG_FIELDS:
            payload.pop(key, None)

    public_defaults = _public_config_defaults()
    public_merged = dict(public_defaults)
    public_merged.update(_sanitize_config(payload))
    migrated = _apply_config_migrations(public_merged, raw_payload=payload)
    if isinstance(payload, dict) and (payload != migrated or migrated_secret_fields):
        try:
            _write_json_file_atomically(CONFIG_FILE, migrated)
        except Exception:
            pass
    return _merge_public_and_secret_config(migrated, load_saved_secrets())


def save_config(payload: dict) -> dict:
    current = load_saved_config()
    normalized_payload = dict(payload or {})
    if "min_publish_date" in normalized_payload and MIN_PUBLISH_DATE_CUSTOMIZED_KEY not in normalized_payload:
        normalized_payload[MIN_PUBLISH_DATE_CUSTOMIZED_KEY] = True
    public_merged = merge_config_patch(current, normalized_payload)
    _write_json_file_atomically(CONFIG_FILE, public_merged)

    incoming_secrets = _sanitize_secret_config(normalized_payload)
    if incoming_secrets:
        saved_secrets = load_saved_secrets()
        saved_secrets.update(incoming_secrets)
        _write_secret_config(saved_secrets)

    return _merge_public_and_secret_config(public_merged, load_saved_secrets())


def _sanitize_auth_status(value: str) -> str:
    status = str(value or "").strip().lower()
    return status if status in {"authorized", "unauthorized", "expired", "needs_auth"} else ""


def _auth_profiles_root() -> str:
    return os.path.realpath(os.path.join(AUTH_DIR, "profiles"))


def _platform_profile_dirs(platform_id: str) -> list[str]:
    prefix = PLATFORM_PROFILE_PREFIX.get(platform_id)
    if not prefix:
        return []
    root = _auth_profiles_root()
    if not os.path.isdir(root):
        return []
    matches = []
    for name in os.listdir(root):
        if not name.startswith(prefix + "-"):
            continue
        full_path = os.path.join(root, name)
        if os.path.isdir(full_path):
            matches.append(full_path)
    return matches


def _platform_profile_dir(platform_id: str, browser_channel: str) -> str:
    prefix = PLATFORM_PROFILE_PREFIX.get(platform_id)
    channel = _sanitize_browser_channel(browser_channel) or DEFAULT_CONFIG["browser_channel"]
    return os.path.join(_auth_profiles_root(), f"{prefix}-{channel}")


def _configured_browser_channel() -> str:
    config = load_saved_config()
    return _sanitize_browser_channel(config.get("browser_channel")) or DEFAULT_CONFIG["browser_channel"]


def _resolve_user_data_dir(platform_id: str, browser_channel: str) -> str:
    return _platform_profile_dir(platform_id, browser_channel)


def _prepare_profile_for_launch(platform_id: str, browser_channel: str) -> list[int]:
    profile_dir = _resolve_user_data_dir(platform_id, browser_channel)
    killed = _terminate_profile_browsers(profile_dir)
    _clear_profile_lock_files(profile_dir)
    return killed


def _platform_has_profile(platform_id: str, browser_channel: str | None = None) -> bool:
    channel = _sanitize_browser_channel(browser_channel) or _configured_browser_channel()
    return os.path.isdir(_platform_profile_dir(platform_id, channel))


def _auth_profile_launch_failure(stdout: str = "", stderr: str = "") -> bool:
    text = "\n".join(part for part in (stdout, stderr) if part).lower()
    if "launchpersistentcontext" not in text:
        return False
    markers = (
        "target page, context or browser has been closed",
        "browser has been closed",
        "<process did exit",
    )
    return any(marker in text for marker in markers)


def _backup_platform_profile(platform_id: str, browser_channel: str, *, reason: str = "startup_failed") -> str:
    source_dir = _platform_profile_dir(platform_id, browser_channel)
    if not os.path.isdir(source_dir):
        return ""

    ensure_runtime_dirs()
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    prefix = PLATFORM_PROFILE_PREFIX.get(platform_id) or platform_id
    target_dir = os.path.join(AUTH_PROFILE_BACKUP_DIR, f"{prefix}-{browser_channel}-{reason}-{timestamp}")
    if os.path.exists(target_dir):
        target_dir = os.path.join(AUTH_PROFILE_BACKUP_DIR, f"{prefix}-{browser_channel}-{reason}-{timestamp}-{int(time.time() * 1000)}")
    shutil.move(source_dir, target_dir)
    _prune_platform_profile_backups(platform_id, browser_channel, keep_path=target_dir)
    return target_dir


def _platform_profile_backup_dirs(platform_id: str, browser_channel: str = "") -> list[str]:
    prefix = PLATFORM_PROFILE_PREFIX.get(platform_id) or platform_id
    channel = _sanitize_browser_channel(browser_channel)
    name_prefix = f"{prefix}-{channel}-" if channel else f"{prefix}-"
    try:
        names = os.listdir(AUTH_PROFILE_BACKUP_DIR)
    except OSError:
        return []
    result = []
    for name in names:
        if not name.startswith(name_prefix):
            continue
        target = os.path.join(AUTH_PROFILE_BACKUP_DIR, name)
        if os.path.isdir(target):
            result.append(target)
    return sorted(result)


def _prune_platform_profile_backups(platform_id: str, browser_channel: str = "", *, keep_path: str = "") -> list[str]:
    kept = os.path.realpath(keep_path) if keep_path else ""
    removed = []
    for target in _platform_profile_backup_dirs(platform_id, browser_channel):
        if kept and os.path.realpath(target) == kept:
            continue
        try:
            if _remove_tree_if_exists(target, allowed_root=AUTH_PROFILE_BACKUP_DIR):
                removed.append(target)
        except (FileNotFoundError, OSError, ValueError):
            continue
    return removed


def _restore_platform_profile(platform_id: str, browser_channel: str, backup_dir: str) -> str:
    if not backup_dir or not os.path.isdir(backup_dir):
        return ""

    target_dir = _platform_profile_dir(platform_id, browser_channel)
    try:
        if os.path.isdir(target_dir):
            shutil.rmtree(target_dir)
        elif os.path.exists(target_dir):
            os.remove(target_dir)
    except Exception:
        return ""

    try:
        shutil.move(backup_dir, target_dir)
    except Exception:
        return ""
    return target_dir


def _profile_backup_has_entries(path: str) -> bool:
    if not path or not os.path.isdir(path):
        return False
    try:
        with os.scandir(path) as entries:
            return any(True for _ in entries)
    except OSError:
        return False


def _profile_needs_bootstrap(profile_dir: str) -> bool:
    if not profile_dir:
        return False
    if not os.path.isdir(profile_dir):
        return True
    return not _profile_backup_has_entries(profile_dir)


def _profile_seed_is_usable(profile_dir: str) -> bool:
    """预热后存活检查：确认 Chromium 确实写入了 profile 目录。

    某些打包环境下 seed 进程退出码为 0，但 Chromium 启动即崩、profile 目录
    没有真正初始化（空的或只有 USER_DATA_DIR 占位）。只看退出码会把这种
    "假成功" 当成预热完成，导致随后真实授权窗口秒退。这里要求目录里至少
    有 Chromium 正常运行后会写入的标志文件之一，才算预热真正生效。
    """
    if not profile_dir or not os.path.isdir(profile_dir):
        return False
    # Chromium 启动后会在 profile 根写 Local State / First Run，在 Default 子目录
    # 里写 Preferences 等文件。只要命中任一关键标志，就认为预热真正落盘了。
    root_markers = ("Local State", "First Run")
    if any(os.path.isfile(os.path.join(profile_dir, name)) for name in root_markers):
        return True
    default_dir = os.path.join(profile_dir, "Default")
    if os.path.isdir(default_dir):
        default_markers = ("Preferences", "Secure Preferences", "History")
        if any(os.path.isfile(os.path.join(default_dir, name)) for name in default_markers):
            return True
    return False


def _effective_subprocess_path(base_path: str) -> str:
    if os.name == "nt":
        return base_path
    default_path = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    return f"{base_path}:{default_path}" if base_path else default_path


_SUPERVISION_SECRET_ENV_KEYS = frozenset(
    {
        "YIRENGONGIS_SESSION_TOKEN",
        "YIRENGONGIS_SUPERVISED_BY_TAURI",
        "YIRENGONGIS_SIDECAR_INSTANCE_ID",
        "YIRENGONGIS_RUNNER_READY_NONCE",
        # Desktop/IDE parent processes may export Node inspector hooks.  A
        # collector inheriting them can print "Waiting for the debugger to
        # disconnect" and keep its queue slot after business completion.
        "NODE_OPTIONS",
        "NODE_INSPECT",
        "NODE_DEBUG",
        "VSCODE_INSPECTOR_OPTIONS",
        "NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S",
        "NODE_REPL_TRUSTED_CODE_PATHS",
    }
)


def _scrub_supervision_env(env: dict | None) -> dict:
    scrubbed = dict(env or {})
    for key in _SUPERVISION_SECRET_ENV_KEYS:
        scrubbed.pop(key, None)
    return scrubbed


def _resolve_node_bin_for_env(env: dict) -> str:
    explicit = str((env or {}).get("NODE_BIN") or "").strip()
    if explicit:
        return explicit
    return resolve_default_node_bin(str((env or {}).get("PATH") or ""))


def _collector_runtime_preflight_error(base_dir: str | None = None) -> str:
    """Return a stable error code when an exported collector runtime is incomplete."""
    runtime_root = os.path.abspath(base_dir or BASE_DIR)
    playwright_package = os.path.join(runtime_root, "node_modules", "playwright", "package.json")
    if not os.path.isfile(playwright_package):
        return "playwright_not_installed"
    return ""


def load_auth_state() -> dict:
    if not os.path.exists(AUTH_STATE_FILE):
        return {}
    try:
        with open(AUTH_STATE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}

    result = {}
    for platform_id, item in payload.items():
        if platform_id not in VALID_PLATFORM_IDS or not isinstance(item, dict):
            continue
        status = _sanitize_auth_status(item.get("status"))
        if not status:
            continue
        result[platform_id] = {
            "status": status,
            "reason": str(item.get("reason") or "").strip().lower(),
            "updated_at": str(item.get("updated_at") or "").strip(),
        }
    return result


def save_auth_state(payload: dict) -> dict:
    with AUTH_STATE_WRITE_LOCK:
        current = load_auth_state()
        merged = {}
        if isinstance(current, dict):
            merged.update(current)
        if isinstance(payload, dict):
            for platform_id, item in payload.items():
                if platform_id not in VALID_PLATFORM_IDS or not isinstance(item, dict):
                    continue
                status = _sanitize_auth_status(item.get("status"))
                if not status:
                    continue
                merged[platform_id] = {
                    "status": status,
                    "reason": str(item.get("reason") or "").strip().lower(),
                    "updated_at": str(item.get("updated_at") or "").strip() or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
        _write_json_file_atomically(AUTH_STATE_FILE, merged)
        return merged


def _set_platform_auth_state(platform_id: str, status: str, reason: str = "", *, reset_health: bool = True) -> dict:
    normalized_status = _sanitize_auth_status(status)
    previous_status = _sanitize_auth_status((load_auth_state().get(platform_id) or {}).get("status"))
    saved = save_auth_state({
        platform_id: {
            "status": normalized_status,
            "reason": reason,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
    })
    if reset_health:
        callback = globals().get("_sync_auth_health_for_auth_state")
        if callable(callback):
            callback(platform_id, normalized_status, reason, previous_status=previous_status)
    return saved


def _persisted_auth_snapshot(platform_id: str) -> dict:
    saved = load_auth_state().get(platform_id) or {}
    saved_status = _sanitize_auth_status(saved.get("status"))
    saved_reason = str(saved.get("reason") or "").strip().lower()
    browser_channel = _configured_browser_channel()
    has_profile = _platform_has_profile(platform_id, browser_channel)

    if saved_status:
        if saved_status == "authorized" and not has_profile:
            return {
                "status": "needs_auth",
                "reason": "profile_missing",
                "source": "auth_state_profile_missing",
            }
        return {
            "status": saved_status,
            "reason": saved_reason,
            "source": "auth_state",
        }
    return {
        "status": "unauthorized",
        "reason": "not_authorized",
        "source": "default",
    }


def reset_onboarding_state(*, clear_auth: bool = False) -> dict:
    downloads_root = DOWNLOADS_DIR
    cleared = []
    missing = []

    targets = [CONFIG_FILE, RUN_HISTORY_FILE, *PLATFORM_PROGRESS_FILES.values()]
    for file_path in targets:
        removed = _remove_file_if_exists(
            file_path,
            allowed_root=AUTH_DIR if _path_within_root(file_path, AUTH_DIR) else downloads_root,
        )
        (cleared if removed else missing).append(file_path)

    auth_profiles_dir = os.path.join(AUTH_DIR, "profiles")
    if clear_auth:
        removed_auth_state = _remove_file_if_exists(AUTH_STATE_FILE, allowed_root=AUTH_DIR)
        (cleared if removed_auth_state else missing).append(AUTH_STATE_FILE)
        removed_auth_health = _remove_file_if_exists(AUTH_HEALTH_FILE, allowed_root=AUTH_DIR)
        (cleared if removed_auth_health else missing).append(AUTH_HEALTH_FILE)
        removed_profiles = _remove_tree_if_exists(auth_profiles_dir, allowed_root=AUTH_DIR)
        (cleared if removed_profiles else missing).append(auth_profiles_dir)
        removed_profile_backups = _remove_tree_if_exists(AUTH_PROFILE_BACKUP_DIR, allowed_root=AUTH_DIR)
        (cleared if removed_profile_backups else missing).append(AUTH_PROFILE_BACKUP_DIR)

    ensure_runtime_dirs()
    current_config = load_saved_config()
    return {
        "ok": True,
        "message": "onboarding_reset",
        "clear_auth": clear_auth,
        "cleared": [os.path.relpath(path, BASE_DIR) for path in cleared],
        "missing": [os.path.relpath(path, BASE_DIR) for path in missing],
        "config": public_config_payload(current_config),
        "summary": config_summary(current_config),
    }


def revoke_platform_auth(platform_id: str) -> dict:
    if platform_id not in VALID_PLATFORM_IDS:
        raise ValueError(f"invalid_platform: {platform_id}")
    cleared = []
    missing = []
    failed = []
    for dir_path in _platform_profile_dirs(platform_id):
        try:
            removed = _remove_tree_if_exists(dir_path, allowed_root=AUTH_DIR)
            (cleared if removed else missing).append(dir_path)
        except Exception:
            failed.append(dir_path)
    for dir_path in _platform_profile_backup_dirs(platform_id):
        try:
            removed = _remove_tree_if_exists(dir_path, allowed_root=AUTH_PROFILE_BACKUP_DIR)
            (cleared if removed else missing).append(dir_path)
        except Exception:
            failed.append(dir_path)
    _set_platform_auth_state(platform_id, "unauthorized", "not_authorized")
    # 重置进度文件，防止旧的 completed 状态覆盖刚撤销的授权
    progress_file = PLATFORM_PROGRESS_FILES.get(platform_id)
    if progress_file and os.path.exists(progress_file):
        try:
            _write_json_file_atomically(progress_file, default_progress(platform_id), indent=None)
        except Exception:
            pass
    return {
        "ok": True,
        "platform": platform_id,
        "message": "auth_revoked",
        "cleared": [os.path.relpath(path, BASE_DIR) for path in cleared],
        "missing": [os.path.relpath(path, BASE_DIR) for path in missing],
        "failed": [os.path.relpath(path, BASE_DIR) for path in failed],
    }


def feishu_config_ready(config: dict) -> bool:
    return bool(_feishu_sync_mode(config))


def _lark_cli_app_id(use_global: bool | None = None) -> str:
    cli_config = _read_lark_cli_config(use_global=use_global)
    if not isinstance(cli_config, dict):
        return ""
    app_id = str(cli_config.get("appId") or cli_config.get("app_id") or "").strip()
    if app_id:
        return app_id
    apps = cli_config.get("apps", [])
    if apps and isinstance(apps, list):
        return str((apps[0] or {}).get("appId") or (apps[0] or {}).get("app_id") or "").strip()
    return ""


def _feishu_cli_ready(config: dict) -> bool:
    if not config.get("feishu_enabled") or not config.get("feishu_cli_mode"):
        return False
    app_id = str(config.get("feishu_app_id") or "").strip()
    if not app_id:
        return False
    use_global = _saved_feishu_cli_use_global_home(config)
    if not _lark_cli_is_configured(use_global=use_global):
        return False
    cli_app_id = _lark_cli_app_id(use_global=use_global)
    return not cli_app_id or cli_app_id == app_id


def _feishu_app_ready(config: dict) -> bool:
    return bool(
        config.get("feishu_enabled")
        and config.get("feishu_app_token")
        and config.get("feishu_app_id")
        and config.get("feishu_app_secret")
    )


def _feishu_sync_mode(config: dict) -> str:
    if _feishu_cli_ready(config or {}):
        return "cli"
    if _feishu_app_ready(config or {}):
        return "app"
    return ""


def _authorized_enabled_platforms(enabled_platforms: list[str]) -> list[str]:
    authorized = []
    for platform_id in enabled_platforms:
        progress_path = PLATFORM_PROGRESS_FILES.get(platform_id, "")
        progress = _load_platform_progress(platform_id, progress_path)
        snapshot = _resolved_auth_snapshot(platform_id, progress)
        if _sanitize_auth_status(snapshot.get("status")) == "authorized":
            authorized.append(platform_id)
    return authorized


def config_summary(config: dict) -> dict:
    enabled_platforms = config.get("enabled_platforms") or []
    authorized_platforms = _authorized_enabled_platforms(enabled_platforms)
    has_run_history = bool(_read_run_history())
    all_enabled_platforms_authorized = bool(enabled_platforms) and len(authorized_platforms) == len(enabled_platforms)
    setup_complete = bool(
        config.get("onboarding_completed")
        and config.get("workspace_name")
        and config.get("min_publish_date")
        and enabled_platforms
        and (all_enabled_platforms_authorized or has_run_history)
    )
    return {
        "setup_complete": setup_complete,
        "feishu_ready": feishu_config_ready(config),
        "feishu_enabled": bool(config.get("feishu_enabled")),
        "auto_sync_enabled": bool(config.get("feishu_auto_sync")),
        "has_customer_name": bool(config.get("customer_name")),
        "has_workspace_name": bool(config.get("workspace_name")),
        "onboarding_completed": bool(config.get("onboarding_completed")),
        "enabled_platform_count": len(enabled_platforms),
        "enabled_platforms": enabled_platforms,
        "authorized_platform_count": len(authorized_platforms),
        "authorized_platforms": authorized_platforms,
        "has_run_history": has_run_history,
    }


def _mask_license_key(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 4:
        return text
    return f"{text[:4]}…{text[-4:]}"


def _session_required_for_path(path: str) -> bool:
    if not SESSION_TOKEN:
        return False
    if path == "/progress" and _is_tauri_supervised():
        return True
    if path in {"/", "/monitor", "/progress", "/session/recover", "/package-info", "/update/check", "/update/download-progress"}:
        return False
    if path.startswith("/assets/"):
        return False
    return True


# Paths that are allowed even without a valid license (UI chrome, activation flow, health).
_LICENSE_EXEMPT_PATHS = frozenset({
    "/", "/monitor", "/progress", "/session/recover", "/package-info",
    SUPERVISED_HEALTH_PATH,
    "/update/check", "/update/download", "/update/download-progress", "/update/install", "/update/reveal",
    "/license", "/license/activate",
})


def _license_required_for_path(path: str) -> bool:
    """Return True if *path* requires a valid & activated license to proceed."""
    if path in _LICENSE_EXEMPT_PATHS:
        return False
    if path.startswith("/assets/"):
        return False
    return True


def _remove_lock_file(lock_path: str) -> None:
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        pass


def _is_lock_file_active(lock_path: str) -> bool:
    if not os.path.exists(lock_path):
        return False

    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            content = (f.read() or "").strip()
    except Exception:
        return True

    # Parse lock content: "timestamp" or "timestamp pid"
    parts = content.split()
    try:
        started = float(parts[0])
    except (IndexError, ValueError):
        return True

    # If lock contains a PID, check if that process is still alive.
    if len(parts) >= 2:
        try:
            pid = int(parts[1])
            if not _pid_alive(pid):
                _remove_lock_file(lock_path)
                return False
            return True
        except (ValueError, TypeError):
            pass

    if time.time() - started > LOCK_STALE_SECONDS:
        _remove_lock_file(lock_path)
        return False

    return True


def _write_lock_file(lock_path: str) -> None:
    _ensure_parent_dir(lock_path)
    with open(lock_path, "w", encoding="utf-8") as f:
        f.write(f"{time.time()} {os.getpid()}")


def unlock(run_id: str = "", *, force: bool = False) -> bool:
    return _RUN_LEASE_STORE.release(str(run_id or ""), force=force)


def is_locked():
    return _RUN_LEASE_STORE.is_active()


def lock(kind: str = "task") -> LeaseToken | None:
    return _RUN_LEASE_STORE.acquire(kind)


def auth_lock_path(platform_id: str) -> str:
    platform = str(platform_id or "").strip()
    if platform not in VALID_PLATFORM_IDS:
        raise ValueError(f"invalid_platform: {platform}")
    return os.path.join(AUTH_LOCK_DIR, f"{platform}.lock")


def unlock_auth(platform_id: str) -> None:
    try:
        _remove_lock_file(auth_lock_path(platform_id))
    except ValueError:
        pass


def is_auth_locked(platform_id: str) -> bool:
    try:
        return _is_lock_file_active(auth_lock_path(platform_id))
    except ValueError:
        return False


def lock_auth(platform_id: str) -> None:
    _write_lock_file(auth_lock_path(platform_id))


def active_auth_locks() -> list[str]:
    active = []
    for platform_id in VALID_PLATFORM_IDS:
        if is_auth_locked(platform_id):
            active.append(platform_id)
    return active


def clear_auth_locks() -> None:
    for platform_id in VALID_PLATFORM_IDS:
        unlock_auth(platform_id)


def default_progress(platform):
    return {
        "platform": platform,
        "status": "idle",
        "phase": "idle",
        "message": "待机中",
        "startedAt": None,
        "finishedAt": None,
        "updatedAt": None,
        "totalWorks": 0,
        "queuedWorks": 0,
        "processedWorks": 0,
        "successWorks": 0,
        "skippedWorks": 0,
        "failedWorks": 0,
        "currentIndex": 0,
        "currentWorkId": "",
        "currentTitle": "",
    }


def _stable_progress_snapshot_path(progress_path: str) -> str:
    return f"{progress_path}.stable.json"


def _write_stable_progress_snapshot(progress_path: str, payload: dict) -> None:
    _write_json_file_atomically(_stable_progress_snapshot_path(progress_path), payload)


def _load_stable_progress_snapshot(platform_id: str, progress_path: str) -> dict:
    snapshot = read_json_file(_stable_progress_snapshot_path(progress_path), default_progress(platform_id))
    if not isinstance(snapshot, dict):
        return default_progress(platform_id)
    return snapshot


def _restore_stale_progress_if_needed(platform_id: str, progress_path: str, payload: dict) -> dict:
    if not _is_stale_running_progress(payload, platform_id):
        return payload
    restored = dict(_load_stable_progress_snapshot(platform_id, progress_path))
    if str(restored.get("status") or "").strip().lower() == "running":
        restored = default_progress(platform_id)
    restored["platform"] = platform_id
    restored["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    restored["recovered_from_stale_run"] = True
    restored["recovery_reason"] = "stale_running_progress"
    _write_json_file_atomically(progress_path, restored)
    return restored


def _load_platform_progress(platform_id: str, progress_path: str) -> dict:
    progress = read_json_file(progress_path, default_progress(platform_id))
    if not isinstance(progress, dict):
        progress = default_progress(platform_id)
    return _restore_stale_progress_if_needed(platform_id, progress_path, progress)


def _progress_auth_text(progress: dict) -> str:
    return " ".join(
        str(progress.get(key) or "")
        for key in ("auth_status", "auth_reason", "message", "phase", "status", "error")
    ).lower()


def _auth_flow_has_explicit_success(progress: dict) -> bool:
    return _sanitize_auth_status(progress.get("auth_status")) == "authorized"


def _auth_verify_launch_failure_can_preserve_success(progress: dict, query: dict, stdout: str = "", stderr: str = "") -> bool:
    if not _auth_flow_has_explicit_success(progress):
        return False
    if _to_bool(_query_value(query, "headless", True)):
        return False
    return _auth_profile_launch_failure(stdout, stderr)


def _query_value(query: dict, key: str, default=""):
    if not isinstance(query, dict):
        return default
    raw = query.get(key, [default])
    if isinstance(raw, list):
        return raw[0] if raw else default
    return raw


def _resolve_query_date_window(query: dict, *, default_min_date: str = "") -> tuple[str, str]:
    raw_min_date = str(_query_value(query, "min_date", default_min_date) or "").strip()
    raw_max_date = str(_query_value(query, "max_date", "") or "").strip()
    min_date = _normalize_date_text(raw_min_date) or _normalize_date_text(default_min_date)
    max_date = _normalize_date_text(raw_max_date)
    if raw_min_date and not min_date:
        raise ValueError("起始日期格式无效，应为 YYYY-MM-DD。")
    if raw_max_date and not max_date:
        raise ValueError("结束日期格式无效，应为 YYYY-MM-DD。")
    if min_date and max_date and max_date < min_date:
        raise ValueError("结束日期不能早于起始日期。")
    return min_date, max_date


def _build_auth_verification_query(query: dict) -> dict:
    probe_query = {}
    if isinstance(query, dict):
        for key, value in query.items():
            if isinstance(value, list):
                probe_query[key] = [str(item) for item in value]
            elif value is None:
                probe_query[key] = []
            else:
                probe_query[key] = [str(value)]
    probe_query["auth_only"] = ["true"]
    probe_query["headless"] = ["true"]
    probe_query["scan_wait_ms"] = ["5000"]
    return probe_query


def _detect_auth_reason(progress: dict) -> str:
    explicit_reason = str(progress.get("auth_reason") or "").strip().lower()
    if explicit_reason in {"not_authorized", "expired_cookie", "login_required", "manual_reauth_required", "profile_missing"}:
        return explicit_reason

    text = _progress_auth_text(progress)
    reason_markers = (
        ("profile_missing", ("profile_missing", "profile missing", "授权浏览器 profile 缺失", "登录态目录缺失")),
        ("manual_reauth_required", ("重新扫码", "重新授权", "manual_reauth_required", "auth_failed")),
        ("expired_cookie", ("cookie 过期", "cookie失效", "expired_cookie", "登录超时", "登录失效", "已失效")),
        ("login_required", ("需要登录", "login_required", "未登录", "请登录", "重新登录")),
        ("not_authorized", ("unauthorized", "not_authorized", "待授权", "未授权", "authorization")),
    )
    for reason, markers in reason_markers:
        if any(marker.lower() in text for marker in markers):
            return reason
    return ""


def _progress_auth_snapshot(progress: dict) -> dict:
    explicit_status = _sanitize_auth_status(progress.get("auth_status"))
    if explicit_status:
        return {
            "status": explicit_status,
            "reason": str(progress.get("auth_reason") or "").strip().lower(),
            "source": "progress_explicit_status",
        }

    explicit_needs_auth = progress.get("needs_auth")
    if explicit_needs_auth is True:
        return {
            "status": "needs_auth",
            "reason": str(progress.get("auth_reason") or "").strip().lower() or "manual_reauth_required",
            "source": "progress_explicit_needs_auth",
        }

    reason = _detect_auth_reason(progress)
    if reason == "not_authorized":
        return {"status": "unauthorized", "reason": reason, "source": "progress_reason"}
    if reason == "expired_cookie":
        return {"status": "expired", "reason": reason, "source": "progress_reason"}
    if reason in {"login_required", "manual_reauth_required", "profile_missing"}:
        return {"status": "needs_auth", "reason": reason, "source": "progress_reason"}

    status = str(progress.get("status") or "idle").lower()
    text = _progress_auth_text(progress)
    success_markers = ("授权成功", "已授权", "登录成功", "auth_success", "authorized")
    if any(marker.lower() in text for marker in success_markers):
        return {"status": "authorized", "reason": "", "source": "progress_success_marker"}
    if status == "failed":
        if reason in {"expired_cookie", "login_required", "manual_reauth_required", "profile_missing"}:
            return {"status": "needs_auth", "reason": reason, "source": "progress_failed"}
        return {"status": "", "reason": "", "source": ""}
    return {"status": "", "reason": "", "source": ""}


def _is_stale_running_progress(progress: dict, platform_id: str = "") -> bool:
    if str(progress.get("status") or "").strip().lower() != "running":
        return False
    if is_locked():
        return False
    if platform_id and is_auth_locked(platform_id):
        return False
    return True


def _resolved_auth_snapshot(platform_id: str, progress: dict) -> dict:
    persisted = _persisted_auth_snapshot(platform_id)
    progress_auth = _progress_auth_snapshot(progress)
    if _is_stale_running_progress(progress, platform_id):
        return persisted
    progress_status = _sanitize_auth_status(progress_auth.get("status"))
    if progress_status in {"unauthorized", "expired", "needs_auth"}:
        return progress_auth
    if progress_status == "authorized":
        return progress_auth
    return persisted


def _auth_status(platform_id: str, progress: dict) -> str:
    return _resolved_auth_snapshot(platform_id, progress).get("status") or "unauthorized"


def _auth_reason(platform_id: str, progress: dict) -> str:
    snapshot = _resolved_auth_snapshot(platform_id, progress)
    reason = str(snapshot.get("reason") or "").strip().lower()
    if reason in {"not_authorized", "expired_cookie", "login_required", "manual_reauth_required", "profile_missing"}:
        return reason
    auth_status = snapshot.get("status") or "unauthorized"
    return {
        "unauthorized": "not_authorized",
        "expired": "expired_cookie",
        "needs_auth": "manual_reauth_required",
    }.get(auth_status, "")


def _auth_action(auth_status: str) -> str:
    return {
        "unauthorized": "authorize",
        "expired": "reauthorize",
        "needs_auth": "reauthorize",
    }.get(auth_status, "none")


def _platform_needs_auth(progress: dict) -> bool:
    return _progress_auth_snapshot(progress).get("status") in {"unauthorized", "expired", "needs_auth"}


def _progress_summary_failure_buckets(platforms: dict) -> tuple[list[str], list[str]]:
    failed = []
    needs_auth = []
    for name, progress in (platforms or {}).items():
        if not isinstance(progress, dict) or not progress.get("enabled"):
            continue
        ui_status = str(progress.get("ui_status") or "").strip().lower()
        if ui_status == "failed":
            failed.append(name)
        elif ui_status == "auth_required":
            needs_auth.append(name)
    return failed, needs_auth


def _safe_int(value, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_bilibili_cleaned_selection_warning(
    platform_id: str,
    status: str,
    message: str,
    error: str = "",
    success_count: int = 0,
    failed_count: int = 0,
) -> bool:
    if platform_id != "bilibili":
        return False
    if str(status or "").strip().lower() not in {"failed", "error"}:
        return False
    text = f"{message or ''} {error or ''}"
    if "选择数量异常" not in text:
        return False
    return success_count > 0 and failed_count == 0


def _progress_started_ts(progress: dict) -> float:
    raw = str((progress or {}).get("startedAt") or "").strip()
    if not raw:
        return 0.0
    ts = pd.to_datetime(raw, errors="coerce")
    if pd.isna(ts):
        return 0.0
    return float(ts.timestamp())


def _platform_output_is_fresh(platform_id: str, progress: dict) -> bool:
    target = PLATFORM_EXPORT_FILES.get(platform_id)
    if not target or not os.path.exists(target):
        return False
    started_ts = _progress_started_ts(progress)
    if started_ts <= 0:
        return True
    return os.path.getmtime(target) >= (started_ts - 1.0)


def _bilibili_cleaned_selection_message(success_count: int) -> str:
    if success_count > 0:
        return f"B 站任务完成，共 {success_count} 条"
    return "B 站任务完成"


def _normalized_completed_message(platform_id: str, message: str, success_count: int) -> str:
    raw = str(message or "").strip()
    if platform_id == "douyin" and raw == "全部导出任务完成":
        if success_count > 0:
            return f"抖音任务完成，共 {success_count} 条"
        return "抖音任务完成"
    if platform_id == "bilibili" and raw.startswith("B 站任务完成"):
        return _bilibili_cleaned_selection_message(success_count)
    return raw


def _ui_status(platform_id: str, progress: dict) -> str:
    status = str(progress.get("status") or "idle")
    phase = str(progress.get("phase") or "idle")
    total = int(progress.get("totalWorks") or 0)
    success = int(progress.get("successWorks") or 0)
    failed = int(progress.get("failedWorks") or 0)
    skipped = int(progress.get("skippedWorks") or 0)
    auth_status = _auth_status(platform_id, progress)
    if _is_stale_running_progress(progress, platform_id):
        return "auth_required" if auth_status in {"unauthorized", "expired", "needs_auth"} else "idle"

    if status == "running" and phase == "queued":
        return "queued"
    if status == "running" and phase not in {"done", "failed"}:
        return "running"
    if auth_status in {"unauthorized", "expired", "needs_auth"}:
        return "auth_required"
    if status == "failed":
        if _is_bilibili_cleaned_selection_warning(
            platform_id,
            status,
            str(progress.get("message") or ""),
            "",
            success,
            failed,
        ) and _platform_output_is_fresh(platform_id, progress):
            return "completed"
        return "failed"
    if status == "completed":
        if success == 0 and failed == 0:
            return "completed_empty"
        return "completed"
    return "idle"


def _decorate_progress(platform_id: str, progress: dict, enabled_platforms: list[str]) -> dict:
    decorated = dict(progress)
    auth_status = _auth_status(platform_id, progress)
    status = str(progress.get("status") or "").strip().lower()
    phase = str(progress.get("phase") or "").strip().lower()
    auth_running = is_auth_locked(platform_id)
    ui_status = _ui_status(platform_id, progress)
    if auth_running or (
        status == "running"
        and (
            phase == "login"
            or auth_status in {"unauthorized", "expired", "needs_auth"}
        )
    ):
        ui_status = "authorizing"
    decorated["enabled"] = platform_id in enabled_platforms
    decorated["auth_status"] = auth_status
    decorated["auth_reason"] = _auth_reason(platform_id, progress)
    decorated["auth_action"] = _auth_action(auth_status)
    decorated["needs_auth"] = auth_status in {"unauthorized", "expired", "needs_auth"}
    decorated["auth_running"] = auth_running
    auth_health = _auth_health_snapshot(platform_id)
    decorated["auth_health_status"] = auth_health.get("status") or "unknown"
    decorated["auth_checked_at"] = auth_health.get("checked_at") or ""
    decorated["auth_last_success_at"] = auth_health.get("last_success_at") or ""
    decorated["auth_next_check_at"] = auth_health.get("next_check_at") or ""
    decorated["auth_check_reason"] = auth_health.get("reason") or ""
    decorated["auth_health_failure_count"] = auth_health.get("failure_count") or 0
    decorated["ui_status"] = ui_status
    if decorated["ui_status"] == "completed" and _is_bilibili_cleaned_selection_warning(
        platform_id,
        str(progress.get("status") or ""),
        str(progress.get("message") or ""),
        "",
        _safe_int(progress.get("successWorks")),
        _safe_int(progress.get("failedWorks")),
    ) and _platform_output_is_fresh(platform_id, progress):
        decorated["message"] = _bilibili_cleaned_selection_message(_safe_int(progress.get("successWorks")))
    elif decorated["ui_status"] == "completed":
        decorated["message"] = _normalized_completed_message(
            platform_id,
            str(progress.get("message") or ""),
            _safe_int(progress.get("successWorks")),
        )
    decorated["last_sync_at"] = progress.get("finishedAt") or progress.get("updatedAt")
    return decorated


def _feishu_sync_target_platforms(platform_results: list[dict]) -> list[str]:
    targets = []
    seen = set()
    for item in platform_results or []:
        if not isinstance(item, dict):
            continue
        platform_id = str(item.get("platform") or "").strip()
        if not platform_id or platform_id in seen:
            continue
        if str(item.get("status") or "").strip() not in {"success", "completed_empty"}:
            continue
        targets.append(platform_id)
        seen.add(platform_id)
    return targets


def _feishu_prepare_platforms(feishu_result: dict) -> list[str]:
    if not isinstance(feishu_result, dict):
        return []
    prepare = feishu_result.get("prepare") if isinstance(feishu_result.get("prepare"), dict) else {}
    targets = []
    seen = set()
    for item in prepare.get("platforms") or []:
        platform_id = str(item or "").strip()
        if not platform_id or platform_id in seen:
            continue
        targets.append(platform_id)
        seen.add(platform_id)
    return targets


def _feishu_prepare_has_syncable_data(prepare_meta: dict) -> bool:
    if not isinstance(prepare_meta, dict):
        return False
    if "has_local_changes" in prepare_meta:
        return bool(prepare_meta.get("has_local_changes"))
    for key in ("detail_count", "work_count", "chart_count", "increment_count"):
        try:
            if int(prepare_meta.get(key) or 0) > 0:
                return True
        except Exception:
            continue
    return False


def _normalize_feishu_baseline_value(value):
    if isinstance(value, dict):
        return {
            str(key): _normalize_feishu_baseline_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list):
        return [_normalize_feishu_baseline_value(item) for item in value]
    return value


def _normalize_feishu_table_row(row: dict) -> dict:
    if not isinstance(row, dict):
        return {}
    normalized = {}
    for key, value in row.items():
        field = str(key or "").strip()
        if not field or field in FEISHU_BASELINE_RUNTIME_ONLY_FIELDS:
            continue
        normalized[field] = _normalize_feishu_baseline_value(value)
    return normalized


def _normalize_feishu_detail_row(row: dict) -> dict:
    return _normalize_feishu_table_row(row)


def _feishu_table_upsert_keys(payload: dict) -> dict[str, str]:
    definitions = payload.get("table_definitions") if isinstance(payload, dict) else []
    keys: dict[str, str] = {}
    if isinstance(definitions, list):
        for definition in definitions:
            if not isinstance(definition, dict):
                continue
            table_name = str(definition.get("name") or "").strip()
            upsert_key = str(definition.get("upsert_key") or "同步键").strip()
            if table_name and upsert_key:
                keys[table_name] = upsert_key
    return keys


def _current_feishu_table_rows(payload: dict) -> dict[str, dict[str, dict]]:
    tables = payload.get("tables") if isinstance(payload, dict) else {}
    if not isinstance(tables, dict):
        return {}

    upsert_keys = _feishu_table_upsert_keys(payload)
    rows_by_table = {}
    for table_name in FEISHU_BUSINESS_TABLE_NAMES:
        raw_rows = tables.get(table_name, [])
        if not isinstance(raw_rows, list):
            continue
        upsert_key = upsert_keys.get(table_name, "同步键")
        table_rows = {}
        for item in raw_rows:
            if not isinstance(item, dict):
                continue
            sync_key = str(item.get(upsert_key) or item.get("同步键") or "").strip()
            if not sync_key:
                continue
            table_rows[sync_key] = _normalize_feishu_table_row(item)
        rows_by_table[table_name] = table_rows
    return rows_by_table


def _current_feishu_detail_rows(payload: dict) -> dict[str, dict]:
    return _current_feishu_table_rows(payload).get("平台明细V2", {})


def _load_feishu_sync_baseline() -> dict:
    raw = read_json_file(FEISHU_SYNC_BASELINE_FILE, {})
    detail_rows = raw.get("detail_rows") if isinstance(raw, dict) else {}
    normalized_rows = {}
    if isinstance(detail_rows, dict):
        for key, item in detail_rows.items():
            sync_key = str(key or "").strip()
            if not sync_key or not isinstance(item, dict):
                continue
            normalized_rows[sync_key] = _normalize_feishu_detail_row(item)

    normalized_tables = {}
    raw_tables = raw.get("tables") if isinstance(raw, dict) else {}
    if isinstance(raw_tables, dict):
        for table_name in FEISHU_BUSINESS_TABLE_NAMES:
            table_rows = raw_tables.get(table_name)
            if not isinstance(table_rows, dict):
                continue
            normalized_tables[table_name] = {
                str(key or "").strip(): _normalize_feishu_table_row(item)
                for key, item in table_rows.items()
                if str(key or "").strip() and isinstance(item, dict)
            }
    if normalized_rows and "平台明细V2" not in normalized_tables:
        normalized_tables["平台明细V2"] = normalized_rows

    return {
        "version": 2,
        "synced_at": str(raw.get("synced_at") or "").strip() if isinstance(raw, dict) else "",
        "timezone": str(raw.get("timezone") or "Asia/Shanghai").strip() if isinstance(raw, dict) else "Asia/Shanghai",
        "detail_rows": normalized_tables.get("平台明细V2", normalized_rows),
        "tables": normalized_tables,
    }


def _summarize_feishu_local_changes(payload: dict, baseline: dict) -> dict:
    current_tables = _current_feishu_table_rows(payload)
    baseline_tables = baseline.get("tables") if isinstance(baseline, dict) and isinstance(baseline.get("tables"), dict) else {}
    if not baseline_tables and isinstance(baseline, dict) and isinstance(baseline.get("detail_rows"), dict):
        baseline_tables = {"平台明细V2": baseline.get("detail_rows") or {}}

    # 同步键 is table-local: detail rows use platform_work_key, while work tables
    # use work_key. Compare rows inside each table instead of pooling all keys.
    new_count = 0
    updated_count = 0
    unchanged_count = 0
    per_table = {}
    for table_name in FEISHU_BUSINESS_TABLE_NAMES:
        current_rows = current_tables.get(table_name, {})
        baseline_rows = baseline_tables.get(table_name, {}) if isinstance(baseline_tables, dict) else {}
        table_new = 0
        table_updated = 0
        table_unchanged = 0
        for sync_key, current_row in current_rows.items():
            previous_row = baseline_rows.get(sync_key) if isinstance(baseline_rows, dict) else None
            if previous_row is None:
                table_new += 1
            elif previous_row != current_row:
                table_updated += 1
            else:
                table_unchanged += 1
        new_count += table_new
        updated_count += table_updated
        unchanged_count += table_unchanged
        per_table[table_name] = {
            "current_count": len(current_rows),
            "new_count": table_new,
            "updated_count": table_updated,
            "unchanged_count": table_unchanged,
        }

    detail_stats = per_table.get("平台明细V2", {})

    return {
        "baseline_available": any(bool(rows) for rows in (baseline_tables or {}).values()) if isinstance(baseline_tables, dict) else False,
        "current_detail_count": detail_stats.get("current_count", 0),
        "new_detail_count": detail_stats.get("new_count", 0),
        "updated_detail_count": detail_stats.get("updated_count", 0),
        "unchanged_detail_count": detail_stats.get("unchanged_count", 0),
        "current_business_row_count": sum(item["current_count"] for item in per_table.values()),
        "new_business_row_count": new_count,
        "updated_business_row_count": updated_count,
        "unchanged_business_row_count": unchanged_count,
        "business_table_changes": per_table,
        "has_local_changes": (new_count + updated_count) > 0,
    }


def _persist_feishu_sync_baseline(payload: dict, *, synced_at: str = "") -> dict:
    existing = _load_feishu_sync_baseline()
    merged_rows = dict(existing.get("detail_rows") or {})
    merged_rows.update(_current_feishu_detail_rows(payload))
    merged_tables = {
        table_name: dict(rows)
        for table_name, rows in (existing.get("tables") or {}).items()
        if isinstance(rows, dict)
    }
    for table_name, current_rows in _current_feishu_table_rows(payload).items():
        table_rows = dict(merged_tables.get(table_name) or {})
        table_rows.update(current_rows)
        merged_tables[table_name] = table_rows
    updated = {
        "version": 2,
        "synced_at": str(synced_at or _format_run_time()).strip(),
        "timezone": "Asia/Shanghai",
        "detail_rows": merged_rows,
        "tables": merged_tables,
    }
    _write_json_file_atomically(FEISHU_SYNC_BASELINE_FILE, updated)
    return updated


def _auth_required_message(platform_id: str, auth_status: str, auth_reason: str) -> str:
    label = _platform_label(platform_id)
    status = _sanitize_auth_status(auth_status) or "unauthorized"
    reason = str(auth_reason or "").strip().lower()
    if reason == "profile_missing":
        return f"{label} 当前授权浏览器的登录态目录缺失，请先重新授权后再同步。"
    if status == "unauthorized" or reason == "not_authorized":
        return f"{label} 未授权，请先授权后再同步。"
    if status == "expired" or reason == "expired_cookie":
        return f"{label} 授权已失效，请先重新授权后再同步。"
    if reason == "login_required":
        return f"{label} 需要重新登录，请先重新授权后再同步。"
    return f"{label} 需要重新授权，请先授权后再同步。"


def _run_platform_auth_gate_info(platform_id: str, progress_path: str) -> dict:
    progress = _load_platform_progress(platform_id, progress_path)
    snapshot = _resolved_auth_snapshot(platform_id, progress)
    auth_status = _sanitize_auth_status(snapshot.get("status")) or "unauthorized"
    auth_reason = str(snapshot.get("reason") or "").strip().lower() or _auth_reason(platform_id, progress)
    blocked = auth_status != "authorized"
    return {
        "platform": platform_id,
        "progress_path": progress_path,
        "blocked": blocked,
        "auth_status": auth_status,
        "auth_reason": auth_reason,
        "message": "" if not blocked else _auth_required_message(platform_id, auth_status, auth_reason),
    }


def _preflight_platform_run(platform_id: str, progress_path: str) -> dict:
    info = _run_platform_auth_gate_info(platform_id, progress_path)
    if not info.get("blocked"):
        return {
            "blocked": False,
            "auth_status": info.get("auth_status"),
            "auth_reason": "",
            "message": "",
        }

    auth_status = str(info.get("auth_status") or "unauthorized")
    auth_reason = str(info.get("auth_reason") or "")
    message = str(info.get("message") or "")
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _patch_progress_state(
        progress_path,
        platform_id,
        {
            "status": "failed",
            "phase": "auth_required",
            "message": message,
            "error": message,
            "startedAt": now,
            "finishedAt": now,
            "auth_status": auth_status,
            "auth_reason": auth_reason,
            "needs_auth": True,
        },
        reset=True,
    )
    _set_platform_auth_state(platform_id, auth_status, auth_reason)
    return {
        "blocked": True,
        "auth_status": auth_status,
        "auth_reason": auth_reason,
        "message": message,
    }


def _resolve_requested_run_targets(path: str, query: dict) -> list[tuple[str, str]]:
    if path == "/run_all":
        enabled = _enabled_platform_scope(load_saved_config())
        allowed = set(resolve_requested_targets(query, enabled))
        return [
            (platform_id, progress_path)
            for platform_id, progress_path in RUN_ALL_PLATFORM_PROGRESS_STEPS
            if platform_id in allowed
        ]

    platform_id = RUN_ROUTE_PLATFORM_MAP.get(path)
    progress_path = PLATFORM_PROGRESS_FILES.get(platform_id or "")
    if platform_id and progress_path:
        return [(platform_id, progress_path)]
    return []


def _blocked_run_request_payload(path: str, query: dict) -> dict | None:
    targets = _resolve_requested_run_targets(path, query)
    if not targets:
        return None

    infos = [_run_platform_auth_gate_info(platform_id, progress_path) for platform_id, progress_path in targets]
    if any(not info.get("blocked") for info in infos):
        return None

    for info in infos:
        _preflight_platform_run(str(info.get("platform") or ""), str(info.get("progress_path") or ""))

    message = str(infos[0].get("message") or "请先授权后再同步。") if len(infos) == 1 else "当前没有已授权的平台，请先授权至少一个平台后再同步。"
    return {
        "ok": False,
        "status": "failed",
        "run_stage_status": "failed",
        "failed_stage": "platform_scraping",
        "duration": 0,
        "error": "auth_required",
        "message": message,
        "platform_results": [
            {
                "platform": info.get("platform"),
                "status": "needs_auth",
                "auth_status": info.get("auth_status"),
                "auth_reason": info.get("auth_reason"),
                "auth_action": _auth_action(str(info.get("auth_status") or "")),
                "needs_auth": True,
                "message": info.get("message") or message,
            }
            for info in infos
        ],
    }


def _write_progress_file(progress_path: str, payload: dict) -> dict:
    _write_json_file_atomically(progress_path, payload)
    if str(payload.get("status") or "").strip().lower() != "running":
        try:
            _write_stable_progress_snapshot(progress_path, payload)
        except Exception:
            pass
    return payload


def _patch_progress_state(progress_path: str, platform_id: str, patch: dict, *, reset: bool = False) -> dict:
    payload = default_progress(platform_id) if reset else read_json_file(progress_path, default_progress(platform_id))
    payload.update(patch)
    payload["platform"] = platform_id
    payload["updatedAt"] = str(patch.get("updatedAt") or time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    return _write_progress_file(progress_path, payload)


def _prime_platform_progress(platform_id: str, progress_path: str, *, phase: str, message: str) -> dict:
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    # Preserve auth fields from the persisted auth state so the frontend never
    # flickers between "authorized" → "unauthorized" → "authorized" during the
    # brief window after priming but before the script writes its own auth status.
    persisted = _persisted_auth_snapshot(platform_id)
    persisted_status = persisted.get("status") or ""
    persisted_reason = persisted.get("reason") or ""
    return _patch_progress_state(
        progress_path,
        platform_id,
        {
            "status": "running",
            "phase": phase,
            "message": message,
            "startedAt": now,
            "finishedAt": None,
            "currentIndex": 0,
            "currentWorkId": "",
            "currentTitle": "",
            "auth_status": persisted_status,
            "auth_reason": persisted_reason,
            "needs_auth": persisted_status in {"unauthorized", "expired", "needs_auth"},
        },
        reset=True,
    )


def _pick_process_message(stdout: str = "", stderr: str = "", fallback: str = "") -> str:
    for text in (stderr, stdout):
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        if not lines:
            continue
        error_lines = [line for line in lines if "[error]" in line.lower()]
        if error_lines:
            return error_lines[-1]
        return lines[-1]
    return fallback


def _process_failure_detail(stdout: str = "", stderr: str = "", fallback: str = "") -> str:
    combined = "\n".join(part for part in (stderr, stdout) if str(part or "").strip())
    lowered = combined.lower()
    if any(marker in lowered for marker in ("need_user_authorization", "91403", "you don't have permission", "openapiaddfield limited", "800004135")) or "补充用户授权" in combined:
        lines = [line.strip() for line in combined.splitlines() if line.strip()]
        focused = [
            line
            for line in lines
            if any(marker in line.lower() for marker in ("need_user_authorization", "91403", "you don't have permission", "openapiaddfield limited", "800004135"))
            or "补充用户授权" in line
        ]
        return "\n".join(focused or lines[-20:]).strip()
    message = _pick_process_message(stdout, stderr, fallback)
    return str(message or fallback or "任务失败").strip()


def _finalize_platform_progress(
    platform_id: str,
    progress_path: str,
    *,
    ok: bool,
    stdout: str = "",
    stderr: str = "",
    auth_only: bool = False,
    success_message: str = "",
    failure_message: str = "",
) -> dict:
    if ok and auth_only:
        progress = read_json_file(progress_path, default_progress(platform_id))
        if _auth_flow_has_explicit_success(progress):
            _mark_auth_flow_completed(platform_id, progress_path)
            return read_json_file(progress_path, default_progress(platform_id))
        ok = False
        stderr = "\n".join(
            part
            for part in (
                stderr,
                "[runner] 授权流程结束，但未检测到明确的授权成功标记。",
            )
            if part
        )

    progress = read_json_file(progress_path, default_progress(platform_id))
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    patch = {}
    current_status = str(progress.get("status") or "").strip().lower()
    current_phase = str(progress.get("phase") or "").strip().lower()
    combined_text = "\n".join(
        part for part in (progress.get("message"), progress.get("error"), stderr, stdout, failure_message) if part
    )
    auth_reason = _detect_auth_reason(
        {
            "auth_reason": progress.get("auth_reason"),
            "message": combined_text,
            "phase": progress.get("phase"),
            "status": progress.get("status"),
            "error": progress.get("error"),
        }
    )
    if auth_only and auth_reason in {"expired_cookie", "login_required"}:
        persisted_status = _sanitize_auth_status(_persisted_auth_snapshot(platform_id).get("status")) or "unauthorized"
        if persisted_status == "unauthorized":
            auth_reason = "not_authorized"

    if ok:
        if current_status in {"", "idle", "running"} or current_phase in {"starting", "login"}:
            patch.update(
                {
                    "status": "completed",
                    "phase": "completed",
                    "message": success_message or progress.get("message") or "同步完成",
                    "finishedAt": now,
                }
            )
        if str(progress.get("auth_status") or "").strip().lower() != "authorized":
            patch["auth_status"] = "authorized"
            patch["auth_reason"] = ""
            patch["needs_auth"] = False
    else:
        if current_status in {"", "idle", "running"} or not progress.get("finishedAt"):
            patch.update(
                {
                    "status": "failed",
                    "phase": "failed",
                    "message": failure_message or _pick_process_message(stdout, stderr, "任务失败"),
                    "finishedAt": now,
                }
            )
        if auth_reason:
            auth_status = "unauthorized" if auth_reason == "not_authorized" else ("expired" if auth_reason == "expired_cookie" else "needs_auth")
            patch["auth_status"] = auth_status
            patch["auth_reason"] = auth_reason
            patch["needs_auth"] = auth_status in {"unauthorized", "expired", "needs_auth"}
        elif auth_only and str(progress.get("auth_status") or "").strip().lower() not in {"unauthorized", "expired", "needs_auth"}:
            patch["auth_status"] = "needs_auth"
            patch["auth_reason"] = "manual_reauth_required"
            patch["needs_auth"] = True

    if not patch:
        return progress
    return _patch_progress_state(progress_path, platform_id, patch)


def _mark_auth_flow_completed(platform_id: str, progress_path: str) -> None:
    progress = read_json_file(progress_path, default_progress(platform_id))
    progress["status"] = "completed"
    progress["phase"] = "completed"
    if not progress.get("manual_reauth_restored"):
        progress["message"] = "授权完成"
    progress["needs_auth"] = False
    progress["auth_status"] = "authorized"
    progress["auth_reason"] = ""
    progress.pop("manual_reauth_restored", None)
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    progress["updatedAt"] = now
    progress["finishedAt"] = now
    _write_json_file_atomically(progress_path, progress)


def _sync_platform_auth_state_from_progress(platform_id: str, progress_path: str) -> dict:
    progress = _load_platform_progress(platform_id, progress_path)
    snapshot = _progress_auth_snapshot(progress)
    status = _sanitize_auth_status(snapshot.get("status"))
    if not status:
        return {}
    if str(progress_path or "").endswith(".probe.json") and status != "authorized":
        return {}
    reason = str(snapshot.get("reason") or "").strip().lower()
    if status == "authorized":
        reason = ""
    _set_platform_auth_state(platform_id, status, reason)
    return {
        "status": status,
        "reason": reason,
    }


AUTH_HEALTH_STATUSES = frozenset({"healthy", "expired", "needs_auth", "unknown", "skipped"})
AUTH_HEALTH_PUBLIC_FIELDS = (
    "status",
    "checked_at",
    "last_success_at",
    "next_check_at",
    "reason",
    "failure_count",
    "error_code",
)


def _auth_health_time_text(timestamp: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() if timestamp is None else timestamp))


def _auth_health_timestamp(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def load_auth_health_state() -> dict:
    with AUTH_HEALTH_STATE_LOCK:
        payload = _load_json_dict(AUTH_HEALTH_FILE)
    result = {}
    for platform_id, item in payload.items():
        if platform_id not in VALID_PLATFORM_IDS or not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if status not in AUTH_HEALTH_STATUSES:
            continue
        result[platform_id] = {
            "status": status,
            "checked_at": str(item.get("checked_at") or "").strip(),
            "last_success_at": str(item.get("last_success_at") or "").strip(),
            "next_check_at": str(item.get("next_check_at") or "").strip(),
            "reason": str(item.get("reason") or "").strip().lower(),
            "failure_count": max(_safe_int(item.get("failure_count")), 0),
            "error_code": str(item.get("error_code") or "").strip().lower(),
        }
    return result


def save_auth_health_state(payload: dict) -> dict:
    with AUTH_HEALTH_STATE_LOCK:
        merged = load_auth_health_state()
        for platform_id, item in (payload or {}).items():
            if platform_id not in VALID_PLATFORM_IDS or not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").strip().lower()
            if status not in AUTH_HEALTH_STATUSES:
                continue
            previous = merged.get(platform_id) or {}
            normalized = {}
            for field in AUTH_HEALTH_PUBLIC_FIELDS:
                value = item[field] if field in item else previous.get(field, "")
                if field == "failure_count":
                    normalized[field] = max(_safe_int(value), 0)
                else:
                    normalized[field] = str(value or "").strip().lower() if field in {"status", "reason", "error_code"} else str(value or "").strip()
            merged[platform_id] = normalized
        _write_json_file_atomically(AUTH_HEALTH_FILE, merged)
        if os.name != "nt":
            try:
                os.chmod(AUTH_HEALTH_FILE, 0o600)
            except OSError:
                pass
        return merged


def _auth_health_snapshot(platform_id: str) -> dict:
    item = load_auth_health_state().get(platform_id) or {}
    return {
        "status": str(item.get("status") or "unknown").strip().lower(),
        "checked_at": str(item.get("checked_at") or "").strip(),
        "last_success_at": str(item.get("last_success_at") or "").strip(),
        "next_check_at": str(item.get("next_check_at") or "").strip(),
        "reason": str(item.get("reason") or "").strip().lower(),
        "failure_count": max(_safe_int(item.get("failure_count")), 0),
        "error_code": str(item.get("error_code") or "").strip().lower(),
    }


def _mark_auth_health_pending(platform_id: str) -> dict:
    previous = _auth_health_snapshot(platform_id)
    return save_auth_health_state({
        platform_id: {
            "status": "unknown",
            "checked_at": previous.get("checked_at") or "",
            "last_success_at": previous.get("last_success_at") or "",
            "next_check_at": "",
            "reason": "new_authorization",
            "failure_count": 0,
            "error_code": "",
        }
    }).get(platform_id) or {}


def _sync_auth_health_for_auth_state(
    platform_id: str,
    auth_status: str,
    reason: str = "",
    *,
    previous_status: str = "",
) -> dict:
    """Keep the health snapshot consistent with an explicit auth transition."""
    normalized_status = _sanitize_auth_status(auth_status)
    previous = _auth_health_snapshot(platform_id)
    if normalized_status == "authorized":
        if _sanitize_auth_status(previous_status) == "authorized":
            return previous
        return _mark_auth_health_pending(platform_id)

    checked_at = _auth_health_time_text()
    health_status = "expired" if normalized_status == "expired" else "needs_auth"
    normalized_reason = str(
        reason
        or ("expired_cookie" if health_status == "expired" else "not_authorized")
    ).strip().lower()
    return save_auth_health_state({
        platform_id: {
            "status": health_status,
            "checked_at": checked_at,
            "last_success_at": previous.get("last_success_at") or "",
            "next_check_at": "",
            "reason": normalized_reason,
            "failure_count": max(_safe_int(previous.get("failure_count")), 0),
            "error_code": normalized_reason,
        }
    }).get(platform_id) or {}


def _record_auth_health_result(
    platform_id: str,
    status: str,
    *,
    reason: str = "",
    error_code: str = "",
    now: float | None = None,
) -> dict:
    checked_ts = time.time() if now is None else float(now)
    checked_at = _auth_health_time_text(checked_ts)
    previous = _auth_health_snapshot(platform_id)
    normalized_status = str(status or "unknown").strip().lower()
    if normalized_status not in AUTH_HEALTH_STATUSES:
        normalized_status = "unknown"

    if normalized_status == "healthy":
        last_success_at = checked_at
        failure_count = 0
        next_delay = AUTH_HEALTH_INTERVAL_SECONDS
        normalized_reason = ""
        normalized_error = ""
    elif normalized_status in {"expired", "needs_auth"}:
        last_success_at = previous.get("last_success_at") or ""
        failure_count = max(_safe_int(previous.get("failure_count")), 0) + 1
        next_delay = AUTH_HEALTH_INTERVAL_SECONDS
        normalized_reason = str(reason or ("expired_cookie" if normalized_status == "expired" else "manual_reauth_required")).strip().lower()
        normalized_error = str(error_code or normalized_reason).strip().lower()
    elif normalized_status == "skipped":
        last_success_at = previous.get("last_success_at") or ""
        failure_count = max(_safe_int(previous.get("failure_count")), 0)
        next_delay = AUTH_HEALTH_BUSY_RETRY_SECONDS
        normalized_reason = str(reason or "busy").strip().lower()
        normalized_error = str(error_code or normalized_reason).strip().lower()
    else:
        last_success_at = previous.get("last_success_at") or ""
        failure_count = max(_safe_int(previous.get("failure_count")), 0) + 1
        next_delay = AUTH_HEALTH_FAILURE_RETRY_SECONDS
        normalized_reason = str(reason or "probe_unavailable").strip().lower()
        normalized_error = str(error_code or "probe_failed").strip().lower()

    item = {
        "status": normalized_status,
        "checked_at": checked_at,
        "last_success_at": last_success_at,
        "next_check_at": _auth_health_time_text(checked_ts + next_delay),
        "reason": normalized_reason,
        "failure_count": failure_count,
        "error_code": normalized_error,
    }
    saved = save_auth_health_state({platform_id: item}).get(platform_id) or item
    if normalized_status == "healthy":
        _set_platform_auth_state(platform_id, "authorized", "", reset_health=False)
    elif normalized_status == "expired":
        _set_platform_auth_state(platform_id, "expired", normalized_reason or "expired_cookie", reset_health=False)
    elif normalized_status == "needs_auth":
        _set_platform_auth_state(platform_id, "needs_auth", normalized_reason or "manual_reauth_required", reset_health=False)
    return saved


def _profile_browser_pids(profile_dir: str) -> list[int]:
    """Read-only owner detection; health probes never terminate profile processes."""
    if not profile_dir or os.name == "nt":
        return []
    try:
        output = subprocess.check_output(
            ["ps", "-axo", "pid=,command="],
            text=True,
            errors="replace",
            timeout=3,
        )
    except Exception:
        return []
    current_pid = os.getpid()
    result = []
    for line in output.splitlines():
        pid_text, separator, command = line.strip().partition(" ")
        if not separator:
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid != current_pid and _command_uses_profile(command, profile_dir):
            result.append(pid)
    return sorted(set(result))


def _cleanup_auth_health_probe_entries(*, keep_dir: str = "") -> None:
    """Remove complete per-probe workspaces, including unknown side effects."""
    root = os.path.realpath(AUTH_HEALTH_PROBE_DIR)
    kept = os.path.realpath(keep_dir) if keep_dir else ""
    try:
        entries = list(os.scandir(root))
    except (FileNotFoundError, OSError):
        return
    for entry in entries:
        target = os.path.realpath(entry.path)
        if kept and target == kept:
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                _remove_tree_if_exists(target, allowed_root=root)
            else:
                _remove_file_if_exists(target, allowed_root=root)
        except (FileNotFoundError, OSError, ValueError):
            continue


def _auth_health_failure_reason(platform_id: str, progress: dict, stdout: str, stderr: str) -> str:
    explicit_status = _sanitize_auth_status(progress.get("auth_status"))
    explicit_reason = str(progress.get("auth_reason") or "").strip().lower()
    if explicit_status == "expired" or explicit_reason == "expired_cookie":
        return "expired_cookie"
    if explicit_status in {"unauthorized", "needs_auth"} and explicit_reason:
        return explicit_reason

    text = "\n".join(
        str(part or "")
        for part in (progress.get("message"), progress.get("error"), stdout, stderr)
        if part
    ).lower()
    exact_login_markers = {
        "douyin": ("抖音未登录（headless=true", "抖音未登录(headless=true"),
        "xiaohongshu": ("小红书未登录（headless=true", "小红书未登录(headless=true"),
        "bilibili": ("b 站未登录（headless=true", "b站未登录（headless=true"),
        "kuaishou": ("快手未登录（headless=true", "快手未登录(headless=true"),
    }
    if any(marker in text for marker in exact_login_markers.get(platform_id, ())):
        return "expired_cookie"
    if any(marker in text for marker in ("cookie 过期", "cookie失效", "登录已失效", "登录超时")):
        return "expired_cookie"
    if any(marker in text for marker in ("重新扫码", "重新授权", "manual_reauth_required")):
        return "manual_reauth_required"
    return ""


def _run_auth_health_probe(platform_id: str) -> dict:
    script = (AUTH_SINGLE_PLATFORM_MAP.get(platform_id) or ("", ""))[0]
    if not script or not os.path.isfile(script):
        return {"status": "unknown", "reason": "script_missing", "error_code": "script_missing"}

    browser_channel = _configured_browser_channel()
    profile_dir = _resolve_user_data_dir(platform_id, browser_channel)
    if not os.path.isdir(profile_dir):
        return {"status": "needs_auth", "reason": "profile_missing", "error_code": "profile_missing"}
    if _profile_browser_pids(profile_dir):
        return {"status": "skipped", "reason": "profile_in_use", "error_code": "profile_in_use"}

    ensure_runtime_dirs()
    _cleanup_auth_health_probe_entries()
    probe_dir = tempfile.mkdtemp(prefix=f"{platform_id}-", dir=AUTH_HEALTH_PROBE_DIR)
    try:
        os.chmod(probe_dir, 0o700)
    except OSError:
        pass
    progress_path = os.path.join(probe_dir, "health-probe.json")
    query = {
        "auth_only": ["true"],
        "headless": ["true"],
        "scan_wait_ms": [str(AUTH_HEALTH_SCAN_WAIT_MS)],
        "browser_channel": [browser_channel],
    }
    try:
        env = _build_env(query, platform_id=platform_id, progress_path=progress_path, is_xhs=platform_id == "xiaohongshu")
        env["CLEAN_PROFILE_LOCKS"] = "false"
        env["DOWNLOAD_DIR"] = probe_dir
        proc = _run_script(
            [script],
            env,
            timeout=AUTH_HEALTH_PROBE_TIMEOUT_SECONDS,
            process_slot="auth_health",
        )
        stdout, stderr = _get_proc_output(proc)
        progress = read_json_file(progress_path, default_progress(platform_id))
        if proc.returncode == 0 and _auth_flow_has_explicit_success(progress):
            return {"status": "healthy", "reason": "", "error_code": ""}
        reason = _auth_health_failure_reason(platform_id, progress, stdout, stderr)
        if reason == "expired_cookie":
            return {"status": "expired", "reason": reason, "error_code": "login_required"}
        if reason:
            return {"status": "needs_auth", "reason": reason, "error_code": reason}
        lowered = f"{stdout}\n{stderr}".lower()
        error_code = "probe_timeout" if "子进程超时" in lowered or "超时(" in lowered else "probe_failed"
        return {"status": "unknown", "reason": "probe_unavailable", "error_code": error_code}
    except Exception:
        return {"status": "unknown", "reason": "probe_unavailable", "error_code": "probe_exception"}
    finally:
        try:
            _remove_tree_if_exists(probe_dir, allowed_root=AUTH_HEALTH_PROBE_DIR)
        except (FileNotFoundError, OSError, ValueError):
            pass


def _auth_health_platforms(*, force: bool = False, now: float | None = None) -> list[str]:
    current = time.time() if now is None else float(now)
    config = load_saved_config()
    enabled = _enabled_platform_scope(config)
    health = load_auth_health_state()
    auth = load_auth_state()
    candidates = []
    for platform_id in enabled:
        if _sanitize_auth_status((auth.get(platform_id) or {}).get("status")) != "authorized":
            continue
        browser_channel = _sanitize_browser_channel(config.get("browser_channel")) or DEFAULT_CONFIG["browser_channel"]
        if not _platform_has_profile(platform_id, browser_channel):
            candidates.append(platform_id)
            continue
        next_check = _auth_health_timestamp((health.get(platform_id) or {}).get("next_check_at"))
        if force or next_check <= current:
            candidates.append(platform_id)
    return candidates


def _run_auth_health_cycle(*, platform_id: str = "", force: bool = False, now: float | None = None) -> dict:
    global _AUTH_HEALTH_ACTIVE_PLATFORM
    candidates = _auth_health_platforms(force=force, now=now)
    selected = platform_id if platform_id in candidates else (candidates[0] if candidates else "")
    if not selected:
        return {"status": "no_due_platform", "platform": ""}

    browser_channel = _configured_browser_channel()
    if not _platform_has_profile(selected, browser_channel):
        health = _record_auth_health_result(selected, "needs_auth", reason="profile_missing", error_code="profile_missing", now=now)
        return {"status": health.get("status"), "platform": selected, "health": health}

    lease_token = None
    with RUN_MUTEX:
        if active_auth_locks():
            health = _record_auth_health_result(selected, "skipped", reason="auth_busy", error_code="auth_busy", now=now)
            return {"status": "skipped", "platform": selected, "health": health}
        lease_token = lock(kind="auth_health")
        if lease_token is None:
            health = _record_auth_health_result(selected, "skipped", reason="task_busy", error_code="task_busy", now=now)
            return {"status": "skipped", "platform": selected, "health": health}
        _AUTH_HEALTH_ACTIVE_PLATFORM = selected

    try:
        result = _run_auth_health_probe(selected)
        current_auth = _sanitize_auth_status((load_auth_state().get(selected) or {}).get("status"))
        if current_auth != "authorized":
            return {"status": "cancelled", "platform": selected, "health": _auth_health_snapshot(selected)}
        health = _record_auth_health_result(
            selected,
            result.get("status") or "unknown",
            reason=result.get("reason") or "",
            error_code=result.get("error_code") or "",
            now=now,
        )
        _append_log(
            f"AUTH_HEALTH_{selected.upper()}",
            f"platform={selected}\nstatus={health.get('status')}\nreason={health.get('reason')}\n",
            "",
        )
        return {"status": health.get("status"), "platform": selected, "health": health}
    finally:
        _AUTH_HEALTH_ACTIVE_PLATFORM = ""
        if lease_token is not None:
            unlock(lease_token.run_id)


def _run_auth_health_cycle_safely(*, platform_id: str = "", force: bool = False) -> dict:
    try:
        return _run_auth_health_cycle(platform_id=platform_id, force=force)
    except Exception as exc:
        try:
            _append_log(
                "AUTH_HEALTH_MONITOR_ERROR",
                f"platform={platform_id or 'scheduled'}\nstatus=unknown\n",
                f"error_type={type(exc).__name__}\n",
            )
        except Exception:
            pass
        return {"status": "unknown", "platform": platform_id, "error_code": "monitor_error"}


def _auth_health_monitor_loop(stop_event: threading.Event) -> None:
    if not AUTH_HEALTH_ENABLED:
        return
    if stop_event.wait(AUTH_HEALTH_STARTUP_DELAY_SECONDS):
        return

    try:
        startup_queue = _auth_health_platforms(force=True)
    except Exception as exc:
        startup_queue = []
        try:
            _append_log("AUTH_HEALTH_MONITOR_ERROR", "phase=startup\n", f"error_type={type(exc).__name__}\n")
        except Exception:
            pass
    for index, platform_id in enumerate(startup_queue):
        if stop_event.is_set():
            return
        _run_auth_health_cycle_safely(platform_id=platform_id, force=True)
        if index + 1 < len(startup_queue) and stop_event.wait(AUTH_HEALTH_STARTUP_SPACING_SECONDS):
            return

    while not stop_event.wait(AUTH_HEALTH_TICK_SECONDS):
        _run_auth_health_cycle_safely()


def _start_auth_health_monitor() -> threading.Thread | None:
    global _AUTH_HEALTH_THREAD
    if not AUTH_HEALTH_ENABLED:
        return None
    with AUTH_HEALTH_THREAD_LOCK:
        if _AUTH_HEALTH_THREAD is not None and _AUTH_HEALTH_THREAD.is_alive():
            return _AUTH_HEALTH_THREAD
        _cleanup_auth_health_probe_entries()
        _AUTH_HEALTH_STOP_EVENT.clear()
        _AUTH_HEALTH_THREAD = threading.Thread(
            target=_auth_health_monitor_loop,
            args=(_AUTH_HEALTH_STOP_EVENT,),
            name="auth_health_monitor",
            daemon=True,
        )
        _AUTH_HEALTH_THREAD.start()
        return _AUTH_HEALTH_THREAD


def _stop_auth_health_monitor(*, join_timeout: float = 2.0) -> None:
    global _AUTH_HEALTH_THREAD
    _AUTH_HEALTH_STOP_EVENT.set()
    _terminate_auth_health_process()
    with AUTH_HEALTH_THREAD_LOCK:
        thread = _AUTH_HEALTH_THREAD
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=max(float(join_timeout), 0.0))
    with AUTH_HEALTH_THREAD_LOCK:
        if _AUTH_HEALTH_THREAD is thread and (thread is None or not thread.is_alive()):
            _AUTH_HEALTH_THREAD = None


def _read_run_history() -> list[dict]:
    data = read_json_file(RUN_HISTORY_FILE, [])
    return data if isinstance(data, list) else []


def _write_run_history(items: list[dict]) -> None:
    _write_json_file_atomically(RUN_HISTORY_FILE, items)


def _platform_history_snapshot(platform_ids: list[str]) -> dict:
    records = []
    successful = 0
    failed = 0
    empty = 0
    for platform_id in platform_ids:
        progress = _load_platform_progress(platform_id, PLATFORM_PROGRESS_FILES.get(platform_id, ""))
        decorated = _decorate_progress(platform_id, progress, platform_ids)
        ui_status = decorated.get("ui_status", "idle")
        if ui_status == "completed":
            successful += 1
        elif ui_status == "completed_empty":
            empty += 1
        elif ui_status in {"failed", "auth_required"}:
            failed += 1
        records.append(
            {
                "platform": platform_id,
                "label": _platform_label(platform_id),
                "ui_status": ui_status,
                "message": decorated.get("message"),
                "last_sync_at": decorated.get("last_sync_at"),
                "total_works": int(decorated.get("totalWorks") or 0),
                "success_works": int(decorated.get("successWorks") or 0),
                "skipped_works": int(decorated.get("skippedWorks") or 0),
                "failed_works": int(decorated.get("failedWorks") or 0),
                "auth_status": decorated.get("auth_status"),
                "auth_reason": decorated.get("auth_reason"),
                "auth_action": decorated.get("auth_action"),
                "status": _normalize_platform_result_status(
                    ui_status,
                    str(decorated.get("auth_status") or ""),
                    bool(decorated.get("needs_auth")),
                ),
                "needs_auth": bool(decorated.get("needs_auth")),
            }
        )
    return {
        "platforms": records,
        "successful_platforms": successful,
        "failed_platforms": failed,
        "empty_platforms": empty,
    }


def _append_run_history_entry(entry: dict) -> dict:
    items = _read_run_history()
    items.insert(0, entry)
    _write_run_history(items[:100])
    return entry


def _latest_attempted_feishu_run(history_runs) -> dict:
    if isinstance(history_runs, dict):
        normalized = _normalize_run_history_entry(history_runs)
        return normalized if normalized and normalized.get("feishu", {}).get("attempted") else {}
    if not isinstance(history_runs, list):
        return {}
    for item in history_runs:
        normalized = _normalize_run_history_entry(item)
        if normalized and normalized.get("feishu", {}).get("attempted"):
            return normalized
    return {}


def _format_run_time(timestamp=None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp or time.time()))


def _resolve_run_mode(raw_mode: str, platform_count: int, requested_mode: str = "") -> str:
    requested = (requested_mode or "").strip().lower()
    if requested in {"incremental", "rerun", "single_platform", "feishu_only"}:
        return requested
    if raw_mode == "feishu_only":
        return "feishu_only"
    if raw_mode == "single_platform" or platform_count <= 1:
        return "single_platform"
    return "incremental"


def _normalize_platform_result_status(ui_status: str, auth_status: str = "", needs_auth: bool = False) -> str:
    if needs_auth or auth_status in {"unauthorized", "expired", "needs_auth"} or ui_status == "auth_required":
        return "needs_auth"
    if ui_status == "completed":
        return "success"
    if ui_status == "completed_empty":
        return "completed_empty"
    if ui_status == "skipped":
        return "skipped"
    if ui_status == "failed":
        return "failed"
    return "failed" if ui_status == "running" else "skipped"


def _friendly_feishu_error_message(error: str, *, action: str = "sync") -> str:
    raw = str(error or "").strip()
    if not raw:
        return ""

    lowered = raw.lower()
    if "app_token / app_id / app_secret" in lowered or "app token" in lowered and "app id" in lowered and "app secret" in lowered:
        return "飞书配置未完成，请补全 App Token、App ID 和 App Secret。"
    if _feishu_cli_requires_document_app_permission(raw):
        return "飞书文档应用没有这个多维表格的编辑权限：请在该多维表格右上角“分享/权限”里，把当前飞书文档应用或机器人加入协作者并给可编辑/可管理权限，然后重试同步。"
    if _feishu_cli_requires_user_auth(raw):
        return "飞书需要补充用户授权以更新表结构，请扫码后重试。"
    if "invalid param" in lowered or '"code": 10003' in lowered or "code=10003" in lowered:
        return "飞书配置有误，请检查 App Token、App ID、App Secret 和目标多维表格权限后重试。"
    if "生成飞书同步 payload 失败" in raw:
        return "本地数据已准备完成，但生成飞书同步数据失败，请稍后重试。"
    if "飞书连接测试失败" in raw or action == "test":
        return "飞书连接失败，请检查当前飞书配置后重试。"
    if "同步到飞书多维表格失败" in raw or "openapi" in lowered or '"code":' in lowered:
        return "飞书同步失败，请检查当前飞书配置后重试。"
    return "飞书同步失败，请稍后重试。" if action == "sync" else "飞书连接失败，请检查当前飞书配置后重试。"


def _collect_feishu_warnings(result) -> list[str]:
    warnings = []
    if not isinstance(result, dict):
        return warnings
    direct = result.get("warnings")
    if isinstance(direct, list):
        warnings.extend(str(item).strip() for item in direct if str(item).strip())
    sync = result.get("sync") if isinstance(result.get("sync"), dict) else {}
    sync_direct = sync.get("warnings")
    if isinstance(sync_direct, list):
        warnings.extend(str(item).strip() for item in sync_direct if str(item).strip())
    for item in sync.get("results") or []:
        if not isinstance(item, dict):
            continue
        for warning in item.get("warnings") or []:
            text = str(warning).strip()
            if text:
                warnings.append(text)
    deduped = []
    seen = set()
    for item in warnings:
        if item in seen:
            continue
        deduped.append(item)
        seen.add(item)
    return deduped


def _extract_feishu_bitable_meta(result) -> dict:
    if not isinstance(result, dict):
        return {"base_url": "", "base_name": "", "app_token": ""}

    prepare = result.get("prepare") if isinstance(result.get("prepare"), dict) else {}
    sync = result.get("sync") if isinstance(result.get("sync"), dict) else {}
    base_url = ""
    base_name = ""
    app_token = ""

    for block in (result, sync, prepare):
        if not isinstance(block, dict):
            continue
        if not base_url:
            base_url = str(block.get("base_url") or block.get("url") or "").strip()
        if not base_name:
            base_name = str(block.get("base_name") or block.get("name") or "").strip()
        if not app_token:
            app_token = str(block.get("app_token") or block.get("base_token") or block.get("token") or "").strip()
        created = block.get("created_bitable")
        if not isinstance(created, dict):
            continue
        if not base_url:
            base_url = str(created.get("base_url") or created.get("url") or "").strip()
        if not base_name:
            base_name = str(created.get("name") or "").strip()
        if not app_token:
            app_token = str(created.get("app_token") or created.get("base_token") or created.get("token") or "").strip()

    if not base_url and app_token:
        base_url = _feishu_bitable_url(app_token)
    return {
        "base_url": base_url,
        "base_name": base_name,
        "app_token": app_token,
    }


def _build_feishu_result(*, attempted: bool, ok: bool, result=None, error: str = "") -> dict:
    result = result or {}
    prepare = result.get("prepare") if isinstance(result.get("prepare"), dict) else {}
    sync = result.get("sync") if isinstance(result.get("sync"), dict) else {}
    bitable_meta = _extract_feishu_bitable_meta(result)
    message = str(result.get("message") or "").strip()
    status = "not_attempted"
    if attempted:
        status = "success" if ok else "failed"

    summary_parts = []
    if prepare.get("detail_count") is not None:
        summary_parts.append(f"明细 {int(prepare.get('detail_count') or 0)}")
    if prepare.get("work_count") is not None:
        summary_parts.append(f"作品 {int(prepare.get('work_count') or 0)}")
    if sync.get("table_count") is not None:
        summary_parts.append(f"写入表 {int(sync.get('table_count') or 0)}")
    if sync.get("record_count") is not None:
        summary_parts.append(f"记录 {int(sync.get('record_count') or 0)}")
    warnings = _collect_feishu_warnings(result)
    if warnings:
        summary_parts.append(f"警告 {len(warnings)} 条")

    raw_error = str(error or "").strip()
    summary = message or "，".join(summary_parts)
    if warnings:
        warning_text = "；".join(warnings)
        summary = f"{summary}；{warning_text}" if summary else warning_text
    return {
        "attempted": attempted,
        "ok": ok if attempted else False,
        "status": status,
        "error": _friendly_feishu_error_message(raw_error),
        "detail": raw_error,
        "summary": summary,
        "prepare": prepare,
        "sync": sync,
        "warnings": warnings,
        "base_url": str(bitable_meta.get("base_url") or "").strip(),
        "base_name": str(bitable_meta.get("base_name") or "").strip(),
    }


def _finalize_run_status(platform_results: list[dict], feishu_result: dict, merge_ok: bool) -> tuple[str, str]:
    success_count = sum(1 for item in platform_results if item.get("status") == "success")
    empty_count = sum(1 for item in platform_results if item.get("status") == "completed_empty")
    skipped_count = sum(1 for item in platform_results if item.get("status") == "skipped")
    failed_count = sum(1 for item in platform_results if item.get("status") == "failed")
    auth_count = sum(1 for item in platform_results if item.get("status") == "needs_auth")
    completed_count = success_count + empty_count + skipped_count

    if not merge_ok:
        return ("failed" if completed_count == 0 else "partial_failed", "platform_scraping")
    if feishu_result.get("status") == "failed":
        return ("failed" if completed_count == 0 and failed_count == 0 and auth_count == 0 else "partial_failed", "feishu_importing")
    if failed_count or auth_count:
        return ("failed" if completed_count == 0 else "partial_failed", "platform_scraping")
    if platform_results and empty_count == len(platform_results):
        return ("completed_empty", "")
    return ("completed", "")


def _normalize_history_platform_result(item: dict) -> dict:
    platform = str(item.get("platform") or "")
    raw_label = str(item.get("label") or "").strip()
    label = raw_label
    if not label or label == platform or raw_label in PLATFORM_LABELS:
        label = _platform_label(platform or raw_label)
    ui_status = str(item.get("ui_status") or item.get("status") or "")
    auth_status = str(item.get("auth_status") or "").strip().lower()
    if auth_status not in {"authorized", "unauthorized", "expired", "needs_auth"}:
        auth_status = "needs_auth" if bool(item.get("needs_auth")) else ("authorized" if ui_status in {"running", "completed", "completed_empty", "failed"} else "unauthorized")
    auth_reason = str(item.get("auth_reason") or "").strip().lower()
    auth_action = str(item.get("auth_action") or _auth_action(auth_status))
    needs_auth = auth_status in {"unauthorized", "expired", "needs_auth"} or bool(item.get("needs_auth"))
    status = str(item.get("status") or _normalize_platform_result_status(ui_status, auth_status, needs_auth))
    success_count = _safe_int(item.get("success_count") if item.get("success_count") is not None else item.get("success_works"))
    skip_count = _safe_int(item.get("skip_count") if item.get("skip_count") is not None else item.get("skipped_works"))
    fail_count = _safe_int(item.get("fail_count") if item.get("fail_count") is not None else item.get("failed_works"))
    total_count = _safe_int(item.get("total_count") if item.get("total_count") is not None else item.get("total_works"))
    message = item.get("message") or ""
    error = item.get("error") or ((message or "") if status in {"failed", "needs_auth"} else "")
    if (ui_status or status) == "completed":
        message = _normalized_completed_message(platform, str(message), success_count)
    return {
        "platform": platform,
        "label": label,
        "status": status,
        "ui_status": ui_status or status,
        "message": message,
        "error": error,
        "last_sync_at": item.get("last_sync_at"),
        "success_count": success_count,
        "skip_count": skip_count,
        "fail_count": fail_count,
        "total_count": total_count,
        "auth_status": auth_status,
        "auth_reason": auth_reason,
        "auth_action": auth_action,
        "needs_auth": needs_auth,
    }


def _build_run_history_entry(
    *,
    raw_mode: str,
    requested_mode: str,
    min_date: str,
    started_at: str,
    ended_at: str,
    duration: float,
    merge_ok: bool,
    platform_snapshot: dict,
    feishu_attempted: bool,
    feishu_ok: bool,
    feishu_result=None,
    feishu_error: str = "",
    max_date: str = "",
) -> dict:
    platform_results = [_normalize_history_platform_result(item) for item in platform_snapshot.get("platforms", [])]
    run_mode = _resolve_run_mode(raw_mode, len(platform_results), requested_mode)
    feishu = _build_feishu_result(attempted=feishu_attempted, ok=feishu_ok, result=feishu_result, error=feishu_error)
    status, failed_stage = _finalize_run_status(platform_results, feishu, merge_ok)
    successful_platforms = sum(1 for item in platform_results if item["status"] == "success")
    empty_platforms = sum(1 for item in platform_results if item["status"] == "completed_empty")
    failed_platforms = sum(1 for item in platform_results if item["status"] in {"failed", "needs_auth"})
    skipped_platforms = sum(1 for item in platform_results if item["status"] == "skipped")
    needs_auth_platforms = sum(1 for item in platform_results if item["status"] == "needs_auth")
    ok = status in {"completed", "completed_empty"}
    return {
        "run_id": int(time.time() * 1000),
        "run_at": ended_at,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration": duration,
        "mode": run_mode,
        "raw_mode": raw_mode,
        "min_date": min_date,
        "max_date": max_date,
        "platforms": [item["platform"] for item in platform_results],
        "platform_count": len(platform_results),
        "platform_results": platform_results,
        "successful_platforms": successful_platforms,
        "failed_platforms": failed_platforms,
        "empty_platforms": empty_platforms,
        "skipped_platforms": skipped_platforms,
        "needs_auth_platforms": needs_auth_platforms,
        "merge_ok": merge_ok,
        "status": status,
        "run_stage_status": "completed" if status in {"completed", "completed_empty"} else status,
        "failed_stage": failed_stage,
        "feishu": feishu,
        "feishu_sync_attempted": feishu_attempted,
        "feishu_sync_ok": feishu_ok,
        "ok": ok,
    }


def _append_single_platform_exception_history(
    platform: str,
    query: dict,
    *,
    started_at: str,
    start: float,
    error: str,
) -> dict:
    error_text = str(error or "unexpected_single_platform_failure").strip()
    snapshot = _platform_history_snapshot([platform])
    platform_items = [
        dict(item)
        for item in snapshot.get("platforms", [])
        if isinstance(item, dict) and item.get("platform") == platform
    ]
    failed_item = platform_items[0] if platform_items else {"platform": platform}
    failed_item.update(
        {
            "label": _platform_label(platform),
            "status": "failed",
            "ui_status": "failed",
            "message": error_text,
            "error": error_text,
            "auth_status": "authorized",
            "auth_reason": "",
            "auth_action": "",
            "needs_auth": False,
        }
    )
    entry = _build_run_history_entry(
        raw_mode="single_platform",
        requested_mode="single_platform",
        min_date=query.get("min_date", [""])[0],
        max_date=query.get("max_date", [""])[0],
        started_at=started_at,
        ended_at=_format_run_time(),
        duration=round(time.time() - start, 2),
        merge_ok=False,
        platform_snapshot={"platforms": [failed_item]},
        feishu_attempted=False,
        feishu_ok=False,
    )
    entry["error"] = error_text
    _append_run_history_entry(entry)
    return entry


def _normalize_run_history_entry(entry: dict) -> dict:
    if not isinstance(entry, dict):
        return {}
    platform_results = [_normalize_history_platform_result(item) for item in entry.get("platform_results", []) if isinstance(item, dict)]
    platform_count = int(entry.get("platform_count") or len(platform_results))
    mode = _resolve_run_mode(str(entry.get("raw_mode") or entry.get("mode") or ""), platform_count, str(entry.get("mode") or ""))
    raw_feishu = entry.get("feishu") if isinstance(entry.get("feishu"), dict) else {}
    feishu_attempted = bool(raw_feishu.get("attempted")) if "attempted" in raw_feishu else bool(entry.get("feishu_sync_attempted"))
    feishu_ok = bool(raw_feishu.get("ok")) if "ok" in raw_feishu else bool(entry.get("feishu_sync_ok"))
    raw_feishu_status = str(raw_feishu.get("status") or "").strip()
    if raw_feishu_status == "success":
        feishu_attempted = True
        feishu_ok = True
    elif raw_feishu_status == "failed":
        feishu_attempted = True
        feishu_ok = False
    elif raw_feishu_status == "not_attempted":
        feishu_attempted = False
        feishu_ok = False
    feishu = _build_feishu_result(
        attempted=feishu_attempted,
        ok=feishu_ok,
        result={
            "prepare": raw_feishu.get("prepare") if isinstance(raw_feishu.get("prepare"), dict) else {},
            "sync": raw_feishu.get("sync") if isinstance(raw_feishu.get("sync"), dict) else {},
            "base_url": raw_feishu.get("base_url"),
            "base_name": raw_feishu.get("base_name"),
        },
        error=str(raw_feishu.get("detail") or raw_feishu.get("error") or entry.get("feishu_sync_error") or ""),
    )
    if raw_feishu.get("summary"):
        feishu["summary"] = str(raw_feishu.get("summary") or "")
    status = str(entry.get("status") or "")
    failed_stage = str(entry.get("failed_stage") or "")
    computed_status, computed_failed_stage = _finalize_run_status(platform_results, feishu, bool(entry.get("merge_ok", True)))
    normalized_failed_platforms = sum(1 for item in platform_results if item["status"] in {"failed", "needs_auth"})
    normalized_successful_platforms = sum(1 for item in platform_results if item["status"] == "success")
    normalized_empty_platforms = sum(1 for item in platform_results if item["status"] == "completed_empty")
    normalized_skipped_platforms = sum(1 for item in platform_results if item["status"] == "skipped")
    normalized_needs_auth_platforms = sum(1 for item in platform_results if item["status"] == "needs_auth")
    recompute_stale_platform_failure = (
        status in {"failed", "partial_failed"}
        and failed_stage in {"", "platform_scraping"}
        and normalized_failed_platforms == 0
    )
    if not status or recompute_stale_platform_failure:
        status, failed_stage = computed_status, computed_failed_stage
    run_stage_status = entry.get("run_stage_status") or ("completed" if status in {"completed", "completed_empty"} else status)
    if recompute_stale_platform_failure:
        run_stage_status = "completed" if status in {"completed", "completed_empty"} else status
    return {
        **entry,
        "run_id": int(entry.get("run_id") or 0),
        "run_at": entry.get("run_at") or entry.get("ended_at") or "",
        "started_at": entry.get("started_at") or entry.get("run_at") or "",
        "ended_at": entry.get("ended_at") or entry.get("run_at") or "",
        "duration": float(entry.get("duration") or 0),
        "mode": mode,
        "platform_count": platform_count,
        "platforms": entry.get("platforms") or [item.get("platform") for item in platform_results if item.get("platform")],
        "platform_results": platform_results,
        "successful_platforms": normalized_successful_platforms,
        "failed_platforms": normalized_failed_platforms,
        "empty_platforms": normalized_empty_platforms,
        "skipped_platforms": normalized_skipped_platforms,
        "needs_auth_platforms": normalized_needs_auth_platforms,
        "status": status,
        "run_stage_status": run_stage_status,
        "failed_stage": failed_stage,
        "feishu": feishu,
        "feishu_sync_attempted": bool(entry.get("feishu_sync_attempted") or feishu.get("attempted")),
        "feishu_sync_ok": (bool(entry.get("feishu_sync_ok")) or feishu.get("status") == "success") if feishu.get("attempted") else False,
        "ok": bool(entry.get("ok")) if "ok" in entry else status in {"completed", "completed_empty"},
    }


def _update_history_entry_after_manual_feishu_sync(
    entry: dict,
    *,
    ok: bool,
    result=None,
    error: str = "",
    synced_at: str = "",
    duration: float | None = None,
) -> dict:
    normalized = _normalize_run_history_entry(entry)
    if not normalized:
        return {}

    attempted = not (isinstance(result, dict) and result.get("attempted") is False)
    feishu = _build_feishu_result(attempted=attempted, ok=ok, result=result, error=error)
    status, failed_stage = _finalize_run_status(
        normalized.get("platform_results") or [],
        feishu,
        bool(normalized.get("merge_ok", True)),
    )
    ended_at = synced_at or str(normalized.get("ended_at") or normalized.get("run_at") or _format_run_time())
    run_at = synced_at or str(normalized.get("run_at") or ended_at)
    updated = {
        **normalized,
        "run_at": run_at,
        "ended_at": ended_at,
        "duration": float(duration if duration is not None else normalized.get("duration") or 0),
        "status": status,
        "run_stage_status": "completed" if status in {"completed", "completed_empty"} else status,
        "failed_stage": failed_stage,
        "feishu": feishu,
        "feishu_sync_attempted": attempted,
        "feishu_sync_ok": ok if attempted else False,
        "ok": status in {"completed", "completed_empty"},
    }
    return updated


def _reconcile_feishu_history_after_manual_sync(
    items: list[dict],
    *,
    min_date: str,
    max_date: str = "",
    ok: bool,
    result=None,
    error: str = "",
    synced_at: str = "",
    duration: float | None = None,
    config: dict | None = None,
) -> tuple[list[dict], dict]:
    normalized_items = [_normalize_run_history_entry(item) for item in items if isinstance(item, dict)]
    attempted = not (isinstance(result, dict) and result.get("attempted") is False)
    target_index = None
    for index, item in enumerate(normalized_items):
        if not item:
            continue
        if min_date and item.get("min_date") and str(item.get("min_date")) != str(min_date):
            continue
        feishu = item.get("feishu") or {}
        if str(item.get("failed_stage") or "") == "feishu_importing" or str(feishu.get("status") or "") == "failed":
            target_index = index
            break

    if target_index is not None:
        updated_entry = _update_history_entry_after_manual_feishu_sync(
            normalized_items[target_index],
            ok=ok,
            result=result,
            error=error,
            synced_at=synced_at,
            duration=duration,
        )
        remaining = [item for idx, item in enumerate(normalized_items) if idx != target_index]
        return [updated_entry, *remaining][:100], updated_entry

    enabled_platforms = []
    if isinstance(config, dict):
        enabled_platforms = [item for item in (config.get("enabled_platforms") or []) if isinstance(item, str)]
    actual_platforms = _feishu_prepare_platforms(result) or enabled_platforms
    history_entry = _build_run_history_entry(
        raw_mode="feishu_only",
        requested_mode="feishu_only",
        min_date=min_date or str((config or {}).get("min_publish_date") or ""),
        max_date=max_date,
        started_at=synced_at or _format_run_time(),
        ended_at=synced_at or _format_run_time(),
        duration=float(duration or 0),
        merge_ok=True,
        platform_snapshot={"platforms": []},
        feishu_attempted=attempted,
        feishu_ok=ok if attempted else False,
        feishu_result=result,
        feishu_error=error,
    )
    history_entry["platforms"] = [item for item in actual_platforms if isinstance(item, str) and item]
    history_entry["platform_count"] = len(history_entry["platforms"])
    return [history_entry, *normalized_items][:100], history_entry


def _build_feishu_runtime_summary(
    config_summary: dict,
    history_runs,
    *,
    current_stage: str,
    enabled_platforms: list[str],
) -> dict:
    enabled = bool((config_summary or {}).get("feishu_enabled"))
    ready = bool((config_summary or {}).get("feishu_ready"))
    auto_sync_enabled = bool((config_summary or {}).get("auto_sync_enabled"))
    latest = {}
    if isinstance(history_runs, list):
        latest = _normalize_run_history_entry(history_runs[0]) if history_runs else {}
    elif isinstance(history_runs, dict):
        latest = _normalize_run_history_entry(history_runs)
    latest_feishu_any = latest.get("feishu") if isinstance(latest.get("feishu"), dict) else {}
    latest_feishu_run = _latest_attempted_feishu_run(history_runs)
    latest_feishu = latest_feishu_run.get("feishu") if isinstance(latest_feishu_run.get("feishu"), dict) else {}
    latest_platforms = [item for item in (latest.get("platforms") or []) if isinstance(item, str) and item]
    last_feishu_platforms = _feishu_prepare_platforms(latest_feishu) or [
        item for item in (latest_feishu_run.get("platforms") or []) if isinstance(item, str) and item
    ]
    fallback_platforms = [item for item in enabled_platforms if isinstance(item, str) and item]
    current_platforms = latest_platforms or fallback_platforms
    current_platform_labels = [_platform_label(item) for item in current_platforms]
    last_platform_labels = [_platform_label(item) for item in last_feishu_platforms]
    last_sync_at = str(latest_feishu_run.get("run_at") or "") if latest_feishu.get("attempted") else ""
    last_summary = str(latest_feishu.get("summary") or "").strip()
    last_message = str(latest_feishu.get("error") or "").strip()
    cli_state = _copy_lark_cli_state()
    cli_phase = str(cli_state.get("phase") or "").strip()
    verification_url = str(cli_state.get("verification_url") or "").strip()
    status = "idle"
    message = "还没有执行过飞书同步。"

    if not enabled:
        status = "disabled"
        message = "飞书同步未启用。"
    elif not ready:
        status = "needs_config"
        message = "飞书配置待补全。"
    elif cli_phase in {"connecting", "scan_qr"} and str(cli_state.get("auth_mode") or "") == "user":
        status = "needs_auth"
        message = str(cli_state.get("message") or "飞书需要重新授权。").strip()
    elif current_stage == "importing":
        status = "running"
        platform_text = "、".join(current_platform_labels) if current_platform_labels else "已启用平台"
        message = f"正在把 {platform_text} 的最新本地结果同步到飞书。"
    elif latest_feishu_any.get("status") == "not_attempted" and str(latest_feishu_any.get("summary") or "").strip():
        status = "idle"
        message = str(latest_feishu_any.get("summary") or "").strip()
    elif latest_feishu.get("status") == "success":
        status = "success"
        platform_text = "、".join(last_platform_labels) if last_platform_labels else "已启用平台"
        time_text = last_sync_at or "最近一次"
        summary_text = f"（{last_summary}）" if last_summary else ""
        message = f"最近已同步 {platform_text} · {time_text}{summary_text}"
    elif latest_feishu.get("status") == "failed" and _feishu_cli_requires_document_app_permission(
        "\n".join(str(latest_feishu.get(key) or "") for key in ("error", "detail", "summary"))
    ):
        status = "failed"
        message = last_message or "飞书文档应用没有该多维表格的编辑权限，请给文档应用/机器人开通协作者权限后重试。"
    elif latest_feishu.get("status") == "failed" and _feishu_cli_requires_user_auth(
        "\n".join(str(latest_feishu.get(key) or "") for key in ("error", "detail", "summary"))
    ):
        status = "needs_auth"
        message = last_message or "飞书需要补充用户授权，请扫码完成后继续同步。"
    elif latest_feishu.get("status") == "failed":
        status = "failed"
        message = last_message or "最近一次飞书同步失败，请检查配置后重试。"

    return {
        "enabled": enabled,
        "ready": ready,
        "auto_sync_enabled": auto_sync_enabled,
        "status": status,
        "message": message,
        "current_platforms": current_platforms,
        "current_platform_labels": current_platform_labels,
        "last_sync_at": last_sync_at,
        "last_platforms": last_feishu_platforms,
        "last_platform_labels": last_platform_labels,
        "last_summary": last_summary,
        "last_error": last_message,
        "verification_url": verification_url,
        "auth_mode": str(cli_state.get("auth_mode") or "").strip(),
        "auth_phase": cli_phase,
    }


def _manual_feishu_sync_preflight(config: dict) -> dict:
    if not bool((config or {}).get("feishu_enabled")):
        return {
            "blocked": True,
            "http_status": 409,
            "error": "feishu_disabled",
            "message": "请先在设置里启用飞书同步，再执行“仅同步飞书”。",
        }
    if not feishu_config_ready(config or {}):
        return {
            "blocked": True,
            "http_status": 409,
            "error": "feishu_needs_config",
            "message": "请先补全飞书配置后，再执行“仅同步飞书”。",
        }
    return {"blocked": False}


def _run_failure_message(history_entry: dict) -> str:
    platform_results = history_entry.get("platform_results") or []
    for item in platform_results:
        status = str(item.get("status") or "")
        if status not in {"failed", "needs_auth"}:
            continue
        if status == "needs_auth":
            auth_message = _auth_required_message(
                str(item.get("platform") or ""),
                str(item.get("auth_status") or ""),
                str(item.get("auth_reason") or ""),
            )
            if auth_message:
                return auth_message
        message = str(item.get("error") or item.get("message") or "").strip()
        if message:
            return message
        label = str(item.get("label") or item.get("platform") or "平台")
        return f"{label} 同步失败"

    feishu = history_entry.get("feishu") or {}
    feishu_error = str(feishu.get("error") or "").strip()
    if feishu_error:
        return feishu_error

    failed_stage = str(history_entry.get("failed_stage") or "").strip()
    if failed_stage == "merge_all_channels":
        return "多平台结果合并失败"
    if failed_stage == "feishu_sync":
        return "飞书同步失败"
    return "同步失败，请查看运行记录"


def _run_failed_only_for_auth(history_entry: dict) -> bool:
    platform_results = history_entry.get("platform_results") or []
    if not platform_results:
        return False
    return all(str(item.get("status") or "") == "needs_auth" for item in platform_results)


def _run_response_meta(history_entry: dict, default_error: str) -> tuple[int, bool, str]:
    auth_only_failure = _run_failed_only_for_auth(history_entry)
    if auth_only_failure:
        return 409, False, "auth_required"
    return 200, True, default_error


def _history_from_analytics_run(run_meta) -> dict:
    return _normalize_run_history_entry(
        {
            "run_id": int(run_meta.run_id),
            "run_at": run_meta.run_at,
            "started_at": run_meta.run_at,
            "ended_at": run_meta.run_at,
            "duration": 0,
            "mode": "incremental",
            "raw_mode": "run_all",
            "platform_count": 0,
            "platform_results": [],
            "merge_ok": True,
            "feishu_sync_attempted": False,
            "feishu_sync_ok": False,
            "status": "completed",
            "run_stage_status": "completed",
            "ok": True,
        }
    )


def _resolve_npx_bin() -> str:
    """Find npx binary, similar to resolve_default_node_bin."""
    base_path = os.environ.get("PATH", "")
    if os.name != "nt":
        base_path = f"{base_path}:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
    names = ["npx.cmd", "npx"] if os.name == "nt" else ["npx"]
    for name in names:
        found = shutil.which(name, path=base_path)
        if found:
            return found
    return ""


def _resolve_lark_cli_bin() -> str:
    names = ["lark-cli.exe", "lark-cli.cmd", "lark-cli"] if os.name == "nt" else ["lark-cli"]

    ext = ".exe" if os.name == "nt" else ""
    patterns = [
        os.path.join(BASE_DIR, "node_modules", "@larksuite", "cli", "bin", f"lark-cli{ext}"),
        os.path.join(BASE_DIR, "node_modules", ".bin", f"lark-cli{ext}"),
    ]
    for pattern in patterns:
        matches = _sort_existing_paths(glob.glob(pattern))
        if matches:
            return matches[0]
    for name in names:
        found = shutil.which(name, path=os.path.join(BASE_DIR, "node_modules", ".bin"))
        if found:
            return found
    return ""


def _resolve_lark_cli_prefix() -> list[str]:
    cli_bin = _resolve_lark_cli_bin()
    if cli_bin:
        return [cli_bin]
    npx = _resolve_npx_bin()
    if npx:
        return [npx, "--yes", LARK_CLI_NPX_PACKAGE]
    raise RuntimeError("未找到 lark-cli 或 npx 命令，请确保 Node.js 已安装。")


PROJECT_LARK_CLI_CONFIG_FILE = os.path.join(LARK_CLI_HOME, ".lark-cli", "config.json")
GLOBAL_LARK_CLI_CONFIG_FILE = os.path.join(GLOBAL_LARK_CLI_STATE_DIR, "config.json")


def _saved_feishu_cli_use_global_home(config: dict | None = None) -> bool:
    if isinstance(config, dict) and "feishu_cli_use_global_home" in config:
        return _to_bool(config.get("feishu_cli_use_global_home"))
    payload = _load_json_dict(CONFIG_FILE)
    return _to_bool(payload.get("feishu_cli_use_global_home"))


def _active_lark_cli_home(use_global: bool | None = None) -> str:
    if use_global is None:
        use_global = _saved_feishu_cli_use_global_home()
    return os.path.expanduser("~") if use_global else LARK_CLI_HOME


def _active_lark_cli_config_file(use_global: bool | None = None) -> str:
    if use_global is None:
        use_global = _saved_feishu_cli_use_global_home()
    return GLOBAL_LARK_CLI_CONFIG_FILE if use_global else PROJECT_LARK_CLI_CONFIG_FILE


def _lark_cli_env(use_global: bool | None = None) -> dict:
    """Build environment dict suitable for running lark-cli commands."""
    env = _scrub_supervision_env(os.environ.copy())
    base_path = env.get("PATH", "")
    if os.name != "nt":
        default_path = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["PATH"] = f"{base_path}:{default_path}" if base_path else default_path
    active_home = _active_lark_cli_home(use_global=use_global)
    if active_home:
        if use_global:
            env["HOME"] = active_home
            env["USERPROFILE"] = active_home
        else:
            os.makedirs(active_home, exist_ok=True)
            env["HOME"] = active_home
            env["USERPROFILE"] = active_home
    # lark-cli still honors standard proxy env vars even with LARK_CLI_NO_PROXY.
    # Auth polling is fragile through local proxies, so force direct Feishu access.
    for proxy_key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(proxy_key, None)
    env["LARK_CLI_NO_PROXY"] = "1"
    return env


def _run_lark_cli_raw(args: list[str], timeout: int = 120, use_global: bool | None = None) -> tuple[str, str, int]:
    """Run a lark-cli command. Returns (stdout, stderr, returncode)."""
    os.makedirs(LARK_CLI_DIR, exist_ok=True)
    cmd = _resolve_lark_cli_prefix() + args
    env = _lark_cli_env(use_global=use_global)

    # lark-cli 输出 UTF-8 JSON（含中文账号名）；Windows 下 text=True 默认
    # GBK 解码会在特定字节序列上崩溃（reader 线程 UnicodeDecodeError，
    # 调用挂起/误报缺权限），必须显式 UTF-8。
    proc = subprocess.run(
        cmd,
        cwd=LARK_CLI_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return (proc.stdout or "").strip(), (proc.stderr or "").strip(), proc.returncode


def _run_lark_cli(args: list[str], timeout: int = 120, use_global: bool | None = None) -> dict:
    """Run a lark-cli command and return parsed JSON output (for commands that support --json)."""
    stdout, stderr, rc = _run_lark_cli_raw(args, timeout, use_global=use_global)
    if rc != 0:
        raise RuntimeError(f"lark-cli 命令失败 (exit {rc}): {stderr or stdout}")
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return {"_raw": stdout}


def _lark_cli_is_configured(use_global: bool | None = None) -> bool:
    """Check if lark-cli has been configured (config.json exists with appId)."""
    try:
        config_file = _active_lark_cli_config_file(use_global=use_global)
        if os.path.exists(config_file):
            with open(config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            # appId can be at root (config show format) or nested in apps[] (raw config)
            if data.get("appId") or data.get("app_id"):
                return True
            apps = data.get("apps", [])
            if apps and isinstance(apps, list) and apps[0].get("appId"):
                return True
    except Exception:
        pass
    return False


def _read_lark_cli_config(use_global: bool | None = None) -> dict:
    """Read credentials from ~/.lark-cli/config.json."""
    try:
        config_file = _active_lark_cli_config_file(use_global=use_global)
        if os.path.exists(config_file):
            with open(config_file, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _parse_lark_cli_status_payload(output: str) -> dict:
    text = str(output or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {}


def _invalidate_lark_cli_caches() -> None:
    _LARK_CLI_EFFECTIVE_CACHE.update({"key": None, "ts": 0.0, "value": {}})
    with _LARK_CLI_STATUS_CACHE_LOCK:
        _LARK_CLI_STATUS_CACHE.clear()


def _lark_cli_status(
    use_global: bool | None = None,
    timeout: int = 20,
    *,
    force_refresh: bool = False,
) -> dict:
    effective_global = _saved_feishu_cli_use_global_home() if use_global is None else bool(use_global)
    cache_key = (effective_global, _active_lark_cli_config_file(use_global=effective_global))
    now = time.time()
    if not force_refresh:
        with _LARK_CLI_STATUS_CACHE_LOCK:
            cached = _LARK_CLI_STATUS_CACHE.get(cache_key)
            if cached and now - float(cached.get("ts") or 0) < LARK_CLI_STATUS_CACHE_TTL_SECONDS:
                return dict(cached.get("value") or {})
    # lark-cli 冷启动（版本检查网络请求、杀软扫描）偶发超时或输出异常，
    # 返回空会被上层误判为“未登录/缺权限”——失败时空结果重试一次。
    for attempt in range(2):
        try:
            stdout, stderr, rc = _run_lark_cli_raw(["auth", "status"], timeout=timeout, use_global=effective_global)
        except Exception:
            if attempt == 0:
                time.sleep(1.5)
                continue
            return {}
        for payload_text in (stdout, stderr):
            payload = _parse_lark_cli_status_payload(payload_text)
            if payload and (rc == 0 or isinstance(payload.get("identities"), dict)):
                with _LARK_CLI_STATUS_CACHE_LOCK:
                    _LARK_CLI_STATUS_CACHE[cache_key] = {"ts": time.time(), "value": dict(payload)}
                return dict(payload)
        if attempt == 0:
            time.sleep(1.5)
            continue
    return {}


def _normalize_lark_cli_scope_values(value) -> set[str]:
    scopes: set[str] = set()
    if isinstance(value, str):
        raw_items = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple, set)):
        raw_items = []
        for item in value:
            raw_items.extend(str(item or "").replace(",", " ").split())
    else:
        raw_items = []
    for item in raw_items:
        cleaned = str(item or "").strip()
        if cleaned:
            scopes.add(cleaned)
    return scopes


def _lark_cli_user_scope_state(
    *,
    required_scopes: list[str] | None = None,
    use_global: bool | None = None,
    timeout: int = 20,
    force_refresh: bool = False,
) -> dict:
    status = _lark_cli_status(use_global=use_global, timeout=timeout, force_refresh=force_refresh)
    identities = status.get("identities") if isinstance(status, dict) else {}
    user = identities.get("user") if isinstance(identities, dict) else {}
    user_available = bool(user.get("available")) if isinstance(user, dict) else False
    scopes = set()
    scopes.update(_normalize_lark_cli_scope_values(status.get("scope") if isinstance(status, dict) else ""))
    scopes.update(_normalize_lark_cli_scope_values(user.get("scope") if isinstance(user, dict) else ""))
    required = set(str(scope or "").strip() for scope in (required_scopes or []) if str(scope or "").strip())
    missing = sorted(scope for scope in required if scope not in scopes)
    return {
        "available": user_available,
        "scopes": sorted(scopes),
        "required_scopes": sorted(required),
        "missing_scopes": missing,
        "has_required_scopes": user_available and not missing,
        "user_name": str((user or {}).get("userName") or status.get("userName") or "").strip() if isinstance(status, dict) else "",
        "open_id": str((user or {}).get("openId") or status.get("userOpenId") or "").strip() if isinstance(status, dict) else "",
    }


def _lark_cli_missing_required_user_scopes(
    *,
    required_scopes: list[str] | None = None,
    use_global: bool | None = None,
    timeout: int = 20,
) -> list[str]:
    required = FEISHU_USER_BASE_SCOPES if required_scopes is None else required_scopes
    state = _lark_cli_user_scope_state(required_scopes=required, use_global=use_global, timeout=timeout)
    return list(state.get("missing_scopes") or [])


def _lark_cli_has_required_user_auth(
    *,
    required_scopes: list[str] | None = None,
    use_global: bool | None = None,
    timeout: int = 20,
) -> bool:
    required = FEISHU_USER_BASE_SCOPES if required_scopes is None else required_scopes
    state = _lark_cli_user_scope_state(required_scopes=required, use_global=use_global, timeout=timeout)
    return bool(state.get("has_required_scopes"))


def _required_user_scope_message(missing_scopes: list[str] | None = None) -> str:
    missing = [str(scope or "").strip() for scope in (missing_scopes or []) if str(scope or "").strip()]
    if missing:
        return f"飞书当前用户授权缺少必要权限：{', '.join(missing)}。请重新打开飞书用户授权页并同意授权后再同步。"
    return "飞书当前用户授权不完整，请重新打开飞书用户授权页并同意授权后再同步。"


def _lark_cli_bot_ready(use_global: bool | None = None) -> bool:
    status = _lark_cli_status(use_global=use_global)
    identities = status.get("identities") if isinstance(status, dict) else {}
    bot = identities.get("bot") if isinstance(identities, dict) else {}
    return bool(bot.get("available"))


def _lark_cli_has_user_auth(cli_config: dict | None = None, use_global: bool | None = None) -> bool:
    status = _lark_cli_status(use_global=use_global)
    identities = status.get("identities") if isinstance(status, dict) else {}
    user = identities.get("user") if isinstance(identities, dict) else {}
    if isinstance(user, dict) and "available" in user:
        return bool(user.get("available"))
    config = cli_config if isinstance(cli_config, dict) else _read_lark_cli_config(use_global=use_global)
    apps = config.get("apps", []) if isinstance(config, dict) else []
    if apps and isinstance(apps, list):
        users = (apps[0] or {}).get("users")
        if users:
            return True
    return False


def _feishu_bitable_url(app_token: str) -> str:
    token = str(app_token or "").strip()
    return f"https://my.feishu.cn/base/{token}" if token else ""


def _lark_cli_auth_list_text(use_global: bool | None = None) -> str:
    try:
        stdout, stderr, _rc = _run_lark_cli_raw(["auth", "list"], timeout=10, use_global=use_global)
    except Exception:
        return ""
    return (stdout or stderr or "").strip()


def _feishu_effective_context(config: dict | None) -> dict:
    cfg = config or {}
    app_id = str(cfg.get("feishu_app_id") or "").strip()
    app_token = str(cfg.get("feishu_app_token") or "").strip()
    cli_mode = _to_bool(cfg.get("feishu_cli_mode"))
    use_global = _to_bool(cfg.get("feishu_cli_use_global_home"))
    owner_identity = str(cfg.get("feishu_bitable_owner_identity") or "").strip()
    cache_key = (bool(cli_mode), bool(use_global), app_id, app_token, owner_identity)
    if cli_mode:
        cached_key = _LARK_CLI_EFFECTIVE_CACHE.get("key")
        cached_ts = float(_LARK_CLI_EFFECTIVE_CACHE.get("ts") or 0)
        if cached_key == cache_key and time.time() - cached_ts < 8:
            return dict(_LARK_CLI_EFFECTIVE_CACHE.get("value") or {})
    context = {
        "sync_mode": "cli" if cli_mode else ("app" if _feishu_app_ready(cfg) else ""),
        "use_global_home": bool(use_global) if cli_mode else False,
        "home_kind": "global" if cli_mode and use_global else ("project" if cli_mode else ""),
        "home_label": "本机全局 ~/.lark-cli" if cli_mode and use_global else ("本项目隔离 .auth/lark-cli-home" if cli_mode else ""),
        "config_path": _active_lark_cli_config_file(use_global=use_global) if cli_mode else "",
        "app_id": app_id,
        "app_id_masked": _mask_value(app_id, prefix=8),
        "app_token": app_token,
        "app_token_masked": _mask_value(app_token, prefix=6),
        "base_url": _feishu_bitable_url(app_token),
        "identity": "",
        "bot_available": False,
        "user_available": False,
        "base_owner_identity": owner_identity,
        "account_display": "",
        "tenant_display": "lark-cli 未返回租户信息",
        "warning": "",
        "user_scope_ready": False,
        "missing_user_scopes": [],
        "user_scopes": [],
    }
    if not cli_mode:
        return context

    status = _lark_cli_status(use_global=use_global, timeout=20)
    identities = status.get("identities") if isinstance(status, dict) else {}
    bot = identities.get("bot") if isinstance(identities, dict) else {}
    user = identities.get("user") if isinstance(identities, dict) else {}
    bot_available = bool(bot.get("available"))
    user_available = bool(user.get("available"))
    context["bot_available"] = bot_available
    context["user_available"] = user_available
    context["identity"] = "user" if user_available else ("bot" if bot_available else "none")
    scope_state = _lark_cli_user_scope_state(required_scopes=FEISHU_USER_BASE_SCOPES, use_global=use_global, timeout=20)
    context["user_scope_ready"] = bool(scope_state.get("has_required_scopes"))
    context["missing_user_scopes"] = list(scope_state.get("missing_scopes") or [])
    context["user_scopes"] = list(scope_state.get("scopes") or [])
    if user_available:
        user_name = str(user.get("userName") or status.get("userName") or "").strip()
        context["account_display"] = user_name or "用户身份已登录，但 lark-cli 未返回账号名称"
    elif bot_available:
        context["account_display"] = "未登录用户身份，仅机器人/应用身份可用"
    else:
        context["account_display"] = "当前 CLI home 下没有可用飞书身份"
    if use_global:
        context["warning"] = "当前正在复用本机全局 ~/.lark-cli，可能仍是旧飞书账号/旧应用。换账号请重新配置并选择“切换账号 / 重新授权”。"
    if user_available and context["missing_user_scopes"]:
        context["warning"] = _required_user_scope_message(context["missing_user_scopes"])
    _LARK_CLI_EFFECTIVE_CACHE.update({"key": cache_key, "ts": time.time(), "value": dict(context)})
    return context


def _friendly_lark_cli_auth_error(detail: str, *, trigger_reason: str = "") -> str:
    text = str(detail or "").strip()
    lowered = text.lower()
    base_owner_reasons = {"initial_feishu_base_owner", "initial_feishu_base_access", "user_owned_base_sync"}
    if "authorization timed out" in lowered or "等待用户授权" in text:
        if trigger_reason in base_owner_reasons:
            return "飞书应用已创建，但当前用户授权没有完成或已超时。请点击“重试”，打开本工具显示的飞书用户授权页，不要停留在开放平台的“创建成功”页面。"
        return "飞书用户授权超时，请点击“重试”重新授权。"
    if "not_logged_in" in lowered or "base:app:create" in lowered:
        if trigger_reason in base_owner_reasons:
            return "飞书应用已创建，但还没有拿到当前用户的多维表格写入权限。请点击“重试”，确认浏览器里登录的是要切换的新飞书账号后再授权。"
        return "飞书用户授权尚未完成，请点击“重试”继续授权。"
    if "missing required scope" in lowered or "base:record:delete" in lowered:
        return _required_user_scope_message(["base:record:delete"] if "base:record:delete" in lowered else [])
    if text:
        return f"飞书用户授权失败：{text[-180:]}"
    return "飞书用户授权失败，请点击“重试”继续授权。"


def _clear_lark_cli_project_state(*, clear_saved_credentials: bool = False) -> dict:
    """Drop project-local lark-cli state so the next init must re-authorize."""
    _invalidate_lark_cli_caches()
    if os.path.isdir(LARK_CLI_HOME):
        shutil.rmtree(LARK_CLI_HOME, ignore_errors=True)
    os.makedirs(LARK_CLI_HOME, exist_ok=True)
    os.makedirs(LARK_CLI_DIR, exist_ok=True)
    if not clear_saved_credentials:
        return load_saved_config()
    config = load_saved_config()
    config["feishu_app_id"] = ""
    config["feishu_app_token"] = ""
    config["feishu_app_secret"] = ""
    config["feishu_cli_use_global_home"] = False
    config["feishu_initial_seed_pending"] = False
    config["feishu_bitable_owner_identity"] = ""
    return save_config(config)


def _extract_first_url(text: str) -> str:
    import re as _re

    match = _re.search(r"(https://open\.feishu\.cn/\S+)", str(text or ""))
    return match.group(1) if match else ""


def _copy_lark_cli_state() -> dict:
    with _LARK_CLI_LOCK:
        return dict(_LARK_CLI_STATE)


def _feishu_cli_requires_user_auth(detail: str) -> bool:
    text = str(detail or "")
    lowered = text.lower()
    return (
        "openapiaddfield limited" in lowered
        or "800004135" in lowered
        or "need_user_authorization" in lowered
        or "missing required scope" in lowered
        or "base:field:update" in lowered
        or "base:field:delete" in lowered
        or "base:record:delete" in lowered
        or "base:view:write_only" in lowered
        or "补充用户授权" in text
        or "需要重新授权" in text
        or "当前用户身份授权" in text
        or "用户可编辑" in text
    )


def _feishu_cli_requires_document_app_permission(detail: str) -> bool:
    lowered = str(detail or "").lower()
    return "91403" in lowered or "you don't have permission" in lowered


def _feishu_cli_requires_app_scope(detail: str) -> bool:
    lowered = str(detail or "").lower()
    return "99991672" in lowered or "base:app:create" in lowered or "app scope not enabled" in lowered


def _extract_feishu_console_url(detail: str) -> str:
    import re as _re

    match = _re.search(r"(https://open\.feishu\.cn/\S+)", str(detail or ""))
    return match.group(1) if match else ""




def _consume_lark_cli_output(proc, on_line, *, timeout_seconds: float = 300.0) -> None:
    """Consume interactive CLI output without letting a silent child block forever."""
    events: queue.Queue = queue.Queue()
    eof_marker = object()

    def reader() -> None:
        try:
            if proc.stdout is not None:
                for line in proc.stdout:
                    events.put(line)
        except Exception as exc:
            events.put(exc)
        finally:
            events.put(eof_marker)

    reader_thread = threading.Thread(target=reader, daemon=True, name="lark_cli_stdout")
    reader_thread.start()
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            reader_thread.join(timeout=1)
            if proc.stdout is not None:
                proc.stdout.close()
            raise subprocess.TimeoutExpired(getattr(proc, "args", "lark-cli"), timeout_seconds)
        try:
            item = events.get(timeout=min(0.25, remaining))
        except queue.Empty:
            if proc.poll() is not None and not reader_thread.is_alive():
                break
            continue
        if item is eof_marker:
            break
        if isinstance(item, BaseException):
            raise item
        on_line(str(item))

    remaining = deadline - time.monotonic()
    try:
        proc.wait(timeout=max(0.1, remaining))
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        raise
    finally:
        reader_thread.join(timeout=1)
        if proc.stdout is not None:
            proc.stdout.close()


def _open_trusted_feishu_verification_url(url: str) -> bool:
    """Open only the trusted Feishu CLI verification page in the user's browser."""
    target = str(url or "").strip()
    try:
        parsed = urlparse(target)
    except Exception:
        return False
    if parsed.scheme != "https" or parsed.hostname != "open.feishu.cn" or parsed.path != "/page/cli":
        return False
    if sys.platform != "darwin":
        return False
    try:
        subprocess.Popen(
            ["/usr/bin/open", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _start_lark_cli_user_auth(trigger_reason: str = "") -> dict:
    global _LARK_CLI_STATE
    with _LARK_CLI_LOCK:
        if _LARK_CLI_STATE.get("phase") in {"connecting", "scan_qr", "auth_waiting"} and _LARK_CLI_STATE.get("auth_mode") == "user":
            # 已有授权流程在等扫码：用户此时点“重新生成授权链接”多半是
            # 页面没弹出来——把现有授权页再拉起一次（仅 Windows）。
            existing_url = str(_LARK_CLI_STATE.get("verification_url") or "").strip()
            if existing_url and os.name == "nt":
                try:
                    os.startfile(existing_url)
                except Exception:
                    pass
            return {"ok": True, **dict(_LARK_CLI_STATE)}
        _LARK_CLI_STATE.update(
            {
                "phase": "connecting",
                "error": "",
                "message": "正在生成飞书用户授权链接...",
                "verification_url": "",
                "user_code": "",
                "device_code": "",
                "auth_mode": "user",
                "trigger_reason": str(trigger_reason or "").strip(),
            }
        )

    worker = threading.Thread(
        target=_lark_cli_user_auth_worker,
        args=(str(trigger_reason or "").strip(),),
        daemon=True,
        name="lark_cli_user_auth",
    )
    worker.start()
    return {"ok": True, **_copy_lark_cli_state()}


def _run_lark_cli_user_auth_flow(
    *,
    trigger_reason: str = "",
    success_message: str = "飞书用户授权完成，请重新执行同步。",
    prompt_message: str = "飞书需要补充用户授权，请扫码完成后自动继续。",
    domains: list[str] | None = None,
    scopes: list[str] | None = None,
    recommend: bool = True,
    use_global: bool | None = None,
) -> str:
    cli_prefix = _resolve_lark_cli_prefix()
    os.makedirs(LARK_CLI_DIR, exist_ok=True)
    env = _lark_cli_env(use_global=use_global)
    cmd = cli_prefix + ["auth", "login", "--no-wait", "--json"]
    if recommend:
        cmd.append("--recommend")
    for domain in (domains or []):
        cleaned = str(domain or "").strip()
        if cleaned:
            cmd.extend(["--domain", cleaned])
    cleaned_scopes = [str(scope or "").strip() for scope in (scopes or []) if str(scope or "").strip()]
    if cleaned_scopes:
        cmd.extend(["--scope", " ".join(cleaned_scopes)])
    init_proc = subprocess.run(
        cmd,
        cwd=LARK_CLI_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    init_output = "\n".join(part for part in (init_proc.stdout, init_proc.stderr) if part).strip()
    if init_proc.returncode != 0:
        raise RuntimeError(f"飞书用户授权初始化失败: {init_output[-300:]}")

    json_line = ""
    for line in reversed(init_output.splitlines()):
        candidate = str(line or "").strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            json_line = candidate
            break
    if not json_line:
        raise RuntimeError(f"飞书用户授权初始化返回异常: {init_output[-300:]}")
    try:
        auth_payload = json.loads(json_line)
    except Exception as exc:
        raise RuntimeError(f"飞书用户授权初始化返回异常: {json_line[:200]}") from exc

    captured_url = str(auth_payload.get("verification_url") or "").strip()
    device_code = str(auth_payload.get("device_code") or "").strip()
    if not captured_url or not device_code:
        raise RuntimeError(f"飞书用户授权初始化缺少 verification_url/device_code: {json_line[:200]}")
    user_code = str(auth_payload.get("user_code") or "").strip()
    if not user_code:
        try:
            user_code = str((parse_qs(urlparse(captured_url).query).get("user_code") or [""])[0]).strip()
        except Exception:
            user_code = ""

    with _LARK_CLI_LOCK:
        _LARK_CLI_STATE.update(
            {
                "phase": "scan_qr",
                "verification_url": captured_url,
                "user_code": user_code,
                "message": prompt_message,
                "error": "",
                "auth_mode": "user",
                "trigger_reason": trigger_reason,
                "device_code": device_code,
            }
        )

    # macOS 走可信 CLI 验证页拉起；Windows（lark-cli 设备流返回的是
    # accounts.feishu.cn 链接，macOS 白名单不匹配）用默认浏览器直接拉起。
    if not _open_trusted_feishu_verification_url(captured_url) and os.name == "nt":
        try:
            os.startfile(captured_url)
        except Exception:
            pass

    complete_output = ""
    try:
        complete_proc = subprocess.run(
            cli_prefix + ["auth", "login", "--device-code", device_code],
            cwd=LARK_CLI_DIR,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        complete_output = "\n".join(part for part in (complete_proc.stdout, complete_proc.stderr) if part).strip()
        _invalidate_lark_cli_caches()
        if complete_proc.returncode != 0 and not _lark_cli_has_required_user_auth(required_scopes=cleaned_scopes, use_global=use_global):
            raise RuntimeError(_friendly_lark_cli_auth_error(complete_output, trigger_reason=trigger_reason))
    except subprocess.TimeoutExpired:
        _invalidate_lark_cli_caches()
        if not _lark_cli_has_required_user_auth(required_scopes=cleaned_scopes, use_global=use_global):
            raise RuntimeError(_friendly_lark_cli_auth_error("authorization timed out", trigger_reason=trigger_reason))

    _invalidate_lark_cli_caches()
    # lark-cli 刚写完 token 就读 auth status，Windows 下偶发配置文件占用或
    # 网络抖动导致查询失败（返回空），会被误判为未登录。重试几次再定论。
    scope_state: dict = {}
    for _attempt in range(3):
        scope_state = _lark_cli_user_scope_state(
            required_scopes=cleaned_scopes,
            use_global=use_global,
            force_refresh=True,
        )
        if scope_state.get("available"):
            break
        time.sleep(2)
    if not scope_state.get("available"):
        raise RuntimeError(_friendly_lark_cli_auth_error("not_logged_in", trigger_reason=trigger_reason))
    if cleaned_scopes and scope_state.get("missing_scopes"):
        raise RuntimeError(_required_user_scope_message(list(scope_state.get("missing_scopes") or [])))

    _LARK_CLI_EFFECTIVE_CACHE.update({"key": None, "ts": 0.0, "value": {}})
    with _LARK_CLI_LOCK:
        _LARK_CLI_STATE.update(
            {
                "phase": "done",
                "message": success_message,
                "error": "",
                "verification_url": captured_url,
                "auth_mode": "user",
                "trigger_reason": trigger_reason,
                "effective": _feishu_effective_context(load_saved_config()),
            }
        )
    return captured_url


def _lark_cli_user_auth_worker(trigger_reason: str = ""):
    global _LARK_CLI_STATE

    try:
        _run_lark_cli_user_auth_flow(
            trigger_reason=trigger_reason,
            success_message="飞书用户授权完成，请重新执行同步。",
            prompt_message="飞书应用已创建。请继续打开“飞书用户授权页”，确认当前登录账号后授权多维表格写入权限。",
            scopes=FEISHU_USER_BASE_SCOPES,
            recommend=False,
        )
    except Exception as exc:
        with _LARK_CLI_LOCK:
            _LARK_CLI_STATE.update(
                {
                    "phase": "error",
                    "error": str(exc),
                    "message": "飞书重新授权失败。",
                    "auth_mode": "user",
                    "trigger_reason": trigger_reason,
                }
            )


def _lark_cli_connect_worker(
    create_base_name: str = "",
    allow_global_reuse: bool = False,
    force_reauth: bool = False,
):
    """Background worker: config init --new → save Feishu app identity.

    This step only links the user's Feishu account and prepares the app/bot
    identity. The first real /sync_feishu call creates the Bitable base and
    writes rows, then later syncs reuse that saved base token.
    """
    global _LARK_CLI_STATE
    import re as _re

    try:
        cli_prefix = _resolve_lark_cli_prefix()
    except RuntimeError:
        with _LARK_CLI_LOCK:
            _LARK_CLI_STATE["phase"] = "error"
            _LARK_CLI_STATE["error"] = "未找到 npx 命令，请确保 Node.js 已安装。"
        return

    try:
        os.makedirs(LARK_CLI_DIR, exist_ok=True)
        if force_reauth:
            _clear_lark_cli_project_state(clear_saved_credentials=True)
            allow_global_reuse = False

        if allow_global_reuse:
            imported_app_id = _lark_cli_app_id(use_global=True)
            if imported_app_id and _lark_cli_bot_ready(use_global=True):
                config = load_saved_config()
                previous_app_id = str(config.get("feishu_app_id") or "").strip()
                config["feishu_app_id"] = imported_app_id
                if previous_app_id and previous_app_id != imported_app_id:
                    config["feishu_app_token"] = ""
                config["feishu_enabled"] = True
                config["feishu_auto_sync"] = True
                config["feishu_cli_mode"] = True
                config["feishu_cli_use_global_home"] = True
                config["feishu_initial_seed_pending"] = False
                config["feishu_bitable_owner_identity"] = ""
                saved = save_config(config)
                if not _lark_cli_has_required_user_auth(use_global=True):
                    _run_lark_cli_user_auth_flow(
                        trigger_reason="initial_feishu_base_owner",
                        success_message="飞书用户授权完成。首次同步时会以当前用户身份创建可编辑的多维表格。",
                        prompt_message="当前复用了本机飞书应用配置，还需要打开“飞书用户授权页”，确认当前登录账号后授权多维表格写入权限。",
                        scopes=FEISHU_USER_BASE_SCOPES,
                        recommend=False,
                        use_global=True,
                    )
                with _LARK_CLI_LOCK:
                    _LARK_CLI_STATE["phase"] = "done"
                    _LARK_CLI_STATE["app_id"] = imported_app_id
                    _LARK_CLI_STATE["app_token"] = str(saved.get("feishu_app_token") or "")
                    _LARK_CLI_STATE["base_url"] = ""
                    _LARK_CLI_STATE["message"] = "已按你的选择复用本机全局飞书 CLI 配置，并完成当前用户写入授权。首次同步时会创建用户可编辑的新表。"
                    _LARK_CLI_STATE["effective"] = _feishu_effective_context(saved)
                    _LARK_CLI_STATE["error"] = ""
                return

        env = _lark_cli_env(use_global=False)

        # --- Step 1: config init --new (if not already configured) ---
        project_configured = _lark_cli_is_configured(use_global=False)
        project_ready = False
        if project_configured:
            project_status = _lark_cli_status(use_global=False, timeout=20, force_refresh=True)
            identities = project_status.get("identities") if isinstance(project_status, dict) else {}
            bot = identities.get("bot") if isinstance(identities, dict) else {}
            user = identities.get("user") if isinstance(identities, dict) else {}
            project_ready = bool((bot or {}).get("available") or (user or {}).get("available"))
        if project_configured and not project_ready:
            _clear_lark_cli_project_state(clear_saved_credentials=False)
            project_configured = False

        if not project_configured:
            with _LARK_CLI_LOCK:
                _LARK_CLI_STATE["phase"] = "connecting"
                _LARK_CLI_STATE["message"] = "正在生成飞书登录链接..."

            cmd = cli_prefix + ["config", "init", "--new", "--lang", "zh"]
            proc = subprocess.Popen(
                cmd, cwd=LARK_CLI_DIR, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )

            # Stream stdout to capture verification URL as soon as it appears.
            # The reader is supervised by an effective deadline; a silent CLI can no longer
            # leave the onboarding spinner alive forever.
            captured_url = ""
            all_output = []

            def handle_cli_line(line: str) -> None:
                nonlocal captured_url
                all_output.append(line)
                m = _re.search(r'(https://open\.feishu\.cn/\S+)', line)
                if m and not captured_url:
                    captured_url = m.group(1)
                    with _LARK_CLI_LOCK:
                        _LARK_CLI_STATE["phase"] = "scan_qr"
                        _LARK_CLI_STATE["verification_url"] = captured_url
                        _LARK_CLI_STATE["message"] = "飞书登录页面已生成；浏览器未自动打开时，请点击页面中的登录按钮。"
                        _LARK_CLI_STATE["error"] = ""
                    _open_trusted_feishu_verification_url(captured_url)

            try:
                _consume_lark_cli_output(proc, handle_cli_line, timeout_seconds=300)
            except subprocess.TimeoutExpired:
                with _LARK_CLI_LOCK:
                    _LARK_CLI_STATE["phase"] = "error"
                    _LARK_CLI_STATE["error"] = "飞书登录超时（5分钟），请重试。"
                return
            except Exception as exc:
                all_output.append(str(exc))
                try:
                    proc.kill()
                except Exception:
                    pass

            if proc.returncode != 0:
                output_text = "".join(all_output).strip()
                with _LARK_CLI_LOCK:
                    _LARK_CLI_STATE["phase"] = "error"
                    _LARK_CLI_STATE["error"] = f"飞书连接失败: {output_text[-300:]}"
                return
            _invalidate_lark_cli_caches()

        # Read app_id from CLI config
        cli_config = _read_lark_cli_config(use_global=False)
        apps = cli_config.get("apps", [])
        app_id = apps[0].get("appId", "") if apps else ""
        if not app_id:
            with _LARK_CLI_LOCK:
                _LARK_CLI_STATE["phase"] = "error"
                _LARK_CLI_STATE["error"] = "飞书应用初始化完成，但未读取到 app_id，请重试授权。"
            return

        config = load_saved_config()
        previous_app_id = str(config.get("feishu_app_id") or "").strip()
        config["feishu_app_id"] = app_id
        if previous_app_id and previous_app_id != app_id:
            config["feishu_app_token"] = ""
        config["feishu_enabled"] = True
        config["feishu_auto_sync"] = True
        config["feishu_cli_mode"] = True
        config["feishu_cli_use_global_home"] = False
        config["feishu_initial_seed_pending"] = False
        config["feishu_bitable_owner_identity"] = ""
        saved = save_config(config)

        if not _lark_cli_has_required_user_auth(use_global=False):
            _run_lark_cli_user_auth_flow(
                trigger_reason="initial_feishu_base_owner",
                success_message="飞书用户授权完成。首次同步时会以当前用户身份创建可编辑的多维表格。",
                prompt_message="飞书应用已创建。请继续打开“飞书用户授权页”，确认当前登录账号后授权多维表格写入权限。",
                scopes=FEISHU_USER_BASE_SCOPES,
                recommend=False,
                use_global=False,
            )

        with _LARK_CLI_LOCK:
            _LARK_CLI_STATE["phase"] = "done"
            _LARK_CLI_STATE["app_id"] = str(app_id)
            _LARK_CLI_STATE["app_token"] = str(saved.get("feishu_app_token") or "")
            _LARK_CLI_STATE["base_url"] = ""
            _LARK_CLI_STATE["effective"] = _feishu_effective_context(saved)
            _LARK_CLI_STATE["message"] = "飞书账号与写入授权已完成。首次同步时会以当前用户身份自动创建可编辑的多维表格。"
            _LARK_CLI_STATE["error"] = ""

    except subprocess.TimeoutExpired:
        with _LARK_CLI_LOCK:
            _LARK_CLI_STATE["phase"] = "error"
            _LARK_CLI_STATE["error"] = "飞书操作超时，请重试。"
    except Exception as exc:
        with _LARK_CLI_LOCK:
            _LARK_CLI_STATE["phase"] = "error"
            _LARK_CLI_STATE["error"] = str(exc)


def _parse_bitable_url(url: str) -> str:
    """Extract app_token from a Feishu bitable URL.

    Supported formats:
        https://xxx.feishu.cn/base/XXXXbcxxxx...
        https://xxx.larkoffice.com/base/XXXXbcxxxx...
        https://xxx.feishu.cn/wiki/XXXXbcxxxx... (wiki-embedded bitable)
    """
    url = (url or "").strip()
    if not url:
        return ""

    # Try to extract from /base/ path
    import re
    match = re.search(r'/base/([A-Za-z0-9]+)', url)
    if match:
        return match.group(1)

    # Try /wiki/ path (some bitables are embedded in wiki)
    match = re.search(r'/wiki/([A-Za-z0-9]+)', url)
    if match:
        return match.group(1)

    # If it looks like a raw token (no URL), return as-is
    if re.match(r'^[A-Za-z0-9]{10,}$', url):
        return url

    return ""


def _create_lark_cli_bitable_base(base_name: str) -> dict:
    name = _sanitize_workspace_name(base_name) or "自媒体数据分析"
    use_global = _saved_feishu_cli_use_global_home()
    if not _lark_cli_has_required_user_auth(use_global=use_global):
        missing_scopes = _lark_cli_missing_required_user_scopes(use_global=use_global, timeout=20)
        raise RuntimeError(_required_user_scope_message(missing_scopes))
    identities = ["user"]
    last_error = ""

    for identity in identities:
        try:
            result = _run_lark_cli(
                ["base", "+base-create", "--as", identity, "--name", name],
                timeout=30,
                use_global=use_global,
            )
        except Exception as exc:
            last_error = str(exc)
            continue

        base_data = result.get("data", {}).get("base", {}) if isinstance(result, dict) else {}
        app_token = str(
            base_data.get("base_token")
            or result.get("app_token")
            or result.get("appToken")
            or result.get("token")
            or ""
        ).strip()
        base_url = str(base_data.get("url") or result.get("url") or result.get("bitable_url") or "").strip()
        if not app_token and base_url:
            app_token = _parse_bitable_url(base_url)
        if not app_token and isinstance(result, dict):
            app_token = _parse_bitable_url(str(result.get("_raw") or ""))
        if not app_token:
            last_error = f"多维表格创建返回异常: {json.dumps(result, ensure_ascii=False)[:300]}"
            continue
        return {"app_token": app_token, "base_url": base_url, "name": name, "identity": identity}

    if last_error:
        raise RuntimeError(last_error)
    raise RuntimeError("多维表格创建失败，未获得可用返回结果。")


def _ensure_feishu_cli_bitable_target(config: dict) -> tuple[dict, dict]:
    current_token = str((config or {}).get("feishu_app_token") or "").strip()
    if current_token:
        return config, {}
    created = _create_lark_cli_bitable_base(str((config or {}).get("workspace_name") or "自媒体数据分析"))
    updated = dict(config or {})
    updated["feishu_app_token"] = created["app_token"]
    updated["feishu_enabled"] = True
    updated["feishu_auto_sync"] = True
    updated["feishu_cli_mode"] = True
    updated["feishu_initial_seed_pending"] = True
    updated["feishu_bitable_owner_identity"] = created["identity"]
    saved = save_config(updated)
    return saved, created


# ── 子进程执行核心（从 Handler 抽出的零状态枢纽，供编排层复用）─────────
def _get_proc_output(proc):
    """取子进程的 stdout/stderr 文本（_run_script 写到 proc.stdout_text）。"""
    return getattr(proc, "stdout_text", "") or "", getattr(proc, "stderr_text", "") or ""


def _append_log(title, stdout, stderr):
    """滚动写 LOG_FILE（2MB 轮转，保留最后 1MB）。模块级，多线程追加写安全。"""
    with LOG_WRITE_LOCK:
        _ensure_parent_dir(LOG_FILE)
        try:
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
                with open(LOG_FILE, "rb") as f:
                    f.seek(-LOG_KEEP_BYTES, 2)
                    kept = f.read()
                with open(LOG_FILE, "wb") as f:
                    f.write(b"[... truncated ...]\n")
                    f.write(kept)
        except Exception:
            pass

        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("\n=== {} {} ===\n".format(title, time.strftime("%Y-%m-%d %H:%M:%S%z")))
            f.write(stdout or "")
            f.write(stderr or "")


def _build_env(query, *, platform_id, progress_path, is_xhs=False):
    """构造采集/授权子进程的环境变量。零 self 状态。"""
    video_limit = query.get("limit", ["999"])[0]
    run_mode = query.get("run_mode", [None])[0]
    min_date = query.get("min_date", [None])[0]
    max_date = query.get("max_date", [None])[0]
    refresh_days = query.get("refresh_days", [None])[0]
    refresh_latest = query.get("refresh_latest", [None])[0]
    force_full = query.get("force_full", [None])[0]
    stale_limit = query.get("stale_limit", [None])[0]
    headless = query.get("headless", [None])[0]
    scan_wait_ms = query.get("scan_wait_ms", [None])[0]
    only_video = query.get("only_video", [None])[0]
    auth_only = query.get("auth_only", [None])[0]

    env = _scrub_supervision_env(os.environ.copy())
    config = load_saved_config()
    env["PATH"] = _effective_subprocess_path(env.get("PATH", ""))
    node_bin = _resolve_node_bin_for_env(env)
    if not node_bin:
        raise RuntimeError(
            "无法找到 Node.js 可执行文件。请确保已安装 Node.js 并在 PATH 中可用，"
            "或者设置 NODE_BIN 环境变量指向 node 二进制文件。"
        )
    env["NODE_BIN"] = node_bin
    env["PYTHON_BIN"] = PYTHON_BIN
    env["PLAYWRIGHT_BROWSERS_PATH"] = PLAYWRIGHT_BROWSERS_DIR
    env["VIDEO_LIMIT"] = video_limit
    env["PROGRESS_PATH"] = progress_path
    browser_channel = _sanitize_browser_channel(query.get("browser_channel", [None])[0]) or _sanitize_browser_channel(
        config.get("browser_channel")
    ) or DEFAULT_CONFIG["browser_channel"]
    env["BROWSER_CHANNEL"] = browser_channel
    browser_executable = _resolve_browser_executable(browser_channel)
    if browser_executable:
        env["BROWSER_EXECUTABLE_PATH"] = browser_executable
    env["USER_DATA_DIR"] = _resolve_user_data_dir(platform_id, browser_channel)

    if min_date:
        env["MIN_PUBLISH_DATE"] = min_date
    elif config.get("min_publish_date"):
        env["MIN_PUBLISH_DATE"] = config["min_publish_date"]
    if max_date:
        env["MAX_PUBLISH_DATE"] = max_date
    if refresh_days is not None:
        env["REFRESH_DAYS"] = refresh_days
    if refresh_latest is not None:
        env["REFRESH_LATEST_COUNT"] = refresh_latest
    elif platform_id in {"douyin", "xiaohongshu", "kuaishou"}:
        env["REFRESH_LATEST_COUNT"] = video_limit
    if stale_limit:
        env["STALE_ROUNDS_LIMIT"] = stale_limit
    if scan_wait_ms:
        env["SCAN_WAIT_MS"] = scan_wait_ms
    if only_video is not None:
        env["XHS_ONLY_VIDEO"] = "true" if str(only_video).lower() in ("1", "true", "yes") else "false"
    else:
        env["XHS_ONLY_VIDEO"] = "false"
    if auth_only is not None:
        env["AUTH_ONLY"] = "true" if str(auth_only).lower() in ("1", "true", "yes") else "false"
    if headless:
        env["HEADLESS"] = "true" if str(headless).lower() in ("1", "true", "yes") else "false"
    if (run_mode and str(run_mode).strip().lower() == "rerun") or (force_full and str(force_full).lower() in ("1", "true", "yes")):
        env["FORCE_FULL_EXPORT"] = "true"
    return env


def _register_auth_health_process(proc) -> None:
    global _AUTH_HEALTH_PROCESS
    with _AUTH_HEALTH_PROCESS_LOCK:
        _AUTH_HEALTH_PROCESS = proc


def _clear_auth_health_process(proc) -> None:
    global _AUTH_HEALTH_PROCESS
    with _AUTH_HEALTH_PROCESS_LOCK:
        if _AUTH_HEALTH_PROCESS is proc:
            _AUTH_HEALTH_PROCESS = None


def _terminate_process_tree(proc, *, requires_user_session: bool = False) -> None:
    if proc is None:
        return
    poll = getattr(proc, "poll", None)
    if requires_user_session and callable(poll) and poll() is not None:
        return
    try:
        if os.name != "nt" and not requires_user_session:
            # Batch jobs start with setsid(), so Popen.pid is also their PGID.
            # Kill the group even if the shell leader already exited while a
            # descendant still owns the inherited stdout/stderr pipes.
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        wait = getattr(proc, "wait", None)
        if callable(wait):
            wait(timeout=3)
        return
    except Exception:
        pass
    try:
        if os.name != "nt" and not requires_user_session:
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        wait = getattr(proc, "wait", None)
        if callable(wait):
            wait(timeout=3)
    except Exception:
        pass


def _terminate_auth_health_process() -> None:
    with _AUTH_HEALTH_PROCESS_LOCK:
        proc = _AUTH_HEALTH_PROCESS
    _terminate_process_tree(proc, requires_user_session=False)


def _run_script(
    command,
    env,
    timeout=SCRIPT_TIMEOUT,
    *,
    requires_user_session: bool = False,
    process_slot: str = "",
):
    """运行子进程并捕获输出。超时按 requires_user_session 区分清理方式。

    注意：requires_user_session=True 的授权类交互式任务不走 os.setsid，与 runner
    同进程组；超时只能 proc.kill() 杀子进程本身，绝不能 killpg（会连 runner
    一起干掉）。这是 Bug1 修复，不可改动。
    """
    popen_command = command
    env = _scrub_supervision_env(env)
    popen_kwargs = {
        "cwd": BASE_DIR,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        popen_kwargs["startupinfo"] = startupinfo
        if isinstance(command, list) and len(command) == 1 and command[0].lower().endswith(".cmd"):
            popen_command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", command[0]]
            popen_kwargs["creationflags"] = 0
        else:
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        if isinstance(command, list) and len(command) == 1 and command[0].endswith(".sh"):
            popen_command = ["/bin/bash", command[0]]
        if not requires_user_session:
            popen_kwargs["preexec_fn"] = os.setsid
    proc = subprocess.Popen(popen_command, **popen_kwargs)
    if process_slot == "auth_health":
        _register_auth_health_process(proc)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(proc, requires_user_session=requires_user_session)
            stdout, stderr = proc.communicate(timeout=10)
            _append_log(
                f"TIMEOUT({timeout}s)",
                stdout or "",
                (stderr or "") + f"\n[runner] 子进程超时({timeout}s)，已强制终止。\n",
            )
            proc.stdout_text = stdout or ""
            proc.stderr_text = stderr or f"[runner] 超时 ({timeout}s)\n"
            return proc

        proc.stdout_text = stdout
        proc.stderr_text = stderr
        return proc
    finally:
        if process_slot == "auth_health":
            _clear_auth_health_process(proc)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        try:
            message = "%s - - [%s] %s\n" % (
                self.address_string(),
                self.log_date_time_string(),
                format % args,
            )
            _ensure_parent_dir(LOG_FILE)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(message)
        except Exception:
            # Never let stderr/logging issues break HTTP responses on Windows.
            pass

    def _allowed_browser_origins(self) -> set[str]:
        port = getattr(self.server, "server_port", RUNNER_PORT)
        return {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        }

    def _request_origin(self) -> str:
        return str(self.headers.get("Origin", "") or "").strip()

    def _is_loopback_client(self) -> bool:
        try:
            client_host = str(self.client_address[0] or "").strip()
        except (AttributeError, IndexError, TypeError):
            return False
        return _is_loopback_host(client_host)

    def _cors_origin_for_request(self) -> str:
        origin = self._request_origin()
        if not origin:
            return ""
        if origin in self._allowed_browser_origins():
            return origin
        parsed = urlparse(self.path)
        if origin == "null" and self.command == "GET" and parsed.path == "/progress":
            return "null"
        return ""

    def _is_allowed_origin(self) -> bool:
        origin = self._request_origin()
        if not origin:
            return True
        return bool(self._cors_origin_for_request())

    def _request_session_token(self) -> str:
        header_token = str(self.headers.get(SESSION_HEADER_NAME, "") or "").strip()
        if header_token:
            return header_token
        if _is_tauri_supervised():
            return ""
        parsed = urlparse(self.path)
        return str(parse_qs(parsed.query).get(SESSION_QUERY_PARAM, [""])[0] or "").strip()

    def _require_request_security(self) -> bool:
        parsed = urlparse(self.path)
        if not self._is_allowed_origin():
            self._send_json(403, {"ok": False, "error": "forbidden_origin"})
            return False
        if _session_required_for_path(parsed.path) and self._request_session_token() != SESSION_TOKEN:
            self._send_json(401, {"ok": False, "error": "session_required"})
            return False
        # ── License enforcement: block protected endpoints when license is invalid ──
        if _license_required_for_path(parsed.path) and not LICENSE_BYPASS_ENABLED:
            phase, ok, _info = _license_access_for_request()
            if phase != "done":
                self._send_json(503, {
                    "ok": False,
                    "error": "license_check_pending",
                    "message": (_info or {}).get("message", "正在后台验证许可证，请稍候。"),
                    "access_mode": "checking",
                })
                return False
            if not ok:
                err_type = (_info or {}).get("error", "license_invalid")
                err_msg = (_info or {}).get("message", "许可证无效或未激活，请先激活许可证")
                self._send_json(403, {"ok": False, "error": "license_invalid",
                                      "license_error": err_type, "message": err_msg,
                                      "access_mode": (_info or {}).get("access_mode", "none")})
                return False
        return True

    def _set_cors_headers(self):
        allowed_origin = self._cors_origin_for_request()
        if not allowed_origin:
            return
        self.send_header("Access-Control-Allow-Origin", allowed_origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", f"Content-Type, {SESSION_HEADER_NAME}")

    def _send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        try:
            self.send_response(status)
            self._set_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Client disconnected (e.g. browser timed out during long auth). Ignore.
            pass

    def handle_error(self, request, client_address):
        try:
            self._append_log(
                "HTTP_THREAD_ERROR",
                "",
                f"client={client_address}\n{traceback.format_exc()}\n",
            )
        except Exception:
            pass

    def _send_file(self, file_path, content_type=None, as_download=False, cache_control: str | None = None):
        if not os.path.exists(file_path):
            self._send_json(404, {"ok": False, "error": "not_found"})
            return

        if content_type is None:
            content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"

        with open(file_path, "rb") as f:
            data = f.read()

        self.send_response(200)
        self._set_cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if cache_control:
            self.send_header("Cache-Control", cache_control)
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        if as_download:
            filename = os.path.basename(file_path)
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0

        if length <= 0:
            return {}

        raw = self.rfile.read(length).decode("utf-8")
        if not raw.strip():
            return {}
        return json.loads(raw)

    def _handle_data(self, parsed, file_path):
        query = parse_qs(parsed.query)
        limit = query.get("limit", [None])[0]
        try:
            limit = int(limit) if limit else None
        except ValueError:
            limit = None

        if not os.path.exists(file_path):
            self._send_json(200, {"ok": True, "rows": [], "row_count": 0})
            return

        df = pd.read_excel(file_path, dtype=str)
        df = df.where(pd.notna(df), None)
        records = json.loads(df.to_json(orient="records", force_ascii=False))
        if limit:
            records = records[:limit]
        self._send_json(200, {"ok": True, "rows": records, "row_count": len(records)})

    def _handle_progress(self):
        config = load_saved_config()
        summary = config_summary(config)
        enabled_platforms = summary.get("enabled_platforms", [])

        douyin = _decorate_progress(
            "douyin",
            _load_platform_progress("douyin", DOUYIN_PROGRESS_FILE),
            enabled_platforms,
        )
        xiaohongshu = _decorate_progress(
            "xiaohongshu",
            _load_platform_progress("xiaohongshu", XHS_PROGRESS_FILE),
            enabled_platforms,
        )
        bilibili = _decorate_progress(
            "bilibili",
            _load_platform_progress("bilibili", BILI_PROGRESS_FILE),
            enabled_platforms,
        )
        kuaishou = _decorate_progress(
            "kuaishou",
            _load_platform_progress("kuaishou", KS_PROGRESS_FILE),
            enabled_platforms,
        )

        platforms = {
            "douyin": douyin,
            "xiaohongshu": xiaohongshu,
            "bilibili": bilibili,
            "kuaishou": kuaishou,
        }
        running_platforms = [name for name, progress in platforms.items() if progress.get("ui_status") == "running"]
        auth_running_platforms = [name for name, progress in platforms.items() if progress.get("ui_status") == "authorizing"]
        active_platform = next(iter(running_platforms or auth_running_platforms), "")
        has_running_platform = bool(running_platforms)
        has_auth_running = bool(auth_running_platforms)
        global_locked = is_locked()
        lease_payload = _RUN_LEASE_STORE.read_payload() if global_locked else {}
        auth_health_running = str(lease_payload.get("kind") or "").strip() == "auth_health"
        if auth_health_running and _AUTH_HEALTH_ACTIVE_PLATFORM:
            active_platform = _AUTH_HEALTH_ACTIVE_PLATFORM
        if has_running_platform:
            current_stage = "scraping"
        elif has_auth_running:
            current_stage = "authorizing"
        elif auth_health_running:
            current_stage = "auth_checking"
        elif global_locked:
            current_stage = "importing"
        else:
            current_stage = "idle"
        completed_platforms = [name for name, progress in platforms.items() if progress.get("ui_status") == "completed"]
        zero_result_platforms = [name for name, progress in platforms.items() if progress.get("ui_status") == "completed_empty"]
        failed_platforms, needs_auth_platforms = _progress_summary_failure_buckets(platforms)
        authorized_platforms = [
            name
            for name, progress in platforms.items()
            if progress.get("enabled") and progress.get("auth_status") == "authorized"
        ]
        latest_history_items = _read_run_history()
        feishu_runtime = _build_feishu_runtime_summary(
            summary,
            latest_history_items,
            current_stage=current_stage,
            enabled_platforms=enabled_platforms,
        )

        payload = {
            "ok": True,
            "running": global_locked or has_auth_running,
            "douyin": douyin,
            "xiaohongshu": xiaohongshu,
            "bilibili": bilibili,
            "kuaishou": kuaishou,
            "summary": {
                "active_platform": active_platform,
                "current_stage": current_stage,
                "has_running_platform": has_running_platform,
                "has_auth_running": has_auth_running,
                "auth_running_platforms": auth_running_platforms,
                "auth_health": {
                    "running": auth_health_running,
                    "active_platform": _AUTH_HEALTH_ACTIVE_PLATFORM if auth_health_running else "",
                },
                "completed_platforms": completed_platforms,
                "zero_result_platforms": zero_result_platforms,
                "failed_platforms": failed_platforms,
                "needs_auth_platforms": needs_auth_platforms,
                "enabled_platform_count": summary.get("enabled_platform_count", 0),
                "enabled_platforms": enabled_platforms,
                "authorized_platform_count": len(authorized_platforms),
                "authorized_platforms": authorized_platforms,
                "setup_complete": summary.get("setup_complete", False),
                "onboarding_completed": summary.get("onboarding_completed", False),
                "has_run_history": summary.get("has_run_history", False),
                "feishu_enabled": summary.get("feishu_enabled", False),
                "feishu_ready": summary.get("feishu_ready", False),
                "auto_sync_enabled": summary.get("auto_sync_enabled", False),
                "feishu": feishu_runtime,
            },
            "serverTime": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        self._send_json(200, payload)

    def _handle_get_config(self):
        config = load_saved_config()
        self._send_json(200, {"ok": True, "config": public_config_payload(config), "summary": config_summary(config)})

    def _save_config_payload(self, payload):
        config = save_config(payload)
        return {"ok": True, "config": public_config_payload(config), "summary": config_summary(config)}

    def _run_feishu_sync(
        self,
        config: dict,
        *,
        min_date: str = "",
        max_date: str = "",
        platforms: list[str] | None = None,
        force_full_sync: bool = False,
    ) -> dict:
        sync_mode = _feishu_sync_mode(config)
        if not sync_mode:
            raise RuntimeError("飞书配置未完成。CLI 模式需要 app_id 且 lark-cli 已登录；App 模式需要 app_token / app_id / app_secret。")

        created_bitable: dict = {}
        if sync_mode == "cli":
            use_global = _saved_feishu_cli_use_global_home(config)
            current_token = str(config.get("feishu_app_token") or "").strip()
            owner_identity = str(config.get("feishu_bitable_owner_identity") or "").strip()
            requires_user_owned_base = not current_token or owner_identity == "user"
            if requires_user_owned_base and not _lark_cli_has_required_user_auth(use_global=use_global, timeout=20):
                reason = "initial_feishu_base_owner" if not current_token else "user_owned_base_sync"
                _start_lark_cli_user_auth(reason)
                missing_scopes = _lark_cli_missing_required_user_scopes(use_global=use_global, timeout=20)
                raise RuntimeError(_required_user_scope_message(missing_scopes))
            try:
                config, created_bitable = _ensure_feishu_cli_bitable_target(config)
            except Exception as exc:
                detail = str(exc)
                if _feishu_cli_requires_user_auth(detail):
                    _start_lark_cli_user_auth("initial_feishu_base_owner")
                    raise RuntimeError(_friendly_lark_cli_auth_error(detail, trigger_reason="initial_feishu_base_owner")) from exc
                raise RuntimeError(f"飞书 CLI 创建/确认目标多维表格失败：{detail}") from exc
            if created_bitable:
                self._append_log("FEISHU_CREATE_BASE", json.dumps(created_bitable, ensure_ascii=False), "")

        bitable_url = str(created_bitable.get("base_url") or _feishu_bitable_url(str(config.get("feishu_app_token") or ""))).strip()
        bitable_name = str(created_bitable.get("name") or "").strip()

        output_path = os.path.join(DOWNLOADS_DIR, "feishu_sync_payload_compare_v2.json")
        pending_snapshot_path = os.path.join(DOWNLOADS_DIR, "feishu_sync_pending_snapshot_v2.json")
        prepare_cmd = [
            PYTHON_BIN,
            FEISHU_PREPARE_SCRIPT,
            "--output",
            output_path,
            "--pending-snapshot",
            pending_snapshot_path,
        ]
        chosen_min_date = min_date or config.get("min_publish_date", "")
        if chosen_min_date:
            prepare_cmd.extend(["--min-date", chosen_min_date])
        chosen_max_date = max_date or ""
        if chosen_max_date:
            prepare_cmd.extend(["--max-date", chosen_max_date])
        selected_platforms = _normalize_platform_ids(platforms or _enabled_platform_scope(config))
        if selected_platforms:
            prepare_cmd.extend(["--platforms", ",".join(selected_platforms)])
        initial_seed_pending = bool(created_bitable) or _to_bool(config.get("feishu_initial_seed_pending"))
        if initial_seed_pending:
            prepare_cmd.append("--baseline-only")

        prepare_proc = self._run_script(prepare_cmd, os.environ.copy(), timeout=180)
        prepare_stdout, prepare_stderr = self._get_proc_output(prepare_proc)
        self._append_log("FEISHU_PREPARE", prepare_stdout, prepare_stderr)
        if prepare_proc.returncode != 0:
            raise RuntimeError(
                f"生成飞书同步 payload 失败：{_process_failure_detail(prepare_stdout, prepare_stderr, '请查看 FEISHU_PREPARE 日志。')}"
            )

        prepare_meta = {}
        try:
            prepare_meta = json.loads((prepare_stdout or "{}").strip() or "{}")
        except Exception:
            pass

        payload_data = read_json_file(output_path, {})
        if isinstance(payload_data, dict) and _current_feishu_detail_rows(payload_data):
            prepare_meta.update(_summarize_feishu_local_changes(payload_data, _load_feishu_sync_baseline()))

        if selected_platforms and not prepare_meta.get("platforms"):
            prepare_meta["platforms"] = list(selected_platforms)
        if created_bitable:
            prepare_meta["created_bitable"] = created_bitable
        if initial_seed_pending:
            prepare_meta["initial_seed_pending"] = True

        if not _feishu_prepare_has_syncable_data(prepare_meta):
            if force_full_sync or initial_seed_pending:
                prepare_meta["forced_full_sync"] = True
                self._append_log(
                    "FEISHU_SYNC_FORCE_INITIAL",
                    json.dumps({"reason": "force_full_sync_or_initial_seed", "prepare": prepare_meta}, ensure_ascii=False),
                    "",
                )
            else:
                skip_message = "本地没有新数据或指标变化，已跳过飞书同步。"
                try:
                    _remove_file_if_exists(pending_snapshot_path, allowed_root=DOWNLOADS_DIR)
                except Exception:
                    pass
                self._append_log(
                    "FEISHU_SYNC_SKIPPED",
                    json.dumps({"reason": "no_new_data", "prepare": prepare_meta}, ensure_ascii=False),
                    "",
                )
                return {
                    "ok": True,
                    "attempted": False,
                    "reason": "no_new_data",
                    "message": skip_message,
                    "base_url": bitable_url,
                    "base_name": bitable_name,
                    "prepare": prepare_meta,
                    "sync": {},
                    "payload_path": output_path,
                }

        sync_cmd = [
            PYTHON_BIN,
            FEISHU_SYNC_SCRIPT,
            "--app-token",
            config.get("feishu_app_token", ""),
            "--payload",
            output_path,
        ]
        if sync_mode == "cli":
            sync_cmd.append("--cli-mode")
            if initial_seed_pending or created_bitable:
                sync_cmd.append("--strict-schema")
        else:
            sync_cmd.extend([
                "--app-id",
                config.get("feishu_app_id", ""),
                "--app-secret",
                config.get("feishu_app_secret", ""),
            ])
        sync_timeout = FEISHU_SYNC_TIMEOUT_CLI if sync_mode == "cli" else FEISHU_SYNC_TIMEOUT_APP
        sync_env = os.environ.copy()
        if sync_mode == "cli" and _saved_feishu_cli_use_global_home(config):
            sync_env["FEISHU_CLI_USE_GLOBAL_HOME"] = "1"
        sync_proc = self._run_script(sync_cmd, sync_env, timeout=sync_timeout)
        sync_stdout, sync_stderr = self._get_proc_output(sync_proc)
        self._append_log("FEISHU_SYNC", sync_stdout, sync_stderr)
        if sync_proc.returncode != 0:
            failure_detail = _process_failure_detail(sync_stdout, sync_stderr, "请查看 FEISHU_SYNC 日志。")
            if sync_mode == "cli" and _feishu_cli_requires_document_app_permission(failure_detail):
                raise RuntimeError(f"飞书文档应用没有该多维表格的编辑权限，请给文档应用/机器人开通协作者权限后重试。\n{failure_detail}")
            if sync_mode == "cli" and _feishu_cli_requires_user_auth(failure_detail):
                _start_lark_cli_user_auth(failure_detail)
                raise RuntimeError(f"飞书需要补充用户授权，请扫码完成后重试同步。\n{failure_detail}")
            raise RuntimeError(
                f"同步到飞书多维表格失败：{failure_detail}"
            )

        sync_meta = {}
        try:
            sync_meta = json.loads((sync_stdout or "{}").strip() or "{}")
        except Exception:
            pass
        if isinstance(sync_meta, dict) and sync_meta.get("ok") is False:
            raise RuntimeError(
                f"同步到飞书多维表格失败：{sync_meta.get('error') or sync_meta.get('message') or 'sync_failed'}"
            )

        if selected_platforms and not sync_meta.get("platforms"):
            sync_meta["platforms"] = list(selected_platforms)
        if selected_platforms and not prepare_meta.get("platforms"):
            prepare_meta["platforms"] = list(selected_platforms)
        if created_bitable:
            sync_meta["created_bitable"] = created_bitable

        prepared_pending_snapshot = str(
            prepare_meta.get("pending_snapshot_path") or pending_snapshot_path
        ).strip()
        if prepared_pending_snapshot and os.path.exists(prepared_pending_snapshot):
            commit_proc = self._run_script(
                [
                    PYTHON_BIN,
                    FEISHU_PREPARE_SCRIPT,
                    "--commit-snapshot",
                    prepared_pending_snapshot,
                ],
                os.environ.copy(),
                timeout=60,
            )
            commit_stdout, commit_stderr = self._get_proc_output(commit_proc)
            self._append_log("FEISHU_SNAPSHOT_COMMIT", commit_stdout, commit_stderr)
            if commit_proc.returncode != 0:
                raise RuntimeError(
                    "飞书远程同步已完成，但本地指标基线提交失败："
                    + _process_failure_detail(
                        commit_stdout,
                        commit_stderr,
                        "请查看 FEISHU_SNAPSHOT_COMMIT 日志。",
                    )
                )

        if isinstance(payload_data, dict):
            _persist_feishu_sync_baseline(payload_data)
        if sync_mode == "cli" and _to_bool(config.get("feishu_initial_seed_pending")):
            updated_config = load_saved_config()
            updated_config["feishu_initial_seed_pending"] = False
            save_config(updated_config)

        return {
            "ok": True,
            "attempted": True,
            "base_url": bitable_url,
            "base_name": bitable_name,
            "prepare": prepare_meta,
            "sync": sync_meta,
            "payload_path": output_path,
        }
    def _test_feishu_connection(self, payload):
        current = load_saved_config()
        merged = dict(current)
        if isinstance(payload, dict):
            if "feishu_enabled" in payload:
                merged["feishu_enabled"] = _to_bool(payload.get("feishu_enabled"))
            if "feishu_cli_mode" in payload:
                merged["feishu_cli_mode"] = _to_bool(payload.get("feishu_cli_mode"))
            for key in SECRET_CONFIG_FIELDS:
                if key not in payload:
                    continue
                value = str(payload.get(key) or "").strip()
                if value:
                    merged[key] = value
        sync_mode = _feishu_sync_mode(merged)
        if not sync_mode:
            raise RuntimeError("飞书测试失败。CLI 模式需要 app_id 且 lark-cli 已登录；App 模式需要 app_token、app_id、app_secret。")
        if sync_mode == "cli" and not str(merged.get("feishu_app_token") or "").strip():
            use_global = _saved_feishu_cli_use_global_home(merged)
            if not _lark_cli_has_required_user_auth(use_global=use_global, timeout=20):
                missing_scopes = _lark_cli_missing_required_user_scopes(use_global=use_global, timeout=20)
                raise RuntimeError(_required_user_scope_message(missing_scopes))
            return {"ok": True, "message": "飞书 CLI 已连接；首次同步时会自动创建可编辑的多维表格。"}

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as temp_file:
            json.dump({"table_definitions": [], "tables": {}}, temp_file, ensure_ascii=False)
            temp_path = temp_file.name

        try:
            sync_cmd = [
                PYTHON_BIN,
                FEISHU_SYNC_SCRIPT,
                "--app-token",
                merged.get("feishu_app_token", ""),
                "--payload",
                temp_path,
            ]
            if sync_mode == "cli":
                sync_cmd.append("--cli-mode")
            else:
                sync_cmd.extend([
                    "--app-id",
                    merged.get("feishu_app_id", ""),
                    "--app-secret",
                    merged.get("feishu_app_secret", ""),
                ])
            sync_timeout = FEISHU_TEST_TIMEOUT_CLI if sync_mode == "cli" else FEISHU_TEST_TIMEOUT_APP
            sync_env = os.environ.copy()
            if sync_mode == "cli" and _saved_feishu_cli_use_global_home(merged):
                sync_env["FEISHU_CLI_USE_GLOBAL_HOME"] = "1"
            proc = self._run_script(sync_cmd, sync_env, timeout=sync_timeout)
            stdout, stderr = self._get_proc_output(proc)
            self._append_log("FEISHU_TEST", stdout, stderr)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"飞书连接测试失败：{_process_failure_detail(stdout, stderr, '请查看 FEISHU_TEST 日志。')}"
                )
            return {"ok": True, "message": "飞书连接可用"}
        finally:
            try:
                os.remove(temp_path)
            except FileNotFoundError:
                pass

    # ------------------------------------------------------------------
    # Lark CLI (飞书 CLI) endpoint handlers
    # ------------------------------------------------------------------

    def _handle_feishu_cli_init(self):
        """POST /feishu/cli-init — Start Feishu login + auto base creation.

        Body: {"mode": "create", "name": "..."} or {"mode": "existing"}
        """
        global _LARK_CLI_STATE
        with _LARK_CLI_LOCK:
            if _LARK_CLI_STATE["phase"] in ("connecting", "scan_qr", "creating_base"):
                self._send_json(200, {"ok": True, "phase": _LARK_CLI_STATE["phase"], "message": "飞书连接进行中。"})
                return
            _LARK_CLI_STATE = {
                "phase": "connecting",
                "error": "",
                "message": "正在连接飞书...",
                "app_id": "",
                "app_secret": "",
                "verification_url": "",
                "user_code": "",
                "device_code": "",
                "app_token": "",
                "base_url": "",
                "effective": {},
            }

        payload = self._read_json_body()
        mode = (payload.get("mode") or "create").strip()
        base_name = (payload.get("name") or "").strip() or "自媒体数据分析"
        create_name = base_name if mode == "create" else ""
        force_reauth = _to_bool(payload.get("force_reauth"))

        worker = threading.Thread(
            target=_lark_cli_connect_worker,
            args=(create_name,),
            kwargs={"force_reauth": force_reauth},
            daemon=True,
            name="lark_cli_connect",
        )
        worker.start()
        self._send_json(200, {"ok": True, "phase": "connecting", "message": "正在连接飞书..."})

    def _handle_feishu_cli_status(self):
        """GET /feishu/cli-status — Return current lark-cli setup state."""
        state_copy = _copy_lark_cli_state()
        # Never expose app_secret to frontend
        state_copy.pop("app_secret", None)
        self._send_json(200, {"ok": True, **state_copy})

    def _handle_feishu_cli_auth(self):
        """POST /feishu/cli-auth — Start user re-authorization flow when CLI bot identity is insufficient."""
        payload = self._read_json_body()
        trigger_reason = str((payload or {}).get("reason") or "").strip()
        state = _start_lark_cli_user_auth(trigger_reason)
        state.pop("app_secret", None)
        self._send_json(200, state)

    def _handle_feishu_cli_auth_poll(self):
        """POST /feishu/cli-auth-poll — Return current user re-authorization state."""
        self._handle_feishu_cli_status()

    def _handle_feishu_cli_create_base(self):
        """POST /feishu/cli-create-base — Create a new Feishu bitable."""
        global _LARK_CLI_STATE
        payload = self._read_json_body()
        base_name = (payload.get("name") or "").strip() or "自媒体数据分析"
        try:
            with _LARK_CLI_LOCK:
                _LARK_CLI_STATE["phase"] = "creating_base"

            result = _create_lark_cli_bitable_base(base_name)
            app_token = str(result.get("app_token") or "")
            bitable_url = str(result.get("base_url") or _feishu_bitable_url(app_token))
            config = load_saved_config()
            config["feishu_app_token"] = app_token
            config["feishu_enabled"] = True
            config["feishu_auto_sync"] = True
            config["feishu_cli_mode"] = True
            config["feishu_initial_seed_pending"] = True
            config["feishu_bitable_owner_identity"] = str(result.get("identity") or "user")
            saved = save_config(config)

            with _LARK_CLI_LOCK:
                _LARK_CLI_STATE["app_token"] = str(app_token)
                _LARK_CLI_STATE["phase"] = "ready"
                _LARK_CLI_STATE["base_url"] = bitable_url
                _LARK_CLI_STATE["effective"] = _feishu_effective_context(saved)

            self._send_json(200, {
                "ok": True,
                "phase": "ready",
                "app_token": str(app_token),
                "url": str(bitable_url),
                "message": "多维表格创建成功！",
            })
        except Exception as exc:
            with _LARK_CLI_LOCK:
                _LARK_CLI_STATE["phase"] = "error"
                _LARK_CLI_STATE["error"] = str(exc)
            self._send_json(400, {"ok": False, "error": str(exc)})

    def _handle_feishu_parse_url(self):
        """POST /feishu/parse-bitable-url — Parse bitable URL to extract app_token."""
        global _LARK_CLI_STATE
        payload = self._read_json_body()
        url = (payload.get("url") or "").strip()
        app_token = _parse_bitable_url(url)
        if not app_token:
            self._send_json(400, {"ok": False, "error": "invalid_url", "message": "无法从 URL 中提取多维表格 Token，请检查链接格式。"})
            return

        with _LARK_CLI_LOCK:
            _LARK_CLI_STATE["app_token"] = app_token
            _LARK_CLI_STATE["phase"] = "ready"

        self._send_json(200, {"ok": True, "app_token": app_token})

    def _handle_feishu_cli_save(self):
        """POST /feishu/cli-save — Save CLI-based Feishu config (no app_secret needed)."""
        with _LARK_CLI_LOCK:
            app_id = _LARK_CLI_STATE.get("app_id", "")
            app_token = _LARK_CLI_STATE.get("app_token", "")

        payload = self._read_json_body()
        app_token = (payload.get("app_token") or "").strip() or app_token

        if not app_token:
            self._send_json(400, {"ok": False, "error": "missing_app_token", "message": "多维表格 Token 缺失。"})
            return

        config = load_saved_config()
        config["feishu_app_id"] = app_id or str(config.get("feishu_app_id") or "").strip()
        config["feishu_app_token"] = app_token
        config["feishu_enabled"] = True
        config["feishu_auto_sync"] = True
        config["feishu_cli_mode"] = True  # sync through lark-cli api, no app_secret
        config["feishu_cli_use_global_home"] = _saved_feishu_cli_use_global_home(config)
        if not config.get("feishu_bitable_owner_identity"):
            config["feishu_bitable_owner_identity"] = "user"
        saved = save_config(config)

        self._send_json(
            200,
            {
                "ok": True,
                "config": public_config_payload(saved),
                "summary": config_summary(saved),
                "message": "飞书配置已保存并启用！",
            },
        )

    def _handle_feishu_cli_reset(self):
        """POST /feishu/cli-reset — Reset lark-cli state to idle."""
        global _LARK_CLI_STATE
        payload = self._read_json_body()
        clear_saved_credentials = _to_bool(payload.get("clear_saved_credentials"))
        if clear_saved_credentials:
            _clear_lark_cli_project_state(clear_saved_credentials=True)
        with _LARK_CLI_LOCK:
            _LARK_CLI_STATE = {
                "phase": "idle",
                "error": "",
                "message": "",
                "app_id": "",
                "app_secret": "",
                "verification_url": "",
                "user_code": "",
                "device_code": "",
                "app_token": "",
                "base_url": "",
                "effective": {},
            }
        self._send_json(200, {"ok": True, "phase": "idle", "cleared_credentials": clear_saved_credentials})

    def _run_all_platform_steps(self, query):
        platform_steps = [
            ("douyin", RUN_SCRIPT, DOUYIN_PROGRESS_FILE, False),
            ("xiaohongshu", RUN_XHS_SCRIPT, XHS_PROGRESS_FILE, True),
            ("bilibili", RUN_BILI_SCRIPT, BILI_PROGRESS_FILE, False),
            ("kuaishou", RUN_KS_SCRIPT, KS_PROGRESS_FILE, False),
        ]
        enabled = _enabled_platform_scope(load_saved_config())
        allowed = set(resolve_requested_targets(query, enabled))
        return [
            (name, script, progress_path, is_xhs_flag)
            for name, script, progress_path, is_xhs_flag in platform_steps
            if name in allowed
        ]

    def _prime_run_all_targets(self, query) -> list[str]:
        platform_ids = []
        for platform_id, progress_path in _resolve_requested_run_targets("/run_all", query):
            platform_ids.append(platform_id)
            _prime_platform_progress(platform_id, progress_path, phase="queued", message="等待采集槽位")
        return platform_ids

    def _finalize_run_all_worker_failure(self, query, started_at: str, start: float, error_message: str) -> None:
        platform_steps = self._run_all_platform_steps(query)
        touched_platforms = []
        for platform_id, _script, progress_path, _is_xhs_flag in platform_steps:
            progress = read_json_file(progress_path, default_progress(platform_id))
            if str(progress.get("status") or "").strip().lower() != "running":
                continue
            touched_platforms.append(platform_id)
            _finalize_platform_progress(
                platform_id,
                progress_path,
                ok=False,
                stderr=error_message,
                failure_message="同步失败",
            )
            _sync_platform_auth_state_from_progress(platform_id, progress_path)

        history_entry = _build_run_history_entry(
            raw_mode="run_all",
            requested_mode=query.get("run_mode", [""])[0],
            min_date=query.get("min_date", [""])[0],
            max_date=query.get("max_date", [""])[0],
            started_at=started_at,
            ended_at=_format_run_time(),
            duration=round(time.time() - start, 2),
            merge_ok=False,
            platform_snapshot=_platform_history_snapshot([name for name, *_ in platform_steps]),
            feishu_attempted=False,
            feishu_ok=False,
            feishu_error=error_message,
        )
        if touched_platforms or history_entry.get("status") != "completed":
            _append_run_history_entry(history_entry)

    def _execute_run_all_platform_step(self, step, query: dict, run_id: str) -> PlatformResult:
        name, script, progress_path, is_xhs_flag = step
        started = time.monotonic()
        if not os.path.exists(script):
            return PlatformResult(
                platform=name,
                outcome="failed",
                retryable=False,
                returncode=1,
                error_message="script_not_found",
            )

        preflight = _preflight_platform_run(name, progress_path)
        if preflight.get("blocked"):
            message = str(preflight.get("message") or "authorization_required")
            self._append_log(
                f"RUN_{name.upper()}_BLOCKED_AUTH",
                "skip run because authorization is required\n"
                f"auth_status={preflight.get('auth_status')}\n"
                f"auth_reason={preflight.get('auth_reason')}\n"
                f"message={message}\n",
                "",
            )
            return PlatformResult(
                platform=name,
                outcome="auth_required",
                retryable=False,
                returncode=1,
                error_message=message,
            )

        _prime_platform_progress(name, progress_path, phase="starting", message="准备启动同步任务")
        try:
            workspace = RunWorkspace(DOWNLOADS_DIR, run_id, name)
            if name == "douyin":
                _seed_douyin_workspace(workspace, DOWNLOADS_DIR)
            artifact_env, artifact_mapping = _platform_artifact_contract(name, workspace)
            env = self._build_env(
                query,
                platform_id=name,
                progress_path=progress_path,
                is_xhs=is_xhs_flag,
            )
            env.update(artifact_env)
            proc = self._run_platform_script(
                [script],
                env,
                platform_id=name,
                progress_path=progress_path,
                run_id=run_id,
            )
            stdout, stderr = self._get_proc_output(proc)
            self._append_log(f"RUN_{name.upper()}", stdout, stderr)
            ok = proc.returncode == 0 and getattr(proc, "outcome", "success") == "success"
            fresh_output = False
            partial_promotion_error = ""
            completed_empty = ok and _platform_completed_without_output(progress_path)
            partial_failure = (
                not ok
                and name == "douyin"
                and _is_promotable_douyin_partial_failure(progress_path)
            )
            if ok:
                if completed_empty:
                    _stage_completed_empty_artifacts(name, artifact_mapping)
                elif not all(source.exists() for source in artifact_mapping):
                    missing = next(source for source in artifact_mapping if not source.exists())
                    raise ArtifactValidationError(f"missing_artifact:{name}:{missing.name}")
                fresh_output = _promote_platform_artifacts(
                    name,
                    workspace,
                    artifact_mapping,
                )
            elif partial_failure:
                try:
                    fresh_output = _promote_platform_artifacts(
                        name,
                        workspace,
                        artifact_mapping,
                    )
                except Exception as exc:
                    partial_promotion_error = str(exc) or repr(exc)
                    self._append_log(
                        "RUN_DOUYIN_PARTIAL_PROMOTION_ERROR",
                        "",
                        partial_promotion_error,
                    )
            _finalize_platform_progress(
                name,
                progress_path,
                ok=ok,
                stdout=stdout,
                stderr=stderr,
                success_message="同步完成",
                failure_message="同步失败",
            )
            _sync_platform_auth_state_from_progress(name, progress_path)
            outcome = (
                "completed_empty"
                if completed_empty
                else (
                    "success"
                    if ok
                    else (
                        "partial_failure"
                        if partial_failure
                        else str(getattr(proc, "outcome", "failed") or "failed")
                    )
                )
            )
            return PlatformResult(
                platform=name,
                outcome=outcome,
                retryable=outcome in {"failed", "stalled"},
                returncode=int(proc.returncode or 0),
                fresh_output=fresh_output,
                duration_seconds=round(time.monotonic() - started, 3),
                error_message=(
                    ""
                    if ok
                    else (
                        f"partial_artifact_promotion_failed:{partial_promotion_error}"
                        if partial_promotion_error
                        else (stderr.strip() or outcome)
                    )
                ),
                started=True,
            )
        except Exception as exc:
            message = str(exc) or repr(exc)
            self._append_log(f"RUN_{name.upper()}_ERROR", "", message)
            _finalize_platform_progress(
                name,
                progress_path,
                ok=False,
                stderr=message,
                failure_message="同步失败",
            )
            _sync_platform_auth_state_from_progress(name, progress_path)
            return PlatformResult(
                platform=name,
                outcome="failed",
                retryable=True,
                returncode=1,
                duration_seconds=round(time.monotonic() - started, 3),
                error_message=message,
                started=True,
            )

    def _run_platform_steps_bounded(self, platform_steps, query: dict, run_id: str) -> dict[str, PlatformResult]:
        step_by_platform = {step[0]: step for step in platform_steps}

        def run_one(platform_id: str) -> PlatformResult:
            return self._execute_run_all_platform_step(
                step_by_platform[platform_id],
                query,
                run_id,
            )

        return run_bounded(
            [step[0] for step in platform_steps],
            run_one,
            max_workers=COLLECTION_MAX_WORKERS,
            retry_delays={"douyin": COLLECTION_DOUYIN_RETRY_DELAY_SECONDS},
        )

    def _execute_run_all(self, query, *, start: float, started_at: str, run_id: str) -> dict:
        resolved_min_date, resolved_max_date = _resolve_query_date_window(
            query,
            default_min_date=load_saved_config().get("min_publish_date") or DEFAULT_CONFIG["min_publish_date"],
        )
        query["min_date"] = [resolved_min_date]
        if resolved_max_date:
            query["max_date"] = [resolved_max_date]
        else:
            query.pop("max_date", None)

        platform_steps = self._run_all_platform_steps(query)
        selected_platforms = [name for name, *_ in platform_steps]
        scheduled_results = self._run_platform_steps_bounded(platform_steps, query, run_id)
        platform_results = {
            name: result.outcome in {"success", "completed_empty"} and result.fresh_output
            for name, result in scheduled_results.items()
        }
        any_platform_started = any(result.started for result in scheduled_results.values())
        all_selected_platforms_succeeded = bool(selected_platforms) and all(
            platform_results.get(name, False) for name in selected_platforms
        )

        successful_platforms = [name for name in selected_platforms if platform_results.get(name, False)]
        merge_ok = False
        if any_platform_started:
            if all_selected_platforms_succeeded:
                try:
                    merge_env = os.environ.copy()
                    merge_env["PYTHON_BIN"] = PYTHON_BIN
                    merge_env["MIN_PUBLISH_DATE"] = query.get("min_date", [DEFAULT_CONFIG["min_publish_date"]])[0]
                    if query.get("max_date", [""])[0]:
                        merge_env["MAX_PUBLISH_DATE"] = query.get("max_date", [""])[0]
                    merge_cmd = [PYTHON_BIN, MERGE_CHANNELS_SCRIPT]
                    merge_cmd.extend(["--platforms", ",".join(successful_platforms)])
                    merge_proc = self._run_script(merge_cmd, merge_env)
                    stdout, stderr = self._get_proc_output(merge_proc)
                    self._append_log("MERGE_ALL_CHANNELS", stdout, stderr)
                    merge_ok = merge_proc.returncode == 0
                except Exception as exc:
                    self._append_log("MERGE_ALL_CHANNELS_ERROR", "", str(exc))
            else:
                self._append_log(
                    "MERGE_ALL_CHANNELS_SKIPPED",
                    "skip merge because not every selected platform produced a successful current output\n",
                    "",
                )
        else:
            self._append_log("MERGE_ALL_CHANNELS_SKIPPED", "skip merge because no platform run was started\n", "")

        if merge_ok and any_platform_started:
            try:
                archive_snapshot_from_excel(ALL_DATA_FILE, DB_PATH)
            except Exception:
                pass

            # Build enriched Excel export (non-blocking)
            try:
                excel_cmd = [PYTHON_BIN, BUILD_EXCEL_SCRIPT, "--mode", "all"]
                if successful_platforms:
                    excel_cmd.extend(["--platforms", ",".join(successful_platforms)])
                chosen_min_date_for_excel = query.get("min_date", [DEFAULT_CONFIG["min_publish_date"]])[0]
                if chosen_min_date_for_excel:
                    excel_cmd.extend(["--min-date", chosen_min_date_for_excel])
                chosen_max_date_for_excel = query.get("max_date", [""])[0]
                if chosen_max_date_for_excel:
                    excel_cmd.extend(["--max-date", chosen_max_date_for_excel])
                excel_proc = self._run_script(excel_cmd, os.environ.copy(), timeout=120)
                e_stdout, e_stderr = self._get_proc_output(excel_proc)
                self._append_log("BUILD_EXCEL_EXPORT", e_stdout, e_stderr)
            except Exception as exc:
                self._append_log("BUILD_EXCEL_EXPORT_ERROR", "", str(exc))

        history_snapshot = _platform_history_snapshot([name for name, *_ in platform_steps])
        feishu_targets = _feishu_sync_target_platforms(history_snapshot.get("platforms") or [])

        feishu_sync_attempted = False
        feishu_sync_ok = False
        feishu_sync_error = ""
        feishu_sync_result = None
        config = load_saved_config()
        if not all_selected_platforms_succeeded:
            feishu_sync_result = _feishu_skip_result(
                "本次所选平台未全部成功，已跳过自动同步飞书，避免覆盖失败平台的历史记录。",
                platforms=selected_platforms,
            )
        elif not merge_ok:
            feishu_sync_result = _feishu_skip_result("多平台结果合并失败，已跳过飞书同步。", platforms=selected_platforms)
        elif not feishu_targets:
            feishu_sync_result = _feishu_skip_result("本次没有可用于飞书同步的平台结果，已跳过飞书同步。", platforms=selected_platforms)
        elif not config.get("feishu_enabled"):
            feishu_sync_result = _feishu_skip_result("飞书同步未启用，已跳过自动同步。", platforms=feishu_targets)
        elif not feishu_config_ready(config):
            feishu_sync_result = _feishu_skip_result("飞书配置未完成，已跳过自动同步。", platforms=feishu_targets)
        elif not config.get("feishu_auto_sync"):
            feishu_sync_result = _feishu_skip_result("未开启采集后自动同步飞书，已跳过本次同步。", platforms=feishu_targets)
        else:
            try:
                feishu_sync_result = self._run_feishu_sync(
                    config,
                    min_date=query.get("min_date", [""])[0],
                    max_date=query.get("max_date", [""])[0],
                    platforms=feishu_targets,
                )
                feishu_sync_attempted = bool(feishu_sync_result.get("attempted"))
                feishu_sync_ok = bool(feishu_sync_result.get("ok")) if feishu_sync_attempted else False
            except Exception as exc:
                feishu_sync_attempted = True
                feishu_sync_ok = False
                feishu_sync_error = str(exc)
                self._append_log("FEISHU_AUTO_SYNC_ERROR", "", feishu_sync_error)

        duration = round(time.time() - start, 2)
        ended_at = _format_run_time()
        history_entry = _build_run_history_entry(
            raw_mode="run_all",
            requested_mode=query.get("run_mode", [""])[0],
            min_date=query.get("min_date", [""])[0],
            max_date=query.get("max_date", [""])[0],
            started_at=started_at,
            ended_at=ended_at,
            duration=duration,
            merge_ok=merge_ok,
            platform_snapshot=history_snapshot,
            feishu_attempted=feishu_sync_attempted,
            feishu_ok=feishu_sync_ok,
            feishu_result=feishu_sync_result,
            feishu_error=feishu_sync_error,
        )
        _append_run_history_entry(history_entry)

        result_payload = {
            "ok": history_entry["ok"],
            "run_ok": history_entry["ok"],
            "status": history_entry["status"],
            "run_stage_status": history_entry["run_stage_status"],
            "failed_stage": history_entry["failed_stage"],
            "duration": duration,
            "douyin_ok": platform_results.get("douyin", False),
            "xhs_ok": platform_results.get("xiaohongshu", False),
            "bili_ok": platform_results.get("bilibili", False),
            "kuaishou_ok": platform_results.get("kuaishou", False),
            "merge_ok": merge_ok,
            "feishu_sync_attempted": feishu_sync_attempted,
            "feishu_sync_ok": feishu_sync_ok,
            "feishu": history_entry["feishu"],
            "run": history_entry,
        }
        if not history_entry["ok"]:
            result_payload["message"] = _run_failure_message(history_entry)
            _http_status, response_ok, error_code = _run_response_meta(history_entry, "run_all_failed")
            result_payload["ok"] = response_ok
            result_payload["error"] = error_code
        return result_payload

    def _run_all_async_worker(self, query, *, start: float, started_at: str, lease_run_id: str) -> None:
        try:
            self._execute_run_all(query, start=start, started_at=started_at, run_id=lease_run_id)
        except Exception as exc:
            error_text = str(exc) or repr(exc)
            try:
                self._append_log(
                    "RUN_ALL_ASYNC_FATAL",
                    "",
                    f"query={query}\nerror={error_text}\n{traceback.format_exc()}",
                )
            except Exception:
                pass
            try:
                self._finalize_run_all_worker_failure(query, started_at, start, error_text)
            except Exception:
                pass
        finally:
            unlock(lease_run_id)

    def _execute_auth_single(self, platform: str, script: str, progress_path: str, query: dict, *, start: float) -> dict:
        stdout = ""
        stderr = ""
        manual_reauth_backup_dir = ""
        manual_reauth_channel = ""
        previous_auth_status = _sanitize_auth_status(_persisted_auth_snapshot(platform).get("status"))
        try:
            manual_reauth_channel = _sanitize_browser_channel(query.get("browser_channel", [None])[0]) or _configured_browser_channel()
            stopped_pids = _prepare_profile_for_launch(platform, manual_reauth_channel)
            if stopped_pids:
                self._append_log(
                    f"AUTH_SINGLE_{platform.upper()}_PROFILE_PROCESS_CLEANUP",
                    f"platform={platform}\nchannel={manual_reauth_channel}\npids={','.join(str(pid) for pid in stopped_pids)}\n",
                    "",
                )
            manual_reauth_backup_dir = _backup_platform_profile(platform, manual_reauth_channel, reason="manual_reauth")
            if manual_reauth_backup_dir:
                self._append_log(
                    f"AUTH_SINGLE_{platform.upper()}_PROFILE_RESET",
                    f"platform={platform}\nchannel={manual_reauth_channel}\nbackup={manual_reauth_backup_dir}\n",
                    f"[runner] 手动授权已切换到全新 {_platform_label(platform)} profile，避免复用旧登录态导致窗口秒退。\n",
                )
                _prime_platform_progress(
                    platform,
                    progress_path,
                    phase="login",
                    message=f"正在启动全新 {_platform_label(platform)} 扫码授权",
                )
            _bootstrap_ok, bootstrap_message = self._bootstrap_auth_profile_if_needed(
                platform,
                manual_reauth_channel,
                progress_path,
                log_title=f"AUTH_SINGLE_{platform.upper()}_PROFILE_BOOTSTRAP",
            )
            ok, stdout, stderr = self._run_auth_process_with_profile_recovery(
                platform,
                script,
                query,
                progress_path,
                initial_log_title=f"AUTH_SINGLE_{platform.upper()}",
                retry_log_title=f"AUTH_SINGLE_{platform.upper()}_RETRY",
            )
            ok, stdout, stderr = self._stabilize_auth_only_result(
                platform,
                script,
                progress_path,
                query,
                ok=ok,
                stdout=stdout,
                stderr="\n".join(part for part in (stderr, "" if _bootstrap_ok else bootstrap_message) if part),
                verify_log_title=f"AUTH_SINGLE_{platform.upper()}_VERIFY",
            )
            restore_previous_profile = (
                not ok
                and manual_reauth_backup_dir
                and previous_auth_status == "authorized"
                and _profile_backup_has_entries(manual_reauth_backup_dir)
            )
            if restore_previous_profile:
                restored_profile = _restore_platform_profile(platform, manual_reauth_channel, manual_reauth_backup_dir)
                if restored_profile:
                    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                    _patch_progress_state(
                        progress_path,
                        platform,
                        {
                            "status": "completed",
                            "phase": "completed",
                            "message": f"{_platform_label(platform)} 重新授权未完成，已恢复原登录态",
                            "finishedAt": now,
                            "auth_status": previous_auth_status or "authorized",
                            "auth_reason": "",
                            "needs_auth": previous_auth_status not in {"authorized"},
                            "manual_reauth_restored": previous_auth_status in {"authorized"},
                        },
                    )
                    stdout = "\n".join(
                        part
                        for part in (
                            stdout,
                            f"[runner] 已恢复原 {_platform_label(platform)} 授权 profile：{restored_profile}",
                        )
                        if part
                    )
                    stderr = "\n".join(
                        part
                        for part in (
                            stderr,
                            f"[runner] {_platform_label(platform)} 重新授权未完成，已恢复原登录态，避免原可用授权被覆盖。",
                        )
                        if part
                    )
                    _mark_auth_health_pending(platform)
                    ok = True
            elif not ok and manual_reauth_backup_dir:
                stderr = "\n".join(
                    part
                    for part in (
                        stderr,
                        f"[runner] {_platform_label(platform)} 重新授权未完成，但旧授权态并非可恢复的 authorized profile，保留当前 profile 以便继续排查。",
                    )
                    if part
                )
        except Exception as exc:
            self._append_log(f"AUTH_SINGLE_{platform.upper()}_ERROR", "", str(exc))
            stderr = str(exc)
            ok = False

        duration = round(time.time() - start, 2)
        _finalize_platform_progress(
            platform,
            progress_path,
            ok=ok,
            stdout=stdout,
            stderr=stderr,
            auth_only=True,
            success_message="授权完成",
            failure_message="授权未完成",
        )
        _sync_platform_auth_state_from_progress(platform, progress_path)
        if ok:
            _mark_auth_health_pending(platform)
            if _sanitize_auth_status((load_auth_state().get(platform) or {}).get("status")) == "authorized":
                _prune_platform_profile_backups(platform, manual_reauth_channel)
        progress = read_json_file(progress_path, default_progress(platform))
        enabled_platforms = [item for item in (load_saved_config().get("enabled_platforms") or []) if isinstance(item, str)]
        decorated = _decorate_progress(platform, progress, enabled_platforms)
        return {
            "ok": ok and decorated.get("auth_status") == "authorized",
            "platform": platform,
            "duration": duration,
            "progress": decorated,
            "stdout": stdout,
            "stderr": stderr,
        }

    def _run_auth_single_async_worker(self, platform: str, script: str, progress_path: str, query: dict, *, start: float) -> None:
        try:
            self._execute_auth_single(platform, script, progress_path, query, start=start)
        except Exception as exc:
            error_text = str(exc) or repr(exc)
            try:
                self._append_log(
                    f"AUTH_SINGLE_{platform.upper()}_ASYNC_FATAL",
                    "",
                    f"platform={platform}\nquery={query}\nerror={error_text}\n{traceback.format_exc()}",
                )
            except Exception:
                pass
            try:
                _finalize_platform_progress(
                    platform,
                    progress_path,
                    ok=False,
                    stderr=error_text,
                    auth_only=True,
                    failure_message="授权未完成",
                )
                _sync_platform_auth_state_from_progress(platform, progress_path)
            except Exception:
                pass
        finally:
            unlock_auth(platform)

    def _resolve_auth_all_steps(self, query: dict) -> list[tuple[str, str, str]]:
        steps = [
            ("douyin", RUN_SCRIPT, DOUYIN_PROGRESS_FILE),
            ("xiaohongshu", RUN_XHS_SCRIPT, XHS_PROGRESS_FILE),
            ("bilibili", RUN_BILI_SCRIPT, BILI_PROGRESS_FILE),
            ("kuaishou", RUN_KS_SCRIPT, KS_PROGRESS_FILE),
        ]
        enabled = _enabled_platform_scope(load_saved_config())
        allowed = set(resolve_requested_targets(query, enabled))
        return [item for item in steps if item[0] in allowed]

    def _prepare_auth_profile_for_ui(
        self,
        platform_id: str,
        browser_channel: str,
        progress_path: str,
    ) -> tuple[bool, str]:
        stopped_pids = _prepare_profile_for_launch(platform_id, browser_channel)
        if stopped_pids:
            self._append_log(
                f"AUTH_UI_{platform_id.upper()}_PROFILE_PROCESS_CLEANUP",
                f"platform={platform_id}\nchannel={browser_channel}\npids={','.join(str(pid) for pid in stopped_pids)}\n",
                "",
            )
        return self._bootstrap_auth_profile_if_needed(
            platform_id,
            browser_channel,
            progress_path,
            log_title=f"AUTH_UI_{platform_id.upper()}_PROFILE_BOOTSTRAP",
        )

    def _wait_for_auth_profile_prewarm(self, future, lease_run_id: str) -> tuple[bool, str]:
        while True:
            try:
                return future.result(timeout=5)
            except FutureTimeoutError:
                try:
                    _RUN_LEASE_STORE.heartbeat(lease_run_id)
                except Exception:
                    pass

    def _execute_auth_all(self, query: dict, *, start: float, lease_run_id: str) -> dict:
        auth_query = {key: list(values) for key, values in query.items()}
        auth_query["auth_only"] = ["true"]
        auth_query.setdefault("scan_wait_ms", ["300000"])
        probe_query = {key: list(values) for key, values in auth_query.items()}
        probe_query["headless"] = ["true"]
        probe_query["scan_wait_ms"] = ["5000"]
        ui_query = {key: list(values) for key, values in auth_query.items()}
        ui_query["headless"] = ["false"]

        steps = self._resolve_auth_all_steps(query)
        if not steps:
            return {"ok": False, "error": "no_platform_selected", "duration": round(time.time() - start, 2)}

        results = {}
        need_ui_steps = []
        for name, script, progress_path in steps:
            try:
                _RUN_LEASE_STORE.heartbeat(lease_run_id)
            except Exception:
                pass
            if not os.path.exists(script):
                results[f"{name}_ok"] = False
                results[f"{name}_skipped"] = False
                continue

            persisted_status = _sanitize_auth_status(_persisted_auth_snapshot(name).get("status")) or "unauthorized"
            if persisted_status != "authorized":
                _prime_platform_progress(name, progress_path, phase="login", message="准备启动扫码授权")
                need_ui_steps.append((name, script, progress_path))
                continue

            _prime_platform_progress(name, progress_path, phase="login", message="正在检查可复用授权")
            stdout = ""
            stderr = ""
            try:
                probe_ok, stdout, stderr = self._run_auth_process_with_profile_recovery(
                    name,
                    script,
                    probe_query,
                    progress_path,
                    initial_log_title=f"AUTH_PROBE_{name.upper()}",
                    retry_log_title=f"AUTH_PROBE_{name.upper()}_RETRY",
                )
                probe_ok, stdout, stderr = self._stabilize_auth_only_result(
                    name,
                    script,
                    progress_path,
                    probe_query,
                    ok=probe_ok,
                    stdout=stdout,
                    stderr=stderr,
                    verify_log_title=f"AUTH_PROBE_{name.upper()}_VERIFY",
                )
                _finalize_platform_progress(
                    name,
                    progress_path,
                    ok=probe_ok,
                    stdout=stdout,
                    stderr=stderr,
                    auth_only=True,
                    success_message="授权完成",
                    failure_message="授权探测未通过",
                )
                _sync_platform_auth_state_from_progress(name, progress_path)
                if probe_ok:
                    _prune_platform_profile_backups(name, _configured_browser_channel())
                    results[f"{name}_ok"] = True
                    results[f"{name}_skipped"] = True
                    continue
            except Exception as exc:
                self._append_log(f"AUTH_PROBE_{name.upper()}_ERROR", "", str(exc))
                _finalize_platform_progress(
                    name,
                    progress_path,
                    ok=False,
                    stderr=str(exc),
                    auth_only=True,
                    failure_message="授权探测未通过",
                )
                _sync_platform_auth_state_from_progress(name, progress_path)

            need_ui_steps.append((name, script, progress_path))

        browser_channel = _sanitize_browser_channel(ui_query.get("browser_channel", [None])[0]) or _configured_browser_channel()
        prewarm_executor = None
        prewarm_futures = {}
        if need_ui_steps:
            prewarm_executor = ThreadPoolExecutor(
                max_workers=len(need_ui_steps),
                thread_name_prefix="auth_profile_prewarm",
            )
            for name, _script, progress_path in need_ui_steps:
                prewarm_futures[name] = prewarm_executor.submit(
                    self._prepare_auth_profile_for_ui,
                    name,
                    browser_channel,
                    progress_path,
                )

        try:
            for name, script, progress_path in need_ui_steps:
                try:
                    _RUN_LEASE_STORE.heartbeat(lease_run_id)
                except Exception:
                    pass
                _prime_platform_progress(name, progress_path, phase="login", message="准备启动扫码授权")
                stdout = ""
                stderr = ""
                try:
                    _bootstrap_ok, bootstrap_message = self._wait_for_auth_profile_prewarm(
                        prewarm_futures[name],
                        lease_run_id,
                    )
                    ui_ok, stdout, stderr = self._run_auth_process_with_profile_recovery(
                        name,
                        script,
                        ui_query,
                        progress_path,
                        initial_log_title=f"AUTH_UI_{name.upper()}",
                        retry_log_title=f"AUTH_UI_{name.upper()}_RETRY",
                    )
                    ui_ok, stdout, stderr = self._stabilize_auth_only_result(
                        name,
                        script,
                        progress_path,
                        ui_query,
                        ok=ui_ok,
                        stdout=stdout,
                        stderr="\n".join(part for part in (stderr, "" if _bootstrap_ok else bootstrap_message) if part),
                        verify_log_title=f"AUTH_UI_{name.upper()}_VERIFY",
                    )
                    results[f"{name}_ok"] = ui_ok
                    results[f"{name}_skipped"] = False
                    _finalize_platform_progress(
                        name,
                        progress_path,
                        ok=ui_ok,
                        stdout=stdout,
                        stderr=stderr,
                        auth_only=True,
                        success_message="授权完成",
                        failure_message="授权未完成",
                    )
                    _sync_platform_auth_state_from_progress(name, progress_path)
                    if ui_ok and _sanitize_auth_status((load_auth_state().get(name) or {}).get("status")) == "authorized":
                        _mark_auth_health_pending(name)
                        _prune_platform_profile_backups(name, browser_channel)
                except Exception as exc:
                    self._append_log(f"AUTH_UI_{name.upper()}_ERROR", "", str(exc))
                    results[f"{name}_ok"] = False
                    results[f"{name}_skipped"] = False
                    _finalize_platform_progress(
                        name,
                        progress_path,
                        ok=False,
                        stderr=str(exc),
                        auth_only=True,
                        failure_message="授权未完成",
                    )
                    _sync_platform_auth_state_from_progress(name, progress_path)
        finally:
            if prewarm_executor is not None:
                prewarm_executor.shutdown(wait=True, cancel_futures=True)

        enabled_platforms = [item for item in (load_saved_config().get("enabled_platforms") or []) if isinstance(item, str)]
        for name, _script, progress_path in steps:
            progress = read_json_file(progress_path, default_progress(name))
            decorated = _decorate_progress(name, progress, enabled_platforms)
            if decorated.get("auth_status") in {"authorized", "unauthorized", "expired", "needs_auth"}:
                _sync_platform_auth_state_from_progress(name, progress_path)

        duration = round(time.time() - start, 2)
        ok = any(results.get(f"{name}_ok") for name, *_ in steps)
        return {
            "ok": ok,
            "duration": duration,
            "platforms": [name for name, *_ in steps],
            **results,
        }

    def _run_auth_all_async_worker(self, query: dict, *, start: float, lease_run_id: str) -> None:
        try:
            self._execute_auth_all(query, start=start, lease_run_id=lease_run_id)
        except Exception as exc:
            error_text = str(exc) or repr(exc)
            try:
                self._append_log("AUTH_ALL_ASYNC_FATAL", "", f"query={query}\nerror={error_text}\n{traceback.format_exc()}")
            except Exception:
                pass
        finally:
            unlock(lease_run_id)

    def _build_env(self, query, *, platform_id, progress_path, is_xhs=False):
        return _build_env(query, platform_id=platform_id, progress_path=progress_path, is_xhs=is_xhs)

    def _append_log(self, title, stdout, stderr):
        _append_log(title, stdout, stderr)

    def _run_script(self, command, env, timeout=SCRIPT_TIMEOUT, *, requires_user_session: bool = False):
        return _run_script(command, env, timeout=timeout, requires_user_session=requires_user_session)

    def _run_platform_script(
        self,
        command,
        env,
        *,
        platform_id: str,
        progress_path: str,
        run_id: str,
    ):
        supervised_command = list(command)
        if (
            os.name != "nt"
            and len(supervised_command) == 1
            and supervised_command[0].endswith(".sh")
        ):
            supervised_command = ["/bin/bash", supervised_command[0]]
        log_path = os.path.join(DOWNLOADS_DIR, "runs", run_id, f"{platform_id}.log")
        return run_supervised(
            supervised_command,
            env=_scrub_supervision_env(env),
            cwd=BASE_DIR,
            log_path=log_path,
            progress_path=progress_path,
            inactivity_timeout=PLATFORM_INACTIVITY_TIMEOUT,
            heartbeat=lambda: _RUN_LEASE_STORE.heartbeat(run_id),
        )

    def _get_proc_output(self, proc):
        return _get_proc_output(proc)

    def _bootstrap_auth_profile_if_needed(
        self,
        platform_id: str,
        browser_channel: str,
        progress_path: str,
        *,
        log_title: str,
    ) -> tuple[bool, str]:
        profile_dir = _resolve_user_data_dir(platform_id, browser_channel)
        if not _profile_needs_bootstrap(profile_dir):
            return True, ""

        ensure_runtime_dirs()
        os.makedirs(profile_dir, exist_ok=True)
        _prime_platform_progress(
            platform_id,
            progress_path,
            phase="login",
            message=f"正在准备 {_platform_label(platform_id)} 授权浏览器环境",
        )

        env = _scrub_supervision_env(os.environ.copy())
        env["PATH"] = _effective_subprocess_path(env.get("PATH", ""))
        node_bin = _resolve_node_bin_for_env(env)
        if not node_bin:
            message = "[runner] 无法找到 Node.js，授权浏览器预热已跳过。"
            self._append_log(log_title, "", message)
            return False, message

        env["NODE_BIN"] = node_bin
        env["PLAYWRIGHT_BROWSERS_PATH"] = PLAYWRIGHT_BROWSERS_DIR
        env["BROWSER_CHANNEL"] = browser_channel
        browser_executable = _resolve_browser_executable(browser_channel)
        if browser_executable:
            env["BROWSER_EXECUTABLE_PATH"] = browser_executable
        else:
            env.pop("BROWSER_EXECUTABLE_PATH", None)
        env["USER_DATA_DIR"] = profile_dir

        # 客户机上全新空 profile 首次图形化授权可能秒退；预热失败时清理 profile
        # 目录重试一次，再不行才放弃。开发机一般一次就过，重试只在异常环境触发。
        proc = self._run_script([node_bin, PROFILE_SEED_SCRIPT], env, requires_user_session=True)
        stdout, stderr = self._get_proc_output(proc)
        self._append_log(log_title, stdout, stderr)
        if proc.returncode == 0 and _profile_seed_is_usable(profile_dir):
            return True, ""

        # 首次预热失败（退出码非0，或退出码0但 profile 没真正写入文件——某些
        # 打包环境下 Chromium 启动即崩但进程退出码仍为0）：清理后重试一次。
        first_message = stderr or stdout or "[runner] 授权浏览器预热未生效。"
        try:
            _remove_tree_if_exists(profile_dir, allowed_root=AUTH_DIR)
        except Exception:
            pass
        os.makedirs(profile_dir, exist_ok=True)
        self._append_log(
            f"{log_title}_RETRY",
            f"[runner] 首次预热未生效（{first_message.strip()[:200]}），已清理 profile 重新预热一次。",
            "",
        )
        proc = self._run_script([node_bin, PROFILE_SEED_SCRIPT], env, requires_user_session=True)
        stdout, stderr = self._get_proc_output(proc)
        self._append_log(f"{log_title}_RETRY", stdout, stderr)
        if proc.returncode == 0 and _profile_seed_is_usable(profile_dir):
            return True, ""

        message = stderr or stdout or "[runner] 授权浏览器预热失败。"
        return False, message

    def _run_auth_process_once(self, platform_id: str, script: str, query: dict, progress_path: str, *, log_title: str):
        env = self._build_env(query, platform_id=platform_id, progress_path=progress_path)
        requires_user_session = (
            _to_bool(_query_value(query, "auth_only", False))
            and not _to_bool(_query_value(query, "headless", True))
        )
        proc = self._run_script([script], env, requires_user_session=requires_user_session)
        stdout, stderr = self._get_proc_output(proc)
        self._append_log(log_title, stdout, stderr)
        return proc.returncode == 0, stdout, stderr

    def _run_auth_process_with_profile_recovery(
        self,
        platform_id: str,
        script: str,
        query: dict,
        progress_path: str,
        *,
        initial_log_title: str,
        retry_log_title: str,
    ):
        ok, stdout, stderr = self._run_auth_process_once(
            platform_id,
            script,
            query,
            progress_path,
            log_title=initial_log_title,
        )
        if ok or not _auth_profile_launch_failure(stdout, stderr):
            return ok, stdout, stderr

        browser_channel = _sanitize_browser_channel(query.get("browser_channel", [None])[0]) or _configured_browser_channel()
        backup_dir = _backup_platform_profile(platform_id, browser_channel, reason="startup_failed")
        if not backup_dir:
            return ok, stdout, stderr

        self._append_log(
            f"{retry_log_title}_PROFILE_BACKUP",
            f"platform={platform_id}\nchannel={browser_channel}\nbackup={backup_dir}\n",
            "",
        )
        _prime_platform_progress(
            platform_id,
            progress_path,
            phase="login",
            message="检测到授权目录异常，已自动重建并重试一次登录窗口",
        )
        _bootstrap_ok, bootstrap_message = self._bootstrap_auth_profile_if_needed(
            platform_id,
            browser_channel,
            progress_path,
            log_title=f"{retry_log_title}_PROFILE_BOOTSTRAP",
        )
        retry_ok, retry_stdout, retry_stderr = self._run_auth_process_once(
            platform_id,
            script,
            query,
            progress_path,
            log_title=retry_log_title,
        )
        merged_stdout = "\n".join(part for part in (stdout, retry_stdout) if part)
        merged_stderr = "\n".join(
            part
            for part in (
                stderr,
                f"[runner] 已自动备份损坏的授权目录到: {backup_dir}",
                "" if _bootstrap_ok else bootstrap_message,
                retry_stderr,
            )
            if part
        )
        return retry_ok, merged_stdout, merged_stderr

    def _stabilize_auth_only_result(
        self,
        platform_id: str,
        script: str,
        progress_path: str,
        query: dict,
        *,
        ok: bool,
        stdout: str = "",
        stderr: str = "",
        verify_log_title: str,
    ):
        if not _to_bool(_query_value(query, "auth_only", False)):
            return ok, stdout, stderr
        if not _to_bool(_query_value(query, "headless", False)):
            return ok, stdout, stderr

        progress = read_json_file(progress_path, default_progress(platform_id))
        verify_query = _build_auth_verification_query(query)
        verify_ok, verify_stdout, verify_stderr = self._run_auth_process_once(
            platform_id,
            script,
            verify_query,
            progress_path,
            log_title=verify_log_title,
        )
        merged_stdout = "\n".join(part for part in (stdout, verify_stdout) if part)
        merged_stderr = "\n".join(part for part in (stderr, verify_stderr) if part)

        if ok and verify_ok:
            if not _auth_flow_has_explicit_success(progress):
                _mark_auth_flow_completed(platform_id, progress_path)
                merged_stderr = "\n".join(
                    part
                    for part in (
                        merged_stderr,
                        "[runner] 授权脚本已成功退出，已通过 AUTH_ONLY 复核确认授权成功。",
                    )
                    if part
                )
            return True, merged_stdout, merged_stderr

        if verify_ok:
            _mark_auth_flow_completed(platform_id, progress_path)
            merged_stderr = "\n".join(
                part
                for part in (
                    merged_stderr,
                    "[runner] 授权窗口异常结束后，已通过登录态复核确认授权成功。",
                )
                if part
            )
            return True, merged_stdout, merged_stderr
        if ok and _auth_verify_launch_failure_can_preserve_success(progress, query, verify_stdout, verify_stderr):
            _mark_auth_flow_completed(platform_id, progress_path)
            merged_stderr = "\n".join(
                part
                for part in (
                    merged_stderr,
                    "[runner] UI 授权已写入成功；后续 headless 复核仅因启动失败未通过，当前授权态将保留。",
                )
                if part
            )
            return True, merged_stdout, merged_stderr
        if ok:
            merged_stderr = "\n".join(
                part
                for part in (
                    merged_stderr,
                    "[runner] 授权脚本已成功退出，但登录态复核未通过。",
                )
                if part
            )
        return False, merged_stdout, merged_stderr

    def _cleanup_auth_probe_files(self, progress_path: str) -> None:
        for target in (progress_path, _stable_progress_snapshot_path(progress_path)):
            try:
                os.remove(target)
            except FileNotFoundError:
                continue
            except Exception:
                pass

    def _probe_live_auth_before_run(self, platform_id: str, progress_path: str, query: dict) -> dict:
        info = _run_platform_auth_gate_info(platform_id, progress_path)
        if info.get("blocked"):
            return info
        if platform_id not in LIVE_AUTH_PRECHECK_PLATFORMS:
            return info
        if _to_bool(_query_value(query, "auth_only", False)):
            return info
        if not _to_bool(_query_value(query, "headless", True)):
            return info

        script = (AUTH_SINGLE_PLATFORM_MAP.get(platform_id) or ("", ""))[0]
        if not script:
            return info

        probe_query = _build_auth_verification_query(query)
        probe_progress_path = f"{progress_path}.probe.json"
        self._cleanup_auth_probe_files(probe_progress_path)

        try:
            ok, stdout, stderr = self._run_auth_process_with_profile_recovery(
                platform_id,
                script,
                probe_query,
                probe_progress_path,
                initial_log_title=f"RUN_PREFLIGHT_{platform_id.upper()}_AUTH_PROBE",
                retry_log_title=f"RUN_PREFLIGHT_{platform_id.upper()}_AUTH_PROBE_RETRY",
            )
        except Exception as exc:
            ok = False
            stdout = ""
            stderr = str(exc)
            self._append_log(f"RUN_PREFLIGHT_{platform_id.upper()}_AUTH_PROBE_ERROR", "", stderr)

        try:
            _finalize_platform_progress(
                platform_id,
                probe_progress_path,
                ok=ok,
                stdout=stdout,
                stderr=stderr,
                auth_only=True,
                success_message="授权探测通过",
                failure_message="授权探测未通过",
            )
            _sync_platform_auth_state_from_progress(platform_id, probe_progress_path)
        finally:
            self._cleanup_auth_probe_files(probe_progress_path)

        return _run_platform_auth_gate_info(platform_id, progress_path)

    def _refresh_live_auth_state_before_run(self, path: str, query: dict) -> None:
        if is_locked():
            return
        for platform_id, progress_path in _resolve_requested_run_targets(path, query):
            try:
                self._probe_live_auth_before_run(platform_id, progress_path, query)
            except Exception as exc:
                self._append_log(f"RUN_PREFLIGHT_{platform_id.upper()}_ERROR", "", str(exc))

    def _blocked_run_request_payload(self, path: str, query: dict) -> dict | None:
        self._refresh_live_auth_state_before_run(path, query)
        return _blocked_run_request_payload(path, query)


    def _prepare_excel_export_file(self, which: str, requested_platforms: list[str]) -> tuple[str, dict | None]:
        enriched_map = {
            "all": ENRICHED_ALL_DATA_FILE,
            **ENRICHED_PLATFORM_FILES,
        }
        raw_map = {
            "all": ALL_DATA_FILE,
            "douyin": DATA_FILE,
            "xiaohongshu": XHS_DATA_FILE,
            "bilibili": BILI_DATA_FILE,
            "kuaishou": KS_DATA_FILE,
        }
        if which == "all" and requested_platforms:
            config = load_saved_config()
            excel_cmd = [
                PYTHON_BIN,
                BUILD_EXCEL_SCRIPT,
                "--mode",
                "all",
                "--platforms",
                ",".join(requested_platforms),
                "--output",
                ENRICHED_ALL_DATA_FILE,
            ]
            chosen_min_date = str(config.get("min_publish_date") or DEFAULT_CONFIG["min_publish_date"]).strip()
            if chosen_min_date:
                excel_cmd.extend(["--min-date", chosen_min_date])
            rebuild_proc = self._run_script(excel_cmd, os.environ.copy(), timeout=180)
            rebuild_stdout, rebuild_stderr = self._get_proc_output(rebuild_proc)
            self._append_log("DOWNLOAD_EXCEL_REBUILD", rebuild_stdout, rebuild_stderr)
            if rebuild_proc.returncode != 0:
                return "", {
                    "ok": False,
                    "error": "download_excel_rebuild_failed",
                    "message": "按当前平台配置生成 Excel 失败，请先完成一次同步后再导出。",
                    "detail": _process_failure_detail(
                        rebuild_stdout,
                        rebuild_stderr,
                        "请查看 DOWNLOAD_EXCEL_REBUILD 日志。",
                    ),
                }

        # 单平台：enriched 与原始文件都不存在时按需重建（与 all 相同的
        # 重建路径），避免采集后从未生成过单平台文件时导出 404。
        if which != "all":
            enriched_target = enriched_map.get(which, "")
            raw_target = raw_map.get(which, "")
            if enriched_target and not os.path.exists(enriched_target) and not os.path.exists(raw_target):
                config = load_saved_config()
                excel_cmd = [
                    PYTHON_BIN,
                    BUILD_EXCEL_SCRIPT,
                    "--mode",
                    which,
                    "--output",
                    enriched_target,
                ]
                chosen_min_date = str(config.get("min_publish_date") or DEFAULT_CONFIG["min_publish_date"]).strip()
                if chosen_min_date:
                    excel_cmd.extend(["--min-date", chosen_min_date])
                rebuild_proc = self._run_script(excel_cmd, os.environ.copy(), timeout=180)
                rebuild_stdout, rebuild_stderr = self._get_proc_output(rebuild_proc)
                self._append_log("DOWNLOAD_EXCEL_REBUILD", rebuild_stdout, rebuild_stderr)

        target = enriched_map.get(which, "")
        if not target or not os.path.exists(target):
            target = raw_map.get(which, "")
        if not target or not os.path.isfile(target):
            return "", {
                "ok": False,
                "error": "excel_not_ready",
                "message": "暂时没有可导出的 Excel，请先完成一次数据采集。",
            }
        if not _is_valid_xlsx_file(target):
            return "", {
                "ok": False,
                "error": "excel_file_invalid",
                "message": "Excel 文件校验失败，请重新完成一次数据采集。",
            }
        return target, None

    def do_GET(self):
        parsed = urlparse(self.path)
        if not self._require_request_security():
            return

        if parsed.path == "/data":
            self._handle_data(parsed, DATA_FILE)
            return

        if parsed.path == "/xhs-data":
            self._handle_data(parsed, XHS_DATA_FILE)
            return

        if parsed.path == "/bili-data":
            self._handle_data(parsed, BILI_DATA_FILE)
            return

        if parsed.path == "/ks-data":
            self._handle_data(parsed, KS_DATA_FILE)
            return

        if parsed.path == "/all-data":
            self._handle_data(parsed, ALL_DATA_FILE)
            return

        # --- 文件下载端点 ---
        if parsed.path == "/download-excel":
            qs = parse_qs(parsed.query)

            which = str(qs.get("file", ["all"])[0] or "all").strip()
            if which not in {"all", *VALID_PLATFORM_IDS}:
                self._send_json(
                    400,
                    {"ok": False, "error": "excel_scope_invalid", "message": "Excel 数据范围无效。"},
                )
                return
            requested_platforms = _normalize_platform_ids(_query_value(qs, "platforms", ""))
            if which != "all":
                requested_platforms = []
            target, error = self._prepare_excel_export_file(which, requested_platforms)
            if error:
                self._send_json(500, error)
                return
            self._send_file(
                target,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                as_download=True,
            )

            return

        if parsed.path.startswith("/assets/"):
            rel_path = parsed.path.lstrip("/")
            frontend_root = os.path.join(BASE_DIR, "frontend")
            target_path = os.path.normpath(os.path.join(frontend_root, rel_path))
            assets_root = os.path.join(frontend_root, "assets")
            if not (
                target_path == assets_root
                or target_path.startswith(assets_root + os.sep)
            ):
                self._send_json(403, {"ok": False, "error": "forbidden"})
                return
            cache_control = None
            if target_path.endswith(".css"):
                cache_control = "no-store, no-cache, must-revalidate, max-age=0"
            self._send_file(target_path, cache_control=cache_control)
            return

        if parsed.path == "/progress":
            self._handle_progress()
            return

        if parsed.path == "/session/recover":
            # Allow the frontend to recover the session token when the URL
            # lost its #session hash (e.g., browser restored an old tab,
            # file:// redirect stripped the hash, double-click race, etc.).
            # Only returns the token for requests from localhost origin.
            if _is_tauri_supervised():
                self._send_json(403, {"ok": False, "error": "forbidden"})
                return
            if not self._is_loopback_client():
                self._send_json(403, {"ok": False, "error": "forbidden"})
                return
            origin = self._request_origin()
            origin_allowed = origin in self._allowed_browser_origins()
            legacy_allowed = not origin
            if SESSION_TOKEN and (origin_allowed or legacy_allowed):
                self._send_json(200, {"ok": True, "token": SESSION_TOKEN})
            else:
                self._send_json(403, {"ok": False, "error": "forbidden"})
            return

        if parsed.path == SUPERVISED_HEALTH_PATH:
            info = current_package_info(BASE_DIR)
            self._send_json(
                200,
                {
                    "ok": True,
                    "package_id": str(info.get("package_id") or ""),
                    "build_version": str(info.get("build_version") or ""),
                    "port": int(getattr(self.server, "server_port", 0) or 0),
                },
            )
            return

        if parsed.path == "/package-info":
            info = current_package_info(BASE_DIR)
            self._send_json(200, {"ok": True, **info})
            return

        if parsed.path == "/update/check":
            try:
                self._send_json(200, check_for_update(BASE_DIR))
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": "update_check_failed", "message": str(exc)})
            return

        if parsed.path == "/update/download-progress":
            try:
                self._send_json(200, get_download_progress(STATE_DIR))
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": "update_progress_failed", "message": str(exc)})
            return

        if parsed.path == "/config":
            self._handle_get_config()
            return

        if parsed.path == "/feishu/cli-status":
            self._handle_feishu_cli_status()
            return

        if parsed.path in ("/", "/monitor"):
            self._send_file(
                MONITOR_HTML,
                "text/html; charset=utf-8",
                cache_control="no-store, no-cache, must-revalidate, max-age=0",
            )
            return

        if parsed.path == "/license":
            if LICENSE_BYPASS_ENABLED:
                self._send_json(200, {
                    "ok": True,
                    "activated": True,
                    "valid": True,
                    "access_mode": "community",
                    "license_key_masked": "COMMUNITY",
                    "customer_name": "本地测试",
                    "trial": {},
                    "info": {
                        "status": "community",
                        "access_mode": "community",
                        "message": "社区版本地访问已启用",
                    },
                })
                return
            phase, ok, info = _license_access_for_request()
            access_mode = (info or {}).get("access_mode") or ("license" if ok else "none")
            license_payload = {
                "ok": True,
                "activated": LICENSE_MGR.is_activated(),
                "valid": ok,
                "access_mode": access_mode,
                "license_key_masked": _mask_license_key(LICENSE_MGR.get_license_key()),
                "customer_name": LICENSE_MGR.get_customer_name() or (info or {}).get("customer_name", ""),
                "trial": (info or {}).get("trial", {}),
                "info": info,
            }
            if _is_tauri_supervised():
                license_payload["checking"] = phase != "done"
            self._send_json(200, license_payload)
            return

        if parsed.path == "/analytics":
            try:
                dashboard = compute_dashboard(DB_PATH)
                self._send_json(200, dashboard)
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return

        if parsed.path == "/analytics/history":
            query = parse_qs(parsed.query)
            try:
                limit = int(query.get("limit", ["30"])[0])
            except ValueError:
                limit = 30
            try:
                explicit_runs = [_normalize_run_history_entry(item) for item in _read_run_history()[: max(limit, 0)]]
                explicit_runs = [item for item in explicit_runs if item]
                if explicit_runs:
                    self._send_json(200, {"ok": True, "runs": explicit_runs})
                    return
                runs = list_recent_runs(DB_PATH, limit=limit)
                self._send_json(200, {
                    "ok": True,
                    "runs": [_history_from_analytics_run(r) for r in runs],
                })
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return

        self._send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if not self._require_request_security():
            return
        if parsed.path not in (
            "/run",
            "/run_xhs",
            "/run_bili",
            "/run_ks",
            "/run_all",
            "/auth_all",
            "/auth_single",
            "/auth_revoke_single",
            "/config",
            "/config/test_feishu",
            "/export-excel",
            "/reset_onboarding",
            "/sync_feishu",
            "/unlock",
            "/license/activate",
            "/feishu/cli-init",
            "/feishu/cli-auth",
            "/feishu/cli-auth-poll",
            "/feishu/cli-create-base",
            "/feishu/cli-save",
            "/feishu/cli-reset",
            "/feishu/parse-bitable-url",
            "/feedback/send",
            "/update/download",
            "/update/install",
            "/update/reveal",
        ):
            self._send_json(404, {"ok": False, "error": "not_found"})
            return

        if parsed.path == "/export-excel":
            try:
                payload = self._read_json_body()
                which = str(payload.get("file") or "all").strip()
                if which not in {"all", *VALID_PLATFORM_IDS}:
                    self._send_json(
                        400,
                        {"ok": False, "error": "excel_scope_invalid", "message": "Excel 数据范围无效。"},
                    )
                    return
                requested_platforms = _normalize_platform_ids(payload.get("platforms") or [])
                if which != "all":
                    requested_platforms = []
                target, prepare_error = self._prepare_excel_export_file(which, requested_platforms)
                if prepare_error:
                    self._send_json(500, prepare_error)
                    return

                dialog_result = _run_excel_save_dialog(os.path.basename(target))
                if dialog_result.get("cancelled"):
                    self._send_json(200, {"ok": True, "cancelled": True, "message": "已取消保存"})
                    return
                if not dialog_result.get("ok"):
                    self._append_log(
                        "EXPORT_EXCEL_SAVE_DIALOG_ERROR",
                        "",
                        str(dialog_result.get("detail") or dialog_result.get("error") or "unknown"),
                    )
                    self._send_json(
                        500,
                        {
                            "ok": False,
                            "error": str(dialog_result.get("error") or "excel_save_dialog_failed"),
                            "message": str(dialog_result.get("message") or "打开 Excel 保存窗口失败，请稍后重试。"),
                        },
                    )
                    return

                saved_path = _save_excel_to_selected_path(target, str(dialog_result.get("path") or ""))
                self._append_log("EXPORT_EXCEL_SAVED", saved_path, "")
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "cancelled": False,
                        "path": saved_path,
                        "file_name": os.path.basename(saved_path),
                    },
                )
            except Exception as exc:
                self._append_log("EXPORT_EXCEL_ERROR", "", str(exc))
                self._send_json(
                    500,
                    {
                        "ok": False,
                        "error": "excel_export_failed",
                        "message": "Excel 保存失败，请重新选择位置后再试。",
                    },
                )
            return

        # --- Lark CLI endpoints (no lock required) ---
        if parsed.path == "/feishu/cli-init":
            self._handle_feishu_cli_init()
            return
        if parsed.path == "/feishu/cli-auth":
            self._handle_feishu_cli_auth()
            return
        if parsed.path == "/feishu/cli-auth-poll":
            self._handle_feishu_cli_auth_poll()
            return
        if parsed.path == "/feishu/cli-create-base":
            self._handle_feishu_cli_create_base()
            return
        if parsed.path == "/feishu/cli-save":
            self._handle_feishu_cli_save()
            return
        if parsed.path == "/feishu/cli-reset":
            self._handle_feishu_cli_reset()
            return
        if parsed.path == "/feishu/parse-bitable-url":
            self._handle_feishu_parse_url()
            return

        if parsed.path == "/update/download":
            try:
                payload = self._read_json_body()
                latest = payload.get("latest") if isinstance(payload.get("latest"), dict) else payload
                self._send_json(200, download_update(STATE_DIR, latest or {}, base_dir=BASE_DIR))
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": "update_download_failed", "message": str(exc)})
            return

        if parsed.path == "/update/reveal":
            try:
                payload = self._read_json_body()
                self._send_json(200, reveal_path(str(payload.get("path") or "")))
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": "update_reveal_failed", "message": str(exc)})
            return

        if parsed.path == "/update/install":
            try:
                self._read_json_body()
                self._send_json(200, install_downloaded_update(STATE_DIR, base_dir=BASE_DIR))
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": "update_install_failed", "message": str(exc)})
            return

        if parsed.path == "/feedback/send":
            try:
                payload = self._read_json_body()
                config = load_saved_config()
                self._send_json(
                    200,
                    send_feedback(
                        BASE_DIR,
                        message=str(payload.get("message") or ""),
                        customer_name=str(config.get("customer_name") or ""),
                        workspace_name=str(config.get("workspace_name") or ""),
                        page_path=str(payload.get("page_path") or ""),
                        user_agent=str(payload.get("user_agent") or ""),
                        license_customer_name=LICENSE_MGR.get_customer_name(),
                    ),
                )
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": "feedback_send_failed", "message": str(exc)})
            return

        if parsed.path == "/license/activate":
            try:
                payload = self._read_json_body()
                key = payload.get("license_key", "").strip()
                if not key:
                    self._send_json(400, {"ok": False, "error": "请输入许可证密钥"})
                    return
                phase, ok, msg = _activate_license_key_for_request(key)
                if phase == "pending":
                    self._send_json(409, {
                        "ok": False,
                        "error": "license_check_pending",
                        "message": "正在后台验证许可证，请稍候。",
                    })
                    return
                if ok:
                    self._send_json(200, {
                        "ok": True,
                        "message": msg,
                        "customer_name": LICENSE_MGR.get_customer_name(),
                        "license_key": LICENSE_MGR.get_license_key(),
                    })
                else:
                    self._send_json(400, {"ok": False, "error": msg})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return

        if parsed.path == "/config":
            try:
                payload = self._read_json_body()
                self._send_json(200, self._save_config_payload(payload))
            except Exception as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            return

        if parsed.path == "/config/test_feishu":
            try:
                payload = self._read_json_body()
                result = self._test_feishu_connection(payload)
                self._send_json(200, result)
            except Exception as exc:
                detail = str(exc)
                self._send_json(
                    400,
                    {
                        "ok": False,
                        "error": "feishu_test_failed",
                        "message": _friendly_feishu_error_message(detail, action="test"),
                        "detail": detail,
                    },
                )
            return

        if parsed.path == "/reset_onboarding":
            lease_token = None
            try:
                payload = self._read_json_body()
                clear_auth = _to_bool((payload or {}).get("clear_auth", False))
                with RUN_MUTEX:
                    if active_auth_locks() or is_locked():
                        self._send_json(409, {"ok": False, "error": "already_running", "message": "同步或授权运行中，暂时不能重置初始化。"})
                        return
                    lease_token = lock(kind="reset_onboarding")
                    if lease_token is None:
                        self._send_json(409, {"ok": False, "error": "already_running", "message": "同步或授权运行中，暂时不能重置初始化。"})
                        return
                self._send_json(200, reset_onboarding_state(clear_auth=clear_auth))
            except Exception as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            finally:
                if lease_token is not None:
                    unlock(lease_token.run_id)
            return

        if parsed.path == "/auth_revoke_single":
            lease_token = None
            try:
                platform = parse_qs(parsed.query).get("platform", [None])[0]
                if platform not in VALID_PLATFORM_IDS:
                    raise ValueError(f"invalid_platform: {platform}")
                with RUN_MUTEX:
                    if active_auth_locks() or is_locked():
                        self._send_json(409, {"ok": False, "error": "already_running", "message": "同步、授权或登录态检查运行中，暂时不能取消授权。"})
                        return
                    lease_token = lock(kind="auth_revoke")
                    if lease_token is None:
                        self._send_json(409, {"ok": False, "error": "already_running", "message": "同步、授权或登录态检查运行中，暂时不能取消授权。"})
                        return
                self._send_json(200, revoke_platform_auth(platform))
            except Exception as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            finally:
                if lease_token is not None:
                    unlock(lease_token.run_id)
            return

        # Manual unlock endpoint
        if parsed.path == "/unlock":
            lease_payload = _RUN_LEASE_STORE.read_payload()
            if str((lease_payload or {}).get("kind") or "") == "auth_health":
                self._send_json(409, {
                    "ok": False,
                    "error": "auth_health_running",
                    "message": "登录态检查即将完成，请稍后再释放任务锁。",
                })
                return
            unlock(force=True)
            clear_auth_locks()
            self._send_json(200, {"ok": True, "message": "lock_released"})
            return

        query = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path == "/auth_single":
            platform = query.get("platform", [None])[0]
            if platform not in AUTH_SINGLE_PLATFORM_MAP:
                self._send_json(400, {"ok": False, "error": "invalid_platform", "valid": list(AUTH_SINGLE_PLATFORM_MAP.keys())})
                return

            script, progress_path = AUTH_SINGLE_PLATFORM_MAP[platform]
            if not os.path.exists(script):
                self._send_json(404, {"ok": False, "error": "script_not_found", "platform": platform})
                return

            runtime_error = _collector_runtime_preflight_error()
            if runtime_error:
                self._send_json(503, {
                    "ok": False,
                    "error": runtime_error,
                    "platform": platform,
                    "message": "采集运行依赖尚未安装，请在项目目录执行 npm install 后重新授权。",
                })
                return

            with RUN_MUTEX:
                if is_locked():
                    self._send_json(409, {
                        "ok": False,
                        "error": "already_running",
                        "message": "当前正在采集、同步或一键授权，请等待完成后再授权。",
                    })
                    return
                auth_platforms = active_auth_locks()
                if auth_platforms:
                    self._send_json(409, {
                        "ok": False,
                        "error": "auth_already_running" if platform in auth_platforms else "auth_running",
                        "message": "当前已有平台授权窗口正在运行，请完成授权后再操作。",
                        "auth_running_platforms": auth_platforms,
                    })
                    return
                lock_auth(platform)

            auth_start = time.time()
            try:
                auth_query = {key: list(values) for key, values in query.items()}
                auth_query["auth_only"] = ["true"]
                auth_query["headless"] = ["false"]
                auth_query.setdefault("scan_wait_ms", ["300000"])

                self._append_log(f"AUTH_SINGLE_{platform.upper()}_START", f"platform={platform}\n", "")
                _prime_platform_progress(platform, progress_path, phase="login", message="准备启动授权流程")
                worker = threading.Thread(
                    target=self._run_auth_single_async_worker,
                    args=(platform, script, progress_path, auth_query),
                    kwargs={"start": auth_start},
                    name=f"auth_single_{platform}_{int(auth_start * 1000)}",
                    daemon=True,
                )
                worker.start()
            except Exception as exc:
                unlock_auth(platform)
                self._send_json(500, {"ok": False, "error": "auth_single_start_failed", "message": str(exc) or repr(exc)})
                return

            self._send_json(
                202,
                {
                    "ok": True,
                    "accepted": True,
                    "platform": platform,
                    "status": "authorizing",
                    "message": f"{_platform_label(platform)} 授权窗口已启动，请在浏览器里完成登录后返回此页查看状态。",
                },
            )
            return

        if parsed.path in {"/run_all", "/auth_all"}:
            try:
                resolve_requested_targets(
                    query,
                    _enabled_platform_scope(load_saved_config()),
                )
            except ValueError as exc:
                error = str(exc)
                self._send_json(
                    400,
                    {
                        "ok": False,
                        "error": error.split(":", 1)[0],
                        "message": error,
                    },
                )
                return

        auth_platforms = active_auth_locks()
        if auth_platforms:
            self._send_json(409, {
                "ok": False,
                "error": "auth_running",
                "message": "当前有平台授权窗口正在运行，请完成授权后再采集、同步或一键授权。",
                "auth_running_platforms": auth_platforms,
            })
            return
        blocked_payload = self._blocked_run_request_payload(parsed.path, query)
        if blocked_payload:
            self._send_json(409, blocked_payload)
            return

        lease_token = None
        lock_held = False
        release_lock_on_exit = True
        with RUN_MUTEX:
            auth_platforms = active_auth_locks()
            if auth_platforms:
                self._send_json(409, {
                    "ok": False,
                    "error": "auth_running",
                    "message": "当前有平台授权窗口正在运行，请完成授权后再采集、同步或一键授权。",
                    "auth_running_platforms": auth_platforms,
                })
                return
            if is_locked():
                self._send_json(409, {
                    "ok": False,
                    "error": "already_running",
                    "message": "当前已有授权或采集任务在运行，请等待完成后再试。",
                })
                return
            lease_token = lock(kind=parsed.path.lstrip("/") or "post")
            if lease_token is None:
                self._send_json(409, {
                    "ok": False,
                    "error": "already_running",
                    "message": "当前已有授权或采集任务在运行，请等待完成后再试。",
                })
                return
            lock_held = True

        start = time.time()
        started_at = _format_run_time(start)
        single_history_recorded = False
        try:
            if parsed.path == "/sync_feishu":
                config = load_saved_config()
                preflight = _manual_feishu_sync_preflight(config)
                if preflight.get("blocked"):
                    self._send_json(
                        int(preflight.get("http_status") or 409),
                        {
                            "ok": False,
                            "error": preflight.get("error") or "feishu_sync_blocked",
                            "message": preflight.get("message") or "当前无法执行飞书同步。",
                        },
                    )
                    return
                chosen_min_date = query.get("min_date", [""])[0]
                chosen_max_date = query.get("max_date", [""])[0]
                chosen_platforms = _resolved_platform_scope(query, config)
                force_full_sync = _to_bool(query.get("force_full_sync", [""])[0])
                sync_error = ""
                sync_result = None
                history_ok = False
                http_status = 200
                try:
                    sync_result = self._run_feishu_sync(
                        config,
                        min_date=chosen_min_date,
                        max_date=chosen_max_date,
                        platforms=chosen_platforms,
                        force_full_sync=force_full_sync,
                    )
                    history_ok = bool(sync_result.get("ok"))
                except Exception as exc:
                    sync_error = str(exc)
                    self._append_log("FEISHU_MANUAL_SYNC_ERROR", "", sync_error)
                    http_status = 409 if (_feishu_cli_requires_user_auth(sync_error) or _feishu_cli_requires_document_app_permission(sync_error)) else 400

                ended_at = _format_run_time()
                duration = round(time.time() - start, 2)
                history_items, history_entry = _reconcile_feishu_history_after_manual_sync(
                    _read_run_history(),
                    min_date=chosen_min_date,
                    max_date=chosen_max_date,
                    ok=history_ok,
                    result=sync_result,
                    error=sync_error,
                    synced_at=ended_at,
                    duration=duration,
                    config=config,
                )
                _write_run_history(history_items[:100])

                if history_ok:
                    response_message = str(sync_result.get("message") or "").strip() if isinstance(sync_result, dict) else ""
                    payload = {
                        **sync_result,
                        "run": history_entry,
                        "status": history_entry["status"],
                        "run_stage_status": history_entry["run_stage_status"],
                        "failed_stage": history_entry["failed_stage"],
                        "message": response_message or "飞书同步已完成。",
                    }
                    self._send_json(200, payload)
                    return

                self._send_json(
                    http_status,
                    {
                        "ok": False,
                        "error": "feishu_permission_required" if _feishu_cli_requires_document_app_permission(sync_error) else ("feishu_user_auth_required" if _feishu_cli_requires_user_auth(sync_error) else "feishu_sync_failed"),
                        "message": history_entry.get("feishu", {}).get("error") or _friendly_feishu_error_message(sync_error, action="sync"),
                        "detail": sync_error,
                        "feishu_cli": ({key: value for key, value in _copy_lark_cli_state().items() if key != "app_secret"} if _feishu_cli_requires_user_auth(sync_error) else {}),
                        "run": history_entry,
                        "status": history_entry.get("status"),
                        "run_stage_status": history_entry.get("run_stage_status"),
                        "failed_stage": history_entry.get("failed_stage"),
                    },
                )
                return

            if parsed.path == "/auth_all":
                async_query = {key: list(values) for key, values in query.items()}
                steps = self._resolve_auth_all_steps(async_query)
                if not steps:
                    self._send_json(400, {"ok": False, "error": "no_platform_selected", "message": "请先至少选择一个需要授权的平台。"})
                    return
                release_lock_on_exit = False
                worker = threading.Thread(
                    target=self._run_auth_all_async_worker,
                    args=(async_query,),
                    kwargs={"start": start, "lease_run_id": lease_token.run_id},
                    name=f"auth_all_{int(start * 1000)}",
                    daemon=True,
                )
                worker.start()
                self._send_json(
                    202,
                    {
                        "ok": True,
                        "accepted": True,
                        "status": "running",
                        "platforms": [name for name, *_ in steps],
                        "message": "已开始按所选平台逐个拉起授权窗口；每完成一个平台后会继续下一个。",
                    },
                )
                return

            if parsed.path == "/run_all":
                try:
                    resolved_min_date, resolved_max_date = _resolve_query_date_window(
                        query,
                        default_min_date=load_saved_config().get("min_publish_date") or DEFAULT_CONFIG["min_publish_date"],
                    )
                except ValueError as exc:
                    self._send_json(400, {"ok": False, "error": "invalid_date_range", "message": str(exc)})
                    return
                query["min_date"] = [resolved_min_date]
                if resolved_max_date:
                    query["max_date"] = [resolved_max_date]
                else:
                    query.pop("max_date", None)
                async_query = {key: list(values) for key, values in query.items()}
                accepted_platforms = self._prime_run_all_targets(async_query)
                release_lock_on_exit = False
                worker = threading.Thread(
                    target=self._run_all_async_worker,
                    args=(async_query,),
                    kwargs={
                        "start": start,
                        "started_at": started_at,
                        "lease_run_id": lease_token.run_id,
                    },
                    name=f"run_all_{int(start * 1000)}",
                    daemon=True,
                )
                worker.start()
                self._send_json(
                    202,
                    {
                        "ok": True,
                        "accepted": True,
                        "status": "running",
                        "run_stage_status": "platform_scraping",
                        "failed_stage": "",
                        "message": "同步任务已启动，可直接刷新页面查看实时状态。",
                        "platforms": accepted_platforms,
                        "min_date": async_query.get("min_date", [""])[0],
                        "max_date": async_query.get("max_date", [""])[0],
                        "mode": async_query.get("run_mode", ["incremental"])[0] or "incremental",
                    },
                )
                return

            is_xhs = parsed.path == "/run_xhs"
            is_bili = parsed.path == "/run_bili"
            is_ks = parsed.path == "/run_ks"
            single_platform = (
                "bilibili"
                if is_bili
                else (
                    "kuaishou"
                    if is_ks
                    else ("xiaohongshu" if is_xhs else "douyin")
                )
            )
            single_progress_path = (
                BILI_PROGRESS_FILE
                if is_bili
                else (
                    KS_PROGRESS_FILE
                    if is_ks
                    else (XHS_PROGRESS_FILE if is_xhs else DOUYIN_PROGRESS_FILE)
                )
            )
            preflight = _preflight_platform_run(single_platform, single_progress_path)
            if preflight.get("blocked"):
                duration = round(time.time() - start, 2)
                ended_at = _format_run_time()
                history_snapshot = _platform_history_snapshot([single_platform])
                history_entry = _build_run_history_entry(
                    raw_mode="single_platform",
                    requested_mode="single_platform",
                    min_date=query.get("min_date", [""])[0],
                    max_date=query.get("max_date", [""])[0],
                    started_at=started_at,
                    ended_at=ended_at,
                    duration=duration,
                    merge_ok=True,
                    platform_snapshot=history_snapshot,
                    feishu_attempted=False,
                    feishu_ok=False,
                )
                _append_run_history_entry(history_entry)
                single_history_recorded = True
                self._append_log(
                    f"RUN_{single_platform.upper()}_BLOCKED_AUTH",
                    "skip run because authorization is required\n"
                    f"auth_status={preflight.get('auth_status')}\n"
                    f"auth_reason={preflight.get('auth_reason')}\n"
                    f"message={preflight.get('message')}\n",
                    "",
                )
                self._send_json(
                    409,
                    {
                        "ok": False,
                        "status": history_entry["status"],
                        "run_stage_status": history_entry["run_stage_status"],
                        "failed_stage": history_entry["failed_stage"],
                        "duration": duration,
                        "error": "auth_required",
                        "message": _run_failure_message(history_entry),
                        "run": history_entry,
                    },
                )
                return

            single_script = (
                RUN_BILI_SCRIPT
                if is_bili
                else (
                    RUN_KS_SCRIPT
                    if is_ks
                    else (RUN_XHS_SCRIPT if is_xhs else RUN_SCRIPT)
                )
            )
            single_result = self._execute_run_all_platform_step(
                (single_platform, single_script, single_progress_path, is_xhs),
                query,
                lease_token.run_id,
            )
            duration = round(time.time() - start, 2)

            if single_result.outcome != "success":
                # 采集脚本失败也要记一笔历史，否则用户在历史记录里查不到这次失败
                # （与授权拦截路径、run_all 保持一致，不能只 send_json 就 return）
                single_fail_history = _build_run_history_entry(
                    raw_mode="single_platform",
                    requested_mode="single_platform",
                    min_date=query.get("min_date", [""])[0],
                    max_date=query.get("max_date", [""])[0],
                    started_at=started_at,
                    ended_at=_format_run_time(),
                    duration=duration,
                    merge_ok=False,
                    platform_snapshot=_platform_history_snapshot([single_platform]),
                    feishu_attempted=False,
                    feishu_ok=False,
                )
                _append_run_history_entry(single_fail_history)
                single_history_recorded = True
                self._send_json(
                    500,
                    {
                        "ok": False,
                        "duration": duration,
                        "error": single_result.outcome or "run_failed",
                        "message": single_result.error_message,
                    },
                )
                return

            # Single-platform run should refresh the global aggregate from all
            # available platform exports, otherwise later manual Feishu sync
            # reads an all-channels file that only contains the last platform.
            merge_env = os.environ.copy()
            merge_env["PYTHON_BIN"] = PYTHON_BIN
            merge_env["MIN_PUBLISH_DATE"] = query.get("min_date", [DEFAULT_CONFIG["min_publish_date"]])[0]
            if query.get("max_date", [""])[0]:
                merge_env["MAX_PUBLISH_DATE"] = query.get("max_date", [""])[0]
            merge_proc = self._run_script([PYTHON_BIN, MERGE_CHANNELS_SCRIPT], merge_env)
            m_stdout, m_stderr = self._get_proc_output(merge_proc)
            self._append_log("MERGE_ALL_CHANNELS", m_stdout, m_stderr)
            if merge_proc.returncode != 0:
                # 合并失败同样要记历史，原因标为 merge 失败
                single_merge_fail_history = _build_run_history_entry(
                    raw_mode="single_platform",
                    requested_mode="single_platform",
                    min_date=query.get("min_date", [""])[0],
                    max_date=query.get("max_date", [""])[0],
                    started_at=started_at,
                    ended_at=_format_run_time(),
                    duration=duration,
                    merge_ok=False,
                    platform_snapshot=_platform_history_snapshot([single_platform]),
                    feishu_attempted=False,
                    feishu_ok=False,
                )
                _append_run_history_entry(single_merge_fail_history)
                single_history_recorded = True
                self._send_json(500, {"ok": False, "duration": duration, "error": "merge_all_channels_failed"})
                return

            # Auto-archive snapshot for analytics
            try:
                archive_snapshot_from_excel(ALL_DATA_FILE, DB_PATH)
            except Exception:
                pass

            # Build enriched Excel export for this platform + all (non-blocking)
            try:
                excel_cmd_all = [PYTHON_BIN, BUILD_EXCEL_SCRIPT, "--mode", "all"]
                excel_cmd_single = [PYTHON_BIN, BUILD_EXCEL_SCRIPT, "--mode", single_platform]
                sp_min_date = query.get("min_date", [DEFAULT_CONFIG["min_publish_date"]])[0]
                sp_max_date = query.get("max_date", [""])[0]
                for cmd in (excel_cmd_all, excel_cmd_single):
                    if sp_min_date:
                        cmd.extend(["--min-date", sp_min_date])
                    if sp_max_date:
                        cmd.extend(["--max-date", sp_max_date])
                excel_proc = self._run_script(excel_cmd_all, os.environ.copy(), timeout=120)
                e_stdout, e_stderr = self._get_proc_output(excel_proc)
                self._append_log("BUILD_EXCEL_EXPORT_ALL", e_stdout, e_stderr)
                excel_proc2 = self._run_script(excel_cmd_single, os.environ.copy(), timeout=120)
                e_stdout2, e_stderr2 = self._get_proc_output(excel_proc2)
                self._append_log("BUILD_EXCEL_EXPORT_SINGLE", e_stdout2, e_stderr2)
            except Exception as exc:
                self._append_log("BUILD_EXCEL_EXPORT_ERROR", "", str(exc))

            config = load_saved_config()
            history_snapshot = _platform_history_snapshot([single_platform])
            # Auto sync after a single-platform run should preserve the user's
            # current enabled-platform scope instead of shrinking Feishu to the
            # last platform and deleting other platform rows.
            feishu_targets = _resolved_platform_scope(query, config) or _feishu_sync_target_platforms(history_snapshot.get("platforms") or [])

            feishu_sync_attempted = False
            feishu_sync_ok = False
            feishu_sync_error = ""
            feishu_sync_result = None
            if not feishu_targets:
                feishu_sync_result = _feishu_skip_result("本次没有可用于飞书同步的平台结果，已跳过飞书同步。", platforms=[single_platform])
            elif not config.get("feishu_enabled"):
                feishu_sync_result = _feishu_skip_result("飞书同步未启用，已跳过自动同步。", platforms=feishu_targets)
            elif not feishu_config_ready(config):
                feishu_sync_result = _feishu_skip_result("飞书配置未完成，已跳过自动同步。", platforms=feishu_targets)
            elif not config.get("feishu_auto_sync"):
                feishu_sync_result = _feishu_skip_result("未开启采集后自动同步飞书，已跳过本次同步。", platforms=feishu_targets)
            else:
                try:
                    feishu_sync_result = self._run_feishu_sync(
                        config,
                        min_date=query.get("min_date", [""])[0],
                        max_date=query.get("max_date", [""])[0],
                        platforms=feishu_targets,
                    )
                    feishu_sync_attempted = bool(feishu_sync_result.get("attempted"))
                    feishu_sync_ok = bool(feishu_sync_result.get("ok")) if feishu_sync_attempted else False
                except Exception as exc:
                    feishu_sync_attempted = True
                    feishu_sync_ok = False
                    feishu_sync_error = str(exc)
                    self._append_log("FEISHU_AUTO_SYNC_ERROR", "", feishu_sync_error)

            ended_at = _format_run_time()
            history_entry = _build_run_history_entry(
                raw_mode="single_platform",
                requested_mode="single_platform",
                min_date=query.get("min_date", [""])[0],
                max_date=query.get("max_date", [""])[0],
                started_at=started_at,
                ended_at=ended_at,
                duration=duration,
                merge_ok=True,
                platform_snapshot=history_snapshot,
                feishu_attempted=feishu_sync_attempted,
                feishu_ok=feishu_sync_ok,
                feishu_result=feishu_sync_result,
                feishu_error=feishu_sync_error,
            )
            _append_run_history_entry(history_entry)
            single_history_recorded = True

            payload = {
                "ok": history_entry["ok"],
                "run_ok": history_entry["ok"],
                "status": history_entry["status"],
                "run_stage_status": history_entry["run_stage_status"],
                "failed_stage": history_entry["failed_stage"],
                "duration": duration,
                "run": history_entry,
            }
            if feishu_sync_attempted:
                payload["feishu_sync_attempted"] = True
                payload["feishu_sync_ok"] = feishu_sync_ok
                payload["feishu"] = history_entry["feishu"]
            if not history_entry["ok"]:
                payload["message"] = _run_failure_message(history_entry)
                http_status, response_ok, error_code = _run_response_meta(history_entry, "run_failed")
                payload["ok"] = response_ok
                payload["error"] = error_code
                self._send_json(http_status, payload)
                return
            self._send_json(200, payload)
        except Exception as exc:
            if parsed.path in RUN_ROUTE_PLATFORM_MAP and not single_history_recorded:
                try:
                    _append_single_platform_exception_history(
                        RUN_ROUTE_PLATFORM_MAP[parsed.path],
                        query,
                        started_at=started_at,
                        start=start,
                        error=str(exc) or repr(exc),
                    )
                    single_history_recorded = True
                except Exception as history_exc:
                    try:
                        self._append_log("RUN_HISTORY_FATAL", "", repr(history_exc))
                    except Exception:
                        pass
            try:
                self._append_log(
                    "POST_FATAL",
                    "",
                    f"path={parsed.path}\nquery={parsed.query}\nerror={repr(exc)}\n",
                )
            except Exception:
                pass
            self._send_json(
                500,
                {
                    "ok": False,
                    "error": "runner_post_failed",
                    "path": parsed.path,
                    "message": str(exc) or repr(exc),
                },
            )
        finally:
            if lock_held and release_lock_on_exit:
                unlock(lease_token.run_id if lease_token else "")

    def do_OPTIONS(self):
        if not self._is_allowed_origin():
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(204)
        self._set_cors_headers()
        self.end_headers()


def _format_http_host(host: str) -> str:
    value = str(host or "").strip()
    if ":" in value and not value.startswith("["):
        return f"[{value}]"
    return value


def runner_listen_url(port: int | None = None) -> str:
    selected_port = RUNNER_PORT if port is None else int(port)
    return f"http://{_format_http_host(RUNNER_HOST)}:{selected_port}"


def _bind_runner_server(host: str, preferred_port: int, handler, *, allow_fallback: bool = True):
    if not _is_loopback_host(host):
        raise ValueError("runner host must be a loopback address")
    try:
        return ThreadingHTTPServer((host, int(preferred_port)), handler)
    except OSError as exc:
        address_in_use = exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) in (
            # 10048 = WSAEADDRINUSE；Windows 的 SO_REUSEADDR 语义下，
            # 绑定已被监听的端口也可能表现为 10013 (WSAEACCES)。
            10048,
            10013,
        )
        if not allow_fallback or int(preferred_port) == 0 or not address_in_use:
            raise
    return ThreadingHTTPServer((host, 0), handler)


def _runner_ready_frame(server) -> str:
    info = current_package_info(BASE_DIR)
    payload = {
        "event": "ready",
        "port": int(server.server_port),
        "package_id": str(info.get("package_id") or ""),
        "build_version": str(info.get("build_version") or ""),
    }
    return RUNNER_READY_PREFIX + json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    )


def _runner_process_command(pid: int) -> str:
    from core.process import process_command
    return process_command(pid)


def _is_current_product_runner_command(command: str) -> bool:
    text = str(command or "").replace("\\", "/")
    expected = os.path.realpath(BASE_DIR).replace("\\", "/")
    return bool(
        expected in text
        and any(marker in text for marker in ("/scripts/_run.py", "/scripts/runner.py", "runner.cpython-"))
    )


def _kill_stale_runner_processes():
    """Kill leftover runner processes from previous launches.

    Only targets runner/_run.py processes that hold our port, to avoid
    killing unrelated processes.  Gives them 2 s to exit gracefully
    before force kill (Windows: taskkill /T /F on the tree).
    """
    from core.process import port_listener_pids, terminate_pid_tree

    pids = port_listener_pids(RUNNER_PORT)
    my_pid = os.getpid()
    for pid in pids:
        if pid == my_pid:
            continue
        if not _is_current_product_runner_command(_runner_process_command(pid)):
            continue
        terminate_pid_tree(pid)


def _should_cleanup_stale_runners() -> bool:
    return not _is_tauri_supervised()


def _start_server():
    """Entry point callable from compiled launcher (_run.py)."""
    ensure_runtime_dirs()
    if _should_cleanup_stale_runners():
        _kill_stale_runner_processes()
    unlock(force=True)
    clear_auth_locks()

    def _shutdown_on_signal(signum, frame):
        _stop_auth_health_monitor(join_timeout=1.0)
        unlock(force=True)
        clear_auth_locks()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown_on_signal)
    signal.signal(signal.SIGINT, _shutdown_on_signal)

    server = _bind_runner_server(
        RUNNER_HOST,
        RUNNER_PORT,
        Handler,
        allow_fallback=_is_tauri_supervised(),
    )
    if _is_tauri_supervised():
        print(_runner_ready_frame(server), flush=True)
        _start_supervised_license_background()
    else:
        print(f"runner listening on {runner_listen_url(server.server_port)}", flush=True)
    _start_auth_health_monitor()
    try:
        server.serve_forever()
    finally:
        _stop_auth_health_monitor()
        unlock(force=True)
        clear_auth_locks()


if __name__ == "__main__":
    try:
        _start_server()
    except Exception as exc:
        try:
            _ensure_parent_dir(LOG_FILE)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write("\n=== RUNNER_FATAL {} ===\n{}\n".format(time.strftime("%Y-%m-%d %H:%M:%S"), repr(exc)))
        except Exception:
            pass
        raise
