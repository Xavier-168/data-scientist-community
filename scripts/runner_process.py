"""兼容 shim — 迁移到 core.process。

实际实现已移至 core/process.py。本文件仅 re-export，保证现有
`from runner_process import ...` 不破坏。重构阶段6清理时删除本文件。

DEPRECATED: 临时兼容，阶段6删除。新代码请用 `from core.process import ...`。
"""

from core.process import (  # noqa: F401
    pid_alive,
    resolve_default_node_bin,
    sort_existing_paths,
    profile_path_candidates,
    command_uses_profile,
    terminate_profile_browsers,
    clear_profile_lock_files,
)
