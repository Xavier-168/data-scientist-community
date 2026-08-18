"""Windows 原生适配契约测试。

覆盖：状态目录 win32 分支、.cmd 采集包装与 .sh 的默认值契约、
跨平台同步看门狗、更新身份的平台一致性、端口监听辅助函数。
"""

import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from core import paths as core_paths
from core import process as core_process


WRAPPER_CONTRACTS = (
    ("run_export.cmd", "run_export.sh", {
        "SCAN_WAIT_MS": "600000", "VIDEO_LIMIT": "999",
        "STALE_ROUNDS_LIMIT": "3", "FORCE_FULL_EXPORT": "false",
    }),
    ("run_xhs_export.cmd", "run_xhs_export.sh", {
        "SCAN_WAIT_MS": "300000", "VIDEO_LIMIT": "50",
        "STALE_ROUNDS_LIMIT": "3",
    }),
    ("run_bili_export.cmd", "run_bili_export.sh", {
        "SCAN_WAIT_MS": "300000", "VIDEO_LIMIT": "200", "AUTH_ONLY": "false",
    }),
    ("run_ks_export.cmd", "run_ks_export.sh", {
        "SCAN_WAIT_MS": "300000", "VIDEO_LIMIT": "200",
        "STALE_ROUNDS_LIMIT": "6", "AUTH_ONLY": "false",
    }),
)

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent


class WindowsStateDirContractTests(unittest.TestCase):
    def test_windows_state_dir_defaults_to_appdata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_appdata = pathlib.Path(temp_dir) / "Roaming"
            source_root = pathlib.Path(temp_dir) / "source"
            source_root.mkdir()
            with (
                mock.patch.object(core_paths.sys, "platform", "win32"),
                mock.patch.dict(
                    core_paths.os.environ,
                    {"APPDATA": str(fake_appdata), "YIRENGONGIS_STATE_DIR": ""},
                    clear=False,
                ),
            ):
                state_dir = pathlib.Path(core_paths.resolve_state_dir(source_root))

            self.assertEqual(state_dir, fake_appdata / core_paths.APP_SUPPORT_NAME)
            self.assertNotEqual(state_dir, source_root)

    def test_windows_state_dir_accepts_explicit_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            explicit = pathlib.Path(temp_dir) / "custom"
            with mock.patch.dict(
                core_paths.os.environ,
                {"YIRENGONGIS_STATE_DIR": str(explicit)},
                clear=False,
            ):
                result = core_paths.resolve_state_dir(pathlib.Path(temp_dir))
            self.assertEqual(pathlib.Path(result), explicit.resolve())


class CollectorWrapperContractTests(unittest.TestCase):
    def _defaults_from_sh(self, sh_text: str) -> dict:
        defaults = {}
        for line in sh_text.splitlines():
            line = line.strip()
            if line.startswith("export ") and ":-" in line:
                name, _, value = line[len("export "):].partition("=")
                value = value.strip('"')
                if ":-" in value:
                    key, _, fallback = value.partition(":-")
                    fallback = fallback.rstrip("}")
                    # 空默认值（MIN_PUBLISH_DATE 等）在 .cmd 中以"未定义"表达，跳过
                    if fallback:
                        defaults[name] = fallback
        return defaults

    def _defaults_from_cmd(self, cmd_text: str) -> dict:
        defaults = {}
        for line in cmd_text.splitlines():
            line = line.strip()
            if line.startswith("if not defined ") and ' set "' in line:
                body = line[len("if not defined "):]
                _, _, assignment = body.partition(' set "')
                name, _, value = assignment.rstrip('"').partition("=")
                defaults[name] = value
        return defaults

    def test_cmd_wrappers_exist_for_every_platform(self):
        for cmd_name, _sh_name, _extra in WRAPPER_CONTRACTS:
            path = SCRIPTS_DIR / cmd_name
            self.assertTrue(path.exists(), f"missing wrapper: {path}")

    def test_cmd_wrapper_defaults_mirror_sh_contract(self):
        for cmd_name, sh_name, expected in WRAPPER_CONTRACTS:
            with self.subTest(wrapper=cmd_name):
                cmd_defaults = self._defaults_from_cmd(
                    (SCRIPTS_DIR / cmd_name).read_text(encoding="utf-8")
                )
                sh_defaults = self._defaults_from_sh(
                    (SCRIPTS_DIR / sh_name).read_text(encoding="utf-8")
                )
                for key, value in expected.items():
                    self.assertEqual(cmd_defaults.get(key), value)
                    self.assertEqual(
                        sh_defaults.get(key), value,
                        f"{sh_name} 默认值变化时需同步 {cmd_name}",
                    )
                # .cmd 与 .sh 的非空默认值集合一致（引用其他变量的动态默认除外）
                literal_cmd = {
                    key for key, value in cmd_defaults.items()
                    if "%" not in value
                }
                literal_sh = {
                    key for key, value in sh_defaults.items()
                    if "$" not in value
                }
                self.assertEqual(literal_cmd, literal_sh)


class SyncWatchdogTests(unittest.TestCase):
    def test_normal_function_returns_result(self):
        import sync_feishu_bitable_openapi as feishu_sync

        self.assertEqual(feishu_sync._run_sync_with_timeout(lambda: "ok", 5), "ok")

    def test_timeout_raises_promptly(self):
        import sync_feishu_bitable_openapi as feishu_sync

        started = time.time()
        with self.assertRaises(feishu_sync.SyncTimeoutError):
            feishu_sync._run_sync_with_timeout(lambda: time.sleep(3), 0.5)
        self.assertLess(time.time() - started, 2)

    def test_worker_exception_is_propagated(self):
        import sync_feishu_bitable_openapi as feishu_sync

        def boom():
            raise ValueError("worker failed")

        with self.assertRaisesRegex(ValueError, "worker failed"):
            feishu_sync._run_sync_with_timeout(boom, 5)


class PlatformIdentityTests(unittest.TestCase):
    def test_update_identity_matches_running_platform(self):
        import update_manager

        if sys.platform == "win32":
            self.assertEqual(update_manager.UPDATE_PLATFORM, "win")
            self.assertEqual(update_manager.UPDATE_ARCH, "x86_64")
            self.assertEqual(
                update_manager.DEFAULT_PACKAGE_ID, "data-scientist-community-win-x64"
            )
        else:
            self.assertEqual(update_manager.UPDATE_PLATFORM, "mac")
            self.assertEqual(update_manager.UPDATE_ARCH, "arm64")
            self.assertEqual(
                update_manager.DEFAULT_PACKAGE_ID, "data-scientist-community-mac-arm64"
            )


class ProcessHelperTests(unittest.TestCase):
    def test_port_listener_pids_finds_bound_socket(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            pids = core_process.port_listener_pids(port)
            self.assertIn(os.getpid(), pids)
        finally:
            server.close()

    def test_process_command_for_own_pid_contains_python(self):
        command = core_process.process_command(os.getpid())
        if os.name == "nt":
            # Windows 的 CommandLine 查询应包含解释器路径
            self.assertIn("python", command.lower())
        else:
            self.assertTrue(command)


if __name__ == "__main__":
    unittest.main()
