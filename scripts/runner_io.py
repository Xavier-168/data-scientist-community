"""兼容 shim — 迁移到 core.io。

实际实现已移至 core/io.py。本文件仅 re-export，保证现有
`from runner_io import ...` 不破坏。重构阶段6清理时删除本文件。

DEPRECATED: 临时兼容，阶段6删除。新代码请用 `from core.io import ...`。
"""

from core.io import (  # noqa: F401
    ensure_parent_dir,
    write_json_file_atomically,
    load_json_dict,
    read_json_file,
    path_within_root,
    remove_file_if_exists,
    remove_tree_if_exists,
)
