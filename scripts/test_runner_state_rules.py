import importlib.util
import json
import os
import pathlib
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from orchestration.run_lease import RunLeaseStore
from orchestration.subprocess_supervisor import SupervisedResult
from orchestration.collection_scheduler import PlatformResult
from orchestration.run_artifacts import RunWorkspace


_IMPORT_RUNTIME = tempfile.TemporaryDirectory()
_PREVIOUS_STATE_DIR = os.environ.get("YIRENGONGIS_STATE_DIR")
os.environ["YIRENGONGIS_STATE_DIR"] = _IMPORT_RUNTIME.name
try:
    MODULE_PATH = pathlib.Path(__file__).with_name("runner.py")
    SPEC = importlib.util.spec_from_file_location("runner_module", MODULE_PATH)
    runner = importlib.util.module_from_spec(SPEC)
    assert SPEC and SPEC.loader
    SPEC.loader.exec_module(runner)
finally:
    if _PREVIOUS_STATE_DIR is None:
        os.environ.pop("YIRENGONGIS_STATE_DIR", None)
    else:
        os.environ["YIRENGONGIS_STATE_DIR"] = _PREVIOUS_STATE_DIR

PREPARE_V2_MODULE_PATH = pathlib.Path(__file__).with_name("prepare_feishu_bitable_sync_v2.py")
PREPARE_V2_SPEC = importlib.util.spec_from_file_location("prepare_feishu_bitable_sync_v2_module", PREPARE_V2_MODULE_PATH)
prepare_feishu_v2 = importlib.util.module_from_spec(PREPARE_V2_SPEC)
assert PREPARE_V2_SPEC and PREPARE_V2_SPEC.loader
PREPARE_V2_SPEC.loader.exec_module(prepare_feishu_v2)


def build_history_entry(*, run_at: str, feishu_attempted: bool, feishu_ok: bool, feishu_result=None):
    return runner._build_run_history_entry(
        raw_mode="run_all",
        requested_mode="incremental",
        min_date="2026-03-05",
        started_at=run_at,
        ended_at=run_at,
        duration=12.5,
        merge_ok=True,
        platform_snapshot={
            "platforms": [
                {
                    "platform": "douyin",
                    "label": "抖音",
                    "ui_status": "completed",
                    "message": "同步完成，共 6 条",
                    "last_sync_at": run_at,
                    "total_works": 6,
                    "success_works": 6,
                    "skipped_works": 0,
                    "failed_works": 0,
                    "auth_status": "authorized",
                    "auth_reason": "",
                    "auth_action": "none",
                    "needs_auth": False,
                }
            ]
        },
        feishu_attempted=feishu_attempted,
        feishu_ok=feishu_ok,
        feishu_result=feishu_result,
        feishu_error="" if feishu_ok else "同步到飞书多维表格失败：invalid param",
    )


@contextmanager
def temporary_runner_server(config_payload: dict, history_items=None):
    history_items = history_items or []
    with tempfile.TemporaryDirectory() as temp_dir:
        root = pathlib.Path(temp_dir)
        auth_dir = root / ".auth"
        downloads_dir = root / "downloads"
        auth_dir.mkdir(parents=True, exist_ok=True)
        downloads_dir.mkdir(parents=True, exist_ok=True)

        config_file = auth_dir / "customer_config.json"
        history_file = downloads_dir / "run_history.json"
        config = dict(runner.DEFAULT_CONFIG)
        config.update(config_payload)
        config_file.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        history_file.write_text(json.dumps(history_items, ensure_ascii=False), encoding="utf-8")

        with (
            patch.object(runner, "CONFIG_FILE", str(config_file)),
            patch.object(runner, "RUN_HISTORY_FILE", str(history_file)),
        ):
            server = runner.ThreadingHTTPServer(("127.0.0.1", 0), runner.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield server, history_file
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class ConfigSummaryTests(unittest.TestCase):
    def test_default_min_publish_date_is_2026_01_01(self):
        self.assertEqual(runner.DEFAULT_CONFIG["min_publish_date"], "2026-01-01")

    def test_load_saved_config_migrates_legacy_default_min_publish_date(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = pathlib.Path(temp_dir) / "customer_config.json"
            config_file.write_text(
                json.dumps({"min_publish_date": "2026-03-05"}, ensure_ascii=False),
                encoding="utf-8",
            )

            with patch.object(runner, "CONFIG_FILE", str(config_file)):
                config = runner.load_saved_config()

        self.assertEqual(config["min_publish_date"], "2026-01-01")

    def test_save_config_preserves_explicit_legacy_min_publish_date_once_user_customizes_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = pathlib.Path(temp_dir) / "customer_config.json"

            with patch.object(runner, "CONFIG_FILE", str(config_file)):
                saved = runner.save_config({"min_publish_date": "2026-03-05"})
                loaded = runner.load_saved_config()

            raw = json.loads(config_file.read_text(encoding="utf-8"))

        self.assertEqual(saved["min_publish_date"], "2026-03-05")
        self.assertEqual(loaded["min_publish_date"], "2026-03-05")
        self.assertTrue(raw.get("min_publish_date_customized"))

    def test_partial_config_update_preserves_unspecified_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = pathlib.Path(temp_dir) / "customer_config.json"
            config_file.write_text(
                json.dumps(
                    {
                        **runner._public_config_defaults(),
                        "workspace_name": "客户工作台",
                        "feishu_enabled": True,
                        "enabled_platforms": ["xiaohongshu"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch.object(runner, "CONFIG_FILE", str(config_file)):
                updated = runner.save_config({"enabled_platforms": ["douyin"]})

        self.assertEqual(updated["workspace_name"], "客户工作台")
        self.assertTrue(updated["feishu_enabled"])
        self.assertEqual(updated["enabled_platforms"], ["douyin"])

    def _base_config(self):
        return {
            "customer_name": "测试客户",
            "workspace_name": "本地数据工作台",
            "min_publish_date": "2026-03-05",
            "enabled_platforms": ["douyin"],
            "onboarding_completed": True,
            "feishu_enabled": False,
            "feishu_auto_sync": False,
        }

    def test_setup_complete_requires_authorized_enabled_platform(self):
        config = self._base_config()
        with (
            patch.object(runner, "_read_run_history", return_value=[]),
            patch.object(
                runner,
                "_resolved_auth_snapshot",
                return_value={"status": "unauthorized", "reason": "not_authorized"},
            ),
        ):
            summary = runner.config_summary(config)

        self.assertFalse(summary["setup_complete"])
        self.assertEqual(summary["authorized_platform_count"], 0)

    def test_setup_complete_is_true_after_any_enabled_platform_is_authorized(self):
        config = self._base_config()

        def fake_auth_snapshot(platform_id, _progress):
            if platform_id == "douyin":
                return {"status": "authorized", "reason": ""}
            return {"status": "unauthorized", "reason": "not_authorized"}

        with (
            patch.object(runner, "_read_run_history", return_value=[]),
            patch.object(runner, "_resolved_auth_snapshot", side_effect=fake_auth_snapshot),
        ):
            summary = runner.config_summary(config)

        self.assertTrue(summary["setup_complete"])
        self.assertEqual(summary["authorized_platform_count"], 1)


class BrowserResolutionTests(unittest.TestCase):
    def test_resolve_browser_executable_prefers_bundled_browser_on_macos(self):
        with (
            patch.object(runner, "PLAYWRIGHT_BROWSERS_DIR", "/tmp/playwright"),
            patch.object(runner, "_find_playwright_browser_executable", return_value="/tmp/playwright/chromium-1208/Google Chrome for Testing"),
            patch.object(runner, "_browser_channel_paths", return_value=["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]),
            patch.object(runner.sys, "platform", "darwin"),
        ):
            resolved = runner._resolve_browser_executable("chrome")

        self.assertEqual(resolved, "/tmp/playwright/chromium-1208/Google Chrome for Testing")


class RunnerSubprocessTests(unittest.TestCase):
    def test_supervision_env_removes_node_debug_hooks(self):
        scrubbed = runner._scrub_supervision_env(
            {
                "PATH": "/usr/bin",
                "NODE_OPTIONS": "--require inspector-hook.js",
                "NODE_INSPECT": "1",
                "NODE_DEBUG": "module",
                "VSCODE_INSPECTOR_OPTIONS": "{}",
                "NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S": "secret",
                "NODE_REPL_TRUSTED_CODE_PATHS": "/tmp/hook",
            }
        )

        self.assertEqual(scrubbed, {"PATH": "/usr/bin"})

    def test_platform_script_uses_activity_supervisor_and_run_log(self):
        handler = runner.Handler.__new__(runner.Handler)
        supervised_result = SupervisedResult(
            returncode=0,
            outcome="success",
            stdout_tail="done\n",
            stderr_tail="",
            duration_seconds=12.5,
        )

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(runner, "DOWNLOADS_DIR", temp_dir),
            patch.object(runner, "run_supervised", return_value=supervised_result) as supervisor,
            patch.object(runner._RUN_LEASE_STORE, "heartbeat", return_value=True),
        ):
            result = handler._run_platform_script(
                ["scripts/run_export.sh"],
                {"PATH": os.environ.get("PATH", "")},
                platform_id="douyin",
                progress_path=os.path.join(temp_dir, "douyin_progress.json"),
                run_id="run-1",
            )

        self.assertIs(result, supervised_result)
        kwargs = supervisor.call_args.kwargs
        self.assertEqual(kwargs["inactivity_timeout"], runner.PLATFORM_INACTIVITY_TIMEOUT)
        self.assertTrue(
            str(kwargs["log_path"]).replace("\\", "/").endswith("runs/run-1/douyin.log")
        )
        self.assertEqual(str(kwargs["progress_path"]), os.path.join(temp_dir, "douyin_progress.json"))

    def test_run_script_keeps_interactive_auth_in_user_session(self):
        handler = runner.Handler.__new__(runner.Handler)
        fake_proc = SimpleNamespace(returncode=0)

        def fake_communicate(timeout=None):
            return "", ""

        fake_proc.communicate = fake_communicate

        with patch.object(runner.subprocess, "Popen", return_value=fake_proc) as popen_mock:
            handler._run_script(
                ["scripts/run_export.sh"],
                {"PATH": os.environ.get("PATH", "")},
                requires_user_session=True,
            )

        popen_kwargs = popen_mock.call_args.kwargs
        self.assertNotIn("preexec_fn", popen_kwargs)

    def test_run_script_timeout_kills_only_child_when_user_session(self):
        """requires_user_session=True 的子进程超时时，只能 kill 子进程本身，
        不能 killpg——否则会连 runner 服务一起干掉（进程组未隔离）。"""
        handler = runner.Handler.__new__(runner.Handler)
        call_count = {"n": 0}

        fake_proc = SimpleNamespace(returncode=-9, pid=99999)

        def fake_communicate(timeout=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise runner.subprocess.TimeoutExpired(cmd=["x"], timeout=1)
            return "stdout", "stderr"

        fake_proc.communicate = fake_communicate
        fake_proc.kill = MagicMock()

        with (
            patch.object(runner.subprocess, "Popen", return_value=fake_proc),
            patch.object(runner.os, "killpg", new=MagicMock(), create=True) as killpg_mock,
            patch.object(runner.os, "getpgid", new=MagicMock(), create=True),
            patch.object(runner.os, "name", "posix"),
        ):
            handler._run_script(
                ["scripts/seed_browser_profile.mjs"],
                {"PATH": os.environ.get("PATH", "")},
                requires_user_session=True,
            )

        # 子进程被 kill
        fake_proc.kill.assert_called()
        # 绝不能调用 killpg（那会误杀 runner 自己的进程组）
        killpg_mock.assert_not_called()

    def test_run_script_timeout_kills_process_group_for_batch_scripts(self):
        """requires_user_session=False 的批处理脚本超时时，仍应 killpg 清理整个进程组。"""
        handler = runner.Handler.__new__(runner.Handler)
        call_count = {"n": 0}

        fake_proc = SimpleNamespace(returncode=-9, pid=99999)

        def fake_communicate(timeout=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise runner.subprocess.TimeoutExpired(cmd=["x"], timeout=1)
            return "stdout", "stderr"

        fake_proc.communicate = fake_communicate
        fake_proc.kill = MagicMock()

        killpg_mock = MagicMock()
        with (
            patch.object(runner.subprocess, "Popen", return_value=fake_proc),
            patch.object(runner.os, "killpg", new=killpg_mock, create=True),
            patch.object(runner.os, "getpgid", return_value=99999, create=True),
            patch.object(runner.os, "setsid", new=MagicMock(), create=True),
            patch.object(runner.os, "name", "posix"),
        ):
            handler._run_script(
                ["scripts/run_export.sh"],
                {"PATH": os.environ.get("PATH", "")},
            )

        # 批处理脚本走 setsid 独立进程组，超时必须 killpg
        killpg_mock.assert_called()

    def test_single_platform_failure_history_marks_merge_failed(self):
        """单平台采集失败/合并失败早退时写入的历史记录必须反映失败状态，
        这样用户才能在历史列表里看到这次失败（与 run_all 行为一致）。"""
        # 模拟采集失败：_platform_history_snapshot 读出的平台是 failed 状态
        platform_snapshot = {
            "platforms": [
                {
                    "platform": "douyin",
                    "label": "抖音",
                    "ui_status": "failed",
                    "message": "同步失败",
                    "last_sync_at": None,
                    "total_works": 0,
                    "success_works": 0,
                    "skipped_works": 0,
                    "failed_works": 0,
                    "auth_status": "authorized",
                    "auth_reason": "",
                    "auth_action": "",
                    "status": "failed",
                    "needs_auth": False,
                }
            ],
            "successful_platforms": 0,
            "failed_platforms": 1,
            "empty_platforms": 0,
        }
        history_entry = runner._build_run_history_entry(
            raw_mode="single_platform",
            requested_mode="single_platform",
            min_date="2026-01-01",
            max_date="",
            started_at="2026-06-25T10:00:00+0800",
            ended_at="2026-06-25T10:01:00+0800",
            duration=60.0,
            merge_ok=False,
            platform_snapshot=platform_snapshot,
            feishu_attempted=False,
            feishu_ok=False,
        )
        # merge_ok=False 必须产出失败状态，而不是 completed
        self.assertFalse(history_entry["ok"])
        self.assertFalse(history_entry["merge_ok"])
        self.assertEqual(history_entry["status"], "failed")
        self.assertEqual(history_entry["failed_stage"], "platform_scraping")
        self.assertEqual(history_entry["raw_mode"], "single_platform")


class LockHeartbeatTests(unittest.TestCase):
    """验证 auth_all 锁心跳：长任务期间每平台前刷新锁时间戳，避免被
    LOCK_STALE_SECONDS(30分钟) 误判过期。"""

    def test_write_lock_file_renews_timestamp_so_lock_stays_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = os.path.join(tmp, "runner.lock")
            # 模拟一个 25 分钟前写入的锁（即将过期）
            stale_ts = time.time() - 25 * 60
            with open(lock_path, "w", encoding="utf-8") as f:
                f.write(f"{stale_ts} {os.getpid()}")

            # 心跳：刷新锁时间戳
            runner._write_lock_file(lock_path)

            # 刷新后锁应当判定为 active（即使再过很久才检查，时间戳也是新的）
            self.assertTrue(runner._is_lock_file_active(lock_path))

    def test_live_pid_lock_does_not_expire_only_because_timestamp_is_old(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = os.path.join(tmp, "runner.lock")
            stale_ts = time.time() - (runner.LOCK_STALE_SECONDS + 60)
            with open(lock_path, "w", encoding="utf-8") as f:
                f.write(f"{stale_ts} {os.getpid()}")

            self.assertTrue(runner._is_lock_file_active(lock_path))
            self.assertTrue(os.path.exists(lock_path))

    def test_global_lease_release_requires_matching_run_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunLeaseStore(
                os.path.join(tmp, "runner.lock"),
                ttl_seconds=10,
                pid_alive=lambda _pid: True,
            )
            with patch.object(runner, "_RUN_LEASE_STORE", store):
                token = runner.lock(kind="run_all")
                self.assertIsNotNone(token)
                self.assertTrue(runner.is_locked())
                self.assertFalse(runner.unlock("wrong"))
                self.assertTrue(runner.is_locked())
                self.assertTrue(runner.unlock(token.run_id))
                self.assertFalse(runner.is_locked())


class RunnerTargetResolutionTests(unittest.TestCase):
    def test_missing_platforms_uses_only_enabled_config(self):
        resolved = runner.resolve_requested_targets({}, ["douyin"])

        self.assertEqual(resolved, ["douyin"])

    def test_explicit_invalid_platform_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid_platform:wechat"):
            runner.resolve_requested_targets({"platforms": ["wechat"]}, ["douyin"])

    def test_explicit_empty_selection_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no_platform_selected"):
            runner.resolve_requested_targets({"platforms": [""]}, ["douyin"])

    def test_duplicates_are_removed_in_supported_platform_order(self):
        resolved = runner.resolve_requested_targets(
            {"platforms": ["kuaishou,douyin", "douyin,bilibili"]},
            ["xiaohongshu"],
        )

        self.assertEqual(resolved, ["douyin", "bilibili", "kuaishou"])


class RunnerParallelIntegrationTests(unittest.TestCase):
    def test_run_all_platforms_delegate_to_two_worker_scheduler(self):
        handler = runner.Handler.__new__(runner.Handler)
        steps = [
            ("douyin", "/tmp/douyin.sh", "/tmp/douyin.json", False),
            ("bilibili", "/tmp/bilibili.sh", "/tmp/bilibili.json", False),
        ]
        scheduled = {
            "douyin": PlatformResult("douyin", "success", fresh_output=True),
            "bilibili": PlatformResult("bilibili", "success", fresh_output=True),
        }

        with patch.object(runner, "run_bounded", return_value=scheduled) as bounded:
            results = handler._run_platform_steps_bounded(steps, {}, "run-1")

        self.assertEqual(results, scheduled)
        self.assertEqual(bounded.call_args.kwargs["max_workers"], 2)
        self.assertEqual(bounded.call_args.args[0], ["douyin", "bilibili"])

    def test_auth_state_writes_are_serialized(self):
        active = 0
        max_active = 0
        counter_lock = threading.Lock()

        def slow_write(_path, _payload):
            nonlocal active, max_active
            with counter_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.04)
            with counter_lock:
                active -= 1

        with (
            patch.object(runner, "load_auth_state", return_value={}),
            patch.object(runner, "_write_json_file_atomically", side_effect=slow_write),
        ):
            threads = [
                threading.Thread(
                    target=runner.save_auth_state,
                    args=({platform: {"status": "authorized"}},),
                )
                for platform in ("douyin", "bilibili")
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

        self.assertEqual(max_active, 1)

    def test_auth_all_prewarms_all_profiles_before_first_ui_window(self):
        handler = runner.Handler.__new__(runner.Handler)
        barrier = threading.Barrier(2)
        prewarm_started = []
        ui_started = []

        def fake_prewarm(platform_id, _browser_channel, _progress_path):
            prewarm_started.append(platform_id)
            barrier.wait(timeout=2)
            return True, ""

        def fake_run(platform_id, _script, _query, _progress_path, **_kwargs):
            ui_started.append(platform_id)
            self.assertEqual(set(prewarm_started), {"douyin", "bilibili"})
            return True, "", ""

        handler._prepare_auth_profile_for_ui = fake_prewarm
        handler._run_auth_process_with_profile_recovery = fake_run
        handler._stabilize_auth_only_result = lambda _platform, _script, _progress, _query, **kwargs: (
            kwargs["ok"], kwargs.get("stdout", ""), kwargs.get("stderr", "")
        )
        handler._append_log = lambda *_args, **_kwargs: None

        steps = [
            ("douyin", "/tmp/douyin.sh", "/tmp/douyin.json"),
            ("bilibili", "/tmp/bilibili.sh", "/tmp/bilibili.json"),
        ]
        with (
            patch.object(handler, "_resolve_auth_all_steps", return_value=steps),
            patch.object(runner.os.path, "exists", return_value=True),
            patch.object(runner, "_persisted_auth_snapshot", return_value={"status": "unauthorized"}),
            patch.object(runner, "_prime_platform_progress"),
            patch.object(runner, "_finalize_platform_progress"),
            patch.object(runner, "_sync_platform_auth_state_from_progress"),
            patch.object(runner, "read_json_file", side_effect=lambda _path, default: default),
            patch.object(runner, "load_saved_config", return_value={"enabled_platforms": ["douyin", "bilibili"]}),
            patch.object(runner._RUN_LEASE_STORE, "heartbeat"),
        ):
            result = handler._execute_auth_all({}, start=time.time(), lease_run_id="lease-1")

        self.assertTrue(result["ok"])
        self.assertEqual(ui_started, ["douyin", "bilibili"])

    def test_runner_log_blocks_are_serialized(self):
        active = 0
        max_active = 0
        counter_lock = threading.Lock()

        class SlowFile:
            def __enter__(self):
                nonlocal active, max_active
                with counter_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.04)
                return self

            def __exit__(self, exc_type, exc, tb):
                nonlocal active
                with counter_lock:
                    active -= 1

            def write(self, _value):
                return None

        with (
            patch.object(runner, "_ensure_parent_dir"),
            patch.object(runner.os.path, "exists", return_value=False),
            patch.object(runner, "open", side_effect=lambda *_args, **_kwargs: SlowFile()),
        ):
            threads = [
                threading.Thread(target=runner._append_log, args=(f"RUN_{index}", "out", "err"))
                for index in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

        self.assertEqual(max_active, 1)


class RunnerArtifactIntegrationTests(unittest.TestCase):
    def test_bilibili_contract_uses_run_workspace_outputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = RunWorkspace(temp_dir, "run-1", "bilibili")

            env, mapping = runner._platform_artifact_contract("bilibili", workspace)

        self.assertTrue(
            env["BILI_OUTPUT_PATH"].replace("\\", "/").endswith(
                "runs/run-1/bilibili/bilibili_all_videos.xlsx"
            )
        )
        self.assertTrue(
            env["BILI_TEMP_ROWS_PATH"].replace("\\", "/").endswith(
                "runs/run-1/bilibili/bilibili_rows.json"
            )
        )
        self.assertEqual(pathlib.Path(env["BILI_OUTPUT_PATH"]), next(iter(mapping)))
        self.assertEqual(next(iter(mapping.values())).name, "bilibili_all_videos.xlsx")

    def test_douyin_workspace_is_seeded_with_previous_checkpoint_and_work_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            downloads = pathlib.Path(temp_dir)
            (downloads / "processed_ids.json").write_text('{"ids":["1"]}', encoding="utf-8")
            (downloads / "merged-2026-01-01-1.xlsx").write_bytes(b"old-work")
            workspace = RunWorkspace(downloads, "run-1", "douyin")

            runner._seed_douyin_workspace(workspace, downloads)

            self.assertEqual(
                (workspace.root / "processed_ids.json").read_text(encoding="utf-8"),
                '{"ids":["1"]}',
            )
            self.assertEqual(
                (workspace.root / "merged-2026-01-01-1.xlsx").read_bytes(),
                b"old-work",
            )

    def test_successful_platform_step_promotes_staged_artifacts(self):
        handler = runner.Handler.__new__(runner.Handler)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            script = root / "run_bili.sh"
            script.write_text("#!/bin/bash\n", encoding="utf-8")
            progress = root / "bilibili_progress.json"
            official_xlsx = root / "bilibili_all_videos.xlsx"
            official_rows = root / "bilibili_rows.json"

            def fake_platform_run(_command, env, **_kwargs):
                pathlib.Path(env["BILI_OUTPUT_PATH"]).write_bytes(b"fresh-xlsx")
                pathlib.Path(env["BILI_TEMP_ROWS_PATH"]).write_text("[]", encoding="utf-8")
                return SupervisedResult(0, "success", "ok", "", 0.1)

            handler._build_env = lambda *_args, **_kwargs: {}
            handler._run_platform_script = fake_platform_run
            handler._get_proc_output = lambda proc: (proc.stdout_text, proc.stderr_text)
            handler._append_log = lambda *_args: None

            with (
                patch.object(runner, "DOWNLOADS_DIR", temp_dir),
                patch.object(runner, "BILI_DATA_FILE", str(official_xlsx)),
                patch.object(runner, "_preflight_platform_run", return_value={"blocked": False}),
                patch.object(runner, "_prime_platform_progress"),
                patch.object(runner, "_finalize_platform_progress"),
                patch.object(runner, "_sync_platform_auth_state_from_progress"),
            ):
                result = handler._execute_run_all_platform_step(
                    ("bilibili", str(script), str(progress), False),
                    {},
                    "run-1",
                )

            self.assertEqual(result.outcome, "success")
            self.assertTrue(result.fresh_output)
            self.assertEqual(official_xlsx.read_bytes(), b"fresh-xlsx")
            self.assertEqual(official_rows.read_text(encoding="utf-8"), "[]")

    def test_douyin_partial_failure_promotes_valid_staged_artifacts_without_retry(self):
        handler = runner.Handler.__new__(runner.Handler)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            script = root / "run_douyin.sh"
            script.write_text("#!/bin/bash\n", encoding="utf-8")
            progress = root / "douyin_progress.json"
            official_xlsx = root / "all_videos.xlsx"

            def fake_platform_run(_command, env, **_kwargs):
                pathlib.Path(env["MASTER_PATH"]).write_bytes(b"fresh-partial-xlsx")
                pathlib.Path(env["SUMMARY_PATH"]).write_text(
                    "work_id,title,publish_date,merged_file,raw_files\n1,title,2026-08-17,merged-1.xlsx,\n",
                    encoding="utf-8",
                )
                pathlib.Path(env["STATE_PATH"]).write_text(
                    json.dumps({"processed_ids": ["1"]}),
                    encoding="utf-8",
                )
                pathlib.Path(env["DOWNLOAD_DIR"], "merged-1.xlsx").write_bytes(b"fresh-work")
                progress.write_text(
                    json.dumps(
                        {
                            "status": "failed",
                            "phase": "failed",
                            "error": "partial_failure",
                            "totalWorks": 28,
                            "processedWorks": 23,
                            "queuedWorks": 5,
                            "successWorks": 21,
                            "failedWorks": 2,
                            "finishedAt": "2026-08-17T18:20:00+0800",
                        }
                    ),
                    encoding="utf-8",
                )
                return SupervisedResult(1, "failed", "", "partial_failure", 0.1)

            handler._build_env = lambda *_args, **_kwargs: {}
            handler._run_platform_script = fake_platform_run
            handler._get_proc_output = lambda proc: (proc.stdout_text, proc.stderr_text)
            handler._append_log = lambda *_args: None

            with (
                patch.object(runner, "DOWNLOADS_DIR", temp_dir),
                patch.object(runner, "DATA_FILE", str(official_xlsx)),
                patch.object(runner, "_preflight_platform_run", return_value={"blocked": False}),
                patch.object(runner, "_prime_platform_progress"),
                patch.object(runner, "_finalize_platform_progress"),
                patch.object(runner, "_sync_platform_auth_state_from_progress"),
            ):
                result = handler._execute_run_all_platform_step(
                    ("douyin", str(script), str(progress), False),
                    {},
                    "run-partial",
                )

            self.assertEqual(result.outcome, "partial_failure")
            self.assertTrue(result.fresh_output)
            self.assertFalse(result.retryable)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(official_xlsx.read_bytes(), b"fresh-partial-xlsx")
            self.assertTrue((root / "merged-1.xlsx").is_file())
            self.assertEqual(
                json.loads((root / "processed_ids.json").read_text(encoding="utf-8")),
                {"processed_ids": ["1"]},
            )

    def test_douyin_zero_success_failure_does_not_promote_and_remains_retryable(self):
        handler = runner.Handler.__new__(runner.Handler)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            script = root / "run_douyin.sh"
            script.write_text("#!/bin/bash\n", encoding="utf-8")
            progress = root / "douyin_progress.json"
            official_xlsx = root / "all_videos.xlsx"
            official_xlsx.write_bytes(b"previous-xlsx")

            def fake_platform_run(_command, env, **_kwargs):
                pathlib.Path(env["MASTER_PATH"]).write_bytes(b"invalid-zero-success-candidate")
                pathlib.Path(env["SUMMARY_PATH"]).write_text(
                    "work_id,title,publish_date,merged_file,raw_files\n",
                    encoding="utf-8",
                )
                pathlib.Path(env["STATE_PATH"]).write_text(
                    json.dumps({"processed_ids": []}),
                    encoding="utf-8",
                )
                progress.write_text(
                    json.dumps(
                        {
                            "status": "failed",
                            "phase": "failed",
                            "error": "partial_failure",
                            "totalWorks": 3,
                            "processedWorks": 3,
                            "queuedWorks": 0,
                            "successWorks": 0,
                            "failedWorks": 3,
                            "finishedAt": "2026-08-17T18:20:00+0800",
                        }
                    ),
                    encoding="utf-8",
                )
                return SupervisedResult(1, "failed", "", "all_candidates_failed", 0.1)

            handler._build_env = lambda *_args, **_kwargs: {}
            handler._run_platform_script = fake_platform_run
            handler._get_proc_output = lambda proc: (proc.stdout_text, proc.stderr_text)
            handler._append_log = lambda *_args: None

            with (
                patch.object(runner, "DOWNLOADS_DIR", temp_dir),
                patch.object(runner, "DATA_FILE", str(official_xlsx)),
                patch.object(runner, "_preflight_platform_run", return_value={"blocked": False}),
                patch.object(runner, "_prime_platform_progress"),
                patch.object(runner, "_finalize_platform_progress"),
                patch.object(runner, "_sync_platform_auth_state_from_progress"),
            ):
                result = handler._execute_run_all_platform_step(
                    ("douyin", str(script), str(progress), False),
                    {},
                    "run-zero-success",
                )

            self.assertEqual(result.outcome, "failed")
            self.assertFalse(result.fresh_output)
            self.assertTrue(result.retryable)
            self.assertEqual(official_xlsx.read_bytes(), b"previous-xlsx")

    def test_douyin_partial_promotion_error_preserves_partial_result_without_retry(self):
        handler = runner.Handler.__new__(runner.Handler)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            script = root / "run_douyin.sh"
            script.write_text("#!/bin/bash\n", encoding="utf-8")
            progress = root / "douyin_progress.json"
            official_xlsx = root / "all_videos.xlsx"
            official_xlsx.write_bytes(b"previous-xlsx")

            partial_progress = {
                "status": "failed",
                "phase": "failed",
                "message": "成功 4 条，仍有 2 条待重试",
                "error": "partial_failure",
                "totalWorks": 8,
                "processedWorks": 6,
                "queuedWorks": 2,
                "successWorks": 4,
                "failedWorks": 2,
                "finishedAt": "2026-08-17T18:20:00+0800",
            }

            def fake_platform_run(_command, env, **_kwargs):
                # Intentionally omit MASTER_PATH so promotion validation fails.
                pathlib.Path(env["SUMMARY_PATH"]).write_text(
                    "work_id,title,publish_date,merged_file,raw_files\n",
                    encoding="utf-8",
                )
                pathlib.Path(env["STATE_PATH"]).write_text(
                    json.dumps({"processed_ids": ["1", "2", "3", "4"]}),
                    encoding="utf-8",
                )
                progress.write_text(json.dumps(partial_progress), encoding="utf-8")
                return SupervisedResult(1, "failed", "", "partial_failure", 0.1)

            handler._build_env = lambda *_args, **_kwargs: {}
            handler._run_platform_script = fake_platform_run
            handler._get_proc_output = lambda proc: (proc.stdout_text, proc.stderr_text)
            handler._append_log = lambda *_args: None

            with (
                patch.object(runner, "DOWNLOADS_DIR", temp_dir),
                patch.object(runner, "DATA_FILE", str(official_xlsx)),
                patch.object(runner, "_preflight_platform_run", return_value={"blocked": False}),
                patch.object(runner, "_prime_platform_progress"),
                patch.object(runner, "_sync_platform_auth_state_from_progress"),
            ):
                result = handler._execute_run_all_platform_step(
                    ("douyin", str(script), str(progress), False),
                    {},
                    "run-partial-invalid",
                )

            persisted = json.loads(progress.read_text(encoding="utf-8"))
            self.assertEqual(result.outcome, "partial_failure")
            self.assertFalse(result.fresh_output)
            self.assertFalse(result.retryable)
            self.assertIn("partial_artifact_promotion_failed", result.error_message)
            self.assertIn("missing_artifact:douyin:all_videos.xlsx", result.error_message)
            self.assertEqual(persisted["successWorks"], 4)
            self.assertEqual(persisted["queuedWorks"], 2)
            self.assertEqual(persisted["error"], "partial_failure")
            self.assertEqual(official_xlsx.read_bytes(), b"previous-xlsx")

    def test_empty_success_publishes_empty_artifact_and_reports_completed_empty(self):
        handler = runner.Handler.__new__(runner.Handler)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            script = root / "run_douyin.sh"
            script.write_text("#!/bin/bash\n", encoding="utf-8")
            progress = root / "douyin_progress.json"
            official_xlsx = root / "all_videos.xlsx"
            runner.pd.DataFrame([{"作品ID": "stale-1", "标题": "旧数据"}]).to_excel(
                official_xlsx,
                index=False,
            )
            progress.write_text(
                json.dumps(
                    {
                        "status": "completed",
                        "totalWorks": 0,
                        "successWorks": 0,
                        "failedWorks": 0,
                    }
                ),
                encoding="utf-8",
            )
            handler._build_env = lambda *_args, **_kwargs: {}
            handler._run_platform_script = lambda *_args, **_kwargs: SupervisedResult(
                0, "success", "", "", 0.1
            )
            handler._get_proc_output = lambda proc: (proc.stdout_text, proc.stderr_text)
            handler._append_log = lambda *_args: None

            with (
                patch.object(runner, "DOWNLOADS_DIR", temp_dir),
                patch.object(runner, "DATA_FILE", str(official_xlsx)),
                patch.object(runner, "_preflight_platform_run", return_value={"blocked": False}),
                patch.object(runner, "_prime_platform_progress"),
                patch.object(runner, "_finalize_platform_progress"),
                patch.object(runner, "_sync_platform_auth_state_from_progress"),
            ):
                result = handler._execute_run_all_platform_step(
                    ("douyin", str(script), str(progress), False),
                    {},
                    "run-1",
                )

            self.assertEqual(result.outcome, "completed_empty")
            self.assertTrue(result.fresh_output)
            self.assertTrue(runner.pd.read_excel(official_xlsx).empty)


class RunnerRunAllSafetyTests(unittest.TestCase):
    def test_run_all_primes_waiting_platforms_as_queued(self):
        handler = runner.Handler.__new__(runner.Handler)
        targets = [
            ("douyin", "/tmp/douyin-progress.json"),
            ("bilibili", "/tmp/bili-progress.json"),
        ]

        with (
            patch.object(runner, "_resolve_requested_run_targets", return_value=targets),
            patch.object(runner, "_prime_platform_progress") as prime,
        ):
            platform_ids = handler._prime_run_all_targets({})

        self.assertEqual(platform_ids, ["douyin", "bilibili"])
        self.assertEqual(prime.call_count, 2)
        for call in prime.call_args_list:
            self.assertEqual(call.kwargs["phase"], "queued")
            self.assertEqual(call.kwargs["message"], "等待采集槽位")

    def test_feishu_exception_is_recorded_as_attempted_failure(self):
        handler = runner.Handler.__new__(runner.Handler)
        handler._append_log = lambda *_args: None
        handler._run_script = lambda *_args, **_kwargs: SimpleNamespace(returncode=0)
        handler._get_proc_output = lambda _proc: ("", "")
        handler._run_feishu_sync = MagicMock(side_effect=RuntimeError("飞书字段创建未完成：平台明细V2 缺失字段 3s跳出率"))
        steps = [("douyin", "/tmp/douyin.sh", "/tmp/douyin.json", False)]
        scheduled = {
            "douyin": PlatformResult(
                "douyin",
                "success",
                fresh_output=True,
                started=True,
            ),
        }
        snapshot = {
            "platforms": [
                {
                    "platform": "douyin",
                    "label": "抖音",
                    "ui_status": "completed",
                    "status": "success",
                    "auth_status": "authorized",
                    "success_works": 1,
                }
            ]
        }
        config = {
            "min_publish_date": "2026-01-01",
            "feishu_enabled": True,
            "feishu_auto_sync": True,
        }

        with (
            patch.object(runner, "load_saved_config", return_value=config),
            patch.object(runner, "feishu_config_ready", return_value=True),
            patch.object(handler, "_run_all_platform_steps", return_value=steps),
            patch.object(handler, "_run_platform_steps_bounded", return_value=scheduled),
            patch.object(runner, "_platform_history_snapshot", return_value=snapshot),
            patch.object(runner, "_append_run_history_entry"),
        ):
            payload = handler._execute_run_all(
                {},
                start=time.time(),
                started_at="2026-07-10 10:00:00",
                run_id="run-1",
            )

        self.assertTrue(payload["feishu_sync_attempted"])
        self.assertFalse(payload["feishu_sync_ok"])
        self.assertEqual(payload["status"], "partial_failed")
        self.assertEqual(payload["failed_stage"], "feishu_importing")
        self.assertIn("3s跳出率", payload["feishu"]["detail"])

    def test_partial_platform_failure_blocks_automatic_feishu_sync(self):
        handler = runner.Handler.__new__(runner.Handler)
        handler._append_log = lambda *_args: None
        handler._run_script = lambda *_args, **_kwargs: SimpleNamespace(returncode=0)
        handler._get_proc_output = lambda _proc: ("", "")
        handler._run_feishu_sync = MagicMock(
            return_value={"ok": True, "attempted": True, "prepare": {}, "sync": {}}
        )
        steps = [
            ("douyin", "/tmp/douyin.sh", "/tmp/douyin.json", False),
            ("xiaohongshu", "/tmp/xhs.sh", "/tmp/xhs.json", True),
        ]
        scheduled = {
            "douyin": PlatformResult(
                "douyin",
                "success",
                fresh_output=True,
                started=True,
            ),
            "xiaohongshu": PlatformResult(
                "xiaohongshu",
                "failed",
                retryable=False,
                returncode=1,
                started=True,
            ),
        }
        snapshot = {
            "platforms": [
                {
                    "platform": "douyin",
                    "label": "抖音",
                    "ui_status": "completed",
                    "status": "success",
                    "auth_status": "authorized",
                    "success_works": 1,
                },
                {
                    "platform": "xiaohongshu",
                    "label": "小红书",
                    "ui_status": "failed",
                    "status": "failed",
                    "auth_status": "authorized",
                    "failed_works": 1,
                },
            ]
        }
        config = {
            "min_publish_date": "2026-01-01",
            "feishu_enabled": True,
            "feishu_auto_sync": True,
        }

        with (
            patch.object(runner, "load_saved_config", return_value=config),
            patch.object(runner, "feishu_config_ready", return_value=True),
            patch.object(handler, "_run_all_platform_steps", return_value=steps),
            patch.object(handler, "_run_platform_steps_bounded", return_value=scheduled),
            patch.object(runner, "_platform_history_snapshot", return_value=snapshot),
            patch.object(runner, "_append_run_history_entry"),
        ):
            payload = handler._execute_run_all(
                {},
                start=time.time(),
                started_at="2026-07-10 10:00:00",
                run_id="run-1",
            )

        handler._run_feishu_sync.assert_not_called()
        self.assertFalse(payload["merge_ok"])
        self.assertEqual(payload["status"], "partial_failed")
        self.assertFalse(payload["feishu_sync_attempted"])
        self.assertIn("未全部成功", payload["feishu"]["summary"])

    def test_fresh_douyin_partial_output_still_blocks_global_merge_and_feishu(self):
        handler = runner.Handler.__new__(runner.Handler)
        handler._append_log = lambda *_args: None
        handler._run_script = MagicMock()
        handler._get_proc_output = lambda _proc: ("", "")
        handler._run_feishu_sync = MagicMock()
        steps = [("douyin", "/tmp/douyin.sh", "/tmp/douyin.json", False)]
        scheduled = {
            "douyin": PlatformResult(
                "douyin",
                "partial_failure",
                retryable=False,
                returncode=1,
                fresh_output=True,
                started=True,
            ),
        }
        snapshot = {
            "platforms": [
                {
                    "platform": "douyin",
                    "label": "抖音",
                    "ui_status": "failed",
                    "status": "failed",
                    "auth_status": "authorized",
                    "total_works": 28,
                    "success_works": 21,
                    "failed_works": 2,
                }
            ]
        }
        config = {
            "min_publish_date": "2026-01-01",
            "feishu_enabled": True,
            "feishu_auto_sync": True,
        }

        with (
            patch.object(runner, "load_saved_config", return_value=config),
            patch.object(runner, "feishu_config_ready", return_value=True),
            patch.object(handler, "_run_all_platform_steps", return_value=steps),
            patch.object(handler, "_run_platform_steps_bounded", return_value=scheduled),
            patch.object(runner, "_platform_history_snapshot", return_value=snapshot),
            patch.object(runner, "_append_run_history_entry"),
        ):
            payload = handler._execute_run_all(
                {},
                start=time.time(),
                started_at="2026-08-17 18:00:00",
                run_id="run-partial",
            )

        handler._run_script.assert_not_called()
        handler._run_feishu_sync.assert_not_called()
        self.assertFalse(payload["douyin_ok"])
        self.assertFalse(payload["merge_ok"])
        self.assertEqual(payload["status"], "failed")
        self.assertFalse(payload["feishu_sync_attempted"])
        self.assertIn("未全部成功", payload["feishu"]["summary"])


class ProfileSeedUsableTests(unittest.TestCase):
    """验证预热后存活检查：区分"Chromium 真正初始化"和"假成功（退出码0但没写入）"，
    后者在打包环境下会导致真实授权窗口秒退。"""

    def test_empty_profile_dir_is_not_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = os.path.join(tmp, "profile")
            os.makedirs(empty)
            self.assertFalse(runner._profile_seed_is_usable(empty))

    def test_profile_with_local_state_is_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "profile")
            os.makedirs(d)
            open(os.path.join(d, "Local State"), "w").close()
            self.assertTrue(runner._profile_seed_is_usable(d))

    def test_profile_with_default_preferences_is_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "profile")
            os.makedirs(os.path.join(d, "Default"))
            open(os.path.join(d, "Default", "Preferences"), "w").close()
            self.assertTrue(runner._profile_seed_is_usable(d))

    def test_profile_with_only_irrelevant_files_is_not_usable(self):
        """退出码0但 Chromium 没真正写标志文件（只有无关文件）→ 判定不可用，触发重试。"""
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "profile")
            os.makedirs(d)
            open(os.path.join(d, "random.txt"), "w").close()
            self.assertFalse(runner._profile_seed_is_usable(d))


class SinglePlatformExceptionHistoryTests(unittest.TestCase):
    def test_unexpected_single_platform_exception_writes_failed_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            history_path = pathlib.Path(temp_dir) / "run_history.json"
            handler = runner.Handler.__new__(runner.Handler)
            handler.path = "/run?min_date=2026-07-01"
            responses = []
            handler._require_request_security = lambda: True
            handler._blocked_run_request_payload = lambda path, query: None
            handler._append_log = lambda *args, **kwargs: None
            handler._send_json = lambda status, payload: responses.append((status, payload))
            handler._execute_run_all_platform_step = MagicMock(
                side_effect=RuntimeError("browser_crashed")
            )

            with (
                patch.object(runner, "RUN_HISTORY_FILE", str(history_path)),
                patch.object(runner, "active_auth_locks", return_value=[]),
                patch.object(runner, "is_locked", return_value=False),
                patch.object(
                    runner,
                    "lock",
                    return_value=SimpleNamespace(run_id="single-run-1"),
                ),
                patch.object(runner, "unlock"),
                patch.object(
                    runner,
                    "_preflight_platform_run",
                    return_value={"blocked": False},
                ),
                patch.object(
                    runner,
                    "_platform_history_snapshot",
                    return_value={
                        "platforms": [
                            {
                                "platform": "douyin",
                                "label": "抖音",
                                "status": "running",
                                "ui_status": "running",
                                "message": "采集中",
                            }
                        ]
                    },
                ),
            ):
                handler.do_POST()

            history = json.loads(history_path.read_text(encoding="utf-8"))

        self.assertEqual(responses[-1][0], 500)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "failed")
        self.assertEqual(history[0]["failed_stage"], "platform_scraping")
        self.assertIn("browser_crashed", history[0]["error"])


class FeishuRuntimeTests(unittest.TestCase):
    def test_lark_cli_status_reuses_short_lived_result(self):
        runner._invalidate_lark_cli_caches()
        payload = {"identities": {"user": {"available": True, "userName": "tester"}}}
        with patch.object(
            runner,
            "_run_lark_cli_raw",
            return_value=(json.dumps(payload), "", 0),
        ) as run_cli:
            first = runner._lark_cli_status(use_global=False)
            second = runner._lark_cli_status(use_global=False)

        runner._invalidate_lark_cli_caches()
        self.assertEqual(first, payload)
        self.assertEqual(second, payload)
        run_cli.assert_called_once()

    def test_pending_metric_snapshot_is_committed_only_after_explicit_commit(self):
        rows = [
            {
                "平台作品键": "douyin:1",
                "作品组ID": "WORK-0001",
                "平台": "douyin",
                "作品ID": "1",
                "标题": "作品A",
                "发布日期": "2026-07-01",
                "内容类型": "video",
                "播放量": 10,
                "点赞量": 2,
                "收藏量": 1,
                "评论量": 1,
                "分享量": 1,
                "涨粉量": 0,
            }
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            pending_path = pathlib.Path(temp_dir) / "pending.json"
            trend_db = pathlib.Path(temp_dir) / "trend.db"
            with patch.object(prepare_feishu_v2, "archive_metric_snapshot") as archive:
                prepare_feishu_v2.write_pending_metric_snapshot(
                    pending_path,
                    batch_id="batch-1",
                    generated_at="2026-07-10 10:00:00",
                    min_date="2026-01-01",
                    metric_rows=rows,
                )
                archive.assert_not_called()
                self.assertTrue(pending_path.exists())

                prepare_feishu_v2.commit_pending_metric_snapshot(pending_path, trend_db)

            archive.assert_called_once()
            self.assertFalse(pending_path.exists())

    def test_ui_status_treats_reused_results_as_no_new_data(self):
        ui_status = runner._ui_status(
            "douyin",
            {
                "status": "completed",
                "phase": "done",
                "totalWorks": 6,
                "processedWorks": 6,
                "successWorks": 0,
                "skippedWorks": 6,
                "failedWorks": 0,
                "auth_status": "authorized",
                "needs_auth": False,
            },
        )

        self.assertEqual(ui_status, "completed_empty")

    def test_ui_status_distinguishes_queued_from_running(self):
        with patch.object(runner, "is_locked", return_value=True):
            ui_status = runner._ui_status(
                "bilibili",
                {
                    "status": "running",
                    "phase": "queued",
                    "message": "等待采集槽位",
                    "auth_status": "authorized",
                },
            )

        self.assertEqual(ui_status, "queued")

    def test_ui_status_prioritizes_auth_required_over_completed_when_auth_is_invalid(self):
        ui_status = runner._ui_status(
            "xiaohongshu",
            {
                "status": "completed",
                "phase": "done",
                "message": "小红书登录完成（AUTH_ONLY）",
                "totalWorks": 0,
                "processedWorks": 0,
                "successWorks": 0,
                "skippedWorks": 0,
                "failedWorks": 0,
                "auth_status": "needs_auth",
                "auth_reason": "manual_reauth_required",
                "needs_auth": True,
            },
        )

        self.assertEqual(ui_status, "auth_required")

    def test_decorate_progress_treats_running_unauthorized_platform_as_authorizing(self):
        with (
            patch.object(runner, "_persisted_auth_snapshot", return_value={"status": "unauthorized", "reason": "not_authorized"}),
            patch.object(runner, "is_auth_locked", return_value=False),
        ):
            decorated = runner._decorate_progress(
                "douyin",
                {
                    "status": "running",
                    "phase": "starting",
                    "message": "初始化导出任务",
                    "auth_status": "unauthorized",
                    "auth_reason": "not_authorized",
                    "needs_auth": True,
                    "totalWorks": 0,
                    "processedWorks": 0,
                    "successWorks": 0,
                    "skippedWorks": 0,
                    "failedWorks": 0,
                },
                ["douyin"],
            )

        self.assertEqual(decorated["ui_status"], "authorizing")

    def test_collector_runtime_preflight_requires_local_playwright_package(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertEqual(
                runner._collector_runtime_preflight_error(temp_dir),
                "playwright_not_installed",
            )
            package = pathlib.Path(temp_dir) / "node_modules" / "playwright" / "package.json"
            package.parent.mkdir(parents=True)
            package.write_text('{"name":"playwright"}', encoding="utf-8")
            self.assertEqual(runner._collector_runtime_preflight_error(temp_dir), "")

    def test_auto_sync_targets_include_completed_empty_for_incremental_updates(self):
        targets = runner._feishu_sync_target_platforms(
            [
                {"platform": "douyin", "status": "completed_empty"},
                {"platform": "xiaohongshu", "status": "needs_auth"},
                {"platform": "kuaishou", "status": "success"},
            ]
        )

        self.assertEqual(targets, ["douyin", "kuaishou"])

    def test_resolved_platform_scope_prefers_query_then_enabled_platforms(self):
        self.assertEqual(
            runner._resolved_platform_scope({"platforms": ["douyin,xiaohongshu"]}, {"enabled_platforms": ["bilibili"]}),
            ["douyin", "xiaohongshu"],
        )
        self.assertEqual(
            runner._resolved_platform_scope({}, {"enabled_platforms": ["douyin", "bilibili"]}),
            ["douyin", "bilibili"],
        )

    def test_progress_summary_separates_enabled_auth_required_platforms_from_failures(self):
        failed, needs_auth = runner._progress_summary_failure_buckets(
            {
                "douyin": {"enabled": True, "ui_status": "auth_required"},
                "xiaohongshu": {"enabled": True, "ui_status": "failed"},
                "bilibili": {"enabled": False, "ui_status": "auth_required"},
            }
        )

        self.assertEqual(failed, ["xiaohongshu"])
        self.assertEqual(needs_auth, ["douyin"])

    def test_runtime_summary_keeps_latest_successful_feishu_run_even_after_plain_sync(self):
        latest_plain_run = build_history_entry(
            run_at="2026-04-04 18:11:06",
            feishu_attempted=False,
            feishu_ok=False,
        )
        earlier_feishu_success = build_history_entry(
            run_at="2026-04-04 18:06:12",
            feishu_attempted=True,
            feishu_ok=True,
            feishu_result={
                "ok": True,
                "prepare": {"detail_count": 6, "work_count": 4},
                "sync": {"table_count": 2, "record_count": 10},
            },
        )

        summary = runner._build_feishu_runtime_summary(
            {
                "feishu_enabled": True,
                "feishu_ready": True,
                "auto_sync_enabled": False,
            },
            [latest_plain_run, earlier_feishu_success],
            current_stage="idle",
            enabled_platforms=["douyin"],
        )

        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["last_sync_at"], "2026-04-04 18:06:12")
        self.assertEqual(summary["last_platform_labels"], ["抖音"])
        self.assertIn("明细 6", summary["last_summary"])


    def test_runtime_summary_prefers_actual_feishu_platforms_from_prepare_meta(self):
        latest_feishu_success = build_history_entry(
            run_at="2026-04-04 21:28:05",
            feishu_attempted=True,
            feishu_ok=True,
            feishu_result={
                "ok": True,
                "prepare": {
                    "detail_count": 11,
                    "work_count": 9,
                    "platforms": ["douyin"],
                },
                "sync": {"table_count": 2, "record_count": 10},
            },
        )
        latest_feishu_success["platforms"] = [
            "douyin",
            "xiaohongshu",
            "bilibili",
            "kuaishou",
            "weixin_mp",
            "weixin_channels",
        ]

        summary = runner._build_feishu_runtime_summary(
            {
                "feishu_enabled": True,
                "feishu_ready": True,
                "auto_sync_enabled": True,
            },
            [latest_feishu_success],
            current_stage="idle",
            enabled_platforms=["douyin", "xiaohongshu", "bilibili"],
        )

        self.assertEqual(summary["last_platforms"], ["douyin"])
        self.assertEqual(summary["last_platform_labels"], ["抖音"])

    def test_feishu_prepare_syncable_data_requires_actual_rows(self):
        self.assertFalse(
            runner._feishu_prepare_has_syncable_data(
                {
                    "detail_count": 0,
                    "work_count": 0,
                    "chart_count": 0,
                    "increment_count": 0,
                }
            )
        )
        self.assertTrue(
            runner._feishu_prepare_has_syncable_data(
                {
                    "detail_count": 1,
                    "work_count": 0,
                    "chart_count": 0,
                    "increment_count": 0,
                }
            )
        )

    def test_feishu_prepare_syncable_data_respects_explicit_local_change_flag(self):
        self.assertFalse(
            runner._feishu_prepare_has_syncable_data(
                {
                    "detail_count": 11,
                    "work_count": 9,
                    "chart_count": 9,
                    "increment_count": 9,
                    "has_local_changes": False,
                    "new_detail_count": 0,
                    "updated_detail_count": 0,
                }
            )
        )
        self.assertTrue(
            runner._feishu_prepare_has_syncable_data(
                {
                    "detail_count": 11,
                    "work_count": 9,
                    "chart_count": 9,
                    "increment_count": 9,
                    "has_local_changes": True,
                    "new_detail_count": 1,
                    "updated_detail_count": 0,
                }
            )
        )

    def test_feishu_local_change_summary_ignores_runtime_only_fields(self):
        payload = {
            "tables": {
                "平台明细V2": [
                    {
                        "日期": 1764950400000,
                        "平台": "抖音",
                        "标题": "样例作品",
                        "发布日期": 1764547200000,
                        "内容类型": "视频",
                        "播放量": 100,
                        "点赞量": 10,
                        "收藏量": 5,
                        "评论量": 2,
                        "分享量": 1,
                        "涨粉量": 0,
                        "点赞率": 10.0,
                        "收藏率": 5.0,
                        "3s跳出率": None,
                        "最近更新时间": 1764950400000,
                        "同步键": "douyin:work-1",
                    }
                ]
            }
        }
        baseline = {
            "version": 1,
            "detail_rows": {
                "douyin:work-1": {
                    "平台": "抖音",
                    "标题": "样例作品",
                    "发布日期": 1764547200000,
                    "内容类型": "视频",
                    "播放量": 100,
                    "点赞量": 10,
                    "收藏量": 5,
                    "评论量": 2,
                    "分享量": 1,
                    "涨粉量": 0,
                    "点赞率": 10.0,
                    "收藏率": 5.0,
                    "3s跳出率": None,
                    "同步键": "douyin:work-1",
                }
            },
        }

        summary = runner._summarize_feishu_local_changes(payload, baseline)

        self.assertFalse(summary["has_local_changes"])
        self.assertEqual(summary["new_detail_count"], 0)
        self.assertEqual(summary["updated_detail_count"], 0)
        self.assertEqual(summary["unchanged_detail_count"], 1)

    def test_feishu_local_change_summary_detects_new_and_updated_rows(self):
        payload = {
            "tables": {
                "平台明细V2": [
                    {
                        "日期": 1764950400000,
                        "平台": "抖音",
                        "标题": "旧作品",
                        "发布日期": 1764547200000,
                        "内容类型": "视频",
                        "播放量": 120,
                        "点赞量": 10,
                        "收藏量": 5,
                        "评论量": 2,
                        "分享量": 1,
                        "涨粉量": 0,
                        "点赞率": 8.33,
                        "收藏率": 4.17,
                        "3s跳出率": None,
                        "最近更新时间": 1764950400000,
                        "同步键": "douyin:work-1",
                    },
                    {
                        "日期": 1764950400000,
                        "平台": "抖音",
                        "标题": "新作品",
                        "发布日期": 1764633600000,
                        "内容类型": "视频",
                        "播放量": 50,
                        "点赞量": 5,
                        "收藏量": 2,
                        "评论量": 1,
                        "分享量": 0,
                        "涨粉量": 0,
                        "点赞率": 10.0,
                        "收藏率": 4.0,
                        "3s跳出率": None,
                        "最近更新时间": 1764950400000,
                        "同步键": "douyin:work-2",
                    },
                ]
            }
        }
        baseline = {
            "version": 1,
            "detail_rows": {
                "douyin:work-1": {
                    "日期": 1764864000000,
                    "平台": "抖音",
                    "标题": "旧作品",
                    "发布日期": 1764547200000,
                    "内容类型": "视频",
                    "播放量": 100,
                    "点赞量": 10,
                    "收藏量": 5,
                    "评论量": 2,
                    "分享量": 1,
                    "涨粉量": 0,
                    "点赞率": 10.0,
                    "收藏率": 5.0,
                    "3s跳出率": None,
                    "最近更新时间": 1764864000000,
                    "同步键": "douyin:work-1",
                }
            },
        }

        summary = runner._summarize_feishu_local_changes(payload, baseline)

        self.assertTrue(summary["has_local_changes"])
        self.assertEqual(summary["new_detail_count"], 1)
        self.assertEqual(summary["updated_detail_count"], 1)
        self.assertEqual(summary["unchanged_detail_count"], 0)


class RunDateWindowTests(unittest.TestCase):
    def test_build_env_passes_max_publish_date_to_platform_scripts(self):
        handler = runner.Handler.__new__(runner.Handler)

        with (
            patch.object(
                runner,
                "load_saved_config",
                return_value={
                    "min_publish_date": "2026-01-01",
                    "browser_channel": "chrome",
                    "enabled_platforms": ["douyin"],
                },
            ),
            patch.object(runner, "resolve_default_node_bin", return_value="node"),
        ):
            env = handler._build_env(
                {
                    "min_date": ["2026-01-01"],
                    "max_date": ["2026-01-31"],
                },
                platform_id="douyin",
                progress_path="downloads/douyin_progress.json",
                is_xhs=False,
            )

        self.assertEqual(env["MIN_PUBLISH_DATE"], "2026-01-01")
        self.assertEqual(env["MAX_PUBLISH_DATE"], "2026-01-31")

    def test_build_env_for_rerun_forces_full_export(self):
        handler = runner.Handler.__new__(runner.Handler)

        with (
            patch.object(
                runner,
                "load_saved_config",
                return_value={
                    "min_publish_date": "2026-01-01",
                    "browser_channel": "chrome",
                    "enabled_platforms": ["douyin"],
                },
            ),
            patch.object(runner, "resolve_default_node_bin", return_value="node"),
        ):
            env = handler._build_env(
                {
                    "run_mode": ["rerun"],
                    "min_date": ["2026-03-01"],
                    "max_date": ["2026-04-05"],
                },
                platform_id="douyin",
                progress_path="downloads/douyin_progress.json",
                is_xhs=False,
            )

        self.assertEqual(env["FORCE_FULL_EXPORT"], "true")


class FeishuPrepareV2PlatformFilterTests(unittest.TestCase):
    def test_filter_by_platforms_keeps_matching_internal_platform_codes(self):
        platform_key = "\u5e73\u53f0"
        rows = [
            {platform_key: "douyin", "title": "alpha"},
            {platform_key: "xiaohongshu", "title": "beta"},
        ]

        filtered = prepare_feishu_v2.filter_by_platforms(rows, ["douyin"])

        self.assertEqual(filtered, [rows[0]])

    def test_filtered_rows_still_derive_platform_labels(self):
        platform_key = "\u5e73\u53f0"
        rows = [
            {platform_key: "douyin", "title": "alpha"},
            {platform_key: "douyin", "title": "beta"},
            {platform_key: "xiaohongshu", "title": "gamma"},
        ]

        filtered = prepare_feishu_v2.filter_by_platforms(rows, ["douyin"])
        included_platforms = [
            platform
            for platform in prepare_feishu_v2.PLATFORM_ORDER
            if any(prepare_feishu_v2.clean_value(row.get(platform_key)) == platform for row in filtered)
        ]
        platform_labels = [prepare_feishu_v2.display_platform(platform) for platform in included_platforms]

        self.assertEqual(included_platforms, ["douyin"])
        self.assertEqual(platform_labels, ["\u6296\u97f3"])

    def test_sync_log_rows_reports_platforms_from_detail_rows(self):
        meta = prepare_feishu_v2.SnapshotMeta(
            generated_at="2026-04-08 22:19:10",
            snapshot_date="2026-04-08",
            generated_at_ms=1760000000000,
            snapshot_date_ms=1760000000000,
            batch_id="sync-v2-20260408-221910",
        )

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(prepare_feishu_v2, "TREND_DB_PATH", pathlib.Path(temp_dir) / "trend.sqlite3"),
        ):
            rows = prepare_feishu_v2.build_sync_log_rows_v2(
                meta,
                detail_rows=[
                    {"平台": "抖音"},
                    {"平台": "小红书"},
                ],
                work_rows=[],
                chart_rows=[],
                increment_rows=[],
            )

        self.assertEqual(rows[0]["纳入平台"], "抖音 / 小红书")


class ManualFeishuSyncPreflightTests(unittest.TestCase):
    def test_feishu_cli_requires_user_auth_detects_schema_permission_limit(self):
        self.assertTrue(runner._feishu_cli_requires_user_auth("API call failed: [800004135] the method：OpenAPIAddField limited"))
        self.assertFalse(runner._feishu_cli_requires_user_auth("普通网络错误"))

    def test_feishu_config_ready_accepts_cli_mode_without_app_secret(self):
        with (
            patch.object(runner, "_lark_cli_is_configured", return_value=True),
            patch.object(
                runner,
                "_read_lark_cli_config",
                return_value={"apps": [{"appId": "cli_test_app_id"}]},
            ),
        ):
            ready = runner.feishu_config_ready(
                {
                    "feishu_enabled": True,
                    "feishu_cli_mode": True,
                    "feishu_app_token": "base_token_123",
                    "feishu_app_id": "cli_test_app_id",
                    "feishu_app_secret": "",
                }
            )

        self.assertTrue(ready)

    def test_manual_feishu_sync_preflight_does_not_pollute_history_when_feishu_is_disabled(self):
        with temporary_runner_server(
            {
                "customer_name": "测试客户",
                "workspace_name": "本地数据工作台",
                "enabled_platforms": [],
                "onboarding_completed": True,
                "feishu_enabled": False,
                "feishu_auto_sync": False,
            }
        ) as (server, history_file):
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/sync_feishu?min_date=2026-03-05",
                data=b"",
                method="POST",
            )

            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(request, timeout=5)

            written_history = json.loads(history_file.read_text(encoding="utf-8"))
            self.assertEqual(written_history, [])

    def test_manual_feishu_sync_without_new_rows_records_not_attempted(self):
        items, entry = runner._reconcile_feishu_history_after_manual_sync(
            [],
            min_date="2026-03-01",
            max_date="2026-04-05",
            ok=True,
            result={
                "ok": True,
                "attempted": False,
                "message": "没有新数据需要同步到飞书。",
                "prepare": {
                    "detail_count": 0,
                    "work_count": 0,
                    "chart_count": 0,
                    "increment_count": 0,
                    "platforms": ["douyin"],
                },
                "sync": {},
            },
            error="",
            synced_at="2026-04-05 10:30:00",
            duration=1.23,
            config={
                "min_publish_date": "2026-01-01",
                "enabled_platforms": ["douyin"],
            },
        )

        self.assertEqual(len(items), 1)
        self.assertFalse(entry["feishu_sync_attempted"])
        self.assertFalse(entry["feishu_sync_ok"])
        self.assertEqual(entry["feishu"]["status"], "not_attempted")
        self.assertIn("没有新数据", entry["feishu"]["summary"])

    def test_run_feishu_sync_skips_when_local_rows_match_success_baseline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            downloads_dir = root / "downloads"
            downloads_dir.mkdir(parents=True, exist_ok=True)
            payload_file = downloads_dir / "feishu_sync_payload_compare_v2.json"
            baseline_file = downloads_dir / "feishu_sync_baseline_v2.json"

            payload = {
                "tables": {
                    "平台明细V2": [
                        {
                            "日期": 1764950400000,
                            "平台": "抖音",
                            "标题": "样例作品",
                            "发布日期": 1764547200000,
                            "内容类型": "视频",
                            "播放量": 100,
                            "点赞量": 10,
                            "收藏量": 5,
                            "评论量": 2,
                            "分享量": 1,
                            "涨粉量": 0,
                            "点赞率": 10.0,
                            "收藏率": 5.0,
                            "3s跳出率": None,
                            "最近更新时间": 1764950400000,
                            "同步键": "douyin:work-1",
                        }
                    ]
                }
            }
            payload_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            baseline_file.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "detail_rows": {
                            "douyin:work-1": {
                                "平台": "抖音",
                                "标题": "样例作品",
                                "发布日期": 1764547200000,
                                "内容类型": "视频",
                                "播放量": 100,
                                "点赞量": 10,
                                "收藏量": 5,
                                "评论量": 2,
                                "分享量": 1,
                                "涨粉量": 0,
                                "点赞率": 10.0,
                                "收藏率": 5.0,
                                "3s跳出率": None,
                                "同步键": "douyin:work-1",
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            handler = runner.Handler.__new__(runner.Handler)
            handler._append_log = lambda *args, **kwargs: None
            run_script_calls = []

            def fake_run_script(cmd, env, timeout=None):
                run_script_calls.append(list(cmd))
                return SimpleNamespace(returncode=0)

            handler._run_script = fake_run_script
            handler._get_proc_output = lambda proc: (
                json.dumps(
                    {
                        "output": str(payload_file),
                        "detail_count": 1,
                        "work_count": 1,
                        "chart_count": 1,
                        "increment_count": 1,
                    },
                    ensure_ascii=False,
                ),
                "",
            )

            with (
                patch.object(runner, "DOWNLOADS_DIR", str(downloads_dir)),
                patch.object(runner, "FEISHU_SYNC_BASELINE_FILE", str(baseline_file)),
            ):
                result = handler._run_feishu_sync(
                    {
                        "feishu_enabled": True,
                        "feishu_app_token": "token",
                        "feishu_app_id": "id",
                        "feishu_app_secret": "secret",
                    },
                    min_date="2026-03-01",
                    max_date="2026-04-05",
                    platforms=["douyin"],
                )

        self.assertFalse(result["attempted"])
        self.assertEqual(result["reason"], "no_new_data")
        self.assertFalse(result["prepare"]["has_local_changes"])
        self.assertEqual(result["prepare"]["unchanged_detail_count"], 1)
        self.assertEqual(len(run_script_calls), 1)

    def test_run_feishu_sync_persists_successful_detail_baseline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            downloads_dir = root / "downloads"
            downloads_dir.mkdir(parents=True, exist_ok=True)
            payload_file = downloads_dir / "feishu_sync_payload_compare_v2.json"
            baseline_file = downloads_dir / "feishu_sync_baseline_v2.json"
            pending_snapshot = downloads_dir / "feishu_sync_pending_snapshot_v2.json"
            pending_snapshot.write_text("{}", encoding="utf-8")

            payload = {
                "tables": {
                    "平台明细V2": [
                        {
                            "日期": 1764950400000,
                            "平台": "抖音",
                            "标题": "新作品",
                            "发布日期": 1764633600000,
                            "内容类型": "视频",
                            "播放量": 50,
                            "点赞量": 5,
                            "收藏量": 2,
                            "评论量": 1,
                            "分享量": 0,
                            "涨粉量": 0,
                            "点赞率": 10.0,
                            "收藏率": 4.0,
                            "3s跳出率": None,
                            "最近更新时间": 1764950400000,
                            "同步键": "douyin:work-2",
                        }
                    ]
                }
            }
            payload_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            handler = runner.Handler.__new__(runner.Handler)
            handler._append_log = lambda *args, **kwargs: None
            run_script_calls = []
            outputs = iter(
                [
                    (
                        json.dumps(
                            {
                                "output": str(payload_file),
                                "detail_count": 1,
                                "work_count": 1,
                                "chart_count": 1,
                                "increment_count": 1,
                                "pending_snapshot_path": str(pending_snapshot),
                            },
                            ensure_ascii=False,
                        ),
                        "",
                    ),
                    (json.dumps({"ok": True, "results": []}, ensure_ascii=False), ""),
                    (json.dumps({"ok": True, "committed_batch_id": "batch-1"}, ensure_ascii=False), ""),
                ]
            )

            handler._run_script = lambda cmd, env, timeout=None: run_script_calls.append(list(cmd)) or SimpleNamespace(returncode=0)
            handler._get_proc_output = lambda proc: next(outputs)

            with (
                patch.object(runner, "DOWNLOADS_DIR", str(downloads_dir)),
                patch.object(runner, "FEISHU_SYNC_BASELINE_FILE", str(baseline_file)),
            ):
                result = handler._run_feishu_sync(
                    {
                        "feishu_enabled": True,
                        "feishu_app_token": "token",
                        "feishu_app_id": "id",
                        "feishu_app_secret": "secret",
                    },
                    min_date="2026-03-01",
                    max_date="2026-04-05",
                    platforms=["douyin"],
                )

            baseline = json.loads(baseline_file.read_text(encoding="utf-8"))

        self.assertTrue(result["attempted"])
        self.assertTrue(result["ok"])
        self.assertIn("douyin:work-2", baseline["detail_rows"])
        self.assertEqual(baseline["detail_rows"]["douyin:work-2"]["标题"], "新作品")
        self.assertEqual(baseline["timezone"], "Asia/Shanghai")
        self.assertEqual(len(run_script_calls), 3)
        self.assertIn("--commit-snapshot", run_script_calls[2])
        self.assertIn(str(pending_snapshot), run_script_calls[2])

    def test_run_feishu_sync_cli_mode_invokes_sync_script_without_app_secret(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            downloads_dir = root / "downloads"
            downloads_dir.mkdir(parents=True, exist_ok=True)
            payload_file = downloads_dir / "feishu_sync_payload_compare_v2.json"
            payload_file.write_text(json.dumps({"table_definitions": [], "tables": {}}, ensure_ascii=False), encoding="utf-8")

            handler = runner.Handler.__new__(runner.Handler)
            handler._append_log = lambda *args, **kwargs: None
            run_script_calls = []
            outputs = iter(
                [
                    (
                        json.dumps(
                            {
                                "output": str(payload_file),
                                "detail_count": 1,
                                "work_count": 1,
                                "chart_count": 1,
                                "increment_count": 1,
                            },
                            ensure_ascii=False,
                        ),
                        "",
                    ),
                    (json.dumps({"ok": True, "results": []}, ensure_ascii=False), ""),
                ]
            )

            handler._run_script = lambda cmd, env, timeout=None: run_script_calls.append(list(cmd)) or SimpleNamespace(returncode=0)
            handler._get_proc_output = lambda proc: next(outputs)

            with (
                patch.object(runner, "BASE_DIR", str(root)),
                patch.object(runner, "DOWNLOADS_DIR", str(downloads_dir)),
                patch.object(runner, "_lark_cli_is_configured", return_value=True),
                patch.object(
                    runner,
                    "_read_lark_cli_config",
                    return_value={"apps": [{"appId": "cli_test_app_id"}]},
                ),
            ):
                result = handler._run_feishu_sync(
                    {
                        "feishu_enabled": True,
                        "feishu_cli_mode": True,
                        "feishu_app_token": "base_token_123",
                        "feishu_app_id": "cli_test_app_id",
                        "feishu_app_secret": "",
                    },
                    min_date="2026-03-01",
                )

        self.assertTrue(result["ok"])
        self.assertEqual(len(run_script_calls), 2)
        self.assertIn("--cli-mode", run_script_calls[1])
        self.assertNotIn("--app-secret", run_script_calls[1])

    def test_run_feishu_sync_defaults_to_enabled_platform_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            downloads_dir = root / "downloads"
            downloads_dir.mkdir(parents=True, exist_ok=True)
            payload_file = downloads_dir / "feishu_sync_payload_compare_v2.json"
            payload_file.write_text(json.dumps({"table_definitions": [], "tables": {}}, ensure_ascii=False), encoding="utf-8")

            handler = runner.Handler.__new__(runner.Handler)
            handler._append_log = lambda *args, **kwargs: None
            run_script_calls = []
            outputs = iter(
                [
                    (
                        json.dumps(
                            {
                                "output": str(payload_file),
                                "detail_count": 1,
                                "work_count": 1,
                                "chart_count": 0,
                                "increment_count": 0,
                            },
                            ensure_ascii=False,
                        ),
                        "",
                    ),
                    (json.dumps({"ok": True, "results": []}, ensure_ascii=False), ""),
                ]
            )

            handler._run_script = lambda cmd, env, timeout=None: run_script_calls.append(list(cmd)) or SimpleNamespace(returncode=0)
            handler._get_proc_output = lambda proc: next(outputs)

            with (
                patch.object(runner, "DOWNLOADS_DIR", str(downloads_dir)),
                patch.object(runner, "_lark_cli_is_configured", return_value=True),
                patch.object(
                    runner,
                    "_read_lark_cli_config",
                    return_value={"apps": [{"appId": "cli_test_app_id"}]},
                ),
            ):
                result = handler._run_feishu_sync(
                    {
                        "feishu_enabled": True,
                        "feishu_cli_mode": True,
                        "feishu_app_token": "base_token_123",
                        "feishu_app_id": "cli_test_app_id",
                        "feishu_app_secret": "",
                        "enabled_platforms": ["douyin", "bilibili"],
                    },
                    min_date="2026-03-01",
                )

        self.assertTrue(result["ok"])
        self.assertIn("--platforms", run_script_calls[0])
        self.assertIn("douyin,bilibili", run_script_calls[0])

    def test_test_feishu_connection_cli_mode_does_not_require_app_secret(self):
        handler = runner.Handler.__new__(runner.Handler)
        handler._append_log = lambda *args, **kwargs: None
        run_script_calls = []
        handler._run_script = lambda cmd, env, timeout=None: run_script_calls.append(list(cmd)) or SimpleNamespace(returncode=0)
        handler._get_proc_output = lambda proc: (json.dumps({"ok": True, "results": []}, ensure_ascii=False), "")

        with (
            patch.object(runner, "load_saved_config", return_value={}),
            patch.object(runner, "_lark_cli_is_configured", return_value=True),
            patch.object(
                runner,
                "_read_lark_cli_config",
                return_value={"apps": [{"appId": "cli_test_app_id"}]},
            ),
        ):
            result = handler._test_feishu_connection(
                {
                    "feishu_enabled": True,
                    "feishu_cli_mode": True,
                    "feishu_app_token": "base_token_123",
                    "feishu_app_id": "cli_test_app_id",
                    "feishu_app_secret": "",
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(len(run_script_calls), 1)
        self.assertIn("--cli-mode", run_script_calls[0])
        self.assertNotIn("--app-secret", run_script_calls[0])

    def test_run_feishu_sync_triggers_user_auth_when_cli_hits_field_permission_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            downloads_dir = root / "downloads"
            downloads_dir.mkdir(parents=True, exist_ok=True)
            payload_file = downloads_dir / "feishu_sync_payload_compare_v2.json"
            payload_file.write_text(json.dumps({"table_definitions": [], "tables": {}}, ensure_ascii=False), encoding="utf-8")

            handler = runner.Handler.__new__(runner.Handler)
            handler._append_log = lambda *args, **kwargs: None
            outputs = iter(
                [
                    (
                        json.dumps(
                            {
                                "output": str(payload_file),
                                "detail_count": 1,
                                "work_count": 1,
                                "chart_count": 0,
                                "increment_count": 0,
                            },
                            ensure_ascii=False,
                        ),
                        "",
                    ),
                    (
                        "",
                        "RuntimeError: lark-cli 命令失败 (exit 1): {\"error\":{\"code\":800004135,\"message\":\"API call failed: [800004135] the method：OpenAPIAddField limited\"}}",
                    ),
                ]
            )

            def fake_run_script(cmd, env, timeout=None):
                return SimpleNamespace(returncode=0 if "--cli-mode" not in cmd else 1)

            handler._run_script = fake_run_script
            handler._get_proc_output = lambda proc: next(outputs)
            started_reauth = []

            with (
                patch.object(runner, "BASE_DIR", str(root)),
                patch.object(runner, "DOWNLOADS_DIR", str(downloads_dir)),
                patch.object(runner, "_start_lark_cli_user_auth", side_effect=lambda reason="": started_reauth.append(reason) or {"ok": True}),
                patch.object(runner, "_lark_cli_is_configured", return_value=True),
                patch.object(
                    runner,
                    "_read_lark_cli_config",
                    return_value={"apps": [{"appId": "cli_test_app_id"}]},
                ),
            ):
                with self.assertRaises(RuntimeError) as exc:
                    handler._run_feishu_sync(
                        {
                            "feishu_enabled": True,
                            "feishu_cli_mode": True,
                            "feishu_app_token": "base_token_123",
                            "feishu_app_id": "cli_test_app_id",
                            "feishu_app_secret": "",
                            "enabled_platforms": ["douyin"],
                        },
                        min_date="2026-03-01",
                        platforms=["douyin"],
                    )

        self.assertIn("飞书需要补充用户授权", str(exc.exception))
        self.assertEqual(len(started_reauth), 1)


class ProgressRecoveryTests(unittest.TestCase):
    def test_load_platform_progress_restores_last_stable_snapshot_when_running_state_is_stale(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            progress_path = pathlib.Path(temp_dir) / "douyin_progress.json"
            progress_path.write_text(
                json.dumps(
                    {
                        **runner.default_progress("douyin"),
                        "status": "running",
                        "phase": "collecting",
                        "message": "正在采集中",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            stable_path = pathlib.Path(str(progress_path) + ".stable.json")
            stable_path.write_text(
                json.dumps(
                    {
                        **runner.default_progress("douyin"),
                        "status": "completed",
                        "phase": "done",
                        "message": "同步完成",
                        "finishedAt": "2026-04-09T00:00:00+0800",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch.object(runner, "is_locked", return_value=False):
                restored = runner._load_platform_progress("douyin", str(progress_path))

            persisted = json.loads(progress_path.read_text(encoding="utf-8"))

        self.assertEqual(restored["status"], "completed")
        self.assertTrue(restored["recovered_from_stale_run"])
        self.assertEqual(persisted["recovery_reason"], "stale_running_progress")


class AuthRecoveryTests(unittest.TestCase):
    def test_auth_single_ui_success_does_not_trigger_headless_probe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            auth_dir = root / ".auth"
            downloads_dir = root / "downloads"
            auth_dir.mkdir(parents=True, exist_ok=True)
            downloads_dir.mkdir(parents=True, exist_ok=True)

            progress_path = downloads_dir / "douyin_progress.json"
            progress_path.write_text(
                json.dumps(
                    {
                        **runner.default_progress("douyin"),
                        "status": "running",
                        "phase": "login",
                        "message": "等待扫码登录（最多 300 秒）",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            handler = runner.Handler.__new__(runner.Handler)
            handler._append_log = lambda *args, **kwargs: None
            call_sequence = []

            def fake_run_auth_process_with_profile_recovery(platform_id, script, query, progress_path, **kwargs):
                call_sequence.append(("ui", dict(query)))
                # Simulate the auth script writing authorized status to progress file
                progress = json.loads(pathlib.Path(progress_path).read_text(encoding="utf-8"))
                progress["auth_status"] = "authorized"
                progress["auth_reason"] = ""
                progress["status"] = "completed"
                progress["phase"] = "done"
                pathlib.Path(progress_path).write_text(json.dumps(progress, ensure_ascii=False), encoding="utf-8")
                return True, "", ""

            def fake_run_auth_process_once(command_platform_id, script, query, progress_path, **kwargs):
                call_sequence.append(("probe", dict(query)))
                return False, "", "登录超时，未检测到登录状态。"

            handler._run_auth_process_with_profile_recovery = fake_run_auth_process_with_profile_recovery
            handler._run_auth_process_once = fake_run_auth_process_once

            with (
                patch.object(runner, "AUTH_DIR", str(auth_dir)),
                patch.object(runner, "AUTH_STATE_FILE", str(auth_dir / "auth_state.json")),
                patch.object(runner, "AUTH_HEALTH_FILE", str(auth_dir / "auth_health.json")),
                patch.object(runner, "CONFIG_FILE", str(auth_dir / "customer_config.json")),
                patch.object(runner, "RUN_HISTORY_FILE", str(downloads_dir / "run_history.json")),
                patch.object(runner, "load_saved_config", return_value={"enabled_platforms": ["douyin"]}),
            ):
                result = handler._execute_auth_single(
                    "douyin",
                    "scripts/douyin_export.mjs",
                    str(progress_path),
                    {
                        "auth_only": ["true"],
                        "headless": ["false"],
                        "scan_wait_ms": ["300000"],
                    },
                    start=0.0,
                )

            progress = json.loads(progress_path.read_text(encoding="utf-8"))

            self.assertTrue(result["ok"])
            self.assertEqual(progress["status"], "completed")
            self.assertEqual(progress["auth_status"], "authorized")
            self.assertEqual([item[0] for item in call_sequence], ["ui"])

    def test_auth_only_timeout_without_prior_authorization_stays_unauthorized(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            auth_dir = root / ".auth"
            downloads_dir = root / "downloads"
            auth_dir.mkdir(parents=True, exist_ok=True)
            downloads_dir.mkdir(parents=True, exist_ok=True)

            progress_path = downloads_dir / "douyin_progress.json"
            progress_path.write_text(
                json.dumps(
                    {
                        **runner.default_progress("douyin"),
                        "status": "running",
                        "phase": "login",
                        "message": "等待扫码登录（最多 300 秒）",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with (
                patch.object(runner, "AUTH_DIR", str(auth_dir)),
                patch.object(runner, "AUTH_STATE_FILE", str(auth_dir / "auth_state.json")),
                patch.object(runner, "AUTH_HEALTH_FILE", str(auth_dir / "auth_health.json")),
            ):
                progress = runner._finalize_platform_progress(
                    "douyin",
                    str(progress_path),
                    ok=False,
                    stderr="登录超时，未检测到登录状态。",
                    auth_only=True,
                    failure_message="授权未完成",
                )

        self.assertEqual(progress["status"], "failed")
        self.assertEqual(progress["auth_status"], "unauthorized")
        self.assertEqual(progress["auth_reason"], "not_authorized")
        self.assertTrue(progress["needs_auth"])

    def test_auth_only_timeout_after_prior_authorization_keeps_expired_cookie(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            auth_dir = root / ".auth"
            downloads_dir = root / "downloads"
            auth_dir.mkdir(parents=True, exist_ok=True)
            downloads_dir.mkdir(parents=True, exist_ok=True)

            (auth_dir / "auth_state.json").write_text(
                json.dumps(
                    {
                        "douyin": {
                            "status": "authorized",
                            "reason": "",
                            "updated_at": "2026-04-05T11:00:00+0800",
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            progress_path = downloads_dir / "douyin_progress.json"
            progress_path.write_text(
                json.dumps(
                    {
                        **runner.default_progress("douyin"),
                        "status": "running",
                        "phase": "login",
                        "message": "等待扫码登录（最多 300 秒）",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with (
                patch.object(runner, "AUTH_DIR", str(auth_dir)),
                patch.object(runner, "AUTH_STATE_FILE", str(auth_dir / "auth_state.json")),
                patch.object(runner, "AUTH_HEALTH_FILE", str(auth_dir / "auth_health.json")),
            ):
                progress = runner._finalize_platform_progress(
                    "douyin",
                    str(progress_path),
                    ok=False,
                    stderr="登录超时，未检测到登录状态。",
                    auth_only=True,
                    failure_message="授权未完成",
                )

        self.assertEqual(progress["status"], "failed")
        self.assertEqual(progress["auth_status"], "expired")
        self.assertEqual(progress["auth_reason"], "expired_cookie")
        self.assertTrue(progress["needs_auth"])

    def test_auth_single_ui_window_closed_without_probe_stays_failed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            auth_dir = root / ".auth"
            downloads_dir = root / "downloads"
            auth_dir.mkdir(parents=True, exist_ok=True)
            downloads_dir.mkdir(parents=True, exist_ok=True)

            progress_path = downloads_dir / "xiaohongshu_progress.json"
            progress_path.write_text(
                json.dumps(
                    {
                        **runner.default_progress("xiaohongshu"),
                        "status": "failed",
                        "phase": "failed",
                        "message": "授权未完成",
                        "auth_status": "needs_auth",
                        "auth_reason": "manual_reauth_required",
                        "needs_auth": True,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            handler = runner.Handler.__new__(runner.Handler)
            handler._append_log = lambda *args, **kwargs: None
            call_sequence = []

            def fake_run_auth_process_with_profile_recovery(platform_id, script, query, progress_path, **kwargs):
                call_sequence.append(("ui", dict(query)))
                return (
                    False,
                    "",
                    "page.waitForTimeout: Target page, context or browser has been closed",
                )

            def fake_run_auth_process_once(command_platform_id, script, query, progress_path, **kwargs):
                call_sequence.append(("probe", dict(query)))
                return True, "", ""

            handler._run_auth_process_with_profile_recovery = fake_run_auth_process_with_profile_recovery
            handler._run_auth_process_once = fake_run_auth_process_once

            with (
                patch.object(runner, "AUTH_DIR", str(auth_dir)),
                patch.object(runner, "AUTH_STATE_FILE", str(auth_dir / "auth_state.json")),
                patch.object(runner, "AUTH_HEALTH_FILE", str(auth_dir / "auth_health.json")),
                patch.object(runner, "CONFIG_FILE", str(auth_dir / "customer_config.json")),
                patch.object(runner, "RUN_HISTORY_FILE", str(downloads_dir / "run_history.json")),
                patch.object(runner, "load_saved_config", return_value={"enabled_platforms": ["xiaohongshu"]}),
            ):
                result = handler._execute_auth_single(
                    "xiaohongshu",
                    "scripts/xiaohongshu_export.mjs",
                    str(progress_path),
                    {
                        "auth_only": ["true"],
                        "headless": ["false"],
                        "scan_wait_ms": ["300000"],
                    },
                    start=0.0,
                )

            progress = json.loads(progress_path.read_text(encoding="utf-8"))

            self.assertFalse(result["ok"])
            self.assertEqual(progress["status"], "failed")
            self.assertEqual(progress["auth_status"], "unauthorized")
            self.assertEqual(progress["auth_reason"], "not_authorized")
            self.assertEqual([item[0] for item in call_sequence], ["ui"])


class FeishuVerificationUrlOpenTests(unittest.TestCase):
    def test_trusted_feishu_cli_url_opens_with_macos_open(self):
        url = "https://open.feishu.cn/page/cli?user_code=TEST-CODE"
        with (
            patch.object(runner.sys, "platform", "darwin"),
            patch.object(runner.subprocess, "Popen") as popen_mock,
        ):
            self.assertTrue(runner._open_trusted_feishu_verification_url(url))
        popen_mock.assert_called_once()
        self.assertEqual(popen_mock.call_args.args[0], ["/usr/bin/open", url])
        self.assertTrue(popen_mock.call_args.kwargs["start_new_session"])

    def test_untrusted_or_non_https_feishu_url_is_never_opened(self):
        urls = [
            "http://open.feishu.cn/page/cli?user_code=TEST",
            "https://open.feishu.cn.evil.example/page/cli?user_code=TEST",
            "https://open.feishu.cn/other/path",
            "javascript:alert(1)",
        ]
        with (
            patch.object(runner.sys, "platform", "darwin"),
            patch.object(runner.subprocess, "Popen") as popen_mock,
        ):
            self.assertTrue(all(not runner._open_trusted_feishu_verification_url(url) for url in urls))
        popen_mock.assert_not_called()

    def test_both_feishu_onboarding_authorization_stages_auto_open(self):
        helper_name = "_open_trusted_feishu_verification_url"
        self.assertIn(helper_name, runner._run_lark_cli_user_auth_flow.__code__.co_names)
        connect_names = set(runner._lark_cli_connect_worker.__code__.co_names)
        for constant in runner._lark_cli_connect_worker.__code__.co_consts:
            if hasattr(constant, "co_names"):
                connect_names.update(constant.co_names)
        self.assertIn(helper_name, connect_names)


    def test_lark_cli_output_deadline_terminates_silent_child(self):
        proc = runner.subprocess.Popen(
            [runner.sys.executable, "-c", "import time; time.sleep(5)"],
            stdout=runner.subprocess.PIPE,
            stderr=runner.subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
        started = time.monotonic()
        with self.assertRaises(runner.subprocess.TimeoutExpired):
            runner._consume_lark_cli_output(proc, lambda _line: None, timeout_seconds=0.1)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIsNotNone(proc.poll())

    def test_lark_cli_output_stream_delivers_lines_before_exit(self):
        proc = runner.subprocess.Popen(
            [runner.sys.executable, "-c", "print('first', flush=True); print('second', flush=True)"],
            stdout=runner.subprocess.PIPE,
            stderr=runner.subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
        lines = []
        runner._consume_lark_cli_output(proc, lines.append, timeout_seconds=2)
        self.assertEqual([line.strip() for line in lines], ["first", "second"])
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
