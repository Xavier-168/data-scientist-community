//! 在系统文件管理器中展示路径：Finder / Explorer。

/// 在文件管理器中定位并选中给定路径。
pub fn reveal_path(path: &std::path::Path) -> Result<(), String> {
    if !path.exists() {
        return Err("reveal_target_missing".into());
    }
    #[cfg(target_os = "macos")]
    {
        let status = std::process::Command::new("/usr/bin/open")
            .arg("-R")
            .arg(path)
            .status()
            .map_err(|_| "reveal_failed".to_string())?;
        if !status.success() {
            return Err("reveal_failed".into());
        }
        Ok(())
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // explorer 的 /select, 参数含逗号与可能的空格，走 raw_arg 原样传递
        let selection = format!("/select,{}", path.display());
        std::process::Command::new("explorer.exe")
            .raw_arg(selection)
            .spawn()
            .map(|_| ())
            .map_err(|_| "reveal_failed".to_string())
    }
    #[cfg(not(any(target_os = "macos", windows)))]
    {
        let _ = path;
        Err("reveal_unsupported".into())
    }
}
