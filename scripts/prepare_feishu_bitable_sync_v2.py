#!/usr/bin/env python3
import argparse
import json
import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from statistics import median
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from core.io import write_json_file_atomically

from platform_source_rows import (
    DOWNLOADS_DIR,
    PLATFORM_LABELS,
    build_douyin_rows,
    build_json_platform_rows,
    build_preferred_platform_rows,
    clean_value,
    filter_by_min_date,
    filter_zero_play,
    normalize_title,
    to_number,
)

# L2 领域层:日期转换已迁移到 domain.datetime_util
from domain.datetime_util import (  # noqa: F401
    ASIA_SHANGHAI,
    to_feishu_date_ms,
    to_feishu_datetime_ms,
)
# L2 领域层:指标归一化已迁移到 domain.metrics
from domain.metrics import (  # noqa: F401
    build_work_duration_seconds_map,
    derived_rate_percent,
    extract_bounce_metric,
    first_nonempty,
    infer_work_duration_seconds,
    interaction_quality,
    log_scale,
    metric_from,
    native_play_progress_percent,
    normalized_play_count,
    number_from,
    parse_number_metric,
    parse_percent_number,
    percent_from,
    safe_rate_percent,
    safe_rate_score,
    unified_avg_watch_seconds,
    unified_bounce_rate,
    unified_comments,
    unified_completion_rate,
    unified_cover_click_rate,
    unified_favorites,
    unified_follows,
    unified_likes,
    unified_play_progress_percent,
    unified_shares,
)
# L2 领域层:作品分组已迁移到 domain.grouping
from domain.grouping import (  # noqa: F401
    assign_work_keys,
    canonical_title,
    episode_title_key,
    title_similarity,
)
# L2 领域层:增量对比 + 平台显示 + 快照元数据 已迁移到 domain.increment
from domain.increment import (  # noqa: F401
    PLATFORM_ORDER,
    SnapshotMeta,
    build_increment_rows_v1,
    choose_summary_title,
    display_platform,
    sort_platform_labels,
)
# L2 领域层:飞书表结构定义已迁移到 domain.feishu_schema
from domain.feishu_schema import (  # noqa: F401
    CHART_BASE_FIELDS,
    CHART_COLUMNS,
    CHART_METRIC_FIELDS,
    CHART_PLATFORM_FIELDS,
    CONTENT_TYPE_OPTIONS,
    PLATFORM_DETAIL_BUSINESS_FIELDS,
    PLATFORM_DETAIL_TECH_FIELDS,
    PLATFORM_FIELD,
    PLATFORM_SELECT_OPTIONS,
    build_table_definitions_v2,
    chart_platform_fields_for,
    make_date_field,
    make_datetime_field,
    make_multi_select_field,
    make_number_field,
    make_single_select_field,
    make_text_field,
    project_rows_to_defined_fields,
)

TREND_DB_PATH = DOWNLOADS_DIR / "content_trends.sqlite3"
ANALYTICS_DB_PATH = DOWNLOADS_DIR / "analytics.db"
# PLATFORM_SELECT_OPTIONS / CHART_* / PLATFORM_DETAIL_* 等表结构常量
# 已迁移至 domain.feishu_schema



# chart_platform_fields_for 已迁移至 domain.feishu_schema

# display_platform / sort_platform_labels 已迁移至 domain.increment


def display_content_type(content_type: str) -> str:
    value = clean_value(content_type).lower()
    if value == "video":
        return "视频"
    if value == "image_text":
        return "图文"
    return clean_value(content_type)


# parse_percent_number .. derived_rate_percent 等 26 个指标函数已迁移至
# domain.metrics,本文件顶部 import,下方不再重复定义。



# to_feishu_date_ms / to_feishu_datetime_ms / ASIA_SHANGHAI 已迁移至
# domain.datetime_util，本文件顶部 import，下方不再重复定义。


# _compact_title_identity .. assign_work_keys 等 9 个分组函数 + UnionFind
# 已迁移至 domain.grouping，本文件顶部 import，下方不再重复定义。


# choose_summary_title / SnapshotMeta 已迁移至 domain.increment


def resolve_snapshot_meta() -> SnapshotMeta:
    candidates = [
        DOWNLOADS_DIR / "all_videos.xlsx",
        DOWNLOADS_DIR / "xiaohongshu_all_videos.xlsx",
        DOWNLOADS_DIR / "bilibili_all_videos.xlsx",
        DOWNLOADS_DIR / "kuaishou_all_videos.xlsx",
        DOWNLOADS_DIR / "xiaohongshu_rows.json",
        DOWNLOADS_DIR / "bilibili_rows.json",
        DOWNLOADS_DIR / "kuaishou_rows.json",
    ]
    generated_at = None
    for path in candidates:
        if not path.exists():
            continue
        try:
            ts = datetime.fromtimestamp(path.stat().st_mtime, tz=ASIA_SHANGHAI)
            if generated_at is None or ts > generated_at:
                generated_at = ts
        except Exception:
            continue
    if generated_at is None:
        generated_at = datetime.now(ASIA_SHANGHAI)
    generated_at = generated_at.strftime("%Y-%m-%d %H:%M:%S")
    snapshot_date = generated_at[:10]
    generated_at_ms = int(pd.Timestamp(generated_at, tz=ASIA_SHANGHAI).timestamp() * 1000)
    snapshot_date_ms = int(pd.Timestamp(snapshot_date, tz=ASIA_SHANGHAI).timestamp() * 1000)
    batch_id = datetime.now(ASIA_SHANGHAI).strftime("sync-v2-%Y%m%d-%H%M%S")
    return SnapshotMeta(
        generated_at=generated_at,
        snapshot_date=snapshot_date,
        generated_at_ms=generated_at_ms,
        snapshot_date_ms=snapshot_date_ms,
        batch_id=batch_id,
    )


def build_metric_snapshot_rows(source_rows: List[dict]) -> List[dict]:
    group_map = assign_work_keys(source_rows)
    metric_rows = []
    for row in source_rows:
        platform_work_key = clean_value(row.get("平台作品键"))
        metric_rows.append(
            {
                "平台作品键": platform_work_key,
                "作品组ID": group_map.get(platform_work_key, platform_work_key),
                "平台": clean_value(row.get("平台")),
                "作品ID": clean_value(row.get("作品ID")),
                "标题": clean_value(row.get("标题")),
                "发布日期": clean_value(row.get("发布日期")),
                "内容类型": clean_value(row.get("内容类型")),
                "播放量": normalized_play_count(row),
                "点赞量": unified_likes(row),
                "收藏量": unified_favorites(row),
                "评论量": unified_comments(row),
                "分享量": unified_shares(row),
                "涨粉量": unified_follows(row),
            }
        )
    return metric_rows


def ensure_growth_db(path: Path):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_runs (
              batch_id TEXT PRIMARY KEY,
              generated_at TEXT NOT NULL,
              min_date TEXT,
              detail_count INTEGER NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS detail_snapshots_v2 (
              snapshot_key TEXT PRIMARY KEY,
              batch_id TEXT NOT NULL,
              generated_at TEXT NOT NULL,
              min_date TEXT,
              platform_work_key TEXT NOT NULL,
              work_group_id TEXT NOT NULL,
              platform TEXT NOT NULL,
              work_id TEXT NOT NULL,
              title TEXT NOT NULL,
              publish_date TEXT NOT NULL,
              content_type TEXT NOT NULL,
              plays REAL NOT NULL,
              likes REAL NOT NULL,
              favorites REAL NOT NULL,
              comments REAL NOT NULL,
              shares REAL NOT NULL,
              follows REAL NOT NULL
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_detail_snapshots_v2_batch ON detail_snapshots_v2(batch_id);")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_detail_snapshots_v2_platform_work ON detail_snapshots_v2(platform_work_key);"
        )
        conn.commit()
    finally:
        conn.close()


def load_previous_v2_snapshot(
    path: Path,
    current_generated_at: str,
    group_map: Optional[Dict[str, str]] = None,
):
    if not path.exists():
        return "", []
    ensure_growth_db(path)
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            """
            SELECT batch_id, generated_at
            FROM sync_runs
            WHERE generated_at < ?
              AND EXISTS (SELECT 1 FROM detail_snapshots_v2 d WHERE d.batch_id = sync_runs.batch_id LIMIT 1)
            ORDER BY generated_at DESC
            LIMIT 1;
            """,
            (current_generated_at,),
        ).fetchone()
        if not row:
            return "", []
        batch_id, generated_at = clean_value(row[0]), clean_value(row[1])
        cur = conn.execute(
            """
            SELECT platform_work_key, work_group_id, platform, work_id, title, publish_date, content_type,
                   plays, likes, favorites, comments, shares, follows
            FROM detail_snapshots_v2
            WHERE batch_id = ?;
            """,
            (batch_id,),
        )
        rows = []
        current_group_map = group_map or {}
        for item in cur.fetchall():
            platform_work_key = clean_value(item[0])
            work_group_id = clean_value(item[1])
            if platform_work_key in current_group_map:
                work_group_id = clean_value(current_group_map.get(platform_work_key)) or work_group_id
            rows.append(
                {
                    "平台作品键": platform_work_key,
                    "作品组ID": work_group_id,
                    "平台": clean_value(item[2]),
                    "作品ID": clean_value(item[3]),
                    "标题": clean_value(item[4]),
                    "发布日期": clean_value(item[5]),
                    "内容类型": clean_value(item[6]),
                    "播放量": to_number(item[7]),
                    "点赞量": to_number(item[8]),
                    "收藏量": to_number(item[9]),
                    "评论量": to_number(item[10]),
                    "分享量": to_number(item[11]),
                    "涨粉量": to_number(item[12]),
                }
            )
        return generated_at, rows
    finally:
        conn.close()


def load_previous_from_analytics_db(path: Path, group_map: Dict[str, str]):
    if not path.exists():
        return "", []
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT run_at, run_id FROM runs ORDER BY run_at DESC LIMIT 1;").fetchone()
        if not row:
            return "", []
        generated_at, run_id = clean_value(row[0]), int(row[1])
        cur = conn.execute(
            """
            SELECT platform_work_id, platform, work_id, title, publish_date, plays, likes, favorites, comments, shares
            FROM snapshots
            WHERE run_id = ?;
            """,
            (run_id,),
        )
        rows = []
        for item in cur.fetchall():
            platform_work_key = clean_value(item[0])
            rows.append(
                {
                    "平台作品键": platform_work_key,
                    "作品组ID": group_map.get(platform_work_key, platform_work_key),
                    "平台": clean_value(item[1]),
                    "作品ID": clean_value(item[2]),
                    "标题": clean_value(item[3]),
                    "发布日期": clean_value(item[4]),
                    "内容类型": "",
                    "播放量": to_number(item[5]),
                    "点赞量": to_number(item[6]),
                    "收藏量": to_number(item[7]),
                    "评论量": to_number(item[8]),
                    "分享量": to_number(item[9]),
                    "涨粉量": 0,
                }
            )
        return generated_at, rows
    finally:
        conn.close()


def archive_metric_snapshot(path: Path, batch_id: str, generated_at: str, min_date_text: str, metric_rows: List[dict]):
    ensure_growth_db(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sync_runs(batch_id, generated_at, min_date, detail_count) VALUES (?, ?, ?, ?);",
            (batch_id, generated_at, min_date_text, int(len(metric_rows))),
        )
        values = []
        for row in metric_rows:
            values.append(
                (
                    f"{batch_id}:{row['平台作品键']}",
                    batch_id,
                    generated_at,
                    min_date_text,
                    clean_value(row["平台作品键"]),
                    clean_value(row["作品组ID"]),
                    clean_value(row["平台"]),
                    clean_value(row["作品ID"]),
                    clean_value(row["标题"]),
                    clean_value(row["发布日期"]),
                    clean_value(row["内容类型"]),
                    float(row["播放量"]),
                    float(row["点赞量"]),
                    float(row["收藏量"]),
                    float(row["评论量"]),
                    float(row["分享量"]),
                    float(row["涨粉量"]),
                )
            )
        conn.executemany(
            """
            INSERT OR REPLACE INTO detail_snapshots_v2(
              snapshot_key, batch_id, generated_at, min_date, platform_work_key, work_group_id, platform, work_id,
              title, publish_date, content_type, plays, likes, favorites, comments, shares, follows
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            values,
        )
        conn.commit()
    finally:
        conn.close()


def write_pending_metric_snapshot(
    path: Path,
    *,
    batch_id: str,
    generated_at: str,
    min_date: str,
    metric_rows: List[dict],
) -> Path:
    payload = {
        "batch_id": str(batch_id),
        "generated_at": str(generated_at),
        "min_date": str(min_date or ""),
        "metric_rows": list(metric_rows or []),
    }
    write_json_file_atomically(str(path), payload)
    return path


def commit_pending_metric_snapshot(pending_path: Path, trend_db_path: Path) -> dict:
    payload = json.loads(Path(pending_path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("metric_rows"), list):
        raise ValueError("invalid_pending_metric_snapshot")
    archive_metric_snapshot(
        Path(trend_db_path),
        str(payload.get("batch_id") or ""),
        str(payload.get("generated_at") or ""),
        str(payload.get("min_date") or ""),
        payload["metric_rows"],
    )
    Path(pending_path).unlink(missing_ok=True)
    return payload


def build_source_rows() -> List[dict]:
    rows: List[dict] = []
    rows.extend(build_douyin_rows())
    rows.extend(build_preferred_platform_rows("xiaohongshu_all_videos.xlsx", "xiaohongshu_rows.json", "xiaohongshu"))
    rows.extend(build_preferred_platform_rows("bilibili_all_videos.xlsx", "bilibili_rows.json", "bilibili"))
    rows.extend(build_preferred_platform_rows("kuaishou_all_videos.xlsx", "kuaishou_rows.json", "kuaishou"))
    rows = [row for row in rows if clean_value(row.get(PLATFORM_FIELD)) in PLATFORM_ORDER]
    return rows


def parse_platforms(raw_value: str) -> List[str]:
    requested: List[str] = []
    seen = set()
    for item in str(raw_value or "").split(","):
        platform = clean_value(item)
        if platform not in PLATFORM_ORDER or platform in seen:
            continue
        requested.append(platform)
        seen.add(platform)
    return requested


def filter_by_platforms(rows: List[dict], platforms: List[str]) -> List[dict]:
    if not platforms:
        return rows
    allowed = set(platforms)
    return [row for row in rows if clean_value(row.get(PLATFORM_FIELD)) in allowed]


def build_detail_rows_v2(source_rows: List[dict], meta: SnapshotMeta) -> List[dict]:
    """Build the canonical platform-detail fact rows."""
    group_map, work_duration_seconds_map = build_work_duration_seconds_map(source_rows)
    detail_rows = []
    for row in source_rows:
        platform = clean_value(row["平台"])
        platform_work_key = clean_value(row.get("平台作品键"))
        work_key = group_map.get(platform_work_key, platform_work_key)
        total_traffic = normalized_play_count(row)
        likes = unified_likes(row)
        favorites = unified_favorites(row)
        comments = unified_comments(row)
        shares = unified_shares(row)
        follows = unified_follows(row)
        detail_rows.append(
            {
                "视频标题": clean_value(row.get("标题")),
                "视频平台": display_platform(platform),
                "视频发布日期": to_feishu_date_ms(clean_value(row.get("发布日期"))),
                "同步键": work_key,
                "平台作品键": platform_work_key,
                "总流量": total_traffic,
                "点赞量": likes,
                "评论量": comments,
                "分享量": shares,
                "收藏量": favorites,
                "涨粉量": follows,
                "平均播放进度": unified_play_progress_percent(row, work_duration_seconds_map.get(work_key)),
                "完播率": unified_completion_rate(row),
                "点赞率": derived_rate_percent(likes, total_traffic),
                "评论率": derived_rate_percent(comments, total_traffic),
                "分享率": derived_rate_percent(shares, total_traffic),
                "收藏率": derived_rate_percent(favorites, total_traffic),
                "封标点击率": unified_cover_click_rate(row),
                "3s跳出率": unified_bounce_rate(row),
            }
        )
    detail_rows.sort(key=lambda row: (row["视频发布日期"] or 0, row["视频平台"], row["视频标题"]))
    return detail_rows


def build_chart_metric_values(row: dict, work_duration_seconds: Optional[float]) -> Dict[str, Optional[float]]:
    total_traffic = normalized_play_count(row)
    likes = unified_likes(row)
    favorites = unified_favorites(row)
    comments = unified_comments(row)
    shares = unified_shares(row)
    follows = unified_follows(row)
    return {
        "总流量": total_traffic,
        "点赞量": likes,
        "评论量": comments,
        "分享量": shares,
        "收藏量": favorites,
        "涨粉量": follows,
        "平均播放进度": unified_play_progress_percent(row, work_duration_seconds),
        "完播率": unified_completion_rate(row),
        "点赞率": derived_rate_percent(likes, total_traffic),
        "评论率": derived_rate_percent(comments, total_traffic),
        "分享率": derived_rate_percent(shares, total_traffic),
        "收藏率": derived_rate_percent(favorites, total_traffic),
        "封标点击率": unified_cover_click_rate(row),
        "3s跳出率": unified_bounce_rate(row),
    }


def aggregate_chart_metric_values(rows: List[dict], work_duration_seconds: Optional[float]) -> Dict[str, Optional[float]]:
    total_traffic = sum(normalized_play_count(row) for row in rows)
    likes = sum(unified_likes(row) for row in rows)
    favorites = sum(unified_favorites(row) for row in rows)
    comments = sum(unified_comments(row) for row in rows)
    shares = sum(unified_shares(row) for row in rows)
    follows = sum(unified_follows(row) for row in rows)
    row_metrics = [build_chart_metric_values(row, work_duration_seconds) for row in rows]

    def weighted_metric(metric_name: str) -> Optional[float]:
        weighted_total = 0.0
        weight_total = 0.0
        fallback_values = []
        for metrics in row_metrics:
            value = metrics.get(metric_name)
            if value is None:
                continue
            weight = float(metrics.get("总流量") or 0)
            if weight > 0:
                weighted_total += float(value) * weight
                weight_total += weight
            else:
                fallback_values.append(float(value))
        if weight_total > 0:
            return round(weighted_total / weight_total, 2)
        if fallback_values:
            return round(sum(fallback_values) / len(fallback_values), 2)
        return None

    return {
        "总流量": total_traffic,
        "点赞量": likes,
        "评论量": comments,
        "分享量": shares,
        "收藏量": favorites,
        "涨粉量": follows,
        "平均播放进度": weighted_metric("平均播放进度"),
        "完播率": weighted_metric("完播率"),
        "点赞率": derived_rate_percent(likes, total_traffic),
        "评论率": derived_rate_percent(comments, total_traffic),
        "分享率": derived_rate_percent(shares, total_traffic),
        "收藏率": derived_rate_percent(favorites, total_traffic),
        "封标点击率": weighted_metric("封标点击率"),
        "3s跳出率": weighted_metric("3s跳出率"),
    }


def build_work_rows_v2(source_rows: List[dict], meta: SnapshotMeta) -> List[dict]:
    group_map, work_duration_seconds_map = build_work_duration_seconds_map(source_rows)
    grouped: Dict[str, List[dict]] = {}
    for row in source_rows:
        grouped.setdefault(group_map[row["平台作品键"]], []).append(row)

    work_rows = []
    for work_key, rows in grouped.items():
        platforms = sort_platform_labels(display_platform(item["平台"]) for item in rows)
        content_types = sorted({display_content_type(item.get("内容类型")) for item in rows if clean_value(item.get("内容类型"))})
        publish_dates = [pd.to_datetime(item.get("发布日期"), errors="coerce") for item in rows]
        publish_dates = [ts for ts in publish_dates if not pd.isna(ts)]
        first_publish = min(publish_dates).strftime("%Y-%m-%d") if publish_dates else ""
        summary_title = choose_summary_title(rows)
        total_likes = sum(unified_likes(item) for item in rows)
        total_favorites = sum(unified_favorites(item) for item in rows)
        total_comments = sum(unified_comments(item) for item in rows)
        total_shares = sum(unified_shares(item) for item in rows)
        work_rows.append(
            {
                "标题": summary_title,
                "首发日期": to_feishu_date_ms(first_publish),
                "覆盖平台数": len(platforms),
                "覆盖平台": platforms,
                "内容类型": content_types,
                "总播放量": sum(normalized_play_count(item) for item in rows),
                "总点赞量": total_likes,
                "总收藏量": total_favorites,
                "总评论量": total_comments,
                "总分享量": total_shares,
                "总涨粉量": sum(unified_follows(item) for item in rows),
                "总互动量": total_likes + total_favorites + total_comments + total_shares,
                "作品总时长秒": work_duration_seconds_map.get(work_key),
                "最近更新时间": meta.generated_at_ms,
                "同步键": work_key,
            }
        )
    work_rows.sort(key=lambda row: (row["首发日期"] or 0, row["标题"]))
    return work_rows


def build_chart_rows_v1(
    source_rows: List[dict],
    meta: SnapshotMeta,
    chart_platform_fields: Optional[List[tuple[str, str]]] = None,
) -> List[dict]:
    group_map, work_duration_seconds_map = build_work_duration_seconds_map(source_rows)
    grouped: Dict[str, List[dict]] = {}
    for row in source_rows:
        grouped.setdefault(group_map[row["平台作品键"]], []).append(row)

    chart_platform_fields = chart_platform_fields or CHART_PLATFORM_FIELDS
    chart_rows = []
    for work_key, rows in grouped.items():
        publish_dates = [pd.to_datetime(item.get("发布日期"), errors="coerce") for item in rows]
        publish_dates = [ts for ts in publish_dates if not pd.isna(ts)]
        first_publish = min(publish_dates).strftime("%Y-%m-%d") if publish_dates else ""
        summary_title = choose_summary_title(rows)
        platforms = sort_platform_labels(display_platform(item["平台"]) for item in rows)
        content_types = sorted(
            {display_content_type(item.get("内容类型")) for item in rows if clean_value(item.get("内容类型"))}
        )

        row = {
            "日期": meta.snapshot_date_ms,
            "标题": summary_title,
            "首发日期": to_feishu_date_ms(first_publish),
            "内容类型": content_types,
            "覆盖平台": platforms,
            "覆盖平台数": len(platforms),
            "同步键": work_key,
        }
        by_platform: Dict[str, List[dict]] = {}
        for item in rows:
            by_platform.setdefault(display_platform(item["平台"]), []).append(item)
        work_duration_seconds = work_duration_seconds_map.get(work_key)
        for label, suffix in chart_platform_fields:
            platform_rows = by_platform.get(label, [])
            if not platform_rows:
                for metric_name in CHART_METRIC_FIELDS:
                    row[f"{metric_name}_{suffix}"] = 0
                continue
            metric_values = aggregate_chart_metric_values(platform_rows, work_duration_seconds)
            for metric_name in CHART_METRIC_FIELDS:
                value = metric_values.get(metric_name)
                row[f"{metric_name}_{suffix}"] = value if value is not None else 0
        chart_rows.append(row)

    chart_rows.sort(key=lambda row: (row["首发日期"] or 0, row["标题"]))
    return chart_rows


# build_increment_rows_v1 已迁移至 domain.increment


def _build_sync_log_row_v2(
    meta: SnapshotMeta,
    included_platforms: List[str],
    detail_count: int,
    work_count: int,
    chart_count: int,
    increment_rows: List[dict],
) -> dict:
    included_platform_text = " / ".join(included_platforms) if included_platforms else "无"
    new_work_statuses = {"新作品", "首次快照", "首次纳入"}
    new_work_count = sum(1 for row in increment_rows if clean_value(row.get("增量状态")) in new_work_statuses)
    growing_work_count = sum(1 for row in increment_rows if clean_value(row.get("增量状态")) == "持续增长")
    return {
        "同步日期": meta.snapshot_date_ms,
        "纳入平台": included_platform_text,
        "平台明细记录数": int(detail_count or 0),
        "作品总表记录数": int(work_count or 0),
        "作品图表表记录数": int(chart_count or 0),
        "作品增量表记录数": len(increment_rows),
        "新增作品数": new_work_count,
        "持续增长作品数": growing_work_count,
        "备注": "本次按最新平台明细和作品聚合口径生成。",
        "批次同步键": meta.batch_id,
    }


def load_sync_log_history_rows_v2(path: Path) -> List[dict]:
    if not path.exists():
        return []
    ensure_growth_db(path)
    conn = sqlite3.connect(path)
    try:
        run_rows = conn.execute(
            """
            SELECT batch_id, generated_at, detail_count
            FROM sync_runs
            WHERE EXISTS (SELECT 1 FROM detail_snapshots_v2 d WHERE d.batch_id = sync_runs.batch_id LIMIT 1)
            ORDER BY generated_at ASC, batch_id ASC;
            """
        ).fetchall()
        if not run_rows:
            return []

        batch_ids = [clean_value(row[0]) for row in run_rows if clean_value(row[0])]
        placeholders = ",".join("?" for _ in batch_ids)
        snapshots_by_batch: Dict[str, List[dict]] = {}
        if batch_ids:
            query = f"""
                SELECT batch_id, platform_work_key, work_group_id, platform, work_id, title, publish_date, content_type,
                       plays, likes, favorites, comments, shares, follows
                FROM detail_snapshots_v2
                WHERE batch_id IN ({placeholders})
                ORDER BY generated_at ASC, batch_id ASC, platform_work_key ASC;
            """
            for item in conn.execute(query, batch_ids):
                batch_id = clean_value(item[0])
                snapshots_by_batch.setdefault(batch_id, []).append(
                    {
                        "平台作品键": clean_value(item[1]),
                        "作品组ID": clean_value(item[2]),
                        "平台": clean_value(item[3]),
                        "作品ID": clean_value(item[4]),
                        "标题": clean_value(item[5]),
                        "发布日期": clean_value(item[6]),
                        "内容类型": clean_value(item[7]),
                        "播放量": float(item[8] or 0),
                        "点赞量": float(item[9] or 0),
                        "收藏量": float(item[10] or 0),
                        "评论量": float(item[11] or 0),
                        "分享量": float(item[12] or 0),
                        "涨粉量": float(item[13] or 0),
                    }
                )

        history_rows = []
        previous_generated_at = ""
        previous_metric_rows: List[dict] = []
        for batch_id, generated_at, detail_count in run_rows:
            batch_id = clean_value(batch_id)
            generated_at = clean_value(generated_at)
            current_metric_rows = snapshots_by_batch.get(batch_id, [])
            if not batch_id or not generated_at or not current_metric_rows:
                continue
            snapshot_date = generated_at[:10]
            meta = SnapshotMeta(
                generated_at=generated_at,
                snapshot_date=snapshot_date,
                generated_at_ms=int(pd.Timestamp(generated_at, tz=ASIA_SHANGHAI).timestamp() * 1000),
                snapshot_date_ms=int(pd.Timestamp(snapshot_date, tz=ASIA_SHANGHAI).timestamp() * 1000),
                batch_id=batch_id,
            )
            increment_rows = build_increment_rows_v1(
                current_metric_rows,
                previous_metric_rows,
                previous_generated_at,
                meta,
            )
            included_platforms = sort_platform_labels(
                display_platform(row.get("平台")) for row in current_metric_rows if clean_value(row.get("平台"))
            )
            work_count = len(
                {
                    clean_value(row.get("作品组ID"))
                    for row in current_metric_rows
                    if clean_value(row.get("作品组ID"))
                }
            )
            history_rows.append(
                _build_sync_log_row_v2(
                    meta,
                    included_platforms,
                    int(detail_count or len(current_metric_rows)),
                    work_count,
                    work_count,
                    increment_rows,
                )
            )
            previous_generated_at = generated_at
            previous_metric_rows = current_metric_rows
        return history_rows
    finally:
        conn.close()


def build_sync_log_rows_v2(
    meta: SnapshotMeta,
    detail_rows: List[dict],
    work_rows: List[dict],
    chart_rows: List[dict],
    increment_rows: List[dict],
) -> List[dict]:
    included_platforms = sort_platform_labels(
        display_platform(row.get("视频平台") or row.get("平台")) for row in detail_rows
    )
    current_row = _build_sync_log_row_v2(
        meta,
        included_platforms,
        len(detail_rows),
        len(work_rows),
        len(chart_rows),
        increment_rows,
    )
    history_rows = [
        row
        for row in load_sync_log_history_rows_v2(TREND_DB_PATH)
        if clean_value(row.get("批次同步键")) != meta.batch_id
    ]
    history_rows.append(current_row)
    return history_rows


# make_*_field / build_table_definitions_v2 / project_rows_to_defined_fields
# 已迁移至 domain.feishu_schema


def main():
    parser = argparse.ArgumentParser(description="Prepare Feishu bitable V2 payload from local export files.")
    parser.add_argument("--min-date", default="")
    parser.add_argument("--max-date", default="")
    parser.add_argument("--platforms", default="")
    parser.add_argument("--output", default=str(DOWNLOADS_DIR / "feishu_sync_payload_compare_v2.json"))
    parser.add_argument("--baseline-only", action="store_true", help="Treat this export as the first baseline snapshot.")
    parser.add_argument("--pending-snapshot", default="")
    parser.add_argument("--commit-snapshot", default="")
    args = parser.parse_args()

    if args.commit_snapshot:
        committed = commit_pending_metric_snapshot(Path(args.commit_snapshot), TREND_DB_PATH)
        print(json.dumps({"ok": True, "committed_batch_id": committed.get("batch_id", "")}, ensure_ascii=False))
        return

    meta = resolve_snapshot_meta()
    source_rows = build_source_rows()
    requested_platforms = parse_platforms(args.platforms)
    source_rows = filter_by_platforms(source_rows, requested_platforms)
    source_rows = filter_by_min_date(source_rows, args.min_date, args.max_date)
    source_rows = filter_zero_play(source_rows)
    included_platforms = [
        platform for platform in PLATFORM_ORDER if any(clean_value(row.get(PLATFORM_FIELD)) == platform for row in source_rows)
    ]
    metric_snapshot_rows = build_metric_snapshot_rows(source_rows)
    group_map = {clean_value(item["平台作品键"]): clean_value(item["作品组ID"]) for item in metric_snapshot_rows}
    if args.baseline_only:
        previous_generated_at, previous_metric_rows = "", []
    else:
        previous_generated_at, previous_metric_rows = load_previous_v2_snapshot(TREND_DB_PATH, meta.generated_at, group_map)
        if not previous_metric_rows:
            previous_generated_at, previous_metric_rows = load_previous_from_analytics_db(ANALYTICS_DB_PATH, group_map)
    detail_rows = build_detail_rows_v2(source_rows, meta)
    work_rows = build_work_rows_v2(source_rows, meta)
    chart_rows = build_chart_rows_v1(source_rows, meta, chart_platform_fields_for(requested_platforms or included_platforms))
    increment_rows = build_increment_rows_v1(metric_snapshot_rows, previous_metric_rows, previous_generated_at, meta)
    sync_log_rows = build_sync_log_rows_v2(meta, detail_rows, work_rows, chart_rows, increment_rows)

    table_definitions = build_table_definitions_v2(requested_platforms or included_platforms)
    field_defs_by_name = {table["name"]: table["fields"] for table in table_definitions}

    payload = {
        "meta": {
            "batch_id": meta.batch_id,
            "generated_at": meta.generated_at,
            "snapshot_date": meta.snapshot_date,
            "timezone": "Asia/Shanghai",
            "time_sources": {
                "generated_at": {
                    "source": "latest_local_export_mtime",
                    "timezone": "Asia/Shanghai",
                },
                "snapshot_date": {
                    "source": "generated_at_date_floor",
                    "timezone": "Asia/Shanghai",
                },
                "compare_generated_at": {
                    "source": "baseline_only" if args.baseline_only else "previous_snapshot_generated_at",
                    "timezone": "Asia/Shanghai",
                },
            },
            "detail_count": len(detail_rows),
            "work_count": len(work_rows),
            "chart_count": len(chart_rows),
            "increment_count": len(increment_rows),
            "log_count": len(sync_log_rows),
            "compare_generated_at": previous_generated_at,
            "increment_baseline_only": bool(args.baseline_only),
            "platforms": included_platforms,
            "platform_labels": [display_platform(platform) for platform in included_platforms],
        },
        "table_definitions": table_definitions,
        "tables": {
            "平台明细V2": project_rows_to_defined_fields(detail_rows, field_defs_by_name["平台明细V2"]),
            "作品总表V2": project_rows_to_defined_fields(work_rows, field_defs_by_name["作品总表V2"]),
            "作品图表表": project_rows_to_defined_fields(chart_rows, field_defs_by_name["作品图表表"]),
            "作品增量表": project_rows_to_defined_fields(increment_rows, field_defs_by_name["作品增量表"]),
            "同步日志V2": project_rows_to_defined_fields(sync_log_rows, field_defs_by_name["同步日志V2"]),
        },
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    pending_snapshot_path = (
        Path(args.pending_snapshot)
        if args.pending_snapshot
        else output_path.with_name(f"{output_path.stem}.pending_snapshot.json")
    )
    write_pending_metric_snapshot(
        pending_snapshot_path,
        batch_id=meta.batch_id,
        generated_at=meta.generated_at,
        min_date=args.min_date,
        metric_rows=metric_snapshot_rows,
    )
    print(
        json.dumps(
            {
                "output": str(output_path),
                "pending_snapshot_path": str(pending_snapshot_path),
                **payload["meta"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
