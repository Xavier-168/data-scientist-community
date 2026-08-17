import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd

# 嵌入式 Python（._pth）不会把脚本目录加进 sys.path，需自举以支持同目录导入
_SELF_DIR = os.path.dirname(os.path.abspath(__file__))
if _SELF_DIR not in sys.path:
    sys.path.insert(0, _SELF_DIR)


DEFAULT_COLUMNS = [
    "作品ID",
    "标题",
    "发布日期",
    "曝光量",
    "阅读量",
    "封面点击率",
    "点赞量",
    "收藏量",
    "评论量",
    "分享量",
    "涨粉量",
    "弹幕量",
    "平均观看时长",
    "平均播放时长",
    "完播率",
    "2s跳出率",
    "跳出率口径",
    "内容类型",
    "链接",
    "详情采集状态",
    "详情采集错误",
    "平台",
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        rows = []
    else:
        with input_path.open("r", encoding="utf-8") as f:
            rows = json.load(f)

    if not isinstance(rows, list):
        rows = []

    normalized_rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized = {key: row.get(key, "") for key in DEFAULT_COLUMNS}
        for key, value in row.items():
            if key not in normalized:
                normalized[key] = value
        normalized_rows.append(normalized)

    if normalized_rows:
        df = pd.DataFrame(normalized_rows)
        for col in reversed(DEFAULT_COLUMNS):
            if col in df.columns:
                columns = [c for c in df.columns if c != col]
                columns.insert(0, col)
                df = df[columns]

        if "发布日期" in df.columns:
            df["__ts"] = pd.to_datetime(df["发布日期"], errors="coerce")
            df = df.sort_values("__ts", ascending=True).drop(columns=["__ts"])

        if "作品ID" in df.columns:
            df["作品ID"] = df["作品ID"].astype(str).str.replace(r"\.0$", "", regex=True)
            df = df.drop_duplicates(subset=["作品ID"], keep="last")

        df = df.fillna("")
    else:
        df = pd.DataFrame(columns=DEFAULT_COLUMNS)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(output_path, index=False)


if __name__ == "__main__":
    main()
