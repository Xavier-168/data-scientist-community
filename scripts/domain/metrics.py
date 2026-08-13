"""指标归一化 (L2 领域层) — 跨平台字段别名映射 → 统一指标。

纯函数:把各平台不同字段名(点赞量/喜欢、完播率/5s完播率 等)归一成统一口径。
计算完播率/跳出率/播放进度/封标点击率/互动质量等指标。不碰文件/网络。

迁移自 prepare_feishu_bitable_sync_v2.py。
"""

import math
import re
from statistics import median
from typing import Dict, List, Optional

from platform_source_rows import clean_value, to_number


# ── 数值/百分比解析 ────────────────────────────────────────────────────────

def parse_percent_number(value) -> Optional[float]:
    text = clean_value(value)
    if not text:
        return None
    star_match = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*星", text)
    if star_match:
        return round(float(star_match.group(1)) / 5.0 * 100.0, 2)
    text = text.replace("%", "").replace(",", "")
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def parse_number_metric(value) -> Optional[float]:
    """解析时长类指标:支持 时:分:秒 / ms / 秒/s / 分/m / 纯数字。"""
    text = clean_value(value)
    if not text:
        return None
    lowered = text.lower().replace(" ", "").replace(",", "").replace("%", "")
    if ":" in lowered:
        parts = lowered.split(":")
        try:
            numbers = [int(float(part)) for part in parts]
        except ValueError:
            return None
        if len(numbers) == 3:
            return float(numbers[0] * 3600 + numbers[1] * 60 + numbers[2])
        if len(numbers) == 2:
            return float(numbers[0] * 60 + numbers[1])
    if lowered.endswith("ms"):
        return round(to_number(lowered[:-2]) / 1000, 2)
    if lowered.endswith("秒") or lowered.endswith("s"):
        return round(to_number(lowered[:-1]), 2)
    if lowered.endswith("分") or lowered.endswith("m"):
        return round(to_number(lowered[:-1]) * 60, 2)
    number = to_number(lowered)
    if number == 0 and lowered not in {"0", "0.0"}:
        return None
    return round(float(number), 2)


# ── 基础比率/刻度工具 ──────────────────────────────────────────────────────

def safe_rate_percent(numerator, denominator) -> Optional[float]:
    den = float(denominator or 0)
    if den <= 0:
        return None
    return round(float(numerator or 0) * 100.0 / den, 2)


def safe_rate_score(numerator, denominator) -> int:
    den = float(denominator or 0)
    if den <= 0:
        return 0
    return int(float(numerator or 0) * 100.0 / den + 0.5)


def log_scale(value) -> float:
    amount = max(0.0, float(value or 0))
    return round(math.log10(amount + 1.0), 4)


def interaction_quality(likes, favorites, comments, shares, plays) -> Optional[float]:
    den = float(plays or 0)
    if den <= 0:
        return None
    interactions = float(likes or 0) + float(favorites or 0) + float(comments or 0) + float(shares or 0)
    return round(interactions * 100.0 / (den + 100.0), 2)


def derived_rate_percent(metric_value: float, total_traffic: float) -> float:
    denominator = float(total_traffic or 0)
    if denominator <= 0:
        return 0.0
    return round(float(metric_value or 0) * 100.0 / denominator, 2)


# ── 字段取值辅助 ──────────────────────────────────────────────────────────

def first_nonempty(row: dict, keys: List[str]):
    for key in keys:
        value = clean_value(row.get(key))
        if value:
            return value
    return ""


def number_from(row: dict, keys: List[str]) -> float:
    return to_number(first_nonempty(row, keys))


def percent_from(row: dict, keys: List[str]) -> float:
    for key in keys:
        value = parse_percent_number(row.get(key))
        if value is not None:
            return value
    return 0.0


def metric_from(row: dict, keys: List[str]) -> float:
    for key in keys:
        value = parse_number_metric(row.get(key))
        if value is not None:
            return value
    return 0.0


# ── 跳出率（多口径：3s / 2s，小红书用 2s）────────────────────────────────

def extract_bounce_metric(row: dict) -> tuple[Optional[float], str]:
    explicit_source = clean_value(row.get("跳出率口径"))
    for field_name, source in (("3s跳出率", "3s"), ("3秒跳出率", "3s"), ("2s跳出率", "2s"), ("2秒退出率", "2s")):
        value = parse_percent_number(row.get(field_name))
        if value is not None:
            if field_name == "3s跳出率" and explicit_source in {"2s", "3s"}:
                return value, explicit_source
            return value, source
    return None, ""


# ── 统一指标（跨平台字段别名 → 单一口径）─────────────────────────────────

def normalized_play_count(row: dict) -> float:
    platform = clean_value(row.get("平台"))
    if platform == "xiaohongshu":
        return to_number(first_nonempty(row, ["观看量", "观看数", "阅读量", "播放量", "曝光量"]))
    return to_number(first_nonempty(row, ["播放量", "阅读量", "曝光量"]))


def unified_likes(row: dict) -> float:
    return number_from(row, ["点赞量", "喜欢"])


def unified_comments(row: dict) -> float:
    return number_from(row, ["评论量"])


def unified_shares(row: dict) -> float:
    return number_from(row, ["转发量", "分享量", "分享"])


def unified_favorites(row: dict) -> float:
    return number_from(row, ["收藏量", "推荐"])


def unified_follows(row: dict) -> float:
    return number_from(row, ["涨粉量", "关注"])


def unified_avg_watch_seconds(row: dict) -> float:
    platform = clean_value(row.get("平台"))
    if platform == "bilibili":
        return 0.0
    return metric_from(row, ["平均观看时长", "平均播放时长"])


def native_play_progress_percent(row: dict) -> Optional[float]:
    for key in ["平均播放进度", "平均播放占比"]:
        value = parse_percent_number(row.get(key))
        if value is not None:
            return value
    return None


def infer_work_duration_seconds(avg_watch_seconds: float, play_progress_percent: Optional[float]) -> Optional[float]:
    """由 平均观看时长 + 播放进度 反推作品总时长（秒）。"""
    avg = float(avg_watch_seconds or 0)
    progress = float(play_progress_percent or 0)
    if avg <= 0 or progress <= 0:
        return None
    return round(avg / (progress / 100.0), 2)


def build_work_duration_seconds_map(source_rows: List[dict]) -> tuple[Dict[str, str], Dict[str, float]]:
    """为每个作品组推断总时长。

    返回 (group_map, duration_map)。group_map 来自 domain.grouping.assign_work_keys。
    延迟 import 避免与 grouping 循环依赖（grouping 不依赖 metrics）。
    """
    from domain.grouping import assign_work_keys  # 延迟 import

    group_map = assign_work_keys(source_rows)
    inferred_by_work: Dict[str, List[float]] = {}
    explicit_by_work: Dict[str, List[float]] = {}
    for row in source_rows:
        platform_work_key = clean_value(row.get("平台作品键"))
        work_key = group_map.get(platform_work_key)
        if not work_key:
            continue
        if clean_value(row.get("平台")) == "douyin":
            inferred = infer_work_duration_seconds(
                unified_avg_watch_seconds(row),
                native_play_progress_percent(row),
            )
            if inferred is not None:
                inferred_by_work.setdefault(work_key, []).append(inferred)
        explicit_duration = to_number(row.get("时长"))
        if explicit_duration > 0:
            explicit_by_work.setdefault(work_key, []).append(float(explicit_duration))

    duration_map: Dict[str, float] = {}
    for work_key, values in inferred_by_work.items():
        if values:
            duration_map[work_key] = round(float(median(values)), 2)
    for work_key, values in explicit_by_work.items():
        if work_key in duration_map or not values:
            continue
        duration_map[work_key] = round(float(median(values)), 2)
    return group_map, duration_map


def unified_play_progress_percent(row: dict, work_duration_seconds: Optional[float]) -> Optional[float]:
    """播放进度:优先用原生进度,缺失时用 平均观看时长/作品时长 反推。"""
    native_progress = native_play_progress_percent(row)
    if native_progress is not None:
        return native_progress
    avg_watch_seconds = unified_avg_watch_seconds(row)
    duration_seconds = float(work_duration_seconds or 0)
    if avg_watch_seconds <= 0 or duration_seconds <= 0:
        return None
    return round(avg_watch_seconds * 100.0 / duration_seconds, 2)


def unified_completion_rate(row: dict) -> float:
    platform = clean_value(row.get("平台"))
    if platform == "bilibili":
        return 0.0
    return percent_from(row, ["完播率", "5s完播率"])


def unified_cover_click_rate(row: dict) -> float:
    return percent_from(row, ["封标点击率", "封面点击率"])


def unified_bounce_rate(row: dict) -> float:
    bounce_rate, _ = extract_bounce_metric(row)
    return bounce_rate if bounce_rate is not None else 0.0
