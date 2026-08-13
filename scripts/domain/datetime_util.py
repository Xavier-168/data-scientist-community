"""飞书日期/时间戳转换 (L2 领域层)。

纯函数:字符串日期 → 飞书毫秒时间戳(上海时区)。
不碰文件/网络,可独立单测。

迁移自 prepare_feishu_bitable_sync_v2.py。
"""

from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _clean_text(value) -> str:
    """轻量清洗,避免对 platform_source_rows 的硬依赖(本模块保持纯领域)。"""
    if value is None:
        return ""
    import math
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "null"} else text


def to_feishu_date_ms(value: str) -> Optional[int]:
    """字符串日期 → 飞书日期字段毫秒戳(当天 00:00,上海时区)。"""
    text = _clean_text(value)
    if not text:
        return None
    ts = pd.to_datetime(text, errors="coerce")
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize(ASIA_SHANGHAI)
    else:
        ts = ts.tz_convert(ASIA_SHANGHAI)
    dt = ts.to_pydatetime().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(dt.timestamp() * 1000)


def to_feishu_datetime_ms(value: str) -> Optional[int]:
    """字符串日期 → 飞书日期时间字段毫秒戳(上海时区)。"""
    text = _clean_text(value)
    if not text:
        return None
    ts = pd.to_datetime(text, errors="coerce")
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize(ASIA_SHANGHAI)
    else:
        ts = ts.tz_convert(ASIA_SHANGHAI)
    return int(ts.to_pydatetime().timestamp() * 1000)
