#!/usr/bin/env python3
import argparse
import copy
import glob
import hashlib
import json
import os
import re
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import certifi


ROOT_DIR = Path(__file__).resolve().parents[1]
# Windows 嵌入式 Python（._pth）不把脚本目录加入 sys.path，本地模块需要自举。
_SELF_DIR = os.path.dirname(os.path.abspath(__file__))
if _SELF_DIR not in sys.path:
    sys.path.insert(0, _SELF_DIR)
from runtime_paths import resolve_auth_dir, resolve_downloads_dir, seed_state_from_bundle  # noqa: E402

STATE_DIR = Path(seed_state_from_bundle(ROOT_DIR))
AUTH_DIR = Path(resolve_auth_dir(ROOT_DIR, STATE_DIR))
DEFAULT_ENV_PATH = AUTH_DIR / "feishu.env"
SCHEMA_STATE_FILE = AUTH_DIR / "feishu_schema_state.json"
DEFAULT_PAYLOAD_PATH = Path(resolve_downloads_dir(ROOT_DIR, STATE_DIR)) / "feishu_sync_payload.json"
OPENAPI_BASE = "https://open.feishu.cn/open-apis"
PAGE_SIZE = 500
RECORD_BATCH_SIZE = 500
RECORD_BATCH_SIZE_CLI = 10
CLI_INLINE_JSON_MAX = 4000
CHECKPOINT_VERSION = 1
LARK_CLI_HOME = AUTH_DIR / "lark-cli-home"
LARK_CLI_TEMP_DIR = AUTH_DIR / "lark-cli-tmp"
PROJECT_LARK_CLI_CONFIG_FILE = LARK_CLI_HOME / ".lark-cli" / "config.json"
GLOBAL_LARK_CLI_CONFIG_FILE = Path.home() / ".lark-cli" / "config.json"
LARK_CLI_NPX_PACKAGE = os.getenv("YIRENGONGIS_LARK_CLI_PACKAGE", "@larksuite/cli@1.0.43")
CHART_PLATFORM_SUFFIXES = {"抖音", "小红书", "B站", "快手", "视频号", "公众号"}
CHART_MANAGED_METRICS = {
    "总流量",
    "播放量",
    "点赞量",
    "评论量",
    "分享量",
    "收藏量",
    "涨粉量",
    "平均播放进度",
    "完播率",
    "点赞率",
    "评论率",
    "分享率",
    "收藏率",
    "封标点击率",
    "3s跳出率",
    "总互动量",
    "互动率",
    "播放量_log",
    "互动质量",
}
# 每张表的同步操作总超时（墙钟时间），防止图表表等大表卡死整个同步流程
TABLE_SYNC_TIMEOUT = 300
# 飞书新建多维表格时，连续创建字段可能返回 800004135，且刚创建的字段会延迟可见。
# 控制创建节奏并在限流后回读字段；总等待时间仍受 TABLE_SYNC_TIMEOUT 约束。
CLI_FIELD_CREATE_PACE_SECONDS = 0.5
CLI_FIELD_CREATE_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)
CLI_FIELD_VISIBILITY_RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0)


class SyncTimeoutError(TimeoutError):
    """单表同步超时（POSIX 由 SIGALRM 触发，Windows 由看门狗线程触发）"""
    pass


def _alarm_handler(signum, frame):
    raise SyncTimeoutError("同步超时")


def _run_sync_with_timeout(fn, timeout_seconds):
    """表级同步看门狗。

    POSIX 保留原 SIGALRM 机制（可在阻塞的系统调用中同步打断）；
    Windows 无 SIGALRM，改为工作线程 + join 超时：超时后主流程立即
    以 SyncTimeoutError 中止，工作线程作为 daemon 随进程退出。
    """
    if os.name != "nt":
        signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(timeout_seconds)
        try:
            return fn()
        finally:
            signal.alarm(0)

    outcome = {}

    def _worker():
        try:
            outcome["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 原样转发到调用线程
            outcome["error"] = exc

    worker = threading.Thread(target=_worker, name="feishu-sync-watchdog", daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive():
        raise SyncTimeoutError(f"同步超时（>{timeout_seconds}s）")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("result")


def load_env_file(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def build_ssl_context():
    if os.getenv("FEISHU_OPENAPI_SKIP_SSL_VERIFY", "").lower() in {"1", "true", "yes"}:
        return ssl._create_unverified_context()
    return ssl.create_default_context(cafile=certifi.where())


def resolve_npx_bin() -> str:
    names = ("npx.cmd", "npx.exe", "npx") if os.name == "nt" else ("npx",)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return ""


def _sort_existing_paths(paths: list[str]) -> list[str]:
    return sorted(
        (path for path in paths if path and os.path.exists(path)),
        key=lambda item: os.path.getmtime(item),
        reverse=True,
    )


def resolve_lark_cli_bin() -> str:
    root_dir = Path(__file__).resolve().parents[1]
    ext = ".exe" if os.name == "nt" else ""
    patterns = [
        str(root_dir / "node_modules" / "@larksuite" / "cli" / "bin" / f"lark-cli{ext}"),
        str(root_dir / "node_modules" / ".bin" / f"lark-cli{ext}"),
    ]
    for pattern in patterns:
        matches = _sort_existing_paths(glob.glob(pattern))
        if matches:
            return matches[0]
    return ""


def resolve_lark_cli_runner() -> list[str]:
    cli_bin = resolve_lark_cli_bin()
    if cli_bin:
        return [cli_bin]
    npx = resolve_npx_bin()
    if npx:
        return [npx, "--yes", LARK_CLI_NPX_PACKAGE]
    raise RuntimeError("未找到 lark-cli 或 npx 命令，请确保 Node.js 已安装。")


def use_global_lark_cli_home() -> bool:
    return str(os.getenv("FEISHU_CLI_USE_GLOBAL_HOME") or "").strip().lower() in {"1", "true", "yes", "on"}


def active_lark_cli_home() -> Path:
    return Path.home() if use_global_lark_cli_home() else LARK_CLI_HOME


def active_lark_cli_config_file() -> Path:
    return GLOBAL_LARK_CLI_CONFIG_FILE if use_global_lark_cli_home() else PROJECT_LARK_CLI_CONFIG_FILE


def lark_cli_env() -> dict:
    env = os.environ.copy()
    base_path = env.get("PATH", "")
    default_path = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    env["PATH"] = f"{base_path}:{default_path}" if base_path else default_path
    home = active_lark_cli_home()
    if not use_global_lark_cli_home():
        home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    for proxy_key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(proxy_key, None)
    env["LARK_CLI_NO_PROXY"] = "1"
    return env


def http_json(method: str, url: str, *, token: str | None = None, payload=None):
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60, context=build_ssl_context()) as response:
            result = json.loads(response.read().decode("utf-8"))
            if isinstance(result, dict) and result.get("code") not in (None, 0):
                raise RuntimeError(
                    f"OpenAPI 业务错误 code={result.get('code')} {result.get('msg', '')}\n"
                    f"{json.dumps(result, ensure_ascii=False)}"
                )
            return result
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {url}\n{body}") from exc


def _prepare_cli_temp_dir() -> Path:
    if LARK_CLI_TEMP_DIR.is_symlink():
        raise RuntimeError(f"飞书 CLI 临时目录不能是符号链接：{LARK_CLI_TEMP_DIR}")
    LARK_CLI_TEMP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        LARK_CLI_TEMP_DIR.chmod(0o700)
    except OSError:
        pass

    stale_before = time.time() - 24 * 60 * 60
    for candidate in LARK_CLI_TEMP_DIR.glob(".lark-cli-json-*.json"):
        try:
            if candidate.is_file() and candidate.stat().st_mtime < stale_before:
                candidate.unlink()
        except OSError:
            continue
    return LARK_CLI_TEMP_DIR


def _write_cli_temp_file(text: str) -> tuple[str, Path]:
    cli_cwd = _prepare_cli_temp_dir()
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=".lark-cli-json-",
        suffix=".json",
        dir=str(cli_cwd),
        delete=False,
    ) as temp_file:
        temp_file.write(text)
        abs_path = Path(temp_file.name)
    try:
        abs_path.chmod(0o600)
    except OSError:
        pass
    return f"./{abs_path.name}", abs_path


def cli_http_json(method: str, path: str, *, payload=None, params=None, page_all=False, page_size: int | None = None):
    cmd = resolve_lark_cli_runner() + ["api", method, path, "--as", "user", "--format", "json"]
    temp_files: list[Path] = []
    try:
        if params is not None:
            params_str = json.dumps(params, ensure_ascii=False)
            if len(params_str) > CLI_INLINE_JSON_MAX:
                rel_name, abs_path = _write_cli_temp_file(params_str)
                temp_files.append(abs_path)
                cmd.extend(["--params", f"@{rel_name}"])
            else:
                cmd.extend(["--params", params_str])
        if payload is not None:
            payload_str = json.dumps(payload, ensure_ascii=False)
            if len(payload_str) > CLI_INLINE_JSON_MAX:
                rel_name, abs_path = _write_cli_temp_file(payload_str)
                temp_files.append(abs_path)
                cmd.extend(["--data", f"@{rel_name}"])
            else:
                cmd.extend(["--data", payload_str])
        if page_all:
            cmd.append("--page-all")
        if page_size is not None:
            cmd.extend(["--page-size", str(page_size)])

        cli_cwd = temp_files[0].parent if temp_files else None
        proc = run_lark_cli_with_user_fallback(cmd, timeout=120, cwd=cli_cwd)
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0:
            raise RuntimeError(f"lark-cli 命令失败 (exit {proc.returncode}): {stderr or stdout}")
        if not stdout:
            return {}
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"lark-cli 返回了非 JSON 输出: {stdout}") from exc
    finally:
        for temp_path in temp_files:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass


def is_cli_bot_permission_error_text(text: str) -> bool:
    lowered = str(text or "").lower()
    return (
        "91403" in lowered
        or "you don't have permission" in lowered
        or "need_user_authorization" in lowered
        or "missing required scope" in lowered
        or "missing_scope" in lowered
        or "base:record:delete" in lowered
    )


def _replace_cli_identity(cmd: list[str], identity: str) -> list[str]:
    updated = list(cmd)
    if "--as" in updated:
        index = updated.index("--as")
        if index + 1 < len(updated):
            updated[index + 1] = identity
            return updated
    updated.extend(["--as", identity])
    return updated


def run_lark_cli_with_user_fallback(cmd: list[str], *, timeout: int = 120, cwd: Path | None = None):
    # 捕获子进程超时，转为 RuntimeError 避免未处理的 TimeoutExpired 崩溃
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=lark_cli_env(),
            cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired as exc:
        cmd_summary = " ".join(cmd[:6]) if len(cmd) > 6 else " ".join(cmd)
        raise RuntimeError(f"lark-cli 命令超时 ({timeout}s): {cmd_summary}") from exc
    if proc.returncode == 0:
        return proc
    detail = "\n".join(part for part in ((proc.stderr or "").strip(), (proc.stdout or "").strip()) if part)
    if is_cli_bot_permission_error_text(detail):
        user_cmd = _replace_cli_identity(cmd, "user")
        if user_cmd != cmd:
            try:
                # user fallback 同样需要超时保护
                return subprocess.run(
                    user_cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    env=lark_cli_env(),
                    cwd=str(cwd) if cwd else None,
                )
            except subprocess.TimeoutExpired as exc:
                cmd_summary = " ".join(user_cmd[:6]) if len(user_cmd) > 6 else " ".join(user_cmd)
                raise RuntimeError(f"lark-cli 命令超时 ({timeout}s, user fallback): {cmd_summary}") from exc
    return proc


def cli_base_json(
    subcommand: str,
    *,
    base_token: str,
    table_id: str | None = None,
    json_payload=None,
    limit: int | None = None,
    offset: int | None = None,
    record_id: str | None = None,
    field_id: str | None = None,
    view_id: str | None = None,
    yes: bool = False,
):
    cmd = resolve_lark_cli_runner() + ["base", subcommand, "--as", "user", "--base-token", base_token]
    if table_id:
        cmd.extend(["--table-id", table_id])
    if json_payload is not None:
        cmd.extend(["--json", json.dumps(json_payload, ensure_ascii=False)])
    if limit is not None:
        cmd.extend(["--limit", str(limit)])
    if offset is not None:
        cmd.extend(["--offset", str(offset)])
    if record_id:
        cmd.extend(["--record-id", record_id])
    if field_id:
        cmd.extend(["--field-id", field_id])
    if view_id:
        cmd.extend(["--view-id", view_id])
    if yes:
        cmd.append("--yes")

    proc = run_lark_cli_with_user_fallback(cmd, timeout=120)
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"lark-cli 命令失败 (exit {proc.returncode}): {stderr or stdout}")
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"lark-cli 返回了非 JSON 输出: {stdout}") from exc


def split_cli_markdown_row(line: str) -> list[str]:
    text = str(line or "").strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    cells = []
    current = []
    escaped = False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "|":
            cells.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    cells.append("".join(current).strip())
    return cells


def is_markdown_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(cell and set(cell) <= {"-", ":", " "} for cell in cells)


def parse_cli_record_list_markdown(stdout: str):
    text = str(stdout or "").strip()
    if not text or "Meta:" not in text:
        return None

    table_lines = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
    if len(table_lines) < 2:
        return None

    headers = split_cli_markdown_row(table_lines[0])
    separator = split_cli_markdown_row(table_lines[1])
    if not headers or headers[0] != "_record_id" or not is_markdown_separator_row(separator):
        return None

    field_names = headers[1:]
    rows = []
    record_ids = []
    for line in table_lines[2:]:
        values = split_cli_markdown_row(line)
        if len(values) < len(headers):
            values.extend([""] * (len(headers) - len(values)))
        elif len(values) > len(headers):
            values = values[: len(headers) - 1] + [" | ".join(values[len(headers) - 1:])]
        record_ids.append(str(values[0]).strip())
        rows.append(values[1:len(headers)])

    meta_match = re.search(r"has_more=(true|false)", text, flags=re.IGNORECASE)
    has_more = bool(meta_match and meta_match.group(1).lower() == "true")
    return {
        "data": {
            "fields": field_names,
            "data": rows,
            "record_id_list": record_ids,
            "has_more": has_more,
        }
    }


def cli_record_list_json(base_token: str, table_id: str, *, limit: int, offset: int):
    cmd = resolve_lark_cli_runner() + [
        "base",
        "+record-list",
        "--as",
        "user",
        "--base-token",
        base_token,
        "--table-id",
        table_id,
        "--limit",
        str(limit),
        "--offset",
        str(offset),
    ]
    proc = run_lark_cli_with_user_fallback(cmd, timeout=120)
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"lark-cli 命令失败 (exit {proc.returncode}): {stderr or stdout}")
    if not stdout:
        return {"data": {"fields": [], "data": [], "record_id_list": [], "has_more": False}}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        parsed = parse_cli_record_list_markdown(stdout)
        if parsed is not None:
            return parsed
        raise RuntimeError(f"lark-cli 返回了非 JSON 输出: {stdout}") from exc


def get_tenant_access_token(app_id: str, app_secret: str) -> str:
    result = http_json(
        "POST",
        f"{OPENAPI_BASE}/auth/v3/tenant_access_token/internal",
        payload={"app_id": app_id, "app_secret": app_secret},
    )
    token = result.get("tenant_access_token")
    if not token:
        raise RuntimeError(f"无法获取 tenant_access_token: {json.dumps(result, ensure_ascii=False)}")
    return token


def create_bitable_app(name: str, token: str, *, time_zone: str = "Asia/Shanghai"):
    url = f"{OPENAPI_BASE}/bitable/v1/apps"
    payload = {"name": name, "time_zone": time_zone}
    result = http_json("POST", url, token=token, payload=payload)
    data = result.get("data", result)
    app_data = data.get("app", data) if isinstance(data, dict) else {}
    return {
        "app_token": app_data.get("app_token") or app_data.get("appToken") or app_data.get("token") or "",
        "url": app_data.get("url") or app_data.get("app_url") or app_data.get("appUrl") or "",
        "name": app_data.get("name") or name,
    }


def paged_get(url: str, token: str | None, *, use_cli: bool = False):
    if use_cli:
        path = url.replace(OPENAPI_BASE, "", 1)
        result = cli_http_json("GET", path, params={}, page_all=True, page_size=PAGE_SIZE)
        data = result.get("data", result)
        return data.get("items", [])

    items = []
    page_token = ""
    while True:
        query = "&" if "?" in url else "?"
        target = f"{url}{query}page_size={PAGE_SIZE}"
        if page_token:
            target += f"&page_token={urllib.parse.quote(page_token)}"
        result = http_json("GET", target, token=token)
        data = result.get("data", result)
        items.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token", "")
        if not page_token:
            break
    return items


def list_tables(app_token: str, token: str | None, *, use_cli: bool = False):
    if use_cli:
        result = cli_base_json("+table-list", base_token=app_token, limit=PAGE_SIZE, offset=0)
        data = result.get("data") or {}
        items = data.get("items") or data.get("tables") or []
        normalized = {}
        for item in items:
            name = item.get("table_name") or item.get("name")
            table_id = item.get("table_id") or item.get("id")
            if name and table_id:
                normalized[str(name)] = {"table_id": str(table_id), "name": str(name)}
        return normalized

    url = f"{OPENAPI_BASE}/bitable/v1/apps/{app_token}/tables"
    items = paged_get(url, token, use_cli=False)
    return {item["name"]: item for item in items}


def create_table(app_token: str, token: str | None, table_definition: dict, *, use_cli: bool = False):
    if use_cli:
        result = run_lark_cli_with_user_fallback(
            resolve_lark_cli_runner() + [
                "base",
                "+table-create",
                "--as",
                "user",
                "--base-token",
                app_token,
                "--name",
                table_definition["name"],
            ],
            timeout=120,
        )
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        if result.returncode != 0:
            raise RuntimeError(f"lark-cli 命令失败 (exit {result.returncode}): {stderr or stdout}")
        try:
            created_payload = json.loads(stdout) if stdout else {}
        except json.JSONDecodeError:
            created_payload = {}
        created_table = (created_payload.get("data") or {}).get("table") or {}
        created_table_id = created_table.get("id") or created_table.get("table_id")
        if created_table_id:
            return str(created_table_id)
        table_map = list_tables(app_token, token, use_cli=True)
        table_info = table_map.get(table_definition["name"]) or {}
        return table_info.get("table_id")

    url = f"{OPENAPI_BASE}/bitable/v1/apps/{app_token}/tables"
    payload = {
        "table": {
            "name": table_definition["name"],
            "default_view_name": table_definition.get("default_view_name", "主视图"),
            "fields": [to_openapi_field_definition(field) for field in table_definition["fields"]],
        }
    }
    result = http_json("POST", url, token=token, payload=payload)
    data = result.get("data", result)
    return data.get("table_id")


def cli_list_fields(app_token: str, table_id: str) -> dict[str, dict]:
    result = cli_base_json("+field-list", base_token=app_token, table_id=table_id)
    data = result.get("data") or {}
    items = data.get("fields") or data.get("items") or []
    normalized = {}
    for item in items:
        name = item.get("field_name") or item.get("name")
        if name:
            normalized[str(name)] = item
    return normalized


def cli_create_field(app_token: str, table_id: str, field_definition: dict):
    result = cli_base_json(
        "+field-create",
        base_token=app_token,
        table_id=table_id,
        json_payload=to_cli_field_definition(field_definition),
    )
    data = result.get("data") or {}
    created = data.get("field") or {}
    return created.get("id") or created.get("field_id") or data.get("field_id") or ""


def cli_update_field(app_token: str, table_id: str, existing_field: dict, field_definition: dict):
    field_id = str(existing_field.get("id") or existing_field.get("field_id") or existing_field.get("name") or "").strip()
    if not field_id:
        return {}
    return cli_base_json(
        "+field-update",
        base_token=app_token,
        table_id=table_id,
        field_id=field_id,
        json_payload=to_cli_field_definition(field_definition),
        yes=True,
    )


def cli_delete_field(app_token: str, table_id: str, existing_field: dict):
    field_id = str(existing_field.get("id") or existing_field.get("field_id") or existing_field.get("name") or "").strip()
    if not field_id:
        return {}
    return cli_base_json(
        "+field-delete",
        base_token=app_token,
        table_id=table_id,
        field_id=field_id,
        yes=True,
    )


def cli_delete_records(app_token: str, table_id: str, record_ids: list[str]):
    ids = [str(item).strip() for item in record_ids if str(item).strip()]
    if not ids:
        return {}
    return cli_base_json(
        "+record-delete",
        base_token=app_token,
        table_id=table_id,
        json_payload={"record_id_list": ids},
        yes=True,
    )


def cli_set_visible_fields(app_token: str, table_id: str, table_definition: dict, existing_fields: dict[str, dict]):
    visible_names = table_definition.get("visible_fields") or [
        str(field.get("field_name") or "").strip()
        for field in table_definition.get("fields", [])
    ]
    visible_ids = []
    for field_name in visible_names:
        field_info = existing_fields.get(field_name)
        if not field_info:
            continue
        field_id = str(field_info.get("id") or field_info.get("field_id") or "").strip()
        if field_id:
            visible_ids.append(field_id)
    if not visible_ids:
        return {}
    view_names = [str(table_definition.get("default_view_name") or "").strip(), "Grid View", "主视图"]
    tried = []
    last_error = None
    for view_name in view_names:
        if not view_name or view_name in tried:
            continue
        tried.append(view_name)
        try:
            return cli_base_json(
                "+view-set-visible-fields",
                base_token=app_token,
                table_id=table_id,
                view_id=view_name,
                json_payload={"visible_fields": visible_ids},
            )
        except RuntimeError as exc:
            last_error = exc
            if "not_found" not in str(exc).lower():
                raise
    if last_error:
        raise last_error
    return {}


def list_records(app_token: str, table_id: str, token: str | None, *, use_cli: bool = False):
    if use_cli:
        normalized = []
        offset = 0
        while True:
            result = cli_record_list_json(app_token, table_id, limit=PAGE_SIZE, offset=offset)
            data = result.get("data") or {}
            field_names = data.get("fields") or []
            rows = data.get("data") or []
            record_ids = data.get("record_id_list") or []
            for idx, row in enumerate(rows):
                fields = {}
                for field_idx, field_name in enumerate(field_names):
                    if field_idx < len(row):
                        fields[str(field_name)] = row[field_idx]
                normalized.append({
                    "record_id": record_ids[idx] if idx < len(record_ids) else "",
                    "fields": fields,
                })
            if not data.get("has_more"):
                break
            offset += len(rows)
            if not rows:
                break
        return normalized

    url = f"{OPENAPI_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    return paged_get(url, token, use_cli=False)


def create_record(app_token: str, table_id: str, token: str | None, fields: dict, *, use_cli: bool = False):
    if use_cli:
        return cli_base_json("+record-upsert", base_token=app_token, table_id=table_id, json_payload=fields)

    url = f"{OPENAPI_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records?ignore_consistency_check=true"
    return http_json("POST", url, token=token, payload={"fields": fields})


def update_record(app_token: str, table_id: str, record_id: str, token: str | None, fields: dict, *, use_cli: bool = False):
    if use_cli:
        return cli_base_json("+record-upsert", base_token=app_token, table_id=table_id, record_id=record_id, json_payload=fields)

    url = (
        f"{OPENAPI_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
        "?ignore_consistency_check=true"
    )
    return http_json("PUT", url, token=token, payload={"fields": fields})


def primary_field_name(table_definition: dict) -> str:
    return table_definition["fields"][0]["field_name"]


def extract_record_id(payload: dict) -> str:
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    record = data.get("record") if isinstance(data, dict) else {}
    record_id_list = (
        (record or {}).get("record_id_list")
        or (record or {}).get("record_ids")
        or data.get("record_id_list")
        or data.get("record_ids")
        or []
    )
    return str(
        (record or {}).get("record_id")
        or (record or {}).get("id")
        or (record_id_list[0] if record_id_list else "")
        or data.get("record_id")
        or data.get("id")
        or ""
    ).strip()


def extract_record_ids(payload: dict) -> list[str]:
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    records = data.get("records") if isinstance(data, dict) else []
    record_ids = []
    for item in records or []:
        if not isinstance(item, dict):
            continue
        record_id = str(item.get("record_id") or item.get("id") or "").strip()
        if record_id:
            record_ids.append(record_id)
    return record_ids


def chunked(items: list[dict], size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def sanitize_fields(fields: dict):
    cleaned = {}
    for key, value in fields.items():
        if value is None:
            continue
        cleaned[key] = value
    return cleaned


def sanitize_fields_for_existing_schema(fields: dict, allowed_fields: set[str] | None):
    cleaned = sanitize_fields(fields)
    if not allowed_fields:
        return cleaned
    return {key: value for key, value in cleaned.items() if key in allowed_fields}


def is_cli_add_field_limited_error(error: Exception) -> bool:
    text = str(error or "")
    return "OpenAPIAddField limited" in text or "800004135" in text


def cli_create_field_with_retry(app_token: str, table_id: str, field_definition: dict):
    field_name = str(field_definition.get("field_name") or "").strip()
    for attempt in range(len(CLI_FIELD_CREATE_RETRY_DELAYS) + 1):
        try:
            return cli_create_field(app_token, table_id, field_definition)
        except RuntimeError as exc:
            if not is_cli_add_field_limited_error(exc):
                raise

            # OpenAPIAddField may have accepted the mutation even when the CLI
            # reports a limit. Re-read before retrying to keep the write idempotent.
            if field_name and field_name in cli_list_fields(app_token, table_id):
                return ""
            if attempt >= len(CLI_FIELD_CREATE_RETRY_DELAYS):
                raise

            delay = CLI_FIELD_CREATE_RETRY_DELAYS[attempt]
            print(
                f"[info] 飞书字段创建受限，{delay:g} 秒后重试: {field_name}",
                file=sys.stderr,
            )
            time.sleep(delay)
            if field_name and field_name in cli_list_fields(app_token, table_id):
                return ""


def wait_for_cli_fields(
    app_token: str,
    table_id: str,
    expected_fields: set[str],
) -> dict[str, dict]:
    fields = cli_list_fields(app_token, table_id)
    for delay in CLI_FIELD_VISIBILITY_RETRY_DELAYS:
        if expected_fields.issubset(syncable_cli_field_names(fields)):
            break
        time.sleep(delay)
        fields = cli_list_fields(app_token, table_id)
    return fields


def is_cli_noop_mutation_error(error: Exception) -> bool:
    text = str(error or "").lower()
    return "no operation produced" in text or "800070003" in text


def is_cli_missing_scope_error(error: Exception) -> bool:
    text = str(error or "").lower()
    return "missing required scope" in text or "missing_scope" in text


def is_cli_method_limited_error(error: Exception) -> bool:
    text = str(error or "")
    return "800004135" in text or " limited" in text.lower()


def syncable_cli_field_names(existing_fields: dict[str, dict]) -> set[str]:
    return {str(name).strip() for name in (existing_fields or {}).keys() if str(name).strip()}


def is_select_field_definition(field_definition: dict) -> bool:
    return field_definition.get("type") in {3, 4} and bool((field_definition.get("property") or {}).get("options"))


def normalized_select_options_from_definition(field_definition: dict) -> list[dict]:
    normalized = []
    for item in (field_definition.get("property") or {}).get("options") or []:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        option = {"name": name}
        hue = str(item.get("hue") or "").strip()
        lightness = str(item.get("lightness") or "").strip()
        color = str(item.get("color") or "").strip()
        if hue:
            option["hue"] = hue
        if lightness:
            option["lightness"] = lightness
        if not hue and not lightness and color:
            option["color"] = color
        normalized.append(option)
    return normalized


def normalized_select_options_from_existing(existing_field: dict) -> list[dict]:
    raw_options = existing_field.get("options")
    if raw_options is None:
        raw_options = (existing_field.get("property") or {}).get("options")
    normalized = []
    for item in raw_options or []:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        option = {"name": name}
        hue = str(item.get("hue") or "").strip()
        lightness = str(item.get("lightness") or "").strip()
        color = str(item.get("color") or "").strip()
        if hue:
            option["hue"] = hue
        if lightness:
            option["lightness"] = lightness
        if not hue and not lightness and color:
            option["color"] = color
        normalized.append(option)
    return normalized


def select_options_match(existing_field: dict, field_definition: dict) -> bool:
    return normalized_select_options_from_existing(existing_field) == normalized_select_options_from_definition(field_definition)


def is_managed_chart_metric_field(field_name: str) -> bool:
    if "_" not in field_name:
        return False
    metric, suffix = field_name.rsplit("_", 1)
    return suffix in CHART_PLATFORM_SUFFIXES and metric in CHART_MANAGED_METRICS


def stale_managed_fields(table_definition: dict, existing_fields: dict[str, dict]) -> list[str]:
    expected_fields = {
        str(field.get("field_name") or "").strip()
        for field in table_definition.get("fields", [])
        if str(field.get("field_name") or "").strip()
    }
    stale = []
    if table_definition.get("name") == "作品图表表":
        for field_name in existing_fields:
            if field_name in expected_fields:
                continue
            if is_managed_chart_metric_field(field_name):
                stale.append(field_name)
    return stale


def load_schema_state() -> dict:
    try:
        payload = json.loads(SCHEMA_STATE_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def save_schema_state(state: dict) -> None:
    try:
        SCHEMA_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SCHEMA_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def table_schema_signature(table_definition: dict) -> str:
    field_specs = []
    for field in table_definition.get("fields", []):
        field_specs.append({
            "field_name": str(field.get("field_name") or "").strip(),
            "type": field.get("type"),
            "property": field.get("property") or {},
        })
    payload = {
        "fields": field_specs,
        "visible_fields": table_definition.get("visible_fields") or [],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def table_schema_state_key(app_token: str, table_id: str, table_name: str) -> str:
    raw = f"{app_token}:{table_id}:{table_name}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def schema_state_is_current(app_token: str, table_id: str, table_name: str, signature: str) -> bool:
    state = load_schema_state()
    key = table_schema_state_key(app_token, table_id, table_name)
    return str((state.get(key) or {}).get("signature") or "") == signature


def mark_schema_state_current(app_token: str, table_id: str, table_name: str, signature: str) -> None:
    state = load_schema_state()
    key = table_schema_state_key(app_token, table_id, table_name)
    state[key] = {"table": table_name, "table_id": table_id, "signature": signature}
    save_schema_state(state)


def default_checkpoint_path(payload_path: Path) -> Path:
    suffix = payload_path.suffix or ".json"
    return payload_path.with_name(f"{payload_path.stem}.checkpoint{suffix}")


def row_checkpoint_hash(fields: dict) -> str:
    payload = json.dumps(sanitize_fields(fields), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_sync_checkpoint(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {"version": CHECKPOINT_VERSION, "tables": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": CHECKPOINT_VERSION, "tables": {}}
    tables = payload.get("tables") if isinstance(payload, dict) else {}
    normalized_tables = {}
    if isinstance(tables, dict):
        for table_name, items in tables.items():
            if not isinstance(items, dict):
                continue
            normalized_tables[str(table_name)] = {
                str(key): str(value)
                for key, value in items.items()
                if str(key).strip() and str(value).strip()
            }
    return {"version": CHECKPOINT_VERSION, "tables": normalized_tables}


def save_sync_checkpoint(path: Path | None, checkpoint: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")


def mark_checkpoint_rows(path: Path | None, checkpoint: dict, table_name: str, rows: list[dict]) -> None:
    if not rows:
        return
    tables = checkpoint.setdefault("tables", {})
    table_entries = tables.setdefault(str(table_name), {})
    for item in rows:
        key = str(item.get("key") or "").strip()
        row_hash = str(item.get("row_hash") or "").strip()
        if not key or not row_hash:
            continue
        table_entries[key] = row_hash
    save_sync_checkpoint(path, checkpoint)


def remove_checkpoint_rows(path: Path | None, checkpoint: dict | None, table_name: str, keys: list[str]) -> None:
    if checkpoint is None or not keys:
        return
    tables = checkpoint.setdefault("tables", {})
    table_entries = tables.get(str(table_name))
    if not isinstance(table_entries, dict):
        return
    changed = False
    for key in keys:
        if str(key) in table_entries:
            table_entries.pop(str(key), None)
            changed = True
    if changed:
        save_sync_checkpoint(path, checkpoint)


def is_checkpointed_row(checkpoint: dict, table_name: str, key: str, row_hash: str) -> bool:
    tables = checkpoint.get("tables") if isinstance(checkpoint, dict) else {}
    table_entries = tables.get(str(table_name)) if isinstance(tables, dict) else {}
    if not isinstance(table_entries, dict):
        return False
    return str(table_entries.get(str(key)) or "") == str(row_hash or "")


def batch_create_records(app_token: str, table_id: str, token: str | None, records: list[dict], *, use_cli: bool = False):
    payload = {"records": [{"fields": item["fields"]} for item in records]}
    if use_cli:
        path = (
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"
            "?ignore_consistency_check=true"
        )
        return cli_http_json("POST", path, payload=payload)
    url = (
        f"{OPENAPI_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"
        "?ignore_consistency_check=true"
    )
    return http_json("POST", url, token=token, payload=payload)


def batch_update_records(app_token: str, table_id: str, token: str | None, records: list[dict], *, use_cli: bool = False):
    payload = {
        "records": [
            {
                "record_id": item["record_id"],
                "fields": item["fields"],
            }
            for item in records
        ]
    }
    if use_cli:
        path = (
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update"
            "?ignore_consistency_check=true"
        )
        return cli_http_json("POST", path, payload=payload)
    url = (
        f"{OPENAPI_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update"
        "?ignore_consistency_check=true"
    )
    return http_json("POST", url, token=token, payload=payload)


def to_cli_field_definition(field_definition: dict) -> dict:
    raw_type = field_definition.get("type")
    property_data = field_definition.get("property") or {}
    cli_type = {
        1: "text",
        2: "number",
        3: "select",
        4: "select",
        5: "datetime",
        7: "checkbox",
        17: "attachment",
    }.get(raw_type)
    if not cli_type:
        raise RuntimeError(f"CLI 模式暂不支持的字段类型: {raw_type} ({field_definition.get('field_name')})")

    result = {
        "field_name": field_definition["field_name"],
        "type": cli_type,
    }
    if raw_type == 4 or property_data.get("multiple"):
        result["multiple"] = True
    if property_data.get("options"):
        options = []
        for item in property_data.get("options") or []:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            option = {"name": name}
            if item.get("color") is not None:
                option["color"] = item.get("color")
            if item.get("hue") is not None:
                option["hue"] = item.get("hue")
            if item.get("lightness") is not None:
                option["lightness"] = item.get("lightness")
            options.append(option)
        result["options"] = options
    if raw_type == 5 and property_data.get("date_formatter"):
        result["style"] = {"format": property_data.get("date_formatter")}
    return result


def to_openapi_field_definition(field_definition: dict) -> dict:
    result = copy.deepcopy(field_definition)
    property_data = result.get("property") or {}
    options = property_data.get("options") or []
    if options:
        property_data["options"] = [
            {
                key: value
                for key, value in (item or {}).items()
                if key not in {"hue", "lightness"}
            }
            for item in options
        ]
        result["property"] = property_data
    return result


def normalize_lookup_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text")
                if text is not None:
                    parts.append(str(text))
                elif "name" in item:
                    parts.append(str(item["name"]))
            else:
                parts.append(str(item))
        return "".join(parts).strip()
    if isinstance(value, dict):
        if "text" in value:
            return str(value["text"]).strip()
        if "name" in value:
            return str(value["name"]).strip()
    return str(value).strip()


def sync_table(
    app_token: str,
    table_definition: dict,
    rows: list[dict],
    token: str | None,
    table_map: dict,
    *,
    use_cli: bool = False,
    checkpoint: dict | None = None,
    checkpoint_path: Path | None = None,
    strict_schema: bool = False,
):
    table_name = table_definition["name"]
    primary_field = primary_field_name(table_definition)
    # The same field name, 同步键, carries table-specific identity. The payload
    # definition chooses the upsert key; this writer should not reinterpret it.
    upsert_key = table_definition.get("upsert_key", primary_field)
    table_info = table_map.get(table_name)
    if not table_info:
        table_id = create_table(app_token, token, table_definition, use_cli=use_cli)
        table_info = {"table_id": table_id, "name": table_name}
        table_map[table_name] = table_info
    table_id = table_info["table_id"]

    if use_cli:
        existing_fields = cli_list_fields(app_token, table_id)
        expected_fields = {
            str(field_definition.get("field_name") or "").strip()
            for field_definition in table_definition.get("fields", [])
            if str(field_definition.get("field_name") or "").strip()
        }
        created_missing_field = False
        field_creation_limited = False
        warnings = []
        missing_fields = []
        pruned_fields = []
        updated_select_fields = []
        visible_fields_updated = False
        schema_signature = table_schema_signature(table_definition)
        for field_definition in table_definition.get("fields", []):
            field_name = str(field_definition.get("field_name") or "").strip()
            if not field_name or field_name in existing_fields:
                continue
            try:
                cli_create_field_with_retry(app_token, table_id, field_definition)
            except RuntimeError as exc:
                if not is_cli_add_field_limited_error(exc):
                    raise
                field_creation_limited = True
                print(
                    f"[warn] 字段创建多次重试后仍受限，CLI 模式将跳过缺失字段并继续同步已有字段: {field_name}",
                    file=sys.stderr,
                )
                break
            created_missing_field = True
            time.sleep(CLI_FIELD_CREATE_PACE_SECONDS)
        if created_missing_field or field_creation_limited:
            existing_fields = wait_for_cli_fields(app_token, table_id, expected_fields)
        for field_definition in table_definition.get("fields", []):
            field_name = str(field_definition.get("field_name") or "").strip()
            existing_field = existing_fields.get(field_name)
            if not field_name or not existing_field or not is_select_field_definition(field_definition):
                continue
            if select_options_match(existing_field, field_definition):
                continue
            try:
                cli_update_field(app_token, table_id, existing_field, field_definition)
                updated_select_fields.append(field_name)
            except RuntimeError as exc:
                if is_cli_noop_mutation_error(exc):
                    continue
                warning = f"选择字段选项更新失败：{field_name}（{exc}）"
                warnings.append(warning)
                print(f"[warn] {warning}", file=sys.stderr)
        stale_fields = stale_managed_fields(table_definition, existing_fields)
        for field_name in stale_fields:
            existing_field = existing_fields.get(field_name)
            if not existing_field:
                continue
            try:
                cli_delete_field(app_token, table_id, existing_field)
                pruned_fields.append(field_name)
            except RuntimeError as exc:
                if is_cli_missing_scope_error(exc):
                    raise RuntimeError(
                        f"飞书字段清理需要补充用户授权：缺少 base:field:delete，无法删除历史字段 {field_name}"
                    ) from exc
                if is_cli_method_limited_error(exc):
                    warning = (
                        f"历史生成字段清理被飞书限制：本轮已删除 {len(pruned_fields)} 个，"
                        f"剩余字段将在下次同步继续清理。"
                    )
                    warnings.append(warning)
                    print(f"[warn] {warning}", file=sys.stderr)
                    break
                warning = f"历史生成字段删除失败：{field_name}（{exc}）"
                warnings.append(warning)
                print(f"[warn] {warning}", file=sys.stderr)
        if updated_select_fields or pruned_fields:
            existing_fields = cli_list_fields(app_token, table_id)
        schema_changed = created_missing_field or bool(updated_select_fields or pruned_fields)
        if schema_changed or not schema_state_is_current(app_token, table_id, table_name, schema_signature):
            try:
                cli_set_visible_fields(app_token, table_id, table_definition, existing_fields)
                visible_fields_updated = True
                mark_schema_state_current(app_token, table_id, table_name, schema_signature)
            except RuntimeError as exc:
                if is_cli_noop_mutation_error(exc):
                    visible_fields_updated = True
                    mark_schema_state_current(app_token, table_id, table_name, schema_signature)
                elif is_cli_method_limited_error(exc):
                    print(
                        "[info] 主视图字段顺序调整被飞书限制，本轮已跳过；不影响数据写入。",
                        file=sys.stderr,
                    )
                else:
                    warning = f"主视图字段顺序更新失败：{exc}"
                    warnings.append(warning)
                    print(f"[warn] {warning}", file=sys.stderr)
        allowed_cli_fields = syncable_cli_field_names(existing_fields)
        if field_creation_limited:
            missing_fields = [
                str(field_definition.get("field_name") or "").strip()
                for field_definition in table_definition.get("fields", [])
                if str(field_definition.get("field_name") or "").strip() not in allowed_cli_fields
            ]
            if missing_fields:
                warning = f"以下字段在飞书表中不存在，本次同步将跳过：{', '.join(missing_fields)}"
                warnings.append(warning)
                print(f"[warn] {warning}", file=sys.stderr)
            print(
                f"[warn] 当前可写字段数: {len(allowed_cli_fields)}，缺失字段将不会写入本轮同步。",
                file=sys.stderr,
            )
        if strict_schema:
            strict_missing_fields = sorted(expected_fields - allowed_cli_fields)
            if strict_missing_fields:
                raise RuntimeError(
                    f"飞书字段创建未完成：{table_name} 缺失字段 {', '.join(strict_missing_fields)}"
                )
    else:
        allowed_cli_fields = None
        warnings = []
        missing_fields = []
        pruned_fields = []
        updated_select_fields = []
        visible_fields_updated = False

    print(f"[sync] {table_name}: listing existing records...", file=sys.stderr)
    existing_records = list_records(app_token, table_id, token, use_cli=use_cli)
    print(f"[sync] {table_name}: {len(existing_records)} existing records, {len(rows)} input rows", file=sys.stderr)
    existing_by_key = {}
    existing_key_by_record_id = {}
    for item in existing_records:
        fields = item.get("fields", {})
        key = normalize_lookup_value(fields.get(upsert_key, ""))
        if key:
            existing_by_key[key] = item["record_id"]
            existing_key_by_record_id[item["record_id"]] = key

    create_records = []
    update_records = []
    skipped = 0
    checkpoint_skipped = 0
    input_keys = set()
    for row in rows:
        key = normalize_lookup_value(row.get(upsert_key, ""))
        if not key:
            skipped += 1
            continue
        input_keys.add(key)
        fields = sanitize_fields_for_existing_schema(row, allowed_cli_fields if use_cli else None)
        row_hash = row_checkpoint_hash(fields)
        record_id = existing_by_key.get(key)
        if record_id:
            if checkpoint is not None and is_checkpointed_row(checkpoint, table_name, key, row_hash):
                skipped += 1
                checkpoint_skipped += 1
                continue
            update_records.append({"record_id": record_id, "fields": fields, "key": key, "row_hash": row_hash})
        else:
            create_records.append({"key": key, "fields": fields, "row_hash": row_hash})

    created = 0
    updated = 0
    deleted = 0
    record_batch_size = RECORD_BATCH_SIZE_CLI if use_cli else RECORD_BATCH_SIZE
    for batch in chunked(update_records, record_batch_size):
        if use_cli:
            batch_update_records(app_token, table_id, token, batch, use_cli=True)
        elif len(batch) == 1:
            update_record(app_token, table_id, batch[0]["record_id"], token, batch[0]["fields"], use_cli=False)
        else:
            batch_update_records(app_token, table_id, token, batch, use_cli=False)
        updated += len(batch)
        mark_checkpoint_rows(checkpoint_path, checkpoint or {}, table_name, batch)
        print(f"[checkpoint] {table_name}: update batch persisted={len(batch)}", file=sys.stderr)

    for batch in chunked(create_records, record_batch_size):
        if use_cli:
            result = batch_create_records(app_token, table_id, token, batch, use_cli=True)
            for item, new_record_id in zip(batch, extract_record_ids(result)):
                existing_by_key[item["key"]] = new_record_id
        elif len(batch) == 1:
            result = create_record(app_token, table_id, token, batch[0]["fields"], use_cli=False)
            new_record_id = extract_record_id(result)
            if new_record_id:
                existing_by_key[batch[0]["key"]] = new_record_id
        else:
            batch_create_records(app_token, table_id, token, batch, use_cli=False)
        created += len(batch)
        mark_checkpoint_rows(checkpoint_path, checkpoint or {}, table_name, batch)
        print(f"[checkpoint] {table_name}: create batch persisted={len(batch)}", file=sys.stderr)

    if table_definition.get("prune_missing_records") and rows:
        stale_records = [
            {"record_id": record_id, "key": key}
            for record_id, key in existing_key_by_record_id.items()
            if key and key not in input_keys
        ]
        deleted_stale_keys = []
        for batch in chunked(stale_records, 100):
            record_ids = [item["record_id"] for item in batch]
            batch_keys = [item["key"] for item in batch]
            try:
                if use_cli:
                    cli_delete_records(app_token, table_id, record_ids)
                else:
                    raise RuntimeError("飞书 OpenAPI App 模式暂未实现过期记录删除。")
                deleted += len(record_ids)
                deleted_stale_keys.extend(batch_keys)
            except RuntimeError as exc:
                if is_cli_missing_scope_error(exc):
                    warning = "历史记录未清理：缺少 base:record:delete，旧记录会暂时保留；补充授权后下次同步会继续清理。"
                    warnings.append(warning)
                    print(f"[warn] {warning}", file=sys.stderr)
                    break
                warning = f"历史记录删除失败：{exc}"
                warnings.append(warning)
                print(f"[warn] {warning}", file=sys.stderr)
                break
        remove_checkpoint_rows(checkpoint_path, checkpoint, table_name, deleted_stale_keys)

    return {
        "table": table_name,
        "table_id": table_id,
        "created": created,
        "updated": updated,
        "deleted": deleted,
        "skipped": skipped,
        "checkpoint_skipped": checkpoint_skipped,
        "warnings": warnings,
        "missing_fields": missing_fields,
        "pruned_fields": pruned_fields,
        "updated_select_fields": updated_select_fields,
        "visible_fields_updated": visible_fields_updated,
    }


def sync_all_tables(
    app_token,
    table_definitions,
    tables,
    token,
    table_map,
    *,
    use_cli,
    checkpoint,
    checkpoint_path,
    strict_schema,
):
    results = []
    warnings = []
    for table_definition in table_definitions:
        table_name = table_definition["name"]
        rows = tables.get(table_name, [])
        try:
            result = _run_sync_with_timeout(
                lambda definition=table_definition: sync_table(
                    app_token,
                    definition,
                    rows,
                    token,
                    table_map,
                    use_cli=use_cli,
                    checkpoint=checkpoint,
                    checkpoint_path=checkpoint_path,
                    strict_schema=bool(strict_schema),
                ),
                TABLE_SYNC_TIMEOUT,
            )
        except SyncTimeoutError:
            print(
                f"[timeout] {table_name}: sync exceeded {TABLE_SYNC_TIMEOUT}s",
                file=sys.stderr,
            )
            return {
                "ok": False,
                "error": "sync_timeout",
                "failed_table": table_name,
                "results": results,
                "warnings": warnings,
                "checkpoint_kept": bool(checkpoint_path and checkpoint_path.exists()),
            }
        results.append(result)
        warnings.extend(result.get("warnings") or [])
        print(
            f"[sync] {table_name}: created={result['created']} updated={result['updated']} deleted={result.get('deleted', 0)} skipped={result['skipped']}",
            file=sys.stderr,
        )

    try:
        if checkpoint_path and checkpoint_path.exists():
            checkpoint_path.unlink()
    except OSError:
        pass
    return {"ok": True, "results": results, "warnings": warnings}


def main():
    parser = argparse.ArgumentParser(description="Sync prepared payload into Feishu Bitable via OpenAPI.")
    parser.add_argument("--app-token", required=True)
    parser.add_argument("--payload", default=str(DEFAULT_PAYLOAD_PATH))
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_PATH))
    parser.add_argument("--app-id", default=os.getenv("FEISHU_APP_ID", ""))
    parser.add_argument("--app-secret", default=os.getenv("FEISHU_APP_SECRET", ""))
    parser.add_argument("--cli-mode", action="store_true")
    parser.add_argument("--strict-schema", action="store_true")
    args = parser.parse_args()

    load_env_file(Path(args.env_file))
    app_id = args.app_id or os.getenv("FEISHU_APP_ID", "")
    app_secret = args.app_secret or os.getenv("FEISHU_APP_SECRET", "")
    use_cli = bool(args.cli_mode)
    if not use_cli and (not app_id or not app_secret):
        raise SystemExit("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET。")

    payload_path = Path(args.payload)
    if not payload_path.exists():
        raise SystemExit(f"找不到 payload 文件：{payload_path}")
    checkpoint_path = default_checkpoint_path(payload_path)
    checkpoint = load_sync_checkpoint(checkpoint_path)
    if (checkpoint.get("tables") or {}) and checkpoint_path.exists():
        print(f"[checkpoint] resume from {checkpoint_path}", file=sys.stderr)

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    table_definitions = payload.get("table_definitions", [])
    tables = payload.get("tables", {})

    token = None if use_cli else get_tenant_access_token(app_id, app_secret)
    table_map = list_tables(args.app_token, token, use_cli=use_cli)

    result = sync_all_tables(
        args.app_token,
        table_definitions,
        tables,
        token,
        table_map,
        use_cli=use_cli,
        checkpoint=checkpoint,
        checkpoint_path=checkpoint_path,
        strict_schema=bool(args.strict_schema),
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
