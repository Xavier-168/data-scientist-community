use std::{
    cmp::Ordering,
    ffi::{CStr, CString, OsStr},
    fs::{self, File},
    io::{self, Read, Write},
    os::fd::{AsRawFd, RawFd},
    os::unix::fs::PermissionsExt,
    os::unix::process::CommandExt,
    path::{Component, Path, PathBuf},
    process::Command,
    sync::{
        atomic::{AtomicBool, Ordering as AtomicOrdering},
        Arc, Condvar, Mutex as StdMutex,
    },
    thread,
    time::{Duration, Instant},
};

use crate::{fault_injection::FaultInjection, manifest::VerifiedPackageManifest};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

const APP_NAME: &str = "数据科学家 Community.app";
const MANIFEST_RELATIVE: &str = "Contents/Resources/package_manifest.json";
const BINARY_RELATIVE: &str = "Contents/MacOS/data-scientist";
const MANIFEST_MAX_BYTES: u64 = 2 * 1024 * 1024;
const BINARY_MAX_BYTES: u64 = 512 * 1024 * 1024;
const LOCK_NAME: &str = ".数据科学家 Community.app.install.lock";
const JOURNAL_NAME: &str = ".数据科学家 Community.app.install.json";
const JOURNAL_MAX_BYTES: usize = 64 * 1024;
const STAGING_PREFIX: &str = ".数据科学家 Community.app.installing-";
const BACKUP_PREFIX: &str = ".数据科学家 Community.app.previous-";

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum InstallTarget {
    AlreadyInstalled,
    CopyTo(PathBuf),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum InstallOutcome {
    AlreadyInstalled,
    Installed(PathBuf),
    Failed(String),
}

#[derive(Clone, Default)]
pub struct InstallCancellation(Arc<AtomicBool>);

impl InstallCancellation {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn cancel(&self) {
        self.0.store(true, AtomicOrdering::Release);
    }

    pub fn reset(&self) {
        self.0.store(false, AtomicOrdering::Release);
    }

    pub fn is_cancelled(&self) -> bool {
        self.0.load(AtomicOrdering::Acquire)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InstallCheckpoint {
    Copied,
    BackupRenamedBeforeJournal,
    BackupMoved,
    TargetRenamedBeforeJournal,
    TargetSwitched,
    Committed,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
enum TransactionPhase {
    Prepared,
    BackupMoved,
    TargetSwitched,
    Committed,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct InstallJournal {
    schema_version: u8,
    phase: TransactionPhase,
    transaction_id: String,
    staging_name: String,
    backup_name: String,
    had_previous: bool,
    package_id: String,
    build_version: String,
    #[serde(skip)]
    previous_snapshot: Option<BundleSnapshot>,
}

impl InstallJournal {
    fn new(manifest: &VerifiedPackageManifest, had_previous: bool) -> Self {
        let transaction_id = uuid::Uuid::new_v4().to_string();
        Self {
            schema_version: 1,
            phase: TransactionPhase::Prepared,
            staging_name: format!("{STAGING_PREFIX}{transaction_id}"),
            backup_name: format!("{BACKUP_PREFIX}{transaction_id}"),
            transaction_id,
            had_previous,
            package_id: manifest.manifest().package_id.clone(),
            build_version: manifest.manifest().build_version.clone(),
            previous_snapshot: None,
        }
    }

    fn validate(&self) -> Result<(), String> {
        let transaction = uuid::Uuid::parse_str(&self.transaction_id)
            .map_err(|_| "install_journal_invalid".to_string())?;
        if self.schema_version != 1
            || transaction.to_string() != self.transaction_id
            || self.staging_name != format!("{STAGING_PREFIX}{}", self.transaction_id)
            || self.backup_name != format!("{BACKUP_PREFIX}{}", self.transaction_id)
            || self.package_id.is_empty()
            || parse_build_version(&self.build_version).is_err()
        {
            return Err("install_journal_invalid".into());
        }
        Ok(())
    }
}

struct ApplicationsDirectory {
    path: PathBuf,
    descriptor: File,
}

struct InstallFileLock(File);

impl Drop for InstallFileLock {
    fn drop(&mut self) {
        let _ = FileExt::unlock(&self.0);
    }
}

pub trait CopyBackend: Send + Sync {
    fn notify_install_start(
        &self,
        _target: &Path,
        _cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        Ok(())
    }

    fn copy_app(
        &self,
        source: &Path,
        target: &Path,
        cancellation: &InstallCancellation,
    ) -> Result<(), String>;

    fn copy_app_anchored(
        &self,
        source: &Path,
        target: &AnchoredPath<'_>,
        cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        self.copy_app(source, &target.display_path, cancellation)
    }

    fn clear_quarantine(
        &self,
        target: &Path,
        cancellation: &InstallCancellation,
    ) -> Result<(), String>;

    fn clear_quarantine_anchored(
        &self,
        target: &AnchoredPath<'_>,
        cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        self.clear_quarantine(&target.display_path, cancellation)
    }

    fn verify_code_signature(
        &self,
        target: &Path,
        cancellation: &InstallCancellation,
    ) -> Result<(), String>;

    fn verify_code_signature_anchored(
        &self,
        target: &AnchoredPath<'_>,
        cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        self.verify_code_signature(&target.display_path, cancellation)
    }

    fn register_app(&self, target: &Path, cancellation: &InstallCancellation)
        -> Result<(), String>;

    fn register_app_anchored(
        &self,
        target: &AnchoredPath<'_>,
        cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        self.register_app(&target.display_path, cancellation)
    }

    fn notify_install_failure(
        &self,
        _target: &Path,
        _cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        Ok(())
    }

    fn checkpoint(
        &self,
        _checkpoint: InstallCheckpoint,
        _cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        Ok(())
    }
}

#[derive(Default)]
pub struct MacCopyBackend;

impl MacCopyBackend {
    fn run(
        binary: &str,
        arguments: &[&OsStr],
        cancellation: &InstallCancellation,
        error_code: &str,
    ) -> Result<(), String> {
        let mut command = Command::new(binary);
        command.args(arguments);
        match run_cancellable_command(&mut command, cancellation) {
            Ok(()) => Ok(()),
            Err(error) if error == "install_cancelled" => Err(error),
            Err(_) => Err(error_code.into()),
        }
    }

    fn run_anchored(
        binary: &str,
        arguments: &[&OsStr],
        parent: RawFd,
        cancellation: &InstallCancellation,
        error_code: &str,
    ) -> Result<(), String> {
        let mut command = Command::new(binary);
        command.args(arguments);
        unsafe {
            command.pre_exec(move || {
                if nix::libc::fchdir(parent) == 0 {
                    Ok(())
                } else {
                    Err(io::Error::last_os_error())
                }
            });
        }
        match run_cancellable_command(&mut command, cancellation) {
            Ok(()) => Ok(()),
            Err(error) if error == "install_cancelled" => Err(error),
            Err(_) => Err(error_code.into()),
        }
    }
}

impl CopyBackend for MacCopyBackend {
    fn notify_install_start(
        &self,
        _target: &Path,
        cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        Self::run(
            "/usr/bin/osascript",
            &[
                OsStr::new("-e"),
                OsStr::new(
                    "display dialog \"数据科学家正在安装到应用程序文件夹，完成后可直接使用。\" buttons {\"好\"} default button 1 giving up after 2 with title \"数据科学家\"",
                ),
            ],
            cancellation,
            "install_notice_failed",
        )
    }

    fn copy_app(
        &self,
        source: &Path,
        target: &Path,
        cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        Self::run(
            "/usr/bin/ditto",
            &[source.as_os_str(), target.as_os_str()],
            cancellation,
            "ditto_failed",
        )
    }

    fn copy_app_anchored(
        &self,
        source: &Path,
        target: &AnchoredPath<'_>,
        cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        Self::run_anchored(
            "/usr/bin/ditto",
            &[source.as_os_str(), OsStr::new(target.entry)],
            target.parent.as_raw_fd(),
            cancellation,
            "ditto_failed",
        )
    }

    fn clear_quarantine(
        &self,
        target: &Path,
        cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        Self::run(
            "/usr/bin/xattr",
            &[OsStr::new("-cr"), target.as_os_str()],
            cancellation,
            "xattr_clear_failed",
        )
    }

    fn clear_quarantine_anchored(
        &self,
        target: &AnchoredPath<'_>,
        cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        Self::run_anchored(
            "/usr/bin/xattr",
            &[OsStr::new("-cr"), OsStr::new(target.entry)],
            target.parent.as_raw_fd(),
            cancellation,
            "xattr_clear_failed",
        )
    }

    fn verify_code_signature(
        &self,
        target: &Path,
        cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        Self::run(
            "/usr/bin/codesign",
            &[
                OsStr::new("--verify"),
                OsStr::new("--deep"),
                OsStr::new("--strict"),
                target.as_os_str(),
            ],
            cancellation,
            "codesign_verify_failed",
        )
    }

    fn verify_code_signature_anchored(
        &self,
        target: &AnchoredPath<'_>,
        cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        Self::run_anchored(
            "/usr/bin/codesign",
            &[
                OsStr::new("--verify"),
                OsStr::new("--deep"),
                OsStr::new("--strict"),
                OsStr::new(target.entry),
            ],
            target.parent.as_raw_fd(),
            cancellation,
            "codesign_verify_failed",
        )
    }

    fn register_app(
        &self,
        target: &Path,
        cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        Self::run(
            "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister",
            &[OsStr::new("-f"), target.as_os_str()],
            cancellation,
            "launchservices_registration_failed",
        )
    }

    fn register_app_anchored(
        &self,
        target: &AnchoredPath<'_>,
        cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        Self::run_anchored(
            "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister",
            &[OsStr::new("-f"), OsStr::new(target.entry)],
            target.parent.as_raw_fd(),
            cancellation,
            "launchservices_registration_failed",
        )
    }

    fn notify_install_failure(
        &self,
        _target: &Path,
        cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        Self::run(
            "/usr/bin/osascript",
            &[
                OsStr::new("-e"),
                OsStr::new(
                    "display alert \"数据科学家安装失败\" message \"请检查磁盘空间，或手动将应用拖入应用程序文件夹。\" as critical giving up after 20",
                ),
            ],
            cancellation,
            "install_failure_notice_failed",
        )
    }
}

fn run_cancellable_command(
    command: &mut Command,
    cancellation: &InstallCancellation,
) -> Result<(), String> {
    command.process_group(0);
    let mut child = command
        .spawn()
        .map_err(|error| format!("install_command_spawn_failed:{error}"))?;
    let process_group = nix::unistd::Pid::from_raw(child.id() as i32);
    loop {
        if let Some(status) = child
            .try_wait()
            .map_err(|error| format!("install_command_wait_failed:{error}"))?
        {
            return status
                .success()
                .then_some(())
                .ok_or_else(|| "install_command_failed".to_string());
        }
        if cancellation.is_cancelled() {
            let _ = nix::sys::signal::killpg(process_group, nix::sys::signal::Signal::SIGTERM);
            let deadline = Instant::now() + Duration::from_millis(250);
            while Instant::now() < deadline {
                if child
                    .try_wait()
                    .map_err(|error| format!("install_command_wait_failed:{error}"))?
                    .is_some()
                {
                    return Err("install_cancelled".into());
                }
                thread::sleep(Duration::from_millis(10));
            }
            let _ = nix::sys::signal::killpg(process_group, nix::sys::signal::Signal::SIGKILL);
            child
                .wait()
                .map_err(|error| format!("install_command_cancel_wait_failed:{error}"))?;
            return Err("install_cancelled".into());
        }
        thread::sleep(Duration::from_millis(25));
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct FileIdentity {
    device: u64,
    inode: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct BundleIdentity {
    root: FileIdentity,
    contents: FileIdentity,
    macos: FileIdentity,
    resources: FileIdentity,
    packs: FileIdentity,
}

struct OpenBundle {
    _root: File,
    macos: File,
    resources: File,
    packs: File,
    identity: BundleIdentity,
}

pub struct AnchoredPath<'a> {
    parent: &'a File,
    entry: &'a str,
    display_path: PathBuf,
}

impl<'a> AnchoredPath<'a> {
    fn new(parent: &'a File, parent_path: &Path, entry: &'a str) -> Result<Self, String> {
        if entry.is_empty() || entry == "." || entry == ".." || entry.contains('/') {
            return Err("install_entry_invalid".into());
        }
        Ok(Self {
            parent,
            entry,
            display_path: parent_path.join(entry),
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct BundleEvidence {
    identity: BundleIdentity,
    binary: (u64, String),
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct BundleSnapshot {
    signed_manifest: Vec<u8>,
    evidence: BundleEvidence,
}

fn file_identity(stat: &rustix::fs::Stat) -> FileIdentity {
    FileIdentity {
        device: stat.st_dev as u64,
        inode: stat.st_ino,
    }
}

fn open_bundle_directory_at(
    parent: &File,
    name: &str,
    label: &str,
) -> Result<(File, FileIdentity), String> {
    use rustix::fs::{fstat, openat, FileType, Mode, OFlags};

    let descriptor = openat(
        parent,
        name,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|_| format!("install_directory_invalid:{label}"))?;
    let file = File::from(descriptor);
    let stat = fstat(&file).map_err(|_| format!("install_directory_invalid:{label}"))?;
    if rustix::fs::FileType::from_raw_mode(stat.st_mode) != FileType::Directory {
        return Err(format!("install_directory_invalid:{label}"));
    }
    Ok((file, file_identity(&stat)))
}

fn open_bundle_from_root(root: File, root_identity: FileIdentity) -> Result<OpenBundle, String> {
    let (contents, contents_identity) = open_bundle_directory_at(&root, "Contents", "Contents")?;
    let (macos, macos_identity) = open_bundle_directory_at(&contents, "MacOS", "Contents/MacOS")?;
    let (resources, resources_identity) =
        open_bundle_directory_at(&contents, "Resources", "Contents/Resources")?;
    let (packs, packs_identity) = open_bundle_directory_at(
        &resources,
        "runtime-packs",
        "Contents/Resources/runtime-packs",
    )?;
    Ok(OpenBundle {
        _root: root,
        macos,
        resources,
        packs,
        identity: BundleIdentity {
            root: root_identity,
            contents: contents_identity,
            macos: macos_identity,
            resources: resources_identity,
            packs: packs_identity,
        },
    })
}

fn open_bundle(root: &Path) -> Result<OpenBundle, String> {
    use rustix::fs::{fstat, open, FileType, Mode, OFlags};

    let descriptor = open(
        root,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|_| "install_directory_invalid:".to_string())?;
    let root = File::from(descriptor);
    let root_stat = fstat(&root).map_err(|_| "install_directory_invalid:".to_string())?;
    if FileType::from_raw_mode(root_stat.st_mode) != FileType::Directory {
        return Err("install_directory_invalid:".into());
    }
    open_bundle_from_root(root, file_identity(&root_stat))
}

fn open_bundle_at(directory: &ApplicationsDirectory, name: &str) -> Result<OpenBundle, String> {
    let (root, identity) = open_bundle_directory_at(&directory.descriptor, name, name)?;
    open_bundle_from_root(root, identity)
}

fn open_regular_file_at(
    parent: &File,
    name: &str,
    maximum_bytes: u64,
    expected_bytes: Option<u64>,
    label: &str,
) -> Result<(File, u64), String> {
    use rustix::fs::{fstat, openat, FileType, Mode, OFlags};

    let descriptor = openat(
        parent,
        name,
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|_| format!("install_file_open_failed:{label}"))?;
    let stat = fstat(&descriptor).map_err(|_| format!("install_file_stat_failed:{label}"))?;
    if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile {
        return Err(format!("install_file_not_regular:{label}"));
    }
    let size =
        u64::try_from(stat.st_size).map_err(|_| format!("install_file_size_invalid:{label}"))?;
    if size > maximum_bytes || expected_bytes.is_some_and(|expected| size != expected) {
        return Err(format!("install_file_size_mismatch:{label}"));
    }
    Ok((File::from(descriptor), size))
}

fn read_regular_bytes(
    parent: &File,
    name: &str,
    maximum_bytes: u64,
    expected_bytes: Option<u64>,
    cancellation: &InstallCancellation,
    label: &str,
) -> Result<Vec<u8>, String> {
    let (mut file, size) =
        open_regular_file_at(parent, name, maximum_bytes, expected_bytes, label)?;
    let mut bytes = Vec::with_capacity(size as usize);
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        if cancellation.is_cancelled() {
            return Err("install_cancelled".into());
        }
        let count = file
            .read(&mut buffer)
            .map_err(|_| format!("install_file_read_failed:{label}"))?;
        if count == 0 {
            break;
        }
        bytes.extend_from_slice(&buffer[..count]);
        if bytes.len() as u64 > maximum_bytes {
            return Err(format!("install_file_size_mismatch:{label}"));
        }
    }
    if bytes.len() as u64 != size {
        return Err(format!("install_file_changed:{label}"));
    }
    Ok(bytes)
}

fn digest_regular_file(
    parent: &File,
    name: &str,
    maximum_bytes: u64,
    expected_bytes: Option<u64>,
    cancellation: &InstallCancellation,
    label: &str,
) -> Result<(u64, String), String> {
    let (mut file, size) =
        open_regular_file_at(parent, name, maximum_bytes, expected_bytes, label)?;
    let mut digest = Sha256::new();
    let mut consumed = 0_u64;
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        if cancellation.is_cancelled() {
            return Err("install_cancelled".into());
        }
        let count = file
            .read(&mut buffer)
            .map_err(|_| format!("install_file_read_failed:{label}"))?;
        if count == 0 {
            break;
        }
        consumed = consumed
            .checked_add(count as u64)
            .ok_or_else(|| format!("install_file_size_mismatch:{label}"))?;
        if consumed > maximum_bytes {
            return Err(format!("install_file_size_mismatch:{label}"));
        }
        digest.update(&buffer[..count]);
    }
    if consumed != size {
        return Err(format!("install_file_changed:{label}"));
    }
    Ok((size, hex::encode(digest.finalize())))
}

fn same_file_identity(left: &rustix::fs::Stat, right: &rustix::fs::Stat) -> bool {
    left.st_dev == right.st_dev && left.st_ino == right.st_ino
}

fn trusted_macos_alias(path: &Path) -> PathBuf {
    for (alias, destination) in [
        (Path::new("/var"), Path::new("/private/var")),
        (Path::new("/tmp"), Path::new("/private/tmp")),
    ] {
        if path.starts_with(alias)
            && fs::symlink_metadata(alias).is_ok_and(|metadata| metadata.file_type().is_symlink())
            && fs::canonicalize(alias).is_ok_and(|resolved| resolved == destination)
        {
            return destination.join(path.strip_prefix(alias).unwrap_or(path));
        }
    }
    path.to_path_buf()
}

fn reject_symlink_components(path: &Path) -> Result<(), String> {
    let mut current = PathBuf::new();
    for component in path.components() {
        match component {
            Component::RootDir | Component::Prefix(_) | Component::Normal(_) => {
                current.push(component.as_os_str());
            }
            Component::CurDir => continue,
            Component::ParentDir => return Err("install_home_invalid".into()),
        }
        if current == Path::new("/") {
            continue;
        }
        let metadata = fs::symlink_metadata(&current).map_err(|_| "install_home_invalid")?;
        if metadata.file_type().is_symlink() {
            return Err("install_home_invalid".into());
        }
    }
    Ok(())
}

fn applications_directory(home: &Path) -> Result<ApplicationsDirectory, String> {
    use rustix::fs::{fstat, open, FileType, Mode, OFlags};

    let home = trusted_macos_alias(home);
    reject_symlink_components(&home)?;
    let home_metadata = fs::symlink_metadata(&home).map_err(|_| "install_home_invalid")?;
    if !home_metadata.is_dir() || home_metadata.file_type().is_symlink() {
        return Err("install_home_invalid".into());
    }
    let canonical_home = fs::canonicalize(&home).map_err(|_| "install_home_invalid")?;
    let path = home.join("Applications");
    match fs::create_dir(&path) {
        Ok(()) => {
            fs::set_permissions(&path, fs::Permissions::from_mode(0o755))
                .map_err(|_| "install_applications_invalid")?;
        }
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
        Err(_) => return Err("install_applications_create_failed".into()),
    }
    let metadata = fs::symlink_metadata(&path).map_err(|_| "install_applications_invalid")?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err("install_applications_invalid".into());
    }
    let canonical = fs::canonicalize(&path).map_err(|_| "install_applications_invalid")?;
    if canonical.parent() != Some(canonical_home.as_path()) {
        return Err("install_applications_invalid".into());
    }
    let descriptor = open(
        &canonical,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|_| "install_applications_invalid")?;
    let descriptor = File::from(descriptor);
    let stat = fstat(&descriptor).map_err(|_| "install_applications_invalid")?;
    if FileType::from_raw_mode(stat.st_mode) != FileType::Directory {
        return Err("install_applications_invalid".into());
    }
    Ok(ApplicationsDirectory {
        path: canonical,
        descriptor,
    })
}

fn ensure_applications_directory_path(directory: &ApplicationsDirectory) -> Result<(), String> {
    use rustix::fs::{fstat, open, FileType, Mode, OFlags};

    let held = fstat(&directory.descriptor).map_err(|_| "install_applications_changed")?;
    let visible = open(
        &directory.path,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|_| "install_applications_changed")?;
    let visible = fstat(&visible).map_err(|_| "install_applications_changed")?;
    if FileType::from_raw_mode(held.st_mode) != FileType::Directory
        || FileType::from_raw_mode(visible.st_mode) != FileType::Directory
        || !same_file_identity(&held, &visible)
    {
        return Err("install_applications_changed".into());
    }
    Ok(())
}

fn stat_entry(
    directory: &ApplicationsDirectory,
    name: &str,
) -> Result<Option<rustix::fs::Stat>, String> {
    use rustix::fs::{statat, AtFlags};

    match statat(&directory.descriptor, name, AtFlags::SYMLINK_NOFOLLOW) {
        Ok(stat) => Ok(Some(stat)),
        Err(rustix::io::Errno::NOENT) => Ok(None),
        Err(_) => Err("install_entry_stat_failed".into()),
    }
}

fn validate_transaction_artifacts(
    directory: &ApplicationsDirectory,
    journal: Option<&InstallJournal>,
) -> Result<(), String> {
    use rustix::fs::Dir;

    let mut entries = Dir::read_from(&directory.descriptor)
        .map_err(|_| "install_artifact_scan_failed".to_string())?;
    let allowed = journal.map(|journal| {
        [
            journal.staging_name.as_bytes(),
            journal.backup_name.as_bytes(),
        ]
    });
    for entry in &mut entries {
        let entry = entry.map_err(|_| "install_artifact_scan_failed".to_string())?;
        let name = entry.file_name().to_bytes();
        let is_transaction_artifact = name.starts_with(STAGING_PREFIX.as_bytes())
            || name.starts_with(BACKUP_PREFIX.as_bytes())
            || name.starts_with(format!("{JOURNAL_NAME}.tmp-").as_bytes());
        let is_allowed = allowed
            .as_ref()
            .is_some_and(|allowed| allowed.contains(&name));
        if is_transaction_artifact && !is_allowed {
            return Err("install_orphan_transaction_artifact".into());
        }
    }
    Ok(())
}

fn acquire_install_lock(directory: &ApplicationsDirectory) -> Result<InstallFileLock, String> {
    use rustix::fs::{fchmod, fstat, fsync, openat, FileType, Mode, OFlags};

    let descriptor = openat(
        &directory.descriptor,
        LOCK_NAME,
        OFlags::RDWR | OFlags::CREATE | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::from_raw_mode(0o600),
    )
    .map_err(|_| "install_lock_invalid")?;
    let file = File::from(descriptor);
    let held = fstat(&file).map_err(|_| "install_lock_invalid")?;
    let visible = stat_entry(directory, LOCK_NAME)?.ok_or("install_lock_invalid")?;
    if FileType::from_raw_mode(held.st_mode) != FileType::RegularFile
        || held.st_nlink != 1
        || !same_file_identity(&held, &visible)
    {
        return Err("install_lock_invalid".into());
    }
    fchmod(&file, Mode::from_raw_mode(0o600)).map_err(|_| "install_lock_invalid")?;
    fsync(&directory.descriptor).map_err(|_| "install_directory_sync_failed")?;
    match file.try_lock_exclusive() {
        Ok(()) => Ok(InstallFileLock(file)),
        Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
            Err("install_already_running".into())
        }
        Err(_) => Err("install_lock_failed".into()),
    }
}

fn read_journal(directory: &ApplicationsDirectory) -> Result<Option<InstallJournal>, String> {
    use rustix::fs::{fstat, openat, FileType, Mode, OFlags};

    let descriptor = match openat(
        &directory.descriptor,
        JOURNAL_NAME,
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
    ) {
        Ok(descriptor) => descriptor,
        Err(rustix::io::Errno::NOENT) => return Ok(None),
        Err(_) => return Err("install_journal_invalid".into()),
    };
    let mut file = File::from(descriptor);
    let stat = fstat(&file).map_err(|_| "install_journal_invalid")?;
    let visible = stat_entry(directory, JOURNAL_NAME)?.ok_or("install_journal_invalid")?;
    if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile
        || stat.st_nlink != 1
        || stat.st_mode & 0o777 != 0o600
        || stat.st_size < 0
        || stat.st_size as usize > JOURNAL_MAX_BYTES
        || !same_file_identity(&stat, &visible)
    {
        return Err("install_journal_invalid".into());
    }
    let mut bytes = Vec::with_capacity(stat.st_size as usize);
    Read::by_ref(&mut file)
        .take((JOURNAL_MAX_BYTES + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| "install_journal_invalid")?;
    if bytes.len() > JOURNAL_MAX_BYTES {
        return Err("install_journal_invalid".into());
    }
    let visible_after = stat_entry(directory, JOURNAL_NAME)?.ok_or("install_journal_invalid")?;
    if !same_file_identity(&stat, &visible_after) {
        return Err("install_journal_invalid".into());
    }
    let journal: InstallJournal =
        serde_json::from_slice(&bytes).map_err(|_| "install_journal_invalid")?;
    journal.validate()?;
    Ok(Some(journal))
}

fn write_journal(
    directory: &ApplicationsDirectory,
    journal: &InstallJournal,
) -> Result<(), String> {
    use rustix::fs::{fchmod, fsync, openat, renameat, unlinkat, AtFlags, FileType, Mode, OFlags};

    journal.validate()?;
    if let Some(existing) = stat_entry(directory, JOURNAL_NAME)? {
        if FileType::from_raw_mode(existing.st_mode) != FileType::RegularFile
            || existing.st_nlink != 1
        {
            return Err("install_journal_invalid".into());
        }
    }
    let bytes = serde_json::to_vec(journal).map_err(|_| "install_journal_invalid")?;
    if bytes.len() > JOURNAL_MAX_BYTES {
        return Err("install_journal_invalid".into());
    }
    let temporary = format!("{JOURNAL_NAME}.tmp-{}", uuid::Uuid::new_v4());
    let result = (|| {
        let descriptor = openat(
            &directory.descriptor,
            temporary.as_str(),
            OFlags::WRONLY
                | OFlags::CREATE
                | OFlags::EXCL
                | OFlags::NOFOLLOW
                | OFlags::NONBLOCK
                | OFlags::CLOEXEC,
            Mode::from_raw_mode(0o600),
        )
        .map_err(|_| "install_journal_write_failed".to_string())?;
        let mut file = File::from(descriptor);
        fchmod(&file, Mode::from_raw_mode(0o600))
            .map_err(|_| "install_journal_write_failed".to_string())?;
        file.write_all(&bytes)
            .map_err(|_| "install_journal_write_failed".to_string())?;
        file.sync_all()
            .map_err(|_| "install_journal_sync_failed".to_string())?;
        renameat(
            &directory.descriptor,
            temporary.as_str(),
            &directory.descriptor,
            JOURNAL_NAME,
        )
        .map_err(|_| "install_journal_publish_failed".to_string())?;
        fsync(&directory.descriptor).map_err(|_| "install_directory_sync_failed".to_string())
    })();
    if result.is_err() {
        let _ = unlinkat(&directory.descriptor, temporary.as_str(), AtFlags::empty());
        let _ = fsync(&directory.descriptor);
    }
    result
}

fn remove_journal(directory: &ApplicationsDirectory) -> Result<(), String> {
    use rustix::fs::{fsync, unlinkat, AtFlags, FileType};

    let Some(stat) = stat_entry(directory, JOURNAL_NAME)? else {
        return Ok(());
    };
    if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile || stat.st_nlink != 1 {
        return Err("install_journal_invalid".into());
    }
    unlinkat(&directory.descriptor, JOURNAL_NAME, AtFlags::empty())
        .map_err(|_| "install_journal_cleanup_failed")?;
    fsync(&directory.descriptor).map_err(|_| "install_directory_sync_failed".to_string())
}

fn rename_entry(
    directory: &ApplicationsDirectory,
    source: &str,
    target: &str,
) -> Result<(), String> {
    use rustix::fs::{fsync, renameat};

    renameat(&directory.descriptor, source, &directory.descriptor, target)
        .map_err(|_| "install_rename_failed")?;
    fsync(&directory.descriptor).map_err(|_| "install_directory_sync_failed".to_string())
}

fn real_directory_entry(directory: &ApplicationsDirectory, name: &str) -> Result<bool, String> {
    use rustix::fs::FileType;

    match stat_entry(directory, name)? {
        None => Ok(false),
        Some(stat) if FileType::from_raw_mode(stat.st_mode) == FileType::Directory => Ok(true),
        Some(_) => Err("install_entry_invalid".into()),
    }
}

fn remove_directory_tree_at(
    parent: &File,
    name: &CStr,
    expected: Option<FileIdentity>,
    error_code: &str,
) -> Result<(), String> {
    use rustix::fs::{
        fstat, fsync, openat, statat, unlinkat, AtFlags, Dir, FileType, Mode, OFlags,
    };

    let descriptor = openat(
        parent,
        name,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|_| error_code.to_string())?;
    let directory = File::from(descriptor);
    let held = fstat(&directory).map_err(|_| error_code.to_string())?;
    if FileType::from_raw_mode(held.st_mode) != FileType::Directory
        || expected.is_some_and(|expected| file_identity(&held) != expected)
    {
        return Err("install_entry_changed".into());
    }
    let mut entries = Dir::read_from(&directory).map_err(|_| error_code.to_string())?;
    for entry in &mut entries {
        let entry = entry.map_err(|_| error_code.to_string())?;
        let child = entry.file_name();
        if child.to_bytes() == b"." || child.to_bytes() == b".." {
            continue;
        }
        let stat = statat(&directory, child, AtFlags::SYMLINK_NOFOLLOW)
            .map_err(|_| error_code.to_string())?;
        if FileType::from_raw_mode(stat.st_mode) == FileType::Directory {
            remove_directory_tree_at(&directory, child, Some(file_identity(&stat)), error_code)?;
        } else {
            unlinkat(&directory, child, AtFlags::empty()).map_err(|_| error_code.to_string())?;
        }
    }
    fsync(&directory).map_err(|_| "install_directory_sync_failed".to_string())?;
    let visible = statat(parent, name, AtFlags::SYMLINK_NOFOLLOW)
        .map_err(|_| "install_entry_changed".to_string())?;
    if !same_file_identity(&held, &visible) {
        return Err("install_entry_changed".into());
    }
    unlinkat(parent, name, AtFlags::REMOVEDIR).map_err(|_| error_code.to_string())?;
    fsync(parent).map_err(|_| "install_directory_sync_failed".to_string())
}

fn remove_owned_directory(
    directory: &ApplicationsDirectory,
    name: &str,
    prefix: &str,
    expected: Option<FileIdentity>,
) -> Result<(), String> {
    use rustix::fs::FileType;
    if !name.starts_with(prefix)
        || uuid::Uuid::parse_str(&name[prefix.len()..])
            .map(|uuid| format!("{prefix}{uuid}") != name)
            .unwrap_or(true)
    {
        return Err("install_artifact_name_invalid".into());
    }
    let Some(stat) = stat_entry(directory, name)? else {
        return Ok(());
    };
    if FileType::from_raw_mode(stat.st_mode) != FileType::Directory {
        return Err("install_entry_invalid".into());
    }
    if expected.is_some_and(|expected| file_identity(&stat) != expected) {
        return Err("install_entry_changed".into());
    }
    let name = CString::new(name.as_bytes()).map_err(|_| "install_artifact_name_invalid")?;
    remove_directory_tree_at(
        &directory.descriptor,
        &name,
        expected,
        "install_artifact_cleanup_failed",
    )
}

fn remove_target(directory: &ApplicationsDirectory, expected: FileIdentity) -> Result<(), String> {
    use rustix::fs::FileType;
    let Some(stat) = stat_entry(directory, APP_NAME)? else {
        return Ok(());
    };
    if FileType::from_raw_mode(stat.st_mode) != FileType::Directory {
        return Err("install_entry_invalid".into());
    }
    if file_identity(&stat) != expected {
        return Err("install_entry_changed".into());
    }
    let name = CString::new(APP_NAME.as_bytes()).map_err(|_| "install_artifact_name_invalid")?;
    remove_directory_tree_at(
        &directory.descriptor,
        &name,
        Some(expected),
        "install_target_cleanup_failed",
    )
}

struct InstallRunGuard(Arc<(StdMutex<bool>, Condvar)>);

impl InstallRunGuard {
    fn enter(state: Arc<(StdMutex<bool>, Condvar)>) -> Result<Self, String> {
        let mut running = state
            .0
            .lock()
            .map_err(|_| "install_state_lock_poisoned".to_string())?;
        if *running {
            return Err("install_already_running".into());
        }
        *running = true;
        drop(running);
        Ok(Self(state))
    }
}

impl Drop for InstallRunGuard {
    fn drop(&mut self) {
        if let Ok(mut running) = self.0 .0.lock() {
            *running = false;
            self.0 .1.notify_all();
        }
    }
}

#[derive(Clone)]
pub struct InstallManager {
    source_app: PathBuf,
    home: PathBuf,
    manifest: Arc<VerifiedPackageManifest>,
    faults: FaultInjection,
    backend: Arc<dyn CopyBackend>,
    cancellation: InstallCancellation,
    in_flight: Arc<(StdMutex<bool>, Condvar)>,
}

impl InstallManager {
    pub fn new(
        source_app: PathBuf,
        home: PathBuf,
        manifest: Arc<VerifiedPackageManifest>,
        faults: FaultInjection,
    ) -> Self {
        Self::with_backend(source_app, home, manifest, faults, Arc::new(MacCopyBackend))
    }

    pub fn with_backend(
        source_app: PathBuf,
        home: PathBuf,
        manifest: Arc<VerifiedPackageManifest>,
        faults: FaultInjection,
        backend: Arc<dyn CopyBackend>,
    ) -> Self {
        Self {
            source_app,
            home,
            manifest,
            faults,
            backend,
            cancellation: InstallCancellation::new(),
            in_flight: Arc::new((StdMutex::new(false), Condvar::new())),
        }
    }

    pub fn reset_cancellation(&self) {
        self.cancellation.reset();
    }

    pub fn reset(&self) {
        self.reset_cancellation();
    }

    pub fn cancel(&self) {
        self.cancellation.cancel();
    }

    pub fn cancellation(&self) -> InstallCancellation {
        self.cancellation.clone()
    }

    pub fn wait_for_idle(&self, timeout: Duration) -> bool {
        let Ok(running) = self.in_flight.0.lock() else {
            return false;
        };
        if !*running {
            return true;
        }
        self.in_flight
            .1
            .wait_timeout_while(running, timeout, |value| *value)
            .map(|(running, _)| !*running)
            .unwrap_or(false)
    }

    fn verify_archive_at(
        &self,
        packs: &File,
        descriptor: &crate::manifest::RuntimeDescriptor,
        cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        let relative = format!("Contents/Resources/runtime-packs/{}", descriptor.archive);
        let (_, digest) = digest_regular_file(
            packs,
            &descriptor.archive,
            descriptor.size_bytes,
            Some(descriptor.size_bytes),
            cancellation,
            &relative,
        )?;
        if digest != descriptor.sha256 {
            return Err(format!("installed_checksum_mismatch:{relative}"));
        }
        Ok(())
    }

    fn inspect_open_bundle(
        &self,
        bundle: &OpenBundle,
        expected: Option<&VerifiedPackageManifest>,
        cancellation: &InstallCancellation,
    ) -> Result<(VerifiedPackageManifest, BundleEvidence), String> {
        let expected_size = expected.map(|manifest| manifest.signed_bytes().len() as u64);
        let bytes = read_regular_bytes(
            &bundle.resources,
            "package_manifest.json",
            MANIFEST_MAX_BYTES,
            expected_size,
            cancellation,
            MANIFEST_RELATIVE,
        )?;
        if expected.is_some_and(|manifest| bytes != manifest.signed_bytes()) {
            return Err("install_manifest_bytes_mismatch".into());
        }
        let verified = self
            .manifest
            .reverify(&bytes)
            .map_err(|_| "installed_manifest_invalid".to_string())?;
        if expected.is_some_and(|manifest| verified.manifest() != manifest.manifest()) {
            return Err("install_manifest_payload_mismatch".into());
        }
        let package = verified.manifest();
        self.verify_archive_at(&bundle.packs, &package.runtimes.core, cancellation)?;
        self.verify_archive_at(&bundle.packs, &package.runtimes.collector, cancellation)?;
        let binary = digest_regular_file(
            &bundle.macos,
            "data-scientist",
            BINARY_MAX_BYTES,
            None,
            cancellation,
            BINARY_RELATIVE,
        )?;
        Ok((
            verified,
            BundleEvidence {
                identity: bundle.identity,
                binary,
            },
        ))
    }

    #[cfg(test)]
    fn inspect_bundle(
        &self,
        root: &Path,
        expected: Option<&VerifiedPackageManifest>,
        cancellation: &InstallCancellation,
    ) -> Result<(VerifiedPackageManifest, BundleEvidence), String> {
        self.inspect_open_bundle(&open_bundle(root)?, expected, cancellation)
    }

    fn inspect_bundle_entry(
        &self,
        directory: &ApplicationsDirectory,
        name: &str,
        expected: Option<&VerifiedPackageManifest>,
        cancellation: &InstallCancellation,
    ) -> Result<(VerifiedPackageManifest, BundleEvidence), String> {
        self.inspect_open_bundle(&open_bundle_at(directory, name)?, expected, cancellation)
    }

    fn bundle_snapshot(
        verified: VerifiedPackageManifest,
        evidence: BundleEvidence,
    ) -> BundleSnapshot {
        BundleSnapshot {
            signed_manifest: verified.signed_bytes().to_vec(),
            evidence,
        }
    }

    #[cfg(test)]
    fn inspect_snapshot(
        &self,
        root: &Path,
        expected: Option<&VerifiedPackageManifest>,
        cancellation: &InstallCancellation,
    ) -> Result<BundleSnapshot, String> {
        let (verified, evidence) = self.inspect_bundle(root, expected, cancellation)?;
        Ok(Self::bundle_snapshot(verified, evidence))
    }

    fn inspect_entry_snapshot(
        &self,
        directory: &ApplicationsDirectory,
        name: &str,
        expected: Option<&VerifiedPackageManifest>,
        cancellation: &InstallCancellation,
    ) -> Result<BundleSnapshot, String> {
        let (verified, evidence) =
            self.inspect_bundle_entry(directory, name, expected, cancellation)?;
        Ok(Self::bundle_snapshot(verified, evidence))
    }

    fn ensure_open_bundle_snapshot_unchanged(
        &self,
        bundle: &OpenBundle,
        expected: &BundleSnapshot,
        cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        let (verified, evidence) = self.inspect_open_bundle(bundle, None, cancellation)?;
        let actual = Self::bundle_snapshot(verified, evidence);
        if actual != *expected {
            return Err("install_entry_changed".into());
        }
        Ok(())
    }

    fn ensure_entry_snapshot_unchanged(
        &self,
        directory: &ApplicationsDirectory,
        name: &str,
        expected: &BundleSnapshot,
        cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        let actual = self.inspect_entry_snapshot(directory, name, None, cancellation)?;
        if actual != *expected {
            return Err("install_entry_changed".into());
        }
        Ok(())
    }

    #[cfg(test)]
    fn verify_copy(&self, source: &Path, staged: &Path) -> Result<BundleIdentity, String> {
        let source_snapshot =
            self.inspect_snapshot(source, Some(&self.manifest), &self.cancellation)?;
        let staged_snapshot =
            self.inspect_snapshot(staged, Some(&self.manifest), &self.cancellation)?;
        if source_snapshot.evidence.binary != staged_snapshot.evidence.binary {
            return Err("installed_binary_mismatch".into());
        }
        Ok(staged_snapshot.evidence.identity)
    }

    fn verify_staged_entry(
        &self,
        source_bundle: &OpenBundle,
        source: &BundleSnapshot,
        directory: &ApplicationsDirectory,
        staging_name: &str,
    ) -> Result<BundleSnapshot, String> {
        self.ensure_open_bundle_snapshot_unchanged(source_bundle, source, &self.cancellation)?;
        let staged = self.inspect_entry_snapshot(
            directory,
            staging_name,
            Some(&self.manifest),
            &self.cancellation,
        )?;
        if source.evidence.binary != staged.evidence.binary {
            return Err("installed_binary_mismatch".into());
        }
        Ok(staged)
    }

    fn verify_journal_bundle_entry(
        &self,
        directory: &ApplicationsDirectory,
        name: &str,
        journal: &InstallJournal,
        cancellation: &InstallCancellation,
    ) -> Result<BundleSnapshot, String> {
        let snapshot = self.inspect_entry_snapshot(directory, name, None, cancellation)?;
        let verified = self
            .manifest
            .reverify(&snapshot.signed_manifest)
            .map_err(|_| "install_recovery_bundle_mismatch".to_string())?;
        if verified.manifest().package_id != journal.package_id
            || verified.manifest().build_version != journal.build_version
        {
            return Err("install_recovery_bundle_mismatch".into());
        }
        Ok(snapshot)
    }

    fn verify_backup_bundle_entry(
        &self,
        directory: &ApplicationsDirectory,
        name: &str,
        package_id: &str,
        cancellation: &InstallCancellation,
    ) -> Result<BundleSnapshot, String> {
        let snapshot = self.inspect_entry_snapshot(directory, name, None, cancellation)?;
        let verified = self
            .manifest
            .reverify(&snapshot.signed_manifest)
            .map_err(|_| "install_recovery_backup_mismatch".to_string())?;
        if verified.manifest().package_id != package_id {
            return Err("install_recovery_backup_mismatch".into());
        }
        Ok(snapshot)
    }

    fn classify_target_path(
        &self,
        directory: &ApplicationsDirectory,
    ) -> Result<(ExistingTargetDecision, BundleSnapshot), String> {
        let snapshot =
            self.inspect_entry_snapshot(directory, APP_NAME, None, &self.cancellation)?;
        let decision = classify_existing_target(&self.manifest, &snapshot.signed_manifest)?;
        self.verify_codesigned_snapshot(directory, APP_NAME, &snapshot, &self.cancellation)?;
        Ok((decision, snapshot))
    }

    fn rename_snapshot_entry(
        &self,
        directory: &ApplicationsDirectory,
        source: &str,
        target: &str,
        snapshot: &BundleSnapshot,
        cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        self.ensure_entry_snapshot_unchanged(directory, source, snapshot, cancellation)?;
        rename_entry(directory, source, target)?;
        if let Err(error) =
            self.ensure_entry_snapshot_unchanged(directory, target, snapshot, cancellation)
        {
            let _ = rename_entry(directory, target, source);
            return Err(error);
        }
        Ok(())
    }

    fn remove_snapshot_entry(
        &self,
        directory: &ApplicationsDirectory,
        name: &str,
        prefix: Option<&str>,
        snapshot: &BundleSnapshot,
        cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        self.ensure_entry_snapshot_unchanged(directory, name, snapshot, cancellation)?;
        match prefix {
            Some(prefix) => remove_owned_directory(
                directory,
                name,
                prefix,
                Some(snapshot.evidence.identity.root),
            ),
            None if name == APP_NAME => remove_target(directory, snapshot.evidence.identity.root),
            None => Err("install_artifact_name_invalid".into()),
        }
    }

    fn verify_codesigned_snapshot(
        &self,
        directory: &ApplicationsDirectory,
        name: &str,
        snapshot: &BundleSnapshot,
        cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        ensure_applications_directory_path(directory)?;
        let target = AnchoredPath::new(&directory.descriptor, &directory.path, name)?;
        self.backend
            .verify_code_signature_anchored(&target, cancellation)?;
        ensure_applications_directory_path(directory)?;
        self.ensure_entry_snapshot_unchanged(directory, name, snapshot, cancellation)
    }

    fn remove_changed_target(&self, directory: &ApplicationsDirectory) -> Result<(), String> {
        use rustix::fs::FileType;

        let Some(stat) = stat_entry(directory, APP_NAME)? else {
            return Ok(());
        };
        if FileType::from_raw_mode(stat.st_mode) != FileType::Directory {
            return Err("install_entry_invalid".into());
        }
        remove_target(directory, file_identity(&stat))
    }

    fn cleanup_staging(
        &self,
        directory: &ApplicationsDirectory,
        journal: &InstallJournal,
        cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        if real_directory_entry(directory, &journal.staging_name)? {
            let snapshot = self.verify_journal_bundle_entry(
                directory,
                &journal.staging_name,
                journal,
                cancellation,
            )?;
            self.remove_snapshot_entry(
                directory,
                &journal.staging_name,
                Some(STAGING_PREFIX),
                &snapshot,
                cancellation,
            )?;
        }
        Ok(())
    }

    fn rollback_journal(
        &self,
        directory: &ApplicationsDirectory,
        journal: &InstallJournal,
        expected_target: Option<&BundleSnapshot>,
    ) -> Result<(), String> {
        journal.validate()?;
        let cleanup_cancellation = InstallCancellation::new();
        let backup_exists = real_directory_entry(directory, &journal.backup_name)?;
        let target_exists = real_directory_entry(directory, APP_NAME)?;
        if backup_exists {
            let backup = if let Some(expected) = journal.previous_snapshot.as_ref() {
                self.ensure_entry_snapshot_unchanged(
                    directory,
                    &journal.backup_name,
                    expected,
                    &cleanup_cancellation,
                )?;
                expected.clone()
            } else {
                let snapshot = self.verify_backup_bundle_entry(
                    directory,
                    &journal.backup_name,
                    &journal.package_id,
                    &cleanup_cancellation,
                )?;
                self.verify_codesigned_snapshot(
                    directory,
                    &journal.backup_name,
                    &snapshot,
                    &cleanup_cancellation,
                )?;
                snapshot
            };
            if target_exists {
                let target = match expected_target {
                    Some(expected) => self
                        .ensure_entry_snapshot_unchanged(
                            directory,
                            APP_NAME,
                            expected,
                            &cleanup_cancellation,
                        )
                        .map(|()| expected.clone()),
                    None => self
                        .verify_journal_bundle_entry(
                            directory,
                            APP_NAME,
                            journal,
                            &cleanup_cancellation,
                        )
                        .and_then(|snapshot| {
                            self.verify_codesigned_snapshot(
                                directory,
                                APP_NAME,
                                &snapshot,
                                &cleanup_cancellation,
                            )?;
                            Ok(snapshot)
                        }),
                };
                match target {
                    Ok(snapshot) => self.remove_snapshot_entry(
                        directory,
                        APP_NAME,
                        None,
                        &snapshot,
                        &cleanup_cancellation,
                    )?,
                    Err(_) => self.remove_changed_target(directory)?,
                }
            }
            self.rename_snapshot_entry(
                directory,
                &journal.backup_name,
                APP_NAME,
                &backup,
                &cleanup_cancellation,
            )?;
            self.cleanup_staging(directory, journal, &cleanup_cancellation)?;
            remove_journal(directory)?;
            return Ok(());
        }
        if journal.had_previous {
            if journal.phase == TransactionPhase::Prepared && target_exists {
                self.cleanup_staging(directory, journal, &cleanup_cancellation)?;
                remove_journal(directory)?;
                return Ok(());
            }
            return Err("install_recovery_backup_missing".into());
        }
        if target_exists {
            let target = match expected_target {
                Some(expected) => self
                    .ensure_entry_snapshot_unchanged(
                        directory,
                        APP_NAME,
                        expected,
                        &cleanup_cancellation,
                    )
                    .map(|()| expected.clone()),
                None => self
                    .verify_journal_bundle_entry(
                        directory,
                        APP_NAME,
                        journal,
                        &cleanup_cancellation,
                    )
                    .and_then(|snapshot| {
                        self.verify_codesigned_snapshot(
                            directory,
                            APP_NAME,
                            &snapshot,
                            &cleanup_cancellation,
                        )?;
                        Ok(snapshot)
                    }),
            };
            match target {
                Ok(snapshot) => self.remove_snapshot_entry(
                    directory,
                    APP_NAME,
                    None,
                    &snapshot,
                    &cleanup_cancellation,
                )?,
                Err(_) => self.remove_changed_target(directory)?,
            }
        }
        self.cleanup_staging(directory, journal, &cleanup_cancellation)?;
        remove_journal(directory)
    }

    fn recover_transaction(
        &self,
        directory: &ApplicationsDirectory,
        journal: Option<InstallJournal>,
    ) -> Result<bool, String> {
        let Some(journal) = journal else {
            return Ok(false);
        };
        let cleanup_cancellation = InstallCancellation::new();
        if journal.phase != TransactionPhase::Committed {
            self.rollback_journal(directory, &journal, None)?;
            return Ok(false);
        }
        let target_snapshot = if real_directory_entry(directory, APP_NAME)? {
            self.verify_journal_bundle_entry(directory, APP_NAME, &journal, &cleanup_cancellation)
                .ok()
        } else {
            None
        };
        let target_is_valid = target_snapshot.as_ref().is_some_and(|snapshot| {
            self.verify_codesigned_snapshot(directory, APP_NAME, snapshot, &cleanup_cancellation)
                .is_ok()
        });
        if !target_is_valid {
            self.rollback_journal(directory, &journal, target_snapshot.as_ref())?;
            return Ok(false);
        }
        let cleanup = (|| {
            self.cleanup_staging(directory, &journal, &cleanup_cancellation)?;
            if real_directory_entry(directory, &journal.backup_name)? {
                let backup = self.verify_backup_bundle_entry(
                    directory,
                    &journal.backup_name,
                    &journal.package_id,
                    &cleanup_cancellation,
                )?;
                self.verify_codesigned_snapshot(
                    directory,
                    &journal.backup_name,
                    &backup,
                    &cleanup_cancellation,
                )?;
                self.remove_snapshot_entry(
                    directory,
                    &journal.backup_name,
                    Some(BACKUP_PREFIX),
                    &backup,
                    &cleanup_cancellation,
                )?;
            }
            remove_journal(directory)
        })();
        if let Err(error) = cleanup {
            tracing::warn!(%error, "committed install cleanup remains pending");
        }
        Ok(true)
    }

    fn rollback_failure(
        &self,
        directory: &ApplicationsDirectory,
        journal: &InstallJournal,
        expected_target: Option<&BundleSnapshot>,
        primary: String,
    ) -> InstallOutcome {
        match self.rollback_journal(directory, journal, expected_target) {
            Ok(()) => InstallOutcome::Failed(primary),
            Err(rollback) => InstallOutcome::Failed(format!("{primary}; {rollback}")),
        }
    }

    fn cleanup_prepared_copy(
        &self,
        directory: &ApplicationsDirectory,
        journal: &InstallJournal,
        expected: Option<&BundleSnapshot>,
        primary: String,
    ) -> InstallOutcome {
        let cleanup = match expected {
            Some(expected) => self.remove_snapshot_entry(
                directory,
                &journal.staging_name,
                Some(STAGING_PREFIX),
                expected,
                &InstallCancellation::new(),
            ),
            None => remove_owned_directory(directory, &journal.staging_name, STAGING_PREFIX, None),
        };
        match cleanup {
            Ok(()) => InstallOutcome::Failed(primary),
            Err(error) => InstallOutcome::Failed(format!("{primary}; {error}")),
        }
    }

    pub fn install(&self) -> InstallOutcome {
        let _run = match InstallRunGuard::enter(self.in_flight.clone()) {
            Ok(run) => run,
            Err(error) => return InstallOutcome::Failed(error),
        };
        if self.cancellation.is_cancelled() {
            return InstallOutcome::Failed("install_cancelled".into());
        }
        if self.faults.install {
            return InstallOutcome::Failed("fault_install".into());
        }
        match choose_target(&self.source_app, &self.home) {
            InstallTarget::AlreadyInstalled => InstallOutcome::AlreadyInstalled,
            InstallTarget::CopyTo(target) => {
                let directory = match applications_directory(&self.home) {
                    Ok(directory) => directory,
                    Err(error) => return InstallOutcome::Failed(error),
                };
                let target_parent = target
                    .parent()
                    .and_then(|parent| fs::canonicalize(parent).ok());
                if target.file_name() != Some(OsStr::new(APP_NAME))
                    || target_parent.as_deref() != Some(directory.path.as_path())
                {
                    return InstallOutcome::Failed("install_target_changed".into());
                }
                let _lock = match acquire_install_lock(&directory) {
                    Ok(lock) => lock,
                    Err(error) => return InstallOutcome::Failed(error),
                };
                if let Err(error) = ensure_applications_directory_path(&directory) {
                    return InstallOutcome::Failed(error);
                }
                let pending_journal = match read_journal(&directory) {
                    Ok(journal) => journal,
                    Err(error) => return InstallOutcome::Failed(error),
                };
                if let Err(error) =
                    validate_transaction_artifacts(&directory, pending_journal.as_ref())
                {
                    return InstallOutcome::Failed(error);
                }
                match self.recover_transaction(&directory, pending_journal) {
                    Ok(_) => {}
                    Err(error) => return InstallOutcome::Failed(error),
                }
                if let Err(error) = validate_transaction_artifacts(&directory, None) {
                    return InstallOutcome::Failed(error);
                }
                let source_bundle = match open_bundle(&self.source_app) {
                    Ok(bundle) => bundle,
                    Err(error) => return InstallOutcome::Failed(error),
                };
                let source_snapshot = match self.inspect_open_bundle(
                    &source_bundle,
                    Some(&self.manifest),
                    &self.cancellation,
                ) {
                    Ok((verified, evidence)) => Self::bundle_snapshot(verified, evidence),
                    Err(error) => return InstallOutcome::Failed(error),
                };
                let had_previous = match real_directory_entry(&directory, APP_NAME) {
                    Ok(exists) => exists,
                    Err(error) => return InstallOutcome::Failed(error),
                };
                let mut previous_snapshot = None;
                if had_previous {
                    match self.classify_target_path(&directory) {
                        Ok((ExistingTargetDecision::Keep, _)) => {
                            return InstallOutcome::AlreadyInstalled;
                        }
                        Ok((ExistingTargetDecision::Replace, snapshot)) => {
                            previous_snapshot = Some(snapshot);
                        }
                        Err(error) => return InstallOutcome::Failed(error),
                    }
                }
                let mut journal = InstallJournal::new(&self.manifest, had_previous);
                journal.previous_snapshot = previous_snapshot.clone();
                for artifact in [&journal.staging_name, &journal.backup_name] {
                    match stat_entry(&directory, artifact) {
                        Ok(None) => {}
                        Ok(Some(_)) => {
                            return InstallOutcome::Failed("install_transaction_collision".into());
                        }
                        Err(error) => return InstallOutcome::Failed(error),
                    }
                }
                if let Err(error) = ensure_applications_directory_path(&directory) {
                    return InstallOutcome::Failed(error);
                }
                let notice = self
                    .backend
                    .notify_install_start(&target, &self.cancellation);
                if let Err(error) = ensure_applications_directory_path(&directory) {
                    return InstallOutcome::Failed(error);
                }
                match notice {
                    Ok(()) => {}
                    Err(error) if error == "install_cancelled" => {
                        return InstallOutcome::Failed(error);
                    }
                    Err(error) => return InstallOutcome::Failed(error),
                }
                if let Err(error) = self.ensure_open_bundle_snapshot_unchanged(
                    &source_bundle,
                    &source_snapshot,
                    &self.cancellation,
                ) {
                    return InstallOutcome::Failed(error);
                }
                if let Err(error) = ensure_applications_directory_path(&directory) {
                    return InstallOutcome::Failed(error);
                }
                let staging_external = match AnchoredPath::new(
                    &directory.descriptor,
                    &directory.path,
                    &journal.staging_name,
                ) {
                    Ok(path) => path,
                    Err(error) => return InstallOutcome::Failed(error),
                };
                let copy_result = self.backend.copy_app_anchored(
                    &self.source_app,
                    &staging_external,
                    &self.cancellation,
                );
                if let Err(error) = ensure_applications_directory_path(&directory) {
                    return self.cleanup_prepared_copy(&directory, &journal, None, error);
                }
                if let Err(error) = copy_result {
                    if error != "install_cancelled" {
                        let _ = self
                            .backend
                            .notify_install_failure(&target, &self.cancellation);
                    }
                    return self.cleanup_prepared_copy(&directory, &journal, None, error);
                }
                match real_directory_entry(&directory, &journal.staging_name) {
                    Ok(true) => {}
                    Ok(false) => {
                        return InstallOutcome::Failed("install_staging_missing".into());
                    }
                    Err(error) => return InstallOutcome::Failed(error),
                }
                if let Err(error) = self
                    .backend
                    .checkpoint(InstallCheckpoint::Copied, &self.cancellation)
                {
                    return self.cleanup_prepared_copy(&directory, &journal, None, error);
                }
                if self.cancellation.is_cancelled() {
                    return self.cleanup_prepared_copy(
                        &directory,
                        &journal,
                        None,
                        "install_cancelled".into(),
                    );
                }
                if let Err(error) = ensure_applications_directory_path(&directory) {
                    return self.cleanup_prepared_copy(&directory, &journal, None, error);
                }
                let quarantine = self
                    .backend
                    .clear_quarantine_anchored(&staging_external, &self.cancellation);
                if let Err(error) = ensure_applications_directory_path(&directory) {
                    return self.cleanup_prepared_copy(&directory, &journal, None, error);
                }
                if let Err(error) = quarantine {
                    return self.cleanup_prepared_copy(&directory, &journal, None, error);
                }
                let staged_snapshot = match self.verify_staged_entry(
                    &source_bundle,
                    &source_snapshot,
                    &directory,
                    &journal.staging_name,
                ) {
                    Ok(snapshot) => snapshot,
                    Err(error) => {
                        return self.cleanup_prepared_copy(&directory, &journal, None, error);
                    }
                };
                if let Err(error) = self.verify_codesigned_snapshot(
                    &directory,
                    &journal.staging_name,
                    &staged_snapshot,
                    &self.cancellation,
                ) {
                    return self.cleanup_prepared_copy(
                        &directory,
                        &journal,
                        Some(&staged_snapshot),
                        error,
                    );
                }
                if let Err(error) = write_journal(&directory, &journal) {
                    return self.cleanup_prepared_copy(
                        &directory,
                        &journal,
                        Some(&staged_snapshot),
                        error,
                    );
                }
                if self.cancellation.is_cancelled() {
                    return self.rollback_failure(
                        &directory,
                        &journal,
                        Some(&staged_snapshot),
                        "install_cancelled".into(),
                    );
                }
                if had_previous {
                    let Some(previous) = previous_snapshot.as_ref() else {
                        return self.rollback_failure(
                            &directory,
                            &journal,
                            Some(&staged_snapshot),
                            "install_previous_snapshot_missing".into(),
                        );
                    };
                    if let Err(error) = self.rename_snapshot_entry(
                        &directory,
                        APP_NAME,
                        &journal.backup_name,
                        previous,
                        &self.cancellation,
                    ) {
                        return self.rollback_failure(
                            &directory,
                            &journal,
                            Some(&staged_snapshot),
                            error,
                        );
                    }
                    if let Err(error) = self.backend.checkpoint(
                        InstallCheckpoint::BackupRenamedBeforeJournal,
                        &self.cancellation,
                    ) {
                        return self.rollback_failure(
                            &directory,
                            &journal,
                            Some(&staged_snapshot),
                            error,
                        );
                    }
                    journal.phase = TransactionPhase::BackupMoved;
                    if let Err(error) = write_journal(&directory, &journal) {
                        return self.rollback_failure(
                            &directory,
                            &journal,
                            Some(&staged_snapshot),
                            error,
                        );
                    }
                    if let Err(error) = self
                        .backend
                        .checkpoint(InstallCheckpoint::BackupMoved, &self.cancellation)
                    {
                        return self.rollback_failure(
                            &directory,
                            &journal,
                            Some(&staged_snapshot),
                            error,
                        );
                    }
                    if self.cancellation.is_cancelled() {
                        return self.rollback_failure(
                            &directory,
                            &journal,
                            Some(&staged_snapshot),
                            "install_cancelled".into(),
                        );
                    }
                }
                if let Err(error) = self.rename_snapshot_entry(
                    &directory,
                    &journal.staging_name,
                    APP_NAME,
                    &staged_snapshot,
                    &self.cancellation,
                ) {
                    return self.rollback_failure(
                        &directory,
                        &journal,
                        Some(&staged_snapshot),
                        error,
                    );
                }
                if let Err(error) = self.backend.checkpoint(
                    InstallCheckpoint::TargetRenamedBeforeJournal,
                    &self.cancellation,
                ) {
                    return self.rollback_failure(
                        &directory,
                        &journal,
                        Some(&staged_snapshot),
                        error,
                    );
                }
                journal.phase = TransactionPhase::TargetSwitched;
                if let Err(error) = write_journal(&directory, &journal) {
                    return self.rollback_failure(
                        &directory,
                        &journal,
                        Some(&staged_snapshot),
                        error,
                    );
                }
                if let Err(error) = self
                    .backend
                    .checkpoint(InstallCheckpoint::TargetSwitched, &self.cancellation)
                {
                    return self.rollback_failure(
                        &directory,
                        &journal,
                        Some(&staged_snapshot),
                        error,
                    );
                }
                if self.cancellation.is_cancelled() {
                    return self.rollback_failure(
                        &directory,
                        &journal,
                        Some(&staged_snapshot),
                        "install_cancelled".into(),
                    );
                }
                if let Err(error) = self.ensure_entry_snapshot_unchanged(
                    &directory,
                    APP_NAME,
                    &staged_snapshot,
                    &self.cancellation,
                ) {
                    return self.rollback_failure(
                        &directory,
                        &journal,
                        Some(&staged_snapshot),
                        error,
                    );
                }
                if let Err(error) = ensure_applications_directory_path(&directory) {
                    return self.rollback_failure(
                        &directory,
                        &journal,
                        Some(&staged_snapshot),
                        error,
                    );
                }
                let target_external =
                    match AnchoredPath::new(&directory.descriptor, &directory.path, APP_NAME) {
                        Ok(path) => path,
                        Err(error) => {
                            return self.rollback_failure(
                                &directory,
                                &journal,
                                Some(&staged_snapshot),
                                error,
                            );
                        }
                    };
                let registration = self
                    .backend
                    .register_app_anchored(&target_external, &self.cancellation);
                if let Err(error) = ensure_applications_directory_path(&directory) {
                    return self.rollback_failure(
                        &directory,
                        &journal,
                        Some(&staged_snapshot),
                        error,
                    );
                }
                if let Err(error) = registration {
                    return self.rollback_failure(
                        &directory,
                        &journal,
                        Some(&staged_snapshot),
                        error,
                    );
                }
                if let Err(error) = self.ensure_entry_snapshot_unchanged(
                    &directory,
                    APP_NAME,
                    &staged_snapshot,
                    &self.cancellation,
                ) {
                    return self.rollback_failure(
                        &directory,
                        &journal,
                        Some(&staged_snapshot),
                        error,
                    );
                }
                if let Err(error) = self.verify_codesigned_snapshot(
                    &directory,
                    APP_NAME,
                    &staged_snapshot,
                    &self.cancellation,
                ) {
                    return self.rollback_failure(
                        &directory,
                        &journal,
                        Some(&staged_snapshot),
                        error,
                    );
                }
                journal.phase = TransactionPhase::Committed;
                if let Err(error) = write_journal(&directory, &journal) {
                    return self.rollback_failure(
                        &directory,
                        &journal,
                        Some(&staged_snapshot),
                        error,
                    );
                }
                if let Err(error) = self
                    .backend
                    .checkpoint(InstallCheckpoint::Committed, &self.cancellation)
                {
                    tracing::warn!(%error, "committed install cleanup intentionally deferred");
                    return InstallOutcome::Installed(target);
                }
                if let Err(error) = ensure_applications_directory_path(&directory) {
                    return self.rollback_failure(
                        &directory,
                        &journal,
                        Some(&staged_snapshot),
                        error,
                    );
                }
                let cleanup = (|| {
                    let cleanup_cancellation = InstallCancellation::new();
                    if had_previous {
                        let previous = previous_snapshot
                            .as_ref()
                            .ok_or_else(|| "install_previous_snapshot_missing".to_string())?;
                        self.remove_snapshot_entry(
                            &directory,
                            &journal.backup_name,
                            Some(BACKUP_PREFIX),
                            previous,
                            &cleanup_cancellation,
                        )?;
                    }
                    remove_journal(&directory)
                })();
                if let Err(error) = cleanup {
                    tracing::warn!(%error, "committed install cleanup remains pending");
                }
                InstallOutcome::Installed(target)
            }
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ExistingTargetDecision {
    Keep,
    Replace,
}

pub fn choose_target(source: &Path, home: &Path) -> InstallTarget {
    let user_applications = home.join("Applications");
    let is_within = |root: &Path| {
        if source
            .components()
            .any(|component| component == Component::ParentDir)
        {
            return false;
        }
        match (fs::canonicalize(source), fs::canonicalize(root)) {
            (Ok(source), Ok(root)) => source.starts_with(root),
            (Ok(_), Err(_)) => false,
            (Err(_), _) => source.starts_with(root),
        }
    };
    if is_within(Path::new("/Applications")) || is_within(&user_applications) {
        InstallTarget::AlreadyInstalled
    } else {
        InstallTarget::CopyTo(user_applications.join(APP_NAME))
    }
}

fn parse_build_version(value: &str) -> Result<(u32, u64), String> {
    let mut parts = value.split('.');
    let date = parts.next().unwrap_or_default();
    let revision = parts.next();
    if parts.next().is_some()
        || date.len() != 8
        || !date.bytes().all(|byte| byte.is_ascii_digit())
        || revision
            .is_some_and(|part| part.is_empty() || !part.bytes().all(|byte| byte.is_ascii_digit()))
    {
        return Err("install_build_version_invalid".into());
    }
    let date = date
        .parse::<u32>()
        .map_err(|_| "install_build_version_invalid".to_string())?;
    let revision = revision
        .unwrap_or("0")
        .parse::<u64>()
        .map_err(|_| "install_build_version_invalid".to_string())?;
    Ok((date, revision))
}

fn compare_build_versions(left: &str, right: &str) -> Result<Ordering, String> {
    Ok(parse_build_version(left)?.cmp(&parse_build_version(right)?))
}

fn classify_existing_target(
    current: &VerifiedPackageManifest,
    installed_signed_manifest: &[u8],
) -> Result<ExistingTargetDecision, String> {
    let installed = current
        .reverify(installed_signed_manifest)
        .map_err(|_| "installed_manifest_invalid".to_string())?;
    let source = current.manifest();
    let target = installed.manifest();
    if target.package_id != source.package_id {
        return Err("installed_package_mismatch".into());
    }
    if target.arch != source.arch
        || target.supported_architectures != source.supported_architectures
    {
        return Err("installed_arch_mismatch".into());
    }
    match compare_build_versions(&target.build_version, &source.build_version)
        .map_err(|_| "installed_build_version_invalid".to_string())?
    {
        Ordering::Greater => Ok(ExistingTargetDecision::Keep),
        Ordering::Less => Ok(ExistingTargetDecision::Replace),
        Ordering::Equal if installed_signed_manifest == current.signed_bytes() => {
            Ok(ExistingTargetDecision::Keep)
        }
        Ordering::Equal => Err("installed_build_collision".into()),
    }
}

#[cfg(test)]
mod tests {
    use std::{
        cmp::Ordering,
        ffi::OsStr,
        fs::{self, File},
        os::{
            fd::AsRawFd,
            unix::fs::{symlink, PermissionsExt},
        },
        path::Path,
        process::Command,
        sync::{
            atomic::{AtomicBool, Ordering as AtomicOrdering},
            Arc, Mutex,
        },
        thread,
        time::{Duration, Instant},
    };

    use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
    use ed25519_dalek::{
        pkcs8::{spki::der::pem::LineEnding, EncodePublicKey},
        Signer, SigningKey,
    };
    use serde_json::{json, Value};
    use sha2::{Digest, Sha256};

    use crate::{fault_injection::FaultInjection, manifest::VerifiedPackageManifest};

    use super::{
        choose_target, classify_existing_target, compare_build_versions, run_cancellable_command,
        AnchoredPath, CopyBackend, ExistingTargetDecision, InstallCancellation, InstallCheckpoint,
        InstallManager, InstallOutcome, InstallTarget, MacCopyBackend,
    };

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

    #[test]
    fn mac_backend_child_cwd_remains_anchored_after_visible_path_swap() {
        let temp = tempfile::tempdir().unwrap();
        let visible = temp.path().join("Applications");
        let held = temp.path().join("Applications-held");
        let outside = temp.path().join("outside");
        fs::create_dir(&visible).unwrap();
        fs::create_dir(&outside).unwrap();
        let descriptor = File::open(&visible).unwrap();
        fs::rename(&visible, &held).unwrap();
        symlink(&outside, &visible).unwrap();

        MacCopyBackend::run_anchored(
            "/usr/bin/touch",
            &[OsStr::new("proof")],
            descriptor.as_raw_fd(),
            &InstallCancellation::new(),
            "touch_failed",
        )
        .unwrap();

        assert!(held.join("proof").is_file());
        assert!(!outside.join("proof").exists());
    }

    fn payload(build_version: &str, package_id: &str) -> Value {
        let descriptor = |kind: &str, bytes: &[u8], tree: &str| {
            json!({
                "archive": format!("{kind}-runtime.tar.zst"),
                "required_files": [format!("scripts/{kind}.json")],
                "sha256": hex::encode(Sha256::digest(bytes)),
                "size_bytes": bytes.len(),
                "tree_sha256": tree.repeat(64),
                "version": format!("{kind}-v1")
            })
        };
        json!({
            "arch": "arm64",
            "build_version": build_version,
            "key_id": "install-test-key",
            "package_id": package_id,
            "supported_architectures": ["arm64"],
            "runtimes": {
                "core": descriptor("core", b"core", "c"),
                "collector": descriptor("collector", b"coll", "d")
            }
        })
    }

    fn signed(payload: Value, signing: &SigningKey) -> Vec<u8> {
        let mut canonical = String::new();
        canonical_json(&payload, &mut canonical);
        let signature = URL_SAFE_NO_PAD.encode(signing.sign(canonical.as_bytes()).to_bytes());
        serde_json::to_vec(&json!({"payload": payload, "signature": signature})).unwrap()
    }

    fn fixture(build_version: &str) -> (VerifiedPackageManifest, SigningKey) {
        let signing = SigningKey::from_bytes(&[47_u8; 32]);
        let public_key = signing
            .verifying_key()
            .to_public_key_pem(LineEnding::LF)
            .unwrap();
        let keys = serde_json::to_vec(&json!({
            "active_key_id": "install-test-key",
            "keys": [{"key_id": "install-test-key", "public_key_pem": public_key}]
        }))
        .unwrap();
        let manifest = VerifiedPackageManifest::from_signed(
            &signed(payload(build_version, "data-scientist-community-mac-arm64"), &signing),
            &keys,
        )
        .unwrap();
        (manifest, signing)
    }

    struct BundleFixture {
        _temp: tempfile::TempDir,
        source: std::path::PathBuf,
        staged: std::path::PathBuf,
        home: std::path::PathBuf,
        manifest: Arc<VerifiedPackageManifest>,
        signing: SigningKey,
    }

    impl BundleFixture {
        fn new() -> Self {
            let temp = tempfile::tempdir().unwrap();
            let source = temp.path().join("Volumes/数据科学家 Community.app");
            let staged = temp.path().join("staged/数据科学家 Community.app");
            let home = temp.path().join("home");
            fs::create_dir(&home).unwrap();
            let (manifest, signing) = fixture("20260711");
            let manifest = Arc::new(manifest);
            Self::write_bundle(&source, manifest.signed_bytes());
            Self::write_bundle(&staged, manifest.signed_bytes());
            Self {
                _temp: temp,
                source,
                staged,
                home,
                manifest,
                signing,
            }
        }

        fn write_bundle(root: &Path, manifest: &[u8]) {
            fs::create_dir_all(root.join("Contents/MacOS")).unwrap();
            fs::create_dir_all(root.join("Contents/Resources/runtime-packs")).unwrap();
            fs::write(root.join("Contents/MacOS/data-scientist"), b"binary").unwrap();
            fs::write(
                root.join("Contents/Resources/package_manifest.json"),
                manifest,
            )
            .unwrap();
            fs::write(
                root.join("Contents/Resources/runtime-packs/core-runtime.tar.zst"),
                b"core",
            )
            .unwrap();
            fs::write(
                root.join("Contents/Resources/runtime-packs/collector-runtime.tar.zst"),
                b"coll",
            )
            .unwrap();
        }

        fn manager(&self) -> InstallManager {
            InstallManager::new(
                self.source.clone(),
                self.home.clone(),
                self.manifest.clone(),
                FaultInjection::default(),
            )
        }

        fn target(&self) -> std::path::PathBuf {
            self.home.join("Applications/数据科学家 Community.app")
        }

        fn write_target_version(&self, version: &str, marker: &[u8]) {
            let manifest = signed(payload(version, "data-scientist-community-mac-arm64"), &self.signing);
            Self::write_bundle(&self.target(), &manifest);
            fs::write(self.target().join("version-marker"), marker).unwrap();
        }
    }

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    enum FailurePoint {
        Notice,
        Copy,
        Xattr,
        Codesign,
        Register,
    }

    struct TestBackend {
        events: Arc<Mutex<Vec<String>>>,
        failure: Option<FailurePoint>,
        cancel_at: Option<InstallCheckpoint>,
        panic_at: Option<InstallCheckpoint>,
        leave_committed_cleanup: bool,
    }

    impl TestBackend {
        fn new(failure: Option<FailurePoint>) -> (Arc<Self>, Arc<Mutex<Vec<String>>>) {
            let events = Arc::new(Mutex::new(Vec::new()));
            (
                Arc::new(Self {
                    events: events.clone(),
                    failure,
                    cancel_at: None,
                    panic_at: None,
                    leave_committed_cleanup: false,
                }),
                events,
            )
        }

        fn cancel_at(checkpoint: InstallCheckpoint) -> Arc<Self> {
            Arc::new(Self {
                events: Arc::new(Mutex::new(Vec::new())),
                failure: None,
                cancel_at: Some(checkpoint),
                panic_at: None,
                leave_committed_cleanup: false,
            })
        }

        fn panic_at(checkpoint: InstallCheckpoint) -> Arc<Self> {
            Arc::new(Self {
                events: Arc::new(Mutex::new(Vec::new())),
                failure: None,
                cancel_at: None,
                panic_at: Some(checkpoint),
                leave_committed_cleanup: false,
            })
        }

        fn leave_committed_cleanup() -> Arc<Self> {
            Arc::new(Self {
                events: Arc::new(Mutex::new(Vec::new())),
                failure: None,
                cancel_at: None,
                panic_at: None,
                leave_committed_cleanup: true,
            })
        }

        fn record(&self, event: &str) {
            self.events.lock().unwrap().push(event.into());
        }
    }

    fn copy_tree(source: &Path, target: &Path) -> Result<(), String> {
        fs::create_dir_all(target).map_err(|error| error.to_string())?;
        for entry in fs::read_dir(source).map_err(|error| error.to_string())? {
            let entry = entry.map_err(|error| error.to_string())?;
            let destination = target.join(entry.file_name());
            if entry
                .file_type()
                .map_err(|error| error.to_string())?
                .is_dir()
            {
                copy_tree(&entry.path(), &destination)?;
            } else {
                fs::copy(entry.path(), destination).map_err(|error| error.to_string())?;
            }
        }
        Ok(())
    }

    impl CopyBackend for TestBackend {
        fn notify_install_start(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            self.record("notice");
            if self.failure == Some(FailurePoint::Notice) {
                Err("install_notice_failed".into())
            } else {
                Ok(())
            }
        }

        fn copy_app(
            &self,
            source: &Path,
            target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            self.record("copy");
            if self.failure == Some(FailurePoint::Copy) {
                return Err("ditto_failed".into());
            }
            copy_tree(source, target)
        }

        fn copy_app_anchored(
            &self,
            source: &Path,
            target: &AnchoredPath<'_>,
            cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            assert!(target.parent.as_raw_fd() >= 0);
            self.copy_app(source, &target.display_path, cancellation)
        }

        fn clear_quarantine(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            self.record("xattr");
            if self.failure == Some(FailurePoint::Xattr) {
                Err("xattr_clear_failed".into())
            } else {
                Ok(())
            }
        }

        fn clear_quarantine_anchored(
            &self,
            target: &AnchoredPath<'_>,
            cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            assert!(target.parent.as_raw_fd() >= 0);
            self.clear_quarantine(&target.display_path, cancellation)
        }

        fn verify_code_signature(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            self.record("codesign");
            if self.failure == Some(FailurePoint::Codesign) {
                Err("codesign_verify_failed".into())
            } else {
                Ok(())
            }
        }

        fn verify_code_signature_anchored(
            &self,
            target: &AnchoredPath<'_>,
            cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            assert!(target.parent.as_raw_fd() >= 0);
            self.verify_code_signature(&target.display_path, cancellation)
        }

        fn register_app(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            self.record("register");
            if self.failure == Some(FailurePoint::Register) {
                Err("launchservices_registration_failed".into())
            } else {
                Ok(())
            }
        }

        fn register_app_anchored(
            &self,
            target: &AnchoredPath<'_>,
            cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            assert!(target.parent.as_raw_fd() >= 0);
            self.register_app(&target.display_path, cancellation)
        }

        fn notify_install_failure(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            self.record("failure_notice");
            Ok(())
        }

        fn checkpoint(
            &self,
            checkpoint: InstallCheckpoint,
            cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            self.record(&format!("checkpoint:{checkpoint:?}"));
            if self.panic_at == Some(checkpoint) {
                panic!("simulated install crash at {checkpoint:?}");
            }
            if self.cancel_at == Some(checkpoint) {
                cancellation.cancel();
            }
            if self.leave_committed_cleanup && checkpoint == InstallCheckpoint::Committed {
                return Err("injected_committed_cleanup_failure".into());
            }
            Ok(())
        }
    }

    struct SwapAtCodesignBackend {
        replacement: std::path::PathBuf,
        saved_verified: std::path::PathBuf,
    }

    impl CopyBackend for SwapAtCodesignBackend {
        fn copy_app(
            &self,
            source: &Path,
            target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            copy_tree(source, target)
        }

        fn clear_quarantine(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            Ok(())
        }

        fn verify_code_signature(
            &self,
            target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            fs::rename(target, &self.saved_verified).unwrap();
            copy_tree(&self.replacement, target).unwrap();
            Ok(())
        }

        fn register_app(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            Ok(())
        }
    }

    struct SwapSourceAtNoticeBackend {
        source: std::path::PathBuf,
        replacement: std::path::PathBuf,
        saved_source: std::path::PathBuf,
    }

    impl CopyBackend for SwapSourceAtNoticeBackend {
        fn notify_install_start(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            fs::rename(&self.source, &self.saved_source).unwrap();
            fs::rename(&self.replacement, &self.source).unwrap();
            Ok(())
        }

        fn copy_app(
            &self,
            source: &Path,
            target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            copy_tree(source, target)
        }

        fn clear_quarantine(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            Ok(())
        }

        fn verify_code_signature(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            Ok(())
        }

        fn register_app(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            Ok(())
        }
    }

    struct SwapTargetAtCodesignBackend {
        replacement: std::path::PathBuf,
        saved_target: std::path::PathBuf,
        swapped: AtomicBool,
    }

    impl CopyBackend for SwapTargetAtCodesignBackend {
        fn copy_app(
            &self,
            source: &Path,
            target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            copy_tree(source, target)
        }

        fn clear_quarantine(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            Ok(())
        }

        fn verify_code_signature(
            &self,
            target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            if target.file_name() == Some(std::ffi::OsStr::new(super::APP_NAME))
                && !self.swapped.swap(true, AtomicOrdering::AcqRel)
            {
                fs::rename(target, &self.saved_target).unwrap();
                copy_tree(&self.replacement, target).unwrap();
            }
            Ok(())
        }

        fn register_app(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            Ok(())
        }
    }

    struct SwapTargetAtRegisterBackend {
        replacement: std::path::PathBuf,
        saved_target: std::path::PathBuf,
    }

    impl CopyBackend for SwapTargetAtRegisterBackend {
        fn copy_app(
            &self,
            source: &Path,
            target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            copy_tree(source, target)
        }

        fn clear_quarantine(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            Ok(())
        }

        fn verify_code_signature(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            Ok(())
        }

        fn register_app(
            &self,
            target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            fs::rename(target, &self.saved_target).unwrap();
            copy_tree(&self.replacement, target).unwrap();
            Ok(())
        }
    }

    struct SwapApplicationsAtNoticeBackend {
        applications: std::path::PathBuf,
        held: std::path::PathBuf,
        outside: std::path::PathBuf,
    }

    impl CopyBackend for SwapApplicationsAtNoticeBackend {
        fn notify_install_start(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            fs::rename(&self.applications, &self.held).unwrap();
            symlink(&self.outside, &self.applications).unwrap();
            Ok(())
        }

        fn copy_app(
            &self,
            _source: &Path,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            panic!("copy must not run after Applications root changes")
        }

        fn clear_quarantine(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            panic!("quarantine must not run after Applications root changes")
        }

        fn verify_code_signature(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            panic!("codesign must not run after Applications root changes")
        }

        fn register_app(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            panic!("registration must not run after Applications root changes")
        }
    }

    struct SwapApplicationsAtRegisterBackend {
        applications: std::path::PathBuf,
        held: std::path::PathBuf,
        outside: std::path::PathBuf,
    }

    impl CopyBackend for SwapApplicationsAtRegisterBackend {
        fn copy_app(
            &self,
            source: &Path,
            target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            copy_tree(source, target)
        }

        fn clear_quarantine(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            Ok(())
        }

        fn verify_code_signature(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            Ok(())
        }

        fn register_app(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            fs::rename(&self.applications, &self.held).unwrap();
            symlink(&self.outside, &self.applications).unwrap();
            Ok(())
        }
    }

    struct SwapApplicationsAfterCopyBackend {
        applications: std::path::PathBuf,
        held: std::path::PathBuf,
        outside: std::path::PathBuf,
    }

    impl CopyBackend for SwapApplicationsAfterCopyBackend {
        fn copy_app(
            &self,
            source: &Path,
            target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            copy_tree(source, target)?;
            fs::rename(&self.applications, &self.held).unwrap();
            symlink(&self.outside, &self.applications).unwrap();
            Ok(())
        }

        fn clear_quarantine(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            panic!("quarantine must not run after Applications root changes")
        }

        fn verify_code_signature(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            panic!("codesign must not run after Applications root changes")
        }

        fn register_app(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            panic!("registration must not run after Applications root changes")
        }
    }

    struct SwapBackupAtCheckpointBackend {
        applications: std::path::PathBuf,
        replacement: std::path::PathBuf,
        saved_backup: std::path::PathBuf,
        swapped: AtomicBool,
    }

    impl CopyBackend for SwapBackupAtCheckpointBackend {
        fn copy_app(
            &self,
            source: &Path,
            target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            copy_tree(source, target)
        }

        fn clear_quarantine(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            Ok(())
        }

        fn verify_code_signature(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            Ok(())
        }

        fn register_app(
            &self,
            _target: &Path,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            Ok(())
        }

        fn checkpoint(
            &self,
            checkpoint: InstallCheckpoint,
            _cancellation: &InstallCancellation,
        ) -> Result<(), String> {
            if checkpoint == InstallCheckpoint::BackupMoved
                && !self.swapped.swap(true, AtomicOrdering::AcqRel)
            {
                let backup = fs::read_dir(&self.applications)
                    .unwrap()
                    .flatten()
                    .find(|entry| {
                        entry
                            .file_name()
                            .to_string_lossy()
                            .starts_with(super::BACKUP_PREFIX)
                    })
                    .unwrap()
                    .path();
                fs::rename(&backup, &self.saved_backup).unwrap();
                copy_tree(&self.replacement, &backup).unwrap();
                return Err("checkpoint_failed".into());
            }
            Ok(())
        }
    }

    fn manager_with_backend(
        fixture: &BundleFixture,
        backend: Arc<dyn CopyBackend>,
    ) -> InstallManager {
        InstallManager::with_backend(
            fixture.source.clone(),
            fixture.home.clone(),
            fixture.manifest.clone(),
            FaultInjection::default(),
            backend,
        )
    }

    fn transaction_artifacts(fixture: &BundleFixture) -> Vec<String> {
        let applications = fixture.home.join("Applications");
        let Ok(entries) = fs::read_dir(applications) else {
            return Vec::new();
        };
        entries
            .flatten()
            .map(|entry| entry.file_name().to_string_lossy().into_owned())
            .filter(|name| {
                name.contains("installing-")
                    || name.contains("previous-")
                    || name.ends_with("install.json")
            })
            .collect()
    }

    #[test]
    fn chooses_only_real_applications_directories_as_already_installed() {
        let home = Path::new("/tmp/community-user");
        assert_eq!(
            choose_target(Path::new("/Applications/数据科学家 Community.app"), home),
            InstallTarget::AlreadyInstalled
        );
        assert_eq!(
            choose_target(Path::new("/tmp/community-user/Applications/数据科学家 Community.app"), home,),
            InstallTarget::AlreadyInstalled
        );
        assert_eq!(
            choose_target(Path::new("/Volumes/Installer/数据科学家 Community.app"), home),
            InstallTarget::CopyTo(Path::new("/tmp/community-user/Applications/数据科学家 Community.app").into())
        );
        assert!(matches!(
            choose_target(Path::new("/Applications-evil/数据科学家 Community.app"), home),
            InstallTarget::CopyTo(_)
        ));

        assert!(matches!(
            choose_target(
                Path::new("/tmp/community-user/Applications/../Downloads/数据科学家 Community.app"),
                home,
            ),
            InstallTarget::CopyTo(_)
        ));

        let temp = tempfile::tempdir().unwrap();
        let real_home = temp.path().join("home");
        let applications = real_home.join("Applications");
        let outside = temp.path().join("outside/数据科学家 Community.app");
        fs::create_dir_all(&applications).unwrap();
        fs::create_dir_all(&outside).unwrap();
        let source_link = applications.join("数据科学家 Community.app");
        symlink(&outside, &source_link).unwrap();
        assert!(matches!(
            choose_target(&source_link, &real_home),
            InstallTarget::CopyTo(_)
        ));
    }

    #[test]
    fn compares_only_strict_numeric_date_build_versions() {
        assert_eq!(
            compare_build_versions("20260711", "20260710.9").unwrap(),
            Ordering::Greater
        );
        assert_eq!(
            compare_build_versions("20260711.2", "20260711.1").unwrap(),
            Ordering::Greater
        );
        assert_eq!(
            compare_build_versions("20260711", "20260711.0").unwrap(),
            Ordering::Equal
        );
        for invalid in [
            "",
            "1.0.7",
            "2026071",
            "20260711.",
            "20260711.1.2",
            "2026a711",
        ] {
            assert!(
                compare_build_versions(invalid, "20260711").is_err(),
                "{invalid}"
            );
        }
    }

    #[test]
    fn newer_and_exact_same_verified_targets_are_never_replaced() {
        let (current, signing) = fixture("20260711");
        let newer = signed(payload("20260712", "data-scientist-community-mac-arm64"), &signing);
        assert_eq!(
            classify_existing_target(&current, &newer).unwrap(),
            ExistingTargetDecision::Keep
        );
        assert_eq!(
            classify_existing_target(&current, current.signed_bytes()).unwrap(),
            ExistingTargetDecision::Keep
        );
    }

    #[test]
    fn only_a_strictly_older_verified_target_can_be_replaced() {
        let (current, signing) = fixture("20260711");
        let older = signed(payload("20260710.9", "data-scientist-community-mac-arm64"), &signing);
        assert_eq!(
            classify_existing_target(&current, &older).unwrap(),
            ExistingTargetDecision::Replace
        );
    }

    #[test]
    fn same_version_with_different_envelope_is_a_collision() {
        let (current, _) = fixture("20260711");
        let envelope: Value = serde_json::from_slice(current.signed_bytes()).unwrap();
        let different_bytes = serde_json::to_vec_pretty(&envelope).unwrap();
        assert_ne!(different_bytes, current.signed_bytes());
        assert_eq!(
            classify_existing_target(&current, &different_bytes).unwrap_err(),
            "installed_build_collision"
        );
    }

    #[test]
    fn foreign_invalid_and_uncomparable_targets_fail_closed() {
        let (current, signing) = fixture("20260711");
        let foreign = signed(payload("20260712", "another-package"), &signing);
        assert_eq!(
            classify_existing_target(&current, &foreign).unwrap_err(),
            "installed_package_mismatch"
        );
        assert_eq!(
            classify_existing_target(&current, b"not-signed-json").unwrap_err(),
            "installed_manifest_invalid"
        );
        let semver = signed(payload("1.0.7", "data-scientist-community-mac-arm64"), &signing);
        assert_eq!(
            classify_existing_target(&current, &semver).unwrap_err(),
            "installed_build_version_invalid"
        );
    }

    #[test]
    fn exact_verified_source_and_staged_bundles_pass_file_verification() {
        let fixture = BundleFixture::new();
        fixture
            .manager()
            .verify_copy(&fixture.source, &fixture.staged)
            .unwrap();
    }

    #[test]
    fn source_manifest_replacement_after_verification_fails_closed() {
        let fixture = BundleFixture::new();
        let envelope: Value = serde_json::from_slice(fixture.manifest.signed_bytes()).unwrap();
        let replacement = serde_json::to_vec_pretty(&envelope).unwrap();
        fs::write(
            fixture
                .source
                .join("Contents/Resources/package_manifest.json"),
            &replacement,
        )
        .unwrap();
        fs::write(
            fixture
                .staged
                .join("Contents/Resources/package_manifest.json"),
            replacement,
        )
        .unwrap();

        let error = fixture
            .manager()
            .verify_copy(&fixture.source, &fixture.staged)
            .unwrap_err();
        assert!(
            error == "install_manifest_bytes_mismatch"
                || error.starts_with("install_file_size_mismatch:")
        );
    }

    #[test]
    fn runtime_pack_symlink_fifo_and_wrong_size_are_rejected_without_blocking() {
        let symlink_fixture = BundleFixture::new();
        let core = symlink_fixture
            .staged
            .join("Contents/Resources/runtime-packs/core-runtime.tar.zst");
        let outside = symlink_fixture._temp.path().join("outside-pack");
        fs::write(&outside, b"core").unwrap();
        fs::remove_file(&core).unwrap();
        symlink(&outside, &core).unwrap();
        let started = Instant::now();
        assert!(symlink_fixture
            .manager()
            .verify_copy(&symlink_fixture.source, &symlink_fixture.staged)
            .unwrap_err()
            .contains("core-runtime.tar.zst"));
        assert!(started.elapsed() < Duration::from_secs(1));

        let fifo_fixture = BundleFixture::new();
        let collector = fifo_fixture
            .staged
            .join("Contents/Resources/runtime-packs/collector-runtime.tar.zst");
        fs::remove_file(&collector).unwrap();
        assert!(Command::new("/usr/bin/mkfifo")
            .arg(&collector)
            .status()
            .unwrap()
            .success());
        let started = Instant::now();
        assert!(fifo_fixture
            .manager()
            .verify_copy(&fifo_fixture.source, &fifo_fixture.staged)
            .unwrap_err()
            .contains("collector-runtime.tar.zst"));
        assert!(started.elapsed() < Duration::from_secs(1));

        let size_fixture = BundleFixture::new();
        fs::write(
            size_fixture
                .staged
                .join("Contents/Resources/runtime-packs/core-runtime.tar.zst"),
            b"oversized",
        )
        .unwrap();
        assert!(size_fixture
            .manager()
            .verify_copy(&size_fixture.source, &size_fixture.staged)
            .unwrap_err()
            .contains("core-runtime.tar.zst"));
    }

    #[test]
    fn executable_symlink_is_rejected_without_reading_its_target() {
        let fixture = BundleFixture::new();
        let binary = fixture.staged.join("Contents/MacOS/data-scientist");
        let outside = fixture._temp.path().join("outside-binary");
        fs::write(&outside, b"binary").unwrap();
        fs::remove_file(&binary).unwrap();
        symlink(&outside, &binary).unwrap();
        assert!(fixture
            .manager()
            .verify_copy(&fixture.source, &fixture.staged)
            .unwrap_err()
            .contains("Contents/MacOS/data-scientist"));
    }

    #[test]
    fn bundle_directory_symlink_is_rejected_without_touching_external_contents() {
        let fixture = BundleFixture::new();
        let resources = fixture.staged.join("Contents/Resources");
        let outside = fixture._temp.path().join("outside-resources");
        fs::rename(&resources, &outside).unwrap();
        symlink(&outside, &resources).unwrap();

        assert_eq!(
            fixture
                .manager()
                .verify_copy(&fixture.source, &fixture.staged)
                .unwrap_err(),
            "install_directory_invalid:Contents/Resources"
        );
        assert!(outside.join("package_manifest.json").is_file());
    }

    #[test]
    fn successful_install_commits_verified_target_and_removes_transaction_artifacts() {
        let fixture = BundleFixture::new();
        let (backend, events) = TestBackend::new(None);
        let outcome = manager_with_backend(&fixture, backend).install();

        assert_eq!(outcome, InstallOutcome::Installed(fixture.target()));
        fixture
            .manager()
            .verify_copy(&fixture.source, &fixture.target())
            .unwrap();
        assert!(transaction_artifacts(&fixture).is_empty());
        assert_eq!(
            *events.lock().unwrap(),
            [
                "notice",
                "copy",
                "checkpoint:Copied",
                "xattr",
                "codesign",
                "checkpoint:TargetRenamedBeforeJournal",
                "checkpoint:TargetSwitched",
                "register",
                "codesign",
                "checkpoint:Committed",
            ]
        );
    }

    #[test]
    fn native_install_notice_failure_stops_before_copy() {
        let fixture = BundleFixture::new();
        let (backend, events) = TestBackend::new(Some(FailurePoint::Notice));

        assert_eq!(
            manager_with_backend(&fixture, backend).install(),
            InstallOutcome::Failed("install_notice_failed".into())
        );
        assert_eq!(*events.lock().unwrap(), ["notice"]);
        assert!(!fixture.target().exists());
        assert!(transaction_artifacts(&fixture).is_empty());
    }

    #[test]
    fn applications_root_swap_at_notice_is_rejected_before_copying_outside() {
        let fixture = BundleFixture::new();
        let applications = fixture.home.join("Applications");
        let held = fixture.home.join("Applications-held");
        let outside = fixture._temp.path().join("outside-applications");
        fs::create_dir(&outside).unwrap();
        fs::write(outside.join("sentinel"), b"outside").unwrap();
        let backend = Arc::new(SwapApplicationsAtNoticeBackend {
            applications,
            held: held.clone(),
            outside: outside.clone(),
        });

        assert_eq!(
            manager_with_backend(&fixture, backend).install(),
            InstallOutcome::Failed("install_applications_changed".into())
        );
        assert_eq!(fs::read(outside.join("sentinel")).unwrap(), b"outside");
        assert_eq!(fs::read_dir(&outside).unwrap().count(), 1);
        assert!(!fs::read_dir(&held).unwrap().flatten().any(|entry| entry
            .file_name()
            .to_string_lossy()
            .starts_with(super::STAGING_PREFIX)));
    }

    #[test]
    fn applications_root_swap_during_registration_rolls_back_held_transaction() {
        let fixture = BundleFixture::new();
        fixture.write_target_version("20260710", b"old");
        let applications = fixture.home.join("Applications");
        let held = fixture.home.join("Applications-held");
        let outside = fixture._temp.path().join("outside-register");
        fs::create_dir(&outside).unwrap();
        fs::write(outside.join("sentinel"), b"outside").unwrap();
        let backend = Arc::new(SwapApplicationsAtRegisterBackend {
            applications,
            held: held.clone(),
            outside: outside.clone(),
        });

        assert_eq!(
            manager_with_backend(&fixture, backend).install(),
            InstallOutcome::Failed("install_applications_changed".into())
        );
        assert_eq!(
            fs::read(held.join(super::APP_NAME).join("version-marker")).unwrap(),
            b"old"
        );
        assert_eq!(fs::read(outside.join("sentinel")).unwrap(), b"outside");
        assert_eq!(fs::read_dir(&outside).unwrap().count(), 1);
        assert!(!fs::read_dir(&held).unwrap().flatten().any(|entry| {
            let name = entry.file_name().to_string_lossy().into_owned();
            name.starts_with(super::STAGING_PREFIX)
                || name.starts_with(super::BACKUP_PREFIX)
                || name.contains("install.json")
        }));
    }

    #[test]
    fn applications_root_swap_after_copy_cleans_held_staging_without_touching_outside() {
        let fixture = BundleFixture::new();
        let applications = fixture.home.join("Applications");
        let held = fixture.home.join("Applications-held");
        let outside = fixture._temp.path().join("outside-after-copy");
        fs::create_dir(&outside).unwrap();
        fs::write(outside.join("sentinel"), b"outside").unwrap();
        let backend = Arc::new(SwapApplicationsAfterCopyBackend {
            applications,
            held: held.clone(),
            outside: outside.clone(),
        });

        assert_eq!(
            manager_with_backend(&fixture, backend).install(),
            InstallOutcome::Failed("install_applications_changed".into())
        );
        assert_eq!(fs::read(outside.join("sentinel")).unwrap(), b"outside");
        assert_eq!(fs::read_dir(&outside).unwrap().count(), 1);
        assert!(!fs::read_dir(&held).unwrap().flatten().any(|entry| entry
            .file_name()
            .to_string_lossy()
            .starts_with(super::STAGING_PREFIX)));
    }

    #[test]
    fn backup_swap_before_rollback_is_rejected_instead_of_restored() {
        let fixture = BundleFixture::new();
        fixture.write_target_version("20260710", b"old");
        let replacement = fixture._temp.path().join("replacement-backup.app");
        let older = signed(payload("20260710", "data-scientist-community-mac-arm64"), &fixture.signing);
        BundleFixture::write_bundle(&replacement, &older);
        fs::write(replacement.join("version-marker"), b"replacement").unwrap();
        fs::write(
            replacement.join("Contents/MacOS/data-scientist"),
            b"replacement-binary",
        )
        .unwrap();
        let saved_backup = fixture._temp.path().join("saved-original-backup.app");
        let backend = Arc::new(SwapBackupAtCheckpointBackend {
            applications: fixture.home.join("Applications"),
            replacement,
            saved_backup: saved_backup.clone(),
            swapped: AtomicBool::new(false),
        });

        let outcome = manager_with_backend(&fixture, backend).install();
        assert!(
            matches!(outcome, InstallOutcome::Failed(ref error) if error.contains("install_entry_changed")),
            "{outcome:?}"
        );
        assert_eq!(
            fs::read(saved_backup.join("version-marker")).unwrap(),
            b"old"
        );
        assert!(
            !fixture.target().exists()
                || fs::read(fixture.target().join("version-marker")).unwrap() != b"replacement"
        );
    }

    #[test]
    fn staging_root_swap_after_codesign_is_rejected_before_target_rename() {
        let fixture = BundleFixture::new();
        let replacement = fixture._temp.path().join("attacker-replacement.app");
        BundleFixture::write_bundle(&replacement, fixture.manifest.signed_bytes());
        fs::write(
            replacement.join("Contents/MacOS/data-scientist"),
            b"evil-binary",
        )
        .unwrap();
        fs::write(replacement.join("attacker-sentinel"), b"preserve").unwrap();
        let saved_verified = fixture._temp.path().join("verified-staging.app");
        let backend = Arc::new(SwapAtCodesignBackend {
            replacement,
            saved_verified: saved_verified.clone(),
        });

        let outcome = manager_with_backend(&fixture, backend).install();
        assert!(
            matches!(outcome, InstallOutcome::Failed(ref error) if error.contains("install_entry_changed")),
            "{outcome:?}"
        );
        assert!(!fixture.target().exists());
        assert_eq!(
            fs::read(saved_verified.join("Contents/MacOS/data-scientist")).unwrap(),
            b"binary"
        );
        let applications = fixture.home.join("Applications");
        let staged = fs::read_dir(&applications)
            .unwrap()
            .flatten()
            .find(|entry| {
                entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with(super::STAGING_PREFIX)
            })
            .expect("changed staging must be retained for fail-closed recovery");
        assert_eq!(
            fs::read(staged.path().join("attacker-sentinel")).unwrap(),
            b"preserve"
        );
    }

    #[test]
    fn source_root_swap_after_initial_verification_is_rejected_before_copy_commit() {
        let fixture = BundleFixture::new();
        let replacement = fixture._temp.path().join("replacement-source.app");
        BundleFixture::write_bundle(&replacement, fixture.manifest.signed_bytes());
        fs::write(
            replacement.join("Contents/MacOS/data-scientist"),
            b"different-signed-binary",
        )
        .unwrap();
        let saved_source = fixture._temp.path().join("saved-source.app");
        let backend = Arc::new(SwapSourceAtNoticeBackend {
            source: fixture.source.clone(),
            replacement,
            saved_source: saved_source.clone(),
        });

        let outcome = manager_with_backend(&fixture, backend).install();
        assert!(
            matches!(outcome, InstallOutcome::Failed(ref error) if error.contains("installed_binary_mismatch")),
            "{outcome:?}"
        );
        assert!(!fixture.target().exists());
        assert_eq!(
            fs::read(saved_source.join("Contents/MacOS/data-scientist")).unwrap(),
            b"binary"
        );
    }

    #[test]
    fn existing_target_swap_during_codesign_is_rejected_before_backup() {
        let fixture = BundleFixture::new();
        fixture.write_target_version("20260710", b"old");
        let replacement = fixture._temp.path().join("replacement-target.app");
        let older = signed(payload("20260710", "data-scientist-community-mac-arm64"), &fixture.signing);
        BundleFixture::write_bundle(&replacement, &older);
        fs::write(replacement.join("version-marker"), b"replacement").unwrap();
        let saved_target = fixture._temp.path().join("saved-target.app");
        let backend = Arc::new(SwapTargetAtCodesignBackend {
            replacement,
            saved_target: saved_target.clone(),
            swapped: AtomicBool::new(false),
        });

        let outcome = manager_with_backend(&fixture, backend).install();
        assert!(
            matches!(outcome, InstallOutcome::Failed(ref error) if error.contains("install_entry_changed")),
            "{outcome:?}"
        );
        assert_eq!(
            fs::read(saved_target.join("version-marker")).unwrap(),
            b"old"
        );
    }

    #[test]
    fn target_swap_during_registration_is_rejected_and_old_target_is_restored() {
        let fixture = BundleFixture::new();
        fixture.write_target_version("20260710", b"old");
        let replacement = fixture._temp.path().join("replacement-at-register.app");
        BundleFixture::write_bundle(&replacement, fixture.manifest.signed_bytes());
        fs::write(
            replacement.join("Contents/MacOS/data-scientist"),
            b"attacker-binary",
        )
        .unwrap();
        let saved_target = fixture._temp.path().join("verified-new-target.app");
        let backend = Arc::new(SwapTargetAtRegisterBackend {
            replacement,
            saved_target: saved_target.clone(),
        });

        let outcome = manager_with_backend(&fixture, backend).install();
        assert!(
            matches!(outcome, InstallOutcome::Failed(ref error) if error.contains("install_entry_changed")),
            "{outcome:?}"
        );
        assert_eq!(
            fs::read(fixture.target().join("version-marker")).unwrap(),
            b"old"
        );
        assert_eq!(
            fs::read(saved_target.join("Contents/MacOS/data-scientist")).unwrap(),
            b"binary"
        );
    }

    #[test]
    fn unknown_transaction_artifacts_fail_closed_and_are_never_deleted() {
        for (name, directory_artifact) in [
            (format!("{}unknown", super::STAGING_PREFIX), true),
            (format!("{}unknown", super::BACKUP_PREFIX), true),
            (format!("{}.tmp-unknown", super::JOURNAL_NAME), false),
        ] {
            let fixture = BundleFixture::new();
            let applications = fixture.home.join("Applications");
            fs::create_dir(&applications).unwrap();
            let orphan = applications.join(name);
            if directory_artifact {
                fs::create_dir(&orphan).unwrap();
                fs::write(orphan.join("sentinel"), b"outside").unwrap();
            } else {
                fs::write(&orphan, b"outside").unwrap();
            }
            let (backend, events) = TestBackend::new(None);

            assert_eq!(
                manager_with_backend(&fixture, backend).install(),
                InstallOutcome::Failed("install_orphan_transaction_artifact".into())
            );
            let sentinel = if directory_artifact {
                orphan.join("sentinel")
            } else {
                orphan
            };
            assert_eq!(fs::read(sentinel).unwrap(), b"outside");
            assert!(events.lock().unwrap().is_empty());
        }
    }

    #[test]
    fn recovery_restores_valid_backup_even_when_switched_target_is_corrupt() {
        let fixture = BundleFixture::new();
        fixture.write_target_version("20260710", b"old");
        let directory = super::applications_directory(&fixture.home).unwrap();
        let mut journal = super::InstallJournal::new(&fixture.manifest, true);
        journal.phase = super::TransactionPhase::TargetSwitched;
        super::rename_entry(&directory, super::APP_NAME, &journal.backup_name).unwrap();
        fs::create_dir(directory.path.join(super::APP_NAME)).unwrap();
        fs::write(
            directory.path.join(super::APP_NAME).join("corrupt"),
            b"corrupt",
        )
        .unwrap();
        super::write_journal(&directory, &journal).unwrap();
        drop(directory);

        let (failing_copy, _) = TestBackend::new(Some(FailurePoint::Copy));
        assert!(matches!(
            manager_with_backend(&fixture, failing_copy).install(),
            InstallOutcome::Failed(_)
        ));
        assert_eq!(
            fs::read(fixture.target().join("version-marker")).unwrap(),
            b"old"
        );
        assert!(transaction_artifacts(&fixture).is_empty());
    }

    #[test]
    fn recovery_removes_corrupt_fresh_uncommitted_target() {
        let fixture = BundleFixture::new();
        let directory = super::applications_directory(&fixture.home).unwrap();
        let mut journal = super::InstallJournal::new(&fixture.manifest, false);
        journal.phase = super::TransactionPhase::TargetSwitched;
        fs::create_dir(directory.path.join(super::APP_NAME)).unwrap();
        fs::write(
            directory.path.join(super::APP_NAME).join("corrupt"),
            b"corrupt",
        )
        .unwrap();
        super::write_journal(&directory, &journal).unwrap();
        drop(directory);

        let (failing_copy, _) = TestBackend::new(Some(FailurePoint::Copy));
        assert!(matches!(
            manager_with_backend(&fixture, failing_copy).install(),
            InstallOutcome::Failed(_)
        ));
        assert!(!fixture.target().exists());
        assert!(transaction_artifacts(&fixture).is_empty());
    }

    #[test]
    fn old_dmg_never_replaces_a_newer_verified_target() {
        let fixture = BundleFixture::new();
        fixture.write_target_version("20260712", b"newer");
        let (backend, events) = TestBackend::new(None);

        assert_eq!(
            manager_with_backend(&fixture, backend).install(),
            InstallOutcome::AlreadyInstalled
        );
        assert_eq!(
            fs::read(fixture.target().join("version-marker")).unwrap(),
            b"newer"
        );
        assert_eq!(*events.lock().unwrap(), ["codesign"]);
    }

    #[test]
    fn copy_xattr_codesign_and_registration_failures_preserve_old_target() {
        for failure in [
            FailurePoint::Copy,
            FailurePoint::Xattr,
            FailurePoint::Codesign,
            FailurePoint::Register,
        ] {
            let fixture = BundleFixture::new();
            fixture.write_target_version("20260710", b"old");
            let (backend, _) = TestBackend::new(Some(failure));
            let outcome = manager_with_backend(&fixture, backend).install();
            assert!(
                matches!(outcome, InstallOutcome::Failed(_)),
                "{failure:?}: {outcome:?}"
            );
            assert_eq!(
                fs::read(fixture.target().join("version-marker")).unwrap(),
                b"old",
                "{failure:?}"
            );
            assert!(transaction_artifacts(&fixture).is_empty(), "{failure:?}");
        }
    }

    #[test]
    fn cancellation_after_copy_backup_and_switch_always_restores_old_target() {
        for checkpoint in [
            InstallCheckpoint::Copied,
            InstallCheckpoint::BackupMoved,
            InstallCheckpoint::TargetSwitched,
        ] {
            let fixture = BundleFixture::new();
            fixture.write_target_version("20260710", b"old");
            let outcome =
                manager_with_backend(&fixture, TestBackend::cancel_at(checkpoint)).install();
            assert_eq!(
                outcome,
                InstallOutcome::Failed("install_cancelled".into()),
                "{checkpoint:?}"
            );
            assert_eq!(
                fs::read(fixture.target().join("version-marker")).unwrap(),
                b"old",
                "{checkpoint:?}"
            );
            assert!(transaction_artifacts(&fixture).is_empty(), "{checkpoint:?}");
        }
    }

    #[test]
    fn interrupted_backup_and_switch_phases_restore_old_target_before_retry() {
        for checkpoint in [
            InstallCheckpoint::BackupRenamedBeforeJournal,
            InstallCheckpoint::BackupMoved,
            InstallCheckpoint::TargetRenamedBeforeJournal,
            InstallCheckpoint::TargetSwitched,
        ] {
            let fixture = BundleFixture::new();
            fixture.write_target_version("20260710", b"old");
            let crashed = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                manager_with_backend(&fixture, TestBackend::panic_at(checkpoint)).install()
            }));
            assert!(crashed.is_err(), "{checkpoint:?}");
            assert!(!transaction_artifacts(&fixture).is_empty());

            let (failing_copy, _) = TestBackend::new(Some(FailurePoint::Copy));
            assert!(matches!(
                manager_with_backend(&fixture, failing_copy).install(),
                InstallOutcome::Failed(_)
            ));
            assert_eq!(
                fs::read(fixture.target().join("version-marker")).unwrap(),
                b"old",
                "{checkpoint:?}"
            );
            assert!(transaction_artifacts(&fixture).is_empty(), "{checkpoint:?}");
        }
    }

    #[test]
    fn committed_cleanup_from_older_version_does_not_skip_the_next_update() {
        let fixture = BundleFixture::new();
        fixture.write_target_version("20260710", b"old");
        assert_eq!(
            manager_with_backend(&fixture, TestBackend::leave_committed_cleanup()).install(),
            InstallOutcome::Installed(fixture.target())
        );
        assert!(!transaction_artifacts(&fixture).is_empty());

        let next_signed = signed(payload("20260712", "data-scientist-community-mac-arm64"), &fixture.signing);
        let public_key = fixture
            .signing
            .verifying_key()
            .to_public_key_pem(LineEnding::LF)
            .unwrap();
        let keys = serde_json::to_vec(&json!({
            "active_key_id": "install-test-key",
            "keys": [{"key_id": "install-test-key", "public_key_pem": public_key}]
        }))
        .unwrap();
        let next_manifest =
            Arc::new(VerifiedPackageManifest::from_signed(&next_signed, &keys).unwrap());
        BundleFixture::write_bundle(&fixture.source, next_manifest.signed_bytes());
        let (backend, _) = TestBackend::new(None);
        assert_eq!(
            InstallManager::with_backend(
                fixture.source.clone(),
                fixture.home.clone(),
                next_manifest.clone(),
                FaultInjection::default(),
                backend,
            )
            .install(),
            InstallOutcome::Installed(fixture.target())
        );
        assert!(transaction_artifacts(&fixture).is_empty());
        assert_eq!(
            fs::read(
                fixture
                    .target()
                    .join("Contents/Resources/package_manifest.json"),
            )
            .unwrap(),
            next_manifest.signed_bytes()
        );
    }

    #[test]
    fn a_real_second_process_install_lock_blocks_before_copy_or_cleanup() {
        let fixture = BundleFixture::new();
        let applications = fixture.home.join("Applications");
        fs::create_dir(&applications).unwrap();
        let lock = applications.join(super::LOCK_NAME);
        let marker = fixture._temp.path().join("lock-held");
        let code = "import fcntl,os,sys,time\nf=open(sys.argv[1],'a+')\nos.chmod(sys.argv[1],0o600)\nfcntl.flock(f,fcntl.LOCK_EX)\nopen(sys.argv[2],'w').write('held')\ntime.sleep(30)\n";
        let python =
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../.venv/bin/python");
        let mut holder = Command::new(python)
            .args(["-c", code])
            .arg(&lock)
            .arg(&marker)
            .spawn()
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        while !marker.exists() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
        assert!(marker.exists());
        let (backend, events) = TestBackend::new(None);
        assert_eq!(
            manager_with_backend(&fixture, backend).install(),
            InstallOutcome::Failed("install_already_running".into())
        );
        assert!(events.lock().unwrap().is_empty());
        holder.kill().unwrap();
        holder.wait().unwrap();
    }

    #[test]
    fn applications_target_lock_and_journal_links_fail_closed() {
        let applications_link = BundleFixture::new();
        let outside_directory = applications_link._temp.path().join("outside-applications");
        fs::create_dir(&outside_directory).unwrap();
        fs::write(outside_directory.join("sentinel"), b"outside").unwrap();
        symlink(
            &outside_directory,
            applications_link.home.join("Applications"),
        )
        .unwrap();
        assert!(matches!(
            applications_link.manager().install(),
            InstallOutcome::Failed(error) if error == "install_applications_invalid"
        ));
        assert_eq!(
            fs::read(outside_directory.join("sentinel")).unwrap(),
            b"outside"
        );

        for hardlink in [false, true] {
            let fixture = BundleFixture::new();
            let applications = fixture.home.join("Applications");
            fs::create_dir(&applications).unwrap();
            let outside = fixture._temp.path().join(if hardlink {
                "outside-lock-hardlink"
            } else {
                "outside-lock-symlink"
            });
            fs::write(&outside, b"outside").unwrap();
            fs::set_permissions(&outside, fs::Permissions::from_mode(0o600)).unwrap();
            if hardlink {
                fs::hard_link(&outside, applications.join(super::LOCK_NAME)).unwrap();
            } else {
                symlink(&outside, applications.join(super::LOCK_NAME)).unwrap();
            }
            assert!(matches!(
                fixture.manager().install(),
                InstallOutcome::Failed(error) if error == "install_lock_invalid"
            ));
            assert_eq!(fs::read(&outside).unwrap(), b"outside");
        }

        for hardlink in [false, true] {
            let fixture = BundleFixture::new();
            let applications = fixture.home.join("Applications");
            fs::create_dir(&applications).unwrap();
            let outside = fixture._temp.path().join(if hardlink {
                "outside-journal-hardlink"
            } else {
                "outside-journal-symlink"
            });
            fs::write(&outside, b"outside").unwrap();
            fs::set_permissions(&outside, fs::Permissions::from_mode(0o600)).unwrap();
            if hardlink {
                fs::hard_link(&outside, applications.join(super::JOURNAL_NAME)).unwrap();
            } else {
                symlink(&outside, applications.join(super::JOURNAL_NAME)).unwrap();
            }
            assert!(matches!(
                fixture.manager().install(),
                InstallOutcome::Failed(error) if error == "install_journal_invalid"
            ));
            assert_eq!(fs::read(&outside).unwrap(), b"outside");
        }

        let target_link = BundleFixture::new();
        let applications = target_link.home.join("Applications");
        fs::create_dir(&applications).unwrap();
        let outside_target = target_link._temp.path().join("outside-target");
        fs::create_dir(&outside_target).unwrap();
        fs::write(outside_target.join("sentinel"), b"outside").unwrap();
        symlink(&outside_target, target_link.target()).unwrap();
        assert!(matches!(
            target_link.manager().install(),
            InstallOutcome::Failed(error) if error == "install_entry_invalid"
        ));
        assert_eq!(
            fs::read(outside_target.join("sentinel")).unwrap(),
            b"outside"
        );
    }

    #[test]
    fn home_ancestor_symlink_is_rejected_without_creating_applications_outside() {
        let fixture = BundleFixture::new();
        let outside = fixture._temp.path().join("outside-home-parent");
        let outside_home = outside.join("home");
        fs::create_dir_all(&outside_home).unwrap();
        fs::write(outside_home.join("sentinel"), b"outside").unwrap();
        let linked_parent = fixture._temp.path().join("linked-parent");
        symlink(&outside, &linked_parent).unwrap();
        let linked_home = linked_parent.join("home");
        let (backend, events) = TestBackend::new(None);
        let manager = InstallManager::with_backend(
            fixture.source.clone(),
            linked_home,
            fixture.manifest.clone(),
            FaultInjection::default(),
            backend,
        );

        assert_eq!(
            manager.install(),
            InstallOutcome::Failed("install_home_invalid".into())
        );
        assert_eq!(fs::read(outside_home.join("sentinel")).unwrap(), b"outside");
        assert!(!outside_home.join("Applications").exists());
        assert!(events.lock().unwrap().is_empty());
    }

    #[test]
    fn install_manager_exposes_shared_resettable_cancellation() {
        let temp = tempfile::tempdir().unwrap();
        let (verified, _) = fixture("20260711");
        let manager = InstallManager::new(
            temp.path().join("source.app"),
            temp.path().to_path_buf(),
            Arc::new(verified),
            FaultInjection::default(),
        );
        let clone = manager.clone();
        let cancellation = manager.cancellation();

        clone.cancel();
        assert!(cancellation.is_cancelled());
        manager.reset();
        assert!(!clone.cancellation().is_cancelled());
        assert!(manager.wait_for_idle(Duration::from_secs(1)));
    }

    #[test]
    fn cancelling_external_command_kills_and_reaps_its_process_group() {
        let temp = tempfile::tempdir().unwrap();
        let pid_file = temp.path().join("child.pid");
        let cancellation = InstallCancellation::new();
        let worker_cancellation = cancellation.clone();
        let mut command = Command::new("/bin/sh");
        command
            .env("PID_FILE", &pid_file)
            .arg("-c")
            .arg("echo $$ > \"$PID_FILE\"; exec /bin/sleep 30");
        let worker =
            thread::spawn(move || run_cancellable_command(&mut command, &worker_cancellation));

        let deadline = Instant::now() + Duration::from_secs(1);
        let pid: i32 = loop {
            if let Ok(pid) = std::fs::read_to_string(&pid_file)
                .map(|value| value.trim().parse())
                .and_then(|value| value.map_err(std::io::Error::other))
            {
                break pid;
            }
            assert!(
                Instant::now() < deadline,
                "sleep child should publish its pid"
            );
            thread::sleep(Duration::from_millis(10));
        };

        let started = Instant::now();
        cancellation.cancel();
        assert_eq!(worker.join().unwrap().unwrap_err(), "install_cancelled");
        assert!(started.elapsed() < Duration::from_secs(2));
        assert_eq!(unsafe { nix::libc::kill(pid, 0) }, -1);
    }
}
