import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import merge_channels
import prepare_feishu_bitable_sync_v2 as prepare_v2
import sync_feishu_bitable_openapi as feishu_sync
from domain import grouping


class EpisodeGroupingTests(unittest.TestCase):
    def test_episode_title_key_accepts_bracket_suffix_after_marker(self):
        title = "Harness，到底是个啥？（上）【小包必会】"

        self.assertEqual(
            prepare_v2.episode_title_key(title),
            "harness到底是个啥episode上",
        )
        self.assertEqual(
            merge_channels.episode_title_key(title),
            "harness到底是个啥episode上",
        )

    def test_assign_work_keys_groups_same_episode_marker_by_fuzzy_base(self):
        rows = [
            {"平台作品键": "dy-up", "标题": "Harness，到底是个啥？（上）【小包必会】"},
            {"平台作品键": "xhs-up", "标题": "Harness，到底是个啥？（上）"},
            {"平台作品键": "dy-down", "标题": "Harness，到底是个啥？（下）【小包必会】"},
            {"平台作品键": "xhs-down", "标题": "Harness，到底是个啥？（下）"},
            {"平台作品键": "dy-march", "标题": "一个月赚10万我的一人公司三月复盘（上）"},
            {"平台作品键": "xhs-march", "标题": "一个月赚了10万？我的一人公司三月复盘（上）"},
            {"平台作品键": "bili-march", "标题": "未通过 一个月赚10万我的一人公司三月复盘（上）"},
        ]

        work_keys = prepare_v2.assign_work_keys(rows)

        self.assertEqual(work_keys["dy-up"], work_keys["xhs-up"])
        self.assertEqual(work_keys["dy-down"], work_keys["xhs-down"])
        self.assertNotEqual(work_keys["dy-up"], work_keys["dy-down"])
        self.assertEqual(work_keys["dy-march"], work_keys["xhs-march"])
        self.assertEqual(work_keys["dy-march"], work_keys["bili-march"])

    def test_assign_work_keys_groups_short_cross_platform_title_with_full_title(self):
        rows = [
            {
                "平台作品键": "douyin:7669703345283583242",
                "标题": "如果你用不了Codex，不妨试试这个！ 【Cherry-Studio手把手教程】",
            },
            {
                "平台作品键": "bilibili:BV1Haum6FE8a",
                "标题": "如果你用不了Codex，不妨试试这个！ 【Cherry-Studio手把手教程】",
            },
            {
                "平台作品键": "xiaohongshu:6a7858d1000000002501771b",
                "标题": "【Cherry-Studio手把手教程】",
            },
        ]

        work_keys = prepare_v2.assign_work_keys(rows)

        self.assertEqual(grouping.TITLE_SIMILARITY_THRESHOLD, 0.40)
        self.assertEqual(work_keys[rows[0]["平台作品键"]], work_keys[rows[1]["平台作品键"]])
        self.assertEqual(work_keys[rows[0]["平台作品键"]], work_keys[rows[2]["平台作品键"]])

        merged = merge_channels.assign_group_ids(
            merge_channels.pd.DataFrame([{"标题": row["标题"]} for row in rows])
        )
        self.assertEqual(merge_channels.TITLE_SIMILARITY_THRESHOLD, 0.40)
        self.assertEqual(merged.loc[0, "group_id"], merged.loc[1, "group_id"])
        self.assertEqual(merged.loc[0, "group_id"], merged.loc[2, "group_id"])

    def test_merge_channels_group_ids_follow_same_episode_rules(self):
        df = merge_channels.pd.DataFrame(
            [
                {"标题": "Harness，到底是个啥？（上）【小包必会】"},
                {"标题": "Harness，到底是个啥？（上）"},
                {"标题": "Harness，到底是个啥？（下）【小包必会】"},
                {"标题": "Harness，到底是个啥？（下）"},
            ]
        )

        grouped = merge_channels.assign_group_ids(df)

        self.assertEqual(grouped.loc[0, "group_id"], grouped.loc[1, "group_id"])
        self.assertEqual(grouped.loc[2, "group_id"], grouped.loc[3, "group_id"])
        self.assertNotEqual(grouped.loc[0, "group_id"], grouped.loc[2, "group_id"])


class FeishuCliPayloadTests(unittest.TestCase):
    def test_cli_http_json_uses_relative_temp_file_for_large_payload(self):
        recorded = {}

        def fake_run(cmd, timeout=120, cwd=None):
            recorded["cmd"] = list(cmd)
            recorded["cwd"] = Path(cwd)
            data_index = cmd.index("--data")
            payload_path = recorded["cwd"] / cmd[data_index + 1][3:]
            recorded["payload_path"] = payload_path
            recorded["payload_mode"] = payload_path.stat().st_mode & 0o777
            recorded["payload_text"] = payload_path.read_text(encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

        payload = {"records": [{"fields": {"标题": "x" * 6000}}]}

        source_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            cli_temp_dir = Path(temp_dir) / "state" / ".auth" / "lark-cli-tmp"
            with (
                mock.patch.object(feishu_sync, "LARK_CLI_TEMP_DIR", cli_temp_dir),
                mock.patch.object(feishu_sync, "resolve_lark_cli_runner", return_value=["lark-cli"]),
                mock.patch.object(feishu_sync, "run_lark_cli_with_user_fallback", side_effect=fake_run),
            ):
                result = feishu_sync.cli_http_json("POST", "/bitable/v1/fake", payload=payload)

        self.assertEqual(result, {})
        cmd = recorded["cmd"]
        data_index = cmd.index("--data")
        data_value = cmd[data_index + 1]
        self.assertTrue(data_value.startswith("@./"), data_value)
        self.assertEqual(recorded["cwd"], cli_temp_dir)
        self.assertNotEqual(recorded["cwd"], source_cwd)
        if os.name != "nt":  # Windows chmod 仅只读位语义，无 0o600
            self.assertEqual(recorded["payload_mode"], 0o600)
        self.assertEqual(json.loads(recorded["payload_text"]), payload)
        self.assertFalse(recorded["payload_path"].exists(), recorded["payload_path"])
        self.assertEqual(list(source_cwd.glob(".lark-cli-json-*.json")), [])

    def test_cli_http_json_keeps_small_payload_inline(self):
        recorded = {}

        def fake_run(cmd, timeout=120, cwd=None):
            recorded["cmd"] = list(cmd)
            recorded["cwd"] = cwd
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"ok": True}), stderr="")

        payload = {"hello": "world"}

        with mock.patch.object(feishu_sync, "resolve_lark_cli_runner", return_value=["lark-cli"]):
            with mock.patch.object(feishu_sync, "run_lark_cli_with_user_fallback", side_effect=fake_run):
                result = feishu_sync.cli_http_json("POST", "/bitable/v1/fake", payload=payload)

        self.assertEqual(result, {"ok": True})
        cmd = recorded["cmd"]
        data_index = cmd.index("--data")
        self.assertEqual(cmd[data_index + 1], json.dumps(payload, ensure_ascii=False))
        self.assertIsNone(recorded["cwd"])

    def test_default_env_file_stays_inside_project_state(self):
        self.assertEqual(feishu_sync.DEFAULT_ENV_PATH, feishu_sync.AUTH_DIR / "feishu.env")
        self.assertNotIn(".cherrystudio", str(feishu_sync.DEFAULT_ENV_PATH))


if __name__ == "__main__":
    unittest.main()
