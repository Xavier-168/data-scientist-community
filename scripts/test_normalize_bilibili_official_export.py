import pathlib
import sys
import unittest

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from normalize_bilibili_official_export import normalize_rows


class NormalizeBilibiliOfficialExportTests(unittest.TestCase):
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
