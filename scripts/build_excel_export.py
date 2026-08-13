#!/usr/bin/env python3
"""Build enriched multi-sheet Excel exports that match Feishu sync data quality.

Reuses all enrichment functions from prepare_feishu_bitable_sync_v2.py.
Produces:
  - All-platform: 5-sheet Excel (平台明细, 作品总表, 同步日志, 作品图表, 作品增量)
  - Single-platform: 1-sheet Excel with derived metrics
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

_SELF_DIR = os.path.dirname(os.path.abspath(__file__))
if _SELF_DIR not in sys.path:
    sys.path.insert(0, _SELF_DIR)

from platform_source_rows import (
    DOWNLOADS_DIR,
    clean_value,
    filter_by_min_date,
    filter_zero_play,
)
from prepare_feishu_bitable_sync_v2 import (
    ANALYTICS_DB_PATH,
    CHART_METRIC_FIELDS,
    PLATFORM_DETAIL_BUSINESS_FIELDS,
    TREND_DB_PATH,
    build_chart_rows_v1,
    build_detail_rows_v2,
    build_increment_rows_v1,
    build_metric_snapshot_rows,
    build_source_rows,
    build_sync_log_rows_v2,
    build_work_rows_v2,
    chart_platform_fields_for,
    filter_by_platforms,
    load_previous_from_analytics_db,
    load_previous_v2_snapshot,
    parse_platforms,
    resolve_snapshot_meta,
)

ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")

ENRICHED_ALL_FILE = DOWNLOADS_DIR / "all_channels_enriched.xlsx"
ENRICHED_PLATFORM_FILES = {
    "douyin": DOWNLOADS_DIR / "douyin_enriched.xlsx",
    "xiaohongshu": DOWNLOADS_DIR / "xiaohongshu_enriched.xlsx",
    "bilibili": DOWNLOADS_DIR / "bilibili_enriched.xlsx",
    "kuaishou": DOWNLOADS_DIR / "kuaishou_enriched.xlsx",
}

# Column ordering for each sheet
DETAIL_COLUMNS = list(PLATFORM_DETAIL_BUSINESS_FIELDS)

WORK_COLUMNS = [
    "标题", "首发日期", "内容类型", "覆盖平台", "覆盖平台数",
    "总播放量", "总点赞量", "总收藏量", "总评论量", "总分享量", "总涨粉量",
    "总互动量", "最近更新时间", "同步键",
]

INCREMENT_COLUMNS = [
    "标题", "首发日期", "覆盖平台", "覆盖平台数",
    "对比快照日期", "对比跨度天数",
    "总播放增量", "总点赞增量", "总收藏增量", "总评论增量", "总分享增量", "总涨粉增量",
    "总互动增量", "日均播放增量", "日均互动增量", "增量状态", "同步键",
]

SYNC_LOG_COLUMNS = [
    "同步日期", "纳入平台",
    "平台明细记录数", "作品总表记录数", "作品图表表记录数", "作品增量表记录数",
    "新增作品数", "持续增长作品数",
    "备注", "批次同步键",
]


def chart_columns(platforms: list[str] | None = None) -> list[str]:
    return [
        "日期",
        "标题",
        "首发日期",
        "内容类型",
        "覆盖平台",
        "覆盖平台数",
        *[
            f"{metric_name}_{suffix}"
            for _label, suffix in chart_platform_fields_for(platforms)
            for metric_name in CHART_METRIC_FIELDS
        ],
        "同步键",
    ]


# ---------------------------------------------------------------------------
# Feishu timestamp → human-readable string adapters
# ---------------------------------------------------------------------------

def ms_to_date_str(ms) -> str:
    if ms is None:
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=ASIA_SHANGHAI).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        return ""


def ms_to_datetime_str(ms) -> str:
    if ms is None:
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=ASIA_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError):
        return ""


def list_to_str(val) -> str:
    if isinstance(val, list):
        return " / ".join(str(v) for v in val)
    return str(val) if val else ""


def safe_rate(numerator, denominator, digits=2) -> float:
    den = float(denominator or 0)
    if den <= 0:
        return 0.0
    return round(float(numerator or 0) * 100.0 / den, digits)


def safe_rate_score(numerator, denominator) -> int:
    den = float(denominator or 0)
    if den <= 0:
        return 0
    return int(float(numerator or 0) * 100.0 / den + 0.5)


def date_only(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if abs(float(value)) > 100_000_000_000:
                return datetime.fromtimestamp(float(value) / 1000, tz=ASIA_SHANGHAI).date()
        except (ValueError, TypeError, OSError):
            return None
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.date()


def filter_rows_by_date_window(rows: list[dict], date_field: str, min_date: str, max_date: str = "") -> list[dict]:
    min_cutoff = date_only(min_date)
    max_cutoff = date_only(max_date)
    if min_cutoff is None and max_cutoff is None:
        return rows
    filtered = []
    for row in rows:
        row_date = date_only(row.get(date_field))
        if row_date is None:
            continue
        if min_cutoff is not None and row_date < min_cutoff:
            continue
        if max_cutoff is not None and row_date > max_cutoff:
            continue
        filtered.append(row)
    return filtered
# ---------------------------------------------------------------------------
# Row adapters: convert Feishu-format dicts to Excel-friendly dicts
# ---------------------------------------------------------------------------

def adapt_detail_rows(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        total_traffic = float(row.get("总流量") or row.get("播放量") or 0)
        avg_play_progress = row.get("平均播放进度")
        out.append({
            "视频标题": row.get("视频标题") or row.get("标题", ""),
            "视频平台": row.get("视频平台") or row.get("平台", ""),
            "视频发布日期": ms_to_date_str(row.get("视频发布日期") or row.get("发布日期")),
            "总流量": total_traffic,
            "点赞量": float(row.get("点赞量") or 0),
            "评论量": float(row.get("评论量") or 0),
            "分享量": float(row.get("分享量") or 0),
            "收藏量": float(row.get("收藏量") or 0),
            "涨粉量": float(row.get("涨粉量") or 0),
            "平均播放进度": avg_play_progress if avg_play_progress is not None else "",
            "完播率": float(row.get("完播率") or 0),
            "点赞率": float(row.get("点赞率") or 0),
            "评论率": float(row.get("评论率") or 0),
            "分享率": float(row.get("分享率") or 0),
            "收藏率": float(row.get("收藏率") or 0),
            "封标点击率": float(row.get("封标点击率") or row.get("封面点击率") or 0),
            "3s跳出率": row.get("3s跳出率") if row.get("3s跳出率") is not None else "",
        })
    return out


def adapt_work_rows(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        total_interaction = (
            float(row.get("总点赞量") or 0) + float(row.get("总收藏量") or 0)
            + float(row.get("总评论量") or 0) + float(row.get("总分享量") or 0)
        )
        out.append({
            "标题": row.get("标题", ""),
            "首发日期": ms_to_date_str(row.get("首发日期")),
            "内容类型": list_to_str(row.get("内容类型")),
            "覆盖平台": list_to_str(row.get("覆盖平台")),
            "覆盖平台数": row.get("覆盖平台数", 0),
            "总播放量": float(row.get("总播放量") or 0),
            "总点赞量": float(row.get("总点赞量") or 0),
            "总收藏量": float(row.get("总收藏量") or 0),
            "总评论量": float(row.get("总评论量") or 0),
            "总分享量": float(row.get("总分享量") or 0),
            "总涨粉量": float(row.get("总涨粉量") or 0),
            "总互动量": total_interaction,
            "最近更新时间": ms_to_datetime_str(row.get("最近更新时间")),
            "同步键": row.get("同步键", ""),
        })
    return out


def adapt_chart_rows(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        base_fields = {"日期", "标题", "首发日期", "内容类型", "覆盖平台", "覆盖平台数", "同步键"}
        adapted = {
            "日期": ms_to_date_str(row.get("日期")),
            "标题": row.get("标题", ""),
            "首发日期": ms_to_date_str(row.get("首发日期")),
            "内容类型": list_to_str(row.get("内容类型")),
            "覆盖平台": list_to_str(row.get("覆盖平台")),
            "覆盖平台数": row.get("覆盖平台数", 0),
            "同步键": row.get("同步键", ""),
        }
        for key, value in row.items():
            if key in base_fields:
                continue
            adapted[key] = value if value is not None else 0
        out.append(adapted)
    return out


def adapt_increment_rows(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        out.append({
            "标题": row.get("标题", ""),
            "首发日期": ms_to_date_str(row.get("首发日期")),
            "覆盖平台": list_to_str(row.get("覆盖平台")),
            "覆盖平台数": row.get("覆盖平台数", 0),
            "对比快照日期": ms_to_date_str(row.get("对比快照日期")),
            "对比跨度天数": row.get("对比跨度天数"),
            "总播放增量": row.get("总播放增量", 0),
            "总点赞增量": row.get("总点赞增量", 0),
            "总收藏增量": row.get("总收藏增量", 0),
            "总评论增量": row.get("总评论增量", 0),
            "总分享增量": row.get("总分享增量", 0),
            "总涨粉增量": row.get("总涨粉增量", 0),
            "总互动增量": row.get("总互动增量", 0),
            "日均播放增量": row.get("日均播放增量"),
            "日均互动增量": row.get("日均互动增量"),
            "增量状态": row.get("增量状态", ""),
            "同步键": row.get("同步键", ""),
        })
    return out


def adapt_sync_log_rows(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        out.append({
            "同步日期": ms_to_date_str(row.get("同步日期")),
            "纳入平台": row.get("纳入平台", ""),
            "平台明细记录数": row.get("平台明细记录数", 0),
            "作品总表记录数": row.get("作品总表记录数", 0),
            "作品图表表记录数": row.get("作品图表表记录数", 0),
            "作品增量表记录数": row.get("作品增量表记录数", 0),
            "新增作品数": row.get("新增作品数", 0),
            "持续增长作品数": row.get("持续增长作品数", 0),
            "备注": row.get("备注", ""),
            "批次同步键": row.get("批次同步键", ""),
        })
    return out


def ordered_df(rows: list[dict], columns: list[str]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(rows)
    existing = [c for c in columns if c in df.columns]
    extra = [c for c in df.columns if c not in columns]
    return df[existing + extra]


# ---------------------------------------------------------------------------
# Main builders
# ---------------------------------------------------------------------------

def build_all_platform_excel(
    min_date: str,
    max_date: str,
    output: str,
    platforms: str = "",
) -> dict:
    meta = resolve_snapshot_meta()
    requested_platforms = parse_platforms(platforms)
    source_rows = build_source_rows()
    source_rows = filter_by_platforms(source_rows, requested_platforms)
    source_rows = filter_by_min_date(source_rows, min_date, max_date)
    source_rows = filter_zero_play(source_rows)

    if not source_rows:
        return {"ok": False, "error": "no_data", "detail_count": 0}

    detail_rows = build_detail_rows_v2(source_rows, meta)
    work_rows = build_work_rows_v2(source_rows, meta)
    chart_platform_scope = requested_platforms or sorted(
        {clean_value(row.get("平台")) for row in source_rows if clean_value(row.get("平台"))}
    )
    chart_rows = build_chart_rows_v1(source_rows, meta, chart_platform_fields_for(chart_platform_scope))

    metric_rows = build_metric_snapshot_rows(source_rows)
    group_map = {clean_value(item["平台作品键"]): clean_value(item["作品组ID"]) for item in metric_rows}
    prev_at, prev_rows = load_previous_v2_snapshot(TREND_DB_PATH, meta.generated_at, group_map)
    if not prev_rows:
        prev_at, prev_rows = load_previous_from_analytics_db(ANALYTICS_DB_PATH, group_map)
    increment_rows = build_increment_rows_v1(metric_rows, prev_rows, prev_at, meta)

    detail_rows = filter_rows_by_date_window(detail_rows, "视频发布日期", min_date, max_date)
    work_rows = filter_rows_by_date_window(work_rows, "首发日期", min_date, max_date)
    chart_rows = filter_rows_by_date_window(chart_rows, "首发日期", min_date, max_date)
    increment_rows = filter_rows_by_date_window(increment_rows, "首发日期", min_date, max_date)
    if not detail_rows:
        return {"ok": False, "error": "no_data_after_date_filter", "detail_count": 0}

    sync_log_rows = build_sync_log_rows_v2(meta, detail_rows, work_rows, chart_rows, increment_rows)

    adapted_detail = adapt_detail_rows(detail_rows)
    adapted_work = adapt_work_rows(work_rows)
    adapted_chart = adapt_chart_rows(chart_rows)
    adapted_increment = adapt_increment_rows(increment_rows)
    adapted_sync_log = adapt_sync_log_rows(sync_log_rows)

    os.makedirs(os.path.dirname(output), exist_ok=True)
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        ordered_df(adapted_detail, DETAIL_COLUMNS).to_excel(writer, sheet_name="平台明细", index=False)
        ordered_df(adapted_work, WORK_COLUMNS).to_excel(writer, sheet_name="作品总表", index=False)
        ordered_df(adapted_sync_log, SYNC_LOG_COLUMNS).to_excel(writer, sheet_name="同步日志", index=False)
        if adapted_chart:
            ordered_df(adapted_chart, chart_columns(chart_platform_scope)).to_excel(writer, sheet_name="作品图表", index=False)
        if adapted_increment:
            ordered_df(adapted_increment, INCREMENT_COLUMNS).to_excel(writer, sheet_name="作品增量", index=False)

    return {
        "ok": True,
        "output": output,
        "detail_count": len(adapted_detail),
        "work_count": len(adapted_work),
        "chart_count": len(adapted_chart),
        "increment_count": len(adapted_increment),
        "log_count": len(adapted_sync_log),
    }


def build_single_platform_excel(platform: str, min_date: str, max_date: str, output: str) -> dict:
    meta = resolve_snapshot_meta()
    source_rows = build_source_rows()
    source_rows = filter_by_platforms(source_rows, [platform])
    source_rows = filter_by_min_date(source_rows, min_date, max_date)
    source_rows = filter_zero_play(source_rows)

    if not source_rows:
        return {"ok": False, "error": "no_data", "detail_count": 0}

    detail_rows = build_detail_rows_v2(source_rows, meta)
    detail_rows = filter_rows_by_date_window(detail_rows, "视频发布日期", min_date, max_date)
    if not detail_rows:
        return {"ok": False, "error": "no_data_after_date_filter", "detail_count": 0}
    adapted = adapt_detail_rows(detail_rows)

    os.makedirs(os.path.dirname(output), exist_ok=True)
    ordered_df(adapted, DETAIL_COLUMNS).to_excel(output, index=False, engine="openpyxl")

    return {"ok": True, "output": output, "detail_count": len(adapted)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build enriched Excel exports.")
    parser.add_argument("--mode", default="all",
                        help="all | douyin | xiaohongshu | bilibili | kuaishou")
    parser.add_argument("--min-date", default="")
    parser.add_argument("--max-date", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--platforms", default="")
    args = parser.parse_args()

    mode = args.mode.strip().lower()

    if mode == "all":
        output = args.output or str(ENRICHED_ALL_FILE)
        result = build_all_platform_excel(args.min_date, args.max_date, output, args.platforms)
    else:
        output = args.output or str(ENRICHED_PLATFORM_FILES.get(mode, DOWNLOADS_DIR / f"{mode}_enriched.xlsx"))
        result = build_single_platform_excel(mode, args.min_date, args.max_date, output)

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
