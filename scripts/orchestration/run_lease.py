from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.io import load_json_dict, write_json_file_atomically
from core.process import pid_alive


@dataclass(frozen=True)
class LeaseToken:
    run_id: str
    owner_pid: int
    kind: str


class RunLeaseStore:
    def __init__(
        self,
        path: str | Path,
        *,
        ttl_seconds: float = 120,
        pid_alive: Callable[[int], bool] = pid_alive,
    ) -> None:
        self.path = Path(path)
        self.ttl_seconds = max(float(ttl_seconds), 0.1)
        self.pid_alive = pid_alive
        self._mutex = threading.RLock()

    def acquire(
        self,
        kind: str,
        *,
        owner_pid: int | None = None,
        now: float | None = None,
    ) -> LeaseToken | None:
        timestamp = time.time() if now is None else float(now)
        owner = int(owner_pid or os.getpid())
        with self._mutex:
            if self.is_active(now=timestamp):
                return None
            payload = {
                "run_id": str(uuid.uuid4()),
                "owner_pid": owner,
                "kind": str(kind or "task"),
                "started_at": timestamp,
                "heartbeat_at": timestamp,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                return None
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                self.path.unlink(missing_ok=True)
                raise
            return LeaseToken(payload["run_id"], owner, payload["kind"])

    def is_active(self, *, now: float | None = None) -> bool:
        current = time.time() if now is None else float(now)
        with self._mutex:
            payload = self.read_payload()
            if not payload:
                return self._invalid_file_is_active(current)
            owner_pid = int(payload.get("owner_pid") or 0)
            if self.pid_alive(owner_pid):
                return True
            heartbeat = float(payload.get("heartbeat_at") or payload.get("started_at") or 0)
            if current - heartbeat <= self.ttl_seconds:
                return True
            self._remove_if_run_id(str(payload.get("run_id") or ""))
            return False

    def heartbeat(self, run_id: str, *, now: float | None = None) -> bool:
        with self._mutex:
            payload = self.read_payload()
            if not payload or payload.get("run_id") != run_id:
                return False
            payload["heartbeat_at"] = time.time() if now is None else float(now)
            write_json_file_atomically(str(self.path), payload, indent=None)
            return True

    def release(self, run_id: str, *, force: bool = False) -> bool:
        with self._mutex:
            payload = self.read_payload()
            if not payload:
                if force:
                    self.path.unlink(missing_ok=True)
                return not self.path.exists()
            if not force and payload.get("run_id") != run_id:
                return False
            self.path.unlink(missing_ok=True)
            return True

    def read_payload(self) -> dict:
        payload = load_json_dict(str(self.path))
        if payload:
            return payload
        if not self.path.exists():
            return {}
        try:
            parts = self.path.read_text(encoding="utf-8").strip().split()
            started_at = float(parts[0])
            owner_pid = int(parts[1]) if len(parts) >= 2 else 0
        except (OSError, ValueError, IndexError):
            return {}
        return {
            "run_id": "legacy",
            "owner_pid": owner_pid,
            "kind": "legacy",
            "started_at": started_at,
            "heartbeat_at": started_at,
        }

    def _remove_if_run_id(self, run_id: str) -> bool:
        payload = self.read_payload()
        if not payload or payload.get("run_id") != run_id:
            return False
        self.path.unlink(missing_ok=True)
        return True

    def _invalid_file_is_active(self, current: float) -> bool:
        if not self.path.exists():
            return False
        try:
            age = current - self.path.stat().st_mtime
        except OSError:
            return True
        if age <= self.ttl_seconds:
            return True
        self.path.unlink(missing_ok=True)
        return False
