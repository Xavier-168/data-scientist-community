#!/usr/bin/env python3
"""生成并签名 Windows 包清单 package_manifest.json。

流程：
  1. 读取 build_windows_runtime.py 产出的 runtime-manifest-fragment.json
  2. 组装清单 payload（package_id/arch/build_version/runtimes）
  3. 使用 Ed25519 私钥签名（package_identity.sign_package_manifest）
  4. 可选把新公钥注册进 scripts/package_public_keys.json

密钥管理：
  默认私钥路径 .signing-keys/pkg-win-private.pem（已加入 .gitignore）。
  不存在时自动生成新密钥对，并要求 --register-public-key 同步注册公钥。

用法示例：
  python scripts/sign_package_manifest.py \
    --fragment build/windows-runtime/runtime-manifest-fragment.json \
    --output build/windows-runtime/package_manifest.json \
    --register-public-key
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import package_identity

DEFAULT_PACKAGE_ID = "data-scientist-community-win-x64"
DEFAULT_ARCH = "x86_64"
KEYS_DIR = REPO_ROOT / ".signing-keys"


def load_or_create_private_key(path: Path) -> tuple[str, str]:
    """返回 (private_pem, public_pem)。文件不存在时生成新密钥对。"""
    if path.exists():
        private_pem = path.read_text(encoding="utf-8")
        key = serialization.load_pem_private_key(private_pem.encode(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise SystemExit(f"[error] {path} 不是 Ed25519 私钥")
    else:
        key = Ed25519PrivateKey.generate()
        private_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(private_pem, encoding="ascii")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        print(f"[key] 已生成新私钥: {path}（妥善备份，不要提交或分发）")
    public_pem = (
        serialization.load_pem_private_key(private_pem.encode(), password=None)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return private_pem, public_pem


def register_public_key(key_id: str, public_pem: str) -> None:
    bundle_path = REPO_ROOT / "scripts" / "package_public_keys.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    keys = bundle.get("keys") or []
    if any(item.get("key_id") == key_id for item in keys):
        existing = next(item for item in keys if item.get("key_id") == key_id)
        if existing.get("public_key_pem", "").strip() != public_pem.strip():
            raise SystemExit(f"[error] key_id 冲突且公钥不一致: {key_id}")
        print(f"[key] 公钥已注册: {key_id}")
        return
    keys.append({"key_id": key_id, "public_key_pem": public_pem})
    bundle["keys"] = keys
    bundle_path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[key] 已注册公钥: {key_id} -> {bundle_path.relative_to(REPO_ROOT)}")


def strip_descriptor(raw: dict) -> dict:
    """清单描述符仅允许固定字段（Rust 侧 deny_unknown_fields）。"""
    return {
        "version": str(raw["version"]),
        "archive": str(raw["archive"]),
        "sha256": str(raw["sha256"]),
        "tree_sha256": str(raw["tree_sha256"]),
        "size_bytes": int(raw["size_bytes"]),
        "required_files": list(raw["required_files"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fragment",
        default=str(REPO_ROOT / "build" / "windows-runtime" / "runtime-manifest-fragment.json"),
    )
    parser.add_argument(
        "--output",
        default=str(REPO_ROOT / "build" / "windows-runtime" / "package_manifest.json"),
    )
    parser.add_argument("--package-id", default=DEFAULT_PACKAGE_ID)
    parser.add_argument("--build-version", default="")
    parser.add_argument("--key-id", default="")
    parser.add_argument("--private-key", default=str(KEYS_DIR / "pkg-win-private.pem"))
    parser.add_argument("--register-public-key", action="store_true")
    args = parser.parse_args()

    fragment = json.loads(Path(args.fragment).read_text(encoding="utf-8"))
    if "core" not in fragment or "collector" not in fragment:
        raise SystemExit("[error] fragment 缺少 core/collector 描述符，请先运行 build_windows_runtime.py")

    build_version = args.build_version or _dt.datetime.now().strftime("%Y%m%d")
    key_id = args.key_id or f"pkg-win-{build_version}"

    private_pem, public_pem = load_or_create_private_key(Path(args.private_key))
    if args.register_public_key:
        register_public_key(key_id, public_pem)

    payload = {
        "build_version": build_version,
        "key_id": key_id,
        "package_id": args.package_id,
        "arch": DEFAULT_ARCH,
        "supported_architectures": [DEFAULT_ARCH],
        "platform": "win",
        "runtimes": {
            "core": strip_descriptor(fragment["core"]),
            "collector": strip_descriptor(fragment["collector"]),
        },
    }
    signed = package_identity.sign_package_manifest(payload, private_pem)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    package_identity.write_signed_package_manifest(str(output_path.parent), signed)

    # 立即用 Python 侧验证链路回验
    status = package_identity.verify_package_manifest(
        str(output_path.parent), trusted_base_dir=str(REPO_ROOT)
    )
    if not status.get("ok"):
        raise SystemExit(f"[error] 签名后回验失败: {status}")
    print(
        f"[done] {output_path}\n"
        f"       package_id={args.package_id} build_version={build_version} key_id={key_id}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
