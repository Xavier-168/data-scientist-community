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


PIN_COLUMNS = [
    "平台",
    "作品ID",
    "标题",
    "发布日期",
]


def normalize_value(value):
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def parse_columns_arg(text: str | None):
    if not text:
        return None
    cols = [c.strip() for c in text.split(",") if c.strip()]
    return cols or None


def build_column_order(df: pd.DataFrame, columns_override: list[str] | None):
    if columns_override:
        # Keep only columns present; append any missing columns from df at the end.
        cols = [c for c in columns_override if c in df.columns]
        for c in df.columns:
            if c not in cols:
                cols.append(c)
        return cols

    cols = list(df.columns)

    # Pin key columns to the front (if present).
    for key in reversed(PIN_COLUMNS):
        if key in cols:
            cols.remove(key)
            cols.insert(0, key)

    return cols


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--columns", default=None)
    parser.add_argument("--dedupe-key", default="作品ID")
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
        normalized_rows.append({k: normalize_value(v) for k, v in row.items()})

    if normalized_rows:
        df = pd.DataFrame(normalized_rows)

        if "发布日期" in df.columns:
            df["__ts"] = pd.to_datetime(df["发布日期"], errors="coerce")
            df = df.sort_values("__ts", ascending=True).drop(columns=["__ts"])

        dedupe_key = args.dedupe_key
        if dedupe_key and dedupe_key in df.columns:
            df[dedupe_key] = df[dedupe_key].astype(str).str.replace(r"\.0$", "", regex=True)
            df = df.drop_duplicates(subset=[dedupe_key], keep="last")

        df = df.fillna("")

        columns_override = parse_columns_arg(args.columns)
        df = df[build_column_order(df, columns_override)]
    else:
        df = pd.DataFrame(columns=parse_columns_arg(args.columns) or PIN_COLUMNS)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(output_path, index=False)


if __name__ == "__main__":
    main()
