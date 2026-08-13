from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


@dataclass(frozen=True)
class SupervisedResult:
    returncode: int
    outcome: str
    stdout_tail: str
    stderr_tail: str
    duration_seconds: float

    @property
    def stdout_text(self) -> str:
        return self.stdout_tail

    @property
    def stderr_text(self) -> str:
        return self.stderr_tail


class _ActivityClock:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = time.monotonic()

    def touch(self) -> None:
        with self._lock:
            self._value = time.monotonic()

    def seconds_since_activity(self) -> float:
        with self._lock:
            return time.monotonic() - self._value


def _progress_mtime(path: str | Path | None) -> int | None:
    if not path:
        return None
    try:
        return Path(path).stat().st_mtime_ns
    except OSError:
        return None


def _start_process(
    command: Sequence[str],
    *,
    env: dict[str, str],
    cwd: str | Path,
    requires_user_session: bool,
) -> subprocess.Popen:
    kwargs = {
        "cwd": str(cwd),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    elif not requires_user_session:
        kwargs["start_new_session"] = True
    return subprocess.Popen(list(command), **kwargs)


def _terminate_process(
    proc: subprocess.Popen,
    *,
    requires_user_session: bool,
    grace_seconds: float,
) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name != "nt" and not requires_user_session:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait(timeout=max(float(grace_seconds), 0.05))
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name != "nt" and not requires_user_session:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        pass


def _pump_stream(
    stream,
    *,
    stream_name: str,
    log_handle,
    log_lock: threading.Lock,
    tail: deque[str],
    activity: _ActivityClock,
) -> None:
    try:
        for line in iter(stream.readline, ""):
            tail.append(line)
            activity.touch()
            with log_lock:
                log_handle.write(f"[{stream_name}] {line}")
                log_handle.flush()
    finally:
        try:
            stream.close()
        except Exception:
            pass


def run_supervised(
    command: Sequence[str],
    *,
    env: dict[str, str],
    cwd: str | Path,
    log_path: str | Path,
    progress_path: str | Path | None = None,
    inactivity_timeout: float = 720,
    requires_user_session: bool = False,
    heartbeat: Callable[[], object] | None = None,
    heartbeat_interval: float = 15,
    poll_interval: float = 0.25,
    terminate_grace_seconds: float = 2,
    tail_lines: int = 400,
) -> SupervisedResult:
    started = time.monotonic()
    activity = _ActivityClock()
    stdout_tail: deque[str] = deque(maxlen=max(int(tail_lines), 1))
    stderr_tail: deque[str] = deque(maxlen=max(int(tail_lines), 1))
    resolved_log_path = Path(log_path)
    resolved_log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = _start_process(
        command,
        env=env,
        cwd=cwd,
        requires_user_session=requires_user_session,
    )
    outcome = "success"
    last_progress_mtime = _progress_mtime(progress_path)
    next_heartbeat = time.monotonic()
    log_lock = threading.Lock()

    with resolved_log_path.open("a", encoding="utf-8") as log_handle:
        log_handle.write(f"\n[supervisor] started pid={proc.pid} command={list(command)!r}\n")
        log_handle.flush()
        threads = [
            threading.Thread(
                target=_pump_stream,
                kwargs={
                    "stream": proc.stdout,
                    "stream_name": "stdout",
                    "log_handle": log_handle,
                    "log_lock": log_lock,
                    "tail": stdout_tail,
                    "activity": activity,
                },
                daemon=True,
            ),
            threading.Thread(
                target=_pump_stream,
                kwargs={
                    "stream": proc.stderr,
                    "stream_name": "stderr",
                    "log_handle": log_handle,
                    "log_lock": log_lock,
                    "tail": stderr_tail,
                    "activity": activity,
                },
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()

        while proc.poll() is None:
            time.sleep(max(float(poll_interval), 0.01))
            if proc.poll() is not None:
                break
            current_progress_mtime = _progress_mtime(progress_path)
            if current_progress_mtime is not None and current_progress_mtime != last_progress_mtime:
                last_progress_mtime = current_progress_mtime
                activity.touch()
            now = time.monotonic()
            if heartbeat and now >= next_heartbeat:
                try:
                    heartbeat()
                except Exception as exc:
                    with log_lock:
                        log_handle.write(f"[supervisor] heartbeat_error={exc!r}\n")
                        log_handle.flush()
                next_heartbeat = now + max(float(heartbeat_interval), 0.01)
            if float(inactivity_timeout) > 0 and activity.seconds_since_activity() > float(inactivity_timeout):
                outcome = "stalled"
                with log_lock:
                    log_handle.write(
                        f"[supervisor] stalled inactivity_seconds={activity.seconds_since_activity():.3f}\n"
                    )
                    log_handle.flush()
                _terminate_process(
                    proc,
                    requires_user_session=requires_user_session,
                    grace_seconds=terminate_grace_seconds,
                )
                break

        try:
            returncode = proc.wait(timeout=max(float(terminate_grace_seconds), 0.1) + 1)
        except subprocess.TimeoutExpired:
            _terminate_process(proc, requires_user_session=requires_user_session, grace_seconds=0.1)
            returncode = proc.wait(timeout=1)
        for thread in threads:
            thread.join(timeout=1)
        if outcome != "stalled":
            outcome = "success" if returncode == 0 else "failed"
        log_handle.write(f"[supervisor] finished returncode={returncode} outcome={outcome}\n")
        log_handle.flush()

    return SupervisedResult(
        returncode=returncode,
        outcome=outcome,
        stdout_tail="".join(stdout_tail),
        stderr_tail="".join(stderr_tail),
        duration_seconds=round(time.monotonic() - started, 3),
    )
