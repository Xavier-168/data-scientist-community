#!/usr/bin/env python3
"""构建 Windows 运行时包（core / collector tar.zst）。

产物（默认 build/windows-runtime/）：
  runtime-packs/core-runtime.tar.zst       内嵌 Python 3.12 + 依赖
  runtime-packs/collector-runtime.tar.zst  Node 22 + Playwright Chromium
  runtime-manifest-fragment.json           供 sign_package_manifest.py 使用的描述符

包内布局与桌面壳的清单契约一致：
  core:      runtime/python-x86_64/<dist>/python.exe
  collector: runtime/node-x86_64/<dist>/node.exe + runtime/playwright-browsers/

树哈希算法镜像 desktop/src-tauri/src/runtime/archive.rs 的
runtime_tree_sha256_at：按相对路径字节序遍历，每项写入
类型字节('F'/'D'/'L') + u64le(路径长) + 路径 + u32le(mode) +
u64le(负载长) + 负载（文件内容 / 符号链接目标）。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PYTHON_VERSION = "3.12.10"
PYTHON_EMBED_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_VERSION}/"
    f"python-{PYTHON_VERSION}-embed-amd64.zip"
)
PYTHON_EMBED_SHA256 = (
    "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
)
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
GET_PIP_SHA256 = (
    "fb24e693bab954209a063d90953621412ccad4a500905a726286e038f508ddf6"
)

NODE_VERSION = "22.23.2"
NODE_DIST_BASE = f"https://nodejs.org/dist/latest-v22.x"
NODE_ARCHIVE = f"node-v{NODE_VERSION}-win-x64.zip"

CORE_PACK_VERSION = f"python-{PYTHON_VERSION}-win-x64"
EXECUTABLE_SUFFIXES = (".exe", ".dll", ".bat", ".cmd", ".ps1")

ZSTD_LEVEL = 19


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, destination: Path, expected_sha256: str = "") -> Path:
    if destination.exists():
        if not expected_sha256 or sha256_file(destination) == expected_sha256:
            print(f"[cache] {destination.name}")
            return destination
        destination.unlink()
    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f"[download] {url}")
    try:
        with urllib.request.urlopen(url) as response, open(destination, "wb") as handle:
            shutil.copyfileobj(response, handle)
    except OSError:
        # 部分网络环境（沙箱/CDN 策略）拦截 urllib；curl 走系统证书与连接路径
        curl = shutil.which("curl.exe") or shutil.which("curl")
        if not curl:
            raise
        subprocess.run(
            [curl, "-sSL", "--fail", "-o", str(destination), url], check=True
        )
    if expected_sha256:
        actual = sha256_file(destination)
        if actual != expected_sha256:
            raise RuntimeError(
                f"sha256 mismatch for {destination.name}: {actual} != {expected_sha256}"
            )
    return destination


def node_archive_sha256() -> str:
    """从官方 SHASUMS256.txt 读取 Node 压缩包校验和（HTTPS 交叉验证）。"""
    with urllib.request.urlopen(f"{NODE_DIST_BASE}/SHASUMS256.txt") as response:
        for line in response.read().decode("ascii").splitlines():
            checksum, _, name = line.partition(" ")
            if name.strip() == NODE_ARCHIVE:
                return checksum.strip()
    raise RuntimeError(f"{NODE_ARCHIVE} not found in SHASUMS256.txt")


def file_mode(relpath: str) -> int:
    return 0o755 if relpath.lower().endswith(EXECUTABLE_SUFFIXES) else 0o644


def collect_tree(root: Path) -> list[tuple[str, str, int, int]]:
    """收集确定性排序的树条目。

    返回 (kind, relpath, mode, payload_length)；kind 为 'D' 或 'F'。
    """
    entries: list[tuple[str, str, int, int]] = []
    for current, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        rel_dir = Path(current).relative_to(root).as_posix()
        if rel_dir != ".":
            entries.append(("D", rel_dir, 0o755, 0))
        for name in filenames:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            size = (Path(current) / name).stat().st_size
            entries.append(("F", rel, file_mode(rel), size))
    entries.sort(key=lambda item: item[1].encode("utf-8"))
    return entries


def tree_sha256(root: Path, entries: list[tuple[str, str, int, int]]) -> str:
    digest = hashlib.sha256()
    for kind, relpath, mode, payload_length in entries:
        digest.update(kind.encode("ascii"))
        digest.update(len(relpath.encode("utf-8")).to_bytes(8, "little"))
        digest.update(relpath.encode("utf-8"))
        digest.update(mode.to_bytes(4, "little"))
        digest.update(payload_length.to_bytes(8, "little"))
        if kind != "F":
            continue
        with open(root / relpath, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def write_pack_archive(source_dir: Path, archive_path: Path) -> None:
    """以确定性元数据写入 tar.zst（uid/gid=0、mtime=0、固定 mode、字节序路径）。"""
    import zstandard

    entries = collect_tree(source_dir)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    compressor = zstandard.ZstdCompressor(level=ZSTD_LEVEL)
    with open(archive_path, "wb") as raw:
        with compressor.stream_writer(raw, closefd=False) as zstd_stream:
            with tarfile.open(
                mode="w|", fileobj=zstd_stream, format=tarfile.PAX_FORMAT
            ) as tar:
                for kind, relpath, mode, _size in entries:
                    full = source_dir / relpath
                    info = tarfile.TarInfo(relpath)
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = 0
                    info.mode = mode
                    if kind == "D":
                        info.type = tarfile.DIRTYPE
                        info.size = 0
                        tar.addfile(info)
                    else:
                        info.size = full.stat().st_size
                        with open(full, "rb") as handle:
                            tar.addfile(info, handle)


def build_core_pack(staging: Path, requirements: Path) -> Path:
    pack_root = staging / "core-tree"
    python_dirname = f"python-{PYTHON_VERSION}-embed-amd64"
    target_base = pack_root / "runtime" / "python-x86_64"
    target_base.mkdir(parents=True, exist_ok=True)
    python_home = target_base / python_dirname
    if python_home.exists():
        shutil.rmtree(python_home)
    python_home.mkdir()

    archive = download(
        PYTHON_EMBED_URL, staging / "downloads" / f"python-{PYTHON_VERSION}-embed-amd64.zip",
        PYTHON_EMBED_SHA256,
    )
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(python_home)

    # 解锁 embeddable 发行版的 site-packages（默认 import site 被注释）。
    # 文件名只含大版本+小版本：python312._pth
    major, minor = PYTHON_VERSION.split(".")[:2]
    pth = python_home / f"python{major}{minor}._pth"
    pth.write_text(
        "\n".join(
            [
                f"python{major}{minor}.zip",
                ".",
                "Lib/site-packages",
                "import site",
                "",
            ]
        ),
        encoding="ascii",
    )

    get_pip = download(GET_PIP_URL, staging / "downloads" / "get-pip.py", GET_PIP_SHA256)
    python_exe = python_home / "python.exe"

    def run_pip(*args: str) -> None:
        subprocess.run(
            [str(python_exe), *args], check=True, cwd=str(python_home)
        )

    run_pip(str(get_pip), "--no-warn-script-location")
    run_pip(
        "-m", "pip", "install", "--no-warn-script-location",
        "--prefer-binary", "-r", str(requirements),
    )
    # 清理缓存与字节码，保持包体确定与精简
    for cache in python_home.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    for pyc in python_home.rglob("*.pyc"):
        pyc.unlink()
    return pack_root


def build_collector_pack(staging: Path) -> tuple[Path, str]:
    pack_root = staging / "collector-tree"
    node_dirname = f"node-v{NODE_VERSION}-win-x64"
    node_target = pack_root / "runtime" / "node-x86_64" / node_dirname
    if node_target.exists():
        shutil.rmtree(node_target)
    node_target.mkdir(parents=True, exist_ok=True)

    node_zip = download(
        f"{NODE_DIST_BASE}/{NODE_ARCHIVE}",
        staging / "downloads" / NODE_ARCHIVE,
        node_archive_sha256(),
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        with zipfile.ZipFile(node_zip) as zf:
            zf.extractall(temp_dir)
        extracted = Path(temp_dir) / node_dirname
        for item in extracted.iterdir():
            shutil.move(str(item), node_target / item.name)

    browsers_root = pack_root / "runtime" / "playwright-browsers"
    if browsers_root.exists():
        shutil.rmtree(browsers_root)
    print("[playwright] installing chromium + headless shell into pack")
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_root)
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if not npx:
        raise RuntimeError("npx not found; install Node 22 first")
    subprocess.run(
        [npx, "playwright", "install", "chromium", "chromium-headless-shell"],
        check=True, cwd=str(REPO_ROOT), env=env,
    )

    chromium_build = ""
    for child in sorted(browsers_root.iterdir(), reverse=True):
        if child.is_dir() and child.name.startswith("chromium-"):
            chromium_build = child.name
            break
    if not chromium_build:
        raise RuntimeError("chromium build dir not found after install")
    version = f"node-{NODE_VERSION}-win-x64-{chromium_build}"
    return pack_root, version


def pack_descriptor(kind: str, pack_root: Path, version: str, output_dir: Path) -> dict:
    entries = collect_tree(pack_root)
    tree_digest = tree_sha256(pack_root, entries)
    archive_name = f"{kind}-runtime.tar.zst"
    archive_path = output_dir / "runtime-packs" / archive_name
    write_pack_archive(pack_root, archive_path)
    required_files = sorted(rel for kind_, rel, _m, _s in entries if kind_ == "F")
    return {
        "version": version,
        "archive": archive_name,
        "sha256": sha256_file(archive_path),
        "tree_sha256": tree_digest,
        "size_bytes": archive_path.stat().st_size,
        "required_files": required_files,
        "file_count": len(required_files),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(REPO_ROOT / "build" / "windows-runtime"))
    parser.add_argument("--staging-dir", default="")
    parser.add_argument("--skip-core", action="store_true")
    parser.add_argument("--skip-collector", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    staging = (
        Path(args.staging_dir).resolve()
        if args.staging_dir
        else output_dir / "staging"
    )
    staging.mkdir(parents=True, exist_ok=True)

    fragment: dict[str, dict] = {}
    if not args.skip_core:
        print("[core] building embedded python pack")
        core_root = build_core_pack(staging, REPO_ROOT / "requirements.txt")
        fragment["core"] = pack_descriptor("core", core_root, CORE_PACK_VERSION, output_dir)
        print(
            f"[core] version={fragment['core']['version']} "
            f"files={fragment['core']['file_count']} "
            f"size={fragment['core']['size_bytes'] / 1e6:.1f}MB"
        )
    if not args.skip_collector:
        print("[collector] building node + chromium pack")
        collector_root, collector_version = build_collector_pack(staging)
        fragment["collector"] = pack_descriptor(
            "collector", collector_root, collector_version, output_dir
        )
        print(
            f"[collector] version={fragment['collector']['version']} "
            f"files={fragment['collector']['file_count']} "
            f"size={fragment['collector']['size_bytes'] / 1e6:.1f}MB"
        )

    fragment_path = output_dir / "runtime-manifest-fragment.json"
    fragment_path.write_text(
        json.dumps(fragment, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[done] {fragment_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
