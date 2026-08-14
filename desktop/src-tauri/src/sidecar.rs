use std::{
    collections::HashMap,
    ffi::OsStr,
    fs::File,
    io,
    path::{Path, PathBuf},
    process::Stdio,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};
#[cfg(unix)]
use std::io::{Read, Write};
#[cfg(unix)]
use std::os::unix::process::CommandExt;

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use fs2::FileExt;
use rand::RngCore;
use serde::{Deserialize, Serialize};
use sysinfo::{Pid, ProcessesToUpdate, System};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    process::{Child, Command},
    sync::{oneshot, Mutex},
    task::JoinHandle,
    time::{sleep, timeout},
};

use crate::{manifest::VerifiedPackageManifest, runtime::VerifiedView};

const IDENTITY_NAME: &str = "sidecar.json";
const LOCK_NAME: &str = ".sidecar.lock";
const LOG_NAME: &str = "runner_process.log";
const IDENTITY_MAX_BYTES: usize = 64 * 1024;
const LOCK_POLL: Duration = Duration::from_millis(25);
const READY_TIMEOUT: Duration = Duration::from_secs(10);
const HEALTH_ATTEMPTS: usize = 50;
const HEALTH_POLL: Duration = Duration::from_millis(100);
const TERMINATE_GRACE: Duration = Duration::from_millis(750);
const READY_PREFIX: &[u8] = b"YRG_SIDECAR_READY ";
const OWNER_MARKER_ENV: &str = "YIRENGONGIS_PROCESS_OWNER_ID";
const STDERR_LINE_MAX_BYTES: usize = 64 * 1024;
pub(crate) const READY_FRAME_MAX_BYTES: usize = 4096;
type WatcherTask = JoinHandle<Result<(), String>>;
#[cfg(test)]
type TestHook = Box<dyn FnOnce() + Send>;

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
enum SidecarPhase {
    Launching,
    Spawned,
    Ready,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct SidecarIdentity {
    schema_version: u8,
    phase: SidecarPhase,
    pid: u32,
    pgid: u32,
    port: u16,
    python: PathBuf,
    script: PathBuf,
    package_id: String,
    build_version: String,
    started_at_secs: u64,
    launched_at_secs: u64,
    owner_uid: u32,
    instance_id: String,
}

impl SidecarIdentity {
    #[cfg(test)]
    fn fixture_for_test(
        pid: u32,
        python: &str,
        script: &str,
        instance_id: &str,
        package_id: &str,
        build_version: &str,
    ) -> Self {
        Self {
            schema_version: 1,
            phase: SidecarPhase::Ready,
            pid,
            pgid: pid,
            port: 8811,
            python: python.into(),
            script: script.into(),
            package_id: package_id.into(),
            build_version: build_version.into(),
            started_at_secs: 1,
            launched_at_secs: 1,
            owner_uid: current_process_uid().unwrap(),
            instance_id: instance_id.into(),
        }
    }

    fn validate(&self) -> Result<(), String> {
        let phase_valid = match self.phase {
            SidecarPhase::Launching => {
                self.pid == 0 && self.pgid == 0 && self.port == 0 && self.started_at_secs == 0
            }
            SidecarPhase::Spawned => {
                self.pid != 0
                    && self.pgid == self.pid
                    && self.port == 0
                    && self.started_at_secs >= self.launched_at_secs
            }
            SidecarPhase::Ready => {
                self.pid != 0
                    && self.pgid == self.pid
                    && self.port != 0
                    && self.started_at_secs >= self.launched_at_secs
            }
        };
        if self.schema_version != 1
            || !phase_valid
            || self.launched_at_secs == 0
            || self.package_id.is_empty()
            || self.build_version.is_empty()
            || self.instance_id.len() < 16
            || self.instance_id.len() > 128
            || self.python.as_os_str().is_empty()
            || self.script.as_os_str().is_empty()
            || !self.python.is_absolute()
            || !self.script.is_absolute()
        {
            return Err("sidecar_identity_invalid".into());
        }
        Ok(())
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ReadyFrame {
    event: String,
    port: u16,
    package_id: String,
    build_version: String,
}

fn parse_ready_frame(
    line: &[u8],
    package_id: &str,
    build_version: &str,
) -> Result<Option<ReadyFrame>, String> {
    let line = line.strip_suffix(b"\r").unwrap_or(line);
    if line.len() > READY_FRAME_MAX_BYTES {
        return Err("sidecar_ready_frame_too_large".into());
    }
    let Some(payload) = line.strip_prefix(READY_PREFIX) else {
        return Ok(None);
    };
    let frame: ReadyFrame =
        serde_json::from_slice(payload).map_err(|_| "sidecar_ready_frame_invalid".to_string())?;
    if frame.event != "ready" || frame.port == 0 {
        return Err("sidecar_ready_frame_invalid".into());
    }
    if frame.package_id != package_id || frame.build_version != build_version {
        return Err("sidecar_ready_identity_mismatch".into());
    }
    Ok(Some(frame))
}

#[derive(Clone)]
pub struct SidecarConnection {
    port: u16,
    token: String,
}

impl SidecarConnection {
    pub fn port(&self) -> u16 {
        self.port
    }

    pub fn token(&self) -> &str {
        &self.token
    }
}

struct SidecarHandle {
    port: u16,
    session_token: String,
    child: Child,
    stdout_task: JoinHandle<Result<(), String>>,
    stderr_task: JoinHandle<Result<(), String>>,
    pid: u32,
    started_at_secs: Option<u64>,
    identity: Option<SidecarIdentity>,
    /// Windows：持有 Job Object 句柄，drop 时整树终止
    #[cfg(windows)]
    job: Option<crate::platform::process::JobHandle>,
}

#[derive(Clone)]
pub struct SidecarSupervisor {
    state_root: PathBuf,
    manifest: Arc<VerifiedPackageManifest>,
    preferred_port: u16,
    client: reqwest::Client,
    state: Arc<DirHandle>,
    runtimes: Arc<DirHandle>,
    downloads: Arc<DirHandle>,
    lock: Arc<File>,
    lock_held: Arc<AtomicBool>,
    running: Arc<AtomicBool>,
    transition: Arc<Mutex<()>>,
    active: Arc<Mutex<Option<SidecarHandle>>>,
    watcher: Arc<Mutex<Option<WatcherTask>>>,
    #[cfg(test)]
    before_spawn: Arc<std::sync::Mutex<Option<TestHook>>>,
    #[cfg(test)]
    after_spawn_identity: Arc<std::sync::Mutex<Option<TestHook>>>,
    #[cfg(test)]
    watcher_before_cleanup: Arc<std::sync::Mutex<Option<TestHook>>>,
}

#[cfg(unix)]
fn same_identity(left: &rustix::fs::Stat, right: &rustix::fs::Stat) -> bool {
    left.st_dev == right.st_dev && left.st_ino == right.st_ino
}

/// 目录句柄：Unix 持有 fd（锚定防替换）；Windows 为校验过的路径。
#[cfg(unix)]
type DirHandle = File;
#[cfg(windows)]
type DirHandle = PathBuf;

#[cfg(unix)]
fn open_directory(path: &Path) -> Result<File, String> {
    use rustix::fs::{fstat, open, FileType, Mode, OFlags};

    let fd = open(
        path,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|error| io::Error::from(error).to_string())?;
    let stat = fstat(&fd).map_err(|error| io::Error::from(error).to_string())?;
    if FileType::from_raw_mode(stat.st_mode) != FileType::Directory {
        return Err("sidecar_directory_invalid".into());
    }
    Ok(File::from(fd))
}

#[cfg(windows)]
fn open_directory(path: &Path) -> Result<PathBuf, String> {
    let metadata = std::fs::symlink_metadata(path).map_err(|error| error.to_string())?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err("sidecar_directory_invalid".into());
    }
    Ok(path.to_path_buf())
}

#[cfg(unix)]
fn open_child_directory(parent: &File, name: &str) -> Result<File, String> {
    use rustix::fs::{fstat, openat, FileType, Mode, OFlags};

    let fd = openat(
        parent,
        name,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|error| io::Error::from(error).to_string())?;
    let stat = fstat(&fd).map_err(|error| io::Error::from(error).to_string())?;
    if FileType::from_raw_mode(stat.st_mode) != FileType::Directory {
        return Err("sidecar_directory_invalid".into());
    }
    Ok(File::from(fd))
}

#[cfg(windows)]
fn open_child_directory(parent: &Path, name: &str) -> Result<PathBuf, String> {
    open_directory(&parent.join(name)).map_err(|_| "sidecar_directory_invalid".into())
}

#[cfg(unix)]
fn ensure_child_directory(parent: &File, name: &str) -> Result<File, String> {
    use rustix::fs::{fchmod, mkdirat, Mode};

    match mkdirat(parent, name, Mode::from_raw_mode(0o700)) {
        Ok(()) | Err(rustix::io::Errno::EXIST) => {}
        Err(error) => return Err(io::Error::from(error).to_string()),
    }
    let directory = open_child_directory(parent, name)?;
    fchmod(&directory, Mode::from_raw_mode(0o700))
        .map_err(|error| io::Error::from(error).to_string())?;
    Ok(directory)
}

#[cfg(windows)]
fn ensure_child_directory(parent: &Path, name: &str) -> Result<PathBuf, String> {
    let child = parent.join(name);
    std::fs::create_dir_all(&child).map_err(|error| error.to_string())?;
    open_directory(&child)
}

/// 目录条目存在性探测（Windows 无 inode 身份，仅存在性/类型）。
#[cfg(unix)]
fn stat_optional(parent: &File, name: &str) -> Result<Option<rustix::fs::Stat>, String> {
    use rustix::fs::{statat, AtFlags};

    match statat(parent, name, AtFlags::SYMLINK_NOFOLLOW) {
        Ok(stat) => Ok(Some(stat)),
        Err(rustix::io::Errno::NOENT) => Ok(None),
        Err(error) => Err(io::Error::from(error).to_string()),
    }
}

#[cfg(windows)]
fn stat_optional(parent: &Path, name: &str) -> Result<Option<()>, String> {
    match std::fs::symlink_metadata(parent.join(name)) {
        Ok(_) => Ok(Some(())),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.to_string()),
    }
}

impl SidecarSupervisor {
    pub fn new(
        state_root: PathBuf,
        manifest: Arc<VerifiedPackageManifest>,
    ) -> Result<Self, String> {
        Self::new_with_preferred_port(state_root, manifest, 8811)
    }

    pub fn new_with_preferred_port(
        state_root: PathBuf,
        manifest: Arc<VerifiedPackageManifest>,
        preferred_port: u16,
    ) -> Result<Self, String> {
        let metadata = std::fs::symlink_metadata(&state_root).map_err(|error| error.to_string())?;
        if !metadata.is_dir() || metadata.file_type().is_symlink() {
            return Err("sidecar_state_root_invalid".into());
        }
        let state_root = std::fs::canonicalize(state_root).map_err(|error| error.to_string())?;
        let state = open_directory(&state_root)?;
        let runtimes = ensure_child_directory(&state, "runtimes")?;
        let downloads = ensure_child_directory(&state, "downloads")?;
        #[cfg(unix)]
        let (runtimes, downloads, lock) = {
            use rustix::fs::{fchmod, fstat, fsync, openat, FileType, Mode, OFlags};
            let lock_fd = openat(
                &runtimes,
                LOCK_NAME,
                OFlags::RDWR | OFlags::CREATE | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
                Mode::from_raw_mode(0o600),
            )
            .map_err(|error| io::Error::from(error).to_string())?;
            let lock = File::from(lock_fd);
            let lock_stat = fstat(&lock).map_err(|error| io::Error::from(error).to_string())?;
            if FileType::from_raw_mode(lock_stat.st_mode) != FileType::RegularFile
                || lock_stat.st_nlink != 1
            {
                return Err("sidecar_lock_invalid".into());
            }
            fchmod(&lock, Mode::from_raw_mode(0o600))
                .map_err(|error| io::Error::from(error).to_string())?;
            fsync(&runtimes).map_err(|error| io::Error::from(error).to_string())?;
            fsync(&downloads).map_err(|error| io::Error::from(error).to_string())?;
            (runtimes, downloads, lock)
        };
        #[cfg(windows)]
        let lock = {
            use std::fs::OpenOptions;
            // fs2 文件锁在 Windows 上同样可用，锁文件按路径打开
            OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .open(runtimes.join(LOCK_NAME))
                .map_err(|error| error.to_string())?
        };
        Ok(Self {
            state_root,
            manifest,
            preferred_port,
            client: reqwest::Client::builder()
                .no_proxy()
                .redirect(reqwest::redirect::Policy::none())
                .build()
                .map_err(|_| "sidecar_client_init_failed".to_string())?,
            state: Arc::new(state),
            runtimes: Arc::new(runtimes),
            downloads: Arc::new(downloads),
            lock: Arc::new(lock),
            lock_held: Arc::new(AtomicBool::new(false)),
            running: Arc::new(AtomicBool::new(false)),
            transition: Arc::new(Mutex::new(())),
            active: Arc::new(Mutex::new(None)),
            watcher: Arc::new(Mutex::new(None)),
            #[cfg(test)]
            before_spawn: Arc::new(std::sync::Mutex::new(None)),
            #[cfg(test)]
            after_spawn_identity: Arc::new(std::sync::Mutex::new(None)),
            #[cfg(test)]
            watcher_before_cleanup: Arc::new(std::sync::Mutex::new(None)),
        })
    }

    #[cfg(test)]
    fn identity_path(&self) -> PathBuf {
        self.state_root.join("runtimes").join(IDENTITY_NAME)
    }

    #[cfg(unix)]
    fn verify_visible(&self) -> Result<(), String> {
        use rustix::fs::{fstat, FileType};

        let state = open_directory(&self.state_root)
            .map_err(|_| "sidecar_state_root_changed".to_string())?;
        let held_state = fstat(&*self.state).map_err(|error| io::Error::from(error).to_string())?;
        let visible_state = fstat(&state).map_err(|error| io::Error::from(error).to_string())?;
        if !same_identity(&held_state, &visible_state) {
            return Err("sidecar_state_root_changed".into());
        }
        let runtimes = open_child_directory(&state, "runtimes")
            .map_err(|_| "sidecar_state_root_changed".to_string())?;
        let downloads = open_child_directory(&state, "downloads")
            .map_err(|_| "sidecar_state_root_changed".to_string())?;
        for (held, visible) in [(&*self.runtimes, &runtimes), (&*self.downloads, &downloads)] {
            let held = fstat(held).map_err(|error| io::Error::from(error).to_string())?;
            let visible = fstat(visible).map_err(|error| io::Error::from(error).to_string())?;
            if !same_identity(&held, &visible) {
                return Err("sidecar_state_root_changed".into());
            }
        }
        let visible_lock = stat_optional(&runtimes, LOCK_NAME)?
            .ok_or_else(|| "sidecar_lock_changed".to_string())?;
        let held_lock = fstat(&*self.lock).map_err(|error| io::Error::from(error).to_string())?;
        if FileType::from_raw_mode(visible_lock.st_mode) != FileType::RegularFile
            || visible_lock.st_mode & 0o777 != 0o600
            || visible_lock.st_nlink != 1
            || !same_identity(&visible_lock, &held_lock)
        {
            return Err("sidecar_lock_changed".into());
        }
        Ok(())
    }

    /// Windows：无 inode 锚定语义，重解析路径确认目录仍指向一致位置。
    #[cfg(windows)]
    fn verify_visible(&self) -> Result<(), String> {
        let state = open_directory(&self.state_root)
            .map_err(|_| "sidecar_state_root_changed".to_string())?;
        open_child_directory(&state, "runtimes").map_err(|_| "sidecar_state_root_changed".to_string())?;
        open_child_directory(&state, "downloads").map_err(|_| "sidecar_state_root_changed".to_string())?;
        if stat_optional(&self.runtimes, LOCK_NAME)?.is_none() {
            return Err("sidecar_lock_changed".into());
        }
        Ok(())
    }

    async fn acquire_process_lock(&self) -> Result<(), String> {
        if self.lock_held.load(Ordering::Acquire) {
            return Ok(());
        }
        loop {
            self.verify_visible()?;
            match self.lock.try_lock_exclusive() {
                Ok(()) => {
                    self.lock_held.store(true, Ordering::Release);
                    return Ok(());
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => sleep(LOCK_POLL).await,
                Err(error) => return Err(error.to_string()),
            }
        }
    }

    fn release_process_lock(&self) -> Result<(), String> {
        if self.lock_held.swap(false, Ordering::AcqRel) {
            FileExt::unlock(&*self.lock).map_err(|error| error.to_string())?;
        }
        Ok(())
    }

    async fn stop_watcher(&self) -> Result<(), String> {
        let Some(task) = self.watcher.lock().await.take() else {
            return Ok(());
        };
        if !task.is_finished() {
            task.abort();
        }
        match task.await {
            Ok(result) => result,
            Err(error) if error.is_cancelled() => Ok(()),
            Err(error) => Err(format!("sidecar_watcher_failed:{error}")),
        }
    }

    async fn start_watcher(&self) {
        let supervisor = self.clone();
        let task = tokio::spawn(async move {
            loop {
                sleep(Duration::from_millis(50)).await;
                let (pid, started_at, exited) = {
                    let mut active = supervisor.active.lock().await;
                    match active.as_mut() {
                        Some(handle) => match handle.child.try_wait() {
                            Ok(Some(_)) | Err(_) => (handle.pid, handle.started_at_secs, true),
                            Ok(None) => (handle.pid, handle.started_at_secs, false),
                        },
                        None => return Ok(()),
                    }
                };
                if !exited {
                    continue;
                }
                supervisor.running.store(false, Ordering::Release);
                supervisor.run_watcher_before_cleanup_hook();
                let identity = supervisor
                    .active
                    .lock()
                    .await
                    .as_ref()
                    .filter(|handle| handle.pid == pid)
                    .and_then(|handle| handle.identity.clone());
                return match identity {
                    Some(identity) => terminate_owner_processes(&identity).await,
                    None if started_at.is_none() => Ok(()),
                    None => Err("sidecar_watcher_identity_missing".into()),
                };
            }
        });
        *self.watcher.lock().await = Some(task);
    }

    #[cfg(test)]
    fn set_before_spawn_hook<F>(&self, hook: F)
    where
        F: FnOnce() + Send + 'static,
    {
        *self.before_spawn.lock().unwrap() = Some(Box::new(hook));
    }

    #[cfg(test)]
    fn run_before_spawn_hook(&self) {
        if let Some(hook) = self.before_spawn.lock().unwrap().take() {
            hook();
        }
    }

    #[cfg(not(test))]
    fn run_before_spawn_hook(&self) {}

    #[cfg(test)]
    fn set_after_spawn_identity_hook<F>(&self, hook: F)
    where
        F: FnOnce() + Send + 'static,
    {
        *self.after_spawn_identity.lock().unwrap() = Some(Box::new(hook));
    }

    #[cfg(test)]
    fn run_after_spawn_identity_hook(&self) {
        if let Some(hook) = self.after_spawn_identity.lock().unwrap().take() {
            hook();
        }
    }

    #[cfg(not(test))]
    fn run_after_spawn_identity_hook(&self) {}

    #[cfg(test)]
    fn set_watcher_before_cleanup_hook<F>(&self, hook: F)
    where
        F: FnOnce() + Send + 'static,
    {
        *self.watcher_before_cleanup.lock().unwrap() = Some(Box::new(hook));
    }

    #[cfg(test)]
    fn run_watcher_before_cleanup_hook(&self) {
        if let Some(hook) = self.watcher_before_cleanup.lock().unwrap().take() {
            hook();
        }
    }

    #[cfg(not(test))]
    fn run_watcher_before_cleanup_hook(&self) {}

    #[cfg(unix)]
    fn open_runner_log(&self) -> Result<File, String> {
        use rustix::fs::{fchmod, fstat, openat, FileType, Mode, OFlags};

        self.verify_visible()?;
        let fd = openat(
            &*self.downloads,
            LOG_NAME,
            OFlags::WRONLY
                | OFlags::APPEND
                | OFlags::CREATE
                | OFlags::NOFOLLOW
                | OFlags::NONBLOCK
                | OFlags::CLOEXEC,
            Mode::from_raw_mode(0o600),
        )
        .map_err(|error| io::Error::from(error).to_string())?;
        let file = File::from(fd);
        let initial = fstat(&file).map_err(|error| io::Error::from(error).to_string())?;
        let visible = stat_optional(&self.downloads, LOG_NAME)?
            .ok_or_else(|| "sidecar_log_changed".to_string())?;
        if FileType::from_raw_mode(initial.st_mode) != FileType::RegularFile
            || initial.st_nlink != 1
            || !same_identity(&initial, &visible)
        {
            return Err("sidecar_log_invalid".into());
        }
        fchmod(&file, Mode::from_raw_mode(0o600))
            .map_err(|error| io::Error::from(error).to_string())?;
        let held = fstat(&file).map_err(|error| io::Error::from(error).to_string())?;
        if held.st_mode & 0o777 != 0o600 || !same_identity(&held, &initial) {
            return Err("sidecar_log_invalid".into());
        }
        Ok(file)
    }

    /// Windows：追加写打开日志（无 inode 锚定，依赖默认 ACL）。
    #[cfg(windows)]
    fn open_runner_log(&self) -> Result<File, String> {
        use std::fs::OpenOptions;

        self.verify_visible()?;
        OpenOptions::new()
            .append(true)
            .create(true)
            .open(self.downloads.join(LOG_NAME))
            .map_err(|error| error.to_string())
    }

    #[cfg(unix)]
    fn read_identity(&self) -> Result<Option<SidecarIdentity>, String> {
        use rustix::fs::{fstat, openat, FileType, Mode, OFlags};

        let fd = match openat(
            &*self.runtimes,
            IDENTITY_NAME,
            OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
            Mode::empty(),
        ) {
            Ok(fd) => fd,
            Err(rustix::io::Errno::NOENT) => return Ok(None),
            Err(error) => return Err(io::Error::from(error).to_string()),
        };
        let mut file = File::from(fd);
        let stat = fstat(&file).map_err(|error| io::Error::from(error).to_string())?;
        if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile
            || stat.st_mode & 0o777 != 0o600
            || stat.st_nlink != 1
            || stat.st_size < 0
            || stat.st_size as usize > IDENTITY_MAX_BYTES
        {
            return Err("sidecar_identity_invalid".into());
        }
        let mut bytes = Vec::with_capacity(stat.st_size as usize);
        Read::by_ref(&mut file)
            .take((IDENTITY_MAX_BYTES + 1) as u64)
            .read_to_end(&mut bytes)
            .map_err(|error| error.to_string())?;
        if bytes.len() > IDENTITY_MAX_BYTES {
            return Err("sidecar_identity_invalid".into());
        }
        let identity: SidecarIdentity =
            serde_json::from_slice(&bytes).map_err(|_| "sidecar_identity_malformed".to_string())?;
        identity.validate()?;
        Ok(Some(identity))
    }

    #[cfg(windows)]
    fn read_identity(&self) -> Result<Option<SidecarIdentity>, String> {
        let path = self.runtimes.join(IDENTITY_NAME);
        let bytes = match std::fs::read(&path) {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.to_string()),
        };
        if bytes.len() > IDENTITY_MAX_BYTES {
            return Err("sidecar_identity_invalid".into());
        }
        let identity: SidecarIdentity =
            serde_json::from_slice(&bytes).map_err(|_| "sidecar_identity_malformed".to_string())?;
        identity.validate()?;
        Ok(Some(identity))
    }

    #[cfg(unix)]
    fn unlink_identity_raw(&self) -> Result<(), String> {
        use rustix::fs::{fsync, unlinkat, AtFlags};

        match unlinkat(&*self.runtimes, IDENTITY_NAME, AtFlags::empty()) {
            Ok(()) | Err(rustix::io::Errno::NOENT) => {}
            Err(error) => return Err(io::Error::from(error).to_string()),
        }
        fsync(&*self.runtimes).map_err(|error| io::Error::from(error).to_string())
    }

    #[cfg(windows)]
    fn unlink_identity_raw(&self) -> Result<(), String> {
        match std::fs::remove_file(self.runtimes.join(IDENTITY_NAME)) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error.to_string()),
        }
    }

    fn unlink_identity_if_matches(&self, expected: &SidecarIdentity) -> Result<bool, String> {
        if stat_optional(&self.runtimes, IDENTITY_NAME)?.is_none() {
            return Ok(false);
        }
        if self.read_identity()?.as_ref() != Some(expected) {
            return Ok(false);
        }
        self.unlink_identity_raw()?;
        Ok(true)
    }

    fn unlink_identity_if_same_owner(&self, expected: &SidecarIdentity) -> Result<bool, String> {
        if stat_optional(&self.runtimes, IDENTITY_NAME)?.is_none() {
            return Ok(false);
        }
        let Some(observed) = self.read_identity()? else {
            return Ok(false);
        };
        if !same_sidecar_owner(&observed, expected) {
            return Ok(false);
        }
        self.unlink_identity_raw()?;
        Ok(true)
    }

    /// Unix 专属：按 inode 身份删除身份文件。
    #[cfg(unix)]
    fn unlink_identity_inode_if_matches(
        &self,
        expected: &rustix::fs::Stat,
    ) -> Result<bool, String> {
        let current = match stat_optional(&self.runtimes, IDENTITY_NAME)? {
            Some(stat) => stat,
            None => return Ok(false),
        };
        if !same_identity(expected, &current) {
            return Ok(false);
        }
        self.unlink_identity_raw()?;
        Ok(true)
    }

    #[cfg(unix)]
    fn write_identity(&self, identity: &SidecarIdentity) -> Result<(), String> {
        use rustix::fs::{fchmod, fsync, openat, renameat, unlinkat, AtFlags, Mode, OFlags};

        self.verify_visible()?;
        identity.validate()?;
        let bytes = serde_json::to_vec(identity).map_err(|error| error.to_string())?;
        if bytes.len() > IDENTITY_MAX_BYTES {
            return Err("sidecar_identity_invalid".into());
        }
        let temporary = format!("{IDENTITY_NAME}.tmp-{}", uuid::Uuid::new_v4());
        let result = (|| {
            let fd = openat(
                &*self.runtimes,
                temporary.as_str(),
                OFlags::WRONLY
                    | OFlags::CREATE
                    | OFlags::EXCL
                    | OFlags::NOFOLLOW
                    | OFlags::NONBLOCK
                    | OFlags::CLOEXEC,
                Mode::from_raw_mode(0o600),
            )
            .map_err(|error| io::Error::from(error).to_string())?;
            let mut file = File::from(fd);
            fchmod(&file, Mode::from_raw_mode(0o600))
                .map_err(|error| io::Error::from(error).to_string())?;
            file.write_all(&bytes).map_err(|error| error.to_string())?;
            file.sync_all().map_err(|error| error.to_string())?;
            renameat(
                &*self.runtimes,
                temporary.as_str(),
                &*self.runtimes,
                IDENTITY_NAME,
            )
            .map_err(|error| io::Error::from(error).to_string())?;
            fsync(&*self.runtimes).map_err(|error| io::Error::from(error).to_string())
        })();
        if result.is_err() {
            let _ = unlinkat(&*self.runtimes, temporary.as_str(), AtFlags::empty());
            let _ = fsync(&*self.runtimes);
        }
        result
    }

    /// Windows：唯一临时名 + rename 的原子写。
    #[cfg(windows)]
    fn write_identity(&self, identity: &SidecarIdentity) -> Result<(), String> {
        self.verify_visible()?;
        identity.validate()?;
        let bytes = serde_json::to_vec(identity).map_err(|error| error.to_string())?;
        if bytes.len() > IDENTITY_MAX_BYTES {
            return Err("sidecar_identity_invalid".into());
        }
        let temporary = self
            .runtimes
            .join(format!("{IDENTITY_NAME}.tmp-{}", uuid::Uuid::new_v4()));
        std::fs::write(&temporary, &bytes).map_err(|error| error.to_string())?;
        match std::fs::rename(&temporary, self.runtimes.join(IDENTITY_NAME)) {
            Ok(()) => Ok(()),
            Err(error) => {
                let _ = std::fs::remove_file(&temporary);
                Err(error.to_string())
            }
        }
    }

    fn write_identity_if_absent(&self, identity: &SidecarIdentity) -> Result<(), String> {
        if stat_optional(&self.runtimes, IDENTITY_NAME)?.is_some() {
            return Err("sidecar_identity_exists".into());
        }
        self.write_identity(identity)
    }

    fn replace_identity_if_matches(
        &self,
        expected: &SidecarIdentity,
        replacement: &SidecarIdentity,
    ) -> Result<(), String> {
        if !same_sidecar_owner(expected, replacement) {
            return Err("sidecar_identity_owner_changed".into());
        }
        if self.read_identity()?.as_ref() != Some(expected) {
            return Err("sidecar_identity_owner_changed".into());
        }
        self.write_identity(replacement)
    }

    fn python_relative(&self) -> Result<PathBuf, String> {
        unique_required_path(
            &self.manifest.manifest().runtimes.core.required_files,
            crate::platform::runtime_python_prefix(),
            crate::platform::python_entry_suffix(),
            "sidecar_python_required_file_invalid",
        )
    }

    fn node_relative(&self) -> Result<PathBuf, String> {
        unique_required_path(
            &self.manifest.manifest().runtimes.collector.required_files,
            crate::platform::runtime_node_prefix(),
            crate::platform::node_entry_suffix(),
            "sidecar_node_required_file_invalid",
        )
    }

    async fn process_start_time(pid: u32) -> Result<u64, String> {
        let pid = Pid::from_u32(pid);
        let mut system = System::new();
        for _ in 0..40 {
            system.refresh_processes(ProcessesToUpdate::Some(&[pid]), true);
            if let Some(process) = system.process(pid) {
                return Ok(process.start_time());
            }
            sleep(Duration::from_millis(25)).await;
        }
        Err("sidecar_process_identity_missing".into())
    }

    async fn cleanup_owned_process(&self) -> Result<(), String> {
        if !self.lock_held.load(Ordering::Acquire) {
            return Err("sidecar_lock_not_held".into());
        }
        self.verify_visible()?;
        let identity = match self.read_identity() {
            Ok(Some(identity)) => identity,
            Ok(None) => return Ok(()),
            #[cfg(unix)]
            Err(error) if error == "sidecar_identity_malformed" => {
                // Unix：身份文件损坏时按 inode 身份定向清除
                if let Some(entry) = stat_optional(&self.runtimes, IDENTITY_NAME)? {
                    self.unlink_identity_inode_if_matches(&entry)?;
                }
                return Ok(());
            }
            Err(error) => return Err(error),
        };
        if identity.package_id != self.manifest.manifest().package_id {
            self.unlink_identity_if_matches(&identity)?;
            return Ok(());
        }
        terminate_owner_processes(&identity).await?;
        self.unlink_identity_if_matches(&identity)?;
        Ok(())
    }

    async fn cleanup_active_locked(&self) -> Result<(), String> {
        let mut failures = Vec::new();
        let mut expected_identity = None;
        if let Some(mut handle) = self.active.lock().await.take() {
            expected_identity = handle.identity.clone();
            self.running.store(false, Ordering::Release);
            if let Some(identity) = handle.identity.as_ref() {
                if let Err(error) = terminate_owner_processes(identity).await {
                    failures.push(error);
                }
            }
            match timeout(Duration::from_secs(2), handle.child.wait()).await {
                Ok(Ok(_)) => {}
                Ok(Err(error)) => failures.push(format!("sidecar_wait_failed:{error}")),
                Err(_) => {
                    let _ = handle.child.start_kill();
                    let _ = handle.child.wait().await;
                    failures.push("sidecar_wait_timeout".into());
                }
            }
            match timeout(Duration::from_secs(2), &mut handle.stdout_task).await {
                Ok(Ok(Ok(()))) => {}
                Ok(Ok(Err(error))) => failures.push(error),
                Ok(Err(error)) => failures.push(format!("sidecar_stdout_task_failed:{error}")),
                Err(_) => {
                    handle.stdout_task.abort();
                    let _ = handle.stdout_task.await;
                    failures.push("sidecar_stdout_task_timeout".into());
                }
            }
            match timeout(Duration::from_secs(2), &mut handle.stderr_task).await {
                Ok(Ok(Ok(()))) => {}
                Ok(Ok(Err(error))) => failures.push(error),
                Ok(Err(error)) => failures.push(format!("sidecar_stderr_task_failed:{error}")),
                Err(_) => {
                    handle.stderr_task.abort();
                    let _ = handle.stderr_task.await;
                    failures.push("sidecar_stderr_task_timeout".into());
                }
            }
        }
        if self.lock_held.load(Ordering::Acquire) {
            if let Some(identity) = expected_identity {
                if let Err(error) = self.unlink_identity_if_same_owner(&identity) {
                    failures.push(format!("sidecar_identity_cleanup_failed:{error}"));
                }
            }
        } else if expected_identity.is_some() {
            failures.push("sidecar_identity_cleanup_without_lock".into());
        }
        if failures.is_empty() {
            Ok(())
        } else {
            Err(failures.join("; "))
        }
    }

    async fn fail_start(&self, primary: String) -> Result<u16, String> {
        let cleanup = self.cleanup_active_locked().await;
        let unlock = self.release_process_lock();
        match (cleanup, unlock) {
            (Ok(()), Ok(())) => Err(primary),
            (cleanup, unlock) => Err(format!(
                "{primary}; sidecar_cleanup_failed:{}{}",
                cleanup.err().unwrap_or_default(),
                unlock
                    .err()
                    .map(|error| format!("; {error}"))
                    .unwrap_or_default()
            )),
        }
    }

    async fn fail_launch(&self, primary: String, intent: &SidecarIdentity) -> Result<u16, String> {
        let processes = terminate_owner_processes(intent).await;
        let identity = self.unlink_identity_if_same_owner(intent);
        let unlock = self.release_process_lock();
        match (processes, identity, unlock) {
            (Ok(()), Ok(_), Ok(())) => Err(primary),
            (processes, identity, unlock) => Err(format!(
                "{primary}; sidecar_launch_cleanup_failed:{}{}{}",
                processes.err().unwrap_or_default(),
                identity
                    .err()
                    .map(|error| format!("; {error}"))
                    .unwrap_or_default(),
                unlock
                    .err()
                    .map(|error| format!("; {error}"))
                    .unwrap_or_default()
            )),
        }
    }

    pub async fn start(&self, view: &VerifiedView) -> Result<u16, String> {
        let _transition = self.transition.lock().await;
        view.verify_visible()?;
        let pinned_root = view.pinned_launch_root()?;
        let package = self.manifest.manifest();
        let current_root = view.path().to_path_buf();
        let python = pinned_root.join(self.python_relative()?);
        let script = pinned_root.join("scripts/_run.py");
        if !package
            .runtimes
            .core
            .required_files
            .iter()
            .any(|path| path == "scripts/_run.py")
        {
            return Err("sidecar_script_required_file_invalid".into());
        }
        let node = current_root.join(self.node_relative()?);
        let node_parent = node
            .parent()
            .ok_or_else(|| "sidecar_node_path_invalid".to_string())?
            .to_path_buf();
        let instance_id = random_secret();
        let session_token = random_secret();
        let arguments = sidecar_arguments(
            &script,
            &instance_id,
            &package.package_id,
            &package.build_version,
        )?;
        let owner_uid = current_process_uid()?;

        if let Err(watcher_error) = self.stop_watcher().await {
            let cleanup = self.cleanup_active_locked().await;
            let unlock = self.release_process_lock();
            return Err(format!(
                "{watcher_error}; sidecar_cleanup_failed:{}{}",
                cleanup.err().unwrap_or_default(),
                unlock
                    .err()
                    .map(|error| format!("; {error}"))
                    .unwrap_or_default()
            ));
        }
        self.acquire_process_lock().await?;
        if self.active.lock().await.is_some() {
            if let Err(error) = self.cleanup_active_locked().await {
                self.release_process_lock()?;
                return Err(error);
            }
        }
        if let Err(error) = self.cleanup_owned_process().await {
            self.release_process_lock()?;
            return Err(error);
        }
        let launched_at_secs = match unix_time_secs() {
            Ok(value) => value,
            Err(error) => {
                self.release_process_lock()?;
                return Err(error);
            }
        };
        let intent = SidecarIdentity {
            schema_version: 1,
            phase: SidecarPhase::Launching,
            pid: 0,
            pgid: 0,
            port: 0,
            python: python.clone(),
            script: script.clone(),
            package_id: package.package_id.clone(),
            build_version: package.build_version.clone(),
            started_at_secs: 0,
            launched_at_secs,
            owner_uid,
            instance_id: instance_id.clone(),
        };
        if let Err(error) = self.write_identity_if_absent(&intent) {
            return self.fail_launch(error, &intent).await;
        }
        let log = match self.open_runner_log() {
            Ok(log) => log,
            Err(error) => return self.fail_launch(error, &intent).await,
        };
        let stdout_log = tokio::fs::File::from_std(log);
        let log = Arc::new(Mutex::new(stdout_log));
        let log_failed = Arc::new(AtomicBool::new(false));
        let (ready_tx, ready_rx) = oneshot::channel();

        self.run_before_spawn_hook();
        #[cfg(windows)]
        let job_handle = match crate::platform::process::JobHandle::create() {
            Ok(job) => Some(job),
            Err(error) => return self.fail_launch(error, &intent).await,
        };
        #[cfg(not(windows))]
        let job_handle: Option<()> = None;
        let mut command = Command::new(&python);
        command
            .kill_on_drop(true)
            .args(&arguments)
            .current_dir(&pinned_root)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .env_clear();
        for key in [
            "HOME",
            "USER",
            "LOGNAME",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TMPDIR",
            "SHELL",
        ] {
            if let Some(value) = std::env::var_os(key) {
                command.env(key, value);
            }
        }
        for key in crate::platform::env_paths::passthrough_env_keys() {
            if let Some(value) = std::env::var_os(key) {
                command.env(key, value);
            }
        }
        command
            .env(
                "PATH",
                crate::platform::env_paths::runner_path(node_parent.as_path()),
            )
            .env("NODE_BIN", &node)
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .env("PYTHONNOUSERSITE", "1")
            .env("YIRENGONGIS_BASE_DIR", &current_root)
            .env("YIRENGONGIS_STATE_DIR", &self.state_root)
            .env("YIRENGONGIS_RUNNER_HOST", "127.0.0.1")
            .env("YIRENGONGIS_RUNNER_PORT", self.preferred_port.to_string())
            .env("YIRENGONGIS_SESSION_TOKEN", &session_token)
            .env("YIRENGONGIS_SUPERVISED_BY_TAURI", "1")
            .env(OWNER_MARKER_ENV, &instance_id)
            .env("YIRENGONGIS_PACKAGE_ID", &package.package_id)
            .env("YIRENGONGIS_BUILD_VERSION", &package.build_version);
        #[cfg(unix)]
        command.as_std_mut().process_group(0);
        #[cfg(windows)]
        crate::platform::process::configure_hidden_command(command.as_std_mut());
        let mut child = match command.spawn() {
            Ok(child) => child,
            Err(error) => return self.fail_launch(error.to_string(), &intent).await,
        };
        #[cfg(windows)]
        if let Some(job) = job_handle.as_ref() {
            // Job Object 覆盖子进程及其后续子进程；壳退出即整树终止
            let _ = job.assign(child.id().unwrap_or_default());
        }
        let pid = match child.id() {
            Some(pid) => pid,
            None => {
                terminate_unowned_child(&mut child).await;
                return self
                    .fail_launch("sidecar_pid_missing".into(), &intent)
                    .await;
            }
        };
        let stdout = match child.stdout.take() {
            Some(stdout) => stdout,
            None => {
                terminate_unowned_child(&mut child).await;
                return self
                    .fail_launch("sidecar_stdout_missing".into(), &intent)
                    .await;
            }
        };
        let stderr = match child.stderr.take() {
            Some(stderr) => stderr,
            None => {
                terminate_unowned_child(&mut child).await;
                return self
                    .fail_launch("sidecar_stderr_missing".into(), &intent)
                    .await;
            }
        };
        let package_id = package.package_id.clone();
        let build_version = package.build_version.clone();
        let redaction = session_token.clone();
        let stdout_log = log.clone();
        let stdout_log_failed = log_failed.clone();
        let stdout_task = tokio::spawn(async move {
            drain_stdout(
                stdout,
                stdout_log,
                stdout_log_failed,
                redaction.as_bytes(),
                &package_id,
                &build_version,
                ready_tx,
            )
            .await
        });
        let stderr_redaction = session_token.clone();
        let stderr_task = tokio::spawn(async move {
            drain_stderr(stderr, log, log_failed, stderr_redaction.as_bytes()).await
        });
        *self.active.lock().await = Some(SidecarHandle {
            port: 0,
            session_token: session_token.clone(),
            child,
            stdout_task,
            stderr_task,
            pid,
            started_at_secs: None,
            identity: Some(intent.clone()),
            #[cfg(windows)]
            job: job_handle,
        });

        let started_at_secs = match Self::process_start_time(pid).await {
            Ok(started) => started,
            Err(error) => return self.fail_start(error).await,
        };
        if let Some(active) = self.active.lock().await.as_mut() {
            active.started_at_secs = Some(started_at_secs);
        }
        let mut spawned_identity = intent.clone();
        spawned_identity.phase = SidecarPhase::Spawned;
        spawned_identity.pid = pid;
        spawned_identity.pgid = pid;
        spawned_identity.started_at_secs = started_at_secs;
        if let Some(active) = self.active.lock().await.as_mut() {
            active.identity = Some(spawned_identity.clone());
        }
        if let Err(error) = self.replace_identity_if_matches(&intent, &spawned_identity) {
            return self.fail_start(error).await;
        }
        self.run_after_spawn_identity_hook();
        let ready = match timeout(READY_TIMEOUT, ready_rx).await {
            Ok(Ok(Ok(ready))) => ready,
            Ok(Ok(Err(error))) => return self.fail_start(error).await,
            Ok(Err(_)) => return self.fail_start("sidecar_ready_eof".into()).await,
            Err(_) => return self.fail_start("sidecar_ready_timeout".into()).await,
        };
        if let Some(active) = self.active.lock().await.as_mut() {
            active.port = ready.port;
        }
        let mut identity = spawned_identity.clone();
        identity.phase = SidecarPhase::Ready;
        identity.port = ready.port;
        if let Some(active) = self.active.lock().await.as_mut() {
            active.identity = Some(identity.clone());
        }
        if let Err(error) = self.replace_identity_if_matches(&spawned_identity, &identity) {
            return self.fail_start(error).await;
        }
        for _ in 0..HEALTH_ATTEMPTS {
            let exit_state = {
                let mut active = self.active.lock().await;
                match active.as_mut() {
                    Some(active) => active
                        .child
                        .try_wait()
                        .map(|status| status.is_some())
                        .map_err(|error| error.to_string()),
                    None => Err("sidecar_handle_missing".into()),
                }
            };
            let exited = match exit_state {
                Ok(exited) => exited,
                Err(error) => return self.fail_start(error).await,
            };
            if exited {
                return self.fail_start("sidecar_exited_before_ready".into()).await;
            }
            let url = format!("http://127.0.0.1:{}/supervised/health", ready.port);
            if let Ok(response) = self
                .client
                .get(url)
                .header("X-YRG-Session", &session_token)
                .timeout(Duration::from_millis(300))
                .send()
                .await
            {
                if response.status().is_success() {
                    if let Ok(value) = response.json::<serde_json::Value>().await {
                        if value["package_id"] != package.package_id
                            || value["build_version"] != package.build_version
                            || value["port"].as_u64() != Some(ready.port as u64)
                        {
                            return self
                                .fail_start("sidecar_health_identity_mismatch".into())
                                .await;
                        }
                        if value["ok"] == true {
                            self.running.store(true, Ordering::Release);
                            self.start_watcher().await;
                            return Ok(ready.port);
                        }
                    }
                }
            }
            sleep(HEALTH_POLL).await;
        }
        self.fail_start("sidecar_health_timeout".into()).await
    }

    pub async fn stop(&self) -> Result<(), String> {
        let _transition = self.transition.lock().await;
        let watcher = self.stop_watcher().await;
        let cleanup = self.cleanup_active_locked().await;
        let unlock = self.release_process_lock();
        match (watcher, cleanup, unlock) {
            (Ok(()), Ok(()), Ok(())) => Ok(()),
            (watcher, cleanup, unlock) => Err(format!(
                "sidecar_stop_failed:{}{}{}",
                watcher.err().unwrap_or_default(),
                cleanup
                    .err()
                    .map(|error| format!("; {error}"))
                    .unwrap_or_default(),
                unlock
                    .err()
                    .map(|error| format!("; {error}"))
                    .unwrap_or_default()
            )),
        }
    }

    pub async fn connection(&self) -> Option<SidecarConnection> {
        if !self.running.load(Ordering::Acquire) {
            return None;
        }
        let mut active = self.active.lock().await;
        let handle = active.as_mut()?;
        if handle.port == 0 || !matches!(handle.child.try_wait(), Ok(None)) {
            self.running.store(false, Ordering::Release);
            return None;
        }
        Some(SidecarConnection {
            port: handle.port,
            token: handle.session_token.clone(),
        })
    }
}

fn sidecar_arguments(
    script: &Path,
    instance_id: &str,
    package_id: &str,
    build_version: &str,
) -> Result<Vec<String>, String> {
    let script = script
        .to_str()
        .ok_or_else(|| "sidecar_script_path_invalid".to_string())?;
    Ok(vec![
        "-I".into(),
        "-B".into(),
        "-u".into(),
        script.into(),
        "--yrg-instance-id".into(),
        instance_id.into(),
        "--yrg-package-id".into(),
        package_id.into(),
        "--yrg-build-version".into(),
        build_version.into(),
    ])
}

fn same_sidecar_owner(left: &SidecarIdentity, right: &SidecarIdentity) -> bool {
    left.instance_id == right.instance_id
        && left.owner_uid == right.owner_uid
        && left.launched_at_secs == right.launched_at_secs
        && left.python == right.python
        && left.script == right.script
        && left.package_id == right.package_id
        && left.build_version == right.build_version
}

async fn terminate_unowned_child(child: &mut Child) {
    let _ = child.start_kill();
    if timeout(Duration::from_secs(2), child.wait()).await.is_err() {
        let _ = child.start_kill();
        let _ = child.wait().await;
    }
}

fn unique_required_path(
    required: &[String],
    prefix: &str,
    suffix: &str,
    error: &str,
) -> Result<PathBuf, String> {
    let matches: Vec<_> = required
        .iter()
        .filter(|path| path.starts_with(prefix) && path.ends_with(suffix))
        .collect();
    if matches.len() != 1 {
        return Err(error.into());
    }
    Ok(PathBuf::from(matches[0]))
}

fn random_secret() -> String {
    let mut bytes = [0_u8; 32];
    rand::thread_rng().fill_bytes(&mut bytes);
    URL_SAFE_NO_PAD.encode(bytes)
}

fn unix_time_secs() -> Result<u64, String> {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .map_err(|_| "sidecar_clock_invalid".to_string())
}

#[cfg(unix)]
fn current_process_uid() -> Result<u32, String> {
    let mut system = System::new();
    let pid = Pid::from_u32(std::process::id());
    system.refresh_processes(ProcessesToUpdate::Some(&[pid]), true);
    system
        .process(pid)
        .and_then(|process| process.user_id())
        .map(|uid| **uid)
        .ok_or_else(|| "sidecar_owner_uid_missing".to_string())
}

/// Windows：owner_uid 记 0（身份匹配退化为 start_time + 环境标记）。
#[cfg(windows)]
fn current_process_uid() -> Result<u32, String> {
    Ok(0)
}

async fn drain_stdout(
    mut stdout: tokio::process::ChildStdout,
    log: Arc<Mutex<tokio::fs::File>>,
    log_failed: Arc<AtomicBool>,
    secret: &[u8],
    package_id: &str,
    build_version: &str,
    ready: oneshot::Sender<Result<ReadyFrame, String>>,
) -> Result<(), String> {
    let mut ready = Some(ready);
    let mut chunk = [0_u8; 4096];
    let mut line = Vec::new();
    let mut oversized = false;
    let mut report_log_failure = false;
    loop {
        let count = stdout
            .read(&mut chunk)
            .await
            .map_err(|error| error.to_string())?;
        if count == 0 {
            if let Some(ready) = ready.take() {
                let _ = ready.send(Err("sidecar_ready_eof".into()));
            }
            return if report_log_failure {
                Err("sidecar_log_write_failed".into())
            } else {
                Ok(())
            };
        }
        for byte in &chunk[..count] {
            if *byte == b'\n' {
                if oversized {
                    if let Some(ready) = ready.take() {
                        let _ = ready.send(Err("sidecar_ready_frame_too_large".into()));
                    }
                    report_log_failure |=
                        write_log_line(&log, &log_failed, b"[oversized stdout line]", secret).await;
                } else {
                    match parse_ready_frame(&line, package_id, build_version) {
                        Ok(Some(frame)) => {
                            if let Some(ready) = ready.take() {
                                let _ = ready.send(Ok(frame));
                            }
                        }
                        Ok(None) => {
                            report_log_failure |=
                                write_log_line(&log, &log_failed, &line, secret).await;
                        }
                        Err(error) => {
                            if let Some(ready) = ready.take() {
                                let _ = ready.send(Err(error));
                            }
                        }
                    }
                }
                line.clear();
                oversized = false;
            } else if line.len() < READY_FRAME_MAX_BYTES + 1 {
                line.push(*byte);
            } else {
                oversized = true;
            }
        }
    }
}

async fn drain_stderr(
    mut stderr: tokio::process::ChildStderr,
    log: Arc<Mutex<tokio::fs::File>>,
    log_failed: Arc<AtomicBool>,
    secret: &[u8],
) -> Result<(), String> {
    let mut chunk = [0_u8; 4096];
    let mut line = Vec::new();
    let mut oversized = false;
    let mut report_log_failure = false;
    loop {
        let count = stderr
            .read(&mut chunk)
            .await
            .map_err(|error| error.to_string())?;
        if count == 0 {
            if oversized {
                report_log_failure |=
                    write_log_line(&log, &log_failed, b"[oversized stderr line]", secret).await;
            } else if !line.is_empty() {
                report_log_failure |= write_log_line(&log, &log_failed, &line, secret).await;
            }
            return if report_log_failure {
                Err("sidecar_log_write_failed".into())
            } else {
                Ok(())
            };
        }
        for byte in &chunk[..count] {
            if *byte == b'\n' {
                if oversized {
                    report_log_failure |=
                        write_log_line(&log, &log_failed, b"[oversized stderr line]", secret).await;
                } else {
                    report_log_failure |= write_log_line(&log, &log_failed, &line, secret).await;
                }
                line.clear();
                oversized = false;
            } else if line.len() < STDERR_LINE_MAX_BYTES {
                line.push(*byte);
            } else {
                oversized = true;
            }
        }
    }
}

async fn write_log_line(
    log: &Arc<Mutex<tokio::fs::File>>,
    log_failed: &AtomicBool,
    line: &[u8],
    secret: &[u8],
) -> bool {
    if log_failed.load(Ordering::Acquire) {
        return false;
    }
    let mut log = log.lock().await;
    let result = if !secret.is_empty() && line.windows(secret.len()).any(|window| window == secret)
    {
        log.write_all(b"[redacted sidecar line]\n").await
    } else {
        match log.write_all(line).await {
            Ok(()) => log.write_all(b"\n").await,
            Err(error) => Err(error),
        }
    };
    let result = match result {
        Ok(()) => log.flush().await,
        Err(error) => Err(error),
    };
    if result.is_err() {
        return !log_failed.swap(true, Ordering::AcqRel);
    }
    false
}

fn process_matches_identity(identity: &SidecarIdentity) -> Result<bool, String> {
    identity.validate()?;
    if identity.phase == SidecarPhase::Launching {
        return Ok(false);
    }
    let mut system = System::new_all();
    let pid = Pid::from_u32(identity.pid);
    system.refresh_processes(ProcessesToUpdate::Some(&[pid]), true);
    let Some(process) = system.process(pid) else {
        return Ok(false);
    };
    if process.start_time() != identity.started_at_secs
        || !process_has_owner_capability(process, identity)
    {
        return Ok(false);
    }
    #[cfg(unix)]
    {
        // Unix：进程组身份校验（spawn 时 setsid，pgid 应等于 pid）
        let pgid = nix::unistd::getpgid(Some(nix::unistd::Pid::from_raw(identity.pid as i32)))
            .map(|pgid| pgid.as_raw() as u32)
            .ok();
        if pgid != Some(identity.pgid) || identity.pgid != identity.pid {
            return Ok(false);
        }
    }
    let expected_python = std::fs::canonicalize(&identity.python).ok();
    let actual_python = process
        .exe()
        .and_then(|path| std::fs::canonicalize(path).ok());
    if expected_python.is_none() || expected_python != actual_python {
        return Ok(false);
    }
    let actual_command: Vec<_> = process
        .cmd()
        .iter()
        .map(|part| part.to_string_lossy().into_owned())
        .collect();
    let mut expected_command = vec![identity.python.to_string_lossy().into_owned()];
    expected_command.extend(sidecar_arguments(
        &identity.script,
        &identity.instance_id,
        &identity.package_id,
        &identity.build_version,
    )?);
    Ok(actual_command == expected_command)
}

fn process_has_owner_capability(process: &sysinfo::Process, identity: &SidecarIdentity) -> bool {
    let marker = format!("{OWNER_MARKER_ENV}={}", identity.instance_id);
    #[cfg(unix)]
    let same_uid = process
        .user_id()
        .is_some_and(|uid| **uid == identity.owner_uid);
    // Windows：owner_uid 恒为 0，不参与匹配；由启动时间 + 环境标记定位
    #[cfg(windows)]
    let same_uid = true;
    same_uid
        && process.start_time() >= identity.launched_at_secs
        && process
            .environ()
            .iter()
            .any(|entry| entry.as_os_str() == OsStr::new(&marker))
        && !matches!(
            process.status(),
            sysinfo::ProcessStatus::Dead | sysinfo::ProcessStatus::Zombie
        )
}

fn owner_process_snapshot(identity: &SidecarIdentity) -> HashMap<u32, u64> {
    let system = System::new_all();
    let mut selected: HashMap<_, _> = system
        .processes()
        .iter()
        .filter(|(_, process)| process_has_owner_capability(process, identity))
        .map(|(pid, process)| (pid.as_u32(), process.start_time()))
        .collect();
    if identity.phase != SidecarPhase::Launching
        && !process_matches_identity(identity).unwrap_or(false)
    {
        selected.remove(&identity.pid);
    }
    selected
}

#[cfg(unix)]
fn signal_owner_if_same(
    pid: u32,
    started: u64,
    identity: &SidecarIdentity,
    signal: nix::sys::signal::Signal,
) {
    if identity.phase != SidecarPhase::Launching
        && pid == identity.pid
        && !process_matches_identity(identity).unwrap_or(false)
    {
        return;
    }
    let mut system = System::new_all();
    let sys_pid = Pid::from_u32(pid);
    system.refresh_processes(ProcessesToUpdate::Some(&[sys_pid]), true);
    if system.process(sys_pid).is_some_and(|process| {
        process.start_time() == started && process_has_owner_capability(process, identity)
    }) {
        let _ = nix::sys::signal::kill(nix::unistd::Pid::from_raw(pid as i32), signal);
    }
}

/// Windows：核对身份后用 taskkill /T /F 终止（无信号语义）。
#[cfg(windows)]
fn signal_owner_if_same(pid: u32, started: u64, identity: &SidecarIdentity) {
    if identity.phase != SidecarPhase::Launching
        && pid == identity.pid
        && !process_matches_identity(identity).unwrap_or(false)
    {
        return;
    }
    let mut system = System::new_all();
    let sys_pid = Pid::from_u32(pid);
    system.refresh_processes(ProcessesToUpdate::Some(&[sys_pid]), true);
    if system.process(sys_pid).is_some_and(|process| {
        process.start_time() == started && process_has_owner_capability(process, identity)
    }) {
        crate::platform::process::terminate_tree(pid);
    }
}

fn owner_process_is_live_same(pid: u32, started: u64, identity: &SidecarIdentity) -> bool {
    if identity.phase != SidecarPhase::Launching
        && pid == identity.pid
        && !process_matches_identity(identity).unwrap_or(false)
    {
        return false;
    }
    let mut system = System::new_all();
    let pid = Pid::from_u32(pid);
    system.refresh_processes(ProcessesToUpdate::Some(&[pid]), true);
    system.process(pid).is_some_and(|process| {
        process.start_time() == started && process_has_owner_capability(process, identity)
    })
}

#[cfg(unix)]
async fn terminate_owner_processes(identity: &SidecarIdentity) -> Result<(), String> {
    identity.validate()?;
    if identity.phase != SidecarPhase::Launching {
        signal_owner_if_same(
            identity.pid,
            identity.started_at_secs,
            identity,
            nix::sys::signal::Signal::SIGTERM,
        );
    }
    sleep(Duration::from_millis(25)).await;
    let mut snapshot = owner_process_snapshot(identity);
    for (target, started) in &snapshot {
        if *target == identity.pid {
            continue;
        }
        signal_owner_if_same(
            *target,
            *started,
            identity,
            nix::sys::signal::Signal::SIGTERM,
        );
    }
    let deadline = tokio::time::Instant::now() + TERMINATE_GRACE;
    loop {
        let observed = owner_process_snapshot(identity);
        for (target, started) in &observed {
            snapshot.entry(*target).or_insert(*started);
            if *target != identity.pid {
                signal_owner_if_same(
                    *target,
                    *started,
                    identity,
                    nix::sys::signal::Signal::SIGTERM,
                );
            }
        }
        let alive = snapshot
            .iter()
            .any(|(target, started)| owner_process_is_live_same(*target, *started, identity));
        if !alive || tokio::time::Instant::now() >= deadline {
            break;
        }
        sleep(Duration::from_millis(25)).await;
    }
    if identity.phase != SidecarPhase::Launching {
        signal_owner_if_same(
            identity.pid,
            identity.started_at_secs,
            identity,
            nix::sys::signal::Signal::SIGKILL,
        );
    }
    for (target, started) in &snapshot {
        if *target == identity.pid {
            continue;
        }
        signal_owner_if_same(
            *target,
            *started,
            identity,
            nix::sys::signal::Signal::SIGKILL,
        );
    }
    let deadline = tokio::time::Instant::now() + Duration::from_secs(1);
    let mut empty_scans = 0_u8;
    loop {
        let observed = owner_process_snapshot(identity);
        for (target, started) in &observed {
            snapshot.entry(*target).or_insert(*started);
            signal_owner_if_same(
                *target,
                *started,
                identity,
                nix::sys::signal::Signal::SIGKILL,
            );
        }
        let survivors: Vec<_> = snapshot
            .iter()
            .filter(|(target, started)| owner_process_is_live_same(**target, **started, identity))
            .map(|(target, _)| *target)
            .collect();
        if observed.is_empty() && survivors.is_empty() {
            empty_scans += 1;
            if empty_scans >= 2 {
                return Ok(());
            }
        } else {
            empty_scans = 0;
        }
        if tokio::time::Instant::now() >= deadline {
            return Err(format!("sidecar_descendants_survived:{survivors:?}"));
        }
        sleep(Duration::from_millis(25)).await;
    }
}

/// Windows：对身份匹配的进程逐个 taskkill /T /F，轮询确认退出。
#[cfg(windows)]
async fn terminate_owner_processes(identity: &SidecarIdentity) -> Result<(), String> {
    identity.validate()?;
    let mut snapshot = owner_process_snapshot(identity);
    for (target, started) in snapshot.clone() {
        signal_owner_if_same(target, started, identity);
    }
    let deadline = tokio::time::Instant::now() + TERMINATE_GRACE + Duration::from_secs(1);
    let mut empty_scans = 0_u8;
    loop {
        let observed = owner_process_snapshot(identity);
        for (target, started) in &observed {
            snapshot.entry(*target).or_insert(*started);
            signal_owner_if_same(*target, *started, identity);
        }
        let survivors: Vec<_> = snapshot
            .iter()
            .filter(|(target, started)| owner_process_is_live_same(**target, **started, identity))
            .map(|(target, _)| *target)
            .collect();
        if observed.is_empty() && survivors.is_empty() {
            empty_scans += 1;
            if empty_scans >= 2 {
                return Ok(());
            }
        } else {
            empty_scans = 0;
        }
        if tokio::time::Instant::now() >= deadline {
            return Err(format!("sidecar_descendants_survived:{survivors:?}"));
        }
        sleep(Duration::from_millis(50)).await;
    }
}

#[cfg(all(test, unix))]
mod tests {
    use std::{
        fs,
        io::{Read, Write},
        net::TcpListener,
        os::unix::{
            fs::{symlink, MetadataExt, PermissionsExt},
            process::CommandExt,
        },
        path::{Path, PathBuf},
        sync::{
            atomic::{AtomicBool, Ordering},
            Arc,
        },
        time::Duration,
    };

    use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
    use ed25519_dalek::{
        pkcs8::{spki::der::pem::LineEnding, EncodePublicKey},
        Signer, SigningKey,
    };
    use serde_json::{json, Value};

    use crate::{
        manifest::VerifiedPackageManifest,
        runtime::{RuntimeKind, RuntimeResolution, VerifiedView, ViewManager},
    };

    use super::{
        current_process_uid, drain_stdout, owner_process_snapshot, parse_ready_frame,
        random_secret, sidecar_arguments, terminate_owner_processes, unix_time_secs,
        SidecarIdentity, SidecarPhase, SidecarSupervisor, IDENTITY_MAX_BYTES, OWNER_MARKER_ENV,
        READY_FRAME_MAX_BYTES,
    };

    const BUILD_VERSION: &str = "20260711";
    const PACKAGE_ID: &str = "data-scientist-community-mac-arm64";
    const NODE_RELATIVE: &str = "runtime/node-arm64/node-v20.15.1-darwin-arm64/bin/node";

    fn canonical_json(value: &Value, output: &mut String) {
        match value {
            Value::Null => output.push_str("null"),
            Value::Bool(value) => output.push_str(if *value { "true" } else { "false" }),
            Value::Number(value) => output.push_str(&value.to_string()),
            Value::String(value) => output.push_str(&serde_json::to_string(value).unwrap()),
            Value::Array(values) => {
                output.push('[');
                for (index, value) in values.iter().enumerate() {
                    if index != 0 {
                        output.push(',');
                    }
                    canonical_json(value, output);
                }
                output.push(']');
            }
            Value::Object(values) => {
                output.push('{');
                let mut keys: Vec<_> = values.keys().collect();
                keys.sort_unstable();
                for (index, key) in keys.into_iter().enumerate() {
                    if index != 0 {
                        output.push(',');
                    }
                    output.push_str(&serde_json::to_string(key).unwrap());
                    output.push(':');
                    canonical_json(&values[key], output);
                }
                output.push('}');
            }
        }
    }

    fn verified_manifest() -> Arc<VerifiedPackageManifest> {
        let descriptor = |kind: &str, required_files: Vec<&str>| {
            json!({
                "archive": format!("{kind}-runtime.tar.zst"),
                "required_files": required_files,
                "sha256": "a".repeat(64),
                "size_bytes": 1,
                "tree_sha256": "b".repeat(64),
                "version": format!("{kind}-v1")
            })
        };
        let payload = json!({
            "arch": "arm64",
            "build_version": BUILD_VERSION,
            "key_id": "test-key",
            "package_id": PACKAGE_ID,
            "runtimes": {
                "core": descriptor("core", vec![
                    "runtime/python-arm64/python/bin/python3",
                    "scripts/_run.py",
                ]),
                "collector": descriptor("collector", vec![NODE_RELATIVE]),
            }
        });
        let signing = SigningKey::from_bytes(&[29_u8; 32]);
        let mut canonical = String::new();
        canonical_json(&payload, &mut canonical);
        let signature = URL_SAFE_NO_PAD.encode(signing.sign(canonical.as_bytes()).to_bytes());
        let signed =
            serde_json::to_vec(&json!({"payload": payload, "signature": signature})).unwrap();
        let public_key = signing
            .verifying_key()
            .to_public_key_pem(LineEnding::LF)
            .unwrap();
        let keys = serde_json::to_vec(&json!({
            "active_key_id": "test-key",
            "keys": [{"key_id": "test-key", "public_key_pem": public_key}]
        }))
        .unwrap();
        Arc::new(VerifiedPackageManifest::from_signed(&signed, &keys).unwrap())
    }

    fn test_python() -> PathBuf {
        if let Some(path) = std::env::var_os("YRG_TEST_PYTHON") {
            return fs::canonicalize(path).unwrap();
        }
        fs::canonicalize(Path::new(env!("CARGO_MANIFEST_DIR")).join("../../.venv/bin/python"))
            .unwrap()
    }

    fn fake_runner(
        wrong_health: bool,
        stderr_bytes: usize,
        spawn_descendants: bool,
        crash_after_ready: bool,
    ) -> String {
        let health_package = serde_json::to_string(if wrong_health {
            "wrong-package"
        } else {
            PACKAGE_ID
        })
        .unwrap();
        let spawn_descendants = if spawn_descendants { "True" } else { "False" };
        let crash_after_ready = if crash_after_ready { "True" } else { "False" };
        format!(
            r#"import http.server, json, os, signal, socket, subprocess, sys, threading, time
preferred = int(os.environ['YIRENGONGIS_RUNNER_PORT'])
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        if self.path != '/supervised/health':
            self.send_response(404); self.end_headers(); return
        if self.headers.get('X-YRG-Session') != os.environ['YIRENGONGIS_SESSION_TOKEN']:
            self.send_response(401); self.end_headers(); return
        payload = {{'ok': True, 'package_id': {health_package}, 'build_version': os.environ['YIRENGONGIS_BUILD_VERSION'], 'port': self.server.server_port}}
        body = json.dumps(payload).encode()
        self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
try:
    server = http.server.ThreadingHTTPServer(('127.0.0.1', preferred), Handler)
except OSError:
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
descendants = []
if {spawn_descendants}:
    child_code = "import os,signal,subprocess,sys,time,json; signal.signal(signal.SIGTERM, signal.SIG_IGN); g=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)'], start_new_session=True); open(os.environ['YRG_CHILD_INFO'],'w').write(json.dumps({{'child':os.getpid(),'grandchild':g.pid}})); time.sleep(300)"
    child = subprocess.Popen([sys.executable, '-c', child_code], env={{**os.environ, 'YRG_CHILD_INFO': os.path.join(os.environ['YIRENGONGIS_STATE_DIR'],'downloads','child-info.json')}}, start_new_session=True)
    descendants.append(child.pid)
    for _ in range(100):
        if os.path.exists(os.path.join(os.environ['YIRENGONGIS_STATE_DIR'],'downloads','child-info.json')): break
        time.sleep(.01)
audit = {{
  'pid': os.getpid(), 'argv': sys.argv,
  'node_bin': os.environ.get('NODE_BIN'), 'path': os.environ.get('PATH'),
  'cwd': os.getcwd(), 'executable': sys.executable,
  'base_dir': os.environ.get('YIRENGONGIS_BASE_DIR'),
  'node_exists': os.path.exists(os.environ.get('NODE_BIN', '')),
  'owner_marker': os.environ.get('YIRENGONGIS_PROCESS_OWNER_ID'),
  'pythonpath': os.environ.get('PYTHONPATH'), 'pythonhome': os.environ.get('PYTHONHOME'),
  'node_options': os.environ.get('NODE_OPTIONS'), 'license_bypass': os.environ.get('YIRENGONGIS_LICENSE_BYPASS'),
}}
open(os.path.join(os.environ['YIRENGONGIS_STATE_DIR'],'downloads','sidecar-audit.json'),'w').write(json.dumps(audit))
if {stderr_bytes}:
    os.write(2, ('ERRTOKEN=' + os.environ['YIRENGONGIS_SESSION_TOKEN'] + '\n').encode())
    os.write(2, b'e' * {stderr_bytes})
frame = {{'event':'ready','port':server.server_port,'package_id':os.environ['YIRENGONGIS_PACKAGE_ID'],'build_version':os.environ['YIRENGONGIS_BUILD_VERSION']}}
print('noise-before-ready', flush=True)
print('YRG_SIDECAR_READY ' + json.dumps(frame, separators=(',',':')), flush=True)
print('TOKEN=' + os.environ['YIRENGONGIS_SESSION_TOKEN'], flush=True)
crash_marker = os.path.join(os.environ['YIRENGONGIS_STATE_DIR'],'downloads','crashed-once')
if {crash_after_ready} and not os.path.exists(crash_marker):
    open(crash_marker, 'w').write('1')
    def crash_once():
        time.sleep(.3)
        os._exit(23)
    threading.Thread(target=crash_once, daemon=True).start()
server.serve_forever()
"#,
        )
    }

    struct SidecarFixture {
        _temp: tempfile::TempDir,
        state_root: PathBuf,
        manifest: Arc<VerifiedPackageManifest>,
        view: VerifiedView,
        manager: ViewManager,
        core: RuntimeResolution,
        collector: RuntimeResolution,
    }

    fn fixture(wrong_health: bool, stderr_bytes: usize, spawn_descendants: bool) -> SidecarFixture {
        fixture_with_options(wrong_health, stderr_bytes, spawn_descendants, false, true)
    }

    fn fixture_with_options(
        wrong_health: bool,
        stderr_bytes: usize,
        spawn_descendants: bool,
        crash_after_ready: bool,
        activate_collector: bool,
    ) -> SidecarFixture {
        let temp = tempfile::tempdir().unwrap();
        let state_root = temp.path().join("state");
        fs::create_dir(&state_root).unwrap();
        let core = state_root.join("core-v1");
        let collector = state_root.join("collector-v1");
        for path in [
            core.join("scripts"),
            core.join("frontend-compat"),
            core.join("runtime/python-arm64/python/bin"),
            collector.join("scripts"),
            collector.join("runtime/node-arm64/node-v20.15.1-darwin-arm64/bin"),
            collector.join("runtime/playwright-browsers"),
            collector.join("node_modules"),
        ] {
            fs::create_dir_all(path).unwrap();
        }
        fs::write(
            core.join("scripts/_run.py"),
            fake_runner(
                wrong_health,
                stderr_bytes,
                spawn_descendants,
                crash_after_ready,
            ),
        )
        .unwrap();
        fs::write(core.join("frontend-compat/progress.html"), b"frontend").unwrap();
        symlink(
            test_python(),
            core.join("runtime/python-arm64/python/bin/python3"),
        )
        .unwrap();
        let node = collector.join(NODE_RELATIVE);
        fs::write(&node, b"#!/bin/sh\nexit 0\n").unwrap();
        fs::set_permissions(&node, fs::Permissions::from_mode(0o755)).unwrap();

        let manifest = verified_manifest();
        let manager = ViewManager::new(state_root.clone(), manifest.clone()).unwrap();
        let core = RuntimeResolution::fixture(RuntimeKind::Core, "core-v1", &core, false).unwrap();
        let collector =
            RuntimeResolution::fixture(RuntimeKind::Collector, "collector-v1", &collector, false)
                .unwrap();
        let view = if activate_collector {
            manager.activate_collector(&core, &collector).unwrap()
        } else {
            manager.activate_core(&core).unwrap()
        };
        SidecarFixture {
            _temp: temp,
            state_root,
            manifest,
            view,
            manager,
            core,
            collector,
        }
    }

    fn pid_alive(pid: u32) -> bool {
        let status = std::process::Command::new("/bin/kill")
            .args(["-0", &pid.to_string()])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()
            .unwrap();
        status.success()
    }

    #[test]
    fn ready_frame_parser_accepts_noise_then_exact_bounded_frame() {
        let valid = format!(
            "YRG_SIDECAR_READY {{\"event\":\"ready\",\"port\":43210,\"package_id\":\"{PACKAGE_ID}\",\"build_version\":\"{BUILD_VERSION}\"}}"
        );
        assert!(parse_ready_frame(b"noise", PACKAGE_ID, BUILD_VERSION)
            .unwrap()
            .is_none());
        assert_eq!(
            parse_ready_frame(valid.as_bytes(), PACKAGE_ID, BUILD_VERSION)
                .unwrap()
                .unwrap()
                .port,
            43210
        );
        let oversized = vec![b'x'; READY_FRAME_MAX_BYTES + 1];
        assert_eq!(
            parse_ready_frame(&oversized, PACKAGE_ID, BUILD_VERSION).unwrap_err(),
            "sidecar_ready_frame_too_large"
        );
        assert!(parse_ready_frame(b"YRG_SIDECAR_READY {bad", PACKAGE_ID, BUILD_VERSION).is_err());
    }

    #[tokio::test]
    async fn stdout_drain_reports_malformed_oversized_and_eof_before_ready() {
        let cases = [
            (
                "malformed",
                "print('YRG_SIDECAR_READY {bad', flush=True)",
                "sidecar_ready_frame_invalid",
            ),
            (
                "oversized",
                "print('x' * 5000, flush=True)",
                "sidecar_ready_frame_too_large",
            ),
            ("eof", "pass", "sidecar_ready_eof"),
        ];
        for (name, code, expected) in cases {
            let mut child = tokio::process::Command::new(test_python())
                .args(["-u", "-c", code])
                .stdout(std::process::Stdio::piped())
                .spawn()
                .unwrap();
            let stdout = child.stdout.take().unwrap();
            let log_file = tempfile::tempfile().unwrap();
            let log = Arc::new(tokio::sync::Mutex::new(tokio::fs::File::from_std(log_file)));
            let (ready_tx, ready_rx) = tokio::sync::oneshot::channel();
            let drain = tokio::spawn(async move {
                drain_stdout(
                    stdout,
                    log,
                    Arc::new(AtomicBool::new(false)),
                    b"",
                    PACKAGE_ID,
                    BUILD_VERSION,
                    ready_tx,
                )
                .await
            });
            let error = tokio::time::timeout(Duration::from_secs(2), ready_rx)
                .await
                .unwrap_or_else(|_| panic!("{name} ready receiver timed out"))
                .unwrap()
                .unwrap_err();
            assert_eq!(error, expected, "case={name}");
            assert!(child.wait().await.unwrap().success(), "case={name}");
            drain.await.unwrap().unwrap();
        }
    }

    #[tokio::test]
    async fn stdout_log_failure_switches_to_discard_and_never_blocks_ready_or_exit() {
        let frame = format!(
            "{{'event':'ready','port':43210,'package_id':'{PACKAGE_ID}','build_version':'{BUILD_VERSION}'}}"
        );
        let code = format!(
            "import os\nprint('noise', flush=True)\nprint('YRG_SIDECAR_READY ' + str({frame}).replace(\"'\", '\"'), flush=True)\nos.write(1, b'x' * 1048576)\n"
        );
        let mut child = tokio::process::Command::new(test_python())
            .args(["-u", "-c", &code])
            .stdout(std::process::Stdio::piped())
            .spawn()
            .unwrap();
        let stdout = child.stdout.take().unwrap();
        let temp = tempfile::NamedTempFile::new().unwrap();
        let read_only = fs::File::open(temp.path()).unwrap();
        let log = Arc::new(tokio::sync::Mutex::new(tokio::fs::File::from_std(
            read_only,
        )));
        let (ready_tx, ready_rx) = tokio::sync::oneshot::channel();
        let drain = tokio::spawn(async move {
            drain_stdout(
                stdout,
                log,
                Arc::new(AtomicBool::new(false)),
                b"",
                PACKAGE_ID,
                BUILD_VERSION,
                ready_tx,
            )
            .await
        });
        let ready = tokio::time::timeout(Duration::from_secs(2), ready_rx)
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        assert_eq!(ready.port, 43210);
        assert!(tokio::time::timeout(Duration::from_secs(2), child.wait())
            .await
            .unwrap()
            .unwrap()
            .success());
        assert_eq!(
            drain.await.unwrap().unwrap_err(),
            "sidecar_log_write_failed"
        );
    }

    #[tokio::test]
    async fn health_client_never_follows_redirects_or_forwards_the_session_header() {
        let fixture = fixture(false, 0, false);
        let supervisor =
            SidecarSupervisor::new(fixture.state_root.clone(), fixture.manifest.clone()).unwrap();
        let redirected = TcpListener::bind("127.0.0.1:0").unwrap();
        redirected.set_nonblocking(true).unwrap();
        let redirected_address = redirected.local_addr().unwrap();
        let redirector = TcpListener::bind("127.0.0.1:0").unwrap();
        let redirector_address = redirector.local_addr().unwrap();
        let observed = Arc::new(std::sync::Mutex::new(Vec::new()));
        let observed_request = observed.clone();

        let redirected_server = std::thread::spawn(move || {
            let deadline = std::time::Instant::now() + Duration::from_millis(500);
            while std::time::Instant::now() < deadline {
                match redirected.accept() {
                    Ok((mut stream, _)) => {
                        stream
                            .set_read_timeout(Some(Duration::from_millis(200)))
                            .unwrap();
                        let mut request = [0_u8; 4096];
                        let length = stream.read(&mut request).unwrap_or_default();
                        observed_request
                            .lock()
                            .unwrap()
                            .extend_from_slice(&request[..length]);
                        stream
                            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
                            .unwrap();
                        return;
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(Duration::from_millis(10));
                    }
                    Err(error) => panic!("redirected listener failed: {error}"),
                }
            }
        });
        let redirector_server = std::thread::spawn(move || {
            let (mut stream, _) = redirector.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_millis(200)))
                .unwrap();
            let mut request = [0_u8; 4096];
            let _ = stream.read(&mut request);
            let response = format!(
                "HTTP/1.1 302 Found\r\nLocation: http://{redirected_address}/capture\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            );
            stream.write_all(response.as_bytes()).unwrap();
        });

        let response = supervisor
            .client
            .get(format!("http://{redirector_address}/supervised/health"))
            .header("X-YRG-Session", "redirect-secret")
            .send()
            .await
            .unwrap();
        redirector_server.join().unwrap();
        redirected_server.join().unwrap();

        assert_eq!(response.status(), reqwest::StatusCode::FOUND);
        let request = String::from_utf8(observed.lock().unwrap().clone()).unwrap();
        assert!(
            request.is_empty(),
            "redirect target was contacted: {request}"
        );
        assert!(!request.contains("redirect-secret"));
    }

    #[tokio::test]
    async fn occupied_port_uses_python_dynamic_port_and_signed_bundled_node() {
        let fixture = fixture(false, 256 * 1024, false);
        let occupied = TcpListener::bind("127.0.0.1:0").unwrap();
        let preferred = occupied.local_addr().unwrap().port();
        let supervisor = SidecarSupervisor::new_with_preferred_port(
            fixture.state_root.clone(),
            fixture.manifest.clone(),
            preferred,
        )
        .unwrap();

        let port = supervisor.start(&fixture.view).await.unwrap();
        assert_ne!(port, preferred);
        let audit: Value = serde_json::from_slice(
            &fs::read(fixture.state_root.join("downloads/sidecar-audit.json")).unwrap(),
        )
        .unwrap();
        let expected_node = fixture.view.path().join(NODE_RELATIVE);
        assert_eq!(audit["node_bin"], expected_node.to_string_lossy().as_ref());
        assert!(audit["path"]
            .as_str()
            .unwrap()
            .starts_with(expected_node.parent().unwrap().to_str().unwrap()));
        for key in ["pythonpath", "pythonhome", "node_options", "license_bypass"] {
            assert!(audit[key].is_null(), "{key} leaked: {}", audit[key]);
        }
        let argv = audit["argv"].as_array().unwrap();
        let marker_index = argv
            .iter()
            .position(|value| value == "--yrg-instance-id")
            .unwrap()
            + 1;
        assert_eq!(audit["owner_marker"], argv[marker_index]);
        let log =
            fs::read_to_string(fixture.state_root.join("downloads/runner_process.log")).unwrap();
        assert!(log.contains("noise-before-ready"));
        assert!(log.contains("[redacted sidecar line]"));
        assert!(!log.contains("TOKEN="));
        let log_metadata =
            fs::symlink_metadata(fixture.state_root.join("downloads/runner_process.log")).unwrap();
        assert_eq!(log_metadata.permissions().mode() & 0o777, 0o600);
        assert_eq!(log_metadata.nlink(), 1);
        supervisor.stop().await.unwrap();
        assert!(!fixture.state_root.join("runtimes/sidecar.json").exists());
    }

    #[tokio::test]
    async fn stale_verified_view_is_rejected_before_spawn() {
        let fixture = fixture(false, 0, false);
        fs::remove_file(fixture.view.path()).unwrap();
        let supervisor =
            SidecarSupervisor::new(fixture.state_root.clone(), fixture.manifest.clone()).unwrap();
        assert_eq!(
            supervisor.start(&fixture.view).await.unwrap_err(),
            "view_handle_changed"
        );
        assert!(!fixture
            .state_root
            .join("downloads/sidecar-audit.json")
            .exists());
    }

    #[tokio::test]
    async fn launch_intent_is_durable_before_spawn_and_contains_no_session_token() {
        let fixture = fixture(false, 0, false);
        let supervisor =
            SidecarSupervisor::new(fixture.state_root.clone(), fixture.manifest.clone()).unwrap();
        let observed = Arc::new(AtomicBool::new(false));
        let spawned_observed = Arc::new(AtomicBool::new(false));
        let hook_supervisor = supervisor.clone();
        let hook_observed = observed.clone();
        supervisor.set_before_spawn_hook(move || {
            let bytes = fs::read(hook_supervisor.identity_path()).unwrap();
            let value: Value = serde_json::from_slice(&bytes).unwrap();
            assert_eq!(value["phase"], "launching");
            assert_eq!(value["pid"], 0);
            assert_eq!(value["port"], 0);
            assert_eq!(value["instance_id"].as_str().unwrap().len(), 43);
            assert!(value.get("session_token").is_none());
            assert!(!String::from_utf8(bytes).unwrap().contains("X-YRG-Session"));
            hook_observed.store(true, Ordering::Release);
        });
        let spawned_supervisor = supervisor.clone();
        let hook_spawned_observed = spawned_observed.clone();
        supervisor.set_after_spawn_identity_hook(move || {
            let identity = spawned_supervisor.read_identity().unwrap().unwrap();
            assert_eq!(identity.phase, SidecarPhase::Spawned);
            assert_ne!(identity.pid, 0);
            assert_eq!(identity.port, 0);
            assert_ne!(identity.started_at_secs, 0);
            hook_spawned_observed.store(true, Ordering::Release);
        });
        let port = supervisor.start(&fixture.view).await.unwrap();
        assert!(observed.load(Ordering::Acquire));
        assert!(spawned_observed.load(Ordering::Acquire));
        let ready = supervisor.read_identity().unwrap().unwrap();
        assert_eq!(ready.phase, SidecarPhase::Ready);
        assert_eq!(ready.port, port);
        supervisor.stop().await.unwrap();
    }

    #[tokio::test]
    async fn spawn_failure_compare_unlinks_its_launch_intent_and_releases_lock() {
        let fixture = fixture(false, 0, false);
        let supervisor =
            SidecarSupervisor::new(fixture.state_root.clone(), fixture.manifest.clone()).unwrap();
        let python = fixture
            .view
            .pinned_launch_root()
            .unwrap()
            .join("runtime/python-arm64/python/bin/python3");
        let bin = python.parent().unwrap().to_path_buf();
        supervisor.set_before_spawn_hook(move || {
            fs::set_permissions(&bin, fs::Permissions::from_mode(0o700)).unwrap();
            fs::remove_file(&python).unwrap();
        });
        assert!(supervisor.start(&fixture.view).await.is_err());
        assert!(!supervisor.identity_path().exists());
        assert!(!supervisor.lock_held.load(Ordering::Acquire));

        let replacement =
            SidecarSupervisor::new(fixture.state_root.clone(), fixture.manifest.clone()).unwrap();
        replacement.acquire_process_lock().await.unwrap();
        replacement.release_process_lock().unwrap();
    }

    #[tokio::test]
    async fn next_supervisor_recovers_parent_exit_at_all_pre_ready_identity_phases() {
        let helper = r#"import fcntl,json,os,subprocess,sys,time
lock_path,payload_path,identity_path,info_path,mode=sys.argv[1:]
lock=open(lock_path,'r+')
fcntl.flock(lock,fcntl.LOCK_EX)
identity=json.load(open(payload_path))
def publish(value):
    tmp=identity_path+'.helper-tmp'
    with open(tmp,'w') as f:
        json.dump(value,f,separators=(',',':'))
        f.flush(); os.fsync(f.fileno())
    os.chmod(tmp,0o600)
    os.replace(tmp,identity_path)
publish(identity)
if mode == 'intent_only':
    os._exit(31)
root_code="""import json,os,signal,subprocess,sys
child=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)'],env=os.environ.copy(),start_new_session=True)
open(os.environ['YRG_OLD_INFO'],'w').write(json.dumps({'root':os.getpid(),'child':child.pid}))
os._exit(0)
"""
env=os.environ.copy(); env['YIRENGONGIS_PROCESS_OWNER_ID']=identity['instance_id']; env['YRG_OLD_INFO']=info_path
root=subprocess.Popen([sys.executable,'-c',root_code],env=env,start_new_session=True)
for _ in range(300):
    if os.path.exists(info_path): break
    time.sleep(.01)
if mode == 'spawned':
    identity.update({'phase':'spawned','pid':root.pid,'pgid':root.pid,'started_at_secs':int(time.time())})
    publish(identity)
os._exit(32)
"#;
        for mode in ["intent_only", "before_pid_update", "spawned"] {
            let fixture = fixture(false, 0, false);
            let supervisor =
                SidecarSupervisor::new(fixture.state_root.clone(), fixture.manifest.clone())
                    .unwrap();
            let pinned = fixture.view.pinned_launch_root().unwrap();
            let identity = SidecarIdentity {
                schema_version: 1,
                phase: SidecarPhase::Launching,
                pid: 0,
                pgid: 0,
                port: 0,
                python: pinned.join("runtime/python-arm64/python/bin/python3"),
                script: pinned.join("scripts/_run.py"),
                package_id: PACKAGE_ID.into(),
                build_version: BUILD_VERSION.into(),
                started_at_secs: 0,
                launched_at_secs: unix_time_secs().unwrap(),
                owner_uid: current_process_uid().unwrap(),
                instance_id: random_secret(),
            };
            let payload_path = fixture
                .state_root
                .join(format!("helper-intent-{mode}.json"));
            let info_path = fixture
                .state_root
                .join(format!("helper-processes-{mode}.json"));
            fs::write(&payload_path, serde_json::to_vec(&identity).unwrap()).unwrap();
            let mut parent = std::process::Command::new(test_python())
                .args([
                    "-c",
                    helper,
                    supervisor
                        .state_root
                        .join("runtimes/.sidecar.lock")
                        .to_str()
                        .unwrap(),
                    payload_path.to_str().unwrap(),
                    supervisor.identity_path().to_str().unwrap(),
                    info_path.to_str().unwrap(),
                    mode,
                ])
                .spawn()
                .unwrap();
            assert!(!parent.wait().unwrap().success(), "mode={mode}");
            assert!(supervisor.identity_path().exists(), "mode={mode}");
            let old_child = if mode == "intent_only" {
                None
            } else {
                let info: Value = serde_json::from_slice(&fs::read(&info_path).unwrap()).unwrap();
                let child = info["child"].as_u64().unwrap() as u32;
                assert!(pid_alive(child), "mode={mode}");
                Some(child)
            };

            supervisor.start(&fixture.view).await.unwrap();
            if let Some(child) = old_child {
                assert!(!pid_alive(child), "mode={mode}");
            }
            supervisor.stop().await.unwrap();
        }
    }

    #[tokio::test]
    async fn verified_generation_stays_pinned_when_current_switches_before_spawn() {
        let fixture = fixture_with_options(false, 0, false, false, false);
        let pinned_root = fixture.view.pinned_launch_root().unwrap();
        let current_root = fixture.view.path().to_path_buf();
        let manager = fixture.manager.clone();
        let core = fixture.core.clone();
        let collector = fixture.collector.clone();
        let supervisor =
            SidecarSupervisor::new(fixture.state_root.clone(), fixture.manifest.clone()).unwrap();
        supervisor.set_before_spawn_hook(move || {
            manager.activate_collector(&core, &collector).unwrap();
        });

        supervisor.start(&fixture.view).await.unwrap();
        let audit: Value = serde_json::from_slice(
            &fs::read(fixture.state_root.join("downloads/sidecar-audit.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(
            audit["argv"][0],
            pinned_root
                .join("scripts/_run.py")
                .to_string_lossy()
                .as_ref()
        );
        assert_eq!(audit["cwd"], pinned_root.to_string_lossy().as_ref());
        assert_eq!(audit["base_dir"], current_root.to_string_lossy().as_ref());
        assert_eq!(
            audit["node_bin"],
            current_root.join(NODE_RELATIVE).to_string_lossy().as_ref()
        );
        assert_eq!(audit["node_exists"], true);
        assert!(audit["executable"]
            .as_str()
            .unwrap()
            .starts_with(pinned_root.to_str().unwrap()));
        supervisor.stop().await.unwrap();
    }

    #[tokio::test]
    async fn health_mismatch_cleans_child_identity_and_tasks() {
        let fixture = fixture(true, 0, false);
        let supervisor =
            SidecarSupervisor::new(fixture.state_root.clone(), fixture.manifest.clone()).unwrap();
        assert_eq!(
            supervisor.start(&fixture.view).await.unwrap_err(),
            "sidecar_health_identity_mismatch"
        );
        let audit: Value = serde_json::from_slice(
            &fs::read(fixture.state_root.join("downloads/sidecar-audit.json")).unwrap(),
        )
        .unwrap();
        let pid = audit["pid"].as_u64().unwrap() as u32;
        assert!(!pid_alive(pid));
        assert!(!fixture.state_root.join("runtimes/sidecar.json").exists());
        assert!(supervisor.connection().await.is_none());
    }

    #[tokio::test]
    async fn repeated_start_stop_and_health_failures_leave_no_owned_state() {
        let success = fixture(false, 0, false);
        let success_supervisor =
            SidecarSupervisor::new(success.state_root.clone(), success.manifest.clone()).unwrap();
        for _ in 0..3 {
            success_supervisor.start(&success.view).await.unwrap();
            assert!(success_supervisor.connection().await.is_some());
            success_supervisor.stop().await.unwrap();
            assert!(success_supervisor.connection().await.is_none());
            assert!(!success.state_root.join("runtimes/sidecar.json").exists());
        }

        let failure = fixture(true, 0, false);
        let failure_supervisor =
            SidecarSupervisor::new(failure.state_root.clone(), failure.manifest.clone()).unwrap();
        for _ in 0..2 {
            assert_eq!(
                failure_supervisor.start(&failure.view).await.unwrap_err(),
                "sidecar_health_identity_mismatch"
            );
            let audit: Value = serde_json::from_slice(
                &fs::read(failure.state_root.join("downloads/sidecar-audit.json")).unwrap(),
            )
            .unwrap();
            assert!(!pid_alive(audit["pid"].as_u64().unwrap() as u32));
            assert!(failure_supervisor.connection().await.is_none());
            assert!(!failure.state_root.join("runtimes/sidecar.json").exists());
        }
    }

    #[tokio::test]
    async fn stop_kills_two_levels_of_setsid_descendants() {
        let fixture = fixture(false, 0, true);
        let supervisor =
            SidecarSupervisor::new(fixture.state_root.clone(), fixture.manifest.clone()).unwrap();
        supervisor.start(&fixture.view).await.unwrap();
        let info: Value = serde_json::from_slice(
            &fs::read(fixture.state_root.join("downloads/child-info.json")).unwrap(),
        )
        .unwrap();
        let child = info["child"].as_u64().unwrap() as u32;
        let grandchild = info["grandchild"].as_u64().unwrap() as u32;
        assert!(pid_alive(child));
        assert!(pid_alive(grandchild));
        supervisor.stop().await.unwrap();
        assert!(!pid_alive(child));
        assert!(!pid_alive(grandchild));
    }

    #[tokio::test]
    async fn real_bash_to_node_inherits_exact_owner_marker_and_other_marker_survives() {
        let temp = tempfile::tempdir().unwrap();
        let node_output = std::process::Command::new("/usr/bin/which")
            .arg("node")
            .output()
            .unwrap();
        assert!(node_output.status.success());
        let node = String::from_utf8(node_output.stdout)
            .unwrap()
            .trim()
            .to_string();
        let script = r#""$YRG_NODE" -e 'setInterval(() => {}, 1000)' &
echo $! > "$YRG_INFO"
wait
"#;
        let launch = unix_time_secs().unwrap();
        let uid = current_process_uid().unwrap();
        let first_marker = random_secret();
        let second_marker = random_secret();
        let first_info = temp.path().join("first-node.pid");
        let second_info = temp.path().join("second-node.pid");
        let spawn = |marker: &str, info: &Path| {
            std::process::Command::new("/bin/bash")
                .args(["-c", script])
                .env(OWNER_MARKER_ENV, marker)
                .env("YRG_NODE", &node)
                .env("YRG_INFO", info)
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .process_group(0)
                .spawn()
                .unwrap()
        };
        let mut first = spawn(&first_marker, &first_info);
        let mut second = spawn(&second_marker, &second_info);
        for _ in 0..200 {
            if first_info.exists() && second_info.exists() {
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        let first_node: u32 = fs::read_to_string(&first_info)
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        let second_node: u32 = fs::read_to_string(&second_info)
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        let identity = |marker: &str| SidecarIdentity {
            schema_version: 1,
            phase: SidecarPhase::Launching,
            pid: 0,
            pgid: 0,
            port: 0,
            python: PathBuf::from("/bin/bash"),
            script: PathBuf::from("/tmp/owner-probe"),
            package_id: PACKAGE_ID.into(),
            build_version: BUILD_VERSION.into(),
            started_at_secs: 0,
            launched_at_secs: launch,
            owner_uid: uid,
            instance_id: marker.into(),
        };
        let first_identity = identity(&first_marker);
        let first_snapshot = owner_process_snapshot(&first_identity);
        let first_node_visible = first_snapshot.contains_key(&first_node);
        let second_node_excluded = !first_snapshot.contains_key(&second_node);

        terminate_owner_processes(&first_identity).await.unwrap();
        let _ = first.wait();
        let first_node_dead = !pid_alive(first_node);
        let second_bash_alive = pid_alive(second.id());
        let second_node_alive = pid_alive(second_node);

        terminate_owner_processes(&identity(&second_marker))
            .await
            .unwrap();
        let _ = second.wait();
        assert!(first_node_visible, "snapshot={first_snapshot:?}");
        assert!(second_node_excluded, "snapshot={first_snapshot:?}");
        assert!(first_node_dead);
        assert!(second_bash_alive);
        assert!(second_node_alive);
        assert!(!pid_alive(second_node));
    }

    #[tokio::test]
    async fn cleanup_rescan_catches_marked_setsid_child_spawned_after_root_term() {
        let temp = tempfile::tempdir().unwrap();
        let script = temp.path().join("spawn-after-term.py");
        let child_info = temp.path().join("spawned-child.pid");
        let root_ready = temp.path().join("root-ready");
        fs::write(
            &script,
            r#"import os, signal, subprocess, sys, time
def on_term(_signum, _frame):
    child = subprocess.Popen(
        [sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)'],
        env=os.environ.copy(),
        start_new_session=True,
    )
    open(os.environ['YRG_CHILD_INFO'], 'w').write(str(child.pid))
signal.signal(signal.SIGTERM, on_term)
open(os.environ['YRG_ROOT_READY'], 'w').write('1')
while True:
    time.sleep(1)
"#,
        )
        .unwrap();
        let marker = random_secret();
        let launch = unix_time_secs().unwrap();
        let python = test_python();
        let arguments = sidecar_arguments(&script, &marker, PACKAGE_ID, BUILD_VERSION).unwrap();
        let mut root = std::process::Command::new(&python)
            .args(&arguments)
            .env(OWNER_MARKER_ENV, &marker)
            .env("YRG_CHILD_INFO", &child_info)
            .env("YRG_ROOT_READY", &root_ready)
            .process_group(0)
            .spawn()
            .unwrap();
        let pid = root.id();
        let started = SidecarSupervisor::process_start_time(pid).await.unwrap();
        for _ in 0..200 {
            if root_ready.exists() {
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(root_ready.exists());
        let identity = SidecarIdentity {
            schema_version: 1,
            phase: SidecarPhase::Ready,
            pid,
            pgid: pid,
            port: 8811,
            python,
            script,
            package_id: PACKAGE_ID.into(),
            build_version: BUILD_VERSION.into(),
            started_at_secs: started,
            launched_at_secs: launch,
            owner_uid: current_process_uid().unwrap(),
            instance_id: marker,
        };
        terminate_owner_processes(&identity).await.unwrap();
        let _ = root.wait();
        assert!(child_info.exists());
        let child: u32 = fs::read_to_string(child_info)
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        assert!(!pid_alive(child));
    }

    #[tokio::test]
    async fn crash_watcher_kills_detached_descendants_and_allows_restart() {
        let fixture = fixture_with_options(false, 0, true, true, true);
        let supervisor =
            SidecarSupervisor::new(fixture.state_root.clone(), fixture.manifest.clone()).unwrap();
        supervisor.start(&fixture.view).await.unwrap();
        let info: Value = serde_json::from_slice(
            &fs::read(fixture.state_root.join("downloads/child-info.json")).unwrap(),
        )
        .unwrap();
        let child = info["child"].as_u64().unwrap() as u32;
        let grandchild = info["grandchild"].as_u64().unwrap() as u32;

        for _ in 0..150 {
            if supervisor.connection().await.is_none()
                && !pid_alive(child)
                && !pid_alive(grandchild)
            {
                break;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        assert!(supervisor.connection().await.is_none());
        assert!(fixture.state_root.join("runtimes/sidecar.json").exists());
        assert!(!pid_alive(child));
        assert!(!pid_alive(grandchild));

        supervisor.start(&fixture.view).await.unwrap();
        assert!(supervisor.connection().await.is_some());
        supervisor.stop().await.unwrap();
    }

    #[tokio::test]
    async fn crashed_owner_keeps_lock_and_late_stop_cannot_unlink_new_owner_identity() {
        let fixture = fixture_with_options(false, 0, true, true, true);
        let first =
            SidecarSupervisor::new(fixture.state_root.clone(), fixture.manifest.clone()).unwrap();
        let second =
            SidecarSupervisor::new(fixture.state_root.clone(), fixture.manifest.clone()).unwrap();
        first.start(&fixture.view).await.unwrap();
        let info: Value = serde_json::from_slice(
            &fs::read(fixture.state_root.join("downloads/child-info.json")).unwrap(),
        )
        .unwrap();
        let child = info["child"].as_u64().unwrap() as u32;
        let grandchild = info["grandchild"].as_u64().unwrap() as u32;
        for _ in 0..150 {
            if first.connection().await.is_none() && !pid_alive(child) && !pid_alive(grandchild) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        assert!(first.connection().await.is_none());
        assert!(first.lock_held.load(Ordering::Acquire));
        assert!(first.identity_path().exists());

        let starting_second = {
            let second = second.clone();
            let view = fixture.view.clone();
            tokio::spawn(async move { second.start(&view).await })
        };
        tokio::time::sleep(Duration::from_millis(200)).await;
        assert!(!starting_second.is_finished());
        first.stop().await.unwrap();
        starting_second.await.unwrap().unwrap();
        let second_identity = fs::read(second.identity_path()).unwrap();

        first.stop().await.unwrap();
        assert_eq!(fs::read(second.identity_path()).unwrap(), second_identity);
        assert!(second.connection().await.is_some());
        second.stop().await.unwrap();
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn stop_during_crash_cleanup_keeps_the_shared_descendant_snapshot() {
        let fixture = fixture_with_options(false, 0, true, true, true);
        let supervisor =
            SidecarSupervisor::new(fixture.state_root.clone(), fixture.manifest.clone()).unwrap();
        let (entered_tx, entered_rx) = std::sync::mpsc::sync_channel(1);
        let (release_tx, release_rx) = std::sync::mpsc::sync_channel(1);
        supervisor.set_watcher_before_cleanup_hook(move || {
            entered_tx.send(()).unwrap();
            release_rx.recv().unwrap();
        });
        supervisor.start(&fixture.view).await.unwrap();
        let info: Value = serde_json::from_slice(
            &fs::read(fixture.state_root.join("downloads/child-info.json")).unwrap(),
        )
        .unwrap();
        let child = info["child"].as_u64().unwrap() as u32;
        let grandchild = info["grandchild"].as_u64().unwrap() as u32;
        tokio::task::spawn_blocking(move || entered_rx.recv_timeout(Duration::from_secs(3)))
            .await
            .unwrap()
            .unwrap();

        let stopping = {
            let supervisor = supervisor.clone();
            tokio::spawn(async move { supervisor.stop().await })
        };
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert!(!stopping.is_finished());
        release_tx.send(()).unwrap();
        stopping.await.unwrap().unwrap();

        assert!(!pid_alive(child));
        assert!(!pid_alive(grandchild));
        assert!(supervisor.connection().await.is_none());
        assert!(!fixture.state_root.join("runtimes/sidecar.json").exists());
    }

    #[tokio::test]
    async fn exact_cross_build_identity_is_taken_over_using_its_own_argv() {
        let fixture = fixture(false, 0, false);
        let supervisor =
            SidecarSupervisor::new(fixture.state_root.clone(), fixture.manifest.clone()).unwrap();
        let old_root = fixture.state_root.join("old-build");
        fs::create_dir(&old_root).unwrap();
        let old_python = old_root.join("python3");
        let old_script = old_root.join("_run.py");
        symlink(test_python(), &old_python).unwrap();
        fs::write(&old_script, "import time\ntime.sleep(300)\n").unwrap();
        let instance = "old-instance-0000000000000001";
        let old_build = "20260710";
        let arguments = sidecar_arguments(&old_script, instance, PACKAGE_ID, old_build).unwrap();
        let mut old = std::process::Command::new(&old_python)
            .args(&arguments)
            .env(OWNER_MARKER_ENV, instance)
            .process_group(0)
            .spawn()
            .unwrap();
        let old_pid = old.id();
        let started_at = SidecarSupervisor::process_start_time(old_pid)
            .await
            .unwrap();
        let mut identity = SidecarIdentity::fixture_for_test(
            old_pid,
            old_python.to_str().unwrap(),
            old_script.to_str().unwrap(),
            instance,
            PACKAGE_ID,
            old_build,
        );
        identity.started_at_secs = started_at;
        supervisor.write_identity(&identity).unwrap();

        supervisor.start(&fixture.view).await.unwrap();
        let terminated = old.try_wait().unwrap().is_some();
        if !terminated {
            let _ = old.kill();
            let _ = old.wait();
        }
        assert!(terminated, "exact old-build process was not taken over");
        supervisor.stop().await.unwrap();
    }

    #[tokio::test]
    async fn root_with_same_marker_but_wrong_full_argv_is_never_signalled() {
        let marker = random_secret();
        let launch = unix_time_secs().unwrap();
        let python = test_python();
        let mut sentinel = std::process::Command::new(&python)
            .args(["-c", "import time; time.sleep(300)"])
            .env(OWNER_MARKER_ENV, &marker)
            .process_group(0)
            .spawn()
            .unwrap();
        let pid = sentinel.id();
        let started = SidecarSupervisor::process_start_time(pid).await.unwrap();
        let identity = SidecarIdentity {
            schema_version: 1,
            phase: SidecarPhase::Ready,
            pid,
            pgid: pid,
            port: 8811,
            python,
            script: PathBuf::from("/tmp/expected-sidecar/_run.py"),
            package_id: PACKAGE_ID.into(),
            build_version: BUILD_VERSION.into(),
            started_at_secs: started,
            launched_at_secs: launch,
            owner_uid: current_process_uid().unwrap(),
            instance_id: marker,
        };
        terminate_owner_processes(&identity).await.unwrap();
        let survived = sentinel.try_wait().unwrap().is_none();
        let _ = sentinel.kill();
        let _ = sentinel.wait();
        assert!(survived);
    }

    #[tokio::test]
    async fn mismatched_and_malformed_identity_never_target_current_process() {
        let fixture = fixture(false, 0, false);
        let supervisor =
            SidecarSupervisor::new(fixture.state_root.clone(), fixture.manifest.clone()).unwrap();
        supervisor.acquire_process_lock().await.unwrap();
        let identity = SidecarIdentity::fixture_for_test(
            std::process::id(),
            "/different/python",
            "/different/_run.py",
            "different-instance",
            PACKAGE_ID,
            BUILD_VERSION,
        );
        supervisor.write_identity(&identity).unwrap();
        supervisor.cleanup_owned_process().await.unwrap();
        assert!(pid_alive(std::process::id()));
        fs::write(supervisor.identity_path(), b"{not-json").unwrap();
        fs::set_permissions(
            supervisor.identity_path(),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        supervisor.cleanup_owned_process().await.unwrap();
        assert!(!supervisor.identity_path().exists());
        assert!(pid_alive(std::process::id()));
        supervisor.release_process_lock().unwrap();
    }

    #[test]
    fn identity_write_is_atomic_private_and_has_no_temporary_file() {
        let fixture = fixture(false, 0, false);
        let supervisor =
            SidecarSupervisor::new(fixture.state_root.clone(), fixture.manifest.clone()).unwrap();
        let identity = SidecarIdentity::fixture_for_test(
            42,
            "/runtime/python3",
            "/view/scripts/_run.py",
            "instance-00000001",
            PACKAGE_ID,
            BUILD_VERSION,
        );
        supervisor.write_identity(&identity).unwrap();
        let metadata = fs::symlink_metadata(supervisor.identity_path()).unwrap();
        assert_eq!(metadata.permissions().mode() & 0o777, 0o600);
        assert_eq!(metadata.nlink(), 1);
        assert!(!fs::read_dir(fixture.state_root.join("runtimes"))
            .unwrap()
            .flatten()
            .any(|entry| entry.file_name().to_string_lossy().contains(".tmp-")));
    }

    #[test]
    fn identity_attacks_fail_closed_and_atomic_write_never_touches_external_target() {
        for case in ["symlink", "hardlink", "fifo", "oversized"] {
            let fixture = fixture(false, 0, false);
            let supervisor =
                SidecarSupervisor::new(fixture.state_root.clone(), fixture.manifest.clone())
                    .unwrap();
            let identity_path = supervisor.identity_path();
            let external = fixture.state_root.join(format!("external-{case}"));
            fs::write(&external, b"sentinel").unwrap();
            fs::set_permissions(&external, fs::Permissions::from_mode(0o600)).unwrap();
            match case {
                "symlink" => symlink(&external, &identity_path).unwrap(),
                "hardlink" => fs::hard_link(&external, &identity_path).unwrap(),
                "fifo" => {
                    assert!(std::process::Command::new("/usr/bin/mkfifo")
                        .arg(&identity_path)
                        .status()
                        .unwrap()
                        .success());
                }
                "oversized" => {
                    let file = fs::File::create(&identity_path).unwrap();
                    file.set_len((IDENTITY_MAX_BYTES + 1) as u64).unwrap();
                    fs::set_permissions(&identity_path, fs::Permissions::from_mode(0o600)).unwrap();
                }
                _ => unreachable!(),
            }
            assert!(supervisor.read_identity().is_err(), "case={case}");
            let identity = SidecarIdentity::fixture_for_test(
                42,
                "/runtime/python3",
                "/view/scripts/_run.py",
                "instance-00000001",
                PACKAGE_ID,
                BUILD_VERSION,
            );
            supervisor.write_identity(&identity).unwrap();
            assert_eq!(fs::read(&external).unwrap(), b"sentinel", "case={case}");
            assert_eq!(supervisor.read_identity().unwrap(), Some(identity));
        }
    }

    #[test]
    fn runner_log_attacks_fail_closed_without_chmod_or_write_to_external_targets() {
        for case in ["symlink", "hardlink", "fifo"] {
            let fixture = fixture(false, 0, false);
            let supervisor =
                SidecarSupervisor::new(fixture.state_root.clone(), fixture.manifest.clone())
                    .unwrap();
            let log_path = fixture.state_root.join("downloads/runner_process.log");
            let external = fixture.state_root.join(format!("external-log-{case}"));
            fs::write(&external, b"sentinel").unwrap();
            fs::set_permissions(&external, fs::Permissions::from_mode(0o640)).unwrap();
            match case {
                "symlink" => symlink(&external, &log_path).unwrap(),
                "hardlink" => fs::hard_link(&external, &log_path).unwrap(),
                "fifo" => {
                    assert!(std::process::Command::new("/usr/bin/mkfifo")
                        .arg(&log_path)
                        .status()
                        .unwrap()
                        .success());
                }
                _ => unreachable!(),
            }
            assert!(supervisor.open_runner_log().is_err(), "case={case}");
            assert_eq!(fs::read(&external).unwrap(), b"sentinel", "case={case}");
            assert_eq!(
                fs::symlink_metadata(&external)
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o640,
                "case={case}"
            );
        }
    }

    #[tokio::test]
    async fn second_process_lock_blocks_until_external_owner_releases() {
        let fixture = fixture(false, 0, false);
        let supervisor =
            SidecarSupervisor::new(fixture.state_root.clone(), fixture.manifest.clone()).unwrap();
        let lock_path = fixture.state_root.join("runtimes/.sidecar.lock");
        let acquired = fixture.state_root.join("downloads/external-lock-acquired");
        let release = fixture.state_root.join("downloads/external-lock-release");
        let script = r#"import fcntl,os,sys,time
f=open(sys.argv[1],'r+'); fcntl.flock(f,fcntl.LOCK_EX); open(sys.argv[2],'w').write('ok')
while not os.path.exists(sys.argv[3]): time.sleep(.01)
"#;
        let mut external = std::process::Command::new(test_python())
            .args([
                "-c",
                script,
                lock_path.to_str().unwrap(),
                acquired.to_str().unwrap(),
                release.to_str().unwrap(),
            ])
            .spawn()
            .unwrap();
        for _ in 0..200 {
            if acquired.exists() {
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(acquired.exists());
        let starting = {
            let supervisor = supervisor.clone();
            let view = fixture.view.clone();
            tokio::spawn(async move { supervisor.start(&view).await })
        };
        tokio::time::sleep(Duration::from_millis(200)).await;
        assert!(!starting.is_finished());
        fs::write(&release, b"release").unwrap();
        assert!(external.wait().unwrap().success());
        starting.await.unwrap().unwrap();
        supervisor.stop().await.unwrap();
    }
}
