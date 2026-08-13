#!/usr/bin/env python3
import argparse
import json
import re
import warnings
from pathlib import Path
from typing import Optional

import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")


COUNT_FIELDS = ["播放量", "点赞量", "评论量", "分享量", "收藏量", "涨粉量"]
PERCENT_FIELDS = ["封面点击率", "2s跳出率", "5s完播率", "完播率"]


def clean_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null", "--", "-"}:
        return ""
    return re.sub(r"\s+", " ", text)


def parse_number(value) -> Optional[float]:
    text = clean_value(value).replace(",", "").replace("%", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def format_number(value: float) -> str:
    rounded = round(float(value), 2)
    if rounded.is_integer():
        return str(int(rounded))
    return str(rounded)


def format_percent(value: float) -> str:
    return f"{format_number(value)}%"


def weighted_percent(df: pd.DataFrame, field: str) -> str:
    if field not in df.columns:
        return ""
    total_weight = 0.0
    weighted_sum = 0.0
    for _, row in df.iterrows():
        value = parse_number(row.get(field))
        if value is None:
            continue
        weight = parse_number(row.get("播放量")) or 0.0
        if weight <= 0:
            continue
        total_weight += weight
        weighted_sum += value * weight
    if total_weight <= 0:
        values = [parse_number(item) for item in df[field].tolist()]
        values = [item for item in values if item is not None]
        if not values:
            return ""
        return format_percent(values[-1])
    return format_percent(weighted_sum / total_weight)


def normalize_detail_export(path: Path) -> dict:
    xls = pd.ExcelFile(path)
    frames = []
    for sheet in xls.sheet_names:
        frame = pd.read_excel(path, sheet_name=sheet).fillna("")
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return {}

    df = pd.concat(frames, ignore_index=True)
    metrics = {}

    for field in COUNT_FIELDS:
        if field not in df.columns:
            continue
        total = 0.0
        for value in df[field].tolist():
            total += parse_number(value) or 0.0
        metrics[field] = format_number(total)

    for field in PERCENT_FIELDS:
        value = weighted_percent(df, field)
        if value:
            metrics[field] = value

    if "2s跳出率" in metrics:
        metrics["跳出率口径"] = "2s"

    metrics["快手详情导出行数"] = str(len(df))
    return metrics


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"快手详情导出文件不存在：{input_path}")

    metrics = normalize_detail_export(input_path)
    text = json.dumps(metrics, ensure_ascii=False, indent=2)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
