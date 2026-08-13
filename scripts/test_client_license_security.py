import importlib.util
import json
import os
import pathlib
import ssl
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


MODULE_PATH = pathlib.Path(__file__).with_name("client_license.py")
SPEC = importlib.util.spec_from_file_location("client_license_security_module", MODULE_PATH)
client_license = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(client_license)


class OfflineEntitlementTests(unittest.TestCase):
    def test_community_defaults_do_not_contact_production_services(self):
        self.assertEqual(client_license.ACTIVATION_SERVER, "")
        self.assertEqual(client_license.DEFAULT_ACTIVATION_SERVER, "")
        self.assertEqual(client_license.LEGACY_ACTIVATION_SERVERS, ())

    def test_explicit_activation_server_is_the_only_candidate(self):
        with patch.dict(client_license.os.environ, {}, clear=True):
            candidates = client_license.resolve_activation_server_candidates(
                temp_dir="/tmp/nonexistent-community-manifest",
                server_url="https://custom-activation.example.com",
            )
        self.assertEqual(candidates, ["https://custom-activation.example.com"])

    def test_manifest_activation_server_is_used_without_implicit_fallback(self):
        manifest = {
            "payload": {
                "activation_servers": ["https://manifest-activation.example.test"],
            }
        }
        with (
            patch.object(client_license, "read_signed_package_manifest", return_value=manifest),
            patch.dict(client_license.os.environ, {}, clear=True),
        ):
            candidates = client_license.resolve_activation_server_candidates(
                temp_dir="/tmp/community-manifest",
                server_url="",
            )
        self.assertEqual(candidates, ["https://manifest-activation.example.test"])

    def _make_manager(self, temp_dir):
        private_key = ed25519.Ed25519PrivateKey.generate()
        public_key_pem = private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")
        key_id = "unit-test-key"
        # macOS 源码态的默认运行目录是用户 Application Support。
        # 仅传 base_dir 不会隔离许可证状态，测试必须显式绑定临时状态目录。
        with patch.dict(os.environ, {"YIRENGONGIS_STATE_DIR": temp_dir}, clear=False):
            manager = client_license.LicenseManager(
                base_dir=temp_dir,
                server_url="https://unit.test",
                offline_public_keys={
                    "active_key_id": key_id,
                    "keys": [
                        {
                            "key_id": key_id,
                            "public_key_pem": public_key_pem,
                        }
                    ],
                },
            )
        self.assertEqual(
            pathlib.Path(manager.license_path).resolve(),
            pathlib.Path(temp_dir).resolve() / ".auth" / "license.json",
        )
        return manager, private_key, key_id

    def _build_signed_offline_entitlement(self, private_key, key_id, payload):
        normalized = dict(payload)
        normalized["key_id"] = key_id
        payload_bytes = client_license._serialize_offline_payload(normalized)
        signature = private_key.sign(payload_bytes)
        return (
            client_license._urlsafe_b64encode(payload_bytes)
            + "."
            + client_license._urlsafe_b64encode(signature)
        )

    def test_network_fallback_uses_signed_offline_entitlement(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager, private_key, key_id = self._make_manager(temp_dir)
            machine_id = "machine-1234"
            offline_entitlement = self._build_signed_offline_entitlement(
                private_key,
                key_id,
                {
                    "license_key": "YRG-TEST-1234",
                    "machine_id": machine_id,
                    "customer_name": "测试客户",
                    "expires_at": "2099-01-01T00:00:00Z",
                }
            )
            manager._save_license(
                {
                    "license_key": "YRG-TEST-1234",
                    "machine_id": machine_id,
                    "customer_name": "测试客户",
                    "last_verified": "2000-01-01 00:00:00",
                    "offline_entitlement": offline_entitlement,
                }
            )

            with (
                patch.object(manager, "get_machine_id", return_value=machine_id),
                patch.object(manager, "_post", return_value={"ok": False, "error": "network_error"}),
            ):
                ok, info = manager.verify()

            self.assertTrue(ok)
            self.assertEqual(info["status"], "offline_entitlement")

    def test_last_verified_tampering_no_longer_extends_offline_grace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager, _private_key, _key_id = self._make_manager(temp_dir)
            machine_id = "machine-1234"
            manager._save_license(
                {
                    "license_key": "YRG-TEST-1234",
                    "machine_id": machine_id,
                    "customer_name": "测试客户",
                    "last_verified": "2099-01-01 00:00:00",
                }
            )

            with (
                patch.object(manager, "get_machine_id", return_value=machine_id),
                patch.object(manager, "_post", return_value={"ok": False, "error": "network_error"}),
            ):
                ok, info = manager.verify()

            self.assertFalse(ok)
            self.assertEqual(info["error"], "offline_expired")

    def test_rejects_tampered_offline_entitlement(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager, private_key, key_id = self._make_manager(temp_dir)
            machine_id = "machine-1234"
            offline_entitlement = self._build_signed_offline_entitlement(
                private_key,
                key_id,
                {
                    "license_key": "YRG-TEST-1234",
                    "machine_id": machine_id,
                    "customer_name": "测试客户",
                    "expires_at": "2099-01-01T00:00:00Z",
                }
            )
            parts = offline_entitlement.split(".", 1)
            tampered_payload = json.loads(client_license._urlsafe_b64decode(parts[0]).decode("utf-8"))
            tampered_payload["expires_at"] = "2099-12-31T00:00:00Z"
            tampered_token = (
                client_license._urlsafe_b64encode(json.dumps(tampered_payload, ensure_ascii=False).encode("utf-8"))
                + "."
                + parts[1]
            )
            manager._save_license(
                {
                    "license_key": "YRG-TEST-1234",
                    "machine_id": machine_id,
                    "offline_entitlement": tampered_token,
                }
            )

            with patch.object(manager, "get_machine_id", return_value=machine_id):
                valid, info = manager._verify_offline_entitlement()

            self.assertFalse(valid)
            self.assertEqual(info["error"], "offline_entitlement_invalid")

    def test_ssl_context_keeps_certificate_validation_enabled(self):
        self.assertIsNotNone(client_license._SSL_CTX)
        self.assertNotEqual(client_license._SSL_CTX.verify_mode, ssl.CERT_NONE)
        self.assertTrue(client_license._SSL_CTX.check_hostname)

    def test_free_trial_is_disabled_without_network_registration(self):
        self.assertFalse(client_license.TRIAL_ENABLED)
        with tempfile.TemporaryDirectory() as temp_dir:
            manager, _private_key, _key_id = self._make_manager(temp_dir)
            with (
                patch.object(manager, "_post") as post_mock,
                patch.object(manager, "_verify_trial_entitlement") as verify_trial_mock,
            ):
                ok, info = manager._verify_access_uncached()

            self.assertFalse(ok)
            self.assertEqual(info["error"], "trial_disabled")
            self.assertEqual(info["access_mode"], "none")
            post_mock.assert_not_called()
            verify_trial_mock.assert_not_called()

    def test_legacy_trial_registration_entrypoint_is_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager, _private_key, _key_id = self._make_manager(temp_dir)
            with patch.object(manager, "_post") as post_mock:
                ok, info = manager.register_trial()

            self.assertFalse(ok)
            self.assertEqual(info["error"], "trial_disabled")
            post_mock.assert_not_called()

    def test_activate_retries_transient_invalid_license_until_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager, _private_key, _key_id = self._make_manager(temp_dir)
            machine_id = "machine-activate-retry"
            responses = iter(
                [
                    {"ok": False, "error": "invalid_license", "message": "许可证无效或已禁用"},
                    {"ok": False, "error": "invalid_license", "message": "许可证无效或已禁用"},
                    {
                        "ok": True,
                        "message": "激活成功！",
                        "customer_name": "测试客户",
                        "plan": "standard",
                        "expires_at": None,
                        "offline_entitlement": "offline-token",
                    },
                ]
            )

            with (
                patch.object(manager, "get_machine_id", return_value=machine_id),
                patch.object(manager, "_post", side_effect=lambda *args, **kwargs: next(responses)) as post_mock,
                patch.object(client_license.time, "sleep", return_value=None),
            ):
                ok, message = manager.activate("YRG-TEST-1234")

            self.assertTrue(ok)
            self.assertEqual(message, "激活成功！")
            self.assertEqual(post_mock.call_count, 3)
            self.assertEqual(manager._license_data["machine_id"], machine_id)
            self.assertEqual(manager._license_data["offline_entitlement"], "offline-token")

    def test_verify_semantic_not_activated_is_final(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager, _private_key, _key_id = self._make_manager(temp_dir)
            machine_id = "machine-verify-retry"
            manager._save_license(
                {
                    "license_key": "YRG-TEST-VERIFY",
                    "machine_id": machine_id,
                    "customer_name": "测试客户",
                    "last_verified": "2000-01-01 00:00:00",
                }
            )
            responses = iter(
                [
                    {"ok": False, "error": "not_activated", "message": "此设备未激活"},
                    {"ok": False, "error": "not_activated", "message": "此设备未激活"},
                    {
                        "ok": True,
                        "status": "valid",
                        "customer_name": "测试客户",
                        "expires_at": None,
                        "offline_entitlement": "offline-token-verify",
                    },
                ]
            )

            with (
                patch.object(manager, "get_machine_id", return_value=machine_id),
                patch.object(manager, "_post", side_effect=lambda *args, **kwargs: next(responses)) as post_mock,
                patch.object(client_license.time, "sleep", return_value=None),
            ):
                ok, info = manager.verify()

            self.assertFalse(ok)
            self.assertEqual(info["error"], "not_activated")
            self.assertEqual(post_mock.call_count, 1)

    def test_concurrent_verify_access_uses_one_online_verification(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager, _private_key, _key_id = self._make_manager(temp_dir)
            calls = 0
            calls_lock = threading.Lock()

            def verify_once():
                nonlocal calls
                with calls_lock:
                    calls += 1
                time.sleep(0.05)
                return True, {"status": "valid", "customer_name": "测试客户"}

            with patch.object(manager, "verify", side_effect=verify_once):
                with ThreadPoolExecutor(max_workers=5) as pool:
                    results = list(pool.map(lambda _index: manager.verify_access(), range(5)))

            self.assertTrue(all(ok for ok, _info in results))
            self.assertEqual(calls, 1)

    def test_post_does_not_fallback_after_semantic_denial(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager, _private_key, _key_id = self._make_manager(temp_dir)
            manager.server_urls = [
                "https://primary-activation.example.com",
                "https://legacy-activation.example.com",
            ]
            seen_urls = []
            responses = iter(
                [
                    {"ok": False, "error": "invalid_license", "message": "许可证无效"},
                    {"ok": True, "status": "valid"},
                ]
            )

            class FakeResponse:
                def __init__(self, payload):
                    self._payload = payload

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self):
                    return json.dumps(self._payload).encode("utf-8")

            def fake_urlopen(req, timeout=0, context=None):
                seen_urls.append(req.full_url)
                return FakeResponse(next(responses))

            with patch.object(client_license.urllib.request, "urlopen", side_effect=fake_urlopen):
                result = manager._post(
                    "/verify",
                    {"license_key": "YRG-TEST", "machine_id": "machine-1"},
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "invalid_license")
            self.assertEqual(seen_urls, ["https://primary-activation.example.com/verify"])

    def test_post_tries_fallback_activation_server_after_network_reset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager, _private_key, _key_id = self._make_manager(temp_dir)
            manager.server_urls = [
                "https://primary-activation.example.com",
                "https://fallback-activation.example.com",
            ]
            seen_urls = []
            responses = iter(
                [
                    OSError(54, "Connection reset by peer"),
                    {"ok": True, "status": "valid"},
                ]
            )

            class FakeResponse:
                def __init__(self, payload):
                    self._payload = payload

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self):
                    return json.dumps(self._payload).encode("utf-8")

            def fake_urlopen(req, timeout=0, context=None):
                seen_urls.append(req.full_url)
                result = next(responses)
                if isinstance(result, Exception):
                    raise result
                return FakeResponse(result)

            with patch.object(client_license.urllib.request, "urlopen", side_effect=fake_urlopen):
                result = manager._post("/verify", {"license_key": "YRG-TEST", "machine_id": "machine-1"})

            self.assertTrue(result["ok"])
            self.assertEqual(
                seen_urls,
                [
                    "https://primary-activation.example.com/verify",
                    "https://fallback-activation.example.com/verify",
                ],
            )

    def test_post_reports_customer_friendly_message_when_all_activation_servers_fail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager, _private_key, _key_id = self._make_manager(temp_dir)
            manager.server_urls = [
                "https://primary-activation.example.com",
                "https://fallback-activation.example.com",
            ]

            with patch.object(
                client_license.urllib.request,
                "urlopen",
                side_effect=OSError(54, "Connection reset by peer"),
            ):
                result = manager._post("/activate", {"license_key": "YRG-TEST", "machine_id": "machine-1"})

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "network_error")
            self.assertIn("激活服务", result["message"])
            self.assertIn("Connection reset by peer", result["message"])


if __name__ == "__main__":
    unittest.main()
