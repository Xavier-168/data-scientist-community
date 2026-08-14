#!/usr/bin/env python3
import argparse
import os
import re
import sys
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd

# 统一从 L1 基础层取路径和平台常量
_SELF_DIR = os.path.dirname(os.path.abspath(__file__))
if _SELF_DIR not in sys.path:
    sys.path.insert(0, _SELF_DIR)

from core.paths import resolve_base_dir, resolve_downloads_dir  # noqa: E402
from core.platforms import VALID_PLATFORM_IDS  # noqa: E402

STANDARD_COLUMNS = [
    "平台",
    "平台作品ID",
    "作品ID",
    "标题",
    "发布日期",
    "曝光量",
    "播放量",
    "点赞量",
    "收藏量",
    "评论量",
    "分享量",
    "内容类型",
    "更新时间",
    "group_id",
]

BASE_DIR = Path(resolve_base_dir())
DOWNLOADS_DIR = Path(resolve_downloads_dir(BASE_DIR))

TITLE_SIMILARITY_THRESHOLD = 0.40
_EPISODE_TAIL = r"(?:\s+|[，,。.!！?？:：\s【】《》「」『』<>〈〉\d]|$)"


def read_excel_safe(path: str) -> pd.DataFrame:
    if not path or not os.path.exists(path):
        return pd.DataFrame()
    try:
        return pd.read_excel(path, dtype=str)
    except Exception:
        return pd.DataFrame()


def clean_value(value):
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value).strip()
    if text.lower() in ("nan", "none", "null"):
        return ""
    return text


def parse_platforms(raw_value) -> list[str]:
    raw_items = []
    if isinstance(raw_value, str):
        raw_items = raw_value.split(",")
    elif isinstance(raw_value, (list, tuple, set)):
        for item in raw_value:
            if isinstance(item, str):
                raw_items.extend(item.split(","))
            elif item is not None:
                raw_items.append(str(item))
    elif raw_value is not None:
        raw_items = [str(raw_value)]

    normalized = []
    seen = set()
    for item in raw_items:
        platform = clean_value(item)
        if platform not in VALID_PLATFORM_IDS or platform in seen:
            continue
        normalized.append(platform)
        seen.add(platform)
    return normalized


def normalize_content_type(row: pd.Series, platform: str) -> str:
    raw = clean_value(row.get("内容类型") or row.get("作品类型") or row.get("类型"))
    text = raw.lower()
    # 精确匹配已归一化的英文值（来自各平台 export 脚本）
    if text == "image_text":
        return "image_text"
    if text == "video":
        return "video"
    # 中文关键词兜底
    if any(token in text for token in ("图文", "文章", "post", "article", "image")):
        return "image_text"
    if any(token in text for token in ("视频", "video")):
        return "video"
    return "video"


def normalize_douyin(df: pd.DataFrame, updated_at: str) -> pd.DataFrame:
    rows = []
    if df.empty:
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    for _, row in df.iterrows():
        work_id = clean_value(row.get("作品ID"))
        if not work_id:
            continue

        rows.append(
            {
                "平台": "douyin",
                "平台作品ID": f"douyin:{work_id}",
                "作品ID": work_id,
                "标题": clean_value(row.get("标题")),
                "发布日期": clean_value(row.get("发布日期")),
                "曝光量": clean_value(row.get("曝光量")),
                "播放量": clean_value(row.get("播放量")),
                "点赞量": clean_value(row.get("点赞量")),
                "收藏量": clean_value(row.get("收藏量")),
                "评论量": clean_value(row.get("评论量")),
                "分享量": clean_value(row.get("分享量")),
                "内容类型": normalize_content_type(row, "douyin"),
                "更新时间": updated_at,
                "group_id": "",
            }
        )

    return pd.DataFrame(rows, columns=STANDARD_COLUMNS)


def normalize_xiaohongshu(df: pd.DataFrame, updated_at: str) -> pd.DataFrame:
    rows = []
    if df.empty:
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    for _, row in df.iterrows():
        work_id = clean_value(row.get("作品ID"))
        if not work_id:
            continue

        play_count = clean_value(row.get("播放量")) or clean_value(row.get("阅读量"))

        rows.append(
            {
                "平台": "xiaohongshu",
                "平台作品ID": f"xiaohongshu:{work_id}",
                "作品ID": work_id,
                "标题": clean_value(row.get("标题")),
                "发布日期": clean_value(row.get("发布日期")),
                "曝光量": clean_value(row.get("曝光量")),
                "播放量": play_count,
                "点赞量": clean_value(row.get("点赞量")),
                "收藏量": clean_value(row.get("收藏量")),
                "评论量": clean_value(row.get("评论量")),
                "分享量": clean_value(row.get("分享量")),
                "内容类型": normalize_content_type(row, "xiaohongshu"),
                "更新时间": updated_at,
                "group_id": "",
            }
        )

    return pd.DataFrame(rows, columns=STANDARD_COLUMNS)


def normalize_publish_date_for_sort(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce")


def normalize_bilibili(df: pd.DataFrame, updated_at: str) -> pd.DataFrame:
    rows = []
    if df.empty:
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    for _, row in df.iterrows():
        work_id = clean_value(row.get("作品ID"))
        if not work_id:
            continue

        rows.append(
            {
                "平台": "bilibili",
                "平台作品ID": f"bilibili:{work_id}",
                "作品ID": work_id,
                "标题": clean_value(row.get("标题")),
                "发布日期": clean_value(row.get("发布日期")),
                "曝光量": clean_value(row.get("曝光量")),
                "播放量": clean_value(row.get("播放量")),
                "点赞量": clean_value(row.get("点赞量")),
                "收藏量": clean_value(row.get("收藏量")),
                "评论量": clean_value(row.get("评论量")),
                "分享量": clean_value(row.get("分享量")),
                "内容类型": normalize_content_type(row, "bilibili"),
                "更新时间": updated_at,
                "group_id": "",
            }
        )

    return pd.DataFrame(rows, columns=STANDARD_COLUMNS)

def normalize_kuaishou(df: pd.DataFrame, updated_at: str) -> pd.DataFrame:
    rows = []
    if df.empty:
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    for _, row in df.iterrows():
        work_id = clean_value(row.get("作品ID"))
        if not work_id:
            continue

        rows.append(
            {
                "平台": "kuaishou",
                "平台作品ID": f"kuaishou:{work_id}",
                "作品ID": work_id,
                "标题": clean_value(row.get("标题")),
                "发布日期": clean_value(row.get("发布日期")),
                "曝光量": clean_value(row.get("曝光量")),
                "播放量": clean_value(row.get("播放量")),
                "点赞量": clean_value(row.get("点赞量")),
                "收藏量": clean_value(row.get("收藏量")),
                "评论量": clean_value(row.get("评论量")),
                "分享量": clean_value(row.get("分享量")),
                "内容类型": normalize_content_type(row, "kuaishou"),
                "更新时间": updated_at,
                "group_id": "",
            }
        )

    return pd.DataFrame(rows, columns=STANDARD_COLUMNS)


# ---------------------------------------------------------------------------
# Group-ID assignment: cluster works across platforms by normalized title.
# Keep these title-identity rules aligned with prepare_feishu_bitable_sync_v2.py;
# this file powers merged raw exports, while the V2 module powers Excel/Feishu.
# ---------------------------------------------------------------------------

def _compact_title_identity(value) -> str:
    text = clean_value(value).replace("\n", " ")
    if not text:
        return ""
    text = text.replace("＃", "#")
    text = re.sub(r"\s*#.*$", "", text)
    text = text.lower()
    text = re.sub(r"[@＠][^\s]+", " ", text)
    text = re.sub(r"[|｜/·•\-—_]+", " ", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "", text)
    return text.strip()


def _parse_episode_parts(value) -> tuple[str, str] | None:
    text = clean_value(value).replace("\n", " ")
    if not text:
        return None
    patterns = (
        (r"^(.{4,}?)[（(]\s*(上|中|下|[一二三四五六七八九十\d]+)\s*[）)]" + _EPISODE_TAIL, "episode"),
        (r"^(.{4,}?)(?:[\s，,。.!！?？:：]+)(上|中|下)" + _EPISODE_TAIL, "episode"),
        (r"^(.{4,}?)\b(?:part|pt|p)\s*([\d一二三四五六七八九十]+)" + _EPISODE_TAIL, "part"),
        (r"^(.{4,}?)第\s*([\d一二三四五六七八九十]+)\s*(集|期|部分)" + _EPISODE_TAIL, "numbered"),
    )
    for pattern, kind in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            base = _compact_title_identity(match.group(1))
            if not base:
                return None
            if kind == "numbered":
                marker = f"第{match.group(2)}{match.group(3)}"
            else:
                marker = f"{kind}{match.group(2).lower()}"
            return base, marker
    return None


def episode_title_key(value) -> str:
    parts = _parse_episode_parts(value)
    if not parts:
        return ""
    return f"{parts[0]}{parts[1]}"


def title_group_key(value) -> str:
    return episode_title_key(value) or _compact_title_identity(value)


def title_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    ratio = SequenceMatcher(None, a, b).ratio()
    if a in b or b in a:
        ratio = max(ratio, min(len(a), len(b)) / max(len(a), len(b)))
    return ratio


class UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x: int, y: int) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1


def _fuzzy_group(entries: list, key_fn, similarity_threshold: float) -> list[list[int]]:
    if not entries:
        return []
    uf = UnionFind(len(entries))
    for left in range(len(entries)):
        for right in range(left + 1, len(entries)):
            if title_similarity(key_fn(entries[left]), key_fn(entries[right])) >= similarity_threshold:
                uf.union(left, right)
    grouped: dict[int, list[int]] = {}
    for pos, entry in enumerate(entries):
        grouped.setdefault(uf.find(pos), []).append(entry[0])
    return list(grouped.values())


def assign_group_ids(df: pd.DataFrame, similarity_threshold: float = TITLE_SIMILARITY_THRESHOLD) -> pd.DataFrame:
    """Assign ``group_id`` by episode-safe title identity and fuzzy title match.

    Publishing dates are intentionally ignored: Douyin may be contractually
    required to publish days before other platforms. Explicit episode markers
    share the same marker and then group by fuzzy base; non-episode titles are
    grouped at 70%+ similarity.
    """
    if df.empty:
        return df

    n = len(df)
    episode_entries: list[tuple[int, str, str]] = []
    plain_entries: list[tuple[int, str]] = []
    for idx, title in enumerate(df["标题"].fillna("").astype(str).tolist()):
        episode_parts = _parse_episode_parts(title)
        if episode_parts:
            episode_entries.append((idx, episode_parts[0], episode_parts[1]))
            continue
        key = _compact_title_identity(title)
        if not key:
            continue
        plain_entries.append((idx, key))

    components: list[list[int]] = []
    if episode_entries:
        markers = sorted({marker for _, _, marker in episode_entries})
        for marker in markers:
            bucket = [(idx, base) for idx, base, entry_marker in episode_entries if entry_marker == marker]
            components.extend(_fuzzy_group(bucket, lambda entry: entry[1], similarity_threshold))
    components.extend(_fuzzy_group(plain_entries, lambda entry: entry[1], similarity_threshold))

    group_ids = [""] * n
    gid_counter = 0
    for members in sorted(components, key=lambda item: min(item)):
        if len(members) < 2:
            continue
        gid_counter += 1
        gid = f"G{gid_counter:04d}"
        for idx in members:
            group_ids[idx] = gid

    df = df.copy()
    df["group_id"] = group_ids
    return df


def build_all_channels(
    douyin_path: str,
    xhs_path: str,
    bili_path: str,
    kuaishou_path: str,
    min_publish_date=None,
    max_publish_date=None,
    platforms=None,
) -> pd.DataFrame:
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    allowed_platforms = set(parse_platforms(platforms))

    def include(platform: str) -> bool:
        return not allowed_platforms or platform in allowed_platforms

    douyin_df = normalize_douyin(read_excel_safe(douyin_path), updated_at) if include("douyin") else pd.DataFrame(columns=STANDARD_COLUMNS)
    xhs_df = normalize_xiaohongshu(read_excel_safe(xhs_path), updated_at) if include("xiaohongshu") else pd.DataFrame(columns=STANDARD_COLUMNS)
    bili_df = normalize_bilibili(read_excel_safe(bili_path), updated_at) if include("bilibili") else pd.DataFrame(columns=STANDARD_COLUMNS)
    ks_df = normalize_kuaishou(read_excel_safe(kuaishou_path), updated_at) if include("kuaishou") else pd.DataFrame(columns=STANDARD_COLUMNS)

    merged = pd.concat(
        [douyin_df, xhs_df, bili_df, ks_df],
        ignore_index=True,
    )
    if merged.empty:
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    merged["__date"] = normalize_publish_date_for_sort(merged["发布日期"])
    cutoff_ts = pd.to_datetime(min_publish_date, errors="coerce") if min_publish_date else pd.NaT
    if pd.notna(cutoff_ts):
        merged = merged.loc[merged["__date"].notna() & (merged["__date"] >= cutoff_ts)]
        if merged.empty:
            return pd.DataFrame(columns=STANDARD_COLUMNS)
    max_cutoff_ts = pd.to_datetime(max_publish_date, errors="coerce") if max_publish_date else pd.NaT
    if pd.notna(max_cutoff_ts):
        merged = merged.loc[merged["__date"].notna() & (merged["__date"] <= max_cutoff_ts)]
        if merged.empty:
            return pd.DataFrame(columns=STANDARD_COLUMNS)

    merged = merged.sort_values(by=["__date", "平台", "作品ID"], kind="stable")
    merged = merged.drop_duplicates(subset=["平台作品ID"], keep="last")
    merged = merged.sort_values(by=["__date", "平台", "作品ID"], kind="stable")
    merged = merged.drop(columns=["__date"])
    merged = merged.reset_index(drop=True)
    merged = merged.fillna("")

    # Assign group_id for cross-platform dedup
    merged = assign_group_ids(merged)

    return merged[STANDARD_COLUMNS]


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge Douyin + Xiaohongshu + Bilibili + Kuaishou into a single excel.")
    parser.add_argument("--douyin", default=str(DOWNLOADS_DIR / "all_videos.xlsx"))
    parser.add_argument("--xhs", default=str(DOWNLOADS_DIR / "xiaohongshu_all_videos.xlsx"))
    parser.add_argument("--bili", default=str(DOWNLOADS_DIR / "bilibili_all_videos.xlsx"))
    parser.add_argument("--kuaishou", default=str(DOWNLOADS_DIR / "kuaishou_all_videos.xlsx"))
    parser.add_argument("--platforms", default="")
    parser.add_argument("--output", default=str(DOWNLOADS_DIR / "all_channels_videos.xlsx"))
    parser.add_argument("--min-date", default=os.environ.get("MIN_PUBLISH_DATE", "2026-01-01"))
    parser.add_argument("--max-date", default=os.environ.get("MAX_PUBLISH_DATE", ""))
    args = parser.parse_args()

    out_df = build_all_channels(
        args.douyin,
        args.xhs,
        args.bili,
        args.kuaishou,
        args.min_date,
        args.max_date,
        args.platforms,
    )
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    out_df.to_excel(args.output, index=False)
    grouped = (out_df["group_id"] != "").sum()
    print(f"[all-channels] rows={len(out_df)} grouped={grouped} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
