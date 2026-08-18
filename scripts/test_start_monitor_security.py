import importlib.util
import os
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


MODULE_PATH = pathlib.Path(__file__).with_name("start_monitor.py")
SPEC = importlib.util.spec_from_file_location("start_monitor_security_module", MODULE_PATH)
start_monitor = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(start_monitor)

from core import paths as core_paths


class _FakeProc:
    def poll(self):
        return None


class StartMonitorSecurityTests(unittest.TestCase):
    def test_macos_source_state_defaults_to_user_application_support(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = pathlib.Path(temp_dir)
            source_root = temp_root / "readonly-source"
            fake_home = temp_root / "home"
            source_root.mkdir()

            with (
                patch.object(core_paths.sys, "platform", "darwin"),
                patch.object(core_paths.Path, "home", return_value=fake_home),
                patch.dict(core_paths.os.environ, {"YIRENGONGIS_STATE_DIR": ""}, clear=False),
            ):
                state_dir = pathlib.Path(core_paths.seed_state_from_bundle(source_root))
                browser_root = start_monitor.playwright_browser_root(source_root, state_dir)

            self.assertEqual(
                state_dir,
                fake_home / "Library" / "Application Support" / core_paths.APP_SUPPORT_NAME,
            )
            self.assertEqual(browser_root, state_dir / ".playwright-browsers")
            self.assertNotEqual(state_dir, source_root)
            self.assertFalse((source_root / ".auth").exists())
            self.assertFalse((source_root / "downloads").exists())
            if os.name != "nt":  # Windows 的 chmod 是只读位语义，无 0o700
                self.assertEqual(state_dir.stat().st_mode & 0o777, 0o700)

    def test_explicit_state_dir_override_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = pathlib.Path(temp_dir)
            explicit_state = temp_root / "custom-state"
            with patch.dict(
                core_paths.os.environ,
                {"YIRENGONGIS_STATE_DIR": str(explicit_state)},
                clear=False,
            ):
                result = core_paths.resolve_state_dir(temp_root / "source")
            self.assertEqual(pathlib.Path(result), explicit_state.resolve())

    def test_node_runtime_accepts_only_supported_node_22_line(self):
        for version in ("v22.12.0", "v22.14.0", "22.99.1"):
            with self.subTest(version=version):
                with (
                    patch.object(start_monitor, "find_cmd", return_value="/tmp/node"),
                    patch.object(
                        start_monitor.subprocess,
                        "run",
                        return_value=SimpleNamespace(returncode=0, stdout=version),
                    ),
                ):
                    start_monitor.ensure_node_runtime()

        for version in ("v18.20.0", "v22.11.9", "v23.0.0", "unknown"):
            with self.subTest(version=version):
                with (
                    patch.object(start_monitor, "find_cmd", return_value="/tmp/node"),
                    patch.object(
                        start_monitor.subprocess,
                        "run",
                        return_value=SimpleNamespace(returncode=0, stdout=version),
                    ),
                ):
                    with self.assertRaises(RuntimeError):
                        start_monitor.ensure_node_runtime()

    def test_launcher_rejects_non_loopback_host_before_starting_or_writing_state(self):
        args = SimpleNamespace(
            port=8899,
            host="0.0.0.0",
            no_open=True,
            foreground=False,
            timeout=5,
            skip_bootstrap=True,
        )
        with (
            patch.object(start_monitor, "parse_args", return_value=args),
            patch.object(start_monitor, "seed_state_from_bundle") as seed_state,
            patch.object(start_monitor.subprocess, "Popen") as popen,
        ):
            result = start_monitor.main()

        self.assertEqual(result, 2)
        seed_state.assert_not_called()
        popen.assert_not_called()

    def test_loopback_host_validation_accepts_only_local_addresses(self):
        for host in ("127.0.0.1", "localhost"):
            with self.subTest(host=host):
                self.assertTrue(start_monitor._is_loopback_host(host))
        for host in ("", "127.0.0.2", "0.0.0.0", "::1", "[::1]", "::", "[::]", "192.168.1.50", "example.test"):
            with self.subTest(host=host):
                self.assertFalse(start_monitor._is_loopback_host(host))

    def test_unrelated_listener_is_not_killed(self):
        with (
            patch.object(start_monitor, "listener_pids_for_port", return_value=[123]),
            patch.object(
                start_monitor,
                "process_command",
                return_value="python3 -m http.server 8811",
            ),
            patch.object(start_monitor.os, "kill") as kill_mock,
        ):
            result = start_monitor.terminate_listener_pids(
                8811,
                reason="build_mismatch",
                expected_base_dir=pathlib.Path("/Applications/数据科学家 Community.app/Contents/Resources/app"),
            )

        self.assertFalse(result)
        kill_mock.assert_not_called()

    def test_launcher_starts_runner_on_loopback_and_persists_session_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            state_dir = root / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            (root / "scripts").mkdir(parents=True, exist_ok=True)
            (root / "frontend").mkdir(parents=True, exist_ok=True)
            (root / ".auth").mkdir(parents=True, exist_ok=True)
            (root / "scripts" / "_run.py").write_text("print('stub')\n", encoding="utf-8")
            (root / "frontend" / "loading.html").write_text("<html></html>", encoding="utf-8")

            args = SimpleNamespace(
                port=8899,
                host="127.0.0.1",
                no_open=True,
                foreground=False,
                timeout=5,
                skip_bootstrap=True,
            )
            popen_kwargs = {}

            def fake_popen(cmd, **kwargs):
                popen_kwargs["cmd"] = cmd
                popen_kwargs["env"] = kwargs["env"]
                return _FakeProc()

            with (
                patch.object(start_monitor, "__file__", str(root / "scripts" / "start_monitor.py")),
                patch.object(start_monitor, "parse_args", return_value=args),
                patch.object(start_monitor, "is_port_open", return_value=False),
                patch.object(start_monitor, "wait_until_ready", return_value=True),
                patch.object(start_monitor, "seed_state_from_bundle", return_value=str(state_dir)),
                patch.object(start_monitor, "verify_package_manifest", return_value={"ok": True}),
                patch.object(start_monitor.subprocess, "Popen", side_effect=fake_popen),
            ):
                result = start_monitor.main()

            self.assertEqual(result, 0)
            self.assertEqual(popen_kwargs["env"]["YIRENGONGIS_RUNNER_HOST"], "127.0.0.1")
            self.assertTrue(popen_kwargs["env"]["YIRENGONGIS_SESSION_TOKEN"])
            saved_token = start_monitor.load_saved_session_token(root, state_dir)
            self.assertEqual(saved_token, popen_kwargs["env"]["YIRENGONGIS_SESSION_TOKEN"])

    @unittest.skipIf(
        os.name == "nt",
        "macOS Launch Services 直启流程仅在 POSIX 验证（Windows 走 Popen 分支）",
    )
    def test_safe_open_browser_prefers_direct_new_window_on_macos(self):
        popen_calls = []

        def fake_popen(cmd, **kwargs):
            popen_calls.append((cmd, kwargs))
            return _FakeProc()

        with (
            patch.object(start_monitor.sys, "platform", "darwin"),
            patch.object(start_monitor, "find_browser_executable", return_value=pathlib.Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")),
            patch.object(start_monitor.subprocess, "Popen", side_effect=fake_popen),
            patch.object(start_monitor.webbrowser, "open") as webbrowser_open,
        ):
            result = start_monitor.safe_open_browser("file:///tmp/loading.html?port=8811#session=abc", pathlib.Path("/tmp"), "chrome")

        self.assertTrue(result)
        self.assertTrue(popen_calls, "expected launcher to invoke macOS Launch Services")
        self.assertEqual(
            popen_calls[0][0],
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "--new-window",
                "file:///tmp/loading.html?port=8811#session=abc",
            ],
        )
        webbrowser_open.assert_not_called()

    def test_find_browser_executable_falls_back_when_system_chrome_is_blocked(self):
        system_chrome = pathlib.Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        managed_chromium = pathlib.Path("/tmp/chromium/Google Chrome for Testing")

        with (
            patch.object(start_monitor, "find_channel_executable", return_value=system_chrome),
            patch.object(start_monitor, "find_playwright_chromium", return_value=managed_chromium),
            patch.object(start_monitor, "browser_executable_is_usable", side_effect=[False, True]) as usable,
        ):
            result = start_monitor.find_browser_executable(pathlib.Path("/tmp/app"), "chrome")

        self.assertEqual(result, managed_chromium)
        self.assertEqual([call.args[0] for call in usable.call_args_list], [system_chrome, managed_chromium])

    def test_find_browser_executable_uses_app_payload_browser_before_staged_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            staged_root = root / "staged-browsers" / "chromium-2000" / "chrome-mac-arm64"
            app_payload = root / "app-payload"
            app_browser_root = app_payload / "runtime" / "playwright-browsers" / "chromium-3000" / "chrome-mac-arm64"
            staged_root.mkdir(parents=True)
            app_browser_root.mkdir(parents=True)
            staged_browser = staged_root / "Google Chrome for Testing"
            app_browser = app_browser_root / "Google Chrome for Testing"
            staged_browser.write_text("", encoding="utf-8")
            app_browser.write_text("", encoding="utf-8")
            # Windows 侧的可执行名不同（chrome.exe），两种名字都放置，
            # 保证 find_playwright_chromium 的目录扫描在两个平台都能命中。
            (staged_root / "chrome.exe").write_text("", encoding="utf-8")
            (app_browser_root / "chrome.exe").write_text("", encoding="utf-8")

            def fake_usable(path):
                return path in (app_browser, app_browser_root / "chrome.exe")

            with (
                patch.dict(
                    start_monitor.os.environ,
                    {
                        "PLAYWRIGHT_BROWSERS_PATH": str(root / "staged-browsers"),
                        "YIRENGONGIS_APP_PAYLOAD_DIR": str(app_payload),
                    },
                    clear=False,
                ),
                patch.object(start_monitor, "find_channel_executable", return_value=pathlib.Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")),
                patch.object(start_monitor, "browser_executable_is_usable", side_effect=fake_usable),
            ):
                result = start_monitor.find_browser_executable(root, "chrome")

            self.assertIn(result, (app_browser, app_browser_root / "chrome.exe"))

    def test_launcher_fails_visibly_when_forced_browser_is_unusable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            state_dir = root / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            (root / "scripts").mkdir(parents=True, exist_ok=True)
            (root / "frontend").mkdir(parents=True, exist_ok=True)
            (root / ".auth").mkdir(parents=True, exist_ok=True)
            (root / "scripts" / "_run.py").write_text("print('stub')\n", encoding="utf-8")
            (root / "frontend" / "loading.html").write_text("<html></html>", encoding="utf-8")

            args = SimpleNamespace(
                port=8899,
                host="127.0.0.1",
                no_open=False,
                foreground=False,
                timeout=5,
                skip_bootstrap=True,
            )

            with (
                patch.object(start_monitor, "__file__", str(root / "scripts" / "start_monitor.py")),
                patch.object(start_monitor, "parse_args", return_value=args),
                patch.object(start_monitor, "is_port_open", return_value=False),
                patch.object(start_monitor, "seed_state_from_bundle", return_value=str(state_dir)),
                patch.object(start_monitor, "verify_package_manifest", return_value={"ok": True}),
                patch.object(start_monitor, "find_browser_executable", return_value=None),
                patch.object(start_monitor.subprocess, "Popen") as popen,
            ):
                result = start_monitor.main()

            self.assertEqual(result, 6)
            popen.assert_not_called()

    def test_ensure_playwright_browsers_does_not_reinstall_blocked_runtime(self):
        managed_chromium = pathlib.Path("/tmp/chromium/Google Chrome for Testing")

        with (
            patch.object(start_monitor, "find_channel_executable", return_value=None),
            patch.object(start_monitor, "find_playwright_chromium", return_value=managed_chromium),
            patch.object(start_monitor, "browser_executable_is_usable", return_value=False),
            patch.object(start_monitor, "run_cmd") as run_cmd,
        ):
            with self.assertRaises(RuntimeError):
                start_monitor.ensure_playwright_browsers(pathlib.Path("/tmp/app"), "chrome")

        run_cmd.assert_not_called()

    def test_ensure_playwright_browsers_does_not_reinstall_corrupt_bundled_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            (root / "runtime" / "playwright-browsers").mkdir(parents=True)
            with (
                patch.object(start_monitor, "find_channel_executable", return_value=None),
                patch.object(start_monitor, "find_playwright_chromium", return_value=None),
                patch.object(start_monitor, "run_cmd") as run_cmd,
            ):
                with self.assertRaises(RuntimeError):
                    start_monitor.ensure_playwright_browsers(root, "chrome")

        run_cmd.assert_not_called()

    def test_launcher_does_not_reuse_runner_from_different_package(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            state_dir = root / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            (root / "scripts").mkdir(parents=True, exist_ok=True)
            (root / "frontend").mkdir(parents=True, exist_ok=True)
            (root / ".auth").mkdir(parents=True, exist_ok=True)
            (root / "scripts" / "_run.py").write_text("print('stub')\n", encoding="utf-8")
            (root / "frontend" / "loading.html").write_text("<html></html>", encoding="utf-8")

            args = SimpleNamespace(
                port=8899,
                host="127.0.0.1",
                no_open=True,
                foreground=False,
                timeout=5,
                skip_bootstrap=True,
            )
            popen_kwargs = {}

            def fake_popen(cmd, **kwargs):
                popen_kwargs["cmd"] = cmd
                popen_kwargs["env"] = kwargs["env"]
                return _FakeProc()

            with (
                patch.object(start_monitor, "__file__", str(root / "scripts" / "start_monitor.py")),
                patch.object(start_monitor, "parse_args", return_value=args),
                patch.object(start_monitor, "is_port_open", side_effect=[True, True, False]),
                patch.object(start_monitor, "is_healthy_runner", return_value=True),
                patch.object(start_monitor, "wait_until_ready", return_value=True),
                patch.object(start_monitor, "seed_state_from_bundle", return_value=str(state_dir)),
                patch.object(start_monitor, "verify_package_manifest", return_value={"ok": True}),
                patch.object(start_monitor, "load_current_package_info", return_value={"package_id": "pkg-current", "build_version": "1.0"}),
                patch.object(start_monitor, "fetch_runner_package_info", return_value={"package_id": "pkg-other", "build_version": "1.0"}),
                patch.object(start_monitor, "terminate_listener_pids", return_value=True),
                patch.object(start_monitor.subprocess, "Popen", side_effect=fake_popen),
            ):
                result = start_monitor.main()

            self.assertEqual(result, 0)
            self.assertEqual(popen_kwargs["env"]["YIRENGONGIS_RUNNER_PORT"], "8900")


if __name__ == "__main__":
    unittest.main()
