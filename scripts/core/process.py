"""进程管理 — PID 存活/Node 查找/Chrome profile 清理 (L1 基础层)。

迁移自 runner_process.py。旧文件保留为兼容 shim。
"""

import os
import shutil
import signal
import subprocess
import time


def pid_alive(pid: int) -> bool:
    """Check if a process with *pid* is still running."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return exit_code.value == STILL_ACTIVE
                return True
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError, SystemError):
        return False


def resolve_default_node_bin(env_path: str) -> str:
    node_names = ["node.exe", "node"] if os.name == "nt" else ["node"]
    for node_name in node_names:
        node = shutil.which(node_name, path=env_path or "")
        if node:
            return node
    if os.name == "nt":
        return ""
    for candidate in (
        "/usr/local/bin/node",
        "/opt/homebrew/bin/node",
        "/usr/bin/node",
    ):
        if os.path.exists(candidate):
            return candidate
    return ""


def sort_existing_paths(paths: list[str]) -> list[str]:
    return sorted(
        (path for path in paths if path and os.path.exists(path)),
        key=lambda item: os.path.getmtime(item),
        reverse=True,
    )


def _powershell_output(script: str, timeout: float = 8.0) -> str:
    """执行 PowerShell 片段并返回 stdout（失败返回空串）。仅 Windows 使用。"""
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]
    kwargs = {
        "capture_output": True,
        "text": True,
        "errors": "replace",
        "timeout": timeout,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(command, **kwargs)
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return str(result.stdout or "")


def port_listener_pids(port: int) -> list[int]:
    """返回监听本地端口的进程 PID。

    POSIX 用 lsof；Windows 用 Get-NetTCPConnection（netstat 输出解析在
    本地化系统上不可靠）。
    """
    if os.name == "nt":
        script = (
            f"(Get-NetTCPConnection -LocalPort {int(port)} -State Listen "
            "-ErrorAction SilentlyContinue).OwningProcess | Sort-Object -Unique"
        )
        pids = []
        for line in _powershell_output(script).splitlines():
            text = line.strip()
            if text.isdigit():
                pids.append(int(text))
        return pids
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return []
    return [
        int(text)
        for text in (result.stdout or "").splitlines()
        if text.strip().isdigit()
    ]


def process_command(pid: int) -> str:
    """返回进程完整命令行（识别本产品进程用；失败返回空串）。"""
    if os.name == "nt":
        script = f"(Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\").CommandLine"
        return _powershell_output(script).strip()
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return ""
    return str(result.stdout or "").strip()


def terminate_pid_tree(pid: int, *, grace_seconds: float = 2.0) -> bool:
    """终止进程及其子进程。Windows 用 taskkill /T /F；POSIX 先 SIGTERM 后 SIGKILL。"""
    if pid <= 0 or pid == os.getpid():
        return False
    if os.name == "nt":
        kwargs = {"capture_output": True, "text": True, "timeout": 15}
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], **kwargs)
        except Exception:
            return False
        return result.returncode == 0
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    deadline = time.time() + max(0.1, grace_seconds)
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.1)
    if pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return True


def profile_path_candidates(profile_dir: str) -> set[str]:
    candidates = set()
    for path_value in (profile_dir, os.path.abspath(profile_dir), os.path.realpath(profile_dir)):
        text = str(path_value or "").strip()
        if text:
            candidates.add(text)
    return candidates


def command_uses_profile(cmd: str, profile_dir: str) -> bool:
    if not cmd:
        return False
    for candidate in profile_path_candidates(profile_dir):
        if f"--user-data-dir={candidate}" in cmd or f"--user-data-dir {candidate}" in cmd:
            return True
    return False


def terminate_profile_browsers(profile_dir: str, *, grace_seconds: float = 1.5) -> list[int]:
    """Stop stale Chrome/Chromium processes that still own this auth profile."""
    if not profile_dir or os.name == "nt":
        return []
    try:
        output = subprocess.check_output(
            ["ps", "-axo", "pid=,command="], text=True, errors="replace"
        )
    except Exception:
        return []

    current_pid = os.getpid()
    pids = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _sep, cmd = stripped.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == current_pid:
            continue
        if command_uses_profile(cmd, profile_dir):
            pids.append(pid)

    killed = []
    for target_pid in sorted(set(pids)):
        try:
            os.kill(target_pid, signal.SIGTERM)
            killed.append(target_pid)
        except ProcessLookupError:
            pass
        except Exception:
            pass

    if killed:
        deadline = time.time() + max(0.1, grace_seconds)
        while time.time() < deadline and any(pid_alive(target_pid) for target_pid in killed):
            time.sleep(0.1)
        for target_pid in killed:
            if not pid_alive(target_pid):
                continue
            try:
                os.kill(target_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception:
                pass
    return killed


def clear_profile_lock_files(profile_dir: str) -> None:
    if not profile_dir:
        return
    for name in (
        "SingletonLock",
        "SingletonCookie",
        "SingletonSocket",
        "RunningChromeVersion",
        "DevToolsActivePort",
    ):
        try:
            os.remove(os.path.join(profile_dir, name))
        except FileNotFoundError:
            pass
        except Exception:
            pass
