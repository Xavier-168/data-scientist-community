"""
客户端许可证验证模块
Mac 和 Win 版本共用，复制到 scripts/ 目录下即可

用法:
    from client_license import LicenseManager
    lm = LicenseManager(server_url="https://your-worker.workers.dev")

    # 首次激活
    result = lm.activate("YRG-XXXX-XXXX-XXXX")

    # 每次启动验证
    ok, info = lm.verify()
"""
import base64
import calendar
import copy
import hashlib
import json
import os
import platform
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid

_SELF_DIR = os.path.dirname(os.path.abspath(__file__))
if _SELF_DIR not in sys.path:
    sys.path.insert(0, _SELF_DIR)

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from package_identity import package_license_allowed, read_signed_package_manifest
from runtime_paths import resolve_auth_dir, seed_state_from_bundle


def _build_ssl_context():
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        try:
            return ssl.create_default_context()
        except Exception:
            return None


_SSL_CTX = _build_ssl_context()


# ============================================================
# 配置
# ============================================================
DEFAULT_ACTIVATION_SERVER = ""
LEGACY_ACTIVATION_SERVERS = ()
ACTIVATION_SERVER = str(os.environ.get("YIRENGONGIS_ACTIVATION_SERVER") or DEFAULT_ACTIVATION_SERVER).strip()
LICENSE_FILE_NAME = "license.json"
TRIAL_FILE_NAME = "trial.json"
GRACE_PERIOD_DAYS = 7  # 离线宽限期（天）
TRIAL_ENABLED = False  # 免费试用已关闭；保留旧凭证解析仅用于兼容现有状态文件
TRIAL_DAYS = 14  # 旧试用凭证的历史期限，不再用于放行或联网登记
OFFLINE_LICENSE_PUBLIC_KEYS_FILE = os.environ.get(
    "YIRENGONGIS_OFFLINE_LICENSE_PUBLIC_KEYS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "license_public_keys.json"),
)
OFFLINE_LICENSE_PUBLIC_KEYS_JSON = str(os.environ.get("YIRENGONGIS_OFFLINE_LICENSE_PUBLIC_KEYS_JSON", "") or "").strip()
ACTIVATION_RETRYABLE_ERRORS = {"invalid_license", "not_activated", "internal_error", "network_error", "http_error"}
ACTIVATION_RETRY_DELAYS_SECONDS = (0.6, 1.2)
VERIFY_RETRYABLE_ERRORS = {"internal_error", "http_error"}
VERIFY_RETRY_DELAYS_SECONDS = (0.6, 1.2, 2.0)
SERVER_FALLBACK_ERRORS = {"internal_error", "http_error", "not_found"}
LICENSE_VERIFY_CACHE_TTL_SECONDS = 30


def get_verified_ssl_context():
    return _SSL_CTX


def _normalize_server_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.rstrip("/")


def _append_server(candidates: list[str], seen: set[str], value: str) -> None:
    normalized = _normalize_server_url(value)
    if not normalized or normalized in seen:
        return
    candidates.append(normalized)
    seen.add(normalized)


def _server_values(raw) -> list[str]:
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(item or "").strip() for item in raw if str(item or "").strip()]
    return []


def _manifest_activation_servers(base_dir: str) -> list[str]:
    if not base_dir:
        return []
    try:
        manifest = read_signed_package_manifest(base_dir)
    except Exception:
        return []
    payload = (manifest or {}).get("payload") or {}
    if not isinstance(payload, dict):
        return []
    values = []
    values.extend(_server_values(payload.get("activation_server")))
    values.extend(_server_values(payload.get("activation_servers")))
    return values


def resolve_activation_server_candidates(temp_dir: str | None = None, server_url: str | None = None) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    explicit = _normalize_server_url(server_url)
    manifest_servers = _manifest_activation_servers(temp_dir or "")
    if explicit:
        _append_server(candidates, seen, explicit)
    for item in manifest_servers:
        _append_server(candidates, seen, item)
    if not explicit:
        _append_server(candidates, seen, ACTIVATION_SERVER)
    env_fallbacks = _server_values(os.environ.get("YIRENGONGIS_ACTIVATION_SERVER_FALLBACKS", ""))
    for item in env_fallbacks:
        _append_server(candidates, seen, item)
    _append_server(candidates, seen, DEFAULT_ACTIVATION_SERVER)
    for item in LEGACY_ACTIVATION_SERVERS:
        _append_server(candidates, seen, item)
    return candidates


def _friendly_network_error_message(endpoints: list[str], last_error: str) -> str:
    hosts = []
    for endpoint in endpoints or []:
        parsed = urllib.parse.urlparse(endpoint)
        hosts.append(parsed.netloc or endpoint)
    host_text = "、".join(hosts) if hosts else "激活服务"
    detail = str(last_error or "").strip()
    return (
        f"无法连接激活服务（已尝试：{host_text}）。"
        f"{('最后错误：' + detail + '。') if detail else ''}"
        "如果当前网络会拦截 workers.dev，请切换网络或联系支持获取正式激活域名。"
    )


def _normalize_error_code(value: str) -> str:
    return str(value or "").strip().lower()


def _urlsafe_b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _urlsafe_b64decode(value: str) -> bytes:
    text = str(value or "").strip()
    if not text:
        return b""
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + padding).encode("ascii"))


def _serialize_offline_payload(payload: dict) -> bytes:
    normalized = {
        "customer_name": str((payload or {}).get("customer_name") or "").strip(),
        "expires_at": str((payload or {}).get("expires_at") or "").strip(),
        "key_id": str((payload or {}).get("key_id") or "").strip(),
        "license_key": str((payload or {}).get("license_key") or "").strip(),
        "machine_id": str((payload or {}).get("machine_id") or "").strip(),
    }
    return json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _serialize_trial_payload(payload: dict) -> bytes:
    normalized = {
        "build_version": str((payload or {}).get("build_version") or "").strip(),
        "customer_name": str((payload or {}).get("customer_name") or "").strip(),
        "key_id": str((payload or {}).get("key_id") or "").strip(),
        "kind": str((payload or {}).get("kind") or "").strip(),
        "machine_id": str((payload or {}).get("machine_id") or "").strip(),
        "package_id": str((payload or {}).get("package_id") or "").strip(),
        "platform": str((payload or {}).get("platform") or "").strip(),
        "trial_expires_at": str((payload or {}).get("trial_expires_at") or "").strip(),
        "trial_started_at": str((payload or {}).get("trial_started_at") or "").strip(),
    }
    return json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _parse_utc_timestamp(value: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return float(calendar.timegm(time.strptime(text, fmt)))
        except Exception:
            continue
    return None


def _days_remaining(expires_ts: float) -> int:
    remaining = max(0.0, float(expires_ts) - time.time())
    return int((remaining + 86399) // 86400)


def _load_offline_public_key(pem_text: str) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(str(pem_text or "").encode("utf-8"))
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("offline license public key must be Ed25519")
    return key


def _load_public_key_bundle_from_disk(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError("offline public key bundle must be an object")
    return payload


def _load_offline_public_keys(bundle: dict | None = None) -> dict[str, Ed25519PublicKey]:
    payload = bundle
    if payload is None:
        if OFFLINE_LICENSE_PUBLIC_KEYS_JSON:
            payload = json.loads(OFFLINE_LICENSE_PUBLIC_KEYS_JSON)
        else:
            payload = _load_public_key_bundle_from_disk(OFFLINE_LICENSE_PUBLIC_KEYS_FILE)
    if not isinstance(payload, dict):
        raise ValueError("offline public key bundle must be an object")

    keys = payload.get("keys") or []
    if not isinstance(keys, list) or not keys:
        raise ValueError("offline public key bundle must contain at least one key")

    trusted_keys = {}
    for item in keys:
        if not isinstance(item, dict):
            continue
        key_id = str(item.get("key_id") or "").strip()
        public_key_pem = str(item.get("public_key_pem") or "").strip()
        if not key_id or not public_key_pem:
            continue
        trusted_keys[key_id] = _load_offline_public_key(public_key_pem + ("\n" if not public_key_pem.endswith("\n") else ""))

    if not trusted_keys:
        raise ValueError("offline public key bundle did not produce any trusted keys")
    return trusted_keys


class VerificationCache:
    def __init__(self, ttl_seconds: float = LICENSE_VERIFY_CACHE_TTL_SECONDS):
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self._condition = threading.Condition()
        self._value = None
        self._expires_at = 0.0
        self._inflight = False

    def get_or_load(self, loader):
        with self._condition:
            now = time.monotonic()
            if self._value is not None and now < self._expires_at:
                return copy.deepcopy(self._value)
            while self._inflight:
                self._condition.wait()
                now = time.monotonic()
                if self._value is not None and now < self._expires_at:
                    return copy.deepcopy(self._value)
            self._inflight = True

        loaded = False
        value = None
        try:
            value = loader()
            loaded = True
            return copy.deepcopy(value)
        finally:
            with self._condition:
                if loaded:
                    self._value = copy.deepcopy(value)
                    self._expires_at = time.monotonic() + self.ttl_seconds
                self._inflight = False
                self._condition.notify_all()

    def invalidate(self):
        with self._condition:
            self._value = None
            self._expires_at = 0.0


class LicenseManager:
    def __init__(self, base_dir=None, server_url=None, offline_public_keys=None):
        if base_dir is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.base_dir = os.path.abspath(base_dir)
        self.state_dir = seed_state_from_bundle(self.base_dir)
        auth_dir = resolve_auth_dir(self.base_dir, self.state_dir)
        explicit_server_url = _normalize_server_url(server_url or "")
        self.server_url = explicit_server_url or ACTIVATION_SERVER
        self.server_urls = resolve_activation_server_candidates(self.base_dir, explicit_server_url or None)
        self.license_path = os.path.join(auth_dir, LICENSE_FILE_NAME)
        self.trial_path = os.path.join(auth_dir, TRIAL_FILE_NAME)
        self._offline_public_keys = _load_offline_public_keys(offline_public_keys)
        self._verification_cache = VerificationCache()
        self._license_data = self._load_license()
        self._trial_data = self._load_trial()

    def reload(self):
        """Re-read license data from disk (e.g. after clearing stale files)."""
        self._license_data = self._load_license()
        self._trial_data = self._load_trial()
        self._verification_cache.invalidate()

    # --------------------------------------------------------
    # 机器指纹
    # --------------------------------------------------------
    def get_machine_id(self):
        """生成当前机器的唯一标识（不可逆哈希）"""
        raw_parts = []

        system = platform.system()
        if system == "Darwin":
            # macOS: 使用硬件 UUID
            try:
                out = subprocess.check_output(
                    ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                    text=True, timeout=5
                )
                for line in out.split("\n"):
                    if "IOPlatformUUID" in line:
                        raw_parts.append(line.split('"')[-2])
                        break
            except Exception:
                pass

        elif system == "Windows":
            # Windows: 使用主板序列号 + BIOS序列号
            try:
                out = subprocess.check_output(
                    ["wmic", "baseboard", "get", "serialnumber"],
                    text=True, timeout=5
                )
                raw_parts.append(out.strip().split("\n")[-1].strip())
            except Exception:
                pass
            try:
                out = subprocess.check_output(
                    ["wmic", "bios", "get", "serialnumber"],
                    text=True, timeout=5
                )
                raw_parts.append(out.strip().split("\n")[-1].strip())
            except Exception:
                pass

        # 后备方案: MAC 地址
        if not raw_parts:
            raw_parts.append(str(uuid.getnode()))

        # 加上用户名作为盐（同一台电脑不同用户算同一设备）
        raw_parts.append(platform.node())

        raw = "|".join(raw_parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def get_machine_info(self):
        """返回可读的机器信息（用于管理后台展示）"""
        return f"{platform.system()} {platform.release()} | {platform.machine()} | {platform.node()}"

    def get_platform_tag(self):
        """mac 或 win"""
        return "mac" if platform.system() == "Darwin" else "win"

    # --------------------------------------------------------
    # 本地存储
    # --------------------------------------------------------
    def _load_license(self):
        if os.path.exists(self.license_path):
            try:
                with open(self.license_path, "r") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _load_trial(self):
        if os.path.exists(self.trial_path):
            try:
                with open(self.trial_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                return payload if isinstance(payload, dict) else {}
            except Exception:
                return {}
        return {}

    def _save_license(self, data):
        os.makedirs(os.path.dirname(self.license_path), exist_ok=True)
        with open(self.license_path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        if os.name != "nt":
            try:
                os.chmod(self.license_path, 0o600)
            except OSError:
                pass
        self._license_data = data
        self._verification_cache.invalidate()

    def _save_trial(self, data):
        os.makedirs(os.path.dirname(self.trial_path), exist_ok=True)
        with open(self.trial_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        if os.name != "nt":
            try:
                os.chmod(self.trial_path, 0o600)
            except OSError:
                pass
        self._trial_data = data
        self._verification_cache.invalidate()

    def _package_trial_metadata(self) -> dict:
        try:
            manifest = read_signed_package_manifest(self.base_dir)
        except Exception:
            manifest = None
        payload = (manifest or {}).get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        return {
            "package_id": str(payload.get("package_id") or "").strip(),
            "build_version": str(payload.get("build_version") or "").strip(),
            "customer_name": str(payload.get("customer_name") or "").strip(),
        }

    def _verify_offline_entitlement(self):
        entitlement = str(self._license_data.get("offline_entitlement") or "").strip()
        if not entitlement:
            return False, {"error": "offline_expired", "message": f"离线超过 {GRACE_PERIOD_DAYS} 天，请连网验证"}

        try:
            encoded_payload, encoded_signature = entitlement.split(".", 1)
            payload_bytes = _urlsafe_b64decode(encoded_payload)
            signature = _urlsafe_b64decode(encoded_signature)
            payload = json.loads(payload_bytes.decode("utf-8"))
            if payload_bytes != _serialize_offline_payload(payload):
                return False, {"error": "offline_entitlement_invalid", "message": "离线许可证格式无效，请重新联网验证"}
            key_id = str(payload.get("key_id") or "").strip()
            public_key = self._offline_public_keys.get(key_id)
            if not public_key:
                return False, {"error": "offline_entitlement_invalid", "message": "离线许可证签名密钥无效，请重新联网验证"}
            public_key.verify(signature, payload_bytes)
            machine_id = self.get_machine_id()
            if payload.get("license_key") != self._license_data.get("license_key") or payload.get("machine_id") != machine_id:
                return False, {"error": "offline_entitlement_invalid", "message": "离线许可证与当前设备不匹配，请重新联网验证"}

            expires_at = str(payload.get("expires_at") or "").strip()
            if not expires_at:
                return False, {"error": "offline_entitlement_invalid", "message": "离线许可证缺少过期时间，请重新联网验证"}
            expires_ts = time.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ")
            if time.time() > calendar.timegm(expires_ts):
                return False, {"error": "offline_expired", "message": f"离线超过 {GRACE_PERIOD_DAYS} 天，请连网验证"}

            return True, {
                "status": "offline_entitlement",
                "message": "离线许可证有效",
                "customer_name": payload.get("customer_name", self._license_data.get("customer_name", "")),
                "expires_at": expires_at,
            }
        except InvalidSignature:
            return False, {"error": "offline_entitlement_invalid", "message": "离线许可证签名无效，请重新联网验证"}
        except Exception:
            return False, {"error": "offline_entitlement_invalid", "message": "离线许可证损坏，请重新联网验证"}

    def _verify_trial_entitlement(self):
        if not TRIAL_ENABLED:
            return False, {
                "error": "trial_disabled",
                "message": "免费试用已关闭，请输入许可证密钥激活",
                "access_mode": "none",
            }

        entitlement = str(self._trial_data.get("trial_entitlement") or "").strip()
        if not entitlement:
            return False, {"error": "trial_not_registered", "message": "首次试用需要联网登记"}

        try:
            encoded_payload, encoded_signature = entitlement.split(".", 1)
            payload_bytes = _urlsafe_b64decode(encoded_payload)
            signature = _urlsafe_b64decode(encoded_signature)
            payload = json.loads(payload_bytes.decode("utf-8"))
            if payload_bytes != _serialize_trial_payload(payload):
                return False, {"error": "trial_entitlement_invalid", "message": "试用凭证格式无效，请联网重新登记"}

            key_id = str(payload.get("key_id") or "").strip()
            public_key = self._offline_public_keys.get(key_id)
            if not public_key:
                return False, {"error": "trial_entitlement_invalid", "message": "试用凭证签名密钥无效，请联网重新登记"}
            public_key.verify(signature, payload_bytes)

            if payload.get("kind") != "trial":
                return False, {"error": "trial_entitlement_invalid", "message": "试用凭证类型无效，请联网重新登记"}
            if payload.get("machine_id") != self.get_machine_id():
                return False, {"error": "trial_machine_mismatch", "message": "试用凭证与当前设备不匹配，请联网重新登记"}

            expires_at = str(payload.get("trial_expires_at") or "").strip()
            expires_ts = _parse_utc_timestamp(expires_at)
            if not expires_ts:
                return False, {"error": "trial_entitlement_invalid", "message": "试用凭证缺少有效到期时间，请联网重新登记"}
            if time.time() > expires_ts:
                return False, {
                    "error": "trial_expired",
                    "message": "试用已结束，请输入许可证继续使用",
                    "access_mode": "none",
                    "trial": {
                        "active": False,
                        "expires_at": expires_at,
                        "days_remaining": 0,
                    },
                }

            started_at = str(payload.get("trial_started_at") or "").strip()
            return True, {
                "access_mode": "trial",
                "status": "trial",
                "message": "试用中",
                "customer_name": payload.get("customer_name", ""),
                "trial": {
                    "active": True,
                    "started_at": started_at,
                    "expires_at": expires_at,
                    "days_remaining": _days_remaining(expires_ts),
                    "package_id": payload.get("package_id", ""),
                    "build_version": payload.get("build_version", ""),
                },
            }
        except InvalidSignature:
            return False, {"error": "trial_entitlement_invalid", "message": "试用凭证签名无效，请联网重新登记"}
        except Exception:
            return False, {"error": "trial_entitlement_invalid", "message": "试用凭证损坏，请联网重新登记"}

    # --------------------------------------------------------
    # 网络请求
    # --------------------------------------------------------
    def _post(self, endpoint, payload):
        if _SSL_CTX is None:
            return {
                "ok": False,
                "error": "network_error",
                "message": "TLS 证书链不可用，请安装 certifi 或修复系统证书后重试",
            }
        last_error = ""
        last_response = None
        for base_url in self.server_urls or [self.server_url]:
            url = f"{base_url}{endpoint}"
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "DataScientistCommunity/0.1",
                },
                method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    if body.get("ok"):
                        return body
                    error_code = _normalize_error_code(body.get("error"))
                    if error_code in SERVER_FALLBACK_ERRORS:
                        last_response = body
                        continue
                    return body
            except urllib.error.HTTPError as e:
                try:
                    body = json.loads(e.read().decode("utf-8"))
                    if body.get("ok"):
                        return body
                    error_code = _normalize_error_code(body.get("error"))
                    if error_code in SERVER_FALLBACK_ERRORS:
                        last_response = body
                        continue
                    return body
                except Exception:
                    last_response = {"ok": False, "error": "http_error", "message": str(e)}
                    continue
            except Exception as e:
                last_error = str(e)
                continue
        if last_response is not None:
            return last_response
        return {
            "ok": False,
            "error": "network_error",
            "message": _friendly_network_error_message(self.server_urls or [self.server_url], last_error),
        }

    # --------------------------------------------------------
    # 公开 API
    # --------------------------------------------------------
    def activate(self, license_key):
        """
        激活许可证，返回 (success: bool, message: str)
        """
        self._verification_cache.invalidate()
        package_allowed, _package_error = package_license_allowed(self.base_dir, license_key)
        if not package_allowed:
            return False, "当前安装包仅允许绑定指定许可证"

        machine_id = self.get_machine_id()
        payload = {
            "license_key": license_key,
            "machine_id": machine_id,
            "machine_info": self.get_machine_info(),
            "platform": self.get_platform_tag(),
        }

        result = {}
        retry_delays = list(ACTIVATION_RETRY_DELAYS_SECONDS)
        for attempt in range(len(retry_delays) + 1):
            result = self._post("/activate", payload)
            if result.get("ok"):
                break
            error_code = _normalize_error_code(result.get("error"))
            if error_code not in ACTIVATION_RETRYABLE_ERRORS or attempt >= len(retry_delays):
                break
            time.sleep(retry_delays[attempt])

        if result.get("ok"):
            self._save_license({
                "license_key": license_key,
                "machine_id": machine_id,
                "customer_name": result.get("customer_name", ""),
                "plan": result.get("plan", "standard"),
                "expires_at": result.get("expires_at"),
                "offline_entitlement": result.get("offline_entitlement", ""),
                "activated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "last_verified": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            return True, result.get("message", "激活成功")
        else:
            return False, result.get("message", "激活失败")

    def register_trial(self):
        """
        Legacy trial registration entrypoint. Free trials are disabled.
        Returns (valid: bool, info: dict).
        """
        if not TRIAL_ENABLED:
            return False, {
                "error": "trial_disabled",
                "message": "免费试用已关闭，请输入许可证密钥激活",
                "access_mode": "none",
            }

        metadata = self._package_trial_metadata()
        result = self._post("/trial/register", {
            "machine_id": self.get_machine_id(),
            "machine_info": self.get_machine_info(),
            "platform": self.get_platform_tag(),
            **metadata,
        })

        if not result.get("ok"):
            return False, {
                "error": "trial_registration_failed",
                "message": result.get("message") or "首次试用登记失败，请联网后重试",
                "detail": result.get("error", ""),
                "access_mode": "none",
            }

        if not result.get("trial_valid"):
            return False, {
                "error": result.get("error") or result.get("status") or "trial_expired",
                "message": result.get("message") or "试用已结束，请输入许可证继续使用",
                "access_mode": "none",
                "trial": {
                    "active": False,
                    "started_at": result.get("trial_started_at", ""),
                    "expires_at": result.get("trial_expires_at", ""),
                    "days_remaining": 0,
                },
            }

        entitlement = str(result.get("trial_entitlement") or "").strip()
        if not entitlement:
            return False, {
                "error": "trial_registration_failed",
                "message": "激活服务未返回试用凭证，请稍后重试",
                "access_mode": "none",
            }

        self._save_trial({
            "trial_entitlement": entitlement,
            "trial_started_at": result.get("trial_started_at", ""),
            "trial_expires_at": result.get("trial_expires_at", ""),
            "registered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "last_checked": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        return self._verify_trial_entitlement()

    def verify(self):
        """
        验证许可证，返回 (valid: bool, info: dict)
        支持离线宽限期
        """
        ld = self._license_data
        if not ld or not ld.get("license_key"):
            return False, {"error": "not_activated", "message": "未激活，请先输入许可证"}

        package_allowed, package_error = package_license_allowed(self.base_dir, ld.get("license_key", ""))
        if not package_allowed:
            return False, {"error": package_error or "license_not_allowed_for_package", "message": "当前安装包与许可证不匹配，请使用对应客户安装包"}

        machine_id = self.get_machine_id()
        if ld.get("machine_id") != machine_id:
            return False, {"error": "machine_mismatch", "message": "机器指纹不匹配，请重新激活"}

        # 尝试在线验证；D1 / Worker 在激活后的短时间内仍可能出现 not_activated /
        # invalid_license 的瞬时读写延迟，因此这里做短重试，避免用户看到
        # “前两次失败第三次成功”的假错误。
        result = {}
        retry_delays = list(VERIFY_RETRY_DELAYS_SECONDS)
        payload = {
            "license_key": ld["license_key"],
            "machine_id": machine_id,
        }
        for attempt in range(len(retry_delays) + 1):
            result = self._post("/verify", payload)
            if result.get("ok"):
                break
            error_code = _normalize_error_code(result.get("error"))
            if error_code not in VERIFY_RETRYABLE_ERRORS or attempt >= len(retry_delays):
                break
            time.sleep(retry_delays[attempt])

        if result.get("ok"):
            # 在线验证通过，更新时间
            ld["last_verified"] = time.strftime("%Y-%m-%d %H:%M:%S")
            ld["customer_name"] = result.get("customer_name", ld.get("customer_name", ""))
            ld["expires_at"] = result.get("expires_at", ld.get("expires_at"))
            ld["offline_entitlement"] = result.get("offline_entitlement", ld.get("offline_entitlement", ""))
            self._save_license(ld)
            return True, {"status": "valid", "customer_name": ld["customer_name"]}

        # 在线验证失败 — 检查是网络问题还是许可证问题
        if result.get("error") == "network_error":
            return self._verify_offline_entitlement()

        # 许可证本身有问题（过期、禁用等）
        return False, {"error": result.get("error", "unknown"), "message": result.get("message", "验证失败")}

    def _verify_access_uncached(self):
        """
        Access gate for product use.
        A valid paid license is required. Free trials are disabled.
        """
        ok, info = self.verify()
        if ok:
            access_info = dict(info or {})
            access_info["access_mode"] = "license"
            return True, access_info

        license_error = str((info or {}).get("error") or "").strip()
        if self.is_activated() or license_error != "not_activated":
            denied = dict(info or {})
            denied["access_mode"] = "none"
            return False, denied

        if not TRIAL_ENABLED:
            return False, {
                "error": "trial_disabled",
                "message": "免费试用已关闭，请输入许可证密钥激活",
                "access_mode": "none",
            }

        trial_ok, trial_info = self._verify_trial_entitlement()
        if trial_ok:
            return True, trial_info

        if (trial_info or {}).get("error") == "trial_expired":
            return False, trial_info

        return self.register_trial()

    def verify_access(self):
        return self._verification_cache.get_or_load(self._verify_access_uncached)

    def get_license_key(self):
        return self._license_data.get("license_key", "")

    def get_customer_name(self):
        return self._license_data.get("customer_name", "")

    def is_activated(self):
        return bool(self._license_data.get("license_key"))


# ============================================================
# CLI 测试入口
# ============================================================
if __name__ == "__main__":
    lm = LicenseManager()
    print(f"Machine ID: {lm.get_machine_id()}")
    print(f"Machine Info: {lm.get_machine_info()}")
    print(f"Platform: {lm.get_platform_tag()}")
    print(f"License file: {lm.license_path}")

    if len(sys.argv) > 1:
        key = sys.argv[1]
        print(f"\nActivating with key: {key}")
        ok, msg = lm.activate(key)
        print(f"Result: {'OK' if ok else 'FAIL'} — {msg}")
    else:
        print(f"\nCurrent license: {lm.get_license_key() or '(none)'}")
        ok, info = lm.verify()
        print(f"Verify: {'VALID' if ok else 'INVALID'} — {info}")
