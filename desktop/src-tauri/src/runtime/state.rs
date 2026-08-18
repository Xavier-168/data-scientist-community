use std::{
    fs::File,
    io::{self, Read, Write},
};

#[cfg(windows)]
use std::path::Path;

use serde::{Deserialize, Serialize};
use thiserror::Error;

const STATE_SCHEMA: u32 = 2;
const MAX_STATE_BYTES: usize = 64 * 1024;
const MAX_VERSION_BYTES: usize = 128;
const STATE_FILE_NAME: &str = "state.json";

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum RuntimeKind {
    Core,
    Collector,
}

#[derive(Clone, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeSlot {
    pub active: Option<String>,
    pub previous: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeState {
    pub schema_version: u32,
    pub core: RuntimeSlot,
    pub collector: RuntimeSlot,
}

impl Default for RuntimeState {
    fn default() -> Self {
        Self {
            schema_version: STATE_SCHEMA,
            core: RuntimeSlot::default(),
            collector: RuntimeSlot::default(),
        }
    }
}

#[derive(Debug, Error)]
pub enum StateError {
    #[error("runtime_state_io: {0}")]
    Io(#[from] io::Error),
    #[error("runtime_state_json: {0}")]
    Json(#[from] serde_json::Error),
    #[error("runtime_state_invalid: {0}")]
    Invalid(&'static str),
    #[error("runtime_state_too_large")]
    TooLarge,
    #[error("runtime_state_cleanup_failed: primary={primary}; cleanup={cleanup}")]
    Cleanup {
        primary: Box<StateError>,
        cleanup: io::Error,
    },
}

fn valid_version(version: &str) -> bool {
    let bytes = version.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= MAX_VERSION_BYTES
        && bytes[0].is_ascii_alphanumeric()
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(byte))
        && !version.contains(".tmp-")
}

fn validate_slot(slot: &RuntimeSlot) -> Result<(), StateError> {
    if slot
        .active
        .as_deref()
        .is_some_and(|value| !valid_version(value))
        || slot
            .previous
            .as_deref()
            .is_some_and(|value| !valid_version(value))
    {
        return Err(StateError::Invalid("version"));
    }
    if slot.active.is_some() && slot.active == slot.previous {
        return Err(StateError::Invalid("active_equals_previous"));
    }
    Ok(())
}

impl RuntimeState {
    pub fn slot(&self, kind: RuntimeKind) -> &RuntimeSlot {
        match kind {
            RuntimeKind::Core => &self.core,
            RuntimeKind::Collector => &self.collector,
        }
    }

    pub fn advance(&mut self, kind: RuntimeKind, version: &str) -> Result<(), StateError> {
        self.validate()?;
        if !valid_version(version) {
            return Err(StateError::Invalid("version"));
        }
        let mut next = self.clone();
        let slot = match kind {
            RuntimeKind::Core => &mut next.core,
            RuntimeKind::Collector => &mut next.collector,
        };
        if slot.active.as_deref() != Some(version) {
            slot.previous = slot.active.replace(version.to_owned());
        }
        next.validate()?;
        *self = next;
        Ok(())
    }

    pub fn validate(&self) -> Result<(), StateError> {
        if self.schema_version != STATE_SCHEMA {
            return Err(StateError::Invalid("schema"));
        }
        validate_slot(&self.core)?;
        validate_slot(&self.collector)
    }
}

#[cfg(unix)]
pub fn load_at(directory: &File) -> Result<RuntimeState, StateError> {
    use rustix::fs::{fstat, openat, FileType, Mode, OFlags};

    let fd = match openat(
        directory,
        STATE_FILE_NAME,
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
    ) {
        Ok(fd) => fd,
        Err(rustix::io::Errno::NOENT) => return Ok(RuntimeState::default()),
        Err(error) => return Err(io::Error::from(error).into()),
    };
    let stat = fstat(&fd).map_err(io::Error::from)?;
    if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile {
        return Err(StateError::Invalid("state_file_type"));
    }
    if stat.st_size < 0 || stat.st_size as u64 > MAX_STATE_BYTES as u64 {
        return Err(StateError::TooLarge);
    }
    let mut file = File::from(fd);
    let mut bytes = Vec::with_capacity(stat.st_size as usize);
    Read::by_ref(&mut file)
        .take((MAX_STATE_BYTES + 1) as u64)
        .read_to_end(&mut bytes)?;
    if bytes.len() > MAX_STATE_BYTES {
        return Err(StateError::TooLarge);
    }
    let state: RuntimeState = serde_json::from_slice(&bytes)?;
    state.validate()?;
    Ok(state)
}

/// Windows 版 `load_at`：std 无法从目录句柄取回路径（也没有 openat），
/// 因此直接以路径读取 state.json。文件类型/大小上限与严格 JSON 校验
/// 语义与 Unix 实现一致；符号链接（junction/reparse point）一律拒绝。
#[cfg(windows)]
pub fn load_at(directory: &Path) -> Result<RuntimeState, StateError> {
    let path = directory.join(STATE_FILE_NAME);
    let metadata = match std::fs::symlink_metadata(&path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            return Ok(RuntimeState::default())
        }
        Err(error) => return Err(error.into()),
    };
    if !metadata.file_type().is_file() {
        return Err(StateError::Invalid("state_file_type"));
    }
    if metadata.len() > MAX_STATE_BYTES as u64 {
        return Err(StateError::TooLarge);
    }
    let mut file = File::open(&path)?;
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    Read::by_ref(&mut file)
        .take((MAX_STATE_BYTES + 1) as u64)
        .read_to_end(&mut bytes)?;
    if bytes.len() > MAX_STATE_BYTES {
        return Err(StateError::TooLarge);
    }
    let state: RuntimeState = serde_json::from_slice(&bytes)?;
    state.validate()?;
    Ok(state)
}

#[cfg(unix)]
pub fn save_at(directory: &File, state: &RuntimeState) -> Result<(), StateError> {
    save_at_with_hook(directory, state, || {})
}

/// Windows 版 `save_at`：写入唯一临时名后以 `std::fs::rename` 原子替换
/// （Windows 允许 rename 覆盖已存在的普通文件）。与 Unix 的 fd 锚定
/// 不同，Windows 依赖“唯一临时名 + rename”保证不发布半写的 state.json。
#[cfg(windows)]
pub fn save_at(directory: &Path, state: &RuntimeState) -> Result<(), StateError> {
    save_at_with_hook(directory, state, || {})
}

#[cfg(unix)]
fn save_at_with_hook<F: FnOnce()>(
    directory: &File,
    state: &RuntimeState,
    after_temporary_sync: F,
) -> Result<(), StateError> {
    use rustix::fs::{fchmod, fsync, openat, renameat, unlinkat, AtFlags, Mode, OFlags};

    state.validate()?;
    let bytes = serde_json::to_vec(state)?;
    if bytes.len() > MAX_STATE_BYTES {
        return Err(StateError::TooLarge);
    }
    let temporary_name = format!(".state.json.tmp-{}", uuid::Uuid::new_v4());
    let mut temporary_created = false;
    let mut renamed = false;
    let result = (|| -> Result<(), StateError> {
        let fd = openat(
            directory,
            temporary_name.as_str(),
            OFlags::WRONLY
                | OFlags::CREATE
                | OFlags::EXCL
                | OFlags::NOFOLLOW
                | OFlags::NONBLOCK
                | OFlags::CLOEXEC,
            Mode::from_raw_mode(0o600),
        )
        .map_err(io::Error::from)?;
        temporary_created = true;
        let mut temporary = File::from(fd);
        fchmod(&temporary, Mode::from_raw_mode(0o600)).map_err(io::Error::from)?;
        temporary.write_all(&bytes)?;
        fsync(&temporary).map_err(io::Error::from)?;
        after_temporary_sync();
        renameat(
            directory,
            temporary_name.as_str(),
            directory,
            STATE_FILE_NAME,
        )
        .map_err(io::Error::from)?;
        renamed = true;
        fsync(directory).map_err(io::Error::from)?;
        Ok(())
    })();

    let cleanup_error = if temporary_created && !renamed {
        match unlinkat(directory, temporary_name.as_str(), AtFlags::empty()) {
            Ok(()) | Err(rustix::io::Errno::NOENT) => None,
            Err(error) => Some(io::Error::from(error)),
        }
    } else {
        None
    };
    match (result, cleanup_error) {
        (Err(primary), Some(cleanup)) => Err(StateError::Cleanup {
            primary: Box::new(primary),
            cleanup,
        }),
        (Err(primary), None) => Err(primary),
        (Ok(()), Some(cleanup)) => Err(cleanup.into()),
        (Ok(()), None) => Ok(()),
    }
}

#[cfg(windows)]
fn save_at_with_hook<F: FnOnce()>(
    directory: &Path,
    state: &RuntimeState,
    after_temporary_sync: F,
) -> Result<(), StateError> {
    state.validate()?;
    let bytes = serde_json::to_vec(state)?;
    if bytes.len() > MAX_STATE_BYTES {
        return Err(StateError::TooLarge);
    }
    let temporary = directory.join(format!(".state.json.tmp-{}", uuid::Uuid::new_v4()));
    let mut temporary_created = false;
    let mut renamed = false;
    let result = (|| -> Result<(), StateError> {
        let mut file = File::create(&temporary)?;
        temporary_created = true;
        file.write_all(&bytes)?;
        file.sync_all()?;
        after_temporary_sync();
        // Windows 的 rename 可原子替换已存在的普通文件；目录无法 fsync（省略）。
        std::fs::rename(&temporary, directory.join(STATE_FILE_NAME))?;
        renamed = true;
        Ok(())
    })();

    let cleanup_error = if temporary_created && !renamed {
        match std::fs::remove_file(&temporary) {
            Ok(()) => None,
            Err(ref error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(error) => Some(error),
        }
    } else {
        None
    };
    match (result, cleanup_error) {
        (Err(primary), Some(cleanup)) => Err(StateError::Cleanup {
            primary: Box::new(primary),
            cleanup,
        }),
        (Err(primary), None) => Err(primary),
        (Ok(()), Some(cleanup)) => Err(cleanup.into()),
        (Ok(()), None) => Ok(()),
    }
}

#[cfg(all(test, unix))]
mod tests {
    use std::{fs, path::Path, sync::{Arc, Barrier}, time::Duration};

    #[cfg(unix)]
    use std::{
        fs::File,
        os::unix::{
            ffi::OsStrExt,
            fs::{MetadataExt, PermissionsExt},
        },
    };

    use super::*;

    /// 测试用目录锚点：Unix 为已打开的目录 fd，Windows 为目录路径。
    #[cfg(unix)]
    fn anchor(directory: &Path) -> File {
        File::open(directory).unwrap()
    }

    #[cfg(windows)]
    fn anchor(directory: &Path) -> std::path::PathBuf {
        directory.to_path_buf()
    }

    #[test]
    fn advance_rotates_active_and_previous_per_runtime_kind() {
        let mut state = RuntimeState::default();
        assert_eq!(state.schema_version, 2);
        assert_eq!(state.slot(RuntimeKind::Core), &RuntimeSlot::default());

        state.advance(RuntimeKind::Core, "core-v1").unwrap();
        assert_eq!(state.core.active.as_deref(), Some("core-v1"));
        assert_eq!(state.core.previous, None);

        state.advance(RuntimeKind::Core, "core-v1").unwrap();
        assert_eq!(state.core.active.as_deref(), Some("core-v1"));
        assert_eq!(state.core.previous, None);

        state.advance(RuntimeKind::Core, "core-v2").unwrap();
        assert_eq!(state.core.active.as_deref(), Some("core-v2"));
        assert_eq!(state.core.previous.as_deref(), Some("core-v1"));

        state.advance(RuntimeKind::Core, "core-v1").unwrap();
        assert_eq!(state.core.active.as_deref(), Some("core-v1"));
        assert_eq!(state.core.previous.as_deref(), Some("core-v2"));
        assert_eq!(state.slot(RuntimeKind::Collector), &RuntimeSlot::default());

        state
            .advance(RuntimeKind::Collector, "collector-v1")
            .unwrap();
        assert_eq!(state.collector.active.as_deref(), Some("collector-v1"));
        state.validate().unwrap();
    }

    #[test]
    fn validation_and_loading_reject_unsafe_or_non_strict_state() {
        let temp = tempfile::tempdir().unwrap();
        let directory = anchor(temp.path());
        for bytes in [
            br#"{"schema_version":2,"core":{"active":null,"previous":null},"collector":{"active":null,"previous":null},"unknown":true}"#.as_slice(),
            br#"{"schema_version":2,"core":{"active":null,"previous":null,"unknown":true},"collector":{"active":null,"previous":null}}"#,
            br#"{"schema_version":2,"core":{"active":null,"previous":null},"collector":{"active":null,"previous":null}} {}"#,
        ] {
            fs::write(temp.path().join("state.json"), bytes).unwrap();
            assert!(load_at(&directory).is_err());
        }

        let wrong_schema = RuntimeState {
            schema_version: 1,
            ..RuntimeState::default()
        };
        assert!(wrong_schema.validate().is_err());

        for version in [
            "",
            "../outside",
            ".tmp-active",
            "core.tmp-old",
            "core/v1",
            "核心",
        ] {
            let mut unsafe_state = RuntimeState::default();
            unsafe_state.core.active = Some(version.into());
            assert!(unsafe_state.validate().is_err(), "{version}");
        }

        let mut duplicate = RuntimeState::default();
        duplicate.core.active = Some("core-v1".into());
        duplicate.core.previous = Some("core-v1".into());
        assert!(duplicate.validate().is_err());
    }

    #[test]
    fn missing_state_loads_default_and_round_trip_is_mode_0600() {
        let temp = tempfile::tempdir().unwrap();
        let directory = anchor(temp.path());
        assert_eq!(load_at(&directory).unwrap(), RuntimeState::default());

        let mut state = RuntimeState::default();
        state.advance(RuntimeKind::Core, "core-v1").unwrap();
        save_at(&directory, &state).unwrap();

        assert_eq!(load_at(&directory).unwrap(), state);
        #[cfg(unix)]
        {
            assert_eq!(
                fs::symlink_metadata(temp.path().join("state.json"))
                    .unwrap()
                    .mode()
                    & 0o777,
                0o600
            );
        }
    }

    #[cfg(unix)]
    #[test]
    fn load_rejects_symlink_fifo_and_oversized_state_without_blocking() {
        use std::os::unix::fs::symlink;

        let temp = tempfile::tempdir().unwrap();
        let directory = File::open(temp.path()).unwrap();
        let outside = temp.path().join("outside.json");
        fs::write(&outside, b"{}").unwrap();
        symlink(&outside, temp.path().join("state.json")).unwrap();
        assert!(load_at(&directory).is_err());

        fs::remove_file(temp.path().join("state.json")).unwrap();
        let fifo = temp.path().join("state.json");
        let fifo_name = std::ffi::CString::new(fifo.as_os_str().as_bytes()).unwrap();
        assert_eq!(unsafe { nix::libc::mkfifo(fifo_name.as_ptr(), 0o600) }, 0);
        let fifo_directory = File::open(temp.path()).unwrap();
        let (sender, receiver) = std::sync::mpsc::channel();
        std::thread::spawn(move || sender.send(load_at(&fifo_directory).is_err()).unwrap());
        assert!(receiver.recv_timeout(Duration::from_millis(500)).unwrap());

        fs::remove_file(&fifo).unwrap();
        fs::write(&fifo, vec![b' '; 64 * 1024 + 1]).unwrap();
        assert!(load_at(&directory).is_err());
    }

    #[cfg(windows)]
    #[test]
    fn oversized_state_is_rejected_on_windows() {
        let temp = tempfile::tempdir().unwrap();
        let directory = anchor(temp.path());
        fs::write(
            temp.path().join("state.json"),
            vec![b' '; 64 * 1024 + 1],
        )
        .unwrap();
        assert!(load_at(&directory).is_err());
    }

    #[test]
    fn concurrent_saves_never_publish_partial_json_or_leave_temps() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().to_path_buf();
        let barrier = Arc::new(Barrier::new(8));
        let mut workers = Vec::new();
        for worker in 0..8 {
            let root = root.clone();
            let barrier = barrier.clone();
            workers.push(std::thread::spawn(move || {
                let directory = anchor(&root);
                barrier.wait();
                for generation in 0..25 {
                    let mut state = RuntimeState::default();
                    state
                        .advance(RuntimeKind::Core, &format!("core-{worker}-{generation}"))
                        .unwrap();
                    save_at(&directory, &state).unwrap();
                    load_at(&directory).unwrap().validate().unwrap();
                }
            }));
        }
        for worker in workers {
            worker.join().unwrap();
        }

        let directory = anchor(temp.path());
        load_at(&directory).unwrap().validate().unwrap();
        assert!(fs::read_dir(temp.path()).unwrap().all(|item| {
            !item
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with(".state.json.tmp-")
        }));
    }

    #[test]
    fn invalid_save_does_not_replace_the_last_valid_state() {
        let temp = tempfile::tempdir().unwrap();
        let directory = anchor(temp.path());
        let mut valid = RuntimeState::default();
        valid.advance(RuntimeKind::Core, "core-v1").unwrap();
        save_at(&directory, &valid).unwrap();
        let before = fs::read(temp.path().join("state.json")).unwrap();

        let mut invalid = RuntimeState::default();
        invalid.core.active = Some("../outside".into());
        assert!(save_at(&directory, &invalid).is_err());

        assert_eq!(fs::read(temp.path().join("state.json")).unwrap(), before);
        assert_eq!(load_at(&directory).unwrap(), valid);
    }

    #[cfg(unix)]
    #[test]
    fn cleanup_failure_reports_both_the_primary_and_cleanup_errors() {
        let temp = tempfile::tempdir().unwrap();
        let directory = File::open(temp.path()).unwrap();
        let state = RuntimeState::default();
        let original_permissions = fs::metadata(temp.path()).unwrap().permissions();

        let result = save_at_with_hook(&directory, &state, || {
            fs::set_permissions(temp.path(), fs::Permissions::from_mode(0o500)).unwrap();
        });
        fs::set_permissions(temp.path(), original_permissions).unwrap();

        match result.unwrap_err() {
            StateError::Cleanup { primary, cleanup } => {
                assert!(primary.to_string().contains("runtime_state_io"));
                assert_eq!(cleanup.kind(), std::io::ErrorKind::PermissionDenied);
            }
            error => panic!("unexpected error: {error}"),
        }
    }
}
