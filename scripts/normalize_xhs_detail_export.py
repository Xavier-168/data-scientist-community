#!/usr/bin/env python3
import argparse
import json
import re
import warnings
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")


METRIC_ALIASES = {
    "曝光数": "曝光量",
    "观看数": "阅读量",
    "封面点击率": "封面点击率",
    "平均观看时长": "平均观看时长",
    "完播率": "完播率",
    "2秒退出率": "2s跳出率",
    "2s退出率": "2s跳出率",
    "涨粉数": "涨粉量",
    "点赞数": "点赞量",
    "评论数": "评论量",
    "收藏数": "收藏量",
    "分享数": "分享量",
    "弹幕数": "弹幕量",
    "曝光数粉丝占比": "曝光粉丝占比",
    "观看数粉丝占比": "观看粉丝占比",
    "封面点击率粉丝占比": "封面点击率粉丝占比",
    "平均观看时长粉丝占比": "平均观看时长粉丝占比",
    "完播率粉丝占比": "完播率粉丝占比",
    "2秒退出率粉丝占比": "2s跳出率粉丝占比",
    "涨粉数粉丝占比": "涨粉粉丝占比",
    "点赞数粉丝占比": "点赞粉丝占比",
    "评论数粉丝占比": "评论粉丝占比",
    "收藏数粉丝占比": "收藏粉丝占比",
    "分享数粉丝占比": "分享粉丝占比",
    "弹幕数粉丝占比": "弹幕粉丝占比",
}


def clean_value(value):
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return re.sub(r"\s+", " ", text)


def normalize_metric_name(value):
    text = clean_value(value)
    text = text.replace("（", "(").replace("）", ")")
    text = re.sub(r"\(.*?\)", "", text)
    text = re.sub(r"\s+", "", text)
    return METRIC_ALIASES.get(text, text)


def normalize_number(value):
    text = clean_value(value).replace(",", "")
    if not text:
        return ""
    suffix = ""
    if text.endswith("%"):
        suffix = "%"
        text = text[:-1]
    elif text.lower().endswith("s"):
        suffix = "s"
        text = text[:-1]
    try:
        number = float(text)
    except ValueError:
        return clean_value(value)
    if number.is_integer():
        number_text = str(int(number))
    else:
        number_text = str(number)
    return f"{number_text}{suffix}" if suffix else number_text


def read_overview_sheet(path: Path) -> pd.DataFrame:
    try:
        return pd.read_excel(path, sheet_name="基础数据总览", header=None).fillna("")
    except ValueError:
        return pd.read_excel(path, sheet_name=0, header=None).fillna("")


def normalize_detail_export(path: Path) -> dict:
    df = read_overview_sheet(path)
    metrics = {}
    for _, row in df.iterrows():
        if len(row) < 2:
            continue
        name = normalize_metric_name(row.iloc[0])
        value = normalize_number(row.iloc[1])
        if not name or name == "指标":
            continue
        metrics[name] = value

    if "平均观看时长" in metrics:
        metrics.setdefault("平均播放时长", metrics["平均观看时长"])
    if "2s跳出率" in metrics:
        metrics.setdefault("跳出率口径", "2s")
    return metrics


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"小红书详情导出文件不存在：{input_path}")

    metrics = normalize_detail_export(input_path)
    text = json.dumps(metrics, ensure_ascii=False, indent=2)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
