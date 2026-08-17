#!/usr/bin/env python3
"""Build a source-only community application staging directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MARKER = ".community-staging.json"
RUNTIME_FILES = (
    "COMMUNITY_EDITION",
    "LICENSE",
    "NOTICE",
    "PRIVACY.md",
    "DISCLAIMER.md",
    "PLATFORM_COMPLIANCE.md",
    "THIRD_PARTY_NOTICES.md",
    "package.json",
    "package-lock.json",
    "requirements.txt",
    "frontend/loading.html",
    "frontend/progress.html",
    "frontend/assets/progress-apple-theme.css",
    "frontend/assets/progress-figma-dashboard.css",
    "frontend/assets/figma/mailbox.svg",
    "frontend/assets/fonts/NotoSerifSC-Variable.ttf",
    "frontend/assets/fonts/NotoSerifSC-OFL.txt",
    "frontend/assets/platforms/bilibili.svg",
    "frontend/assets/platforms/douyin.svg",
    "frontend/assets/platforms/kuaishou.svg",
    "frontend/assets/vendor/tabler/LICENSE",
    "frontend/assets/vendor/tabler/walk.svg",
)
RUNTIME_SCRIPT_NAMES = (
    "_run.py",
    "analytics_engine.py",
    "bilibili_export.mjs",
    "browser_auth_utils.mjs",
    "build_excel_export.py",
    "client_license.py",
    "douyin_export.mjs",
    "feedback_manager.py",
    "kuaishou_export.mjs",
    "license_public_keys.json",
    "merge_all_videos.py",
    "merge_channels.py",
    "merge_exports.py",
    "normalize_bilibili_official_export.py",
    "normalize_kuaishou_detail_export.py",
    "normalize_xhs_detail_export.py",
    "package_identity.py",
    "package_public_keys.json",
    "platform_source_rows.py",
    "prepare_feishu_bitable_sync_v2.py",
    "run_bili_export.cmd",
    "run_bili_export.sh",
    "run_export.cmd",
    "run_export.sh",
    "run_ks_export.cmd",
    "run_ks_export.sh",
    "run_xhs_export.cmd",
    "run_xhs_export.sh",
    "runner.py",
    "runner_io.py",
    "runner_platforms.py",
    "runner_process.py",
    "runtime_paths.py",
    "seed_browser_profile.mjs",
    "runtime_paths.mjs",
    "start_monitor.py",
    "sync_feishu_bitable_openapi.py",
    "title_cleanup_utils.mjs",
    "update_manager.py",
    "write_rows_excel.py",
    "write_xhs_excel.py",
    "xiaohongshu_export.mjs",
)
RUNTIME_SCRIPT_DIRS = ("core", "domain", "orchestration")
FORBIDDEN_SUFFIXES = {".dmg", ".p12", ".pem", ".pfx", ".pyc", ".pyo", ".so"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_file(relative: str, destination: Path) -> None:
    source = ROOT / relative
    if not source.is_file():
        raise FileNotFoundError(f"required community runtime file missing: {relative}")
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def prepare_destination(destination: Path, replace: bool) -> None:
    if not destination.exists():
        destination.mkdir(parents=True)
        return
    if not replace:
        raise FileExistsError(f"destination already exists: {destination}")
    marker = destination / MARKER
    if not marker.is_file():
        raise RuntimeError(f"refusing to replace directory without {MARKER}: {destination}")
    for child in destination.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def validate_staging(destination: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(destination.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(destination).as_posix()
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            raise RuntimeError(f"forbidden binary in community staging: {relative}")
        hashes[relative] = sha256(path)
    for platform_script in (
        "scripts/douyin_export.mjs",
        "scripts/xiaohongshu_export.mjs",
        "scripts/bilibili_export.mjs",
        "scripts/kuaishou_export.mjs",
    ):
        if platform_script not in hashes:
            raise RuntimeError(f"platform entry missing: {platform_script}")
    return hashes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="build/community-staging/app")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()

    destination = (ROOT / args.output).resolve() if not Path(args.output).is_absolute() else Path(args.output).resolve()
    prepare_destination(destination, args.replace)
    for relative in RUNTIME_FILES:
        copy_file(relative, destination)
    for name in RUNTIME_SCRIPT_NAMES:
        copy_file(f"scripts/{name}", destination)
    for directory in RUNTIME_SCRIPT_DIRS:
        source_dir = ROOT / "scripts" / directory
        for source in sorted(source_dir.rglob("*.py")):
            copy_file(source.relative_to(ROOT).as_posix(), destination)

    with tempfile.TemporaryDirectory(prefix="community-pycache-") as pycache:
        env = os.environ.copy()
        env["PYTHONPYCACHEPREFIX"] = pycache
        subprocess.run(
            [sys.executable, "-m", "compileall", "-q", str(destination / "scripts")],
            check=True,
            env=env,
        )
    for script in sorted((destination / "scripts").glob("*.mjs")):
        subprocess.run(["node", "--check", str(script)], check=True)

    hashes = validate_staging(destination)
    marker_payload = {
        "schema_version": 1,
        "edition": "community",
        "file_count": len(hashes),
        "files": hashes,
    }
    (destination / MARKER).write_text(
        json.dumps(marker_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"ok": True, "output": str(destination), "file_count": len(hashes)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
