"""兼容 shim — 迁移到 core.platforms。

实际实现已移至 core/platforms.py。本文件仅 re-export，保证现有
`from runner_platforms import ...` 不破坏。重构阶段6清理时删除本文件。

DEPRECATED: 临时兼容，阶段6删除。新代码请用 `from core.platforms import ...`。
"""

from core.platforms import (  # noqa: F401
    VALID_PLATFORM_IDS,
    PLATFORM_PROFILE_PREFIX,
    PLATFORM_LABELS,
    normalize_platform_ids,
    platform_label,
)
