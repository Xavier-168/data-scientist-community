"""原子 IO — 唯一的 JSON 读写 + 安全删除入口 (L1 基础层)。

所有 JSON 持久化 (config / progress / auth_state / history) 必须用这里的
原子写,禁止散落的 json.dump (临时调试除外)。

迁移自 runner_io.py。旧文件保留为兼容 shim。
"""

import json
import os
import shutil
import tempfile
import time


_REPLACE_BUSY_ATTEMPTS = 4
_REPLACE_BUSY_DELAY_SECONDS = 0.3


def _os_replace_with_retry(temp_path: str, file_path: str) -> None:
    """Windows 上目标文件被并发读取的瞬间（supervisor 每 250ms 轮询进度）
    os.replace 会抛 PermissionError，短暂重试消化掉读写竞态窗口。"""
    for attempt in range(_REPLACE_BUSY_ATTEMPTS):
        try:
            os.replace(temp_path, file_path)
            return
        except PermissionError:
            if attempt < _REPLACE_BUSY_ATTEMPTS - 1:
                time.sleep(_REPLACE_BUSY_DELAY_SECONDS)
            else:
                raise


def ensure_parent_dir(file_path: str) -> None:
    os.makedirs(os.path.dirname(file_path), exist_ok=True)


def write_json_file_atomically(file_path: str, payload, *, indent: int | None = 2) -> None:
    ensure_parent_dir(file_path)
    directory = os.path.dirname(file_path)
    prefix = f".{os.path.basename(file_path)}."
    fd, temp_path = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        _os_replace_with_retry(temp_path, file_path)
    finally:
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def load_json_dict(file_path: str) -> dict:
    if not os.path.exists(file_path):
        return {}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def read_json_file(file_path, fallback):
    if not os.path.exists(file_path):
        return fallback
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback


def path_within_root(target_path: str, root_path: str) -> bool:
    try:
        return os.path.commonpath(
            [os.path.realpath(target_path), os.path.realpath(root_path)]
        ) == os.path.realpath(root_path)
    except ValueError:
        return False


def remove_file_if_exists(file_path: str, *, allowed_root: str) -> bool:
    if not path_within_root(file_path, allowed_root):
        raise ValueError(f"refuse_to_remove_outside_root: {file_path}")
    if not os.path.exists(file_path):
        return False
    if os.path.isdir(file_path):
        raise ValueError(f"expected_file_but_got_directory: {file_path}")
    os.remove(file_path)
    return True


def remove_tree_if_exists(dir_path: str, *, allowed_root: str) -> bool:
    if not path_within_root(dir_path, allowed_root):
        raise ValueError(f"refuse_to_remove_outside_root: {dir_path}")
    if not os.path.exists(dir_path):
        return False
    if not os.path.isdir(dir_path):
        raise ValueError(f"expected_directory_but_got_file: {dir_path}")
    # Windows 删除目录树时任一文件被占用即中止；重试一次后放弃残留
    # （ignore_errors），调用方语义是“尽量清理”而非强一致删除。
    for attempt in range(2):
        try:
            shutil.rmtree(dir_path)
            return True
        except OSError:
            if attempt == 0:
                time.sleep(0.3)
    shutil.rmtree(dir_path, ignore_errors=True)
    return True
