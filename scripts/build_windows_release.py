#!/usr/bin/env python3
"""端到端构建 Windows 发布包（NSIS 安装器）。

编排顺序：
  1. build_community_staging.py   组装源码负载（scripts/frontend/文档）
  2. build_windows_runtime.py     构建并校验运行时包（Python/Node/Chromium）
  3. sign_package_manifest.py     签名 package_manifest.json
  4. 暂存 src-tauri/resources/    （负载 + node_modules + 运行时包 + 清单）
  5. tauri build                  产出 NSIS 安装器与裸 exe

用法：
  python scripts/build_windows_release.py [--skip-runtime-build]

产物：
  desktop/src-tauri/target/release/bundle/nsis/*-setup.exe
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = REPO_ROOT / "build" / "windows-runtime"
RESOURCES_DIR = REPO_ROOT / "desktop" / "src-tauri" / "resources"
NSIS_DIR = REPO_ROOT / "desktop" / "src-tauri" / "target" / "release" / "bundle" / "nsis"


def run(command: list[str], *, cwd: Path = REPO_ROOT, env: dict | None = None) -> None:
    print(f"[exec] {' '.join(command)}")
    merged = os.environ.copy()
    if env:
        merged.update(env)
    subprocess.run(command, cwd=str(cwd), env=merged, check=True)


def stage_resources(staging_app: Path, manifest_path: Path, packs_dir: Path) -> None:
    """把应用负载按 tauri 资源映射布局（resources/**/* → resources/）暂存。"""
    if RESOURCES_DIR.exists():
        shutil.rmtree(RESOURCES_DIR)
    RESOURCES_DIR.mkdir(parents=True)

    # 源码负载（staging 组装产物：scripts/ frontend/ 文档与配置）
    for item in staging_app.iterdir():
        target = RESOURCES_DIR / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)

    # 采集器 node_modules（playwright / @larksuite/cli）
    node_modules = REPO_ROOT / "node_modules"
    if not node_modules.is_dir():
        raise SystemExit("[error] 缺少 node_modules，请先在仓库根执行 npm ci")
    shutil.copytree(node_modules, RESOURCES_DIR / "node_modules")

    # 签名清单与运行时包
    shutil.copy2(manifest_path, RESOURCES_DIR / "package_manifest.json")
    packs_target = RESOURCES_DIR / "runtime-packs"
    packs_target.mkdir()
    for archive in packs_dir.glob("*.tar.zst"):
        shutil.copy2(archive, packs_target / archive.name)

    total = sum(p.stat().st_size for p in RESOURCES_DIR.rglob("*") if p.is_file())
    print(f"[resources] staged {total / 1e6:.1f}MB -> {RESOURCES_DIR}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-runtime-build", action="store_true",
                        help="复用 build/windows-runtime 下既有产物")
    parser.add_argument("--skip-staging", action="store_true",
                        help="跳过 build_community_staging（复用既有 staging）")
    parser.add_argument("--build-version", default="",
                        help="透传给 sign_package_manifest.py（YYYYMMDD[.N]）")
    args = parser.parse_args()

    python_bin = sys.executable

    if not args.skip_staging:
        run([python_bin, str(REPO_ROOT / "scripts" / "build_community_staging.py")])
    if not args.skip_runtime_build:
        run([python_bin, str(REPO_ROOT / "scripts" / "build_windows_runtime.py")])
    sign_command = [
        python_bin,
        str(REPO_ROOT / "scripts" / "sign_package_manifest.py"),
        "--register-public-key",
    ]
    if args.build_version:
        sign_command += ["--build-version", args.build_version]
    run(sign_command)

    staging_app = REPO_ROOT / "build" / "community-staging" / "app"
    if not staging_app.is_dir():
        raise SystemExit("[error] staging 产物缺失，请勿同时使用 --skip-staging/--skip-runtime-build 于首次构建")
    manifest_path = RUNTIME_DIR / "package_manifest.json"
    if not manifest_path.is_file():
        raise SystemExit("[error] 签名清单缺失，请先完成运行时构建")

    stage_resources(staging_app, manifest_path, RUNTIME_DIR / "runtime-packs")

    # tauri build（beforeBuildCommand 会先构建 web 前端）。
    # 走 npm 脚本入口以获得 desktop/node_modules/.bin（tauri CLI）。
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise SystemExit("[error] 未找到 npm，请先准备 Node 22 构建环境")
    run([npm, "run", "build:app"], cwd=REPO_ROOT / "desktop")

    installers = sorted(NSIS_DIR.glob("*.exe")) if NSIS_DIR.is_dir() else []
    if not installers:
        raise SystemExit(f"[error] 未在 {NSIS_DIR} 找到 NSIS 安装器")
    for installer in installers:
        print(f"[done] {installer.name}  {installer.stat().st_size / 1e6:.1f}MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
