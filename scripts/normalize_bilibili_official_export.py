#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
from pathlib import Path

import pandas as pd


OFFICIAL_TO_INTERNAL = {
    "视频标题": "标题",
    "稿件名称": "标题",
    "标题": "标题",
    "发布时间": "发布时间",
    "发布日期": "发布时间",
    "播放量": "播放量",
    "游客播放占比": "游客播放占比",
    "粉丝观看率": "粉丝观看率",
    "封标点击率": "封面点击率",
    "封面点击率": "封面点击率",
    "3秒跳出率": "3s跳出率",
    "3s跳出率": "3s跳出率",
    "涨粉量": "涨粉量",
    "点赞量": "点赞量",
    "评论量": "评论量",
    "弹幕量": "弹幕量",
    "收藏量": "收藏量",
    "投币量": "投币量",
    "转发量": "分享量",
    "分享量": "分享量",
    "平均播放进度": "平均播放占比",
    "平均播放占比": "平均播放占比",
    "完播率": "完播率",
    "视频完播率": "完播率",
    "播放完成率": "完播率",
}

REQUIRED_METRIC_ALIASES = {
    "封标点击率": {"封标点击率", "封面点击率"},
    "封面点击率": {"封标点击率", "封面点击率"},
    "3秒跳出率": {"3秒跳出率", "3s跳出率"},
    "3s跳出率": {"3秒跳出率", "3s跳出率"},
}

# B 站官方稿件对比导出没有 bvid/avid；保持 title + publish_at 生成稳定本地 ID。
OUTPUT_COLUMNS = [
    "平台",
    "作品ID",
    "标题",
    "发布日期",
    "发布时间",
    "播放量",
    "游客播放占比",
    "粉丝观看率",
    "粉丝播放占比",
    "封标点击率",
    "封面点击率",
    "3秒跳出率",
    "3s跳出率",
    "涨粉量",
    "点赞量",
    "评论量",
    "弹幕量",
    "收藏量",
    "投币量",
    "转发量",
    "分享量",
    "平均播放进度",
    "平均播放占比",
    "完播率",
    "内容类型",
]


def clean_value(value):
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return re.sub(r"\s+", " ", text)


def normalize_header(value):
    return clean_value(value).replace("\ufeff", "").replace("\u200b", "")


def normalize_publish_text(value):
    text = clean_value(value)
    if not text:
        return "", pd.NaT

    match = re.search(
        r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})[日]?\s*(\d{1,2}:\d{2}(?::\d{2})?)?",
        text,
    )
    if match:
        year, month, day, clock = match.groups()
        clock = clock or "00:00:00"
        if len(clock.split(":")) == 2:
            clock = f"{clock}:00"
        normalized = f"{int(year):04d}-{int(month):02d}-{int(day):02d} {clock}"
    else:
        normalized = text

    ts = pd.to_datetime(normalized, errors="coerce")
    if pd.isna(ts):
        return text, pd.NaT
    return ts.strftime("%Y-%m-%d %H:%M:%S"), ts


def stable_work_id(title, publish_at):
    seed = f"{clean_value(title)}|{clean_value(publish_at)}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    return f"bili-{digest}"


def read_official_export(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(path, dtype=str).fillna("")

    errors = []
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return pd.read_csv(path, dtype=str, encoding=encoding).fillna("")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{encoding}: {exc}")
    raise RuntimeError(f"无法读取 B 站官方导出文件：{path}；" + " | ".join(errors))


def to_number_text(value):
    text = clean_value(value)
    if not text:
        return "0"
    compact = text.replace(",", "").replace("%", "")
    try:
        number = float(compact)
    except ValueError:
        return text
    if number.is_integer():
        return str(int(number))
    return str(number)


def normalize_cover_click_rate(value):
    text = clean_value(value)
    if not text:
        return ""
    star_match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*星", text)
    if not star_match:
        return text
    percent = float(star_match.group(1)) / 5 * 100
    if percent.is_integer():
        return f"{int(percent)}%"
    return f"{round(percent, 2):g}%"


def validate_required_metric_columns(df: pd.DataFrame, required_metrics, source_name=""):
    normalized_column_pairs = [
        (index, OFFICIAL_TO_INTERNAL.get(normalize_header(column), normalize_header(column)))
        for index, column in enumerate(df.columns)
    ]
    missing = []
    empty = []
    for metric in required_metrics or []:
        aliases = REQUIRED_METRIC_ALIASES.get(metric, {metric})
        normalized_aliases = {
            OFFICIAL_TO_INTERNAL.get(normalize_header(alias), normalize_header(alias))
            for alias in aliases
        }
        matching_indexes = [
            index
            for index, normalized_column in normalized_column_pairs
            if normalized_column in normalized_aliases
        ]
        if not matching_indexes:
            missing.append(metric)
            continue
        if not any(
            clean_value(value)
            for index in matching_indexes
            for value in df.iloc[:, index].tolist()
        ):
            empty.append(metric)
    if missing or empty:
        label = source_name or "官方导出文件"
        problems = []
        if missing:
            problems.append(f"缺少 {'、'.join(missing)}")
        if empty:
            problems.append(f"{'、'.join(empty)} 整批无有效值")
        raise ValueError(f"B 站官方导出字段异常：{label} {'；'.join(problems)}")


def normalize_rows(df: pd.DataFrame, min_date: str, max_date: str):
    rename_map = {col: OFFICIAL_TO_INTERNAL.get(normalize_header(col), normalize_header(col)) for col in df.columns}
    normalized_df = df.rename(columns=rename_map)
    min_ts = pd.to_datetime(min_date, errors="coerce") if min_date else pd.NaT
    max_ts = pd.to_datetime(max_date, errors="coerce") if max_date else pd.NaT

    rows = []
    for _, source in normalized_df.iterrows():
        title = clean_value(source.get("标题"))
        publish_at, publish_ts = normalize_publish_text(source.get("发布时间") or source.get("发布日期"))
        if not title or pd.isna(publish_ts):
            continue
        if not pd.isna(min_ts) and publish_ts.normalize() < min_ts.normalize():
            continue
        if not pd.isna(max_ts) and publish_ts.normalize() > max_ts.normalize():
            continue

        cover_click_rate = normalize_cover_click_rate(source.get("封面点击率"))
        avg_playback_progress = clean_value(source.get("平均播放占比"))
        completion_rate = clean_value(source.get("完播率"))

        row = {
            "平台": "bilibili",
            "作品ID": stable_work_id(title, publish_at),
            "标题": title,
            "发布日期": publish_ts.strftime("%Y-%m-%d"),
            "发布时间": publish_at,
            "播放量": to_number_text(source.get("播放量")),
            "游客播放占比": clean_value(source.get("游客播放占比")),
            "粉丝观看率": clean_value(source.get("粉丝观看率")),
            "粉丝播放占比": clean_value(source.get("粉丝观看率") or source.get("粉丝播放占比")),
            "封标点击率": cover_click_rate,
            "封面点击率": cover_click_rate,
            "3秒跳出率": clean_value(source.get("3s跳出率")),
            "3s跳出率": clean_value(source.get("3s跳出率")),
            "涨粉量": to_number_text(source.get("涨粉量")),
            "点赞量": to_number_text(source.get("点赞量")),
            "评论量": to_number_text(source.get("评论量")),
            "弹幕量": to_number_text(source.get("弹幕量")),
            "收藏量": to_number_text(source.get("收藏量")),
            "投币量": to_number_text(source.get("投币量")),
            "转发量": to_number_text(source.get("分享量")),
            "分享量": to_number_text(source.get("分享量")),
            "平均播放进度": avg_playback_progress,
            "平均播放占比": avg_playback_progress,
            "完播率": completion_rate,
            "内容类型": "video",
        }
        rows.append(row)

    rows.sort(key=lambda item: (item["发布日期"], item["发布时间"], item["标题"]))
    return rows


def dedupe_rows(rows):
    # 多批官方 CSV 会在日期边界或重跑时重复，按稳定作品 ID 保留最后一次读取值。
    by_id = {}
    for row in rows:
        by_id[row["作品ID"]] = row
    return sorted(by_id.values(), key=lambda item: (item["发布日期"], item["发布时间"], item["标题"]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, nargs="+")
    parser.add_argument("--rows-output", required=True)
    parser.add_argument("--excel-output", required=True)
    parser.add_argument("--min-date", default="")
    parser.add_argument("--max-date", default="")
    parser.add_argument("--required-metric", action="append", default=[])
    args = parser.parse_args()

    all_rows = []
    for raw_input in args.input:
        input_path = Path(raw_input)
        if not input_path.exists():
            raise FileNotFoundError(f"B 站官方导出文件不存在：{input_path}")
        official_df = read_official_export(input_path)
        validate_required_metric_columns(official_df, args.required_metric, input_path.name)
        all_rows.extend(normalize_rows(official_df, args.min_date, args.max_date))

    rows = dedupe_rows(all_rows)
    rows_output = Path(args.rows_output)
    excel_output = Path(args.excel_output)
    rows_output.parent.mkdir(parents=True, exist_ok=True)
    excel_output.parent.mkdir(parents=True, exist_ok=True)

    rows_output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(rows, columns=OUTPUT_COLUMNS).to_excel(excel_output, index=False)
    print(json.dumps({"rows": len(rows), "rows_output": str(rows_output), "excel_output": str(excel_output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
