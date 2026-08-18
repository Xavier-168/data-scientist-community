"""路径解析 — 唯一的运行态目录解析入口 (L1 基础层)。

所有业务模块获取 BASE_DIR / DOWNLOADS_DIR / AUTH_DIR / STATE_DIR
必须经过这里,禁止硬编码 Path(__file__).parents[N]。

迁移自 runtime_paths.py。旧文件保留为兼容 shim (re-export 本模块)。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

APP_SUPPORT_NAME = "数据科学家 Community"
PACKAGED_APP_NAMES = ("数据科学家 Community.app", "打开数据中心.app")


def _load_manifest_payload(base_dir: str | Path) -> dict:
    manifest_path = Path(base_dir) / "package_manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    manifest_payload = (payload or {}).get("payload") or {}
    return manifest_payload if isinstance(manifest_payload, dict) else {}


def read_package_id(base_dir: str | Path) -> str:
    return str(_load_manifest_payload(base_dir).get("package_id") or "").strip()


def is_packaged_runtime(base_dir: str | Path) -> bool:
    base_path = Path(base_dir)
    if not (base_path / "package_manifest.json").exists():
        return False
    if (base_path / "runtime").is_dir():
        return True
    return (base_path / "README_START.txt").exists() or any(
        (base_path.parent / name).is_dir() for name in PACKAGED_APP_NAMES
    )


def resolve_state_dir(base_dir: str | Path, explicit_state_dir: str | Path | None = None) -> str:
    explicit = str(explicit_state_dir or os.environ.get("YIRENGONGIS_STATE_DIR") or "").strip()
    if explicit:
        return str(Path(explicit).expanduser().resolve())

    base_path = Path(base_dir).resolve()
    if sys.platform == "win32":
        # Windows 与 macOS 同理：源码目录/安装目录可能是只读位置，
        # 也不应承载 Cookie、导出、日志等敏感运行状态，默认放到
        # 每用户 Roaming 目录（%APPDATA%）。首次运行由
        # seed_state_from_bundle 把仓库内既有状态播种过去。
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        app_support_root = Path(appdata) / APP_SUPPORT_NAME
        package_id = read_package_id(base_path) if is_packaged_runtime(base_path) else ""
        return str((app_support_root / package_id) if package_id else app_support_root)
    if sys.platform != "darwin":
        return str(base_path)

    # macOS 源码态和封装态都必须把 Cookie、导出、日志等运行数据放到
    # 用户可写目录。源码目录可能是只读安装位置，也不应承载敏感状态。
    app_support_root = Path.home() / "Library" / "Application Support" / APP_SUPPORT_NAME
    package_id = read_package_id(base_path) if is_packaged_runtime(base_path) else ""
    return str((app_support_root / package_id) if package_id else app_support_root)


def resolve_downloads_dir(base_dir: str | Path, explicit_state_dir: str | Path | None = None) -> str:
    return str(Path(resolve_state_dir(base_dir, explicit_state_dir)) / "downloads")


def resolve_auth_dir(base_dir: str | Path, explicit_state_dir: str | Path | None = None) -> str:
    return str(Path(resolve_state_dir(base_dir, explicit_state_dir)) / ".auth")


def seed_state_from_bundle(
    base_dir: str | Path,
    state_dir: str | Path | None = None,
    *,
    subdirs: tuple[str, ...] = ("downloads", ".auth"),
) -> str:
    base_path = Path(base_dir).resolve()
    resolved_state_dir = Path(resolve_state_dir(base_path, state_dir))
    resolved_state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        resolved_state_dir.chmod(0o700)
    except OSError:
        pass

    if resolved_state_dir == base_path:
        return str(resolved_state_dir)

    for name in subdirs:
        source_dir = base_path / name
        if not source_dir.exists() or source_dir.is_symlink():
            continue

        target_dir = resolved_state_dir / name
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not source_dir.is_dir():
            continue

        for item in source_dir.iterdir():
            target = target_dir / item.name
            if target.exists():
                continue
            try:
                if item.is_dir():
                    shutil.copytree(item, target)
                else:
                    shutil.copy2(item, target)
            except Exception:
                continue

    return str(resolved_state_dir)


def resolve_base_dir() -> str:
    """返回项目根目录 (scripts 的父目录)。

    供数据流水线模块 (merge_channels / platform_source_rows 等) 使用,
    替代它们原先硬编码的 Path(__file__).parents[1]。
    开发态 = 源码根;打包态由 YIRENGONGIS_BASE_DIR 环境变量决定。
    """
    env_base = str(os.environ.get("YIRENGONGIS_BASE_DIR") or "").strip()
    if env_base:
        return str(Path(env_base).resolve())
    # scripts/core/paths.py 的父父目录就是项目根
    return str(Path(__file__).resolve().parents[2])
