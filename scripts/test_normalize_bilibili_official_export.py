import pathlib
import sys
import unittest

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from normalize_bilibili_official_export import normalize_rows, validate_required_metric_columns


class NormalizeBilibiliOfficialExportTests(unittest.TestCase):
    def test_required_metric_columns_accept_official_aliases(self):
        validate_required_metric_columns(
            pd.DataFrame(
                [["作品A", "2026-07-01", "20%", "30%"]],
                columns=["视频标题", "发布时间", "封面点击率", "3s跳出率"],
            ),
            ["封标点击率", "3秒跳出率"],
            "batch-1.csv",
        )

    def test_required_metric_columns_reject_each_incomplete_batch(self):
        with self.assertRaisesRegex(ValueError, "batch-2.csv.*3秒跳出率"):
            validate_required_metric_columns(
                pd.DataFrame(
                    [["作品A", "2026-07-01", "20%"]],
                    columns=["视频标题", "发布时间", "封面点击率"],
                ),
                ["封标点击率", "3秒跳出率"],
                "batch-2.csv",
            )

    def test_required_metric_columns_reject_all_blank_values_per_batch(self):
        with self.assertRaisesRegex(ValueError, "batch-3.csv.*封标点击率.*整批无有效值"):
            validate_required_metric_columns(
                pd.DataFrame(
                    [["作品A", "2026-07-01", "", "30%"]],
                    columns=["视频标题", "发布时间", "封面点击率", "3s跳出率"],
                ),
                ["封标点击率", "3秒跳出率"],
                "batch-3.csv",
            )

    def test_completion_rate_uses_official_completion_field(self):
        rows = normalize_rows(
            pd.DataFrame(
                [
                    {
                        "视频标题": "作品A",
                        "发布时间": "2026-07-01 10:00:00",
                        "平均播放进度": "60%",
                        "完播率": "20%",
                    }
                ]
            ),
            "",
            "",
        )

        self.assertEqual(rows[0]["平均播放进度"], "60%")
        self.assertEqual(rows[0]["完播率"], "20%")

    def test_missing_completion_rate_stays_blank(self):
        rows = normalize_rows(
            pd.DataFrame(
                [
                    {
                        "视频标题": "作品A",
                        "发布时间": "2026-07-01 10:00:00",
                        "平均播放进度": "60%",
                    }
                ]
            ),
            "",
            "",
        )

        self.assertEqual(rows[0]["完播率"], "")


if __name__ == "__main__":
    unittest.main()
