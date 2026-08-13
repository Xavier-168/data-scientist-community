from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Callable, Mapping


class ArtifactValidationError(RuntimeError):
    pass


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


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
                    os.replace(target, backup)
                    backups.append((backup, target))
                os.replace(source, target)
                promoted.append(target)
        except Exception:
            for target in reversed(promoted):
                _remove_path(target)
            for backup, target in reversed(backups):
                if backup.exists():
                    os.replace(backup, target)
            raise

        for backup, _target in backups:
            _remove_path(backup)
