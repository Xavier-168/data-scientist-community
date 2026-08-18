from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Callable, Mapping


class ArtifactValidationError(RuntimeError):
    pass


class ArtifactBusyError(RuntimeError):
    pass


_REPLACE_BUSY_ATTEMPTS = 4
_REPLACE_BUSY_DELAY_SECONDS = 0.5


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        # Windows 下目录内文件可能被杀软/索引器短暂占用，rmtree 失败不致命
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def _replace_with_busy_retry(source: Path, target: Path) -> None:
    """os.replace；Windows 上目标被 Excel 等程序打开时抛 PermissionError，短暂重试。"""
    last_error: OSError | None = None
    for attempt in range(_REPLACE_BUSY_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            last_error = exc
            if attempt < _REPLACE_BUSY_ATTEMPTS - 1:
                time.sleep(_REPLACE_BUSY_DELAY_SECONDS)
    raise ArtifactBusyError(
        f"artifact_busy:{target.name}: 目标文件正被其他程序占用（如 Excel 正在打开该导出文件），"
        f"请关闭后重试 ({last_error})"
    ) from last_error


class RunWorkspace:
    def __init__(self, downloads_dir: str | Path, run_id: str, platform: str) -> None:
        self.downloads_dir = Path(downloads_dir)
        self.run_id = str(run_id)
        self.platform = str(platform)
        self.root = self.downloads_dir / "runs" / self.run_id / self.platform
        self.root.mkdir(parents=True, exist_ok=True)

    def stage_path(self, name: str | Path) -> Path:
        path = self.root / Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def promote(
        self,
        mapping: Mapping[str | Path, str | Path],
        *,
        validator: Callable[[], None],
    ) -> None:
        validator()
        normalized = [(Path(source), Path(target)) for source, target in mapping.items()]
        missing = [str(source) for source, _target in normalized if not source.exists()]
        if missing:
            raise ArtifactValidationError(f"missing_artifact:{missing[0]}")

        transaction_id = uuid.uuid4().hex
        backups: list[tuple[Path, Path]] = []
        promoted: list[Path] = []
        try:
            for source, target in normalized:
                target.parent.mkdir(parents=True, exist_ok=True)
                backup = target.with_name(f".{target.name}.previous-{transaction_id}")
                if target.exists():
                    _replace_with_busy_retry(target, backup)
                    backups.append((backup, target))
                _replace_with_busy_retry(source, target)
                promoted.append(target)
        except Exception:
            for target in reversed(promoted):
                _remove_path(target)
            for backup, target in reversed(backups):
                if backup.exists():
                    try:
                        _replace_with_busy_retry(backup, target)
                    except Exception:
                        pass
            raise

        for backup, _target in backups:
            _remove_path(backup)
