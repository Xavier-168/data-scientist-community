import importlib.util
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch


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


class ReconcileFeishuHistoryAfterManualSyncTests(unittest.TestCase):
    def _failed_feishu_run(self):
        return runner._build_run_history_entry(
            raw_mode="run_all",
            requested_mode="incremental",
            min_date="2026-03-05",
            started_at="2026-04-04 11:38:24",
            ended_at="2026-04-04 11:38:48",
            duration=24.0,
            merge_ok=True,
            platform_snapshot={
                "platforms": [
                    {
                        "platform": "douyin",
                        "label": "抖音",
                        "ui_status": "completed",
                        "message": "同步完成，共 3 条",
                        "last_sync_at": "2026-04-04 11:38:47",
                        "total_works": 3,
                        "success_works": 3,
                        "skipped_works": 0,
                        "failed_works": 0,
                        "auth_status": "authorized",
                        "auth_reason": "",
                        "auth_action": "none",
                        "needs_auth": False,
                    }
                ]
            },
            feishu_attempted=True,
            feishu_ok=False,
            feishu_error='同步到飞书多维表格失败：{"code": 10003, "data": {}, "msg": "invalid param"}',
        )

    def test_successful_manual_feishu_sync_repairs_latest_failed_run(self):
        failed_run = self._failed_feishu_run()

        items, updated = runner._reconcile_feishu_history_after_manual_sync(
            [failed_run],
            min_date="2026-03-05",
            ok=True,
            result={
                "ok": True,
                "prepare": {"detail_count": 3, "work_count": 3},
                "sync": {"table_count": 2, "record_count": 3},
            },
            error="",
            synced_at="2026-04-04 11:39:38",
            duration=6.2,
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(updated["failed_stage"], "")
        self.assertTrue(updated["ok"])
        self.assertEqual(updated["feishu"]["status"], "success")
        self.assertEqual(updated["feishu"]["error"], "")
        self.assertIn("写入表 2", updated["feishu"]["summary"])

    def test_build_feishu_runtime_summary_reports_running_state(self):
        summary = runner._build_feishu_runtime_summary(
            {
                "feishu_enabled": True,
                "feishu_ready": True,
                "auto_sync_enabled": True,
            },
            None,
            current_stage="importing",
            enabled_platforms=["douyin"],
        )

        self.assertEqual(summary["status"], "running")
        self.assertIn("抖音", summary["message"])
        self.assertEqual(summary["current_platform_labels"], ["抖音"])

    def test_build_feishu_runtime_summary_reports_latest_success_metadata(self):
        latest_run = self._failed_feishu_run()
        latest_run = runner._update_history_entry_after_manual_feishu_sync(
            latest_run,
            ok=True,
            result={
                "ok": True,
                "prepare": {"detail_count": 3, "work_count": 3},
                "sync": {"table_count": 2, "record_count": 3},
            },
            synced_at="2026-04-04 11:39:38",
            duration=6.2,
        )

        summary = runner._build_feishu_runtime_summary(
            {
                "feishu_enabled": True,
                "feishu_ready": True,
                "auto_sync_enabled": True,
            },
            latest_run,
            current_stage="idle",
            enabled_platforms=["douyin"],
        )

        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["last_sync_at"], "2026-04-04 11:39:38")
        self.assertEqual(summary["last_platform_labels"], ["抖音"])
        self.assertIn("明细 3", summary["last_summary"])

    def test_build_feishu_runtime_summary_surfaces_latest_skip_reason(self):
        latest_success = runner._build_run_history_entry(
            raw_mode="feishu_only",
            requested_mode="feishu_only",
            min_date="2026-03-01",
            max_date="2026-04-05",
            started_at="2026-04-05 11:19:29",
            ended_at="2026-04-05 11:19:29",
            duration=1.0,
            merge_ok=True,
            platform_snapshot={"platforms": []},
            feishu_attempted=True,
            feishu_ok=True,
            feishu_result={"ok": True, "prepare": {"detail_count": 11, "work_count": 9}, "sync": {"table_count": 5}},
        )
        latest_skip = runner._build_run_history_entry(
            raw_mode="feishu_only",
            requested_mode="feishu_only",
            min_date="2026-03-01",
            max_date="2026-04-05",
            started_at="2026-04-05 11:19:30",
            ended_at="2026-04-05 11:19:30",
            duration=0.8,
            merge_ok=True,
            platform_snapshot={"platforms": []},
            feishu_attempted=False,
            feishu_ok=False,
            feishu_result={
                "ok": True,
                "attempted": False,
                "message": "本地没有新数据或指标变化，已跳过飞书同步。",
                "prepare": {"detail_count": 11, "work_count": 9},
                "sync": {},
            },
        )

        summary = runner._build_feishu_runtime_summary(
            {
                "feishu_enabled": True,
                "feishu_ready": True,
                "auto_sync_enabled": True,
            },
            [latest_skip, latest_success],
            current_stage="idle",
            enabled_platforms=["douyin"],
        )

        self.assertEqual(summary["status"], "idle")
        self.assertIn("没有新数据", summary["message"])
        self.assertEqual(summary["last_sync_at"], "2026-04-05 11:19:29")


    def test_manual_feishu_only_history_ignores_stale_platform_progress_states(self):
        stale_snapshot = {
            "platforms": [
                {
                    "platform": "douyin",
                    "label": "鎶栭煶",
                    "ui_status": "running",
                    "message": "姝ｅ湪瀵煎嚭浣滃搧鏁版嵁",
                    "last_sync_at": "2026-04-05T06:35:25.483Z",
                    "total_works": 6,
                    "success_works": 0,
                    "skipped_works": 0,
                    "failed_works": 0,
                    "auth_status": "authorized",
                    "auth_reason": "",
                    "auth_action": "none",
                    "needs_auth": False,
                    "status": "failed",
                }
            ]
        }

        with patch.object(runner, "_platform_history_snapshot", return_value=stale_snapshot):
            items, history_entry = runner._reconcile_feishu_history_after_manual_sync(
                [],
                min_date="2026-03-06",
                max_date="",
                ok=False,
                result={
                    "ok": True,
                    "attempted": False,
                    "message": "鏈湴娌℃湁鏂版暟鎹垨鎸囨爣鍙樺寲锛屽凡璺宠繃椋炰功鍚屾銆?",
                    "prepare": {
                        "detail_count": 11,
                        "work_count": 9,
                        "platforms": [],
                    },
                    "sync": {},
                },
                error="",
                synced_at="2026-04-05 14:41:41",
                duration=0.93,
                config={"enabled_platforms": ["douyin", "xiaohongshu"]},
            )

        self.assertEqual(len(items), 1)
        self.assertEqual(history_entry["mode"], "feishu_only")
        self.assertEqual(history_entry["platforms"], ["douyin", "xiaohongshu"])
        self.assertEqual(history_entry["platform_count"], 2)
        self.assertEqual(history_entry["platform_results"], [])
        self.assertEqual(history_entry["status"], "completed")
        self.assertEqual(history_entry["failed_stage"], "")
        self.assertEqual(history_entry["feishu"]["status"], "not_attempted")

    def test_runtime_summary_surfaces_auto_sync_skip_reason(self):
        latest_skip = runner._build_run_history_entry(
            raw_mode="run_all",
            requested_mode="incremental",
            min_date="2026-03-01",
            max_date="2026-04-05",
            started_at="2026-04-05 11:19:30",
            ended_at="2026-04-05 11:19:30",
            duration=0.8,
            merge_ok=True,
            platform_snapshot={
                "platforms": [
                    {
                        "platform": "douyin",
                        "label": "抖音",
                        "ui_status": "completed_empty",
                        "message": "本轮没有新增导出，已沿用已有本地结果",
                        "last_sync_at": "2026-04-05 11:19:29",
                        "total_works": 5,
                        "success_works": 0,
                        "skipped_works": 5,
                        "failed_works": 0,
                        "auth_status": "authorized",
                        "auth_reason": "",
                        "auth_action": "none",
                        "needs_auth": False,
                    }
                ]
            },
            feishu_attempted=False,
            feishu_ok=False,
            feishu_result={
                "ok": True,
                "attempted": False,
                "message": "未开启采集后自动同步飞书，已跳过本次同步。",
                "prepare": {"platforms": ["douyin"]},
                "sync": {},
            },
        )

        summary = runner._build_feishu_runtime_summary(
            {
                "feishu_enabled": True,
                "feishu_ready": True,
                "auto_sync_enabled": False,
            },
            [latest_skip],
            current_stage="idle",
            enabled_platforms=["douyin"],
        )

        self.assertEqual(summary["status"], "idle")
        self.assertIn("已跳过本次同步", summary["message"])

    def test_runtime_summary_exposes_reauth_link_when_user_auth_is_pending(self):
        with patch.object(
            runner,
            "_copy_lark_cli_state",
            return_value={
                "phase": "scan_qr",
                "auth_mode": "user",
                "message": "飞书需要补充用户授权，请扫码完成后自动继续。",
                "verification_url": "https://open.feishu.cn/mock-auth",
            },
        ):
            summary = runner._build_feishu_runtime_summary(
                {
                    "feishu_enabled": True,
                    "feishu_ready": True,
                    "auto_sync_enabled": True,
                },
                [],
                current_stage="idle",
                enabled_platforms=["douyin"],
            )

        self.assertEqual(summary["status"], "needs_auth")
        self.assertEqual(summary["verification_url"], "https://open.feishu.cn/mock-auth")
        self.assertIn("补充用户授权", summary["message"])


if __name__ == "__main__":
    unittest.main()
