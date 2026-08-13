#!/usr/bin/env python3
"""Local platform export readers for the current Feishu V2/Excel pipeline.

This module is intentionally limited to source-row loading and raw-field
normalization. It does not build legacy Feishu V1 payloads, analytics score
tables, or old platform-detail schemas.
"""
import json
import os
import re
import sys
from pathlib import Path

import pandas as pd

# 统一从 L1 基础层取路径和平台常量，禁止在本文件重复定义
_SELF_DIR = os.path.dirname(os.path.abspath(__file__))
if _SELF_DIR not in sys.path:
    sys.path.insert(0, _SELF_DIR)

from core.paths import resolve_base_dir, resolve_downloads_dir  # noqa: E402
from core.platforms import PLATFORM_LABELS  # noqa: E402

BASE_DIR = Path(resolve_base_dir())
DOWNLOADS_DIR = Path(resolve_downloads_dir(BASE_DIR))


def clean_value(value):
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def to_number(value):
    text = clean_value(value)
    if not text:
        return 0
    text = text.replace(",", "").replace("%", "").strip()
    multiplier = 1
    if text.endswith("万"):
        multiplier = 10_000
        text = text[:-1]
    elif text.endswith("亿"):
        multiplier = 100_000_000
        text = text[:-1]
    try:
        amount = float(text)
    except ValueError:
        return 0
    result = amount * multiplier
    return int(result) if result.is_integer() else result


def to_date_text(value):
    text = clean_value(value)
    if not text:
        return ""
    ts = pd.to_datetime(text, errors="coerce")
    if pd.isna(ts):
        return text
    return ts.strftime("%Y-%m-%d")


def normalize_title(value):
    text = clean_value(value).replace("\n", " ")
    text = re.sub(r"[\u200b-\u200d\ufeff\xa0]+", " ", text)
    text = text.replace("＃", "#")
    text = " ".join(text.split())
    # 平台源可能只提供话题文本；以 # 开头时保留原值。
    if text.startswith("#"):
        return text
    # 只移除空白分隔的尾部话题，避免把 C# / F# 等正文截断。
    return re.sub(r"\s+#.*$", "", text).strip()


def pick_first_nonempty(row, keys):
    for key in keys:
        value = clean_value(row.get(key))
        if value:
            return value
    return ""


def read_excel_rows(path: Path):
    if not path.exists():
        return []
    df = pd.read_excel(path, dtype=str).fillna("")
    return df.to_dict(orient="records")


def read_json_rows(path: Path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        rows = data.get("rows") or data.get("data")
        if isinstance(rows, list):
            return rows
    return []


def extract_bounce_rate(row):
    if clean_value(row.get("3s跳出率")):
        return clean_value(row.get("3s跳出率")), "3s"
    if clean_value(row.get("3秒跳出率")):
        return clean_value(row.get("3秒跳出率")), "3s"
    if clean_value(row.get("2s跳出率")):
        return clean_value(row.get("2s跳出率")), "2s"
    if clean_value(row.get("2秒退出率")):
        return clean_value(row.get("2秒退出率")), "2s"
    return "", ""


def build_platform_specific_fields(row):
    bounce_rate, bounce_source = extract_bounce_rate(row)
    return {
        "完播率": clean_value(row.get("完播率")),
        "3s跳出率": bounce_rate,
        "跳出率口径": bounce_source,
        "5s完播率": clean_value(row.get("5s完播率")),
        "平均观看时长": pick_first_nonempty(row, ["平均观看时长", "平均播放时长"]),
        "平均播放时长": clean_value(row.get("平均播放时长")),
        "平均播放进度": clean_value(row.get("平均播放进度")),
        "平均播放占比": clean_value(row.get("平均播放占比")),
        "封标点击率": pick_first_nonempty(row, ["封标点击率", "封面点击率"]),
        "封面点击率": clean_value(row.get("封面点击率")),
        "粉丝播放占比": clean_value(row.get("粉丝播放占比")),
        "点赞率": clean_value(row.get("点赞率")),
        "评论率": clean_value(row.get("评论率")),
        "分享率": clean_value(row.get("分享率")),
        "收藏率": clean_value(row.get("收藏率")),
        "不感兴趣率": clean_value(row.get("不感兴趣率")),
        "来源占比_推荐页": clean_value(row.get("来源占比_推荐页")),
        "来源占比_搜索": clean_value(row.get("来源占比_搜索")),
        "来源占比_个人主页": clean_value(row.get("来源占比_个人主页")),
        "来源占比_关注页": clean_value(row.get("来源占比_关注页")),
        "划走率": clean_value(row.get("划走率")),
        "文案展开率": clean_value(row.get("文案展开率")),
        "平均浏览图片数": to_number(row.get("平均浏览图片数")),
        "文案完读率": clean_value(row.get("文案完读率")),
        "评论进入率": clean_value(row.get("评论进入率")),
        "下载量": to_number(row.get("下载量")),
    }


def build_douyin_rows():
    rows = []
    for row in read_excel_rows(DOWNLOADS_DIR / "all_videos.xlsx"):
        work_id = clean_value(row.get("作品ID"))
        if not work_id:
            continue
        item = {
            "平台作品键": f"douyin:{work_id}",
            "平台": "douyin",
            "作品ID": work_id,
            "标题": normalize_title(row.get("标题")),
            "发布日期": to_date_text(row.get("发布日期")),
            "内容类型": "video",
            "曝光量": to_number(row.get("曝光量")),
            "播放量": to_number(row.get("播放量")),
            "阅读量": 0,
            "点赞量": to_number(row.get("点赞量")),
            "收藏量": to_number(row.get("收藏量")),
            "评论量": to_number(row.get("评论量")),
            "分享量": to_number(row.get("分享量")),
            "涨粉量": to_number(row.get("涨粉量")),
            "投币量": 0,
            "弹幕量": to_number(row.get("弹幕量")),
            "时长": to_number(row.get("时长")),
            "链接": clean_value(row.get("链接")),
            "来源文件": "all_videos.xlsx",
        }
        item.update(build_platform_specific_fields(row))
        rows.append(item)
    return rows


def build_excel_platform_rows(file_name: str, platform: str):
    rows = []
    for row in read_excel_rows(DOWNLOADS_DIR / file_name):
        work_id = clean_value(row.get("作品ID"))
        if not work_id:
            continue
        content_type = clean_value(row.get("内容类型")) or "video"
        item = {
            "平台作品键": f"{platform}:{work_id}",
            "平台": platform,
            "作品ID": work_id,
            "标题": normalize_title(row.get("标题")),
            "发布日期": to_date_text(row.get("发布日期")),
            "内容类型": content_type,
            "曝光量": to_number(row.get("曝光量")),
            "播放量": to_number(row.get("播放量")),
            "阅读量": to_number(pick_first_nonempty(row, ["阅读量", "观看量", "观看数"])),
            "点赞量": to_number(pick_first_nonempty(row, ["点赞量", "喜欢"])),
            "收藏量": to_number(pick_first_nonempty(row, ["收藏量", "推荐"])),
            "评论量": to_number(row.get("评论量")),
            "分享量": to_number(pick_first_nonempty(row, ["分享量", "转发量", "分享"])),
            "涨粉量": to_number(pick_first_nonempty(row, ["涨粉量", "关注"])),
            "投币量": to_number(row.get("投币量")),
            "弹幕量": to_number(row.get("弹幕量")),
            "时长": to_number(row.get("时长")),
            "链接": clean_value(row.get("链接")),
            "来源文件": file_name,
        }
        item.update(build_platform_specific_fields(row))
        rows.append(item)
    return rows


def build_json_platform_rows(file_name: str, platform: str):
    rows = []
    for row in read_json_rows(DOWNLOADS_DIR / file_name):
        work_id = clean_value(row.get("作品ID"))
        if not work_id:
            continue
        content_type = clean_value(row.get("内容类型")) or "video"
        item = {
            "平台作品键": f"{platform}:{work_id}",
            "平台": platform,
            "作品ID": work_id,
            "标题": normalize_title(row.get("标题")),
            "发布日期": to_date_text(row.get("发布日期")),
            "内容类型": content_type,
            "曝光量": to_number(row.get("曝光量")),
            "播放量": to_number(row.get("播放量")),
            "阅读量": to_number(pick_first_nonempty(row, ["阅读量", "观看量", "观看数"])),
            "点赞量": to_number(pick_first_nonempty(row, ["点赞量", "喜欢"])),
            "收藏量": to_number(pick_first_nonempty(row, ["收藏量", "推荐"])),
            "评论量": to_number(row.get("评论量")),
            "分享量": to_number(pick_first_nonempty(row, ["分享量", "转发量", "分享"])),
            "涨粉量": to_number(pick_first_nonempty(row, ["涨粉量", "关注"])),
            "投币量": to_number(row.get("投币量")),
            "弹幕量": to_number(row.get("弹幕量")),
            "时长": to_number(row.get("时长")),
            "链接": clean_value(row.get("链接")),
            "来源文件": file_name,
        }
        item.update(build_platform_specific_fields(row))
        rows.append(item)
    return rows


def _merge_extra_value(key: str, current_value, fallback_value):
    if key == "时长":
        if to_number(current_value) <= 0 and to_number(fallback_value) > 0:
            return to_number(fallback_value)
        return current_value
    if not clean_value(current_value) and clean_value(fallback_value):
        return fallback_value
    return current_value


def merge_preferred_and_fallback_rows(preferred_rows, fallback_rows):
    if not preferred_rows or not fallback_rows:
        return preferred_rows

    fallback_by_key = {}
    fallback_by_title_date = {}
    for row in fallback_rows:
        platform_work_key = clean_value(row.get("平台作品键"))
        if platform_work_key:
            fallback_by_key[platform_work_key] = row
        title_date_key = (clean_value(row.get("标题")), clean_value(row.get("发布日期")))
        if title_date_key[0]:
            fallback_by_title_date[title_date_key] = row

    merged_rows = []
    for row in preferred_rows:
        merged = dict(row)
        platform_work_key = clean_value(row.get("平台作品键"))
        title_date_key = (clean_value(row.get("标题")), clean_value(row.get("发布日期")))
        fallback = fallback_by_key.get(platform_work_key) or fallback_by_title_date.get(title_date_key)
        if fallback:
            for key, fallback_value in fallback.items():
                if key in {"平台作品键", "平台", "作品ID", "标题", "发布日期", "来源文件"}:
                    continue
                merged[key] = _merge_extra_value(key, merged.get(key), fallback_value)
            fallback_file = clean_value(fallback.get("来源文件"))
            preferred_file = clean_value(merged.get("来源文件"))
            if fallback_file and fallback_file != preferred_file:
                merged["来源文件"] = f"{preferred_file}+{fallback_file}" if preferred_file else fallback_file
        merged_rows.append(merged)
    return merged_rows


def build_preferred_platform_rows(detail_file_name: str, json_file_name: str, platform: str):
    detail_rows = build_excel_platform_rows(detail_file_name, platform)
    fallback_rows = build_json_platform_rows(json_file_name, platform)
    if detail_rows:
        return merge_preferred_and_fallback_rows(detail_rows, fallback_rows)
    return fallback_rows


def filter_by_min_date(rows, min_date_text: str, max_date_text: str = ""):
    min_cutoff = pd.to_datetime(min_date_text, errors="coerce") if min_date_text else pd.NaT
    max_cutoff = pd.to_datetime(max_date_text, errors="coerce") if max_date_text else pd.NaT
    if pd.isna(min_cutoff) and pd.isna(max_cutoff):
        return rows
    filtered = []
    for row in rows:
        ts = pd.to_datetime(row.get("发布日期"), errors="coerce")
        if pd.isna(ts):
            continue
        if pd.notna(min_cutoff) and ts < min_cutoff:
            continue
        if pd.notna(max_cutoff) and ts > max_cutoff:
            continue
        filtered.append(row)
    return filtered


def filter_zero_play(rows):
    """Drop rows whose effective play/read count is zero.

    Uses the same field priority as ``normalized_play_count`` in
    prepare_feishu_bitable_sync_v2.py so behaviour is consistent across
    Excel export and Feishu sync.
    """
    filtered = []
    for row in rows:
        play = to_number(row.get("播放量"))
        if play <= 0:
            # 小红书优先看阅读量
            read = to_number(row.get("阅读量"))
            if read <= 0:
                continue
        filtered.append(row)
    return filtered
