//! 为 runner 子进程构建的最小环境变量。

/// 剥离 `std::fs::canonicalize` 在 Windows 上加的 `\\?\` / `\\?\UNC\` 前缀。
/// 子进程链（cmd.exe 执行 .cmd、node 的 file URL、PATH 目录）都无法处理
/// verbatim 前缀路径，交给子进程的路径必须是普通绝对路径。
#[cfg(windows)]
pub fn strip_verbatim(path: &std::path::Path) -> std::path::PathBuf {
    let text = path.as_os_str().to_string_lossy();
    if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
        std::path::PathBuf::from(format!(r"\\{rest}"))
    } else if let Some(rest) = text.strip_prefix(r"\\?\") {
        std::path::PathBuf::from(rest.to_string())
    } else {
        path.to_path_buf()
    }
}

/// sidecar 环境的 PATH：node 目录优先，随后是系统目录。
/// Unix 用 ':' 与 /usr/bin；Windows 用 ';' 与系统目录。
pub fn runner_path(node_bin_dir: &std::path::Path) -> std::ffi::OsString {
    #[cfg(unix)]
    {
        let mut value = std::ffi::OsString::new();
        value.push(node_bin_dir.as_os_str());
        value.push(":/usr/bin:/bin:/usr/sbin:/sbin");
        value
    }
    #[cfg(windows)]
    {
        // System32 提供 cmd/taskkill/netstat；WindowsPowerShell 供进程检查兜底。
        let system_root =
            std::env::var_os("SystemRoot").unwrap_or_else(|| "C:\\Windows".into());
        let mut value = std::ffi::OsString::new();
        value.push(node_bin_dir.as_os_str());
        value.push(";");
        value.push(&system_root);
        value.push("\\System32;");
        value.push(&system_root);
        value.push(";");
        value.push(&system_root);
        value.push("\\System32\\Wbem;");
        value.push(&system_root);
        value.push("\\System32\\WindowsPowerShell\\v1.0");
        value
    }
}

/// 环境变量透传白名单（父进程 → runner）。
pub fn passthrough_env_keys() -> &'static [&'static str] {
    #[cfg(target_os = "macos")]
    {
        &["__CF_USER_TEXT_ENCODING", "SECURITYSESSIONID"]
    }
    // Windows：env_clear 后必须透传系统变量——缺 SYSTEMROOT 会导致
    // Winsock 无法初始化（WinError 10106，socket 绑定失败）；
    // COMSPEC 供 runner 调起 .cmd 采集包装；APPDATA/LOCALAPPDATA 供
    // 状态目录与 Playwright 浏览器注册表解析。
    #[cfg(windows)]
    {
        &[
            "SYSTEMROOT",
            "SYSTEMDRIVE",
            "COMSPEC",
            "PATHEXT",
            "TEMP",
            "TMP",
            "USERNAME",
            "USERPROFILE",
            "HOMEDRIVE",
            "HOMEPATH",
            "APPDATA",
            "LOCALAPPDATA",
            "PROGRAMDATA",
            "PUBLIC",
            "ALLUSERSPROFILE",
            "NUMBER_OF_PROCESSORS",
            "PROCESSOR_ARCHITECTURE",
        ]
    }
    #[cfg(not(any(target_os = "macos", windows)))]
    {
        &[]
    }
}

/// 状态目录根（每用户应用数据）。
pub fn app_state_root(app_name: &str, package_id: &str) -> Result<std::path::PathBuf, String> {
    #[cfg(target_os = "macos")]
    {
        let home = std::env::var_os("HOME").ok_or("home_missing")?;
        let mut root = std::path::PathBuf::from(home);
        root.push("Library");
        root.push("Application Support");
        root.push(app_name);
        if !package_id.is_empty() {
            root.push(package_id);
        }
        Ok(root)
    }
    #[cfg(windows)]
    {
        let appdata = std::env::var_os("APPDATA")
            .ok_or_else(|| "appdata_missing".to_string())?;
        let mut root = std::path::PathBuf::from(appdata);
        root.push(app_name);
        if !package_id.is_empty() {
            root.push(package_id);
        }
        Ok(root)
    }
    #[cfg(not(any(target_os = "macos", windows)))]
    {
        let _ = (app_name, package_id);
        Err("unsupported_platform".into())
    }
}
