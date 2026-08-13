"""作品分组 (L2 领域层) — 标题清洗 → 上下集识别 → 模糊聚类 → 作品组ID。

把跨平台的同一作品(如"vlog 第一集"在抖音/B站/快手的版本)归为同一组,
生成作品组 key (WORK-NNNN)。纯函数,不碰文件/网络。

迁移自 prepare_feishu_bitable_sync_v2.py。
"""

import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Dict, List, Optional

from platform_source_rows import normalize_title

# 标题相似度阈值:达到则判定为同一作品的多个平台版本
TITLE_SIMILARITY_THRESHOLD = 0.70
# episode 正则的结尾兜底(允许结尾标点/数字/空白)
_EPISODE_TAIL = r"(?:\s+|[，,。.!！?？:：\s【】《》「」『』<>〈〉\d]|$)"


def _compact_title_identity(value: str) -> str:
    """标题紧凑化:去话题标签/@用户/分隔符/常见噪声词,用于相似度比对。"""
    text = normalize_title(value).lower()
    if not text:
        return ""
    text = text.replace("＃", "#")
    text = re.sub(r"\s*#.*$", "", text)
    text = re.sub(r"#[^#]+", " ", text)
    text = re.sub(r"[@＠][^\s]+", " ", text)
    text = re.sub(r"[|｜/·•\-—_]+", " ", text)
    text = re.sub(r"\b(vlog|a roll|a-roll|4k)\b", " ", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "", text)
    return text.strip()


def _parse_episode_parts(value: str) -> Optional[tuple[str, str]]:
    """识别上下集/Part/第N集,返回 (base_title, marker)。"""
    text = normalize_title(value)
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
        if not match:
            continue
        base = _compact_title_identity(match.group(1))
        if not base:
            return None
        if kind == "numbered":
            marker = f"第{match.group(2)}{match.group(3)}"
        else:
            marker = f"{kind}{match.group(2).lower()}"
        return base, marker
    return None


def episode_title_key(value: str) -> Optional[str]:
    parts = _parse_episode_parts(value)
    if not parts:
        return None
    return f"{parts[0]}{parts[1]}"


def canonical_title(value: str) -> str:
    return episode_title_key(value) or _compact_title_identity(value)


def title_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    ratio = SequenceMatcher(None, a, b).ratio()
    if a in b or b in a:
        ratio = max(ratio, min(len(a), len(b)) / max(len(a), len(b)))
    return ratio


class UnionFind:
    """并查集(带按秩合并 + 路径压缩),用于把相似标题聚类成同一组。"""

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


def _fuzzy_group(entries: List, key_fn) -> List[List[int]]:
    """按 key_fn 提取的相似键,用 UnionFind 把 entries 聚类。"""
    if not entries:
        return []
    uf = UnionFind(len(entries))
    for left in range(len(entries)):
        for right in range(left + 1, len(entries)):
            if title_similarity(key_fn(entries[left]), key_fn(entries[right])) >= TITLE_SIMILARITY_THRESHOLD:
                uf.union(left, right)
    grouped: Dict[int, List[int]] = {}
    for pos, entry in enumerate(entries):
        grouped.setdefault(uf.find(pos), []).append(entry[0])
    return list(grouped.values())


def assign_work_keys(rows: List[dict]) -> Dict[str, str]:
    """把每个平台作品映射到作品组 key (单一作品用自身平台作品键,多平台合集用 WORK-NNNN)。"""
    if not rows:
        return {}

    episode_entries: List[tuple[int, str, str]] = []
    plain_entries: List[tuple[int, str]] = []
    fallback_entries: List[int] = []
    for idx, row in enumerate(rows):
        title = row.get("标题", "")
        episode_parts = _parse_episode_parts(title)
        if episode_parts:
            episode_entries.append((idx, episode_parts[0], episode_parts[1]))
            continue
        title_key = _compact_title_identity(title)
        if title_key:
            plain_entries.append((idx, title_key))
        else:
            fallback_entries.append(idx)

    components: List[List[int]] = []
    if episode_entries:
        markers = sorted({marker for _, _, marker in episode_entries})
        for marker in markers:
            bucket = [(idx, base) for idx, base, entry_marker in episode_entries if entry_marker == marker]
            components.extend(_fuzzy_group(bucket, lambda entry: entry[1]))

    components.extend(_fuzzy_group(plain_entries, lambda entry: entry[1]))

    components.extend([idx] for idx in fallback_entries)
    components.sort(key=lambda members: min(members))

    work_keys: Dict[str, str] = {}
    seq = 1
    for members in components:
        member_rows = [rows[idx] for idx in members]
        key_source = member_rows[0]["平台作品键"] if len(members) == 1 else f"WORK-{seq:04d}"
        if len(members) > 1:
            seq += 1
        for idx in members:
            work_keys[rows[idx]["平台作品键"]] = key_source
    return work_keys
