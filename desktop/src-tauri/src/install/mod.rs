//! 安装管理：macOS 为应用内自安装（ditto/codesign 事务），
//! Windows 由 NSIS 安装器承担安装/卸载/升级，壳内仅保留
//! 取消/空闲语义以对齐启动编排器。

#[cfg(target_os = "macos")]
mod macos;

#[cfg(target_os = "macos")]
pub use macos::*;

#[cfg(windows)]
mod windows_impl;

#[cfg(windows)]
pub use windows_impl::*;
