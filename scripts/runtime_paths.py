"""兼容 shim — 迁移到 core.paths。

实际实现已移至 core/paths.py。本文件仅 re-export，保证现有
`from runtime_paths import ...` 不破坏。重构阶段6清理时删除本文件。

DEPRECATED: 临时兼容，阶段6删除。新代码请用 `from core.paths import ...`。
"""

from core.paths import (  # noqa: F401
    APP_SUPPORT_NAME,
    PACKAGED_APP_NAMES,
    read_package_id,
    is_packaged_runtime,
    resolve_state_dir,
    resolve_downloads_dir,
    resolve_auth_dir,
    seed_state_from_bundle,
    resolve_base_dir,
)
