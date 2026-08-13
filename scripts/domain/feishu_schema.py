"""飞书表结构定义 (L2 领域层) — 5 张多维表的字段 schema 单一事实源。

定义平台明细/作品总表/同步日志/作品图表/作品增量 5 张表的飞书字段
(type 编码、ui_type、选项、upsert_key)。prepare 和 sync 共用本模块,
消除原先两文件各自维护表结构常量的隐式契约隐患。

迁移自 prepare_feishu_bitable_sync_v2.py。
"""

from typing import List, Optional

# ── 平台/内容类型选项（飞书单选/多选字段的选项配置）─────────────────────
PLATFORM_SELECT_OPTIONS = [
    {"name": "抖音", "color": 2, "hue": "Wathet", "lightness": "Lighter"},
    {"name": "小红书", "color": 5, "hue": "Red", "lightness": "Light"},
    {"name": "B站", "color": 4, "hue": "Blue", "lightness": "Lighter"},
    {"name": "快手", "color": 1, "hue": "Purple", "lightness": "Lighter"},
]
PLATFORM_FIELD = "平台"
CONTENT_TYPE_OPTIONS = [
    {"name": "视频", "color": 2},
    {"name": "图文", "color": 5},
]

# ── 图表表平台列（每平台一组指标列）─────────────────────────────────────
CHART_PLATFORM_FIELDS = [
    ("抖音", "抖音"),
    ("小红书", "小红书"),
    ("B站", "B站"),
    ("快手", "快手"),
]

# ── 平台明细业务字段（用户可见字段，2026-05-20 a03a 验收口径）────────────
PLATFORM_DETAIL_BUSINESS_FIELDS = [
    "视频标题", "视频平台", "视频发布日期", "总流量",
    "点赞量", "评论量", "分享量", "收藏量", "涨粉量",
    "平均播放进度", "完播率", "点赞率", "评论率", "分享率", "收藏率",
    "封标点击率", "3s跳出率",
]
PLATFORM_DETAIL_TECH_FIELDS = ["同步键", "平台作品键"]

# ── 图表表字段 ──────────────────────────────────────────────────────────
CHART_BASE_FIELDS = ["日期", "标题", "首发日期", "内容类型", "覆盖平台", "覆盖平台数", "同步键"]
CHART_METRIC_FIELDS = [
    "总流量", "点赞量", "评论量", "分享量", "收藏量", "涨粉量",
    "平均播放进度", "完播率", "点赞率", "评论率", "分享率", "收藏率",
    "封标点击率", "3s跳出率",
]
CHART_COLUMNS = [
    *CHART_BASE_FIELDS[:-1],
    *[
        f"{metric_name}_{suffix}"
        for _, suffix in CHART_PLATFORM_FIELDS
        for metric_name in CHART_METRIC_FIELDS
    ],
    "同步键",
]


def chart_platform_fields_for(platforms: Optional[List[str]] = None) -> List[tuple[str, str]]:
    """按指定平台过滤图表列（无参数时返回全部）。"""
    if not platforms:
        return list(CHART_PLATFORM_FIELDS)
    from core.platforms import PLATFORM_LABELS
    labels = {PLATFORM_LABELS.get(p, p) for p in platforms if p}
    return [(label, suffix) for label, suffix in CHART_PLATFORM_FIELDS if label in labels]


# ── 字段构造器（type 编码：1=Text 2=Number 3=SingleSelect 4=MultiSelect 5=DateTime）──

def make_text_field(name: str):
    return {"field_name": name, "type": 1, "ui_type": "Text"}


def make_number_field(name: str):
    return {"field_name": name, "type": 2, "ui_type": "Number"}


def make_single_select_field(name: str, options: List[dict]):
    return {"field_name": name, "type": 3, "ui_type": "SingleSelect", "property": {"options": options}}


def make_multi_select_field(name: str, options: List[dict]):
    return {"field_name": name, "type": 4, "ui_type": "MultiSelect", "property": {"options": options}}


def make_date_field(name: str):
    return {
        "field_name": name,
        "type": 5,
        "ui_type": "DateTime",
        "property": {"date_formatter": "yyyy-MM-dd"},
    }


def make_datetime_field(name: str):
    return {
        "field_name": name,
        "type": 5,
        "ui_type": "DateTime",
        "property": {"date_formatter": "yyyy-MM-dd HH:mm"},
    }


def build_table_definitions_v2(chart_platforms: Optional[List[str]] = None):
    """构建 5 张飞书多维表的完整字段定义。

    返回的 table_definitions 是 prepare→sync 之间 JSON payload 的核心契约:
    每张表含 name / upsert_key / fields / visible_fields / prune_missing_records。
    """
    chart_fields = chart_platform_fields_for(chart_platforms)
    return [
        {
            "name": "平台明细V2",
            "default_view_name": "主视图",
            "upsert_key": "平台作品键",
            "prune_missing_records": True,
            "visible_fields": PLATFORM_DETAIL_BUSINESS_FIELDS,
            "fields": [
                make_text_field("视频标题"),
                make_single_select_field("视频平台", PLATFORM_SELECT_OPTIONS),
                make_date_field("视频发布日期"),
                make_text_field("同步键"),
                make_text_field("平台作品键"),
                make_number_field("总流量"),
                make_number_field("点赞量"),
                make_number_field("评论量"),
                make_number_field("分享量"),
                make_number_field("收藏量"),
                make_number_field("涨粉量"),
                make_number_field("平均播放进度"),
                make_number_field("完播率"),
                make_number_field("点赞率"),
                make_number_field("评论率"),
                make_number_field("分享率"),
                make_number_field("收藏率"),
                make_number_field("封标点击率"),
                make_number_field("3s跳出率"),
            ],
        },
        {
            "name": "作品总表V2",
            "default_view_name": "主视图",
            "upsert_key": "同步键",
            "prune_missing_records": True,
            "visible_fields": [
                "标题", "首发日期", "内容类型", "覆盖平台", "覆盖平台数",
                "总播放量", "总点赞量", "总收藏量", "总评论量", "总分享量", "总涨粉量",
                "总互动量", "最近更新时间",
            ],
            "fields": [
                make_text_field("标题"),
                make_date_field("首发日期"),
                make_multi_select_field("内容类型", CONTENT_TYPE_OPTIONS),
                make_multi_select_field("覆盖平台", PLATFORM_SELECT_OPTIONS),
                make_number_field("覆盖平台数"),
                make_number_field("总播放量"),
                make_number_field("总点赞量"),
                make_number_field("总收藏量"),
                make_number_field("总评论量"),
                make_number_field("总分享量"),
                make_number_field("总涨粉量"),
                make_number_field("总互动量"),
                make_datetime_field("最近更新时间"),
                make_text_field("同步键"),
            ],
        },
        {
            "name": "同步日志V2",
            "default_view_name": "主视图",
            "upsert_key": "批次同步键",
            "fields": [
                make_date_field("同步日期"),
                make_text_field("纳入平台"),
                make_number_field("平台明细记录数"),
                make_number_field("作品总表记录数"),
                make_number_field("作品图表表记录数"),
                make_number_field("作品增量表记录数"),
                make_number_field("新增作品数"),
                make_number_field("持续增长作品数"),
                make_text_field("备注"),
                make_text_field("批次同步键"),
            ],
        },
        {
            "name": "作品图表表",
            "default_view_name": "主视图",
            "upsert_key": "同步键",
            "prune_missing_records": True,
            "visible_fields": [
                "日期",
                "标题",
                "首发日期",
                "内容类型",
                "覆盖平台",
                "覆盖平台数",
                *[
                    f"{metric_label}_{suffix}"
                    for _, suffix in chart_fields
                    for metric_label in CHART_METRIC_FIELDS
                ],
            ],
            "fields": [
                make_date_field("日期"),
                make_text_field("标题"),
                make_date_field("首发日期"),
                make_multi_select_field("内容类型", CONTENT_TYPE_OPTIONS),
                make_multi_select_field("覆盖平台", PLATFORM_SELECT_OPTIONS),
                make_number_field("覆盖平台数"),
                *[
                    make_number_field(f"{metric_label}_{suffix}")
                    for _, suffix in chart_fields
                    for metric_label in CHART_METRIC_FIELDS
                ],
                make_text_field("同步键"),
            ],
        },
        {
            "name": "作品增量表",
            "default_view_name": "主视图",
            "upsert_key": "同步键",
            "prune_missing_records": True,
            "visible_fields": [
                "日期", "标题", "首发日期", "覆盖平台", "覆盖平台数",
                "对比快照日期", "对比跨度天数",
                "总播放增量", "总点赞增量", "总收藏增量", "总评论增量", "总分享增量", "总涨粉增量",
                "总互动增量", "日均播放增量", "日均互动增量", "增量状态",
            ],
            "fields": [
                make_date_field("日期"),
                make_text_field("标题"),
                make_date_field("首发日期"),
                make_multi_select_field("覆盖平台", PLATFORM_SELECT_OPTIONS),
                make_number_field("覆盖平台数"),
                make_date_field("对比快照日期"),
                make_number_field("对比跨度天数"),
                make_number_field("总播放增量"),
                make_number_field("总点赞增量"),
                make_number_field("总收藏增量"),
                make_number_field("总评论增量"),
                make_number_field("总分享增量"),
                make_number_field("总涨粉增量"),
                make_number_field("总互动增量"),
                make_number_field("日均播放增量"),
                make_number_field("日均互动增量"),
                make_text_field("增量状态"),
                make_text_field("同步键"),
            ],
        },
    ]


def project_rows_to_defined_fields(rows: List[dict], field_defs: List[dict]) -> List[dict]:
    """把行数据按字段定义投影(只保留定义里的字段)。"""
    field_names = [field["field_name"] for field in field_defs]
    projected = []
    for row in rows:
        projected.append({name: row.get(name) for name in field_names if name in row})
    return projected
