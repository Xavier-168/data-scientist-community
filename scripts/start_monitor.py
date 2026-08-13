#!/usr/bin/env python3
"""One-click launcher for the local monitor service.

- Bootstraps the minimum Python/Node dependencies on first run.
- Starts the runner service if it is not already running.
- Opens the monitor in the configured browser channel when possible.
- Exits after the runner is healthy, while keeping the runner alive.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

_SELF_DIR = Path(__file__).resolve().parent
if str(_SELF_DIR) not in sys.path:
    sys.path.insert(0, str(_SELF_DIR))

from package_identity import verify_package_manifest
from runtime_paths import resolve_auth_dir, resolve_state_dir, seed_state_from_bundle

VALID_BROWSER_CHANNELS = {"chrome", "msedge", "chromium"}
FORCED_BROWSER_CHANNEL = "chrome"
SESSION_STATE_FILE = ".auth/session.json"
SUPPORTED_NODE_MAJOR = 22
SUPPORTED_NODE_MIN_MINOR = 12


def find_cmd(*names: str) -> str | None:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start Yirengongis Monitor service")
    parser.add_argument("--port", type=int, default=8811, help="Runner HTTP port (default: 8811)")
    parser.add_argument("--host", default="127.0.0.1", help="Runner loopback host (default: 127.0.0.1)")
    parser.add_argument("--no-open", action="store_true", help="Do not auto-open the browser")
    parser.add_argument("--foreground", action="store_true", help="Keep launcher attached to the runner process")
    parser.add_argument("--timeout", type=float, default=45.0, help="Wait timeout in seconds")
    parser.add_argument("--skip-bootstrap", action="store_true", help="Skip dependency bootstrap")
    return parser.parse_args()


def run_cmd(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    print("[exec]", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), env=env)
    except FileNotFoundError as exc:
        raise RuntimeError(f"command not found: {cmd[0]}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}")


def ensure_python_deps(root_dir: Path) -> None:
    required = ["pandas", "openpyxl", "certifi", "cryptography"]
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if not missing:
        return
    print(f"[bootstrap] installing Python packages: {', '.join(missing)}")
    run_cmd([sys.executable, "-m", "pip", "install", *missing], root_dir)


def ensure_node_runtime() -> None:
    node = find_cmd("node.exe", "node") if os.name == "nt" else find_cmd("node")
    if not node:
        raise RuntimeError("Node.js 22.12.x is required. Please install Node.js and make sure node/npm/npx are in PATH.")
    try:
        proc = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10)
    except OSError as exc:
        raise RuntimeError(f"Unable to execute Node.js: {exc}") from exc
    match = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.\d+)?(?:[-+].*)?", (proc.stdout or "").strip())
    if proc.returncode != 0 or not match:
        raise RuntimeError("Unable to determine the installed Node.js version.")
    major, minor = (int(match.group(1)), int(match.group(2)))
    if major != SUPPORTED_NODE_MAJOR or minor < SUPPORTED_NODE_MIN_MINOR:
        raise RuntimeError(
            f"Unsupported Node.js version {(proc.stdout or '').strip() or '(unknown)'}; "
            "this project requires Node.js >= 22.12 and < 23."
        )


def ensure_node_deps(root_dir: Path) -> None:
    if (root_dir / "node_modules" / "playwright").exists():
        return
    npm = find_cmd("npm.cmd", "npm") if os.name == "nt" else find_cmd("npm")
    if not npm:
        raise RuntimeError("npm was not found. Please install Node.js 22.12.x first.")
    print("[bootstrap] installing Node dependencies")
    run_cmd([npm, "install"], root_dir)


def playwright_browser_root(root_dir: Path, state_dir: Path | None = None) -> Path:
    raw = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if raw:
        return Path(raw)
    bundled = root_dir / "runtime" / "playwright-browsers"
    if bundled.exists():
        return bundled
    resolved_state_dir = state_dir or Path(resolve_state_dir(root_dir))
    return resolved_state_dir / ".playwright-browsers"


def _playwright_chromium_binary_names() -> tuple[str, ...]:
    if os.name == "nt":
        return ("chrome.exe", "Chromium.exe")
    if sys.platform == "darwin":
        return ("Google Chrome for Testing", "Chromium")
    return ("chrome", "chromium")


def _find_playwright_chromium_in_tree(browser_root: Path) -> Path | None:
    binary_names = set(_playwright_chromium_binary_names())
    for item in sorted(browser_root.iterdir(), key=lambda path: path.name, reverse=True):
        if not item.is_dir() or not item.name.startswith("chromium-"):
            continue
        stack = [item]
        while stack:
            current = stack.pop()
            try:
                children = sorted(current.iterdir(), key=lambda path: path.name, reverse=True)
            except OSError:
                continue
            for child in children:
                if child.is_dir():
                    stack.append(child)
                    continue
                if child.name in binary_names and child.exists():
                    return child
    return None


def _candidate_playwright_browser_roots(root_dir: Path) -> list[Path]:
    roots: list[Path] = []
    app_payload = os.environ.get("YIRENGONGIS_APP_PAYLOAD_DIR")
    if app_payload:
        roots.append(Path(app_payload) / "runtime" / "playwright-browsers")
    roots.append(playwright_browser_root(root_dir))
    roots.append(root_dir / "runtime" / "playwright-browsers")

    unique_roots: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.expanduser())
        if key in seen:
            continue
        seen.add(key)
        unique_roots.append(root.expanduser())
    return unique_roots


def _resolve_playwright_chromium_with_node(root_dir: Path) -> Path | None:
    if not (root_dir / "node_modules" / "playwright").exists():
        return None
    node = find_cmd("node.exe", "node") if os.name == "nt" else find_cmd("node")
    if not node:
        return None

    browser_root = playwright_browser_root(root_dir)
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_root)
    script = (
        "const { chromium } = require('playwright');"
        " process.stdout.write(chromium.executablePath() || '');"
    )
    try:
        proc = subprocess.run(
            [node, "-e", script],
            cwd=str(root_dir),
            env=env,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None

    resolved = str(proc.stdout or "").strip()
    if not resolved:
        return None
    candidate = Path(resolved).expanduser()
    if not candidate.is_absolute():
        candidate = (root_dir / candidate).resolve()
    return candidate if candidate.exists() else None


def find_playwright_chromium(root_dir: Path) -> Path | None:
    candidates: list[Path] = []
    for browser_root in _candidate_playwright_browser_roots(root_dir):
        if not browser_root.exists():
            continue
        discovered = _find_playwright_chromium_in_tree(browser_root)
        if discovered:
            candidates.append(discovered)

    node_resolved = _resolve_playwright_chromium_with_node(root_dir)
    if node_resolved:
        candidates.append(node_resolved)

    unique_candidates: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique_candidates.append(candidate)

    for candidate in unique_candidates:
        if browser_executable_is_usable(candidate):
            return candidate
    return unique_candidates[0] if unique_candidates else None


def _sanitize_browser_channel(value: str | None) -> str:
    channel = str(value or "").strip().lower()
    if not channel:
        return ""
    return FORCED_BROWSER_CHANNEL if channel in VALID_BROWSER_CHANNELS else ""


def find_channel_executable(channel: str) -> Path | None:
    if sys.platform == "darwin":
        # macOS: 浏览器安装在 /Applications/
        mac_paths = {
            "chrome": [
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            ],
            "msedge": [
                Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            ],
        }
        candidates = mac_paths.get(channel, [])
    else:
        # Windows
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if channel == "chrome":
            candidates = [
                Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            ]
            if local_app_data:
                candidates.append(Path(local_app_data) / "Google/Chrome/Application/chrome.exe")
        elif channel == "msedge":
            candidates = [
                Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
                Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            ]
            if local_app_data:
                candidates.append(Path(local_app_data) / "Microsoft/Edge/Application/msedge.exe")
        else:
            candidates = []
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def detect_browser_channel() -> str:
    return FORCED_BROWSER_CHANNEL


def detect_default_browser_channel() -> str:
    return detect_browser_channel()


def load_saved_browser_channel(root_dir: Path, state_dir: Path) -> str:
    config_file = Path(resolve_auth_dir(root_dir, state_dir)) / "customer_config.json"
    if not config_file.exists():
        return detect_default_browser_channel()
    try:
        payload = json.loads(config_file.read_text(encoding="utf-8"))
    except Exception:
        return detect_default_browser_channel()
    return _sanitize_browser_channel(payload.get("browser_channel")) or detect_default_browser_channel()


def _session_state_path(root_dir: Path, state_dir: Path) -> Path:
    return Path(resolve_auth_dir(root_dir, state_dir)) / Path(SESSION_STATE_FILE).name


def load_saved_session_token(root_dir: Path, state_dir: Path) -> str:
    file_path = _session_state_path(root_dir, state_dir)
    if not file_path.exists():
        return ""
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    return str((payload or {}).get("token") or "").strip()


def save_session_token(root_dir: Path, state_dir: Path, token: str) -> None:
    file_path = _session_state_path(root_dir, state_dir)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        json.dumps({"token": token, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if os.name != "nt":
        try:
            file_path.chmod(0o600)
        except OSError:
            pass


def issue_session_token(root_dir: Path, state_dir: Path) -> str:
    token = secrets.token_urlsafe(24)
    save_session_token(root_dir, state_dir, token)
    return token


def find_browser_executable(root_dir: Path, channel: str) -> Path | None:
    normalized = _sanitize_browser_channel(channel) or FORCED_BROWSER_CHANNEL
    if normalized == "chrome":
        system_chrome = find_channel_executable("chrome")
        if browser_executable_is_usable(system_chrome):
            return system_chrome
        if system_chrome:
            print(f"[warn] system Chrome is present but cannot launch: {system_chrome}")
        managed_chromium = find_playwright_chromium(root_dir)
        if browser_executable_is_usable(managed_chromium):
            return managed_chromium
        if managed_chromium:
            print(f"[warn] bundled Chromium is present but cannot launch: {managed_chromium}")
        return None
    if normalized == "chromium":
        managed_chromium = find_playwright_chromium(root_dir)
        return managed_chromium if browser_executable_is_usable(managed_chromium) else None
    candidate = find_channel_executable(normalized)
    return candidate if browser_executable_is_usable(candidate) else None


def browser_executable_is_usable(browser_path: Path | None) -> bool:
    if browser_path is None or not browser_path.exists():
        return False
    try:
        proc = subprocess.run(
            [str(browser_path), "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=3,
            check=False,
        )
    except Exception as exc:
        print(f"[warn] browser usability probe failed for {browser_path}: {exc}")
        return False
    if proc.returncode == 0:
        return True
    stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
    detail = f": {stderr}" if stderr else ""
    print(f"[warn] browser usability probe returned {proc.returncode} for {browser_path}{detail}")
    return False


def ensure_playwright_browsers(root_dir: Path, channel: str) -> None:
    normalized = _sanitize_browser_channel(channel) or FORCED_BROWSER_CHANNEL
    if normalized == "chrome" and browser_executable_is_usable(find_channel_executable("chrome")):
        return
    managed_chromium = find_playwright_chromium(root_dir)
    if browser_executable_is_usable(managed_chromium):
        return
    if managed_chromium:
        raise RuntimeError(
            "Bundled Chrome runtime exists but macOS blocked it. Restart macOS or reinstall Google Chrome."
        )
    bundled_browser_root = root_dir / "runtime" / "playwright-browsers"
    if bundled_browser_root.exists():
        raise RuntimeError("Bundled Chrome runtime is missing or incomplete. Reinstall the app package.")

    browser_root = playwright_browser_root(root_dir)
    browser_root.mkdir(parents=True, exist_ok=True)
    npx = find_cmd("npx.cmd", "npx") if os.name == "nt" else find_cmd("npx")
    if not npx:
        raise RuntimeError("npx was not found. Cannot install Playwright Chromium automatically.")

    print("[bootstrap] installing managed Chrome runtime")
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_root)
    run_cmd([npx, "playwright", "install", "chromium"], root_dir, env=env)

    if not browser_executable_is_usable(find_playwright_chromium(root_dir)):
        raise RuntimeError("Managed Chrome runtime installation finished, but the browser executable was not found.")


def _activate_macos_app(app_bundle: Path | None) -> None:
    if sys.platform != "darwin" or app_bundle is None:
        return
    app_name = str(app_bundle.stem or "").strip()
    if not app_name:
        return
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                "delay 0.4",
                "-e",
                f'tell application "{app_name}" to activate',
                "-e",
                "delay 0.2",
                "-e",
                f'tell application "{app_name}" to activate',
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        )
    except Exception as exc:
        print(f"[warn] AppleScript activate failed for {app_name}: {exc}")


def _escape_applescript_text(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _open_macos_browser_via_applescript(app_bundle: Path | None, url: str) -> bool:
    if sys.platform != "darwin" or app_bundle is None:
        return False
    app_name = str(app_bundle.stem or "").strip()
    if not app_name:
        return False
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'tell application "{_escape_applescript_text(app_name)}" to activate',
                "-e",
                "delay 0.2",
                "-e",
                f'tell application "{_escape_applescript_text(app_name)}" to open location "{_escape_applescript_text(url)}"',
                "-e",
                "delay 0.2",
                "-e",
                f'tell application "{_escape_applescript_text(app_name)}" to activate',
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        )
        return True
    except Exception as exc:
        if app_name:
            print(f"[warn] AppleScript browser open failed for {app_name}: {exc}")
        return False


def safe_open_browser(url: str, root_dir: Path, channel: str) -> bool:
    normalized = _sanitize_browser_channel(channel) or FORCED_BROWSER_CHANNEL
    browser_path = find_browser_executable(root_dir, channel)
    app_bundle = next((parent for parent in [browser_path, *browser_path.parents] if parent.suffix == ".app"), None) if browser_path else None
    if sys.platform == "darwin":
        if normalized in {"chrome", "chromium"}:
            if not browser_path:
                print_browser_unavailable_message()
                return False
            try:
                print(f"[browser] launching {normalized} via executable: {browser_path} --new-window {url}")
                subprocess.Popen(
                    [str(browser_path), "--new-window", url],
                    cwd=str(root_dir),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except Exception as exc:
                print(f"[warn] failed to open {normalized} via direct new-window launch: {exc}")
                print_browser_unavailable_message()
                return False
        try:
            cmd = ["open"]
            if app_bundle:
                cmd.extend(["-a", str(app_bundle)])
            cmd.append(url)
            print(f"[browser] launching via Launch Services: {' '.join(cmd)}")
            subprocess.Popen(cmd, cwd=str(root_dir), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception as exc:
            print(f"[warn] failed to open browser via Launch Services, falling back: {exc}")
    if browser_path:
        try:
            print(f"[browser] launching directly: {browser_path} {url}")
            subprocess.Popen([str(browser_path), url], cwd=str(root_dir))
            _activate_macos_app(app_bundle)
            return True
        except Exception as exc:
            print(f"[warn] failed to open {channel} directly, falling back to default browser: {exc}")
    try:
        webbrowser.open(url)
        return True
    except Exception as exc:
        print(f"[warn] browser did not open automatically. Please open manually: {url} ({exc})")
        return False


def print_browser_unavailable_message() -> None:
    msg = (
        "Chrome/Chromium 无法启动，macOS 安全策略正在拦截浏览器进程。\n"
        "请重启 Mac 后再打开数据科学家；如果仍失败，请重新安装 Google Chrome。"
    )
    print(f"[error] {msg}", file=sys.stderr)
    print(f"[user-message] {msg}")


def is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def is_healthy_runner(base_url: str, timeout: float = 2.0) -> bool:
    try:
        req = Request(f"{base_url}/progress", headers={"User-Agent": "YirengongisLauncher/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            payload = json.loads(resp.read().decode("utf-8"))
            if not isinstance(payload, dict) or not payload.get("ok"):
                return False
            required = {"douyin", "xiaohongshu", "bilibili", "kuaishou"}
            if not required.issubset(payload.keys()):
                return False

        req = Request(f"{base_url}/monitor", headers={"User-Agent": "YirengongisLauncher/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            return resp.status == 200 and "text/html" in resp.headers.get("Content-Type", "")
    except Exception:
        return False


def load_current_package_info(root_dir: Path) -> dict[str, str]:
    manifest_path = root_dir / "package_manifest.json"
    if not manifest_path.exists():
        return {"package_id": "", "build_version": ""}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return {"package_id": "", "build_version": ""}
    manifest_payload = payload.get("payload") or {}
    return {
        "package_id": str(manifest_payload.get("package_id") or "").strip(),
        "build_version": str(manifest_payload.get("build_version") or "").strip(),
    }


def fetch_runner_package_info(base_url: str, timeout: float = 2.0) -> dict[str, str]:
    try:
        req = Request(f"{base_url}/package-info", headers={"User-Agent": "YirengongisLauncher/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return {"package_id": "", "build_version": ""}
            payload = json.loads(resp.read().decode("utf-8"))
            if not isinstance(payload, dict) or not payload.get("ok"):
                return {"package_id": "", "build_version": ""}
            return {
                "package_id": str(payload.get("package_id") or "").strip(),
                "build_version": str(payload.get("build_version") or payload.get("version") or "").strip(),
            }
    except Exception:
        return {"package_id": "", "build_version": ""}


def runner_build_matches(current: dict[str, str], running: dict[str, str]) -> bool:
    current_package_id = str(current.get("package_id") or "").strip()
    running_package_id = str(running.get("package_id") or "").strip()
    current_version = str(current.get("build_version") or "").strip()
    running_version = str(running.get("build_version") or "").strip()

    if current_package_id or running_package_id:
        if current_package_id != running_package_id:
            return False

    # Packaged builds must not reuse older runners that do not expose build_version.
    if current_version:
        return running_version == current_version
    return not running_version


def listener_pids_for_port(port: int) -> list[int]:
    if sys.platform != "darwin":
        return []
    lsof = find_cmd("lsof")
    if not lsof:
        return []
    try:
        result = subprocess.run(
            [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except Exception:
        return []
    pids: list[int] = []
    for line in result.stdout.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid > 0 and pid != os.getpid():
            pids.append(pid)
    return sorted(set(pids))


def process_command(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except Exception:
        return ""
    return result.stdout.strip()


def is_expected_runner_command(command: str, expected_base_dir: Path | str) -> bool:
    raw_command = str(command or "")
    expected = str(Path(expected_base_dir).expanduser())
    resolved = os.path.realpath(expected)
    path_matches = expected in raw_command or resolved in raw_command
    runner_matches = any(
        marker in raw_command
        for marker in ("/scripts/_run.py", "/scripts/runner.py", "runner.cpython-")
    )
    return bool(path_matches and runner_matches)


def terminate_listener_pids(
    port: int,
    *,
    reason: str,
    expected_base_dir: Path | str,
) -> bool:
    pids = listener_pids_for_port(port)
    if not pids:
        return False
    matching_pids = []
    for pid in pids:
        command = process_command(pid)
        if not is_expected_runner_command(command, expected_base_dir):
            print(f"[warn] refusing to stop unrelated listener pid={pid} port={port} cmd={command}")
            continue
        matching_pids.append(pid)
        print(f"[warn] stopping stale runner pid={pid} port={port} reason={reason} cmd={command}")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except Exception as exc:
            print(f"[warn] failed to terminate pid={pid}: {exc}")
    if not matching_pids:
        return False
    deadline = time.time() + 4
    while time.time() < deadline:
        current = set(listener_pids_for_port(port))
        if not any(pid in current for pid in matching_pids):
            return True
        time.sleep(0.2)
    current = set(listener_pids_for_port(port))
    for pid in matching_pids:
        if pid not in current or not is_expected_runner_command(process_command(pid), expected_base_dir):
            continue
        print(f"[warn] force stopping stale runner pid={pid} port={port}")
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    time.sleep(0.3)
    remaining = set(listener_pids_for_port(port))
    return not any(pid in remaining for pid in matching_pids)


def find_available_port(host: str, start_port: int, attempts: int = 10) -> int:
    for port in range(start_port, start_port + attempts):
        if not is_port_open(host, port):
            return port
    raise RuntimeError(f"ports {start_port}-{start_port + attempts - 1} are all in use")


def wait_until_ready(base_url: str, host: str, port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_port_open(host, port) and is_healthy_runner(base_url):
            return True
        time.sleep(0.35)
    return False


def build_loading_url(root_dir: Path, state_dir: Path, port: int, session_token: str) -> str:
    loading_html = (root_dir / "frontend" / "loading.html").resolve()
    downloads_dir = state_dir / "downloads"
    query = urlencode(
        {
            "port": str(port),
            "launcher_log": str((downloads_dir / "launcher.log").resolve()),
            "runner_log": str((downloads_dir / "runner.log").resolve()),
        }
    )
    return f"{loading_html.as_uri()}?{query}#session={quote(session_token)}"


def _launcher_access_host(host: str) -> str:
    normalized = str(host or "").strip()
    return normalized


def _is_loopback_host(host: str) -> bool:
    value = str(host or "").strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return value.lower() == "localhost" or value == "127.0.0.1"


def main() -> int:
    args = parse_args()

    if not _is_loopback_host(args.host):
        msg = "为保护本地会话和采集数据，服务只允许监听 127.0.0.1 或 localhost。"
        print(f"[error] {msg}", file=sys.stderr)
        print(f"[user-message] {msg}")
        return 2

    root_dir = Path(__file__).resolve().parents[1]
    state_dir = Path(seed_state_from_bundle(root_dir))
    package_status = verify_package_manifest(str(root_dir))
    if not package_status.get("ok"):
        print(
            f"[error] package identity verification failed: {package_status.get('error')}",
            file=sys.stderr,
        )
        return 6

    # Prefer the packaged launcher when present, otherwise run the server module.
    _run_script = root_dir / "scripts" / "_run.py"
    runner_script = _run_script if _run_script.exists() else root_dir / "scripts" / "runner.py"
    if not runner_script.exists():
        print(f"[error] runner script not found: {runner_script}", file=sys.stderr)
        return 2

    browser_channel = load_saved_browser_channel(root_dir, state_dir)
    current_package_info = load_current_package_info(root_dir)
    session_token = load_saved_session_token(root_dir, state_dir)
    access_host = _launcher_access_host(args.host)
    base_url = f"http://{access_host}:{args.port}"
    monitor_url = f"{base_url}/monitor"
    browser_monitor_url = f"{monitor_url}#session={quote(session_token)}" if session_token else monitor_url

    if not args.skip_bootstrap:
        try:
            ensure_python_deps(root_dir)
            ensure_node_runtime()
            ensure_node_deps(root_dir)
            ensure_playwright_browsers(root_dir, browser_channel)
        except Exception as exc:
            print(f"[error] bootstrap failed: {exc}", file=sys.stderr)
            if "macOS blocked" in str(exc) or "Chrome runtime" in str(exc):
                print_browser_unavailable_message()
                return 6
            return 4

    if not args.no_open and not find_browser_executable(root_dir, browser_channel):
        print_browser_unavailable_message()
        return 6

    if is_port_open(access_host, args.port) and is_healthy_runner(base_url):
        existing_package_info = fetch_runner_package_info(base_url)
        if runner_build_matches(current_package_info, existing_package_info):
            print(f"[ok] runner already healthy: {base_url}")
            # Recover the REAL session token from the running runner process,
            # because the saved token on disk may be stale (different launch).
            try:
                req = Request(
                    f"{base_url}/session/recover",
                    headers={"User-Agent": "YirengongisLauncher/1.0"},
                )
                with urlopen(req, timeout=3) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                    if payload.get("ok") and payload.get("token"):
                        session_token = payload["token"]
                        save_session_token(root_dir, state_dir, session_token)
                        browser_monitor_url = f"{monitor_url}#session={quote(session_token)}"
                        print(f"[ok] recovered live session from runner")
            except Exception:
                pass  # fall through with whatever we had
            if not args.no_open:
                if not safe_open_browser(browser_monitor_url, root_dir, browser_channel):
                    return 6
            return 0

        print(
            "[warn] existing runner build mismatch: "
            f"current={current_package_info.get('package_id') or '(source)'}@{current_package_info.get('build_version') or '(unknown)'} "
            f"running={existing_package_info.get('package_id') or '(unknown)'}@{existing_package_info.get('build_version') or '(unknown)'}"
        )
        terminate_listener_pids(
            args.port,
            reason="build_mismatch",
            expected_base_dir=root_dir,
        )

    if is_port_open(access_host, args.port):
        print(f"[warn] port {args.port} is occupied by another process, choosing a new port")
        try:
            selected_port = find_available_port(access_host, args.port + 1)
            print(f"[user-message] 端口 {args.port} 被其他程序占用，已自动切换到端口 {selected_port}")
        except RuntimeError as exc:
            msg = f"无法启动：端口 {args.port}-{args.port + 9} 均被占用。\n请关闭占用端口的程序后重试。"
            print(f"[error] {msg}", file=sys.stderr)
            print(f"[user-message] {msg}")
            return 5
    else:
        selected_port = args.port

    base_url = f"http://{access_host}:{selected_port}"
    monitor_url = f"{base_url}/monitor"
    session_token = issue_session_token(root_dir, state_dir)
    browser_monitor_url = f"{monitor_url}#session={quote(session_token)}"

    env = os.environ.copy()
    env["YIRENGONGIS_BASE_DIR"] = str(root_dir)
    env["YIRENGONGIS_STATE_DIR"] = str(state_dir)
    env["YIRENGONGIS_RUNNER_HOST"] = str(args.host)
    env["YIRENGONGIS_RUNNER_PORT"] = str(selected_port)
    env["YIRENGONGIS_SESSION_TOKEN"] = session_token
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(playwright_browser_root(root_dir, state_dir)))

    cmd = [sys.executable, str(runner_script)]
    print("[start]", " ".join(cmd))
    popen_kwargs = {
        "cwd": str(root_dir),
        "env": env,
    }
    runner_log_handle = None
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        if not args.foreground:
            creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
            popen_kwargs["stdin"] = subprocess.DEVNULL
            popen_kwargs["stdout"] = subprocess.DEVNULL
            popen_kwargs["stderr"] = subprocess.DEVNULL
        popen_kwargs["creationflags"] = creationflags
    elif not args.foreground:
        process_log_path = state_dir / "downloads" / "runner_process.log"
        process_log_path.parent.mkdir(parents=True, exist_ok=True)
        runner_log_handle = open(process_log_path, "ab", buffering=0)
        popen_kwargs["stdin"] = subprocess.DEVNULL
        popen_kwargs["stdout"] = runner_log_handle
        popen_kwargs["stderr"] = subprocess.STDOUT
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    if runner_log_handle is not None:
        runner_log_handle.close()

    # ── 立刻打开 loading 页面（不等服务就绪）─────────────────────
    if not args.no_open:
        loading_html = root_dir / "frontend" / "loading.html"
        if loading_html.exists():
            loading_url = build_loading_url(root_dir, state_dir, selected_port, session_token)
            print(f"[open] loading page: {loading_url}")
            if not safe_open_browser(loading_url, root_dir, browser_channel):
                try:
                    proc.terminate()
                except Exception:
                    pass
                return 6
        else:
            # fallback: 没有 loading.html 就等服务起来再打开
            print("[info] loading.html not found, will open monitor after server starts")

    try:
        if not wait_until_ready(base_url, access_host, selected_port, args.timeout):
            print(f"[error] runner start timed out after {args.timeout}s", file=sys.stderr)
            try:
                proc.terminate()
            except Exception:
                pass
            return 3

        print(f"[ok] runner started: {monitor_url}")
        # loading.html 会自动轮询并跳转到 /monitor，无需再次打开浏览器

        if not args.foreground:
            print(f"[ok] launcher exiting, runner stays alive: {browser_monitor_url}")
            return 0

        while True:
            ret = proc.poll()
            if ret is not None:
                return ret
            time.sleep(0.8)

    except KeyboardInterrupt:
        print("\n[stop] stopping runner...")
        try:
            if sys.platform.startswith("win"):
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
