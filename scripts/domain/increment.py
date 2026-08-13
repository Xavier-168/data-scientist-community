"""增量对比 + 平台显示 (L2 领域层) — 当前 vs 上次快照做差,产出增量状态。

纯函数:计算作品在各指标上的增长趋势(持续增长/流量增长/热度回落等)。
不碰文件/网络/数据库。

迁移自 prepare_feishu_bitable_sync_v2.py。
包含:平台显示映射(被多处复用)、摘要标题选择、增量状态机。
"""

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List

import pandas as pd

from core.platforms import PLATFORM_LABELS
from platform_source_rows import clean_value, normalize_title
from domain.datetime_util import to_feishu_date_ms

PLATFORM_ORDER = ["douyin", "xiaohongshu", "bilibili", "kuaishou"]


@dataclass
class SnapshotMeta:
    generated_at: str
    snapshot_date: str
    generated_at_ms: int
    snapshot_date_ms: int
    batch_id: str


def display_platform(platform: str) -> str:
    """平台内部码 → 中文标签。"""
    return PLATFORM_LABELS.get(clean_value(platform), clean_value(platform))


def sort_platform_labels(labels) -> List[str]:
    """按固定平台顺序(抖音/小红书/B站/快手)排序标签。"""
    return sorted(
        {clean_value(label) for label in labels if clean_value(label)},
        key=lambda name: PLATFORM_ORDER.index(
            next(code for code, label in PLATFORM_LABELS.items() if label == name)
        ),
    )


def choose_summary_title(rows: List[dict]) -> str:
    """从多条同组作品里选出代表性标题(出现最多 → 最短 → 字典序)。"""
    candidates = []
    for row in rows:
        original = normalize_title(row.get("标题"))
        cleaned = re.sub(r"\s+", " ", re.sub(r"#[^#]+", " ", original)).strip(" -|｜")
        candidates.append(cleaned or original)
    ranked = Counter(candidates)
    return sorted(ranked.items(), key=lambda item: (-item[1], len(item[0]), item[0]))[0][0] if ranked else ""


def build_increment_rows_v1(
    current_rows: List[dict],
    previous_rows: List[dict],
    previous_generated_at: str,
    meta: "SnapshotMeta",
):
    """增量状态机:对比当前与上次快照,判定每个作品组的增长趋势。

    返回作品增量表行(含各指标增量、日均增量、增量状态)。
    状态: 首次快照 / 首次纳入 / 持续增长 / 流量增长 / 互动增长 / 热度回落 / 基本持平。
    """
    metric_names = ["播放量", "点赞量", "收藏量", "评论量", "分享量", "涨粉量"]
    current_grouped: Dict[str, List[dict]] = {}
    for row in current_rows:
        current_grouped.setdefault(clean_value(row.get("作品组ID")), []).append(row)

    previous_by_platform_key: Dict[str, dict] = {}
    for row in previous_rows:
        platform_work_key = clean_value(row.get("平台作品键"))
        if not platform_work_key:
            continue
        bucket = previous_by_platform_key.setdefault(platform_work_key, {metric: 0.0 for metric in metric_names})
        for metric in bucket:
            bucket[metric] += float(row.get(metric) or 0)

    span_days = None
    if previous_generated_at:
        current_ts = pd.to_datetime(meta.generated_at, errors="coerce")
        previous_ts = pd.to_datetime(previous_generated_at, errors="coerce")
        if not pd.isna(current_ts) and not pd.isna(previous_ts):
            span_days = max(1, int(math.ceil((current_ts - previous_ts).total_seconds() / 86400)))

    rows = []
    for work_key, items in current_grouped.items():
        publish_dates = [pd.to_datetime(item.get("发布日期"), errors="coerce") for item in items]
        publish_dates = [ts for ts in publish_dates if not pd.isna(ts)]
        first_publish = min(publish_dates).strftime("%Y-%m-%d") if publish_dates else ""
        platforms = sort_platform_labels(display_platform(item["平台"]) for item in items)
        deltas = {metric: 0.0 for metric in metric_names}
        previous_match_count = 0
        for item in items:
            old_item = previous_by_platform_key.get(clean_value(item.get("平台作品键")))
            if not old_item:
                continue
            previous_match_count += 1
            for metric in deltas:
                deltas[metric] += float(item.get(metric) or 0) - float(old_item.get(metric, 0) or 0)
        deltas = {metric: round(amount, 2) for metric, amount in deltas.items()}
        interaction_delta = round(deltas["点赞量"] + deltas["收藏量"] + deltas["评论量"] + deltas["分享量"], 2)

        if previous_match_count == 0:
            status = "首次快照" if not previous_generated_at else "首次纳入"
        elif deltas["播放量"] > 0 and interaction_delta > 0:
            status = "持续增长"
        elif deltas["播放量"] > 0:
            status = "流量增长"
        elif interaction_delta > 0:
            status = "互动增长"
        elif deltas["播放量"] < 0 or interaction_delta < 0:
            status = "热度回落"
        else:
            status = "基本持平"

        rows.append(
            {
                "日期": meta.snapshot_date_ms,
                "标题": choose_summary_title(items),
                "首发日期": to_feishu_date_ms(first_publish),
                "覆盖平台": platforms,
                "覆盖平台数": len(platforms),
                "对比快照日期": to_feishu_date_ms(previous_generated_at[:10]) if previous_generated_at else None,
                "对比跨度天数": span_days,
                "总播放增量": deltas["播放量"],
                "总点赞增量": deltas["点赞量"],
                "总收藏增量": deltas["收藏量"],
                "总评论增量": deltas["评论量"],
                "总分享增量": deltas["分享量"],
                "总涨粉增量": deltas["涨粉量"],
                "总互动增量": interaction_delta,
                "日均播放增量": round(deltas["播放量"] / span_days, 2) if span_days else None,
                "日均互动增量": round(interaction_delta / span_days, 2) if span_days else None,
                "增量状态": status,
                "同步键": work_key,
            }
        )

    rows.sort(key=lambda row: (-float(row["总播放增量"] or 0), -float(row["总互动增量"] or 0), row["标题"]))
    return rows
