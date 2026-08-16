import importlib.util
import json
import os
import pathlib
import socket
import time
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from unittest.mock import patch


_IMPORT_RUNTIME = tempfile.TemporaryDirectory()
_PREVIOUS_STATE_DIR = os.environ.get("YIRENGONGIS_STATE_DIR")
os.environ["YIRENGONGIS_STATE_DIR"] = _IMPORT_RUNTIME.name
try:
    MODULE_PATH = pathlib.Path(__file__).with_name("runner.py")
    SPEC = importlib.util.spec_from_file_location("runner_security_module", MODULE_PATH)
    runner = importlib.util.module_from_spec(SPEC)
    assert SPEC and SPEC.loader
    SPEC.loader.exec_module(runner)
finally:
    if _PREVIOUS_STATE_DIR is None:
        os.environ.pop("YIRENGONGIS_STATE_DIR", None)
    else:
        os.environ["YIRENGONGIS_STATE_DIR"] = _PREVIOUS_STATE_DIR


def _request_json(url: str, *, method: str = "GET", headers: dict | None = None, payload=None):
    request = urllib.request.Request(url, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request.data = data
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, dict(response.headers.items()), json.loads(response.read().decode("utf-8"))


@contextmanager
def temporary_runner_server(
    *,
    public_config: dict | None = None,
    secret_config: dict | None = None,
    session_token: str = "session-token",
    license_mgr=None,
):
    with tempfile.TemporaryDirectory() as temp_dir:
        root = pathlib.Path(temp_dir)
        auth_dir = root / ".auth"
        downloads_dir = root / "downloads"
        auth_dir.mkdir(parents=True, exist_ok=True)
        downloads_dir.mkdir(parents=True, exist_ok=True)

        config_file = auth_dir / "customer_config.json"
        secret_file = auth_dir / "customer_secrets.json"
        monitor_file = root / "monitor.html"
        history_file = downloads_dir / "run_history.json"

        merged_public = dict(runner.DEFAULT_CONFIG)
        merged_public.update(public_config or {})
        merged_public.pop("feishu_app_token", None)
        merged_public.pop("feishu_app_secret", None)
        config_file.write_text(json.dumps(merged_public, ensure_ascii=False), encoding="utf-8")
        secret_file.write_text(json.dumps(secret_config or {}, ensure_ascii=False), encoding="utf-8")
        history_file.write_text("[]", encoding="utf-8")
        monitor_file.write_text("<!doctype html><title>monitor</title>", encoding="utf-8")

        fake_license_mgr = license_mgr or type(
            "FakeLicenseManager",
            (),
            {
                "verify": lambda self: (True, {"status": "valid"}),
                "verify_access": lambda self: (True, {"status": "valid", "access_mode": "licensed"}),
                "is_activated": lambda self: True,
                "get_license_key": lambda self: "YRG-TEST-1234",
                "get_customer_name": lambda self: "测试客户",
            },
        )()

        with (
            patch.object(runner, "AUTH_DIR", str(auth_dir)),
            patch.object(runner, "CONFIG_FILE", str(config_file)),
            patch.object(runner, "SECRET_CONFIG_FILE", str(secret_file)),
            patch.object(runner, "RUN_HISTORY_FILE", str(history_file)),
            patch.object(runner, "MONITOR_HTML", str(monitor_file)),
            patch.object(runner, "SESSION_TOKEN", session_token),
            patch.object(runner, "LICENSE_MGR", fake_license_mgr),
        ):
            server = runner.ThreadingHTTPServer(("127.0.0.1", 0), runner.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield server, config_file, secret_file
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class ConfigStorageTests(unittest.TestCase):
    def test_load_saved_config_migrates_secrets_out_of_public_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            config_file = root / "customer_config.json"
            secret_file = root / "customer_secrets.json"
            config_file.write_text(
                json.dumps(
                    {
                        "customer_name": "测试客户",
                        "feishu_app_id": "cli_aabbccdd",
                        "feishu_app_token": "bascn123456",
                        "feishu_app_secret": "secret-value",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with (
                patch.object(runner, "CONFIG_FILE", str(config_file)),
                patch.object(runner, "SECRET_CONFIG_FILE", str(secret_file)),
            ):
                merged = runner.load_saved_config()

            public_payload = json.loads(config_file.read_text(encoding="utf-8"))
            secret_payload = json.loads(secret_file.read_text(encoding="utf-8"))
            self.assertNotIn("feishu_app_secret", public_payload)
            self.assertNotIn("feishu_app_token", public_payload)
            self.assertEqual(secret_payload["feishu_app_secret"], "secret-value")
            self.assertEqual(secret_payload["feishu_app_token"], "bascn123456")
            self.assertEqual(merged["feishu_app_secret"], "secret-value")


class ApiSecurityTests(unittest.TestCase):
    def test_auth_health_lease_blocks_revoke_and_reset_routes(self):
        with temporary_runner_server() as (server, config_file, _secret_file):
            base_url = f"http://127.0.0.1:{server.server_port}"
            store = runner.RunLeaseStore(config_file.parent.parent / "downloads" / "runner.lock", ttl_seconds=120)
            lease = store.acquire("auth_health")
            self.assertIsNotNone(lease)
            headers = {"X-YRG-Session": "session-token"}
            with (
                patch.object(runner, "_RUN_LEASE_STORE", store),
                patch.object(runner, "revoke_platform_auth") as revoke,
                patch.object(runner, "reset_onboarding_state") as reset,
            ):
                with self.assertRaises(urllib.error.HTTPError) as revoke_error:
                    _request_json(
                        f"{base_url}/auth_revoke_single?platform=douyin",
                        method="POST",
                        headers=headers,
                        payload={},
                    )
                with self.assertRaises(urllib.error.HTTPError) as reset_error:
                    _request_json(
                        f"{base_url}/reset_onboarding",
                        method="POST",
                        headers=headers,
                        payload={"clear_auth": True},
                    )
                with self.assertRaises(urllib.error.HTTPError) as unlock_error:
                    _request_json(
                        f"{base_url}/unlock",
                        method="POST",
                        headers=headers,
                        payload={},
                    )
            self.assertEqual(revoke_error.exception.code, 409)
            self.assertEqual(reset_error.exception.code, 409)
            self.assertEqual(unlock_error.exception.code, 409)
            self.assertEqual(store.read_payload()["kind"], "auth_health")
            revoke.assert_not_called()
            reset.assert_not_called()
            store.release(lease.run_id)

    def test_runner_binding_rejects_non_loopback_hosts(self):
        for host in ("0.0.0.0", "::", "192.168.1.50", "example.test"):
            with self.subTest(host=host):
                with (
                    patch.object(runner, "_is_wsl", return_value=False),
                    self.assertRaisesRegex(ValueError, "loopback"),
                ):
                    runner._bind_runner_server(host, 0, runner.Handler)

    def test_runner_binding_permits_wildcard_on_wsl_only(self):
        with patch.object(runner, "_is_wsl", return_value=True):
            server = runner._bind_runner_server("0.0.0.0", 0, runner.Handler)
            server.server_close()

    def test_runner_binding_accepts_localhost_only(self):
        servers = []
        try:
            servers.append(runner._bind_runner_server("127.0.0.1", 0, runner.Handler))
            servers.append(runner._bind_runner_server("localhost", 0, runner.Handler))
        finally:
            for server in servers:
                server.server_close()

    def test_session_recovery_without_origin_requires_loopback_client(self):
        with temporary_runner_server(session_token="session-token") as (server, _, _):
            base_url = f"http://127.0.0.1:{server.server_port}"
            status, _, payload = _request_json(f"{base_url}/session/recover")
            self.assertEqual(status, 200)
            self.assertEqual(payload["token"], "session-token")

            with patch.object(runner.Handler, "_is_loopback_client", return_value=False):
                with self.assertRaises(urllib.error.HTTPError) as remote_request:
                    _request_json(f"{base_url}/session/recover")
            self.assertEqual(remote_request.exception.code, 403)

    def test_package_info_endpoint_exposes_package_identity_without_session(self):
        with temporary_runner_server() as (server, _, _):
            base_url = f"http://127.0.0.1:{server.server_port}"
            with patch.object(runner, "current_package_info", return_value={"package_id": "pkg-test-1234", "build_version": "1.0.6", "platform": "mac", "arch": "arm64", "activation_server": "", "activation_servers": []}):
                status, _, payload = _request_json(f"{base_url}/package-info")
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["package_id"], "pkg-test-1234")

    def test_progress_endpoint_allows_file_origin_without_session(self):
        with temporary_runner_server() as (server, _, _):
            base_url = f"http://127.0.0.1:{server.server_port}"
            status, headers, payload = _request_json(
                f"{base_url}/progress",
                headers={"Origin": "null"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("Access-Control-Allow-Origin"), "null")
            self.assertTrue(payload["ok"])

    def test_config_endpoint_requires_session_token(self):
        with temporary_runner_server(
            secret_config={
                "feishu_app_id": "cli_aabbccdd",
                "feishu_app_token": "bascn123456",
                "feishu_app_secret": "secret-value",
            }
        ) as (server, _, _):
            base_url = f"http://127.0.0.1:{server.server_port}"
            with self.assertRaises(urllib.error.HTTPError) as excinfo:
                _request_json(f"{base_url}/config")
            self.assertEqual(excinfo.exception.code, 401)

            status, _, payload = _request_json(
                f"{base_url}/config",
                headers={"X-YRG-Session": "session-token"},
            )
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            self.assertNotIn("feishu_app_secret", payload["config"])
            self.assertNotIn("feishu_app_token", payload["config"])
            self.assertNotIn("feishu_effective", payload["config"])
            self.assertEqual(payload["config"]["feishu_app_id_masked"], "cli_aabb…")
            self.assertEqual(payload["config"]["feishu_app_token_masked"], "bascn1…")

    def test_mutating_endpoint_rejects_cross_origin_even_with_valid_token(self):
        with temporary_runner_server() as (server, _, _):
            base_url = f"http://127.0.0.1:{server.server_port}"
            request = urllib.request.Request(
                f"{base_url}/config",
                method="POST",
                data=json.dumps({"customer_name": "恶意修改"}).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Origin": "https://evil.example",
                    "X-YRG-Session": "session-token",
                },
            )
            with self.assertRaises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(excinfo.exception.code, 403)

    def test_license_endpoint_no_longer_returns_full_license_key(self):
        with temporary_runner_server() as (server, _, _):
            base_url = f"http://127.0.0.1:{server.server_port}"
            status, _, payload = _request_json(
                f"{base_url}/license",
                headers={"X-YRG-Session": "session-token"},
            )
            self.assertEqual(status, 200)
            self.assertNotIn("license_key", payload)
            self.assertEqual(payload["license_key_masked"], "COMMUNITY")


class TauriSupervisionTests(unittest.TestCase):
    def tearDown(self):
        reset = getattr(runner, "_reset_supervised_license_state_for_test", None)
        if reset:
            reset()

    def test_tauri_supervision_disables_legacy_process_sweep(self):
        with patch.dict(
            runner.os.environ,
            {"YIRENGONGIS_SUPERVISED_BY_TAURI": "1"},
            clear=False,
        ):
            self.assertFalse(runner._should_cleanup_stale_runners())

    def test_legacy_launcher_keeps_existing_cleanup(self):
        with patch.dict(runner.os.environ, {}, clear=True):
            self.assertTrue(runner._should_cleanup_stale_runners())

    def test_explicit_bundled_node_wins_without_system_path_lookup(self):
        explicit = "/signed/runtime/node-arm64/node-v20/bin/node"
        env = {"NODE_BIN": explicit, "PATH": ""}
        with patch.object(
            runner,
            "resolve_default_node_bin",
            side_effect=AssertionError("system Node lookup must not run"),
        ):
            self.assertEqual(runner._resolve_node_bin_for_env(env), explicit)

    def test_business_subprocess_environment_scrubs_supervision_secrets(self):
        env = {
            "PATH": "/usr/bin",
            "NODE_BIN": "/signed/bin/node",
            "YIRENGONGIS_SESSION_TOKEN": "secret-token",
            "YIRENGONGIS_SUPERVISED_BY_TAURI": "1",
            "YIRENGONGIS_SIDECAR_INSTANCE_ID": "secret-instance",
            "YIRENGONGIS_RUNNER_READY_NONCE": "secret-ready",
        }
        scrubbed = runner._scrub_supervision_env(env)
        self.assertEqual(scrubbed["NODE_BIN"], "/signed/bin/node")
        for key in (
            "YIRENGONGIS_SESSION_TOKEN",
            "YIRENGONGIS_SUPERVISED_BY_TAURI",
            "YIRENGONGIS_SIDECAR_INSTANCE_ID",
            "YIRENGONGIS_RUNNER_READY_NONCE",
        ):
            self.assertNotIn(key, scrubbed)

    def test_platform_supervisor_scrubs_secrets_even_for_bypass_env(self):
        captured = {}

        def fake_run_supervised(command, **kwargs):
            captured.update(kwargs["env"])
            return object()

        handler = object.__new__(runner.Handler)
        with patch.object(runner, "run_supervised", side_effect=fake_run_supervised):
            handler._run_platform_script(
                ["/bin/true"],
                {
                    "YIRENGONGIS_SESSION_TOKEN": "secret-token",
                    "YIRENGONGIS_SUPERVISED_BY_TAURI": "1",
                    "YIRENGONGIS_SIDECAR_INSTANCE_ID": "secret-instance",
                },
                platform_id="douyin",
                progress_path="/tmp/progress.json",
                run_id="run-test",
            )
        self.assertNotIn("YIRENGONGIS_SESSION_TOKEN", captured)
        self.assertNotIn("YIRENGONGIS_SUPERVISED_BY_TAURI", captured)
        self.assertNotIn("YIRENGONGIS_SIDECAR_INSTANCE_ID", captured)

    def test_effective_lark_cli_env_scrubs_supervision_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.dict(
                    runner.os.environ,
                    {
                        "PATH": "/signed/node/bin",
                        "NODE_BIN": "/signed/node/bin/node",
                        "YIRENGONGIS_SESSION_TOKEN": "secret-token",
                        "YIRENGONGIS_SUPERVISED_BY_TAURI": "1",
                        "YIRENGONGIS_SIDECAR_INSTANCE_ID": "secret-instance",
                        "YIRENGONGIS_RUNNER_READY_NONCE": "secret-ready",
                    },
                    clear=True,
                ),
                patch.object(runner, "_active_lark_cli_home", return_value=temp_dir),
            ):
                env = runner._lark_cli_env(use_global=False)
        self.assertEqual(env["NODE_BIN"], "/signed/node/bin/node")
        for key in (
            "YIRENGONGIS_SESSION_TOKEN",
            "YIRENGONGIS_SUPERVISED_BY_TAURI",
            "YIRENGONGIS_SIDECAR_INSTANCE_ID",
            "YIRENGONGIS_RUNNER_READY_NONCE",
        ):
            self.assertNotIn(key, env)

    def test_occupied_preferred_port_is_rebound_by_python_to_dynamic_port(self):
        occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        preferred = occupied.getsockname()[1]
        server = None
        try:
            server = runner._bind_runner_server("127.0.0.1", preferred, runner.Handler)
            self.assertNotEqual(server.server_port, preferred)
        finally:
            if server is not None:
                server.server_close()
            occupied.close()

    def test_supervised_health_requires_header_token_and_reports_identity(self):
        with temporary_runner_server(session_token="session-token") as (server, _, _):
            base_url = f"http://127.0.0.1:{server.server_port}"
            with (
                patch.dict(
                    runner.os.environ,
                    {"YIRENGONGIS_SUPERVISED_BY_TAURI": "1"},
                    clear=False,
                ),
                patch.object(
                    runner,
                    "current_package_info",
                    return_value={
                        "package_id": "pkg-test",
                        "build_version": "20260711",
                    },
                ),
            ):
                with self.assertRaises(urllib.error.HTTPError) as excinfo:
                    _request_json(f"{base_url}/supervised/health")
                self.assertEqual(excinfo.exception.code, 401)
                status, _, payload = _request_json(
                    f"{base_url}/supervised/health",
                    headers={"X-YRG-Session": "session-token"},
                )
            self.assertEqual(status, 200)
            self.assertEqual(payload["package_id"], "pkg-test")
            self.assertEqual(payload["build_version"], "20260711")
            self.assertEqual(payload["port"], server.server_port)

    def test_supervised_session_recovery_rejects_missing_origin_and_query_token(self):
        with temporary_runner_server(session_token="session-token") as (server, _, _):
            base_url = f"http://127.0.0.1:{server.server_port}"
            with patch.dict(
                runner.os.environ,
                {"YIRENGONGIS_SUPERVISED_BY_TAURI": "1"},
                clear=False,
            ):
                with self.assertRaises(urllib.error.HTTPError) as no_origin:
                    _request_json(f"{base_url}/session/recover")
                self.assertEqual(no_origin.exception.code, 403)
                with self.assertRaises(urllib.error.HTTPError) as query_auth:
                    _request_json(f"{base_url}/config?session=session-token")
                self.assertEqual(query_auth.exception.code, 401)
                with self.assertRaises(urllib.error.HTTPError) as same_origin:
                    _request_json(
                        f"{base_url}/session/recover",
                        headers={"Origin": base_url},
                    )
                self.assertEqual(same_origin.exception.code, 403)

    def test_supervised_progress_requires_header_token(self):
        with temporary_runner_server(session_token="session-token") as (server, _, _):
            base_url = f"http://127.0.0.1:{server.server_port}"
            with patch.dict(
                runner.os.environ,
                {"YIRENGONGIS_SUPERVISED_BY_TAURI": "1"},
                clear=False,
            ):
                with self.assertRaises(urllib.error.HTTPError) as missing:
                    _request_json(f"{base_url}/progress")
                self.assertEqual(missing.exception.code, 401)
                status, _, payload = _request_json(
                    f"{base_url}/progress",
                    headers={"X-YRG-Session": "session-token"},
                )
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])

    @unittest.skipIf(runner.COMMUNITY_EDITION_ENABLED, "commercial license flow")
    def test_supervised_license_activation_is_deferred_and_nonblocking(self):
        activated = threading.Event()

        class SlowLicenseManager:
            def is_activated(self):
                return False

            def verify_access(self):
                time.sleep(0.4)
                activated.set()
                return False, {"error": "network_error", "access_mode": "none"}

        with (
            patch.dict(
                runner.os.environ,
                {"YIRENGONGIS_SUPERVISED_BY_TAURI": "1"},
                clear=False,
            ),
            patch.object(runner, "LICENSE_MGR", SlowLicenseManager()),
        ):
            runner._reset_supervised_license_state_for_test()
            started = time.monotonic()
            runner._initialize_seeded_license_activation()
            runner._start_supervised_license_background()
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.15)
            self.assertFalse(activated.wait(0.1))
            self.assertTrue(activated.wait(2))
            runner._wait_for_supervised_license_terminal_for_test(2)

    @unittest.skipIf(runner.COMMUNITY_EDITION_ENABLED, "commercial license flow")
    def test_supervised_license_pending_is_fast_serialized_and_terminal(self):
        class BlockingLicenseManager:
            def __init__(self):
                self.verify_access_calls = 0
                self.activate_calls = 0
                self.entered = threading.Event()
                self.release = threading.Event()

            def is_activated(self):
                return False

            def get_license_key(self):
                return ""

            def get_customer_name(self):
                return ""

            def verify_access(self):
                self.verify_access_calls += 1
                self.entered.set()
                self.release.wait(5)
                return False, {
                    "error": "network_error",
                    "message": "verification unavailable",
                    "access_mode": "none",
                }

            def activate(self, key):
                self.activate_calls += 1
                return True, key

        manager = BlockingLicenseManager()
        with (
            patch.dict(
                runner.os.environ,
                {"YIRENGONGIS_SUPERVISED_BY_TAURI": "1"},
                clear=False,
            ),
            patch.object(runner, "LICENSE_MGR", manager),
        ):
            runner._reset_supervised_license_state_for_test()
            runner._start_supervised_license_background()
            self.assertTrue(manager.entered.wait(2))
            with temporary_runner_server(license_mgr=manager) as (server, _, _):
                base_url = f"http://127.0.0.1:{server.server_port}"
                headers = {"X-YRG-Session": "session-token"}
                started = time.monotonic()
                for _ in range(100):
                    status, _, payload = _request_json(
                        f"{base_url}/license",
                        headers=headers,
                    )
                    self.assertEqual(status, 200)
                    self.assertTrue(payload["checking"])
                    self.assertEqual(payload["info"]["error"], "license_check_pending")
                self.assertLess(time.monotonic() - started, 2.0)

                with self.assertRaises(urllib.error.HTTPError) as protected:
                    _request_json(f"{base_url}/config", headers=headers)
                self.assertEqual(protected.exception.code, 503)
                self.assertEqual(
                    json.loads(protected.exception.read().decode("utf-8"))["error"],
                    "license_check_pending",
                )

                with self.assertRaises(urllib.error.HTTPError) as activation:
                    _request_json(
                        f"{base_url}/license/activate",
                        method="POST",
                        headers=headers,
                        payload={"license_key": "YRG-DIFFERENT-KEY"},
                    )
                self.assertEqual(activation.exception.code, 409)
                self.assertEqual(manager.activate_calls, 0)

                manager.release.set()
                runner._wait_for_supervised_license_terminal_for_test(2)
                calls = manager.verify_access_calls
                status, _, payload = _request_json(f"{base_url}/license", headers=headers)
                self.assertEqual(status, 200)
                self.assertFalse(payload["checking"])
                self.assertEqual(payload["info"]["error"], "network_error")
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    _request_json(f"{base_url}/config", headers=headers)
                self.assertEqual(denied.exception.code, 403)
                self.assertEqual(manager.verify_access_calls, calls)

    @unittest.skipIf(runner.COMMUNITY_EDITION_ENABLED, "commercial license flow")
    def test_supervised_seed_activation_preserves_automatic_first_machine_flow(self):
        class SeededLicenseManager:
            def __init__(self):
                self.verify_access_calls = 0
                self.activate_calls = 0

            def is_activated(self):
                return True

            def get_license_key(self):
                return "YRG-TEST-1234"

            def get_customer_name(self):
                return "测试客户"

            def verify_access(self):
                self.verify_access_calls += 1
                if self.verify_access_calls == 1:
                    return False, {
                        "error": "machine_mismatch",
                        "message": "machine mismatch",
                        "access_mode": "none",
                    }
                return True, {"status": "valid", "access_mode": "license"}

            def activate(self, key):
                self.activate_calls += 1
                return True, "activated"

        manager = SeededLicenseManager()
        with (
            patch.dict(
                runner.os.environ,
                {"YIRENGONGIS_SUPERVISED_BY_TAURI": "1"},
                clear=False,
            ),
            patch.object(runner, "LICENSE_MGR", manager),
            patch(
                "package_identity.package_license_allowed",
                return_value=(True, ""),
            ),
        ):
            runner._reset_supervised_license_state_for_test()
            runner._start_supervised_license_background()
            runner._wait_for_supervised_license_terminal_for_test(2)
            phase, ok, info = runner._supervised_license_snapshot()
        self.assertEqual(phase, "done")
        self.assertTrue(ok)
        self.assertEqual(info["access_mode"], "license")
        self.assertEqual(manager.activate_calls, 1)
        self.assertEqual(manager.verify_access_calls, 2)

    @unittest.skipIf(runner.COMMUNITY_EDITION_ENABLED, "commercial license flow")
    def test_expired_supervised_license_blocks_business_until_background_refresh_finishes(self):
        class RevokedLicenseManager:
            def __init__(self):
                self.verify_access_calls = 0
                self.refresh_entered = threading.Event()
                self.refresh_release = threading.Event()

            def is_activated(self):
                return False

            def get_license_key(self):
                return ""

            def get_customer_name(self):
                return "测试客户"

            def verify_access(self):
                self.verify_access_calls += 1
                if self.verify_access_calls == 1:
                    return True, {"status": "valid", "access_mode": "license"}
                self.refresh_entered.set()
                self.refresh_release.wait(5)
                return False, {
                    "error": "revoked",
                    "message": "license revoked",
                    "access_mode": "none",
                }

        manager = RevokedLicenseManager()
        with (
            patch.dict(
                runner.os.environ,
                {"YIRENGONGIS_SUPERVISED_BY_TAURI": "1"},
                clear=False,
            ),
            patch.object(runner, "LICENSE_MGR", manager),
            patch.object(runner, "_SUPERVISED_LICENSE_RESULT_TTL_SECONDS", 0.0),
        ):
            runner._reset_supervised_license_state_for_test()
            runner._start_supervised_license_background()
            runner._wait_for_supervised_license_terminal_for_test(2)
            with temporary_runner_server(license_mgr=manager) as (server, _, _):
                base_url = f"http://127.0.0.1:{server.server_port}"
                headers = {"X-YRG-Session": "session-token"}
                status, _, payload = _request_json(f"{base_url}/license", headers=headers)
                self.assertEqual(status, 200)
                self.assertTrue(payload["checking"])
                self.assertTrue(payload["info"]["last_known_valid"])
                self.assertTrue(manager.refresh_entered.wait(2))
                with self.assertRaises(urllib.error.HTTPError) as pending:
                    _request_json(f"{base_url}/config", headers=headers)
                self.assertEqual(pending.exception.code, 503)

                manager.refresh_release.set()
                runner._wait_for_supervised_license_terminal_for_test(2)
                runner._SUPERVISED_LICENSE_RESULT_TTL_SECONDS = 999.0
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    _request_json(f"{base_url}/config", headers=headers)
                self.assertEqual(denied.exception.code, 403)
                self.assertEqual(manager.verify_access_calls, 2)

    @unittest.skipIf(runner.COMMUNITY_EDITION_ENABLED, "commercial license flow")
    def test_supervised_license_worker_failure_always_publishes_terminal_result(self):
        class FailingLicenseManager:
            def is_activated(self):
                return False

            def verify_access(self):
                raise SystemExit("worker stopped")

        with (
            patch.dict(
                runner.os.environ,
                {"YIRENGONGIS_SUPERVISED_BY_TAURI": "1"},
                clear=False,
            ),
            patch.object(runner, "LICENSE_MGR", FailingLicenseManager()),
        ):
            runner._reset_supervised_license_state_for_test()
            runner._start_supervised_license_background()
            phase, ok, info = runner._wait_for_supervised_license_terminal_for_test(2)
        self.assertEqual(phase, "done")
        self.assertFalse(ok)
        self.assertEqual(info["error"], "license_check_failed")

    def test_legacy_license_activation_remains_synchronous(self):
        with (
            patch.dict(runner.os.environ, {}, clear=True),
            patch.object(runner, "LICENSE_BYPASS_ENABLED", False),
            patch.object(runner, "_auto_activate_seeded_license") as activate,
        ):
            runner._initialize_seeded_license_activation()
        activate.assert_called_once_with()

    def test_community_license_skips_legacy_auto_activation(self):
        with (
            patch.object(runner, "LICENSE_BYPASS_ENABLED", True),
            patch.object(runner, "_auto_activate_seeded_license") as activate,
        ):
            runner._initialize_seeded_license_activation()
        activate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
