//! 平台抽象层：Unix（macOS）与 Windows 的进程/路径/环境差异统一收口。
//!
//! - 进程树终止：Unix 用进程组信号；Windows 用 Job Object
//!   （KILL_ON_JOB_CLOSE）+ taskkill /T /F 兜底。
//! - 路径展示：Finder `open -R` / Explorer `/select,`。
//! - 运行时目录名：python-arm64|node-arm64（Unix） vs
//!   python-x86_64|node-x86_64（Windows），可执行名 python3|/bin/node vs
//!   python.exe|node.exe。

pub mod env_paths;
pub mod process;
pub mod reveal;

/// 运行时包内目录前缀（core=python，collector=node）。
pub fn runtime_python_prefix() -> &'static str {
    if cfg!(windows) {
        "runtime/python-x86_64/"
    } else {
        "runtime/python-arm64/"
    }
}

pub fn runtime_node_prefix() -> &'static str {
    if cfg!(windows) {
        "runtime/node-x86_64/"
    } else {
        "runtime/node-arm64/"
    }
}

/// 清单 required_files 中 Python 解释器的路径后缀。
pub fn python_entry_suffix() -> &'static str {
    if cfg!(windows) {
        "python.exe"
    } else {
        "/bin/python3"
    }
}

/// 清单 required_files 中 Node 可执行的路径后缀。
pub fn node_entry_suffix() -> &'static str {
    if cfg!(windows) {
        "node.exe"
    } else {
        "/bin/node"
    }
}
