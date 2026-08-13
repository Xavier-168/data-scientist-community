"""平台常量 — 唯一的平台标识/标签/profile前缀来源 (L1 基础层)。

所有需要平台 ID、中文标签、profile 目录前缀的模块必须从这里 import,
禁止重复定义 PLATFORM_LABELS / VALID_PLATFORM_IDS。

迁移自 runner_platforms.py。旧文件保留为兼容 shim。
"""

VALID_PLATFORM_IDS = (
    "douyin",
    "xiaohongshu",
    "bilibili",
    "kuaishou",
)

PLATFORM_PROFILE_PREFIX = {
    "douyin": "douyin",
    "xiaohongshu": "xiaohongshu",
    "bilibili": "bilibili",
    "kuaishou": "kuaishou",
}

PLATFORM_LABELS = {
    "douyin": "抖音",
    "xiaohongshu": "小红书",
    "bilibili": "B站",
    "kuaishou": "快手",
}


def normalize_platform_ids(values) -> list[str]:
    raw_items = []
    if isinstance(values, str):
        raw_items = values.split(",")
    elif isinstance(values, (list, tuple, set)):
        for item in values:
            if isinstance(item, str):
                raw_items.extend(item.split(","))
            elif item is not None:
                raw_items.append(str(item))
    elif values is not None:
        raw_items = [str(values)]

    normalized = []
    seen = set()
    for item in raw_items:
        platform_id = str(item or "").strip()
        if platform_id not in VALID_PLATFORM_IDS or platform_id in seen:
            continue
        normalized.append(platform_id)
        seen.add(platform_id)
    return normalized


def platform_label(platform_id: str) -> str:
    return PLATFORM_LABELS.get(
        str(platform_id or "").strip(),
        str(platform_id or "").strip() or "平台",
    )
