#!/usr/bin/env python3
"""Signed package identity helpers.

This module protects customer builds by embedding a signed package manifest
inside the release bundle and verifying it at runtime.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PACKAGE_MANIFEST_FILE_NAME = "package_manifest.json"
PACKAGE_PUBLIC_KEYS_FILE_NAME = os.path.join("scripts", "package_public_keys.json")
PACKAGED_APP_NAMES = ("数据科学家 Community.app", "打开数据中心.app")


def _urlsafe_b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _urlsafe_b64decode(value: str) -> bytes:
    text = str(value or "").strip()
    if not text:
        return b""
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + padding).encode("ascii"))


def normalize_license_key(value: str) -> str:
    return str(value or "").strip().upper()


def license_fingerprint(value: str) -> str:
    normalized = normalize_license_key(value)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def serialize_manifest_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload or {},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _load_public_key(pem_text: str) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(str(pem_text or "").encode("utf-8"))
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("package identity public key must be Ed25519")
    return key


def _load_private_key(pem_text: str) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(str(pem_text or "").encode("utf-8"), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("package identity private key must be Ed25519")
    return key


def package_manifest_required(base_dir: str) -> bool:
    return (
        os.path.isdir(os.path.join(base_dir, "runtime"))
        or os.path.exists(os.path.join(base_dir, "start.command"))
        or os.path.exists(os.path.join(base_dir, "README_START.txt"))
        or any(os.path.isdir(os.path.join(base_dir, app_name)) for app_name in PACKAGED_APP_NAMES)
    )


def read_signed_package_manifest(base_dir: str) -> dict[str, Any] | None:
    path = os.path.join(base_dir, PACKAGE_MANIFEST_FILE_NAME)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError("package manifest must be a JSON object")
    return payload


def load_package_public_key_bundle(base_dir: str) -> dict[str, Any]:
    path = os.path.join(base_dir, PACKAGE_PUBLIC_KEYS_FILE_NAME)
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError("package public key bundle must be a JSON object")
    return payload


def load_trusted_package_keys(base_dir: str) -> dict[str, Ed25519PublicKey]:
    bundle = load_package_public_key_bundle(base_dir)
    keys = bundle.get("keys") or []
    if not isinstance(keys, list) or not keys:
        raise ValueError("package public key bundle must contain keys")
    trusted = {}
    for item in keys:
        if not isinstance(item, dict):
            continue
        key_id = str(item.get("key_id") or "").strip()
        public_key_pem = str(item.get("public_key_pem") or "").strip()
        if not key_id or not public_key_pem:
            continue
        trusted[key_id] = _load_public_key(public_key_pem + ("\n" if not public_key_pem.endswith("\n") else ""))
    if not trusted:
        raise ValueError("package public key bundle did not produce any trusted keys")
    return trusted


def verify_package_manifest(
    base_dir: str,
    *,
    trusted_base_dir: str | None = None,
) -> dict[str, Any]:
    manifest = read_signed_package_manifest(base_dir)
    required = package_manifest_required(base_dir)
    if not manifest:
        if required:
            return {"ok": False, "required": True, "error": "package_manifest_missing"}
        return {"ok": True, "required": False, "present": False, "payload": {}}

    try:
        payload = manifest.get("payload") or {}
        signature_text = str(manifest.get("signature") or "").strip()
        if not isinstance(payload, dict) or not signature_text:
            raise ValueError("package manifest missing payload or signature")
        payload_bytes = serialize_manifest_payload(payload)
        key_id = str(payload.get("key_id") or "").strip()
        public_key = load_trusted_package_keys(trusted_base_dir or base_dir).get(key_id)
        if not public_key:
            raise ValueError("package signing key not trusted")
        public_key.verify(_urlsafe_b64decode(signature_text), payload_bytes)
        return {"ok": True, "required": required, "present": True, "payload": payload}
    except InvalidSignature:
        return {"ok": False, "required": required, "error": "package_manifest_signature_invalid"}
    except Exception as exc:
        return {"ok": False, "required": required, "error": "package_manifest_invalid", "detail": str(exc)}


def package_license_allowed(base_dir: str, license_key: str) -> tuple[bool, str]:
    status = verify_package_manifest(base_dir)
    if not status.get("ok"):
        return False, str(status.get("error") or "package_manifest_invalid")
    payload = status.get("payload") or {}
    required_fingerprint = str(payload.get("license_key_sha256") or "").strip()
    if not required_fingerprint:
        return True, ""
    current_fingerprint = license_fingerprint(license_key)
    if not current_fingerprint or not hmac.compare_digest(required_fingerprint, current_fingerprint):
        return False, "license_not_allowed_for_package"
    return True, ""


def sign_package_manifest(payload: dict[str, Any], private_key_pem: str) -> dict[str, Any]:
    private_key = _load_private_key(private_key_pem)
    payload_bytes = serialize_manifest_payload(payload)
    signature = private_key.sign(payload_bytes)
    return {
        "payload": payload,
        "signature": _urlsafe_b64encode(signature),
    }


def write_signed_package_manifest(base_dir: str, signed_manifest: dict[str, Any]) -> str:
    path = os.path.join(base_dir, PACKAGE_MANIFEST_FILE_NAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(signed_manifest, f, ensure_ascii=False, indent=2)
    return path
