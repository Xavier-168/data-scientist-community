#!/usr/bin/env python3
"""Remote update helpers for packaged desktop releases."""

from __future__ import annotations

import hashlib
import contextlib
import fcntl
import json
import os
import pathlib
import plistlib
import re
import shutil
import ssl
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from package_identity import read_signed_package_manifest, verify_package_manifest
from runtime_paths import read_package_id


UPDATE_PLATFORM = "mac"
UPDATE_ARCH = "arm64"
UPDATE_DIR_NAME = "updates"
UPDATE_CHECK_TIMEOUT_SECONDS = 8
UPDATE_DOWNLOAD_TIMEOUT_SECONDS = 120
DEFAULT_UPDATE_SERVER = ""
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
DETACHED_HELPER_STRIP_ENV = (
    "YIRENGONGIS_PROCESS_OWNER_ID",
    "YIRENGONGIS_SESSION_TOKEN",
    "YIRENGONGIS_SUPERVISED_BY_TAURI",
    "YIRENGONGIS_SIDECAR_INSTANCE_ID",
    "YIRENGONGIS_RUNNER_READY_NONCE",
)
INSTALL_LOCK_SUFFIX = ".install.lock"
INSTALL_JOURNAL_SUFFIX = ".install.json"
INSTALL_STAGING_MARKER = ".installing-"
INSTALL_BACKUP_MARKER = ".previous-"
INSTALL_JOURNAL_MAX_BYTES = 64 * 1024
_TRUSTED_PACKAGE_BASE_DIR = pathlib.Path(__file__).resolve().parent.parent

_DOWNLOAD_LOCK = threading.RLock()
_TRUSTED_RELEASE_LOCK = threading.RLock()
_TRUSTED_RELEASES_BY_BASE: dict[str, dict[str, Any]] = {}
_DOWNLOAD_STATE: dict[str, Any] = {
    "ok": True,
    "status": "idle",
    "running": False,
    "downloaded_bytes": 0,
    "total_bytes": 0,
    "percent": 0,
    "path": "",
    "filename": "",
    "message": "",
    "error": "",
    "updated_at": 0,
}


class UpdateValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ReleaseDescriptor:
    package_id: str
    version: str
    platform: str
    arch: str
    download_url: str
    sha256: str
    size_bytes: int


def validate_build_version(value: str) -> str:
    text = _safe_text(value)
    match = re.fullmatch(r"[0-9]{8}(?:\.([0-9]+))?", text)
    if not match or int(match.group(1) or "0") > (2**64 - 1):
        raise UpdateValidationError("invalid_build_version")
    return text


def validate_release(
    payload: dict[str, Any],
    *,
    expected_package_id: str = "data-scientist-community-mac-arm64",
    expected_arch: str = UPDATE_ARCH,
) -> ReleaseDescriptor:
    source = payload if isinstance(payload, dict) else {}
    download_url = _safe_text(source.get("download_url"))
    parsed_url = urlparse(download_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise UpdateValidationError("https_required")
    sha256 = _safe_text(source.get("sha256")).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise UpdateValidationError("sha256_required")
    package_id = _safe_text(source.get("package_id"))
    if package_id != _safe_text(expected_package_id):
        raise UpdateValidationError("package_id_mismatch")
    platform_name = _safe_text(source.get("platform"))
    arch = _safe_text(source.get("arch"))
    if platform_name != UPDATE_PLATFORM or arch != _safe_text(expected_arch):
        raise UpdateValidationError("platform_arch_mismatch")
    try:
        size_bytes = int(source.get("size_bytes") or 0)
    except (TypeError, ValueError) as exc:
        raise UpdateValidationError("size_required") from exc
    if size_bytes <= 0:
        raise UpdateValidationError("size_required")
    return ReleaseDescriptor(
        package_id=package_id,
        version=validate_build_version(source.get("version")),
        platform=platform_name,
        arch=arch,
        download_url=download_url,
        sha256=sha256,
        size_bytes=size_bytes,
    )


def _build_ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_SSL_CONTEXT = _build_ssl_context()


def _package_json_version(base_dir: str) -> str:
    package_path = os.path.join(base_dir, "package.json")
    try:
        with open(package_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return ""
    return str(payload.get("version") or "").strip() if isinstance(payload, dict) else ""


def _manifest_payload(base_dir: str) -> dict[str, Any]:
    manifest = read_signed_package_manifest(base_dir) or {}
    payload = manifest.get("payload") or {}
    return payload if isinstance(payload, dict) else {}


def current_package_info(base_dir: str) -> dict[str, Any]:
    payload = _manifest_payload(base_dir)
    activation_servers = payload.get("activation_servers") or []
    if isinstance(activation_servers, str):
        activation_servers = [activation_servers]
    feedback_endpoint = str(
        payload.get("feedback_endpoint")
        or os.environ.get("YRG_FEEDBACK_ENDPOINT")
        or ""
    ).strip()
    primary_server = str(
        payload.get("activation_server")
        or os.environ.get("YRG_UPDATE_SERVER")
        or os.environ.get("YRG_ACTIVATION_SERVER")
        or DEFAULT_UPDATE_SERVER
    ).strip()
    if primary_server and primary_server not in activation_servers:
        activation_servers = [primary_server, *activation_servers]
    return {
        "package_id": str(payload.get("package_id") or read_package_id(base_dir) or "data-scientist-community-mac-arm64").strip(),
        "build_version": str(payload.get("build_version") or _package_json_version(base_dir) or "1.0.0").strip(),
        "platform": UPDATE_PLATFORM,
        "arch": UPDATE_ARCH,
        "activation_server": primary_server,
        "activation_servers": [str(item).strip().rstrip("/") for item in activation_servers if str(item).strip()],
        "feedback_endpoint": feedback_endpoint.rstrip("/"),
        "customer_name": str(payload.get("customer_name") or "").strip(),
    }


def _version_parts(value: str) -> list[int | str]:
    text = str(value or "").strip().lower()
    if not text:
        return []
    parts: list[int | str] = []
    for chunk in re.findall(r"\d+|[a-z]+", text):
        if chunk.isdigit():
            parts.append(int(chunk))
        else:
            parts.append(chunk)
    return parts


def compare_versions(left: str, right: str) -> int:
    """Return -1/0/1 when left is older/equal/newer than right."""
    left_parts = _version_parts(left)
    right_parts = _version_parts(right)
    max_len = max(len(left_parts), len(right_parts))
    for index in range(max_len):
        left_item = left_parts[index] if index < len(left_parts) else 0
        right_item = right_parts[index] if index < len(right_parts) else 0
        if left_item == right_item:
            continue
        if isinstance(left_item, int) and isinstance(right_item, int):
            return -1 if left_item < right_item else 1
        return -1 if str(left_item) < str(right_item) else 1
    return 0


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_latest_payload(payload: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    if "latest" in payload:
        latest = payload.get("latest") if isinstance(payload.get("latest"), dict) else {}
    else:
        latest = payload
    if not isinstance(latest, dict):
        latest = {}
    latest_version = _safe_text(
        latest.get("internal_version")
        or latest.get("build_version")
        or latest.get("version")
    )
    current_version = _safe_text(current.get("build_version"))
    update_available = bool(payload.get("update_available"))
    if latest_version and current_version:
        update_available = compare_versions(current_version, latest_version) < 0
    return {
        "version": latest_version,
        "download_url": _safe_text(latest.get("download_url")),
        "sha256": _safe_text(latest.get("sha256")).lower(),
        "size_bytes": int(latest.get("size_bytes") or 0),
        "release_notes": _safe_text(latest.get("release_notes")),
        "mandatory": bool(latest.get("mandatory")),
        "published_at": _safe_text(latest.get("published_at")),
        "package_id": _safe_text(latest.get("package_id")),
        "platform": _safe_text(latest.get("platform")),
        "arch": _safe_text(latest.get("arch")),
        "update_available": update_available,
    }


def _trusted_release_key(base_dir: str) -> str:
    return os.path.realpath(str(base_dir or ""))


def _store_trusted_release(base_dir: str, release: dict[str, Any] | None) -> None:
    key = _trusted_release_key(base_dir)
    with _TRUSTED_RELEASE_LOCK:
        if release:
            _TRUSTED_RELEASES_BY_BASE[key] = dict(release)
        else:
            _TRUSTED_RELEASES_BY_BASE.pop(key, None)


def _load_trusted_release(base_dir: str) -> dict[str, Any]:
    key = _trusted_release_key(base_dir)
    with _TRUSTED_RELEASE_LOCK:
        return dict(_TRUSTED_RELEASES_BY_BASE.get(key) or {})


def _fetch_json(url: str, timeout: int = UPDATE_CHECK_TIMEOUT_SECONDS) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "YRG-Desktop-Updater/1.0"})
    with urlopen(request, timeout=timeout, context=_SSL_CONTEXT) as response:
        raw = response.read(1024 * 1024)
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("update response must be a JSON object")
    return payload


def check_for_update(base_dir: str, *, override_url: str = "") -> dict[str, Any]:
    current = current_package_info(base_dir)
    _store_trusted_release(base_dir, None)
    allow_override = os.environ.get("YRG_ALLOW_UPDATE_SERVER_OVERRIDE") == "1"
    if override_url and allow_override:
        servers = [override_url.rstrip("/")]
    else:
        servers = current.get("activation_servers") or []
    if not servers:
        return {
            "ok": False,
            "error": "update_service_not_configured",
            "unavailable": True,
            "current": current,
            "latest": {},
            "update_available": False,
            "update_status": "not_configured",
            "message": "暂无可用更新",
            "checked_at": int(time.time()),
        }

    query = urlencode({
        "package_id": current.get("package_id") or "",
        "platform": current.get("platform") or UPDATE_PLATFORM,
        "arch": current.get("arch") or UPDATE_ARCH,
        "current_version": current.get("build_version") or "",
    })
    errors = []
    for server in servers:
        url = f"{server}/updates/latest?{query}"
        try:
            payload = _fetch_json(url)
            if not payload.get("ok"):
                errors.append({"server": server, "error": payload.get("error") or "update_check_failed"})
                continue
            latest = _normalize_latest_payload(payload, current)
            if latest.get("update_available"):
                descriptor = validate_release(
                    latest,
                    expected_package_id=_safe_text(current.get("package_id")),
                    expected_arch=_safe_text(current.get("arch") or UPDATE_ARCH),
                )
                latest.update(asdict(descriptor))
                _store_trusted_release(base_dir, latest)
            return {
                "ok": True,
                "current": current,
                "latest": latest,
                "update_available": bool(latest.get("update_available")),
                "checked_at": int(time.time()),
                "server": server,
            }
        except Exception as exc:
            errors.append({"server": server, "error": str(exc)})

    return {
        "ok": False,
        "error": "update_service_unavailable",
        "unavailable": True,
        "current": current,
        "latest": {},
        "update_available": False,
        "update_status": "unavailable",
        "message": "暂时无法连接更新服务，请稍后重试",
        "checked_at": int(time.time()),
        "errors": errors,
    }


def _update_download_dir(state_dir: str) -> str:
    path = os.path.join(state_dir, UPDATE_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def _filename_from_url(url: str, fallback_version: str) -> str:
    parsed = urlparse(url)
    name = os.path.basename(parsed.path) or f"yirengongis-monitor-{fallback_version or 'latest'}.dmg"
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return name or "yirengongis-monitor-latest.dmg"


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_snapshot() -> dict[str, Any]:
    with _DOWNLOAD_LOCK:
        payload = dict(_DOWNLOAD_STATE)
    payload["ok"] = bool(payload.get("ok", True))
    payload["running"] = payload.get("status") in {"queued", "running", "verifying"}
    return payload


def _set_download_state(**updates: Any) -> dict[str, Any]:
    with _DOWNLOAD_LOCK:
        _DOWNLOAD_STATE.update(updates)
        _DOWNLOAD_STATE["updated_at"] = int(time.time())
        total = int(_DOWNLOAD_STATE.get("total_bytes") or 0)
        downloaded = int(_DOWNLOAD_STATE.get("downloaded_bytes") or 0)
        if total > 0:
            _DOWNLOAD_STATE["percent"] = max(0, min(100, round(downloaded * 100 / total)))
        else:
            _DOWNLOAD_STATE["percent"] = 0
        return dict(_DOWNLOAD_STATE)


def get_download_progress(state_dir: str) -> dict[str, Any]:
    return _download_snapshot()


def _download_worker(
    *,
    download_url: str,
    target_path: str,
    tmp_path: str,
    filename: str,
    latest: dict[str, Any],
) -> None:
    expected_sha256 = _safe_text(latest.get("sha256")).lower()
    fallback_total = int(latest.get("size_bytes") or 0)
    digest = hashlib.sha256()
    downloaded = 0
    try:
        request = Request(download_url, headers={"User-Agent": "YRG-Desktop-Updater/1.0"})
        with urlopen(request, timeout=UPDATE_DOWNLOAD_TIMEOUT_SECONDS, context=_SSL_CONTEXT) as response, open(tmp_path, "wb") as handle:
            header_total = int(response.headers.get("Content-Length") or 0)
            total = header_total or fallback_total
            _set_download_state(
                ok=True,
                status="running",
                running=True,
                total_bytes=total,
                downloaded_bytes=0,
                path="",
                filename=filename,
                message="正在下载新版安装包...",
                error="",
            )
            last_update = 0.0
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                handle.write(chunk)
                digest.update(chunk)
                downloaded += len(chunk)
                now = time.time()
                if now - last_update >= 0.25:
                    _set_download_state(downloaded_bytes=downloaded)
                    last_update = now

        _set_download_state(status="verifying", downloaded_bytes=downloaded, message="正在校验安装包...")
        actual_sha256 = digest.hexdigest()
        if fallback_total <= 0 or downloaded != fallback_total:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            _set_download_state(
                ok=False,
                status="error",
                running=False,
                error="size_mismatch",
                message="新版安装包大小校验失败，请稍后重新下载。",
                actual_size_bytes=downloaded,
                expected_size_bytes=fallback_total,
            )
            return
        if expected_sha256 and actual_sha256 != expected_sha256:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            _set_download_state(
                ok=False,
                status="error",
                running=False,
                error="sha256_mismatch",
                message="新版安装包校验失败，请稍后重新下载。",
                actual_sha256=actual_sha256,
                expected_sha256=expected_sha256,
            )
            return

        os.replace(tmp_path, target_path)
        _set_download_state(
            ok=True,
            status="completed",
            running=False,
            downloaded_bytes=os.path.getsize(target_path),
            total_bytes=os.path.getsize(target_path),
            path=target_path,
            filename=filename,
            sha256=actual_sha256,
            size_bytes=os.path.getsize(target_path),
            release=dict(latest),
            message="新版安装包已下载完成，准备安装...",
            error="",
        )
    except Exception as exc:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        _set_download_state(
            ok=False,
            status="error",
            running=False,
            downloaded_bytes=downloaded,
            error="download_failed",
            message=f"下载新版失败：{exc}",
        )


def download_update(
    state_dir: str,
    latest: dict[str, Any] | None = None,
    *,
    base_dir: str = "",
) -> dict[str, Any]:
    if not base_dir:
        return {
            "ok": False,
            "error": "trusted_release_required",
            "message": "请先通过应用内检查更新获取可信版本信息。",
        }
    trusted_latest = _load_trusted_release(base_dir)
    requested_version = _safe_text((latest or {}).get("version"))
    if not trusted_latest or (
        requested_version and requested_version != _safe_text(trusted_latest.get("version"))
    ):
        return {
            "ok": False,
            "error": "trusted_release_required",
            "message": "更新信息已过期，请重新检查更新。",
        }
    current = current_package_info(base_dir)
    descriptor = validate_release(
        trusted_latest,
        expected_package_id=_safe_text(current.get("package_id")),
        expected_arch=_safe_text(current.get("arch") or UPDATE_ARCH),
    )
    latest = {**trusted_latest, **asdict(descriptor)}
    download_url = descriptor.download_url
    if not download_url:
        return {"ok": False, "error": "download_url_missing", "message": "新版下载地址为空。"}
    parsed = urlparse(download_url)
    if parsed.scheme != "https":
        return {"ok": False, "error": "https_required", "message": "新版下载地址必须使用 HTTPS。"}

    update_dir = _update_download_dir(state_dir)
    filename = _filename_from_url(download_url, _safe_text(latest.get("version")))
    target_path = os.path.join(update_dir, filename)
    tmp_path = f"{target_path}.part"
    with _DOWNLOAD_LOCK:
        status = str(_DOWNLOAD_STATE.get("status") or "")
        active_url = str(_DOWNLOAD_STATE.get("download_url") or "")
        if status in {"queued", "running", "verifying"}:
            return _download_snapshot()
        if status == "completed" and active_url == download_url and os.path.exists(str(_DOWNLOAD_STATE.get("path") or "")):
            return _download_snapshot()
        _DOWNLOAD_STATE.update(
            {
                "ok": True,
                "status": "queued",
                "running": True,
                "download_url": download_url,
                "downloaded_bytes": 0,
                "total_bytes": int((latest or {}).get("size_bytes") or 0),
                "percent": 0,
                "path": "",
                "filename": filename,
                "message": "已开始下载新版安装包...",
                "error": "",
                "release": {},
                "updated_at": int(time.time()),
            }
        )

    thread = threading.Thread(
        target=_download_worker,
        kwargs={
            "download_url": download_url,
            "target_path": target_path,
            "tmp_path": tmp_path,
            "filename": filename,
            "latest": latest or {},
        },
        daemon=True,
    )
    thread.start()
    return _download_snapshot()


def _install_lock_path(target_app: os.PathLike | str) -> pathlib.Path:
    target = pathlib.Path(target_app)
    return target.with_name(f".{target.name}{INSTALL_LOCK_SUFFIX}")


@dataclass(frozen=True)
class _InstallDirectory:
    descriptor: int
    target_name: str


@dataclass(frozen=True)
class _InstallEntryCapability(os.PathLike):
    path: pathlib.Path
    parent_fd: int
    entry_name: str

    def __fspath__(self) -> str:
        return os.fspath(self.path)


def _trusted_macos_alias(path: pathlib.Path) -> pathlib.Path:
    text = os.fspath(path)
    for alias, destination in (("/var", "/private/var"), ("/tmp", "/private/tmp")):
        if text == alias or text.startswith(alias + os.sep):
            try:
                metadata = os.lstat(alias)
            except OSError:
                break
            if stat.S_ISLNK(metadata.st_mode) and os.path.realpath(alias) == destination:
                suffix = text[len(alias) :].lstrip(os.sep)
                return pathlib.Path(destination) / suffix
            break
    return path


def _open_install_parent(
    target_app: os.PathLike | str,
    *,
    create: bool,
) -> tuple[_InstallDirectory, int]:
    target = pathlib.Path(os.path.abspath(os.fspath(target_app)))
    target = _trusted_macos_alias(target)
    if not target.name or target.name in {".", ".."}:
        raise RuntimeError("install_applications_invalid")
    parent = target.parent
    if not parent.is_absolute():
        raise RuntimeError("install_applications_invalid")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(os.sep, directory_flags)
    try:
        for component in parent.parts[1:]:
            if not component or component in {".", ".."}:
                raise RuntimeError("install_applications_invalid")
            try:
                child = os.open(component, directory_flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise RuntimeError("install_applications_invalid") from None
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                try:
                    child = os.open(component, directory_flags, dir_fd=descriptor)
                except OSError as exc:
                    raise RuntimeError("install_applications_invalid") from exc
                os.fsync(descriptor)
            except OSError as exc:
                raise RuntimeError("install_applications_invalid") from exc
            metadata = os.fstat(child)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(child)
                raise RuntimeError("install_applications_invalid")
            os.close(descriptor)
            descriptor = child
        return _InstallDirectory(descriptor, target.name), descriptor
    except Exception:
        os.close(descriptor)
        raise


@contextlib.contextmanager
def _install_directory(
    target_app: os.PathLike | str,
    *,
    create: bool,
):
    directory, descriptor = _open_install_parent(target_app, create=create)
    try:
        yield directory
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _journal_directory(
    target: pathlib.Path,
    directory: _InstallDirectory | None,
):
    if directory is not None:
        if directory.target_name != target.name:
            raise RuntimeError("install_directory_mismatch")
        yield directory
        return
    with _install_directory(target, create=False) as opened:
        yield opened


@contextlib.contextmanager
def install_transaction_lock(
    target_app: os.PathLike | str,
    *,
    blocking: bool = True,
):
    target = pathlib.Path(os.path.abspath(os.fspath(target_app)))
    with _install_directory(target, create=True) as directory:
        lock_name = _install_lock_path(target).name
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(lock_name, flags, 0o600, dir_fd=directory.descriptor)
        except OSError as exc:
            raise RuntimeError("install_lock_invalid") from exc
        try:
            held = os.fstat(descriptor)
            visible = os.stat(
                lock_name,
                dir_fd=directory.descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(held.st_mode)
                or held.st_nlink != 1
                or (held.st_dev, held.st_ino) != (visible.st_dev, visible.st_ino)
            ):
                raise RuntimeError("install_lock_invalid")
            os.fchmod(descriptor, 0o600)
            operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(descriptor, operation)
            except BlockingIOError as exc:
                raise RuntimeError("install_already_running") from exc
            yield directory
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)


def _strict_install_version(value: object) -> tuple[int, int]:
    text = validate_build_version(value)
    date, separator, revision = text.partition(".")
    return int(date), int(revision) if separator else 0


def _compare_install_versions(left: object, right: object) -> int:
    left_parts = _strict_install_version(left)
    right_parts = _strict_install_version(right)
    return (left_parts > right_parts) - (left_parts < right_parts)


def _remove_path(path: os.PathLike | str) -> None:
    target = os.fspath(path)
    if os.path.isdir(target) and not os.path.islink(target):
        shutil.rmtree(target)
    else:
        try:
            os.remove(target)
        except FileNotFoundError:
            pass


def copy_app(source_app: os.PathLike | str, destination_app: os.PathLike | str) -> None:
    source = os.fspath(source_app)
    destination = os.fspath(destination_app)
    _remove_path(destination)
    shutil.copytree(source, destination, symlinks=True)


def _require_real_directory(path: pathlib.Path, error: str) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise UpdateValidationError(error) from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise UpdateValidationError(error)


def _verify_runtime_pack(
    path: pathlib.Path,
    *,
    expected_size: object,
    expected_sha256: object,
) -> None:
    if type(expected_size) is not int or expected_size <= 0:
        raise UpdateValidationError("runtime_pack_descriptor_invalid")
    digest_text = _safe_text(expected_sha256)
    if not re.fullmatch(r"[0-9a-f]{64}", digest_text):
        raise UpdateValidationError("runtime_pack_descriptor_invalid")
    try:
        visible = os.lstat(path)
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
    except OSError as exc:
        raise UpdateValidationError("runtime_pack_invalid") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(visible.st_mode)
            or stat.S_ISLNK(visible.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or visible.st_nlink != 1
            or opened.st_nlink != 1
            or (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_size != expected_size
        ):
            raise UpdateValidationError("runtime_pack_invalid")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as handle:
            while True:
                chunk = handle.read(DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
        descriptor = -1
        visible_after = os.lstat(path)
        if (visible_after.st_dev, visible_after.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise UpdateValidationError("runtime_pack_invalid")
        if digest.hexdigest() != digest_text:
            raise UpdateValidationError("runtime_pack_checksum_mismatch")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def validate_mounted_app(
    app_path: os.PathLike | str,
    expected_release: dict[str, Any] | None = None,
    *,
    trusted_base_dir: os.PathLike | str | None = None,
) -> dict[str, Any]:
    app = pathlib.Path(os.path.abspath(os.fspath(app_path)))
    contents_dir = app / "Contents"
    resources_path = contents_dir / "Resources"
    _require_real_directory(app, "package_layout_invalid")
    _require_real_directory(contents_dir, "package_layout_invalid")
    _require_real_directory(resources_path, "package_layout_invalid")
    resources_dir = os.fspath(resources_path)
    payload_dir = ""
    status: dict[str, Any] = {}
    trusted_base = os.fspath(trusted_base_dir or _TRUSTED_PACKAGE_BASE_DIR)
    for candidate in (resources_path, resources_path / "app"):
        if candidate != resources_path:
            try:
                _require_real_directory(candidate, "package_layout_invalid")
            except UpdateValidationError:
                continue
        manifest_path = candidate / "package_manifest.json"
        try:
            manifest_metadata = os.lstat(manifest_path)
        except OSError:
            manifest_metadata = None
        if manifest_metadata is not None and (
            not stat.S_ISREG(manifest_metadata.st_mode)
            or stat.S_ISLNK(manifest_metadata.st_mode)
            or manifest_metadata.st_nlink != 1
        ):
            raise UpdateValidationError("package_manifest_invalid")
        candidate_status = verify_package_manifest(
            os.fspath(candidate),
            trusted_base_dir=trusted_base,
        )
        if candidate_status.get("ok") and candidate_status.get("present"):
            payload_dir = os.fspath(candidate)
            status = candidate_status
            break
        if candidate_status.get("present"):
            status = candidate_status
            break
    if not status.get("ok") or not status.get("present"):
        raise UpdateValidationError(status.get("error") or "package_manifest_invalid")
    manifest_payload = status.get("payload") or {}
    expected = expected_release or {}
    field_pairs = {
        "package_id": "package_id_mismatch",
        "build_version": "version_mismatch",
        "platform": "platform_arch_mismatch",
        "arch": "platform_arch_mismatch",
    }
    expected_values = {
        "package_id": _safe_text(expected.get("package_id")),
        "build_version": _safe_text(expected.get("version")),
        "platform": _safe_text(expected.get("platform") or UPDATE_PLATFORM),
        "arch": _safe_text(expected.get("arch") or UPDATE_ARCH),
    }
    for key, error in field_pairs.items():
        if expected_values[key] and _safe_text(manifest_payload.get(key)) != expected_values[key]:
            raise UpdateValidationError(error)
    if os.path.normpath(payload_dir) == os.path.normpath(resources_dir):
        packs_dir = resources_path / "runtime-packs"
        _require_real_directory(packs_dir, "package_layout_invalid")
        required_paths = [
            os.path.join(app, "Contents", "MacOS", "data-scientist"),
            os.path.join(resources_dir, "package_manifest.json"),
        ]
        runtimes = manifest_payload.get("runtimes") or {}
        for kind in ("core", "collector"):
            candidate_descriptor = runtimes.get(kind) if isinstance(runtimes, dict) else {}
            descriptor = candidate_descriptor if isinstance(candidate_descriptor, dict) else {}
            archive = _safe_text(descriptor.get("archive"))
            if archive != f"{kind}-runtime.tar.zst":
                raise UpdateValidationError("required_entrypoint_missing")
            archive_path = packs_dir / archive
            _verify_runtime_pack(
                archive_path,
                expected_size=descriptor.get("size_bytes"),
                expected_sha256=descriptor.get("sha256"),
            )
            required_paths.append(os.fspath(archive_path))
    else:
        required_paths = [
            os.path.join(app, "Contents", "MacOS", "launcher"),
            os.path.join(payload_dir, "scripts", "start_monitor.py"),
            os.path.join(payload_dir, "scripts", "_run.py"),
        ]
    if not all(os.path.exists(path) for path in required_paths):
        raise UpdateValidationError("required_entrypoint_missing")
    return manifest_payload


def _install_journal_path(target: pathlib.Path) -> pathlib.Path:
    return target.with_name(f".{target.name}{INSTALL_JOURNAL_SUFFIX}")


def _install_transaction_paths(
    target: pathlib.Path,
    transaction_id: str,
) -> tuple[pathlib.Path, pathlib.Path]:
    canonical_id = str(uuid.UUID(transaction_id))
    if canonical_id != transaction_id:
        raise RuntimeError("install_journal_invalid")
    return (
        target.with_name(f".{target.name}{INSTALL_STAGING_MARKER}{canonical_id}"),
        target.with_name(f".{target.name}{INSTALL_BACKUP_MARKER}{canonical_id}"),
    )


def _fsync_directory(path: pathlib.Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _install_directory_path(directory: _InstallDirectory) -> pathlib.Path:
    try:
        raw = fcntl.fcntl(directory.descriptor, 50, b"\0" * 1024)
        path = pathlib.Path(raw.split(b"\0", 1)[0].decode("utf-8"))
    except (OSError, UnicodeDecodeError):
        proc_path = pathlib.Path(f"/proc/self/fd/{directory.descriptor}")
        if not proc_path.exists():
            raise RuntimeError("install_directory_path_unavailable") from None
        path = pathlib.Path(os.readlink(proc_path))
    visible = os.stat(path, follow_symlinks=False)
    held = os.fstat(directory.descriptor)
    if (
        not stat.S_ISDIR(visible.st_mode)
        or (visible.st_dev, visible.st_ino) != (held.st_dev, held.st_ino)
    ):
        raise RuntimeError("install_directory_changed")
    return path


def _install_entry_path(
    directory: _InstallDirectory,
    entry: pathlib.Path,
) -> _InstallEntryCapability:
    if not entry.name or entry.name in {".", ".."}:
        raise RuntimeError("install_entry_invalid")
    return _InstallEntryCapability(
        path=_install_directory_path(directory) / entry.name,
        parent_fd=directory.descriptor,
        entry_name=entry.name,
    )


def _entry_stat(
    directory: _InstallDirectory,
    entry: pathlib.Path,
) -> os.stat_result | None:
    try:
        return os.stat(
            entry.name,
            dir_fd=directory.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None


def _replace_install_entry(
    source: pathlib.Path,
    destination: pathlib.Path,
    directory: _InstallDirectory,
) -> None:
    os.replace(
        source.name,
        destination.name,
        src_dir_fd=directory.descriptor,
        dst_dir_fd=directory.descriptor,
    )
    os.fsync(directory.descriptor)


def _validate_install_journal(target: pathlib.Path, payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "phase",
        "transaction_id",
        "staging_name",
        "backup_name",
        "had_previous",
        "package_id",
        "build_version",
    }:
        raise RuntimeError("install_journal_invalid")
    transaction_id = str(payload.get("transaction_id") or "")
    staging, backup = _install_transaction_paths(target, transaction_id)
    if (
        payload.get("schema_version") != 1
        or payload.get("phase") not in {"prepared", "backup_moved", "target_switched", "committed"}
        or str(payload.get("staging_name") or "") != staging.name
        or str(payload.get("backup_name") or "") != backup.name
        or not isinstance(payload.get("had_previous"), bool)
        or not _safe_text(payload.get("package_id"))
    ):
        raise RuntimeError("install_journal_invalid")
    _strict_install_version(payload.get("build_version"))
    return dict(payload)


def _read_install_journal(
    target: pathlib.Path,
    *,
    directory: _InstallDirectory | None = None,
) -> dict[str, Any] | None:
    if directory is None:
        with _install_directory(target, create=False) as opened:
            return _read_install_journal(target, directory=opened)
    if directory.target_name != target.name:
        raise RuntimeError("install_directory_mismatch")
    path_name = _install_journal_path(target).name
    try:
        metadata = os.stat(
            path_name,
            dir_fd=directory.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > INSTALL_JOURNAL_MAX_BYTES
    ):
        raise RuntimeError("install_journal_invalid")
    try:
        descriptor = os.open(
            path_name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory.descriptor,
        )
    except OSError as exc:
        raise RuntimeError("install_journal_invalid") from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise RuntimeError("install_journal_invalid")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        visible_after = os.stat(
            path_name,
            dir_fd=directory.descriptor,
            follow_symlinks=False,
        )
        if (opened.st_dev, opened.st_ino) != (
            visible_after.st_dev,
            visible_after.st_ino,
        ):
            raise RuntimeError("install_journal_invalid")
    except Exception as exc:
        raise RuntimeError("install_journal_invalid") from exc
    return _validate_install_journal(target, payload)


def _write_install_journal(
    target: pathlib.Path,
    payload: dict[str, Any],
    *,
    directory: _InstallDirectory | None = None,
) -> None:
    journal = _validate_install_journal(target, payload)
    path_name = _install_journal_path(target).name
    temporary_name = f"{path_name}.tmp-{uuid.uuid4()}"
    with _journal_directory(target, directory) as held:
        try:
            existing = os.stat(
                path_name,
                dir_fd=held.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1
        ):
            raise RuntimeError("install_journal_invalid")
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=held.descriptor,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    journal,
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(
                temporary_name,
                path_name,
                src_dir_fd=held.descriptor,
                dst_dir_fd=held.descriptor,
            )
            os.fsync(held.descriptor)
        except Exception:
            try:
                os.unlink(temporary_name, dir_fd=held.descriptor)
            except FileNotFoundError:
                pass
            raise


def _remove_install_journal(
    target: pathlib.Path,
    *,
    directory: _InstallDirectory | None = None,
) -> None:
    if directory is None:
        with _install_directory(target, create=False) as opened:
            _remove_install_journal(target, directory=opened)
        return
    path_name = _install_journal_path(target).name
    try:
        metadata = os.stat(
            path_name,
            dir_fd=directory.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RuntimeError("install_journal_invalid")
    os.unlink(path_name, dir_fd=directory.descriptor)
    os.fsync(directory.descriptor)


def _real_app_directory(
    path: pathlib.Path,
    directory: _InstallDirectory | None = None,
) -> bool:
    if directory is None:
        with _install_directory(path, create=False) as opened:
            return _real_app_directory(path, opened)
    metadata = _entry_stat(directory, path)
    if metadata is None:
        return False
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError("install_entry_invalid")
    return True


def _remove_tree_at(parent_descriptor: int, name: str) -> None:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise RuntimeError("install_entry_invalid")
    child = os.open(name, directory_flags, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(child)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError("install_entry_changed")
        for item in os.listdir(child):
            if item in {".", ".."}:
                continue
            metadata = os.stat(item, dir_fd=child, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                _remove_tree_at(child, item)
            else:
                os.unlink(item, dir_fd=child)
        os.fsync(child)
        visible = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino):
            raise RuntimeError("install_entry_changed")
    finally:
        os.close(child)
    os.rmdir(name, dir_fd=parent_descriptor)
    os.fsync(parent_descriptor)


def _remove_known_app(
    path: pathlib.Path,
    directory: _InstallDirectory | None = None,
) -> None:
    if directory is None:
        with _install_directory(path, create=False) as opened:
            _remove_known_app(path, opened)
        return
    if _real_app_directory(path, directory):
        _remove_tree_at(directory.descriptor, path.name)


def _journal_expected_release(journal: dict[str, Any]) -> dict[str, Any]:
    return {
        "package_id": journal["package_id"],
        "version": journal["build_version"],
        "platform": UPDATE_PLATFORM,
        "arch": UPDATE_ARCH,
    }


def _validate_journal_app(path: pathlib.Path, journal: dict[str, Any]) -> dict[str, Any]:
    return validate_mounted_app(path, _journal_expected_release(journal))


def _validate_mounted_app_at(
    path: pathlib.Path,
    expected_release: dict[str, Any] | None,
    directory: _InstallDirectory,
) -> dict[str, Any]:
    before = _entry_stat(directory, path)
    if before is None or not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise RuntimeError("install_entry_invalid")
    result = validate_mounted_app(
        _install_entry_path(directory, path),
        expected_release,
    )
    after = _entry_stat(directory, path)
    if after is None or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise RuntimeError("install_entry_changed")
    return result


def _validate_journal_app_at(
    path: pathlib.Path,
    journal: dict[str, Any],
    directory: _InstallDirectory,
) -> dict[str, Any]:
    return _validate_mounted_app_at(
        path,
        _journal_expected_release(journal),
        directory,
    )


def _verify_code_signature_at(
    path: pathlib.Path,
    directory: _InstallDirectory,
    verifier,
) -> None:
    if verifier is None:
        return
    before = _entry_stat(directory, path)
    if before is None or not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise RuntimeError("install_entry_invalid")
    verifier(_install_entry_path(directory, path))
    after = _entry_stat(directory, path)
    if after is None or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise RuntimeError("install_entry_changed")


def _cleanup_journal_staging(
    target: pathlib.Path,
    journal: dict[str, Any],
    directory: _InstallDirectory,
    verifier=None,
) -> None:
    staging, _ = _install_transaction_paths(target, journal["transaction_id"])
    if _real_app_directory(staging, directory):
        _validate_journal_app_at(staging, journal, directory)
        _verify_code_signature_at(staging, directory, verifier)
        _remove_known_app(staging, directory)


def _install_artifact_names(
    target: pathlib.Path,
    directory: _InstallDirectory,
) -> set[str]:
    prefixes = (
        f".{target.name}{INSTALL_STAGING_MARKER}",
        f".{target.name}{INSTALL_BACKUP_MARKER}",
        f"{_install_journal_path(target).name}.tmp-",
    )
    try:
        names = os.listdir(directory.descriptor)
    except OSError as exc:
        raise RuntimeError("install_artifact_scan_failed") from exc
    return {
        name
        for name in names
        if isinstance(name, str) and name.startswith(prefixes)
    }


def _assert_owned_install_artifacts(
    target: pathlib.Path,
    directory: _InstallDirectory,
    journal: dict[str, Any] | None,
) -> None:
    present = _install_artifact_names(target, directory)
    allowed = (
        {journal["staging_name"], journal["backup_name"]}
        if journal is not None
        else set()
    )
    if present - allowed:
        raise RuntimeError("install_orphan_artifact")


def _recover_install_transaction(
    target: pathlib.Path,
    *,
    directory: _InstallDirectory | None = None,
    verify_code_signature=None,
    verify_candidate_code_signature=None,
) -> bool:
    if directory is None:
        with _install_directory(target, create=False) as opened:
            return _recover_install_transaction(
                target,
                directory=opened,
                verify_code_signature=verify_code_signature,
                verify_candidate_code_signature=verify_candidate_code_signature,
            )
    if directory.target_name != target.name:
        raise RuntimeError("install_directory_mismatch")
    journal = _read_install_journal(target, directory=directory)
    _assert_owned_install_artifacts(target, directory, journal)
    if journal is None:
        return False
    staging, backup = _install_transaction_paths(target, journal["transaction_id"])
    if journal["phase"] == "committed" and _real_app_directory(target, directory):
        try:
            _validate_journal_app_at(target, journal, directory)
            _verify_code_signature_at(
                target,
                directory,
                verify_candidate_code_signature or verify_code_signature,
            )
        except Exception:
            pass
        else:
            try:
                _cleanup_journal_staging(
                    target,
                    journal,
                    directory,
                    verify_candidate_code_signature or verify_code_signature,
                )
                if _real_app_directory(backup, directory):
                    backup_payload = _validate_mounted_app_at(backup, None, directory)
                    if _safe_text(backup_payload.get("package_id")) != journal["package_id"]:
                        raise RuntimeError("install_recovery_backup_mismatch")
                    _verify_code_signature_at(backup, directory, verify_code_signature)
                    _remove_known_app(backup, directory)
                _remove_install_journal(target, directory=directory)
            except Exception:
                return True
            return True
    backup_exists = _real_app_directory(backup, directory)
    target_exists = _real_app_directory(target, directory)
    if backup_exists:
        backup_payload = _validate_mounted_app_at(backup, None, directory)
        if _safe_text(backup_payload.get("package_id")) != journal["package_id"]:
            raise RuntimeError("install_recovery_backup_mismatch")
        _verify_code_signature_at(backup, directory, verify_code_signature)
        if target_exists:
            _remove_known_app(target, directory)
        _replace_install_entry(backup, target, directory)
        _cleanup_journal_staging(
            target,
            journal,
            directory,
            verify_candidate_code_signature or verify_code_signature,
        )
        _remove_install_journal(target, directory=directory)
        return False
    if journal["had_previous"]:
        if journal["phase"] == "prepared" and target_exists:
            installed = _validate_mounted_app_at(target, None, directory)
            if _safe_text(installed.get("package_id")) != journal["package_id"]:
                raise RuntimeError("install_recovery_target_mismatch")
            _verify_code_signature_at(target, directory, verify_code_signature)
            _cleanup_journal_staging(
                target,
                journal,
                directory,
                verify_candidate_code_signature or verify_code_signature,
            )
            _remove_install_journal(target, directory=directory)
            return False
        raise RuntimeError("install_recovery_backup_missing")
    if target_exists:
        if journal["phase"] != "target_switched":
            _validate_journal_app_at(target, journal, directory)
        _remove_known_app(target, directory)
    _cleanup_journal_staging(
        target,
        journal,
        directory,
        verify_candidate_code_signature or verify_code_signature,
    )
    _remove_install_journal(target, directory=directory)
    return False


def replace_app_staged(
    source_app: os.PathLike | str,
    target_app: os.PathLike | str,
    *,
    expected_release: dict[str, Any] | None = None,
    copy_fn=None,
    prepare_staging=None,
    before_switch=None,
    after_switch=None,
    verify_code_signature=None,
    verify_candidate_code_signature=None,
) -> str:
    source = pathlib.Path(source_app)
    target = pathlib.Path(target_app)
    with install_transaction_lock(target) as directory:
        _recover_install_transaction(
            target,
            directory=directory,
            verify_code_signature=verify_code_signature,
        )
        _assert_owned_install_artifacts(target, directory, None)
        if _real_app_directory(target, directory) and expected_release:
            installed = _validate_mounted_app_at(target, None, directory)
            _verify_code_signature_at(target, directory, verify_code_signature)
            expected_package = _safe_text(expected_release.get("package_id"))
            if _safe_text(installed.get("package_id")) != expected_package:
                raise UpdateValidationError("package_id_mismatch")
            if _compare_install_versions(
                installed.get("build_version"),
                expected_release.get("version"),
            ) >= 0:
                return str(target)
        transaction_id = str(uuid.uuid4())
        staging, backup = _install_transaction_paths(target, transaction_id)
        if _entry_stat(directory, staging) is not None or _entry_stat(directory, backup) is not None:
            raise RuntimeError("install_transaction_collision")
        copy_operation = copy_fn or copy_app
        try:
            copy_operation(source, _install_entry_path(directory, staging))
            if prepare_staging is not None:
                prepare_staging(_install_entry_path(directory, staging))
            manifest_payload = _validate_mounted_app_at(
                staging,
                expected_release,
                directory,
            )
            _verify_code_signature_at(
                staging,
                directory,
                verify_candidate_code_signature or verify_code_signature,
            )
        except Exception:
            if _real_app_directory(staging, directory):
                _remove_known_app(staging, directory)
            raise
        had_previous = _real_app_directory(target, directory)
        journal = {
            "schema_version": 1,
            "phase": "prepared",
            "transaction_id": transaction_id,
            "staging_name": staging.name,
            "backup_name": backup.name,
            "had_previous": had_previous,
            "package_id": _safe_text(manifest_payload.get("package_id")),
            "build_version": _safe_text(manifest_payload.get("build_version")),
        }
        _write_install_journal(target, journal, directory=directory)
        try:
            if before_switch is not None:
                before_switch()
            if had_previous:
                _replace_install_entry(target, backup, directory)
                journal["phase"] = "backup_moved"
                _write_install_journal(target, journal, directory=directory)
            _replace_install_entry(staging, target, directory)
            journal["phase"] = "target_switched"
            _write_install_journal(target, journal, directory=directory)
            if after_switch is not None:
                after_switch(_install_entry_path(directory, target))
            _validate_journal_app_at(target, journal, directory)
            _verify_code_signature_at(
                target,
                directory,
                verify_candidate_code_signature or verify_code_signature,
            )
            journal["phase"] = "committed"
            _write_install_journal(target, journal, directory=directory)
        except Exception as exc:
            try:
                _recover_install_transaction(
                    target,
                    directory=directory,
                    verify_code_signature=verify_code_signature,
                    verify_candidate_code_signature=verify_candidate_code_signature,
                )
            except Exception as recovery:
                if hasattr(exc, "add_note"):
                    exc.add_note(f"install recovery failed: {recovery}")
            raise
        try:
            if had_previous:
                backup_payload = _validate_mounted_app_at(backup, None, directory)
                if _safe_text(backup_payload.get("package_id")) != journal["package_id"]:
                    raise RuntimeError("install_recovery_backup_mismatch")
                _verify_code_signature_at(backup, directory, verify_code_signature)
                _remove_known_app(backup, directory)
            _remove_install_journal(target, directory=directory)
        except Exception:
            pass
    return str(target)


def _install_helper_script(
    *,
    dmg_path: str,
    install_app: str,
    log_path: str,
    expected_release: dict[str, Any],
    module_base_dir: str,
) -> str:
    return f"""#!/usr/bin/env python3
import json
import hashlib
import os
import plistlib
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

dmg_path = {json.dumps(dmg_path, ensure_ascii=False)}
install_app = Path({json.dumps(install_app, ensure_ascii=False)}).expanduser()
log_path = Path({json.dumps(log_path, ensure_ascii=False)})
expected_release = json.loads({json.dumps(json.dumps(expected_release, ensure_ascii=False), ensure_ascii=False)})
module_base_dir = Path({json.dumps(module_base_dir, ensure_ascii=False)})
source_code_signature_identity = ""
sys.path.insert(0, str(module_base_dir / "scripts"))
from update_manager import replace_app_staged

def log(message):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{{time.strftime('%Y-%m-%d %H:%M:%S')}}] {{message}}\\n")

def run(cmd, **kwargs):
    log("exec: " + " ".join(map(str, cmd)))
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False, check=False, **kwargs)

def run_at_install_entry(cmd, target, **kwargs):
    parent_fd = target.parent_fd
    entry_name = target.entry_name
    def enter_install_directory():
        os.fchdir(parent_fd)
    return run(
        [*cmd, entry_name],
        pass_fds=(parent_fd,),
        preexec_fn=enter_install_directory,
        **kwargs,
    )

def prepare_verified_dmg_snapshot():
    expected_size = int(expected_release["size_bytes"])
    expected_sha256 = str(expected_release["sha256"]).lower()
    if expected_size <= 0 or not re.fullmatch(r"[0-9a-f]{{64}}", expected_sha256):
        raise RuntimeError("trusted DMG descriptor invalid")
    source_descriptor = os.open(
        dmg_path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    snapshot_path = Path(tempfile.mkdtemp(prefix=".install-dmg-", dir=log_path.parent))
    os.chmod(snapshot_path, 0o700)
    directory_descriptor = os.open(
        snapshot_path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    snapshot_descriptor = -1
    try:
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != expected_size:
            raise RuntimeError("DMG identity mismatch")
        snapshot_descriptor = os.open(
            "verified.dmg",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o400,
            dir_fd=directory_descriptor,
        )
        digest = hashlib.sha256()
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(snapshot_descriptor, view)
                view = view[written:]
        os.fsync(snapshot_descriptor)
        after = os.fstat(source_descriptor)
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if before_identity != after_identity:
            raise RuntimeError("DMG changed during verification")
        if digest.hexdigest() != expected_sha256:
            raise RuntimeError("DMG checksum mismatch")
        os.close(snapshot_descriptor)
        snapshot_descriptor = -1
        os.fsync(directory_descriptor)
        return directory_descriptor, snapshot_path
    except Exception:
        if snapshot_descriptor >= 0:
            os.close(snapshot_descriptor)
        try:
            os.unlink("verified.dmg", dir_fd=directory_descriptor)
        except OSError:
            pass
        os.close(directory_descriptor)
        try:
            os.rmdir(snapshot_path)
        except OSError:
            pass
        raise
    finally:
        os.close(source_descriptor)

def stop_installed_app_processes():
    marker = str(install_app / "Contents" / "Resources" / "app" / "scripts")
    result = subprocess.run(["ps", "-axo", "pid=,command="], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
    pids = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        if marker in command:
            pids.append(pid)
    if not pids:
        return
    log("stopping installed app processes: " + ",".join(map(str, pids)))
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(1)
    for pid in pids:
        try:
            os.kill(pid, 0)
        except OSError:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

def copy_with_ditto(source, staging):
    install_notice = (
        'display dialog "数据科学家正在安装到应用程序文件夹，请稍候。" '
        'buttons {{"好"}} default button 1 with title "正在安装" giving up after 2'
    )
    notice = run(["/usr/bin/osascript", "-e", install_notice])
    if notice.returncode != 0:
        raise RuntimeError(notice.stderr.decode("utf-8", errors="replace") or "install notice failed")
    log("copying app to staging: " + str(source) + " -> " + str(staging))
    ditto = run_at_install_entry(["/usr/bin/ditto", str(source)], staging)
    if ditto.returncode != 0:
        install_error = (
            'display alert "数据科学家安装失败" message '
            '"拷贝到应用程序文件夹失败（错误码 '
            + str(ditto.returncode)
            + '）。请检查磁盘空间后重试。" as critical'
        )
        run(["/usr/bin/osascript", "-e", install_error])
        raise RuntimeError(ditto.stderr.decode("utf-8", errors="replace") or "ditto copy failed")

def prepare_staging(staging):
    writable = run_at_install_entry(["/bin/chmod", "-R", "u+w"], staging)
    if writable.returncode != 0:
        raise RuntimeError(writable.stderr.decode("utf-8", errors="replace") or "chmod staging failed")
    quarantine = run_at_install_entry(["/usr/bin/xattr", "-cr"], staging)
    if quarantine.returncode != 0:
        raise RuntimeError(quarantine.stderr.decode("utf-8", errors="replace") or "xattr staging failed")

def verify_code_signature(target):
    signature = run_at_install_entry(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict"],
        target,
    )
    if signature.returncode != 0:
        raise RuntimeError(signature.stderr.decode("utf-8", errors="replace") or "codesign verification failed")

def verify_candidate_code_signature(target):
    verify_code_signature(target)
    identity = run_at_install_entry(
        ["/usr/bin/codesign", "-d", "--verbose=4"],
        target,
    )
    if identity.returncode != 0:
        raise RuntimeError(identity.stderr.decode("utf-8", errors="replace") or "codesign identity failed")
    match = re.search(rb"(?:^|\\n)CDHash=([0-9a-fA-F]+)(?:\\n|$)", identity.stderr)
    if not match or match.group(1).decode("ascii").lower() != source_code_signature_identity:
        raise RuntimeError("codesign identity mismatch")

def source_signature_identity(source):
    verify = run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(source)])
    if verify.returncode != 0:
        raise RuntimeError(verify.stderr.decode("utf-8", errors="replace") or "source codesign verification failed")
    identity = run(["/usr/bin/codesign", "-d", "--verbose=4", str(source)])
    if identity.returncode != 0:
        raise RuntimeError(identity.stderr.decode("utf-8", errors="replace") or "source codesign identity failed")
    match = re.search(rb"(?:^|\\n)CDHash=([0-9a-fA-F]+)(?:\\n|$)", identity.stderr)
    if not match:
        raise RuntimeError("source codesign identity missing")
    return match.group(1).decode("ascii").lower()

def after_switch(target):
    parent_fd = target.parent_fd
    def enter_install_directory():
        os.fchdir(parent_fd)
    subprocess.Popen(
        ["open", "-n", target.entry_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        pass_fds=(parent_fd,),
        preexec_fn=enter_install_directory,
    )
    log("opened installed app: " + str(target))

def main():
    global source_code_signature_identity
    mount_points = []
    dmg_directory_descriptor = -1
    dmg_snapshot_path = None
    time.sleep(1.5)
    log("install helper start: " + dmg_path)
    dmg_directory_descriptor, dmg_snapshot_path = prepare_verified_dmg_snapshot()
    try:
        def enter_dmg_directory():
            os.fchdir(dmg_directory_descriptor)
        attach = run(
            ["hdiutil", "attach", "-nobrowse", "-readonly", "-plist", "verified.dmg"],
            pass_fds=(dmg_directory_descriptor,),
            preexec_fn=enter_dmg_directory,
        )
        if attach.returncode != 0:
            raise RuntimeError(attach.stderr.decode("utf-8", errors="replace") or "hdiutil attach failed")
        payload = plistlib.loads(attach.stdout)
        for entity in payload.get("system-entities", []):
            mount_point = entity.get("mount-point")
            if mount_point:
                mount_points.append(mount_point)
        if not mount_points:
            raise RuntimeError("DMG mount point not found")
        app_source = None
        for mount_point in mount_points:
            mount = Path(mount_point)
            preferred = mount / install_app.name
            if preferred.exists():
                app_source = preferred
                break
            matches = list(mount.glob("*.app"))
            if matches:
                app_source = matches[0]
                break
        if not app_source:
            raise RuntimeError("DMG does not contain an .app bundle")
        source_code_signature_identity = source_signature_identity(app_source)
        replace_app_staged(
            app_source,
            install_app,
            expected_release=expected_release,
            copy_fn=copy_with_ditto,
            prepare_staging=prepare_staging,
            before_switch=stop_installed_app_processes,
            after_switch=after_switch,
            verify_code_signature=verify_code_signature,
            verify_candidate_code_signature=verify_candidate_code_signature,
        )
    finally:
        for mount_point in reversed(mount_points):
            run(["hdiutil", "detach", mount_point])
        if dmg_directory_descriptor >= 0:
            try:
                os.unlink("verified.dmg", dir_fd=dmg_directory_descriptor)
                os.fsync(dmg_directory_descriptor)
            except OSError as exc:
                log("verified DMG cleanup failed: " + str(exc))
            os.close(dmg_directory_descriptor)
        if dmg_snapshot_path is not None:
            try:
                os.rmdir(dmg_snapshot_path)
            except OSError as exc:
                log("verified DMG directory cleanup failed: " + str(exc))

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("install helper failed: " + str(exc))
        raise
"""


def _current_app_install_path(base_dir: str) -> str:
    payload_path = pathlib.Path(
        os.environ.get("YIRENGONGIS_APP_PAYLOAD_DIR") or base_dir or ""
    ).expanduser()
    if payload_path.name == "app" and len(payload_path.parents) >= 3:
        app_bundle = payload_path.parents[2]
        if app_bundle.suffix == ".app":
            return str(app_bundle)
    for candidate in (
        pathlib.Path("/Applications/数据科学家 Community.app"),
        pathlib.Path.home() / "Applications" / "数据科学家 Community.app",
    ):
        if candidate.exists():
            return str(candidate)
    return str(pathlib.Path.home() / "Applications" / "数据科学家 Community.app")


def install_downloaded_update(
    state_dir: str,
    path: str = "",
    *,
    base_dir: str = "",
) -> dict[str, Any]:
    snapshot = _download_snapshot()
    target = os.path.abspath(str(snapshot.get("path") or ""))
    release = snapshot.get("release") if isinstance(snapshot.get("release"), dict) else {}
    if snapshot.get("status") != "completed" or not target or not os.path.exists(target):
        return {"ok": False, "error": "completed_download_required", "message": "请先完成新版安装包下载和校验。"}
    current = current_package_info(base_dir) if base_dir else {
        "package_id": release.get("package_id"),
        "arch": release.get("arch") or UPDATE_ARCH,
    }
    descriptor = validate_release(
        release,
        expected_package_id=_safe_text(current.get("package_id")),
        expected_arch=_safe_text(current.get("arch") or UPDATE_ARCH),
    )
    if os.path.getsize(target) != descriptor.size_bytes:
        return {"ok": False, "error": "size_mismatch", "message": "已下载安装包大小校验失败，请重新下载。"}
    if _sha256_file(target) != descriptor.sha256:
        return {"ok": False, "error": "sha256_mismatch", "message": "已下载安装包完整性校验失败，请重新下载。"}
    if not sys_platform_is_macos():
        return reveal_path(target)

    update_dir = _update_download_dir(state_dir)
    helper_path = os.path.join(update_dir, "install_downloaded_update.py")
    log_path = os.path.join(update_dir, "update_install.log")
    install_app = _current_app_install_path(base_dir)
    with open(helper_path, "w", encoding="utf-8") as handle:
        handle.write(
            _install_helper_script(
                dmg_path=target,
                install_app=install_app,
                log_path=log_path,
                expected_release=asdict(descriptor),
                module_base_dir=base_dir,
            )
        )
    os.chmod(helper_path, 0o700)
    helper_env = os.environ.copy()
    for key in DETACHED_HELPER_STRIP_ENV:
        helper_env.pop(key, None)
    subprocess.Popen(
        [sys.executable, helper_path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=helper_env,
        start_new_session=True,
    )
    _set_download_state(
        ok=True,
        status="installing",
        running=False,
        path=target,
        message="正在安装新版，应用将自动重启...",
        error="",
    )
    return {"ok": True, "status": "installing", "path": target, "message": "正在安装新版，应用将自动重启。"}


def reveal_path(path: str) -> dict[str, Any]:
    target = os.path.abspath(str(path or ""))
    if not target or not os.path.exists(target):
        return {"ok": False, "error": "file_not_found", "message": "未找到已下载的安装包。"}
    if sys_platform_is_macos():
        subprocess.run(["open", "-R", target], check=False)
    return {"ok": True, "path": target}


def sys_platform_is_macos() -> bool:
    return os.name == "posix" and os.uname().sysname == "Darwin"
