use std::{
    collections::{HashMap, HashSet},
    ffi::CString,
    fs::File,
    io::{self, Read, Write},
    path::{Path, PathBuf},
    str,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex as StdMutex, MutexGuard as StdMutexGuard, OnceLock, TryLockError, Weak,
    },
    thread,
    time::Duration,
};

use fs2::FileExt;
use serde::{Deserialize, Serialize};
use tokio::sync::Mutex;
use uuid::Uuid;

use crate::{
    fault_injection::FaultInjection,
    manifest::{RuntimeDescriptor, VerifiedPackageManifest},
};

use super::{
    archive::{self, RUNTIME_MARKER_NAME, RUNTIME_PROVENANCE_NAME},
    state::{self, RuntimeKind, RuntimeState},
};

const LOCK_POLL: Duration = Duration::from_millis(25);
const MAX_METADATA_BYTES: usize = 2 * 1024 * 1024;
const MAX_CLEANUP_DEPTH: usize = 256;

#[derive(Clone, Debug)]
pub struct RuntimeResolution {
    kind: RuntimeKind,
    version: String,
    root: PathBuf,
    used_fallback: bool,
    directory: Arc<File>,
    identity: (u64, u64),
}

impl RuntimeResolution {
    fn from_verified_directory(
        kind: RuntimeKind,
        version: String,
        root: PathBuf,
        used_fallback: bool,
        directory: File,
    ) -> Result<Self, String> {
        use rustix::fs::{fstat, FileType};

        let stat = fstat(&directory).map_err(|error| io::Error::from(error).to_string())?;
        if FileType::from_raw_mode(stat.st_mode) != FileType::Directory {
            return Err("runtime_resolution_not_directory".into());
        }
        Ok(Self {
            kind,
            version,
            root,
            used_fallback,
            directory: Arc::new(directory),
            identity: (stat.st_dev as u64, stat.st_ino as u64),
        })
    }

    pub fn kind(&self) -> RuntimeKind {
        self.kind
    }

    pub fn version(&self) -> &str {
        &self.version
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn used_fallback(&self) -> bool {
        self.used_fallback
    }

    pub(crate) fn duplicate_directory(&self) -> Result<File, String> {
        rustix::io::dup(&*self.directory)
            .map(File::from)
            .map_err(|error| io::Error::from(error).to_string())
    }

    pub(crate) fn identity(&self) -> (u64, u64) {
        self.identity
    }

    #[cfg(test)]
    pub(crate) fn fixture(
        kind: RuntimeKind,
        version: &str,
        root: &Path,
        used_fallback: bool,
    ) -> Result<Self, String> {
        Self::from_verified_directory(
            kind,
            version.into(),
            root.into(),
            used_fallback,
            open_directory(root)?,
        )
    }
}

#[derive(Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct VerifiedMarker {
    schema_version: u8,
    kind: String,
    version: String,
    archive_sha: String,
    tree_sha: String,
    required: Vec<String>,
    package_id: String,
    build_version: String,
    key_id: String,
}

#[derive(Default)]
struct RuntimeGates {
    core: Mutex<()>,
    collector: Mutex<()>,
    state: StdMutex<()>,
}

type GateIdentity = (u64, u64);

fn shared_gates(identity: GateIdentity) -> Result<Arc<RuntimeGates>, String> {
    static REGISTRY: OnceLock<StdMutex<HashMap<GateIdentity, Weak<RuntimeGates>>>> =
        OnceLock::new();
    let registry = REGISTRY.get_or_init(|| StdMutex::new(HashMap::new()));
    let mut registry = registry
        .lock()
        .map_err(|_| "runtime_gate_registry_poisoned".to_string())?;
    registry.retain(|_, gates| gates.strong_count() != 0);
    if let Some(gates) = registry.get(&identity).and_then(Weak::upgrade) {
        return Ok(gates);
    }
    let gates = Arc::new(RuntimeGates::default());
    registry.insert(identity, Arc::downgrade(&gates));
    Ok(gates)
}

#[derive(Clone)]
pub struct RuntimeManager {
    state_root: PathBuf,
    resource_root: PathBuf,
    manifest: Arc<VerifiedPackageManifest>,
    faults: FaultInjection,
    runtimes: Arc<File>,
    core: Arc<File>,
    collector: Arc<File>,
    locks: Arc<File>,
    gates: Arc<RuntimeGates>,
    cancellation: Arc<AtomicBool>,
}

enum CurrentVersion {
    Absent,
    Invalid,
    Valid(File),
}

fn same_identity(left: &rustix::fs::Stat, right: &rustix::fs::Stat) -> bool {
    left.st_dev == right.st_dev && left.st_ino == right.st_ino
}

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
        return Err("runtime_directory_invalid".into());
    }
    Ok(File::from(fd))
}

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
        return Err(format!("runtime_directory_invalid:{name}"));
    }
    Ok(File::from(fd))
}

fn ensure_child_directory(parent: &File, name: &str) -> Result<File, String> {
    use rustix::fs::{mkdirat, Mode};

    match mkdirat(parent, name, Mode::from_raw_mode(0o700)) {
        Ok(()) | Err(rustix::io::Errno::EXIST) => open_child_directory(parent, name),
        Err(error) => Err(io::Error::from(error).to_string()),
    }
}

fn stat_optional(parent: &File, name: &str) -> Result<Option<rustix::fs::Stat>, String> {
    use rustix::fs::{statat, AtFlags};

    match statat(parent, name, AtFlags::SYMLINK_NOFOLLOW) {
        Ok(stat) => Ok(Some(stat)),
        Err(rustix::io::Errno::NOENT) => Ok(None),
        Err(error) => Err(io::Error::from(error).to_string()),
    }
}

fn read_regular_file(parent: &File, name: &str) -> Result<Vec<u8>, String> {
    use rustix::fs::{fstat, openat, FileType, Mode, OFlags};

    let fd = openat(
        parent,
        name,
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|error| io::Error::from(error).to_string())?;
    let stat = fstat(&fd).map_err(|error| io::Error::from(error).to_string())?;
    if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile
        || stat.st_size < 0
        || stat.st_size as u64 > MAX_METADATA_BYTES as u64
    {
        return Err(format!("runtime_metadata_invalid:{name}"));
    }
    let mut file = File::from(fd);
    let mut bytes = Vec::with_capacity(stat.st_size as usize);
    Read::by_ref(&mut file)
        .take((MAX_METADATA_BYTES + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|error| error.to_string())?;
    if bytes.len() > MAX_METADATA_BYTES {
        return Err(format!("runtime_metadata_too_large:{name}"));
    }
    Ok(bytes)
}

fn write_exclusive_file(parent: &File, name: &str, bytes: &[u8]) -> Result<(), String> {
    use rustix::fs::{fchmod, openat, Mode, OFlags};

    let fd = openat(
        parent,
        name,
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
    file.write_all(bytes).map_err(|error| error.to_string())?;
    file.sync_all().map_err(|error| error.to_string())
}

fn remove_directory_contents(
    directory: &File,
    cancellation: &AtomicBool,
    depth: usize,
) -> Result<(), String> {
    use rustix::fs::{fstat, openat, statat, unlinkat, AtFlags, Dir, FileType, Mode, OFlags};

    if depth > MAX_CLEANUP_DEPTH {
        return Err("runtime_cleanup_depth".into());
    }
    let mut names = Vec::new();
    for item in Dir::read_from(directory).map_err(|error| io::Error::from(error).to_string())? {
        let item = item.map_err(|error| io::Error::from(error).to_string())?;
        if !matches!(item.file_name().to_bytes(), b"." | b"..") {
            names.push(item.file_name().to_owned());
        }
    }
    for name in names {
        if cancellation.load(Ordering::Acquire) {
            return Err("runtime_cancelled".into());
        }
        let before = statat(directory, &name, AtFlags::SYMLINK_NOFOLLOW)
            .map_err(|error| io::Error::from(error).to_string())?;
        if FileType::from_raw_mode(before.st_mode) == FileType::Directory {
            let child_fd = openat(
                directory,
                &name,
                OFlags::RDONLY
                    | OFlags::DIRECTORY
                    | OFlags::NOFOLLOW
                    | OFlags::NONBLOCK
                    | OFlags::CLOEXEC,
                Mode::empty(),
            )
            .map_err(|error| io::Error::from(error).to_string())?;
            let child = File::from(child_fd);
            let opened = fstat(&child).map_err(|error| io::Error::from(error).to_string())?;
            if !same_identity(&before, &opened) {
                return Err("runtime_cleanup_changed".into());
            }
            remove_directory_contents(&child, cancellation, depth + 1)?;
            let after = statat(directory, &name, AtFlags::SYMLINK_NOFOLLOW)
                .map_err(|error| io::Error::from(error).to_string())?;
            if !same_identity(&opened, &after)
                || FileType::from_raw_mode(after.st_mode) != FileType::Directory
            {
                return Err("runtime_cleanup_changed".into());
            }
            unlinkat(directory, &name, AtFlags::REMOVEDIR)
                .map_err(|error| io::Error::from(error).to_string())?;
        } else {
            unlinkat(directory, &name, AtFlags::empty())
                .map_err(|error| io::Error::from(error).to_string())?;
        }
    }
    Ok(())
}

fn remove_name_if_present(
    parent: &File,
    name: &str,
    cancellation: &AtomicBool,
) -> Result<(), String> {
    use rustix::fs::{fstat, openat, statat, unlinkat, AtFlags, FileType, Mode, OFlags};

    let Some(before) = stat_optional(parent, name)? else {
        return Ok(());
    };
    if FileType::from_raw_mode(before.st_mode) == FileType::Directory {
        let fd = openat(
            parent,
            name,
            OFlags::RDONLY
                | OFlags::DIRECTORY
                | OFlags::NOFOLLOW
                | OFlags::NONBLOCK
                | OFlags::CLOEXEC,
            Mode::empty(),
        )
        .map_err(|error| io::Error::from(error).to_string())?;
        let directory = File::from(fd);
        let opened = fstat(&directory).map_err(|error| io::Error::from(error).to_string())?;
        if !same_identity(&before, &opened) {
            return Err("runtime_cleanup_changed".into());
        }
        remove_directory_contents(&directory, cancellation, 0)?;
        let after = statat(parent, name, AtFlags::SYMLINK_NOFOLLOW)
            .map_err(|error| io::Error::from(error).to_string())?;
        if !same_identity(&opened, &after) {
            return Err("runtime_cleanup_changed".into());
        }
        unlinkat(parent, name, AtFlags::REMOVEDIR)
            .map_err(|error| io::Error::from(error).to_string())?;
    } else {
        unlinkat(parent, name, AtFlags::empty())
            .map_err(|error| io::Error::from(error).to_string())?;
    }
    Ok(())
}

fn classify_failed_install(
    primary: String,
    cleanup: Result<(), String>,
    cancelled: bool,
) -> String {
    match cleanup {
        Err(cleanup) => format!("{primary}; runtime_cleanup_failed:{cleanup}"),
        Ok(()) if primary.contains("runtime_cleanup_failed:") => primary,
        Ok(()) if cancelled => "runtime_cancelled".into(),
        Ok(()) => primary,
    }
}

impl RuntimeManager {
    pub fn new(
        state_root: PathBuf,
        resource_root: PathBuf,
        manifest: Arc<VerifiedPackageManifest>,
        faults: FaultInjection,
    ) -> Result<Self, String> {
        let state = open_directory(&state_root)?;
        let runtimes = ensure_child_directory(&state, "runtimes")?;
        let identity =
            rustix::fs::fstat(&runtimes).map_err(|error| io::Error::from(error).to_string())?;
        let gates = shared_gates((identity.st_dev as u64, identity.st_ino as u64))?;
        let core = ensure_child_directory(&runtimes, "core")?;
        let collector = ensure_child_directory(&runtimes, "collector")?;
        let locks = ensure_child_directory(&runtimes, ".locks")?;
        rustix::fs::fsync(&runtimes).map_err(|error| io::Error::from(error).to_string())?;
        rustix::fs::fsync(&state).map_err(|error| io::Error::from(error).to_string())?;
        Ok(Self {
            state_root,
            resource_root,
            manifest,
            faults,
            runtimes: Arc::new(runtimes),
            core: Arc::new(core),
            collector: Arc::new(collector),
            locks: Arc::new(locks),
            gates,
            cancellation: Arc::new(AtomicBool::new(false)),
        })
    }

    pub fn cancel(&self) {
        self.cancellation.store(true, Ordering::Release);
    }

    pub fn reset_cancellation(&self) {
        self.cancellation.store(false, Ordering::Release);
    }

    fn check_cancelled(&self) -> Result<(), String> {
        if self.cancellation.load(Ordering::Acquire) {
            Err("runtime_cancelled".into())
        } else {
            Ok(())
        }
    }

    fn kind_name(kind: RuntimeKind) -> &'static str {
        match kind {
            RuntimeKind::Core => "core",
            RuntimeKind::Collector => "collector",
        }
    }

    fn descriptor(&self, kind: RuntimeKind) -> RuntimeDescriptor {
        match kind {
            RuntimeKind::Core => self.manifest.manifest().runtimes.core.clone(),
            RuntimeKind::Collector => self.manifest.manifest().runtimes.collector.clone(),
        }
    }

    fn descriptor_from(manifest: &VerifiedPackageManifest, kind: RuntimeKind) -> RuntimeDescriptor {
        match kind {
            RuntimeKind::Core => manifest.manifest().runtimes.core.clone(),
            RuntimeKind::Collector => manifest.manifest().runtimes.collector.clone(),
        }
    }

    fn kind_directory(&self, kind: RuntimeKind) -> &File {
        match kind {
            RuntimeKind::Core => &self.core,
            RuntimeKind::Collector => &self.collector,
        }
    }

    fn version_root(&self, kind: RuntimeKind, version: &str) -> PathBuf {
        self.state_root
            .join("runtimes")
            .join(Self::kind_name(kind))
            .join(version)
    }

    fn marker_for(
        kind: RuntimeKind,
        descriptor: &RuntimeDescriptor,
        manifest: &VerifiedPackageManifest,
    ) -> VerifiedMarker {
        let package = manifest.manifest();
        VerifiedMarker {
            schema_version: 2,
            kind: Self::kind_name(kind).into(),
            version: descriptor.version.clone(),
            archive_sha: descriptor.sha256.clone(),
            tree_sha: descriptor.tree_sha256.clone(),
            required: descriptor.required_files.clone(),
            package_id: package.package_id.clone(),
            build_version: package.build_version.clone(),
            key_id: package.key_id.clone(),
        }
    }

    fn acquire_lock(&self, name: &str) -> Result<File, String> {
        use rustix::fs::{fchmod, fstat, openat, FileType, Mode, OFlags};

        self.check_cancelled()?;
        let fd = openat(
            &self.locks,
            name,
            OFlags::RDWR | OFlags::CREATE | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
            Mode::from_raw_mode(0o600),
        )
        .map_err(|error| io::Error::from(error).to_string())?;
        let file = File::from(fd);
        let stat = fstat(&file).map_err(|error| io::Error::from(error).to_string())?;
        if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile {
            return Err("runtime_lock_invalid".into());
        }
        fchmod(&file, Mode::from_raw_mode(0o600))
            .map_err(|error| io::Error::from(error).to_string())?;
        loop {
            self.check_cancelled()?;
            match FileExt::try_lock_exclusive(&file) {
                Ok(()) => return Ok(file),
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => thread::sleep(LOCK_POLL),
                Err(error) => return Err(error.to_string()),
            }
        }
    }

    fn acquire_state_gate(&self) -> Result<StdMutexGuard<'_, ()>, String> {
        loop {
            self.check_cancelled()?;
            match self.gates.state.try_lock() {
                Ok(guard) => return Ok(guard),
                Err(TryLockError::WouldBlock) => thread::sleep(LOCK_POLL),
                Err(TryLockError::Poisoned(_)) => return Err("runtime_state_gate_poisoned".into()),
            }
        }
    }

    fn load_state(&self) -> Result<RuntimeState, String> {
        let _in_process = self.acquire_state_gate()?;
        let _lock = self.acquire_lock("state.lock")?;
        state::load_at(&self.runtimes).map_err(|error| error.to_string())
    }

    fn persist_active(&self, kind: RuntimeKind, version: &str) -> Result<RuntimeState, String> {
        let _in_process = self.acquire_state_gate()?;
        let _lock = self.acquire_lock("state.lock")?;
        let mut current = state::load_at(&self.runtimes).map_err(|error| error.to_string())?;
        current
            .advance(kind, version)
            .map_err(|error| error.to_string())?;
        state::save_at(&self.runtimes, &current).map_err(|error| error.to_string())?;
        Ok(current)
    }

    fn signed_descriptor_at(
        &self,
        root: &File,
        kind: RuntimeKind,
    ) -> Result<(VerifiedPackageManifest, RuntimeDescriptor), String> {
        let bytes = read_regular_file(root, RUNTIME_PROVENANCE_NAME)?;
        let verified = self
            .manifest
            .reverify(&bytes)
            .map_err(|error| error.to_string())?;
        if verified.manifest().package_id != self.manifest.manifest().package_id {
            return Err("runtime_provenance_package_mismatch".into());
        }
        let descriptor = Self::descriptor_from(&verified, kind);
        Ok((verified, descriptor))
    }

    fn verify_version_at(
        &self,
        root: &File,
        kind: RuntimeKind,
        directory_version: &str,
    ) -> Result<RuntimeDescriptor, String> {
        self.check_cancelled()?;
        let (provenance, descriptor) = self.signed_descriptor_at(root, kind)?;
        if descriptor.version != directory_version {
            return Err("runtime_provenance_version_mismatch".into());
        }
        let marker: VerifiedMarker =
            serde_json::from_slice(&read_regular_file(root, RUNTIME_MARKER_NAME)?)
                .map_err(|error| error.to_string())?;
        if marker != Self::marker_for(kind, &descriptor, &provenance) {
            return Err("runtime_marker_mismatch".into());
        }
        archive::verify_installed_runtime_at(root, &descriptor, &self.cancellation)
            .map_err(|error| error.to_string())?;
        Ok(descriptor)
    }

    fn inspect_current(
        &self,
        kind: RuntimeKind,
        descriptor: &RuntimeDescriptor,
    ) -> Result<CurrentVersion, String> {
        let parent = self.kind_directory(kind);
        if stat_optional(parent, &descriptor.version)?.is_none() {
            return Ok(CurrentVersion::Absent);
        }
        let root = match open_child_directory(parent, &descriptor.version) {
            Ok(root) => root,
            Err(_) => return Ok(CurrentVersion::Invalid),
        };
        if let Ok((_, signed)) = self.signed_descriptor_at(&root, kind) {
            if signed.version == descriptor.version && signed != *descriptor {
                return Err("runtime_version_collision".into());
            }
        }
        match self.verify_version_at(&root, kind, &descriptor.version) {
            Ok(installed) if installed == *descriptor => Ok(CurrentVersion::Valid(root)),
            _ if self.cancellation.load(Ordering::Acquire) => Err("runtime_cancelled".into()),
            _ => Ok(CurrentVersion::Invalid),
        }
    }

    fn cleanup_kind(
        &self,
        kind: RuntimeKind,
        keep: &HashSet<String>,
        stale_only: bool,
    ) -> Result<(), String> {
        use rustix::fs::Dir;

        let parent = self.kind_directory(kind);
        let mut names = Vec::new();
        for item in Dir::read_from(parent).map_err(|error| io::Error::from(error).to_string())? {
            let item = item.map_err(|error| io::Error::from(error).to_string())?;
            if !matches!(item.file_name().to_bytes(), b"." | b"..") {
                names.push(item.file_name().to_owned());
            }
        }
        for name in names {
            self.check_cancelled()?;
            let utf8 = str::from_utf8(name.to_bytes()).ok();
            if utf8.is_some_and(|value| keep.contains(value)) {
                continue;
            }
            let stale = utf8
                .is_some_and(|value| value.contains(".tmp-") || value.starts_with(".quarantine-"));
            if stale_only && !stale {
                continue;
            }
            let name = CString::new(name.to_bytes())
                .map_err(|_| "runtime_cleanup_name_invalid".to_string())?;
            remove_name_if_present(
                parent,
                name.to_str()
                    .map_err(|_| "runtime_cleanup_name_invalid".to_string())?,
                &self.cancellation,
            )?;
        }
        Ok(())
    }

    fn keep_set(state: &RuntimeState, kind: RuntimeKind) -> HashSet<String> {
        [
            state.slot(kind).active.clone(),
            state.slot(kind).previous.clone(),
        ]
        .into_iter()
        .flatten()
        .collect()
    }

    fn quarantine_current(
        &self,
        kind: RuntimeKind,
        version: &str,
    ) -> Result<Option<String>, String> {
        use rustix::fs::{fsync, renameat_with, RenameFlags};

        let parent = self.kind_directory(kind);
        if stat_optional(parent, version)?.is_none() {
            return Ok(None);
        }
        let quarantine = format!(".quarantine-{version}-{}", Uuid::new_v4());
        renameat_with(
            parent,
            version,
            parent,
            quarantine.as_str(),
            RenameFlags::NOREPLACE,
        )
        .map_err(|error| io::Error::from(error).to_string())?;
        if let Err(sync_error) = fsync(parent) {
            let restore = renameat_with(
                parent,
                quarantine.as_str(),
                parent,
                version,
                RenameFlags::NOREPLACE,
            );
            let restore_sync = fsync(parent);
            if let Err(restore) = restore {
                return Err(format!(
                    "{}; runtime_restore_failed:{}",
                    io::Error::from(sync_error),
                    io::Error::from(restore)
                ));
            }
            if let Err(restore_sync) = restore_sync {
                return Err(format!(
                    "{}; runtime_restore_sync_failed:{}",
                    io::Error::from(sync_error),
                    io::Error::from(restore_sync)
                ));
            }
            return Err(io::Error::from(sync_error).to_string());
        }
        Ok(Some(quarantine))
    }

    fn publish_staging(
        &self,
        kind: RuntimeKind,
        staging: &str,
        descriptor: &RuntimeDescriptor,
        replace_invalid: bool,
    ) -> Result<(), String> {
        use rustix::fs::{fsync, renameat_with, RenameFlags};

        let parent = self.kind_directory(kind);
        let staging_root = open_child_directory(parent, staging)?;
        let marker = serde_json::to_vec(&Self::marker_for(kind, descriptor, &self.manifest))
            .map_err(|error| error.to_string())?;
        write_exclusive_file(&staging_root, RUNTIME_MARKER_NAME, &marker)?;
        write_exclusive_file(
            &staging_root,
            RUNTIME_PROVENANCE_NAME,
            self.manifest.signed_bytes(),
        )?;
        fsync(&staging_root).map_err(|error| io::Error::from(error).to_string())?;
        let verified = self.verify_version_at(&staging_root, kind, &descriptor.version)?;
        if verified != *descriptor {
            return Err("runtime_staging_descriptor_mismatch".into());
        }
        self.check_cancelled()?;

        let quarantine = if replace_invalid {
            self.quarantine_current(kind, &descriptor.version)?
        } else {
            None
        };
        let publish = renameat_with(
            parent,
            staging,
            parent,
            descriptor.version.as_str(),
            RenameFlags::NOREPLACE,
        );
        if let Err(error) = publish {
            if let Some(quarantine) = &quarantine {
                let restore = renameat_with(
                    parent,
                    quarantine.as_str(),
                    parent,
                    descriptor.version.as_str(),
                    RenameFlags::NOREPLACE,
                );
                if let Err(restore) = restore {
                    return Err(format!(
                        "{}; runtime_restore_failed:{}",
                        io::Error::from(error),
                        io::Error::from(restore)
                    ));
                }
            }
            if let Err(sync) = fsync(parent) {
                return Err(format!(
                    "{}; runtime_restore_sync_failed:{}",
                    io::Error::from(error),
                    io::Error::from(sync)
                ));
            }
            return Err(io::Error::from(error).to_string());
        }
        if let Err(sync_error) = fsync(parent) {
            let rollback = renameat_with(
                parent,
                descriptor.version.as_str(),
                parent,
                staging,
                RenameFlags::NOREPLACE,
            );
            if let Err(rollback) = rollback {
                return Err(format!(
                    "{}; runtime_publish_rollback_failed:{}",
                    io::Error::from(sync_error),
                    io::Error::from(rollback)
                ));
            }
            if let Some(quarantine) = &quarantine {
                if let Err(restore) = renameat_with(
                    parent,
                    quarantine.as_str(),
                    parent,
                    descriptor.version.as_str(),
                    RenameFlags::NOREPLACE,
                ) {
                    return Err(format!(
                        "{}; runtime_restore_failed:{}",
                        io::Error::from(sync_error),
                        io::Error::from(restore)
                    ));
                }
            }
            if let Err(restore_sync) = fsync(parent) {
                return Err(format!(
                    "{}; runtime_restore_sync_failed:{}",
                    io::Error::from(sync_error),
                    io::Error::from(restore_sync)
                ));
            }
            return Err(io::Error::from(sync_error).to_string());
        }
        Ok(())
    }

    fn cleanup_failed_staging(&self, kind: RuntimeKind, staging: &str) -> Result<(), String> {
        let cleanup_cancellation = AtomicBool::new(false);
        remove_name_if_present(self.kind_directory(kind), staging, &cleanup_cancellation)
    }

    fn install_current(
        &self,
        kind: RuntimeKind,
        descriptor: &RuntimeDescriptor,
        replace_invalid: bool,
    ) -> Result<(), String> {
        if self.faults.disk_full {
            return Err("disk_space_insufficient".into());
        }
        if (kind == RuntimeKind::Core && self.faults.core_hash)
            || (kind == RuntimeKind::Collector && self.faults.collector_hash)
        {
            return Err("archive_hash_mismatch".into());
        }
        let staging = format!("{}.tmp-{}", descriptor.version, Uuid::new_v4());
        let archive_path = self.resource_root.join(&descriptor.archive);
        let result = archive::extract_verified_at(
            &archive_path,
            descriptor,
            self.kind_directory(kind),
            &staging,
            &self.cancellation,
        )
        .map_err(|error| error.to_string())
        .and_then(|_| self.publish_staging(kind, &staging, descriptor, replace_invalid));
        if let Err(primary) = result {
            let cleanup = self.cleanup_failed_staging(kind, &staging);
            return Err(classify_failed_install(
                primary,
                cleanup,
                self.cancellation.load(Ordering::Acquire),
            ));
        }
        Ok(())
    }

    fn fallback(&self, kind: RuntimeKind) -> Result<Option<RuntimeResolution>, String> {
        let state = self.load_state()?;
        let mut seen = HashSet::new();
        for version in [
            state.slot(kind).active.as_ref(),
            state.slot(kind).previous.as_ref(),
        ]
        .into_iter()
        .flatten()
        {
            self.check_cancelled()?;
            if !seen.insert(version.clone()) {
                continue;
            }
            let root = match open_child_directory(self.kind_directory(kind), version) {
                Ok(root) => root,
                Err(_) => continue,
            };
            match self.verify_version_at(&root, kind, version) {
                Ok(_) => {
                    return RuntimeResolution::from_verified_directory(
                        kind,
                        version.clone(),
                        self.version_root(kind, version),
                        true,
                        root,
                    )
                    .map(Some);
                }
                Err(_) if self.cancellation.load(Ordering::Acquire) => {
                    return Err("runtime_cancelled".into())
                }
                Err(_) => {}
            }
        }
        Ok(None)
    }

    fn fallback_or(&self, kind: RuntimeKind, primary: String) -> Result<RuntimeResolution, String> {
        if primary.contains("runtime_cleanup_failed:") {
            return Err(primary);
        }
        if self.cancellation.load(Ordering::Acquire) {
            return Err("runtime_cancelled".into());
        }
        if primary == "runtime_cancelled" || primary == "runtime_version_collision" {
            return Err(primary);
        }
        match self.fallback(kind) {
            Ok(Some(resolution)) => Ok(resolution),
            Ok(None) => Err(primary),
            Err(error) => Err(format!("{primary}; runtime_fallback_failed:{error}")),
        }
    }

    fn ensure_blocking(&self, kind: RuntimeKind) -> Result<RuntimeResolution, String> {
        self.check_cancelled()?;
        let lock_name = format!("{}.lock", Self::kind_name(kind));
        let _kind_lock = self.acquire_lock(&lock_name)?;
        self.check_cancelled()?;

        let state_before = self.load_state()?;
        let keep_before = Self::keep_set(&state_before, kind);
        self.cleanup_kind(kind, &keep_before, true)?;
        let descriptor = self.descriptor(kind);
        let current = self.inspect_current(kind, &descriptor)?;
        let replace_invalid = match current {
            CurrentVersion::Valid(root) => {
                let state = self.persist_active(kind, &descriptor.version)?;
                self.cleanup_kind(kind, &Self::keep_set(&state, kind), false)?;
                return RuntimeResolution::from_verified_directory(
                    kind,
                    descriptor.version.clone(),
                    self.version_root(kind, &descriptor.version),
                    false,
                    root,
                );
            }
            CurrentVersion::Invalid => true,
            CurrentVersion::Absent => false,
        };

        if let Err(error) = self.install_current(kind, &descriptor, replace_invalid) {
            return self.fallback_or(kind, error);
        }
        let state = self.persist_active(kind, &descriptor.version)?;
        self.cleanup_kind(kind, &Self::keep_set(&state, kind), false)?;
        let root = open_child_directory(self.kind_directory(kind), &descriptor.version)?;
        let installed = self.verify_version_at(&root, kind, &descriptor.version)?;
        if installed != descriptor {
            return Err("runtime_published_descriptor_mismatch".into());
        }
        RuntimeResolution::from_verified_directory(
            kind,
            descriptor.version.clone(),
            self.version_root(kind, &descriptor.version),
            false,
            root,
        )
    }

    async fn wait_collector_delay(&self) -> Result<(), String> {
        let mut remaining = self.faults.collector_delay_ms;
        while remaining != 0 {
            self.check_cancelled()?;
            let slice = remaining.min(LOCK_POLL.as_millis() as u64);
            tokio::time::sleep(Duration::from_millis(slice)).await;
            remaining -= slice;
        }
        self.check_cancelled()
    }

    pub async fn ensure(&self, kind: RuntimeKind) -> Result<RuntimeResolution, String> {
        self.check_cancelled()?;
        if kind == RuntimeKind::Collector {
            self.wait_collector_delay().await?;
        }
        let gate = match kind {
            RuntimeKind::Core => &self.gates.core,
            RuntimeKind::Collector => &self.gates.collector,
        };
        let guard = loop {
            self.check_cancelled()?;
            match tokio::time::timeout(LOCK_POLL, gate.lock()).await {
                Ok(guard) => break guard,
                Err(_) => continue,
            }
        };
        self.check_cancelled()?;
        #[cfg(any(test, feature = "test-harness"))]
        if kind == RuntimeKind::Collector {
            if let Some(entered) = &self.faults.collector_gate_entered {
                entered.notify_one();
            }
            if let Some(release) = &self.faults.collector_gate_release {
                let notified = release.notified();
                tokio::pin!(notified);
                loop {
                    tokio::select! {
                        _ = &mut notified => break,
                        _ = tokio::time::sleep(LOCK_POLL) => self.check_cancelled()?,
                    }
                }
            }
        }
        let manager = self.clone();
        let result = tokio::task::spawn_blocking(move || manager.ensure_blocking(kind))
            .await
            .map_err(|error| format!("runtime_worker_failed:{error}"))?;
        drop(guard);
        result
    }
}

#[cfg(test)]
mod tests {
    use std::{
        fs::{self, File},
        io::Cursor,
        path::{Path, PathBuf},
        sync::Arc,
    };

    use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
    use ed25519_dalek::{
        pkcs8::{spki::der::pem::LineEnding, EncodePublicKey},
        Signer, SigningKey,
    };
    use serde_json::{json, Value};
    use sha2::{Digest, Sha256};

    use crate::{
        fault_injection::FaultInjection,
        manifest::{RuntimeDescriptor, VerifiedPackageManifest},
        runtime::state::{self, RuntimeKind},
    };

    use super::{classify_failed_install, RuntimeManager};

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

    fn append_directory(builder: &mut tar::Builder<Vec<u8>>, path: &str) {
        let mut header = tar::Header::new_ustar();
        header.set_path(path).unwrap();
        header.set_entry_type(tar::EntryType::Directory);
        header.set_size(0);
        header.set_mode(0o755);
        header.set_uid(0);
        header.set_gid(0);
        header.set_mtime(0);
        header.set_cksum();
        builder.append(&header, Cursor::new([])).unwrap();
    }

    fn append_file(builder: &mut tar::Builder<Vec<u8>>, path: &str, bytes: &[u8]) {
        let mut header = tar::Header::new_ustar();
        header.set_path(path).unwrap();
        header.set_entry_type(tar::EntryType::Regular);
        header.set_size(bytes.len() as u64);
        header.set_mode(0o644);
        header.set_uid(0);
        header.set_gid(0);
        header.set_mtime(0);
        header.set_cksum();
        builder.append(&header, Cursor::new(bytes)).unwrap();
    }

    fn hash_tree(file_name: &str, bytes: &[u8]) -> String {
        let mut digest = Sha256::new();
        for (kind, path, mode, payload) in [
            (b'D', b"config".as_slice(), 0o755_u32, b"".as_slice()),
            (b'F', file_name.as_bytes(), 0o644_u32, bytes),
        ] {
            digest.update([kind]);
            digest.update((path.len() as u64).to_le_bytes());
            digest.update(path);
            digest.update(mode.to_le_bytes());
            digest.update((payload.len() as u64).to_le_bytes());
            digest.update(payload);
        }
        hex::encode(digest.finalize())
    }

    fn write_pack(
        resource_root: &Path,
        kind: &str,
        version: &str,
        bytes: &[u8],
    ) -> RuntimeDescriptor {
        let archive_name = format!("{kind}-runtime.tar.zst");
        let required = format!("config/{kind}.json");
        let mut builder = tar::Builder::new(Vec::new());
        append_directory(&mut builder, "config");
        append_file(&mut builder, &required, bytes);
        let tar = builder.into_inner().unwrap();
        let compressed = zstd::stream::encode_all(Cursor::new(tar), 1).unwrap();
        fs::create_dir_all(resource_root).unwrap();
        fs::write(resource_root.join(&archive_name), &compressed).unwrap();
        RuntimeDescriptor {
            version: version.to_owned(),
            archive: archive_name,
            sha256: hex::encode(Sha256::digest(&compressed)),
            tree_sha256: hash_tree(&required, bytes),
            size_bytes: compressed.len() as u64,
            required_files: vec![required],
        }
    }

    fn verified_manifest(
        core: RuntimeDescriptor,
        collector: RuntimeDescriptor,
        build_version: &str,
    ) -> Arc<VerifiedPackageManifest> {
        verified_manifest_for_package(core, collector, build_version, "data-scientist-community-mac-arm64")
    }

    fn verified_manifest_for_package(
        core: RuntimeDescriptor,
        collector: RuntimeDescriptor,
        build_version: &str,
        package_id: &str,
    ) -> Arc<VerifiedPackageManifest> {
        let signing = SigningKey::from_bytes(&[19_u8; 32]);
        let payload = json!({
            "arch": "arm64",
            "build_version": build_version,
            "key_id": "test-key",
            "package_id": package_id,
            "runtimes": {"core": core, "collector": collector}
        });
        let mut canonical = String::new();
        canonical_json(&payload, &mut canonical);
        let signature = URL_SAFE_NO_PAD.encode(signing.sign(canonical.as_bytes()).to_bytes());
        let signed = serde_json::to_vec(&json!({
            "payload": payload,
            "signature": signature
        }))
        .unwrap();
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

    struct Harness {
        _temp: tempfile::TempDir,
        state_root: PathBuf,
        resource_root: PathBuf,
        manifest: Arc<VerifiedPackageManifest>,
    }

    impl Harness {
        fn new(core_version: &str, collector_version: &str) -> Self {
            let temp = tempfile::tempdir().unwrap();
            let state_root = temp.path().join("state");
            let resource_root = temp.path().join("resources");
            fs::create_dir(&state_root).unwrap();
            let core = write_pack(&resource_root, "core", core_version, b"core-v1");
            let collector = write_pack(
                &resource_root,
                "collector",
                collector_version,
                b"collector-v1",
            );
            let manifest = verified_manifest(core, collector, "20260711");
            Self {
                _temp: temp,
                state_root,
                resource_root,
                manifest,
            }
        }

        fn manager(&self, faults: FaultInjection) -> RuntimeManager {
            self.manager_with_manifest(self.manifest.clone(), faults)
        }

        fn manager_with_manifest(
            &self,
            manifest: Arc<VerifiedPackageManifest>,
            faults: FaultInjection,
        ) -> RuntimeManager {
            RuntimeManager::new(
                self.state_root.clone(),
                self.resource_root.clone(),
                manifest,
                faults,
            )
            .unwrap()
        }

        fn next_core_manifest(
            &self,
            version: &str,
            bytes: &[u8],
            build_version: &str,
        ) -> Arc<VerifiedPackageManifest> {
            let core = write_pack(&self.resource_root, "core", version, bytes);
            verified_manifest(
                core,
                self.manifest.manifest().runtimes.collector.clone(),
                build_version,
            )
        }

        fn load_state(&self) -> state::RuntimeState {
            let runtimes = File::open(self.state_root.join("runtimes")).unwrap();
            state::load_at(&runtimes).unwrap()
        }
    }

    #[tokio::test]
    async fn first_ensure_installs_and_activates_core() {
        let harness = Harness::new("core-v1", "collector-v1");
        let resolution = harness
            .manager(FaultInjection::default())
            .ensure(RuntimeKind::Core)
            .await
            .unwrap();

        assert_eq!(resolution.kind, RuntimeKind::Core);
        assert_eq!(resolution.version, "core-v1");
        assert!(!resolution.used_fallback);
        assert_eq!(
            resolution.root,
            harness.state_root.join("runtimes/core/core-v1")
        );
        assert!(resolution.root.join("config/core.json").is_file());
        assert!(resolution.root.join(".runtime-verified.json").is_file());
        assert!(resolution.root.join(".runtime-provenance.json").is_file());
        assert_eq!(
            fs::read(resolution.root.join(".runtime-provenance.json")).unwrap(),
            harness.manifest.signed_bytes()
        );
        let marker: Value = serde_json::from_slice(
            &fs::read(resolution.root.join(".runtime-verified.json")).unwrap(),
        )
        .unwrap();
        let keys: std::collections::BTreeSet<_> = marker
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        assert_eq!(
            keys,
            std::collections::BTreeSet::from([
                "archive_sha",
                "build_version",
                "key_id",
                "kind",
                "package_id",
                "required",
                "schema_version",
                "tree_sha",
                "version",
            ])
        );
        let state = harness.load_state();
        assert_eq!(state.core.active.as_deref(), Some("core-v1"));
        assert_eq!(state.core.previous, None);
    }

    #[tokio::test]
    async fn two_managers_share_the_process_lock_and_reuse_one_publish() {
        let harness = Harness::new("core-v1", "collector-v1");
        let first = harness.manager(FaultInjection::default());
        let second = harness.manager(FaultInjection::default());
        let (left, right) = tokio::join!(
            first.ensure(RuntimeKind::Core),
            second.ensure(RuntimeKind::Core)
        );

        let left = left.unwrap();
        let right = right.unwrap();
        assert_eq!(left.root, right.root);
        assert_eq!(left.version, "core-v1");
        assert_eq!(right.version, "core-v1");
        let state = harness.load_state();
        assert_eq!(state.core.active.as_deref(), Some("core-v1"));
        assert_eq!(state.core.previous, None);
        let names: Vec<_> = fs::read_dir(harness.state_root.join("runtimes/core"))
            .unwrap()
            .map(|item| item.unwrap().file_name())
            .collect();
        assert_eq!(names, [std::ffi::OsString::from("core-v1")]);
    }

    #[tokio::test]
    async fn collector_gate_does_not_block_core() {
        let harness = Harness::new("core-v1", "collector-v1");
        let entered = Arc::new(tokio::sync::Notify::new());
        let release = Arc::new(tokio::sync::Notify::new());
        let manager = harness.manager(FaultInjection {
            collector_gate_entered: Some(entered.clone()),
            collector_gate_release: Some(release.clone()),
            ..FaultInjection::default()
        });
        let collector = {
            let manager = manager.clone();
            tokio::spawn(async move { manager.ensure(RuntimeKind::Collector).await })
        };
        tokio::time::timeout(std::time::Duration::from_secs(1), entered.notified())
            .await
            .unwrap();

        let core = tokio::time::timeout(
            std::time::Duration::from_secs(1),
            manager.ensure(RuntimeKind::Core),
        )
        .await
        .expect("core must not wait for the collector gate")
        .unwrap();
        assert_eq!(core.version, "core-v1");
        release.notify_one();
        assert_eq!(collector.await.unwrap().unwrap().version, "collector-v1");
    }

    #[tokio::test]
    async fn cancellation_before_ensure_has_zero_runtime_mutation() {
        let harness = Harness::new("core-v1", "collector-v1");
        let manager = harness.manager(FaultInjection::default());
        manager.cancel();

        assert_eq!(
            manager.ensure(RuntimeKind::Core).await.unwrap_err(),
            "runtime_cancelled"
        );
        assert!(!harness.state_root.join("runtimes/state.json").exists());
        assert!(fs::read_dir(harness.state_root.join("runtimes/core"))
            .unwrap()
            .next()
            .is_none());
        assert!(fs::read_dir(harness.state_root.join("runtimes/.locks"))
            .unwrap()
            .next()
            .is_none());
    }

    #[tokio::test]
    async fn stale_temporary_directory_is_removed_before_install() {
        let harness = Harness::new("core-v1", "collector-v1");
        let manager = harness.manager(FaultInjection::default());
        let stale = harness
            .state_root
            .join("runtimes/core/core-v1.tmp-interrupted");
        fs::create_dir(&stale).unwrap();
        fs::write(stale.join("partial"), b"partial").unwrap();

        let resolution = manager.ensure(RuntimeKind::Core).await.unwrap();

        assert_eq!(resolution.version, "core-v1");
        assert!(!stale.exists());
    }

    async fn install_v1_then_make_v2_archive_bad(harness: &Harness) -> (RuntimeManager, PathBuf) {
        harness
            .manager(FaultInjection::default())
            .ensure(RuntimeKind::Core)
            .await
            .unwrap();
        let v1_root = harness.state_root.join("runtimes/core/core-v1");
        let v2 = harness.next_core_manifest("core-v2", b"core-v2", "20260712");
        fs::write(
            harness.resource_root.join("core-runtime.tar.zst"),
            b"corrupt archive",
        )
        .unwrap();
        (
            harness.manager_with_manifest(v2, FaultInjection::default()),
            v1_root,
        )
    }

    #[tokio::test]
    async fn bad_v2_archive_falls_back_using_saved_signed_v1_provenance() {
        let harness = Harness::new("core-v1", "collector-v1");
        let (manager, v1_root) = install_v1_then_make_v2_archive_bad(&harness).await;

        let resolution = manager.ensure(RuntimeKind::Core).await.unwrap();

        assert!(resolution.used_fallback);
        assert_eq!(resolution.version, "core-v1");
        assert_eq!(resolution.root, v1_root);
        assert_eq!(
            fs::read(v1_root.join("config/core.json")).unwrap(),
            b"core-v1"
        );
    }

    #[tokio::test]
    async fn tampered_fallback_provenance_is_rejected() {
        let harness = Harness::new("core-v1", "collector-v1");
        let (manager, v1_root) = install_v1_then_make_v2_archive_bad(&harness).await;
        fs::write(
            v1_root.join(".runtime-provenance.json"),
            br#"{"payload":{},"signature":"tampered"}"#,
        )
        .unwrap();

        assert!(manager.ensure(RuntimeKind::Core).await.is_err());
    }

    #[tokio::test]
    async fn tampered_fallback_marker_is_rejected() {
        let harness = Harness::new("core-v1", "collector-v1");
        let (manager, v1_root) = install_v1_then_make_v2_archive_bad(&harness).await;
        let marker = v1_root.join(".runtime-verified.json");
        let mut document: Value = serde_json::from_slice(&fs::read(&marker).unwrap()).unwrap();
        document["archive_sha"] = json!("0".repeat(64));
        fs::write(marker, serde_json::to_vec(&document).unwrap()).unwrap();

        assert!(manager.ensure(RuntimeKind::Core).await.is_err());
    }

    #[tokio::test]
    async fn tampered_fallback_tree_is_rejected() {
        let harness = Harness::new("core-v1", "collector-v1");
        let (manager, v1_root) = install_v1_then_make_v2_archive_bad(&harness).await;
        fs::write(v1_root.join("config/core.json"), b"tampered").unwrap();

        assert!(manager.ensure(RuntimeKind::Core).await.is_err());
    }

    #[tokio::test]
    async fn same_version_with_a_different_signed_descriptor_is_a_collision() {
        let harness = Harness::new("core-v1", "collector-v1");
        harness
            .manager(FaultInjection::default())
            .ensure(RuntimeKind::Core)
            .await
            .unwrap();
        let replacement = harness.next_core_manifest("core-v1", b"different", "20260712");
        let manager = harness.manager_with_manifest(replacement, FaultInjection::default());

        assert_eq!(
            manager.ensure(RuntimeKind::Core).await.unwrap_err(),
            "runtime_version_collision"
        );
        assert_eq!(
            fs::read(
                harness
                    .state_root
                    .join("runtimes/core/core-v1/config/core.json")
            )
            .unwrap(),
            b"core-v1"
        );
    }

    #[tokio::test]
    async fn successful_advancement_keeps_only_active_and_previous() {
        let harness = Harness::new("core-v1", "collector-v1");
        harness
            .manager(FaultInjection::default())
            .ensure(RuntimeKind::Core)
            .await
            .unwrap();
        for (version, bytes, build) in [
            ("core-v2", b"core-v2".as_slice(), "20260712"),
            ("core-v3", b"core-v3".as_slice(), "20260713"),
        ] {
            let manifest = harness.next_core_manifest(version, bytes, build);
            harness
                .manager_with_manifest(manifest, FaultInjection::default())
                .ensure(RuntimeKind::Core)
                .await
                .unwrap();
        }

        let state = harness.load_state();
        assert_eq!(state.core.active.as_deref(), Some("core-v3"));
        assert_eq!(state.core.previous.as_deref(), Some("core-v2"));
        assert!(!harness.state_root.join("runtimes/core/core-v1").exists());
        assert!(harness.state_root.join("runtimes/core/core-v2").is_dir());
        assert!(harness.state_root.join("runtimes/core/core-v3").is_dir());
    }

    #[tokio::test]
    async fn foreign_package_provenance_cannot_be_used_for_fallback() {
        let harness = Harness::new("core-v1", "collector-v1");
        let (manager, v1_root) = install_v1_then_make_v2_archive_bad(&harness).await;
        let foreign = verified_manifest_for_package(
            harness.manifest.manifest().runtimes.core.clone(),
            harness.manifest.manifest().runtimes.collector.clone(),
            "20260711",
            "other-product",
        );
        fs::write(
            v1_root.join(".runtime-provenance.json"),
            foreign.signed_bytes(),
        )
        .unwrap();

        assert!(manager.ensure(RuntimeKind::Core).await.is_err());
    }

    #[tokio::test]
    async fn cancellation_is_not_hidden_by_fallback_failure() {
        let harness = Harness::new("core-v1", "collector-v1");
        let manager = harness.manager(FaultInjection::default());
        manager.cancel();

        assert_eq!(
            manager
                .fallback_or(RuntimeKind::Core, "archive_hash_mismatch".into())
                .unwrap_err(),
            "runtime_cancelled"
        );
    }

    #[test]
    fn failed_install_cleanup_ignores_manager_cancellation() {
        let harness = Harness::new("core-v1", "collector-v1");
        let manager = harness.manager(FaultInjection::default());
        let staging_name = "core-v1.tmp-interrupted";
        let staging = harness.state_root.join("runtimes/core").join(staging_name);
        fs::create_dir(&staging).unwrap();
        fs::write(staging.join("partial"), b"partial").unwrap();
        manager.cancel();

        manager
            .cleanup_failed_staging(RuntimeKind::Core, staging_name)
            .unwrap();

        assert!(!staging.exists());
    }

    #[test]
    fn cancellation_interrupts_waiting_for_the_in_process_state_gate() {
        let harness = Harness::new("core-v1", "collector-v1");
        let manager = harness.manager(FaultInjection::default());
        let held = manager.gates.state.lock().unwrap();
        let worker = manager.clone();
        let (sender, receiver) = std::sync::mpsc::channel();
        std::thread::spawn(move || sender.send(worker.load_state()).unwrap());
        std::thread::sleep(std::time::Duration::from_millis(50));
        manager.cancel();

        let result = receiver
            .recv_timeout(std::time::Duration::from_millis(250))
            .expect("cancelled state-gate waiter must not remain blocked");
        assert_eq!(result.unwrap_err(), "runtime_cancelled");
        drop(held);
    }

    #[test]
    fn cancellation_preserves_a_cleanup_failure_compound_error() {
        let harness = Harness::new("core-v1", "collector-v1");
        let manager = harness.manager(FaultInjection::default());
        manager.cancel();
        let compound =
            "runtime_cancelled; runtime_cleanup_failed:runtime_cleanup_changed".to_string();

        assert_eq!(
            manager
                .fallback_or(RuntimeKind::Core, compound.clone())
                .unwrap_err(),
            compound
        );
    }

    #[test]
    fn archive_cleanup_failure_is_not_hidden_by_later_cancellation() {
        let compound =
            "archive_hash_mismatch; runtime_cleanup_failed:cleanup_destination_changed".to_string();

        assert_eq!(
            classify_failed_install(compound.clone(), Ok(()), true),
            compound
        );
    }

    #[tokio::test]
    async fn quarantine_names_cannot_collide_with_signed_versions() {
        let harness = Harness::new("core-v1", "collector-v1");
        let manager = harness.manager(FaultInjection::default());
        manager.ensure(RuntimeKind::Core).await.unwrap();

        let quarantine = manager
            .quarantine_current(RuntimeKind::Core, "core-v1")
            .unwrap()
            .unwrap();

        assert!(quarantine.starts_with(".quarantine-"));
        assert!(!harness.state_root.join("runtimes/core/core-v1").exists());
    }

    #[tokio::test]
    async fn cancellation_interrupts_collector_delay_in_one_slice() {
        let harness = Harness::new("core-v1", "collector-v1");
        let manager = harness.manager(FaultInjection {
            collector_delay_ms: 30_000,
            ..FaultInjection::default()
        });
        let worker = {
            let manager = manager.clone();
            tokio::spawn(async move { manager.ensure(RuntimeKind::Collector).await })
        };
        tokio::time::sleep(std::time::Duration::from_millis(60)).await;
        manager.cancel();

        let error = tokio::time::timeout(std::time::Duration::from_millis(250), worker)
            .await
            .expect("collector delay must poll cancellation in <=25ms slices")
            .unwrap()
            .unwrap_err();
        assert_eq!(error, "runtime_cancelled");
        assert!(fs::read_dir(harness.state_root.join("runtimes/collector"))
            .unwrap()
            .next()
            .is_none());
    }

    #[tokio::test]
    async fn corrupt_same_descriptor_tree_is_repaired_after_staging_is_verified() {
        let harness = Harness::new("core-v1", "collector-v1");
        let manager = harness.manager(FaultInjection::default());
        let first = manager.ensure(RuntimeKind::Core).await.unwrap();
        fs::write(first.root.join("config/core.json"), b"tampered").unwrap();

        let repaired = manager.ensure(RuntimeKind::Core).await.unwrap();

        assert_eq!(repaired.version, "core-v1");
        assert!(!repaired.used_fallback);
        assert_eq!(
            fs::read(repaired.root.join("config/core.json")).unwrap(),
            b"core-v1"
        );
        assert!(fs::read_dir(harness.state_root.join("runtimes/core"))
            .unwrap()
            .all(|item| !item
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with(".quarantine-")));
    }

    #[tokio::test]
    async fn lock_symlink_is_rejected_without_touching_its_target() {
        use std::os::unix::fs::symlink;

        let harness = Harness::new("core-v1", "collector-v1");
        let manager = harness.manager(FaultInjection::default());
        let outside = harness.state_root.join("outside-lock");
        fs::write(&outside, b"outside").unwrap();
        symlink(
            &outside,
            harness.state_root.join("runtimes/.locks/core.lock"),
        )
        .unwrap();

        assert!(manager.ensure(RuntimeKind::Core).await.is_err());
        assert_eq!(fs::read(outside).unwrap(), b"outside");
    }

    #[tokio::test]
    async fn parallel_kinds_serialize_state_without_serializing_installation() {
        let harness = Harness::new("core-v1", "collector-v1");
        let manager = harness.manager(FaultInjection::default());

        let (core, collector) = tokio::join!(
            manager.ensure(RuntimeKind::Core),
            manager.ensure(RuntimeKind::Collector)
        );

        assert_eq!(core.unwrap().version, "core-v1");
        assert_eq!(collector.unwrap().version, "collector-v1");
        let state = harness.load_state();
        assert_eq!(state.core.active.as_deref(), Some("core-v1"));
        assert_eq!(state.collector.active.as_deref(), Some("collector-v1"));
    }
}
