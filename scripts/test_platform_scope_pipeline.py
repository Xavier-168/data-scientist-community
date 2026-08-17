import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


ROOT = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent

MERGE_MODULE_PATH = ROOT / "merge_channels.py"
MERGE_SPEC = importlib.util.spec_from_file_location("merge_channels_module", MERGE_MODULE_PATH)
merge_channels = importlib.util.module_from_spec(MERGE_SPEC)
assert MERGE_SPEC and MERGE_SPEC.loader
MERGE_SPEC.loader.exec_module(merge_channels)

BUILD_EXCEL_MODULE_PATH = ROOT / "build_excel_export.py"
BUILD_EXCEL_SPEC = importlib.util.spec_from_file_location("build_excel_export_module", BUILD_EXCEL_MODULE_PATH)
build_excel_export = importlib.util.module_from_spec(BUILD_EXCEL_SPEC)
assert BUILD_EXCEL_SPEC and BUILD_EXCEL_SPEC.loader
BUILD_EXCEL_SPEC.loader.exec_module(build_excel_export)

SOURCE_ROWS_MODULE_PATH = ROOT / "platform_source_rows.py"
SOURCE_ROWS_SPEC = importlib.util.spec_from_file_location("platform_source_rows_module", SOURCE_ROWS_MODULE_PATH)
platform_source_rows = importlib.util.module_from_spec(SOURCE_ROWS_SPEC)
assert SOURCE_ROWS_SPEC and SOURCE_ROWS_SPEC.loader
SOURCE_ROWS_SPEC.loader.exec_module(platform_source_rows)


class SourceRowDateWindowTests(unittest.TestCase):
    def test_hashtag_only_title_is_preserved_instead_of_becoming_blank(self):
        self.assertEqual(
            platform_source_rows.normalize_title("#ai #AI新星计划 #教程"),
            "#ai #AI新星计划 #教程",
        )

    def test_plain_title_still_drops_trailing_hashtags(self):
        self.assertEqual(
            platform_source_rows.normalize_title("普通正文 #ai #教程"),
            "普通正文",
        )

    def test_programming_language_hash_is_not_mistaken_for_a_topic(self):
        self.assertEqual(
            platform_source_rows.normalize_title("C#入门指南 #教程"),
            "C#入门指南",
        )

    def test_unknown_date_is_excluded_when_min_date_is_set(self):
        rows = platform_source_rows.filter_by_min_date(
            [{"作品ID": "1", "发布日期": ""}],
            "2026-01-01",
            "",
        )

        self.assertEqual(rows, [])

    def test_unknown_date_is_excluded_when_max_date_is_set(self):
        rows = platform_source_rows.filter_by_min_date(
            [{"作品ID": "1", "发布日期": "unknown-date"}],
            "",
            "2026-07-10",
        )

        self.assertEqual(rows, [])

    def test_unknown_date_is_kept_without_date_window(self):
        source = [{"作品ID": "1", "发布日期": ""}]

        rows = platform_source_rows.filter_by_min_date(source, "", "")

        self.assertEqual(rows, source)

class MergeChannelsPlatformScopeTests(unittest.TestCase):
    def test_build_all_channels_filters_to_requested_platforms(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = pathlib.Path(temp_dir)
            douyin_path = temp_root / "douyin.xlsx"
            xhs_path = temp_root / "xhs.xlsx"

            pd.DataFrame(
                [
                    {
                        "作品ID": "dy-1",
                        "标题": "抖音作品",
                        "发布日期": "2026-04-01",
                        "播放量": "100",
                        "点赞量": "10",
                        "收藏量": "5",
                        "评论量": "2",
                        "分享量": "1",
                        "内容类型": "video",
                    }
                ]
            ).to_excel(douyin_path, index=False)
            pd.DataFrame(
                [
                    {
                        "作品ID": "xhs-1",
                        "标题": "小红书作品",
                        "发布日期": "2026-04-01",
                        "播放量": "200",
                        "点赞量": "20",
                        "收藏量": "10",
                        "评论量": "4",
                        "分享量": "2",
                        "内容类型": "image_text",
                    }
                ]
            ).to_excel(xhs_path, index=False)

            merged = merge_channels.build_all_channels(
                str(douyin_path),
                str(xhs_path),
                "",
                "",
                "",
                "",
                platforms="douyin",
            )

        self.assertEqual(merged["平台"].tolist(), ["douyin"])
        self.assertEqual(merged["作品ID"].tolist(), ["dy-1"])


class BuildExcelPlatformScopeTests(unittest.TestCase):
    def test_build_all_platform_excel_filters_source_rows_by_requested_platforms(self):
        observed_platforms = []

        def capture_detail_rows(source_rows, meta):
            # 捕获进入 build_detail_rows_v2 的 source_rows 平台，这是本测试的核心断言对象
            observed_platforms.append([row.get("平台") for row in source_rows])
            # 返回一条非空 detail 行（带落在 min_date 窗口内的发布日期），
            # 让函数通过 filter_rows_by_date_window 守卫，否则会被
            # no_data_after_date_filter 拦截（毫秒时间戳会被 date_only 转成
            # 实际日期，必须晚于 min_date=2026-01-01）
            return [{"视频标题": "抖音作品", "视频平台": "抖音", "视频发布日期": "2026-04-08"}]

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = pathlib.Path(temp_dir) / "scoped.xlsx"
            with (
                patch.object(
                    build_excel_export,
                    "resolve_snapshot_meta",
                    return_value=SimpleNamespace(
                        generated_at="2026-04-08 22:19:10",
                        snapshot_date="2026-04-08",
                        generated_at_ms=1760000000000,
                        snapshot_date_ms=1760000000000,
                        batch_id="sync-v2-20260408-221910",
                    ),
                ),
                patch.object(
                    build_excel_export,
                    "build_source_rows",
                    return_value=[
                        {"平台": "douyin", "平台作品键": "douyin:1", "作品ID": "1", "标题": "抖音作品", "发布日期": "2026-04-01", "播放量": "100"},
                        {"平台": "xiaohongshu", "平台作品键": "xiaohongshu:1", "作品ID": "1", "标题": "小红书作品", "发布日期": "2026-04-01", "播放量": "200"},
                    ],
                ),
                patch.object(build_excel_export, "build_detail_rows_v2", side_effect=capture_detail_rows),
                patch.object(build_excel_export, "build_work_rows_v2", return_value=[]),
                patch.object(build_excel_export, "build_chart_rows_v1", return_value=[]),
                patch.object(build_excel_export, "build_metric_snapshot_rows", return_value=[]),
                patch.object(build_excel_export, "load_previous_v2_snapshot", return_value=("", [])),
                patch.object(build_excel_export, "load_previous_from_analytics_db", return_value=("", [])),
                patch.object(build_excel_export, "build_increment_rows_v1", return_value=[]),
            ):
                result = build_excel_export.build_all_platform_excel(
                    "2026-01-01",
                    "",
                    str(output_path),
                    platforms="douyin",
                )

        self.assertTrue(result["ok"])
        self.assertEqual(observed_platforms, [["douyin"]])


class IsolatedStateDirectoryPipelineTests(unittest.TestCase):
    @staticmethod
    def _write_douyin_fixture(downloads_dir: pathlib.Path) -> None:
        downloads_dir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            [
                {
                    "作品ID": "dy-isolated-1",
                    "标题": "隔离状态目录测试作品",
                    "发布日期": "2026-04-01",
                    "播放量": "100",
                    "点赞量": "10",
                    "收藏量": "5",
                    "评论量": "2",
                    "分享量": "1",
                    "内容类型": "video",
                }
            ]
        ).to_excel(downloads_dir / "all_videos.xlsx", index=False)

    def _subprocess_env(self, base_dir: pathlib.Path, state_dir: pathlib.Path) -> dict:
        env = os.environ.copy()
        env["YIRENGONGIS_BASE_DIR"] = str(base_dir)
        env["YIRENGONGIS_STATE_DIR"] = str(state_dir)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        return env

    def _run_script(
        self,
        script_name: str,
        args: list[str],
        *,
        base_dir: pathlib.Path,
        state_dir: pathlib.Path,
    ) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [sys.executable, str(ROOT / script_name), *args],
            cwd=PROJECT_ROOT,
            env=self._subprocess_env(base_dir, state_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"{script_name} failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        return result

    def test_merge_defaults_to_isolated_state_downloads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = pathlib.Path(temp_dir)
            base_dir = temp_root / "base"
            state_dir = temp_root / "state"
            base_dir.mkdir()
            self._write_douyin_fixture(state_dir / "downloads")

            self._run_script(
                "merge_channels.py",
                ["--platforms", "douyin", "--min-date", "2026-01-01"],
                base_dir=base_dir,
                state_dir=state_dir,
            )

            state_output = state_dir / "downloads" / "all_channels_videos.xlsx"
            self.assertTrue(state_output.exists())
            self.assertEqual(len(pd.read_excel(state_output)), 1)
            self.assertFalse((base_dir / "downloads" / "all_channels_videos.xlsx").exists())

    def test_excel_and_feishu_prepare_read_isolated_state_downloads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = pathlib.Path(temp_dir)
            base_dir = temp_root / "base"
            state_dir = temp_root / "state"
            downloads_dir = state_dir / "downloads"
            base_dir.mkdir()
            self._write_douyin_fixture(downloads_dir)

            excel_output = downloads_dir / "isolated-export.xlsx"
            self._run_script(
                "build_excel_export.py",
                [
                    "--mode",
                    "all",
                    "--platforms",
                    "douyin",
                    "--min-date",
                    "2026-01-01",
                    "--output",
                    str(excel_output),
                ],
                base_dir=base_dir,
                state_dir=state_dir,
            )
            self.assertEqual(len(pd.read_excel(excel_output, sheet_name="平台明细")), 1)

            payload_output = downloads_dir / "isolated-feishu-payload.json"
            pending_output = downloads_dir / "isolated-feishu-pending.json"
            self._run_script(
                "prepare_feishu_bitable_sync_v2.py",
                [
                    "--platforms",
                    "douyin",
                    "--min-date",
                    "2026-01-01",
                    "--output",
                    str(payload_output),
                    "--pending-snapshot",
                    str(pending_output),
                    "--baseline-only",
                ],
                base_dir=base_dir,
                state_dir=state_dir,
            )
            payload = json.loads(payload_output.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["tables"]["平台明细V2"]), 1)
            self.assertEqual(len(payload["tables"]["作品总表V2"]), 1)
            self.assertFalse((base_dir / "downloads").exists())

if __name__ == "__main__":
    unittest.main()
