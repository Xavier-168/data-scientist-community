pub mod fault_injection;
pub mod install;
pub mod manifest;
pub mod platform;
pub mod runtime;
pub mod sidecar;
pub mod startup;

use std::{
    error::Error,
    fs,
    path::{Path, PathBuf},
    process::Command,
    sync::Arc,
    time::Instant,
};

use startup::{
    metrics::{StartupMetricEvent, StartupMetrics},
    model::{RetryStage, StartupSnapshot},
    orchestrator::{StartupDependencies, StartupOrchestrator, TauriStartupEventSink},
    store::StartupStore,
};
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

use install::InstallManager;
use manifest::VerifiedPackageManifest;
use runtime::{RuntimeManager, ViewManager};
use sidecar::SidecarSupervisor;

const RUNNER_LOG_NAME: &str = "runner_process.log";

#[derive(Clone)]
pub struct StateRoot(pub PathBuf);

#[tauri::command]
fn get_startup_snapshot(store: tauri::State<'_, StartupStore>) -> StartupSnapshot {
    store.snapshot()
}

#[tauri::command]
async fn retry_startup_stage(
    stage: RetryStage,
    orchestrator: tauri::State<'_, Arc<StartupOrchestrator>>,
) -> Result<(), String> {
    orchestrator.retry(stage).await
}

#[tauri::command]
fn open_startup_log(state_root: tauri::State<'_, StateRoot>) -> Result<(), String> {
    let log_path = prepare_startup_log(&state_root.0)?;
    crate::platform::reveal::reveal_path(&log_path)
}

#[tauri::command]
fn mark_react_interactive(metrics: tauri::State<'_, StartupMetrics>) -> Result<(), String> {
    metrics.record(StartupMetricEvent::ReactInteractive)
}

#[tauri::command]
async fn open_legacy_console(
    app: tauri::AppHandle,
    sidecar: tauri::State<'_, SidecarSupervisor>,
) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("legacy") {
        window.set_focus().map_err(|error| error.to_string())?;
        return Ok(());
    }
    let connection = sidecar.connection().await.ok_or("sidecar_not_ready")?;
    // 健康门通过到窗口真正导航之间仍有竞态（runner 刚死/端口未就绪），
    // WebView2 对拒绝连接只显示死胡同错误页；创建窗口前先确认端口可连。
    let mut probe_ok = false;
    for _ in 0..10 {
        match tokio::net::TcpStream::connect(("127.0.0.1", connection.port())).await {
            Ok(_) => {
                probe_ok = true;
                break;
            }
            Err(_) => tokio::time::sleep(std::time::Duration::from_millis(300)).await,
        }
    }
    if !probe_ok {
        return Err("sidecar_not_ready".into());
    }
    let url = tauri::Url::parse(&format!(
        "http://127.0.0.1:{}/monitor#session={}",
        connection.port(),
        connection.token()
    ))
    .map_err(|error| format!("legacy_url_invalid:{error}"))?;
    let mut console_builder = WebviewWindowBuilder::new(&app, "legacy", WebviewUrl::External(url))
        .title("数据科学家 · 兼容控制台");
    // Windows：控制台窗口同样启动即最大化（与主窗口一致）。
    #[cfg(windows)]
    {
        console_builder = console_builder.maximized(true);
    }
    console_builder
        .on_new_window(|url, _features| {
            if should_open_in_system_browser(&url) {
                // macOS 用 open；Windows 用 FileProtocolHandler 打开默认浏览器——
                // explorer.exe 解析不了带 ?/& 查询参数的 URL，会退化成打开文件夹。
                #[cfg(unix)]
                let _ = Command::new("/usr/bin/open").arg(url.as_str()).spawn();
                #[cfg(windows)]
                let _ = Command::new("rundll32.exe")
                    .args(["url.dll,FileProtocolHandler", url.as_str()])
                    .spawn();
            }
            tauri::webview::NewWindowResponse::Deny
        })
        .build()
        .map_err(|error| error.to_string())?;
    Ok(())
}

fn should_open_in_system_browser(url: &tauri::Url) -> bool {
    let Some(host) = url.host_str() else {
        return false;
    };
    let feishu_host = host == "feishu.cn"
        || host.ends_with(".feishu.cn")
        || host == "larkoffice.com"
        || host.ends_with(".larkoffice.com");
    let local_excel =
        matches!(host, "127.0.0.1" | "localhost" | "::1") && url.path() == "/download-excel";
    (url.scheme() == "https" && feishu_host) || (url.scheme() == "http" && local_excel)
}

#[cfg(test)]
mod external_url_tests {
    use super::should_open_in_system_browser;

    #[test]
    fn allows_feishu_and_local_excel_only() {
        for url in [
            "https://accounts.feishu.cn/oauth/v1/device/verify?user_code=TEST",
            "https://tenant.larkoffice.com/base/test",
            "http://127.0.0.1:8811/download-excel?file=all",
            "http://localhost:8811/download-excel?file=bilibili",
        ] {
            assert!(
                should_open_in_system_browser(&tauri::Url::parse(url).unwrap()),
                "{url}"
            );
        }
        for url in [
            "http://accounts.feishu.cn/oauth/v1/device/verify",
            "https://feishu.cn.evil.example/path",
            "http://127.0.0.1:8811/config",
            "https://example.com/download-excel",
        ] {
            assert!(
                !should_open_in_system_browser(&tauri::Url::parse(url).unwrap()),
                "{url}"
            );
        }
    }
}

#[cfg(unix)]
fn prepare_startup_log(app_data: &Path) -> Result<PathBuf, String> {
    prepare_startup_log_with_hook(app_data, || {})
}

#[cfg(unix)]
fn prepare_startup_log_with_hook<F>(
    app_data: &Path,
    after_downloads_open: F,
) -> Result<PathBuf, String>
where
    F: FnOnce(),
{
    let app_fd = open_real_directory_path(app_data, true)?;

    match mkdirat(&app_fd, "downloads", Mode::RWXU) {
        Ok(()) | Err(Errno::EXIST) => {}
        Err(_) => return Err(log_prepare_error()),
    }
    let downloads_fd = openat(&app_fd, "downloads", directory_open_flags(), Mode::empty())
        .map_err(|_| log_prepare_error())?;
    require_file_type(
        &fstat(&downloads_fd).map_err(|_| log_prepare_error())?,
        FileType::Directory,
    )?;

    after_downloads_open();
    let log_file = open_or_create_startup_log(&downloads_fd)?;
    validate_visible_log_identity(app_data, &app_fd, &downloads_fd, &log_file)?;

    Ok(app_data.join("downloads").join(RUNNER_LOG_NAME))
}

#[cfg(unix)]
fn ensure_real_directory(path: &Path) -> Result<(), String> {
    open_real_directory_path(path, true).map(|_| ())
}

#[cfg(unix)]
fn open_real_directory_path(path: &Path, create_missing: bool) -> Result<OwnedFd, String> {
    if path.as_os_str().is_empty() {
        return Err(log_prepare_error());
    }
    let mut current = if path.is_absolute() {
        open("/", directory_open_flags(), Mode::empty()).map_err(|_| log_prepare_error())?
    } else {
        open(".", directory_open_flags(), Mode::empty()).map_err(|_| log_prepare_error())?
    };

    for component in path.components() {
        let name = match component {
            Component::RootDir | Component::CurDir => continue,
            Component::Normal(name) => name,
            Component::ParentDir | Component::Prefix(_) => return Err(log_prepare_error()),
        };
        if create_missing {
            match mkdirat(&current, name, Mode::RWXU) {
                Ok(()) | Err(Errno::EXIST) => {}
                Err(_) => return Err(log_prepare_error()),
            }
        }
        let next = openat(&current, name, directory_open_flags(), Mode::empty())
            .map_err(|_| log_prepare_error())?;
        require_file_type(
            &fstat(&next).map_err(|_| log_prepare_error())?,
            FileType::Directory,
        )?;
        current = next;
    }
    Ok(current)
}

#[cfg(unix)]
fn open_or_create_startup_log(downloads_fd: &OwnedFd) -> Result<File, String> {
    let create_flags =
        OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC;
    let log_file = match openat(
        downloads_fd,
        RUNNER_LOG_NAME,
        create_flags,
        Mode::RUSR | Mode::WUSR,
    ) {
        Ok(log_fd) => {
            let mut file = File::from(log_fd);
            file.write_all(b"startup logging initialized\n")
                .map_err(|_| log_prepare_error())?;
            file.flush().map_err(|_| log_prepare_error())?;
            file
        }
        Err(Errno::EXIST) => File::from(
            openat(
                downloads_fd,
                RUNNER_LOG_NAME,
                existing_log_open_flags(),
                Mode::empty(),
            )
            .map_err(|_| log_prepare_error())?,
        ),
        Err(_) => return Err(log_prepare_error()),
    };
    require_file_type(
        &fstat(&log_file).map_err(|_| log_prepare_error())?,
        FileType::RegularFile,
    )?;
    Ok(log_file)
}

#[cfg(unix)]
fn validate_visible_log_identity(
    app_data: &Path,
    app_fd: &OwnedFd,
    downloads_fd: &OwnedFd,
    log_file: &File,
) -> Result<(), String> {
    let visible_app_fd = open_real_directory_path(app_data, false)?;
    require_same_identity(app_fd, &visible_app_fd)?;

    let visible_downloads_fd = openat(
        &visible_app_fd,
        "downloads",
        directory_open_flags(),
        Mode::empty(),
    )
    .map_err(|_| log_prepare_error())?;
    require_same_identity(downloads_fd, &visible_downloads_fd)?;

    let visible_log_fd = openat(
        &visible_downloads_fd,
        RUNNER_LOG_NAME,
        existing_log_open_flags(),
        Mode::empty(),
    )
    .map_err(|_| log_prepare_error())?;
    let visible_log = File::from(visible_log_fd);
    require_same_file_identity(log_file, &visible_log)
}

#[cfg(unix)]
fn require_same_identity(left: &OwnedFd, right: &OwnedFd) -> Result<(), String> {
    let left_stat = fstat(left).map_err(|_| log_prepare_error())?;
    let right_stat = fstat(right).map_err(|_| log_prepare_error())?;
    require_matching_stat(&left_stat, &right_stat)
}

#[cfg(unix)]
fn require_same_file_identity(left: &File, right: &File) -> Result<(), String> {
    let left_stat = fstat(left).map_err(|_| log_prepare_error())?;
    let right_stat = fstat(right).map_err(|_| log_prepare_error())?;
    require_matching_stat(&left_stat, &right_stat)
}

#[cfg(unix)]
fn require_matching_stat(left: &Stat, right: &Stat) -> Result<(), String> {
    if left.st_dev != right.st_dev || left.st_ino != right.st_ino {
        return Err(log_prepare_error());
    }
    Ok(())
}

#[cfg(unix)]
fn require_file_type(stat: &Stat, expected: FileType) -> Result<(), String> {
    if FileType::from_raw_mode(stat.st_mode) != expected {
        return Err(log_prepare_error());
    }
    Ok(())
}

#[cfg(unix)]
fn directory_open_flags() -> OFlags {
    OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC
}

#[cfg(unix)]
fn existing_log_open_flags() -> OFlags {
    OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC | OFlags::NONBLOCK
}

#[cfg(unix)]
fn log_prepare_error() -> String {
    "startup_log_prepare_failed".to_string()
}

#[cfg(unix)]
fn containing_app(executable: &Path) -> Option<PathBuf> {
    executable
        .ancestors()
        .find(|path| path.extension() == Some(OsStr::new("app")))
        .map(Path::to_path_buf)
}

/// Windows：启动日志准备（downloads 目录 + runner 日志文件）。
/// 不做 fd 锚定校验（无 dir_fd 语义），依赖唯一文件名与默认 ACL。
#[cfg(windows)]
fn prepare_startup_log(app_data: &Path) -> Result<PathBuf, String> {
    use std::fs::OpenOptions;
    use std::io::Write;

    let downloads = app_data.join("downloads");
    std::fs::create_dir_all(&downloads).map_err(|_| log_prepare_error())?;
    let log_path = downloads.join(RUNNER_LOG_NAME);
    let mut created = false;
    match OpenOptions::new().write(true).create_new(true).open(&log_path) {
        Ok(mut handle) => {
            created = true;
            handle
                .write_all(b"startup logging initialized\n")
                .map_err(|_| log_prepare_error())?;
        }
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(_) => return Err(log_prepare_error()),
    }
    if !created && !log_path.is_file() {
        return Err(log_prepare_error());
    }
    Ok(log_path)
}

#[cfg(windows)]
fn ensure_real_directory(path: &Path) -> Result<(), String> {
    std::fs::create_dir_all(path).map_err(|_| log_prepare_error())
}

#[cfg(windows)]
fn log_prepare_error() -> String {
    "startup_log_prepare_failed".to_string()
}

/// Windows：应用负载目录在 resources/ 子目录下（tauri 资源映射约定）。
fn payload_dir(resource_dir: &Path) -> PathBuf {
    #[cfg(windows)]
    {
        resource_dir.join("resources")
    }
    #[cfg(not(windows))]
    {
        resource_dir.to_path_buf()
    }
}

fn setup_desktop(app: &mut tauri::App, process_started: Instant) -> Result<(), Box<dyn Error>> {
    let resource_dir = app.path().resource_dir()?;
    let payload = payload_dir(&resource_dir);
    let manifest_bytes = fs::read(payload.join("package_manifest.json"))?;
    let key_bundle_bytes = fs::read(payload.join("scripts/package_public_keys.json"))?;
    let manifest = Arc::new(VerifiedPackageManifest::from_signed(
        &manifest_bytes,
        &key_bundle_bytes,
    )?);
    let faults = fault_injection::from_env();
    let state_root = match faults.state_root.clone() {
        Some(explicit) => explicit,
        None => platform::env_paths::app_state_root("数据科学家", &manifest.manifest().package_id)
            .map_err(|error| std::io::Error::other(format!("state_root_missing:{error}")))?,
    };
    ensure_real_directory(&state_root).map_err(std::io::Error::other)?;
    let source_app = source_app_path()?;

    let metrics = StartupMetrics::new(state_root.join("startup-metrics.jsonl"), process_started)
        .map_err(std::io::Error::other)?;
    metrics
        .record(StartupMetricEvent::WindowCreated)
        .map_err(std::io::Error::other)?;
    let store = StartupStore::default();
    let runtimes = RuntimeManager::new(
        state_root.clone(),
        payload.join("runtime-packs"),
        Arc::clone(&manifest),
        faults.clone(),
    )
    .map_err(std::io::Error::other)?;
    let views = ViewManager::new(state_root.clone(), payload.clone(), Arc::clone(&manifest))
        .map_err(std::io::Error::other)?;
    let sidecar = SidecarSupervisor::new(state_root.clone(), Arc::clone(&manifest))
        .map_err(std::io::Error::other)?;
    let installer = InstallManager::new(
        source_app,
        state_root.clone(),
        Arc::clone(&manifest),
        faults,
    );
    let orchestrator = Arc::new(StartupOrchestrator::new(StartupDependencies {
        events: Arc::new(TauriStartupEventSink::new(app.handle().clone())),
        store: store.clone(),
        runtimes,
        views,
        sidecar: sidecar.clone(),
        installer,
        metrics: metrics.clone(),
        manifest,
    }));

    app.manage(StateRoot(state_root));
    app.manage(store);
    app.manage(metrics);
    app.manage(sidecar);
    app.manage(Arc::clone(&orchestrator));
    // Windows：主窗口启动即最大化，免去每次手动放大。
    #[cfg(windows)]
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.maximize();
    }
    orchestrator.launch();
    Ok(())
}

/// macOS：当前 exe 所属 .app；缺失时回退 ~/Applications 默认安装位。
#[cfg(unix)]
fn source_app_path() -> Result<PathBuf, Box<dyn Error>> {
    let home = PathBuf::from(
        std::env::var_os("HOME").ok_or_else(|| std::io::Error::other("home_missing"))?,
    );
    Ok(containing_app(&std::env::current_exe()?)
        .unwrap_or_else(|| home.join("Applications/数据科学家 Community.app")))
}

/// Windows：NSIS 已安装到位，source 即 exe 所在目录。
#[cfg(windows)]
fn source_app_path() -> Result<PathBuf, Box<dyn Error>> {
    let exe = std::env::current_exe()?;
    Ok(exe
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from(".")))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let process_started = Instant::now();

    let app = tauri::Builder::default()
        // 单实例保护：重复启动时聚焦已有窗口并退出新进程，
        // 避免第二个实例抢 sidecar 锁后以降级态启动（用户会看到
        // “被其他程序控制”类错误）。
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            for label in ["legacy", "main"] {
                if let Some(window) = app.get_webview_window(label) {
                    let _ = window.unminimize();
                    let _ = window.show();
                    // Windows 前台限制下 set_focus 可能只闪任务栏不抬窗；
                    // 短暂置顶再取消可强制把窗口带到最前。
                    #[cfg(windows)]
                    {
                        let _ = window.set_always_on_top(true);
                        let _ = window.set_focus();
                        let _ = window.set_always_on_top(false);
                    }
                    #[cfg(not(windows))]
                    let _ = window.set_focus();
                }
            }
        }))
        .invoke_handler(tauri::generate_handler![
            get_startup_snapshot,
            retry_startup_stage,
            open_startup_log,
            mark_react_interactive,
            open_legacy_console,
        ])
        .setup(move |app| setup_desktop(app, process_started))
        .build(tauri::generate_context!())
        .expect("failed to build 数据科学家 desktop shell");
    app.run(|handle, event| {
        if matches!(event, RunEvent::Exit) {
            if let Some(orchestrator) = handle.try_state::<Arc<StartupOrchestrator>>() {
                if let Err(error) = tauri::async_runtime::block_on(orchestrator.shutdown()) {
                    tracing::error!(%error, "startup shutdown failed");
                }
            }
        }
    });
}

// 测试基于 symlink/权限位等 Unix 语义，Windows 下跳过
#[cfg(all(test, unix))]
mod tests {
    use std::{
        fs,
        os::unix::fs::{symlink, PermissionsExt},
    };

    use tempfile::tempdir;

    use super::{prepare_startup_log, prepare_startup_log_with_hook};

    #[test]
    fn prepares_a_real_log_without_truncating_existing_contents() {
        let root = tempdir().unwrap();
        let root_path = root.path().canonicalize().unwrap();
        let app_data = root_path.join("app-data");
        fs::create_dir(&app_data).unwrap();

        let log_path = prepare_startup_log(&app_data).unwrap();
        assert_eq!(
            log_path.file_name().unwrap(),
            "runner_process.log",
            "the command must reveal the sidecar's actual log"
        );
        assert_eq!(
            fs::metadata(&log_path).unwrap().permissions().mode() & 0o777,
            0o600,
        );
        fs::write(&log_path, b"existing diagnostic\n").unwrap();
        let second_path = prepare_startup_log(&app_data).unwrap();

        assert_eq!(second_path, log_path);
        assert_eq!(fs::read(&second_path).unwrap(), b"existing diagnostic\n");
    }

    #[test]
    fn rejects_a_downloads_parent_symlink() {
        let root = tempdir().unwrap();
        let root_path = root.path().canonicalize().unwrap();
        let app_data = root_path.join("app-data");
        let escaped = root_path.join("escaped");
        fs::create_dir(&app_data).unwrap();
        fs::create_dir(&escaped).unwrap();
        symlink(&escaped, app_data.join("downloads")).unwrap();

        let error = prepare_startup_log(&app_data).unwrap_err();

        assert_eq!(error, "startup_log_prepare_failed");
        assert!(!escaped.join("runner_process.log").exists());
    }

    #[test]
    fn rejects_a_startup_log_symlink() {
        let root = tempdir().unwrap();
        let root_path = root.path().canonicalize().unwrap();
        let app_data = root_path.join("app-data");
        let downloads = app_data.join("downloads");
        let escaped = root_path.join("escaped.log");
        fs::create_dir_all(&downloads).unwrap();
        fs::write(&escaped, b"do not touch\n").unwrap();
        symlink(&escaped, downloads.join("runner_process.log")).unwrap();

        let error = prepare_startup_log(&app_data).unwrap_err();

        assert_eq!(error, "startup_log_prepare_failed");
        assert_eq!(fs::read(&escaped).unwrap(), b"do not touch\n");
    }

    #[test]
    fn rejects_a_non_directory_path_component() {
        let root = tempdir().unwrap();
        let root_path = root.path().canonicalize().unwrap();
        let app_data = root_path.join("app-data");
        fs::create_dir(&app_data).unwrap();
        fs::write(app_data.join("downloads"), b"not a directory").unwrap();

        let error = prepare_startup_log(&app_data).unwrap_err();

        assert_eq!(error, "startup_log_prepare_failed");
    }

    #[test]
    fn creates_a_missing_real_app_data_directory() {
        let root = tempdir().unwrap();
        let root_path = root.path().canonicalize().unwrap();
        let app_data = root_path.join("missing-app-data");

        let log_path = prepare_startup_log(&app_data).unwrap();

        assert!(fs::symlink_metadata(&app_data)
            .unwrap()
            .file_type()
            .is_dir());
        assert!(log_path.is_file());
    }

    #[test]
    fn creates_a_fully_missing_nested_app_data_hierarchy() {
        let root = tempdir().unwrap();
        let canonical_root = root.path().canonicalize().unwrap();
        let app_data = canonical_root.join("new/Library/Application Support/package-id");

        let log_path = prepare_startup_log(&app_data).unwrap();

        assert_eq!(log_path, app_data.join("downloads/runner_process.log"));
        assert!(log_path.is_file());
    }

    #[test]
    fn rejects_a_symlink_in_an_app_data_ancestor() {
        let root = tempdir().unwrap();
        let canonical_root = root.path().canonicalize().unwrap();
        let escaped = canonical_root.join("escaped");
        let linked_parent = canonical_root.join("linked-parent");
        fs::create_dir(&escaped).unwrap();
        symlink(&escaped, &linked_parent).unwrap();

        let error = prepare_startup_log(&linked_parent.join("package-id")).unwrap_err();

        assert_eq!(error, "startup_log_prepare_failed");
        assert!(!escaped.join("package-id").exists());
    }

    #[test]
    fn rejects_an_app_data_symlink() {
        let root = tempdir().unwrap();
        let root_path = root.path().canonicalize().unwrap();
        let real_app_data = root_path.join("real-app-data");
        let app_data_link = root_path.join("app-data-link");
        fs::create_dir(&real_app_data).unwrap();
        symlink(&real_app_data, &app_data_link).unwrap();

        let error = prepare_startup_log(&app_data_link).unwrap_err();

        assert_eq!(error, "startup_log_prepare_failed");
    }

    #[test]
    fn rejects_a_non_directory_app_data_path() {
        let root = tempdir().unwrap();
        let root_path = root.path().canonicalize().unwrap();
        let app_data = root_path.join("app-data-file");
        fs::write(&app_data, b"not a directory").unwrap();

        let error = prepare_startup_log(&app_data).unwrap_err();

        assert_eq!(error, "startup_log_prepare_failed");
    }

    #[test]
    fn anchored_creation_does_not_follow_a_replaced_downloads_parent() {
        let root = tempdir().unwrap();
        let root_path = root.path().canonicalize().unwrap();
        let app_data = root_path.join("app-data");
        let downloads = app_data.join("downloads");
        let moved_downloads = app_data.join("downloads-anchored");
        let escaped = root_path.join("escaped");
        fs::create_dir_all(&downloads).unwrap();
        fs::create_dir(&escaped).unwrap();

        let error = prepare_startup_log_with_hook(&app_data, || {
            fs::rename(&downloads, &moved_downloads).unwrap();
            symlink(&escaped, &downloads).unwrap();
        })
        .unwrap_err();

        assert_eq!(error, "startup_log_prepare_failed");
        assert!(moved_downloads.join("runner_process.log").is_file());
        assert!(!escaped.join("runner_process.log").exists());
    }
}
