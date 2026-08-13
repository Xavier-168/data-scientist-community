use std::{
    collections::HashMap,
    ffi::{CStr, CString, OsStr, OsString},
    fs::File,
    io::{self, Read, Write},
    ops::Deref,
    os::unix::ffi::{OsStrExt, OsStringExt},
    path::{Component, Path, PathBuf},
    str,
    sync::{Arc, Mutex, OnceLock, Weak},
    thread,
    time::Duration,
};

use fs2::FileExt;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::manifest::VerifiedPackageManifest;

use super::{RuntimeKind, RuntimeResolution};

const VIEW_LOCK_NAME: &str = ".view.lock";
const MANIFEST_NAME: &str = "package_manifest.json";
const GENERATION_MARKER_NAME: &str = ".view-generation.json";
const CURRENT_NAME: &str = "current";
const CURRENT_NEXT_NAME: &str = "current.next";
const LOCK_POLL: Duration = Duration::from_millis(25);
const MAX_VIEW_DEPTH: usize = 256;
const MAX_METADATA_BYTES: usize = 2 * 1024 * 1024;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct GenerationMarker {
    schema_version: u8,
    core_version: String,
    core_dev: u64,
    core_ino: u64,
    collector_version: Option<String>,
    collector_dev: Option<u64>,
    collector_ino: Option<u64>,
}

impl GenerationMarker {
    fn core(core: &SourceRoot) -> Self {
        Self {
            schema_version: 1,
            core_version: core.version.clone(),
            core_dev: core.identity.st_dev as u64,
            core_ino: core.identity.st_ino,
            collector_version: None,
            collector_dev: None,
            collector_ino: None,
        }
    }

    fn full(core: &SourceRoot, collector: &SourceRoot) -> Self {
        Self {
            schema_version: 1,
            core_version: core.version.clone(),
            core_dev: core.identity.st_dev as u64,
            core_ino: core.identity.st_ino,
            collector_version: Some(collector.version.clone()),
            collector_dev: Some(collector.identity.st_dev as u64),
            collector_ino: Some(collector.identity.st_ino),
        }
    }

    fn validate(&self) -> Result<(), String> {
        if self.schema_version != 1
            || !valid_version(&self.core_version)
            || self
                .collector_version
                .as_deref()
                .is_some_and(|version| !valid_version(version))
            || !matches!(
                (
                    self.collector_version.is_some(),
                    self.collector_dev.is_some(),
                    self.collector_ino.is_some(),
                ),
                (false, false, false) | (true, true, true)
            )
        {
            return Err("view_generation_marker_invalid".into());
        }
        Ok(())
    }
}

struct SourceRoot {
    input: PathBuf,
    canonical: PathBuf,
    version: String,
    identity: rustix::fs::Stat,
    scripts: File,
    frontend: Option<File>,
    python: Option<File>,
    node: Option<File>,
    browser: Option<File>,
    node_modules: Option<File>,
}

struct ViewFileLock<'a>(&'a File);

impl Drop for ViewFileLock<'_> {
    fn drop(&mut self) {
        let _ = FileExt::unlock(self.0);
    }
}

#[derive(Clone)]
pub struct VerifiedView {
    current: PathBuf,
    target: String,
    build: Arc<File>,
    generation: Arc<File>,
}

impl VerifiedView {
    pub fn path(&self) -> &Path {
        &self.current
    }

    pub(crate) fn verify_visible(&self) -> Result<(), String> {
        use rustix::fs::{fstat, readlinkat, FileType};

        let build_path = self
            .current
            .parent()
            .ok_or_else(|| "view_handle_changed".to_string())?;
        let visible_build =
            open_directory(build_path).map_err(|_| "view_handle_changed".to_string())?;
        let held_build = fstat(&*self.build).map_err(|error| io::Error::from(error).to_string())?;
        let visible_build_stat =
            fstat(&visible_build).map_err(|error| io::Error::from(error).to_string())?;
        if !same_identity(&held_build, &visible_build_stat) {
            return Err("view_handle_changed".into());
        }
        let Some(before) = stat_optional(&visible_build, CURRENT_NAME)? else {
            return Err("view_handle_changed".into());
        };
        if FileType::from_raw_mode(before.st_mode) != FileType::Symlink {
            return Err("view_handle_changed".into());
        }
        let target = readlinkat(&visible_build, CURRENT_NAME, Vec::new())
            .map_err(|_| "view_handle_changed".to_string())?;
        let Some(after) = stat_optional(&visible_build, CURRENT_NAME)? else {
            return Err("view_handle_changed".into());
        };
        if !same_identity(&before, &after) || target.as_bytes() != self.target.as_bytes() {
            return Err("view_handle_changed".into());
        }
        let visible_generation = open_child_directory(&visible_build, self.target.as_str())
            .map_err(|_| "view_handle_changed".to_string())?;
        let held_generation =
            fstat(&*self.generation).map_err(|error| io::Error::from(error).to_string())?;
        let visible_generation_stat =
            fstat(&visible_generation).map_err(|error| io::Error::from(error).to_string())?;
        if !same_identity(&held_generation, &visible_generation_stat) {
            return Err("view_handle_changed".into());
        }
        Ok(())
    }

    pub(crate) fn pinned_launch_root(&self) -> Result<PathBuf, String> {
        use rustix::fs::fstat;

        let build_path = self
            .current
            .parent()
            .ok_or_else(|| "view_handle_changed".to_string())?;
        let visible_build =
            open_directory(build_path).map_err(|_| "view_handle_changed".to_string())?;
        let held_build = fstat(&*self.build).map_err(|error| io::Error::from(error).to_string())?;
        let visible_build_stat =
            fstat(&visible_build).map_err(|error| io::Error::from(error).to_string())?;
        if !same_identity(&held_build, &visible_build_stat) {
            return Err("view_handle_changed".into());
        }
        let visible_generation = open_child_directory(&visible_build, self.target.as_str())
            .map_err(|_| "view_handle_changed".to_string())?;
        let held_generation =
            fstat(&*self.generation).map_err(|error| io::Error::from(error).to_string())?;
        let visible_generation_stat =
            fstat(&visible_generation).map_err(|error| io::Error::from(error).to_string())?;
        if !same_identity(&held_generation, &visible_generation_stat) {
            return Err("view_handle_changed".into());
        }
        Ok(build_path.join(&self.target))
    }
}

impl Deref for VerifiedView {
    type Target = Path;

    fn deref(&self) -> &Self::Target {
        self.path()
    }
}

impl AsRef<Path> for VerifiedView {
    fn as_ref(&self) -> &Path {
        self.path()
    }
}

impl std::fmt::Debug for VerifiedView {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("VerifiedView")
            .field("current", &self.current)
            .field("target", &self.target)
            .finish_non_exhaustive()
    }
}

impl PartialEq for VerifiedView {
    fn eq(&self, other: &Self) -> bool {
        self.current == other.current
    }
}

impl Eq for VerifiedView {}

#[derive(Clone)]
pub struct ViewManager {
    state_root: PathBuf,
    build_root: PathBuf,
    manifest: Arc<VerifiedPackageManifest>,
    _state: Arc<File>,
    downloads: Arc<File>,
    auth: Arc<File>,
    _views: Arc<File>,
    build: Arc<File>,
    lock: Arc<File>,
    gate: Arc<Mutex<()>>,
    #[cfg(test)]
    fail_switch_sync: Arc<std::sync::atomic::AtomicBool>,
    #[cfg(test)]
    fail_pre_switch_sync: Arc<std::sync::atomic::AtomicBool>,
    #[cfg(test)]
    fail_switch_rename: Arc<std::sync::atomic::AtomicBool>,
    #[cfg(test)]
    fail_generation_parent_sync: Arc<std::sync::atomic::AtomicBool>,
    #[cfg(test)]
    fail_verified_view: Arc<std::sync::atomic::AtomicBool>,
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
        return Err("view_directory_invalid".into());
    }
    Ok(File::from(fd))
}

fn open_child_directory<P: rustix::path::Arg>(parent: &File, name: P) -> Result<File, String> {
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
        return Err("view_directory_invalid".into());
    }
    Ok(File::from(fd))
}

fn ensure_child_directory(parent: &File, name: &str, mode: u16) -> Result<File, String> {
    use rustix::fs::{mkdirat, Mode};

    match mkdirat(parent, name, Mode::from_raw_mode(mode)) {
        Ok(()) | Err(rustix::io::Errno::EXIST) => open_child_directory(parent, name),
        Err(error) => Err(io::Error::from(error).to_string()),
    }
}

fn stat_optional<P: rustix::path::Arg>(
    parent: &File,
    name: P,
) -> Result<Option<rustix::fs::Stat>, String> {
    use rustix::fs::{statat, AtFlags};

    match statat(parent, name, AtFlags::SYMLINK_NOFOLLOW) {
        Ok(stat) => Ok(Some(stat)),
        Err(rustix::io::Errno::NOENT) => Ok(None),
        Err(error) => Err(io::Error::from(error).to_string()),
    }
}

fn read_regular(parent: &File, name: &str) -> Result<Vec<u8>, String> {
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
        || stat.st_size as usize > MAX_METADATA_BYTES
    {
        return Err("view_metadata_invalid".into());
    }
    let mut file = File::from(fd);
    let mut bytes = Vec::with_capacity(stat.st_size as usize);
    Read::by_ref(&mut file)
        .take((MAX_METADATA_BYTES + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|error| error.to_string())?;
    if bytes.len() > MAX_METADATA_BYTES {
        return Err("view_metadata_too_large".into());
    }
    Ok(bytes)
}

fn write_regular(parent: &File, name: &str, bytes: &[u8], mode: u16) -> Result<(), String> {
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
        Mode::from_raw_mode(mode),
    )
    .map_err(|error| io::Error::from(error).to_string())?;
    let mut file = File::from(fd);
    fchmod(&file, Mode::from_raw_mode(mode)).map_err(|error| io::Error::from(error).to_string())?;
    file.write_all(bytes).map_err(|error| error.to_string())?;
    file.sync_all().map_err(|error| error.to_string())
}

type ViewGate = Mutex<()>;
type ViewGateRegistry = Mutex<HashMap<(u64, u64), Weak<ViewGate>>>;

fn shared_gate(identity: (u64, u64)) -> Result<Arc<ViewGate>, String> {
    static REGISTRY: OnceLock<ViewGateRegistry> = OnceLock::new();
    let registry = REGISTRY.get_or_init(|| Mutex::new(HashMap::new()));
    let mut registry = registry
        .lock()
        .map_err(|_| "view_gate_registry_poisoned".to_string())?;
    registry.retain(|_, gate| gate.strong_count() != 0);
    if let Some(gate) = registry.get(&identity).and_then(Weak::upgrade) {
        return Ok(gate);
    }
    let gate = Arc::new(Mutex::new(()));
    registry.insert(identity, Arc::downgrade(&gate));
    Ok(gate)
}

fn valid_version(value: &str) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 128
        && bytes[0].is_ascii_alphanumeric()
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(byte))
        && !value.contains(".tmp-")
}

fn component_replacement_allowed(current: &str, requested: &str, signed: &str) -> bool {
    current == requested || requested == signed
}

fn normalize_source(
    resolution: &RuntimeResolution,
    kind: RuntimeKind,
) -> Result<SourceRoot, String> {
    use rustix::fs::fstat;

    if resolution.kind() != kind
        || !valid_version(resolution.version())
        || resolution.root().file_name() != Some(OsStr::new(resolution.version()))
    {
        return Err("view_resolution_invalid".into());
    }
    let metadata =
        std::fs::symlink_metadata(resolution.root()).map_err(|error| error.to_string())?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err("view_source_not_real_directory".into());
    }
    let canonical = std::fs::canonicalize(resolution.root()).map_err(|error| error.to_string())?;
    let directory = resolution.duplicate_directory()?;
    let identity = fstat(&directory).map_err(|error| io::Error::from(error).to_string())?;
    if (identity.st_dev as u64, identity.st_ino as u64) != resolution.identity() {
        return Err("view_resolution_identity_invalid".into());
    }
    let visible = open_directory(resolution.root())?;
    let visible_identity = fstat(&visible).map_err(|error| io::Error::from(error).to_string())?;
    if !same_identity(&identity, &visible_identity) {
        return Err("view_resolution_changed".into());
    }
    let scripts = open_relative_directory(&directory, "scripts")?;
    let (frontend, python, node, browser, node_modules) = match kind {
        RuntimeKind::Core => (
            Some(open_relative_directory(&directory, "frontend-compat")?),
            Some(open_relative_directory(&directory, "runtime/python-arm64")?),
            None,
            None,
            None,
        ),
        RuntimeKind::Collector => (
            None,
            None,
            Some(open_relative_directory(&directory, "runtime/node-arm64")?),
            Some(open_relative_directory(
                &directory,
                "runtime/playwright-browsers",
            )?),
            Some(open_relative_directory(&directory, "node_modules")?),
        ),
    };
    Ok(SourceRoot {
        input: resolution.root().into(),
        canonical,
        version: resolution.version().into(),
        identity,
        scripts,
        frontend,
        python,
        node,
        browser,
        node_modules,
    })
}

fn open_relative_directory(root: &File, relative: &str) -> Result<File, String> {
    let mut directory =
        File::from(rustix::io::dup(root).map_err(|error| io::Error::from(error).to_string())?);
    for component in relative.split('/') {
        directory = open_child_directory(&directory, component)?;
    }
    Ok(directory)
}

fn verify_source_visible(source: &SourceRoot) -> Result<(), String> {
    use rustix::fs::fstat;

    let visible_root = open_directory(&source.input)?;
    let visible_identity =
        fstat(&visible_root).map_err(|error| io::Error::from(error).to_string())?;
    if !same_identity(&source.identity, &visible_identity) {
        return Err("view_source_changed".into());
    }
    for (relative, held) in [
        ("scripts", Some(&source.scripts)),
        ("frontend-compat", source.frontend.as_ref()),
        ("runtime/python-arm64", source.python.as_ref()),
        ("runtime/node-arm64", source.node.as_ref()),
        ("runtime/playwright-browsers", source.browser.as_ref()),
        ("node_modules", source.node_modules.as_ref()),
    ] {
        let Some(held) = held else {
            continue;
        };
        let visible = open_relative_directory(&visible_root, relative)
            .map_err(|_| "view_source_subtree_changed".to_string())?;
        let held = fstat(held).map_err(|error| io::Error::from(error).to_string())?;
        let visible = fstat(&visible).map_err(|error| io::Error::from(error).to_string())?;
        if !same_identity(&held, &visible) {
            return Err("view_source_subtree_changed".into());
        }
    }
    Ok(())
}

fn relative_path(from: &Path, target: &Path) -> Result<PathBuf, String> {
    let from: Vec<_> = from.components().collect();
    let target: Vec<_> = target.components().collect();
    let mut common = 0;
    while common < from.len() && common < target.len() && from[common] == target[common] {
        common += 1;
    }
    if common == 0 {
        return Err("view_relative_link_unavailable".into());
    }
    let mut result = PathBuf::new();
    for _ in common..from.len() {
        result.push("..");
    }
    for component in &target[common..] {
        result.push(component.as_os_str());
    }
    if result.as_os_str().is_empty() || result.is_absolute() {
        return Err("view_relative_link_invalid".into());
    }
    Ok(result)
}

fn safe_internal_link(relative_parent: &Path, target: &Path) -> bool {
    if target.is_absolute() {
        return false;
    }
    let mut depth = relative_parent.components().count();
    for component in target.components() {
        match component {
            Component::Normal(_) => depth += 1,
            Component::CurDir => {}
            Component::ParentDir if depth != 0 => depth -= 1,
            _ => return false,
        }
    }
    true
}

fn copy_regular(
    source: &File,
    destination: &File,
    name: &CStr,
    expected: &rustix::fs::Stat,
) -> Result<(), String> {
    use rustix::fs::{linkat, statat, AtFlags};

    let before = statat(source, name, AtFlags::SYMLINK_NOFOLLOW)
        .map_err(|error| io::Error::from(error).to_string())?;
    if !same_identity(expected, &before) {
        return Err("view_source_entry_changed".into());
    }

    match linkat(source, name, destination, name, AtFlags::empty()) {
        Ok(()) => {
            let source_after = statat(source, name, AtFlags::SYMLINK_NOFOLLOW)
                .map_err(|error| io::Error::from(error).to_string())?;
            let destination_after = statat(destination, name, AtFlags::SYMLINK_NOFOLLOW)
                .map_err(|error| io::Error::from(error).to_string())?;
            if !same_identity(expected, &source_after)
                || !same_identity(expected, &destination_after)
            {
                return Err("view_source_entry_changed".into());
            }
            Ok(())
        }
        Err(rustix::io::Errno::XDEV) => Err("view_scripts_cross_device".into()),
        Err(rustix::io::Errno::EXIST) => Err("view_script_collision".into()),
        Err(error) => Err(io::Error::from(error).to_string()),
    }
}

fn overlay_scripts(
    source: &File,
    destination: &File,
    relative: &Path,
    skip_root_key_bundle: bool,
) -> Result<(), String> {
    use rustix::fs::{
        fchmod, fstat, mkdirat, readlinkat, statat, symlinkat, AtFlags, Dir, FileType, Mode,
    };

    let mut entries = Vec::new();
    for item in Dir::read_from(source).map_err(|error| io::Error::from(error).to_string())? {
        let item = item.map_err(|error| io::Error::from(error).to_string())?;
        if !matches!(item.file_name().to_bytes(), b"." | b"..") {
            entries.push(item.file_name().to_owned());
        }
    }
    entries.sort_by(|left, right| left.to_bytes().cmp(right.to_bytes()));
    for name in entries {
        if name.to_bytes() == b"package_public_keys.json" && relative.as_os_str().is_empty() {
            if skip_root_key_bundle {
                continue;
            }
            return Err("view_script_collision".into());
        }
        if stat_optional(destination, &name)?.is_some() {
            return Err("view_script_collision".into());
        }
        let stat = statat(source, &name, AtFlags::SYMLINK_NOFOLLOW)
            .map_err(|error| io::Error::from(error).to_string())?;
        match FileType::from_raw_mode(stat.st_mode) {
            FileType::RegularFile => copy_regular(source, destination, &name, &stat)?,
            FileType::Directory => {
                let source_child = open_child_directory(source, &name)?;
                let opened =
                    fstat(&source_child).map_err(|error| io::Error::from(error).to_string())?;
                if !same_identity(&stat, &opened) {
                    return Err("view_source_entry_changed".into());
                }
                mkdirat(destination, &name, Mode::from_raw_mode(0o755))
                    .map_err(|error| io::Error::from(error).to_string())?;
                let destination_child = open_child_directory(destination, &name)?;
                fchmod(&destination_child, Mode::from_raw_mode(0o755))
                    .map_err(|error| io::Error::from(error).to_string())?;
                overlay_scripts(
                    &source_child,
                    &destination_child,
                    &relative.join(OsStr::from_bytes(name.to_bytes())),
                    skip_root_key_bundle,
                )?;
                let source_after = statat(source, &name, AtFlags::SYMLINK_NOFOLLOW)
                    .map_err(|error| io::Error::from(error).to_string())?;
                if !same_identity(&opened, &source_after) {
                    return Err("view_source_entry_changed".into());
                }
                rustix::fs::fsync(&destination_child)
                    .map_err(|error| io::Error::from(error).to_string())?;
            }
            FileType::Symlink => {
                let target = readlinkat(source, &name, Vec::new())
                    .map_err(|error| io::Error::from(error).to_string())?;
                let source_after = statat(source, &name, AtFlags::SYMLINK_NOFOLLOW)
                    .map_err(|error| io::Error::from(error).to_string())?;
                if !same_identity(&stat, &source_after) {
                    return Err("view_source_entry_changed".into());
                }
                let target_path = PathBuf::from(OsString::from_vec(target.as_bytes().to_vec()));
                if !safe_internal_link(relative, &target_path) {
                    return Err("view_script_link_unsafe".into());
                }
                symlinkat(&target_path, destination, &name)
                    .map_err(|error| io::Error::from(error).to_string())?;
            }
            _ => return Err("view_script_type_unsafe".into()),
        }
    }
    Ok(())
}

fn directory_names(directory: &File) -> Result<Vec<CString>, String> {
    use rustix::fs::Dir;

    let mut names = Vec::new();
    for item in Dir::read_from(directory).map_err(|error| io::Error::from(error).to_string())? {
        let item = item.map_err(|error| io::Error::from(error).to_string())?;
        if !matches!(item.file_name().to_bytes(), b"." | b"..") {
            names.push(item.file_name().to_owned());
        }
    }
    names.sort_by(|left, right| left.to_bytes().cmp(right.to_bytes()));
    Ok(names)
}

fn directory_has_mode(directory: &File, mode: u16) -> Result<bool, String> {
    use rustix::fs::{fstat, FileType};

    let stat = fstat(directory).map_err(|error| io::Error::from(error).to_string())?;
    Ok(
        FileType::from_raw_mode(stat.st_mode) == FileType::Directory
            && stat.st_mode & 0o777 == mode,
    )
}

fn validate_scripts_overlay(
    core: Option<&File>,
    collector: Option<&File>,
    destination: &File,
    relative: &Path,
    key_bundle: &[u8],
) -> Result<bool, String> {
    use rustix::fs::{fstat, readlinkat, statat, AtFlags, FileType};

    if !directory_has_mode(destination, 0o555)? {
        return Ok(false);
    }
    let core_names = core.map(directory_names).transpose()?.unwrap_or_default();
    let collector_names = collector
        .map(directory_names)
        .transpose()?
        .unwrap_or_default();
    let mut expected = Vec::<CString>::new();
    for name in &core_names {
        if relative.as_os_str().is_empty() && name.to_bytes() == b"package_public_keys.json" {
            continue;
        }
        expected.push(name.clone());
    }
    for name in &collector_names {
        if relative.as_os_str().is_empty() && name.to_bytes() == b"package_public_keys.json" {
            return Ok(false);
        }
        if expected
            .iter()
            .any(|existing| existing.to_bytes() == name.to_bytes())
        {
            return Ok(false);
        }
        expected.push(name.clone());
    }
    if relative.as_os_str().is_empty() {
        expected.push(CString::new("package_public_keys.json").unwrap());
    }
    expected.sort_by(|left, right| left.to_bytes().cmp(right.to_bytes()));
    if directory_names(destination)? != expected {
        return Ok(false);
    }

    for name in expected {
        if relative.as_os_str().is_empty() && name.to_bytes() == b"package_public_keys.json" {
            if validate_regular_mode(destination, "package_public_keys.json", 0o400).is_err()
                || read_regular(destination, "package_public_keys.json")? != key_bundle
            {
                return Ok(false);
            }
            continue;
        }
        let core_stat = core
            .map(|source| stat_optional(source, &name))
            .transpose()?
            .flatten();
        let collector_stat = collector
            .map(|source| stat_optional(source, &name))
            .transpose()?
            .flatten();
        let (source, source_stat) = match (core, core_stat, collector, collector_stat) {
            (Some(source), Some(stat), _, None) => (source, stat),
            (_, None, Some(source), Some(stat)) => (source, stat),
            _ => return Ok(false),
        };
        let Some(destination_stat) = stat_optional(destination, &name)? else {
            return Ok(false);
        };
        let source_type = FileType::from_raw_mode(source_stat.st_mode);
        if source_type != FileType::from_raw_mode(destination_stat.st_mode) {
            return Ok(false);
        }
        match source_type {
            FileType::RegularFile => {
                let source_after = statat(source, &name, AtFlags::SYMLINK_NOFOLLOW)
                    .map_err(|error| io::Error::from(error).to_string())?;
                let destination_after = statat(destination, &name, AtFlags::SYMLINK_NOFOLLOW)
                    .map_err(|error| io::Error::from(error).to_string())?;
                if !same_identity(&source_stat, &source_after)
                    || !same_identity(&source_stat, &destination_after)
                {
                    return Ok(false);
                }
            }
            FileType::Directory => {
                let source_child = open_child_directory(source, &name)?;
                let source_opened =
                    fstat(&source_child).map_err(|error| io::Error::from(error).to_string())?;
                let destination_child = open_child_directory(destination, &name)?;
                if !same_identity(&source_stat, &source_opened)
                    || !validate_scripts_overlay(
                        Some(&source_child),
                        None,
                        &destination_child,
                        &relative.join(OsStr::from_bytes(name.to_bytes())),
                        key_bundle,
                    )?
                {
                    return Ok(false);
                }
            }
            FileType::Symlink => {
                let source_target = readlinkat(source, &name, Vec::new())
                    .map_err(|error| io::Error::from(error).to_string())?;
                let destination_target = readlinkat(destination, &name, Vec::new())
                    .map_err(|error| io::Error::from(error).to_string())?;
                let source_after = statat(source, &name, AtFlags::SYMLINK_NOFOLLOW)
                    .map_err(|error| io::Error::from(error).to_string())?;
                let destination_after = statat(destination, &name, AtFlags::SYMLINK_NOFOLLOW)
                    .map_err(|error| io::Error::from(error).to_string())?;
                let target = PathBuf::from(OsString::from_vec(source_target.as_bytes().to_vec()));
                if !same_identity(&source_stat, &source_after)
                    || !same_identity(&destination_stat, &destination_after)
                    || source_target.as_bytes() != destination_target.as_bytes()
                    || !safe_internal_link(relative, &target)
                {
                    return Ok(false);
                }
            }
            _ => return Ok(false),
        }
    }
    Ok(true)
}

fn seal_directory_tree(directory: &File) -> Result<(), String> {
    use rustix::fs::{fchmod, fsync, statat, AtFlags, FileType, Mode};

    for name in directory_names(directory)? {
        let stat = statat(directory, &name, AtFlags::SYMLINK_NOFOLLOW)
            .map_err(|error| io::Error::from(error).to_string())?;
        if FileType::from_raw_mode(stat.st_mode) == FileType::Directory {
            let child = open_child_directory(directory, &name)?;
            seal_directory_tree(&child)?;
        }
    }
    fchmod(directory, Mode::from_raw_mode(0o555))
        .map_err(|error| io::Error::from(error).to_string())?;
    fsync(directory).map_err(|error| io::Error::from(error).to_string())
}

fn remove_contents(directory: &File, depth: usize) -> Result<(), String> {
    use rustix::fs::{
        fchmod, fstat, openat, statat, unlinkat, AtFlags, Dir, FileType, Mode, OFlags,
    };

    if depth > MAX_VIEW_DEPTH {
        return Err("view_cleanup_depth".into());
    }
    fchmod(directory, Mode::from_raw_mode(0o700))
        .map_err(|error| io::Error::from(error).to_string())?;
    let mut names = Vec::new();
    for item in Dir::read_from(directory).map_err(|error| io::Error::from(error).to_string())? {
        let item = item.map_err(|error| io::Error::from(error).to_string())?;
        if !matches!(item.file_name().to_bytes(), b"." | b"..") {
            names.push(item.file_name().to_owned());
        }
    }
    for name in names {
        let before = statat(directory, &name, AtFlags::SYMLINK_NOFOLLOW)
            .map_err(|error| io::Error::from(error).to_string())?;
        if FileType::from_raw_mode(before.st_mode) == FileType::Directory {
            let fd = openat(
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
            let child = File::from(fd);
            let opened = fstat(&child).map_err(|error| io::Error::from(error).to_string())?;
            if !same_identity(&before, &opened) {
                return Err("view_cleanup_changed".into());
            }
            remove_contents(&child, depth + 1)?;
            let after = statat(directory, &name, AtFlags::SYMLINK_NOFOLLOW)
                .map_err(|error| io::Error::from(error).to_string())?;
            if !same_identity(&opened, &after) {
                return Err("view_cleanup_changed".into());
            }
            unlinkat(directory, &name, AtFlags::REMOVEDIR)
                .map_err(|error| io::Error::from(error).to_string())?;
        } else {
            let after = statat(directory, &name, AtFlags::SYMLINK_NOFOLLOW)
                .map_err(|error| io::Error::from(error).to_string())?;
            if !same_identity(&before, &after) {
                return Err("view_cleanup_changed".into());
            }
            unlinkat(directory, &name, AtFlags::empty())
                .map_err(|error| io::Error::from(error).to_string())?;
        }
    }
    Ok(())
}

fn remove_name(parent: &File, name: &str) -> Result<(), String> {
    remove_name_with_hook(parent, name, || {})
}

fn remove_name_with_hook<F: FnOnce()>(
    parent: &File,
    name: &str,
    after_initial_stat: F,
) -> Result<(), String> {
    use rustix::fs::{fstat, unlinkat, AtFlags, FileType};

    let Some(before) = stat_optional(parent, name)? else {
        return Ok(());
    };
    after_initial_stat();
    if FileType::from_raw_mode(before.st_mode) == FileType::Directory {
        let directory = open_child_directory(parent, name)?;
        let opened = fstat(&directory).map_err(|error| io::Error::from(error).to_string())?;
        if !same_identity(&before, &opened) {
            return Err("view_cleanup_changed".into());
        }
        remove_contents(&directory, 0)?;
        let Some(after) = stat_optional(parent, name)? else {
            return Err("view_cleanup_changed".into());
        };
        if !same_identity(&opened, &after) {
            return Err("view_cleanup_changed".into());
        }
        unlinkat(parent, name, AtFlags::REMOVEDIR)
            .map_err(|error| io::Error::from(error).to_string())?;
    } else {
        let Some(after) = stat_optional(parent, name)? else {
            return Err("view_cleanup_changed".into());
        };
        if !same_identity(&before, &after) {
            return Err("view_cleanup_changed".into());
        }
        unlinkat(parent, name, AtFlags::empty())
            .map_err(|error| io::Error::from(error).to_string())?;
    }
    Ok(())
}

fn parse_uuid_name<'a>(value: &'a str, prefix: &str) -> Option<&'a str> {
    let suffix = value.strip_prefix(prefix)?;
    Uuid::parse_str(suffix).ok().map(|_| suffix)
}

fn validate_regular_mode(parent: &File, name: &str, mode: u16) -> Result<(), String> {
    use rustix::fs::FileType;

    let Some(stat) = stat_optional(parent, name)? else {
        return Err("view_generation_structure_invalid".into());
    };
    if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile
        || stat.st_mode & 0o777 != mode
    {
        return Err("view_generation_structure_invalid".into());
    }
    Ok(())
}

fn validate_relative_link(
    parent: &File,
    name: &str,
    expected: Option<&Path>,
) -> Result<PathBuf, String> {
    use rustix::fs::{readlinkat, FileType};

    let Some(before) = stat_optional(parent, name)? else {
        return Err("view_generation_structure_invalid".into());
    };
    if FileType::from_raw_mode(before.st_mode) != FileType::Symlink {
        return Err("view_generation_structure_invalid".into());
    }
    let target =
        readlinkat(parent, name, Vec::new()).map_err(|error| io::Error::from(error).to_string())?;
    let target = PathBuf::from(OsString::from_vec(target.as_bytes().to_vec()));
    let Some(after) = stat_optional(parent, name)? else {
        return Err("view_generation_structure_invalid".into());
    };
    if !same_identity(&before, &after)
        || target.as_os_str().is_empty()
        || target.is_absolute()
        || expected.is_some_and(|expected| target != expected)
    {
        return Err("view_generation_structure_invalid".into());
    }
    Ok(target)
}

fn validate_empty_directory(parent: &File, name: &str) -> Result<(), String> {
    use rustix::fs::Dir;

    let directory = open_child_directory(parent, name)
        .map_err(|_| "view_generation_structure_invalid".to_string())?;
    if !directory_has_mode(&directory, 0o555)? {
        return Err("view_generation_structure_invalid".into());
    }
    for item in Dir::read_from(&directory).map_err(|error| io::Error::from(error).to_string())? {
        let item = item.map_err(|error| io::Error::from(error).to_string())?;
        if !matches!(item.file_name().to_bytes(), b"." | b"..") {
            return Err("view_generation_structure_invalid".into());
        }
    }
    Ok(())
}

fn validate_linked_directory_identity(
    parent_path: &Path,
    target: &Path,
    expected: &File,
) -> Result<(), String> {
    use rustix::fs::fstat;

    let path = std::fs::canonicalize(parent_path.join(target))
        .map_err(|_| "view_generation_structure_invalid".to_string())?;
    let visible =
        open_directory(&path).map_err(|_| "view_generation_structure_invalid".to_string())?;
    let expected = fstat(expected).map_err(|error| io::Error::from(error).to_string())?;
    let visible = fstat(&visible).map_err(|error| io::Error::from(error).to_string())?;
    if !same_identity(&expected, &visible) {
        return Err("view_generation_structure_invalid".into());
    }
    Ok(())
}

impl ViewManager {
    pub fn new(
        state_root: PathBuf,
        manifest: Arc<VerifiedPackageManifest>,
    ) -> Result<Self, String> {
        use rustix::fs::{fchmod, fstat, fsync, openat, FileType, Mode, OFlags};

        let build_version = manifest.manifest().build_version.clone();
        if !valid_version(&build_version) {
            return Err("view_build_version_invalid".into());
        }
        let metadata = std::fs::symlink_metadata(&state_root).map_err(|error| error.to_string())?;
        if !metadata.is_dir() || metadata.file_type().is_symlink() {
            return Err("view_state_root_invalid".into());
        }
        let state_root = std::fs::canonicalize(&state_root).map_err(|error| error.to_string())?;
        let state = open_directory(&state_root)?;
        let downloads = ensure_child_directory(&state, "downloads", 0o700)?;
        let auth = ensure_child_directory(&state, ".auth", 0o700)?;
        let runtimes = ensure_child_directory(&state, "runtimes", 0o700)?;
        let views = ensure_child_directory(&runtimes, "views", 0o700)?;
        let build = ensure_child_directory(&views, &build_version, 0o700)?;
        for directory in [&downloads, &auth, &runtimes, &views, &build] {
            fchmod(directory, Mode::from_raw_mode(0o700))
                .map_err(|error| io::Error::from(error).to_string())?;
        }
        fsync(&state).map_err(|error| io::Error::from(error).to_string())?;
        fsync(&downloads).map_err(|error| io::Error::from(error).to_string())?;
        fsync(&auth).map_err(|error| io::Error::from(error).to_string())?;
        fsync(&runtimes).map_err(|error| io::Error::from(error).to_string())?;
        fsync(&views).map_err(|error| io::Error::from(error).to_string())?;
        let build_root = state_root.join("runtimes/views").join(&build_version);
        let lock_fd = openat(
            &build,
            VIEW_LOCK_NAME,
            OFlags::RDWR | OFlags::CREATE | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
            Mode::from_raw_mode(0o600),
        )
        .map_err(|error| io::Error::from(error).to_string())?;
        let lock = File::from(lock_fd);
        let lock_stat = fstat(&lock).map_err(|error| io::Error::from(error).to_string())?;
        if FileType::from_raw_mode(lock_stat.st_mode) != FileType::RegularFile
            || lock_stat.st_nlink != 1
        {
            return Err("view_lock_invalid".into());
        }
        fchmod(&lock, Mode::from_raw_mode(0o600))
            .map_err(|error| io::Error::from(error).to_string())?;
        fsync(&build).map_err(|error| io::Error::from(error).to_string())?;
        let identity = fstat(&build).map_err(|error| io::Error::from(error).to_string())?;
        let gate = shared_gate((identity.st_dev as u64, identity.st_ino as u64))?;
        Ok(Self {
            state_root,
            build_root,
            manifest,
            _state: Arc::new(state),
            downloads: Arc::new(downloads),
            auth: Arc::new(auth),
            _views: Arc::new(views),
            build: Arc::new(build),
            lock: Arc::new(lock),
            gate,
            #[cfg(test)]
            fail_switch_sync: Arc::new(std::sync::atomic::AtomicBool::new(false)),
            #[cfg(test)]
            fail_pre_switch_sync: Arc::new(std::sync::atomic::AtomicBool::new(false)),
            #[cfg(test)]
            fail_switch_rename: Arc::new(std::sync::atomic::AtomicBool::new(false)),
            #[cfg(test)]
            fail_generation_parent_sync: Arc::new(std::sync::atomic::AtomicBool::new(false)),
            #[cfg(test)]
            fail_verified_view: Arc::new(std::sync::atomic::AtomicBool::new(false)),
        })
    }

    fn acquire_file_lock(&self) -> Result<ViewFileLock<'_>, String> {
        loop {
            match FileExt::try_lock_exclusive(&*self.lock) {
                Ok(()) => return Ok(ViewFileLock(&self.lock)),
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => thread::sleep(LOCK_POLL),
                Err(error) => return Err(error.to_string()),
            }
        }
    }

    fn verify_manager_visible(&self) -> Result<(), String> {
        use rustix::fs::{fstat, FileType};

        let visible_state =
            open_directory(&self.state_root).map_err(|_| "view_state_root_changed".to_string())?;
        let expected_state =
            fstat(&*self._state).map_err(|error| io::Error::from(error).to_string())?;
        let actual_state =
            fstat(&visible_state).map_err(|error| io::Error::from(error).to_string())?;
        if !same_identity(&expected_state, &actual_state) {
            return Err("view_state_root_changed".into());
        }
        for (name, held) in [("downloads", &*self.downloads), (".auth", &*self.auth)] {
            let visible = open_child_directory(&visible_state, name)
                .map_err(|_| "view_state_child_changed".to_string())?;
            let expected = fstat(held).map_err(|error| io::Error::from(error).to_string())?;
            let actual = fstat(&visible).map_err(|error| io::Error::from(error).to_string())?;
            if !same_identity(&expected, &actual) {
                return Err("view_state_child_changed".into());
            }
        }
        let runtimes = open_child_directory(&visible_state, "runtimes")
            .map_err(|_| "view_state_root_changed".to_string())?;
        let views = open_child_directory(&runtimes, "views")
            .map_err(|_| "view_state_root_changed".to_string())?;
        let expected_views =
            fstat(&*self._views).map_err(|error| io::Error::from(error).to_string())?;
        let actual_views = fstat(&views).map_err(|error| io::Error::from(error).to_string())?;
        if !same_identity(&expected_views, &actual_views) {
            return Err("view_state_root_changed".into());
        }
        let build_name = self
            .build_root
            .file_name()
            .ok_or_else(|| "view_state_root_changed".to_string())?;
        let build = open_child_directory(&views, build_name)
            .map_err(|_| "view_state_root_changed".to_string())?;
        let expected_build =
            fstat(&*self.build).map_err(|error| io::Error::from(error).to_string())?;
        let actual_build = fstat(&build).map_err(|error| io::Error::from(error).to_string())?;
        if !same_identity(&expected_build, &actual_build) {
            return Err("view_state_root_changed".into());
        }
        let lock_visible = stat_optional(&build, VIEW_LOCK_NAME)?
            .ok_or_else(|| "view_lock_changed".to_string())?;
        let lock_held = fstat(&*self.lock).map_err(|error| io::Error::from(error).to_string())?;
        if FileType::from_raw_mode(lock_visible.st_mode) != FileType::RegularFile
            || !same_identity(&lock_visible, &lock_held)
            || lock_visible.st_nlink != 1
            || lock_held.st_nlink != 1
        {
            return Err("view_lock_changed".into());
        }
        Ok(())
    }

    fn generation_info(
        &self,
        name: &str,
        generation: &File,
    ) -> Result<(GenerationMarker, bool), String> {
        use rustix::fs::{fstat, FileType};

        let root_stat = fstat(generation).map_err(|error| io::Error::from(error).to_string())?;
        if FileType::from_raw_mode(root_stat.st_mode) != FileType::Directory
            || root_stat.st_mode & 0o777 != 0o500
        {
            return Err("view_generation_structure_invalid".into());
        }
        validate_regular_mode(generation, MANIFEST_NAME, 0o400)?;
        if read_regular(generation, MANIFEST_NAME)? != self.manifest.signed_bytes() {
            return Err("view_build_collision".into());
        }
        validate_regular_mode(generation, GENERATION_MARKER_NAME, 0o400)?;
        let marker: GenerationMarker =
            serde_json::from_slice(&read_regular(generation, GENERATION_MARKER_NAME)?)
                .map_err(|_| "view_generation_marker_invalid".to_string())?;
        marker.validate()?;

        let scripts = open_child_directory(generation, "scripts")
            .map_err(|_| "view_generation_structure_invalid".to_string())?;
        validate_regular_mode(&scripts, "package_public_keys.json", 0o400)?;
        if read_regular(&scripts, "package_public_keys.json")? != self.manifest.key_bundle_bytes() {
            return Err("view_generation_key_bundle_invalid".into());
        }
        let runtime = open_child_directory(generation, "runtime")
            .map_err(|_| "view_generation_structure_invalid".to_string())?;
        if !directory_has_mode(&runtime, 0o555)? {
            return Err("view_generation_structure_invalid".into());
        }
        let generation_path = self.build_root.join(name);
        let downloads = relative_path(&generation_path, &self.state_root.join("downloads"))?;
        let auth = relative_path(&generation_path, &self.state_root.join(".auth"))?;
        let frontend_target = validate_relative_link(generation, "frontend", None)?;
        let frontend_path = std::fs::canonicalize(generation_path.join(&frontend_target))
            .map_err(|_| "view_generation_structure_invalid".to_string())?;
        let core_root = frontend_path
            .parent()
            .ok_or_else(|| "view_generation_structure_invalid".to_string())?;
        if core_root.file_name() != Some(OsStr::new(&marker.core_version)) {
            return Err("view_generation_source_invalid".into());
        }
        let core_directory =
            open_directory(core_root).map_err(|_| "view_generation_source_invalid".to_string())?;
        let core_scripts = open_relative_directory(&core_directory, "scripts")
            .map_err(|_| "view_generation_source_invalid".to_string())?;
        let core_frontend = open_relative_directory(&core_directory, "frontend-compat")
            .map_err(|_| "view_generation_source_invalid".to_string())?;
        let core_python = open_relative_directory(&core_directory, "runtime/python-arm64")
            .map_err(|_| "view_generation_source_invalid".to_string())?;
        let core_identity =
            fstat(&core_directory).map_err(|error| io::Error::from(error).to_string())?;
        let mut source_identity_matches = core_identity.st_dev as u64 == marker.core_dev
            && core_identity.st_ino as u64 == marker.core_ino;
        let expected_frontend =
            relative_path(&generation_path, &core_root.join("frontend-compat"))?;
        let expected_python = relative_path(
            &generation_path.join("runtime"),
            &core_root.join("runtime/python-arm64"),
        )?;
        if frontend_target != expected_frontend {
            return Err("view_generation_structure_invalid".into());
        }
        validate_linked_directory_identity(&generation_path, &frontend_target, &core_frontend)?;
        let downloads_target = validate_relative_link(generation, "downloads", Some(&downloads))?;
        let auth_target = validate_relative_link(generation, ".auth", Some(&auth))?;
        validate_linked_directory_identity(&generation_path, &downloads_target, &self.downloads)?;
        validate_linked_directory_identity(&generation_path, &auth_target, &self.auth)?;
        let python_target =
            validate_relative_link(&runtime, "python-arm64", Some(&expected_python))?;
        validate_linked_directory_identity(
            &generation_path.join("runtime"),
            &python_target,
            &core_python,
        )?;
        let collector_scripts = if marker.collector_version.is_some() {
            let node_modules_target = validate_relative_link(generation, "node_modules", None)?;
            let node_modules_path =
                std::fs::canonicalize(generation_path.join(&node_modules_target))
                    .map_err(|_| "view_generation_structure_invalid".to_string())?;
            let collector_root = node_modules_path
                .parent()
                .ok_or_else(|| "view_generation_structure_invalid".to_string())?;
            if collector_root.file_name() != marker.collector_version.as_deref().map(OsStr::new) {
                return Err("view_generation_source_invalid".into());
            }
            let collector_directory = open_directory(collector_root)
                .map_err(|_| "view_generation_source_invalid".to_string())?;
            let collector_scripts = open_relative_directory(&collector_directory, "scripts")
                .map_err(|_| "view_generation_source_invalid".to_string())?;
            let collector_node =
                open_relative_directory(&collector_directory, "runtime/node-arm64")
                    .map_err(|_| "view_generation_source_invalid".to_string())?;
            let collector_browser =
                open_relative_directory(&collector_directory, "runtime/playwright-browsers")
                    .map_err(|_| "view_generation_source_invalid".to_string())?;
            let collector_node_modules =
                open_relative_directory(&collector_directory, "node_modules")
                    .map_err(|_| "view_generation_source_invalid".to_string())?;
            let collector_identity =
                fstat(&collector_directory).map_err(|error| io::Error::from(error).to_string())?;
            source_identity_matches &= Some(collector_identity.st_dev as u64)
                == marker.collector_dev
                && Some(collector_identity.st_ino as u64) == marker.collector_ino;
            let expected_node_modules =
                relative_path(&generation_path, &collector_root.join("node_modules"))?;
            let expected_node = relative_path(
                &generation_path.join("runtime"),
                &collector_root.join("runtime/node-arm64"),
            )?;
            let expected_browser = relative_path(
                &generation_path.join("runtime"),
                &collector_root.join("runtime/playwright-browsers"),
            )?;
            if node_modules_target != expected_node_modules {
                return Err("view_generation_structure_invalid".into());
            }
            validate_linked_directory_identity(
                &generation_path,
                &node_modules_target,
                &collector_node_modules,
            )?;
            let node_target = validate_relative_link(&runtime, "node-arm64", Some(&expected_node))?;
            let browser_target =
                validate_relative_link(&runtime, "playwright-browsers", Some(&expected_browser))?;
            validate_linked_directory_identity(
                &generation_path.join("runtime"),
                &node_target,
                &collector_node,
            )?;
            validate_linked_directory_identity(
                &generation_path.join("runtime"),
                &browser_target,
                &collector_browser,
            )?;
            Some(collector_scripts)
        } else {
            validate_empty_directory(&runtime, "node-arm64")?;
            validate_empty_directory(&runtime, "playwright-browsers")?;
            validate_empty_directory(generation, "node_modules")?;
            None
        };
        source_identity_matches &= validate_scripts_overlay(
            Some(&core_scripts),
            collector_scripts.as_ref(),
            &scripts,
            Path::new(""),
            self.manifest.key_bundle_bytes(),
        )?;
        Ok((marker, source_identity_matches))
    }

    fn current_info(&self) -> Result<Option<(String, GenerationMarker, bool)>, String> {
        use rustix::fs::{readlinkat, FileType};

        let Some(stat) = stat_optional(&self.build, CURRENT_NAME)? else {
            return Ok(None);
        };
        if FileType::from_raw_mode(stat.st_mode) != FileType::Symlink {
            return Err("view_current_unsafe".into());
        }
        let target = readlinkat(&self.build, CURRENT_NAME, Vec::new())
            .map_err(|error| io::Error::from(error).to_string())?;
        let Some(after) = stat_optional(&self.build, CURRENT_NAME)? else {
            return Err("view_current_unsafe".into());
        };
        if !same_identity(&stat, &after) {
            return Err("view_current_unsafe".into());
        }
        let target =
            str::from_utf8(target.as_bytes()).map_err(|_| "view_current_unsafe".to_string())?;
        if parse_uuid_name(target, "generation-").is_none() || target.contains('/') {
            return Err("view_current_unsafe".into());
        }
        let generation = open_child_directory(&self.build, target)
            .map_err(|_| "view_current_unsafe".to_string())?;
        let (marker, source_identity_matches) = self.generation_info(target, &generation)?;
        Ok(Some((target.into(), marker, source_identity_matches)))
    }

    fn verified_view_for_target(&self, target: &str) -> Result<VerifiedView, String> {
        use rustix::fs::{fstat, readlinkat, FileType};

        #[cfg(test)]
        if self
            .fail_verified_view
            .swap(false, std::sync::atomic::Ordering::AcqRel)
        {
            return Err("view_injected_handle_failure".into());
        }
        let Some(before) = stat_optional(&self.build, CURRENT_NAME)? else {
            return Err("view_current_missing".into());
        };
        if FileType::from_raw_mode(before.st_mode) != FileType::Symlink {
            return Err("view_current_unsafe".into());
        }
        let visible = readlinkat(&self.build, CURRENT_NAME, Vec::new())
            .map_err(|error| io::Error::from(error).to_string())?;
        let Some(after) = stat_optional(&self.build, CURRENT_NAME)? else {
            return Err("view_current_unsafe".into());
        };
        if !same_identity(&before, &after) || visible.as_bytes() != target.as_bytes() {
            return Err("view_current_changed".into());
        }
        let generation = open_child_directory(&self.build, target)?;
        let generation_stat =
            fstat(&generation).map_err(|error| io::Error::from(error).to_string())?;
        if FileType::from_raw_mode(generation_stat.st_mode) != FileType::Directory {
            return Err("view_current_unsafe".into());
        }
        let view = VerifiedView {
            current: self.build_root.join(CURRENT_NAME),
            target: target.into(),
            build: self.build.clone(),
            generation: Arc::new(generation),
        };
        view.verify_visible()?;
        Ok(view)
    }

    fn cleanup_stale(&self) -> Result<(), String> {
        use rustix::fs::{unlinkat, AtFlags, Dir, FileType};

        for item in
            Dir::read_from(&self.build).map_err(|error| io::Error::from(error).to_string())?
        {
            let item = item.map_err(|error| io::Error::from(error).to_string())?;
            let Ok(name) = str::from_utf8(item.file_name().to_bytes()) else {
                continue;
            };
            let temporary = parse_uuid_name(name, "generation.tmp-").is_some();
            let backup = parse_uuid_name(name, ".current.backup-").is_some();
            if temporary {
                remove_name(&self.build, name)?;
            } else if backup {
                let Some(stat) = stat_optional(&self.build, name)? else {
                    continue;
                };
                if FileType::from_raw_mode(stat.st_mode) != FileType::Symlink {
                    return Err("view_stale_unsafe".into());
                }
                unlinkat(&self.build, name, AtFlags::empty())
                    .map_err(|error| io::Error::from(error).to_string())?;
            }
        }
        if let Some(stat) = stat_optional(&self.build, CURRENT_NEXT_NAME)? {
            if FileType::from_raw_mode(stat.st_mode) != FileType::Symlink {
                return Err("view_stale_unsafe".into());
            }
            unlinkat(&self.build, CURRENT_NEXT_NAME, AtFlags::empty())
                .map_err(|error| io::Error::from(error).to_string())?;
        }
        rustix::fs::fsync(&self.build).map_err(|error| io::Error::from(error).to_string())
    }

    fn find_generation(&self, expected: &GenerationMarker) -> Result<Option<String>, String> {
        use rustix::fs::Dir;

        for item in
            Dir::read_from(&self.build).map_err(|error| io::Error::from(error).to_string())?
        {
            let item = item.map_err(|error| io::Error::from(error).to_string())?;
            let Ok(name) = str::from_utf8(item.file_name().to_bytes()) else {
                continue;
            };
            if parse_uuid_name(name, "generation-").is_none() {
                continue;
            }
            let Ok(generation) = open_child_directory(&self.build, name) else {
                continue;
            };
            let (marker, source_identity_matches) = match self.generation_info(name, &generation) {
                Ok(info) => info,
                Err(error) if error == "view_build_collision" => return Err(error),
                Err(_) => continue,
            };
            if source_identity_matches && &marker == expected {
                return Ok(Some(name.into()));
            }
        }
        Ok(None)
    }

    fn create_relative_link(
        &self,
        parent: &File,
        parent_path: &Path,
        name: &str,
        target: &Path,
    ) -> Result<(), String> {
        let relative = relative_path(parent_path, target)?;
        rustix::fs::symlinkat(&relative, parent, name)
            .map_err(|error| io::Error::from(error).to_string())
    }

    fn build_generation(
        &self,
        core: &SourceRoot,
        collector: Option<&SourceRoot>,
        marker: &GenerationMarker,
    ) -> Result<String, String> {
        use rustix::fs::{fchmod, fsync, mkdirat, renameat, Mode};

        let temporary = format!("generation.tmp-{}", Uuid::new_v4());
        let generation = format!("generation-{}", Uuid::new_v4());
        mkdirat(&self.build, temporary.as_str(), Mode::from_raw_mode(0o700))
            .map_err(|error| io::Error::from(error).to_string())?;
        let temporary_path = self.build_root.join(&temporary);
        let mut renamed = false;
        let result = (|| {
            let root = open_child_directory(&self.build, temporary.as_str())?;
            fchmod(&root, Mode::from_raw_mode(0o700))
                .map_err(|error| io::Error::from(error).to_string())?;
            let scripts = ensure_child_directory(&root, "scripts", 0o755)?;
            let runtime = ensure_child_directory(&root, "runtime", 0o755)?;
            overlay_scripts(&core.scripts, &scripts, Path::new(""), true)?;
            if let Some(collector) = collector {
                overlay_scripts(&collector.scripts, &scripts, Path::new(""), false)?;
            }
            write_regular(
                &scripts,
                "package_public_keys.json",
                self.manifest.key_bundle_bytes(),
                0o400,
            )
            .map_err(|error| format!("view_key_write:{error}"))?;
            self.create_relative_link(
                &root,
                &temporary_path,
                "frontend",
                &core.canonical.join("frontend-compat"),
            )?;
            self.create_relative_link(
                &root,
                &temporary_path,
                "downloads",
                &self.state_root.join("downloads"),
            )?;
            self.create_relative_link(
                &root,
                &temporary_path,
                ".auth",
                &self.state_root.join(".auth"),
            )?;
            self.create_relative_link(
                &runtime,
                &temporary_path.join("runtime"),
                "python-arm64",
                &core.canonical.join("runtime/python-arm64"),
            )?;
            if let Some(collector) = collector {
                self.create_relative_link(
                    &runtime,
                    &temporary_path.join("runtime"),
                    "node-arm64",
                    &collector.canonical.join("runtime/node-arm64"),
                )?;
                self.create_relative_link(
                    &runtime,
                    &temporary_path.join("runtime"),
                    "playwright-browsers",
                    &collector.canonical.join("runtime/playwright-browsers"),
                )?;
                self.create_relative_link(
                    &root,
                    &temporary_path,
                    "node_modules",
                    &collector.canonical.join("node_modules"),
                )?;
            } else {
                let node = ensure_child_directory(&runtime, "node-arm64", 0o755)?;
                let browser = ensure_child_directory(&runtime, "playwright-browsers", 0o755)?;
                let node_modules = ensure_child_directory(&root, "node_modules", 0o755)?;
                for directory in [&node, &browser, &node_modules] {
                    fchmod(directory, Mode::from_raw_mode(0o555))
                        .map_err(|error| io::Error::from(error).to_string())?;
                    fsync(directory).map_err(|error| io::Error::from(error).to_string())?;
                }
            }
            write_regular(&root, MANIFEST_NAME, self.manifest.signed_bytes(), 0o400)
                .map_err(|error| format!("view_manifest_write:{error}"))?;
            write_regular(
                &root,
                GENERATION_MARKER_NAME,
                &serde_json::to_vec(marker).map_err(|error| error.to_string())?,
                0o400,
            )
            .map_err(|error| format!("view_marker_write:{error}"))?;
            seal_directory_tree(&scripts).map_err(|error| format!("view_scripts_seal:{error}"))?;
            if !validate_scripts_overlay(
                Some(&core.scripts),
                collector.map(|collector| &collector.scripts),
                &scripts,
                Path::new(""),
                self.manifest.key_bundle_bytes(),
            )? {
                return Err("view_scripts_validation_failed".into());
            }
            fchmod(&runtime, Mode::from_raw_mode(0o555))
                .map_err(|error| format!("view_runtime_seal:{}", io::Error::from(error)))?;
            fsync(&runtime)
                .map_err(|error| format!("view_runtime_sync:{}", io::Error::from(error)))?;
            fsync(&root).map_err(|error| format!("view_root_sync:{}", io::Error::from(error)))?;
            verify_source_visible(core).map_err(|error| format!("view_core_visible:{error}"))?;
            if let Some(collector) = collector {
                verify_source_visible(collector)
                    .map_err(|error| format!("view_collector_visible:{error}"))?;
            }
            self.verify_manager_visible()
                .map_err(|error| format!("view_manager_visible:{error}"))?;
            if stat_optional(&self.build, generation.as_str())?.is_some() {
                return Err("view_generation_collision".into());
            }
            // macOS rejects renaming a 0500 directory, so all contents and
            // child directories are sealed first and the root is sealed
            // immediately after its private UUID name is published.
            renameat(
                &self.build,
                temporary.as_str(),
                &self.build,
                generation.as_str(),
            )
            .map_err(|error| format!("view_generation_rename:{}", io::Error::from(error)))?;
            renamed = true;
            fchmod(&root, Mode::from_raw_mode(0o500))
                .map_err(|error| format!("view_root_seal:{}", io::Error::from(error)))?;
            fsync(&root).map_err(|error| format!("view_root_sync:{}", io::Error::from(error)))?;
            self.sync_generation_parent()?;
            Ok::<(), String>(())
        })();
        if let Err(error) = result {
            if renamed {
                return Err(error);
            }
            let cleanup = remove_name(&self.build, &temporary);
            return match cleanup {
                Ok(()) => Err(error),
                Err(cleanup) => Err(format!("{error}; view_cleanup_failed:{cleanup}")),
            };
        }
        Ok(generation)
    }

    fn sync_after_switch(&self) -> Result<(), String> {
        #[cfg(test)]
        if self
            .fail_switch_sync
            .swap(false, std::sync::atomic::Ordering::AcqRel)
        {
            return Err("view_injected_switch_sync_failure".into());
        }
        rustix::fs::fsync(&self.build).map_err(|error| io::Error::from(error).to_string())
    }

    fn sync_generation_parent(&self) -> Result<(), String> {
        #[cfg(test)]
        if self
            .fail_generation_parent_sync
            .swap(false, std::sync::atomic::Ordering::AcqRel)
        {
            return Err("view_injected_generation_parent_sync_failure".into());
        }
        rustix::fs::fsync(&self.build)
            .map_err(|error| format!("view_generation_parent_sync:{}", io::Error::from(error)))
    }

    fn sync_before_switch(&self) -> Result<(), String> {
        #[cfg(test)]
        if self
            .fail_pre_switch_sync
            .swap(false, std::sync::atomic::Ordering::AcqRel)
        {
            return Err("view_injected_pre_switch_sync_failure".into());
        }
        rustix::fs::fsync(&self.build).map_err(|error| io::Error::from(error).to_string())
    }

    fn rename_current(&self) -> Result<(), String> {
        #[cfg(test)]
        if self
            .fail_switch_rename
            .swap(false, std::sync::atomic::Ordering::AcqRel)
        {
            return Err("view_injected_switch_rename_failure".into());
        }
        rustix::fs::renameat(&self.build, CURRENT_NEXT_NAME, &self.build, CURRENT_NAME)
            .map_err(|error| io::Error::from(error).to_string())
    }

    fn cleanup_switch_links(&self, remove_next: bool, backup: Option<&str>) -> Result<(), String> {
        use rustix::fs::{fsync, unlinkat, AtFlags};

        let mut errors = Vec::new();
        if remove_next {
            if let Err(error) = unlinkat(&self.build, CURRENT_NEXT_NAME, AtFlags::empty()) {
                if error != rustix::io::Errno::NOENT {
                    errors.push(io::Error::from(error).to_string());
                }
            }
        }
        if let Some(backup) = backup {
            if let Err(error) = unlinkat(&self.build, backup, AtFlags::empty()) {
                if error != rustix::io::Errno::NOENT {
                    errors.push(io::Error::from(error).to_string());
                }
            }
        }
        if let Err(error) = fsync(&self.build) {
            errors.push(io::Error::from(error).to_string());
        }
        if errors.is_empty() {
            Ok(())
        } else {
            Err(errors.join("; "))
        }
    }

    fn switch_failure(&self, primary: String, cleanup: Result<(), String>) -> String {
        match cleanup {
            Ok(()) => primary,
            Err(cleanup) => format!("{primary}; view_switch_cleanup_failed:{cleanup}"),
        }
    }

    fn switch_current<F, T>(
        &self,
        generation: &str,
        old: Option<&str>,
        validate_after_switch: F,
    ) -> Result<T, String>
    where
        F: FnOnce() -> Result<T, String>,
    {
        use rustix::fs::{fsync, renameat, symlinkat, unlinkat, AtFlags};

        let backup = old.map(|_| format!(".current.backup-{}", Uuid::new_v4()));
        if let (Some(old), Some(backup)) = (old, backup.as_deref()) {
            symlinkat(old, &self.build, backup)
                .map_err(|error| io::Error::from(error).to_string())?;
        }
        if let Err(error) = symlinkat(generation, &self.build, CURRENT_NEXT_NAME) {
            let primary = io::Error::from(error).to_string();
            return Err(
                self.switch_failure(primary, self.cleanup_switch_links(false, backup.as_deref()))
            );
        }
        if let Err(primary) = self.sync_before_switch() {
            return Err(
                self.switch_failure(primary, self.cleanup_switch_links(true, backup.as_deref()))
            );
        }
        if let Err(primary) = self.rename_current() {
            return Err(
                self.switch_failure(primary, self.cleanup_switch_links(true, backup.as_deref()))
            );
        }
        let verified = match self
            .sync_after_switch()
            .and_then(|()| validate_after_switch())
        {
            Ok(verified) => verified,
            Err(primary) => {
                let restore = if let Some(backup) = backup.as_deref() {
                    renameat(&self.build, backup, &self.build, CURRENT_NAME)
                        .map_err(|error| io::Error::from(error).to_string())
                } else {
                    unlinkat(&self.build, CURRENT_NAME, AtFlags::empty())
                        .map_err(|error| io::Error::from(error).to_string())
                };
                let sync = fsync(&self.build).map_err(|error| io::Error::from(error).to_string());
                if let Err(restore) = restore {
                    return Err(format!("{primary}; view_restore_failed:{restore}"));
                }
                if let Err(sync) = sync {
                    return Err(format!("{primary}; view_restore_sync_failed:{sync}"));
                }
                return Err(primary);
            }
        };
        if let Some(backup) = backup.as_deref() {
            let _ = unlinkat(&self.build, backup, AtFlags::empty());
            let _ = fsync(&self.build);
        }
        Ok(verified)
    }

    fn activate_with_hooks<F: FnOnce(), G: FnOnce()>(
        &self,
        core: &RuntimeResolution,
        collector: Option<&RuntimeResolution>,
        after_source_open: F,
        after_current_switch: G,
    ) -> Result<VerifiedView, String> {
        let _gate = self
            .gate
            .lock()
            .map_err(|_| "view_gate_poisoned".to_string())?;
        let _process = self.acquire_file_lock()?;
        self.verify_manager_visible()?;
        let core_source = normalize_source(core, RuntimeKind::Core)?;
        let collector_source = collector
            .map(|collector| normalize_source(collector, RuntimeKind::Collector))
            .transpose()?;
        after_source_open();
        verify_source_visible(&core_source)?;
        if let Some(collector) = &collector_source {
            verify_source_visible(collector)?;
        }
        self.verify_manager_visible()?;
        let current = self.current_info()?;
        self.cleanup_stale()?;
        let desired = collector_source
            .as_ref()
            .map(|collector| GenerationMarker::full(&core_source, collector))
            .unwrap_or_else(|| GenerationMarker::core(&core_source));
        if let Some((target, marker, source_identity_matches)) = &current {
            let exact = *source_identity_matches && marker == &desired;
            let no_downgrade = *source_identity_matches
                && collector.is_none()
                && marker.core_version == desired.core_version
                && marker.core_dev == desired.core_dev
                && marker.core_ino == desired.core_ino
                && marker.collector_version.is_some();
            let signed = &self.manifest.manifest().runtimes;
            let core_blocked = !component_replacement_allowed(
                &marker.core_version,
                &desired.core_version,
                &signed.core.version,
            );
            let collector_blocked = match (
                marker.collector_version.as_deref(),
                desired.collector_version.as_deref(),
            ) {
                (Some(current), Some(requested)) => {
                    !component_replacement_allowed(current, requested, &signed.collector.version)
                }
                _ => false,
            };
            let monotonic_preserve =
                *source_identity_matches && (core_blocked || collector_blocked);
            if exact || no_downgrade || monotonic_preserve {
                self.verify_manager_visible()?;
                return self.verified_view_for_target(target);
            }
            if target.is_empty() {
                return Err("view_current_unsafe".into());
            }
        }
        let generation = match self.find_generation(&desired)? {
            Some(generation) => generation,
            None => self.build_generation(&core_source, collector_source.as_ref(), &desired)?,
        };
        verify_source_visible(&core_source)?;
        if let Some(collector) = &collector_source {
            verify_source_visible(collector)?;
        }
        self.verify_manager_visible()?;
        let old = current.as_ref().map(|(target, _, _)| target.as_str());
        self.switch_current(&generation, old, || {
            after_current_switch();
            verify_source_visible(&core_source)?;
            if let Some(collector) = &collector_source {
                verify_source_visible(collector)?;
            }
            self.verify_manager_visible()?;
            self.verified_view_for_target(&generation)
        })
    }

    pub fn activate_core(&self, core: &RuntimeResolution) -> Result<VerifiedView, String> {
        self.activate_with_hooks(core, None, || {}, || {})
    }

    pub fn activate_collector(
        &self,
        core: &RuntimeResolution,
        collector: &RuntimeResolution,
    ) -> Result<VerifiedView, String> {
        self.activate_with_hooks(core, Some(collector), || {}, || {})
    }

    #[cfg(test)]
    fn activate_core_with_hook<F: FnOnce()>(
        &self,
        core: &RuntimeResolution,
        hook: F,
    ) -> Result<VerifiedView, String> {
        self.activate_with_hooks(core, None, hook, || {})
    }

    #[cfg(test)]
    fn activate_collector_with_hook<F: FnOnce()>(
        &self,
        core: &RuntimeResolution,
        collector: &RuntimeResolution,
        hook: F,
    ) -> Result<VerifiedView, String> {
        self.activate_with_hooks(core, Some(collector), hook, || {})
    }

    #[cfg(test)]
    fn activate_collector_with_post_switch_hook<F: FnOnce()>(
        &self,
        core: &RuntimeResolution,
        collector: &RuntimeResolution,
        hook: F,
    ) -> Result<VerifiedView, String> {
        self.activate_with_hooks(core, Some(collector), || {}, hook)
    }

    #[cfg(test)]
    fn fail_next_switch_sync_for_test(&self) {
        self.fail_switch_sync
            .store(true, std::sync::atomic::Ordering::Release);
    }

    #[cfg(test)]
    fn fail_next_pre_switch_sync_for_test(&self) {
        self.fail_pre_switch_sync
            .store(true, std::sync::atomic::Ordering::Release);
    }

    #[cfg(test)]
    fn fail_next_switch_rename_for_test(&self) {
        self.fail_switch_rename
            .store(true, std::sync::atomic::Ordering::Release);
    }

    #[cfg(test)]
    fn fail_next_generation_parent_sync_for_test(&self) {
        self.fail_generation_parent_sync
            .store(true, std::sync::atomic::Ordering::Release);
    }

    #[cfg(test)]
    fn fail_next_verified_view_for_test(&self) {
        self.fail_verified_view
            .store(true, std::sync::atomic::Ordering::Release);
    }
}

#[cfg(test)]
mod tests {
    use std::{
        fs,
        os::unix::fs::{symlink, PermissionsExt},
        path::{Path, PathBuf},
        process::Command,
        sync::{
            atomic::{AtomicBool, Ordering},
            Arc, Barrier, Mutex,
        },
        thread,
    };

    use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
    use ed25519_dalek::{
        pkcs8::{spki::der::pem::LineEnding, EncodePublicKey},
        Signer, SigningKey,
    };
    use serde_json::{json, Value};

    use crate::{
        manifest::VerifiedPackageManifest,
        runtime::{RuntimeKind, RuntimeResolution},
    };

    use super::{open_directory, remove_name_with_hook, ViewManager};

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

    fn verified_manifest(build_version: &str, package_id: &str) -> Arc<VerifiedPackageManifest> {
        let descriptor = |kind: &str| {
            json!({
                "archive": format!("{kind}-runtime.tar.zst"),
                "required_files": [format!("config/{kind}.json")],
                "sha256": "a".repeat(64),
                "size_bytes": 1,
                "tree_sha256": "b".repeat(64),
                "version": format!("{kind}-v2")
            })
        };
        let payload = json!({
            "arch": "arm64",
            "build_version": build_version,
            "key_id": "test-key",
            "package_id": package_id,
            "runtimes": {"core": descriptor("core"), "collector": descriptor("collector")}
        });
        let signing = SigningKey::from_bytes(&[23_u8; 32]);
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

    struct Sources {
        core: PathBuf,
        collector: PathBuf,
    }

    fn sources(root: &Path) -> Sources {
        let core = root.join("core-v1");
        let collector = root.join("collector-v1");
        for path in [
            core.join("scripts/domain"),
            core.join("frontend-compat"),
            core.join("runtime/python-arm64"),
            collector.join("scripts"),
            collector.join("runtime/node-arm64"),
            collector.join("runtime/playwright-browsers"),
            collector.join("node_modules"),
        ] {
            fs::create_dir_all(path).unwrap();
        }
        fs::write(core.join("scripts/_run.py"), b"core").unwrap();
        fs::write(
            core.join("scripts/package_public_keys.json"),
            b"{\"keys\":[\"stale-fallback-key\"]}",
        )
        .unwrap();
        fs::write(core.join("scripts/sync_feishu_bitable_openapi.py"), b"sync").unwrap();
        fs::write(core.join("scripts/domain/helper.py"), b"helper").unwrap();
        symlink("helper.py", core.join("scripts/domain/helper-link.py")).unwrap();
        fs::write(core.join("frontend-compat/index.html"), b"frontend").unwrap();
        fs::write(core.join("runtime/python-arm64/python"), b"python-runtime").unwrap();
        fs::write(collector.join("scripts/douyin_export.mjs"), b"collector").unwrap();
        fs::write(collector.join("runtime/node-arm64/node"), b"node").unwrap();
        fs::write(
            collector.join("runtime/playwright-browsers/chromium"),
            b"chromium",
        )
        .unwrap();
        fs::write(collector.join("node_modules/package.json"), b"{}").unwrap();
        Sources { core, collector }
    }

    fn assert_empty_real_directory(path: &Path) {
        let metadata = fs::symlink_metadata(path).unwrap();
        assert!(metadata.is_dir());
        assert!(!metadata.file_type().is_symlink());
        assert!(fs::read_dir(path).unwrap().next().is_none());
    }

    fn resolution(kind: RuntimeKind, version: &str, root: &Path) -> RuntimeResolution {
        RuntimeResolution::fixture(kind, version, root, false).unwrap()
    }

    #[test]
    fn core_then_full_switch_is_complete_and_keeps_scripts_in_generation() {
        let temp = tempfile::tempdir().unwrap();
        let sources = sources(temp.path());
        let manifest = verified_manifest("20260711", "data-scientist-community-mac-arm64");
        let manager = ViewManager::new(temp.path().to_path_buf(), manifest.clone()).unwrap();
        fs::write(temp.path().join("downloads/existing.json"), b"state").unwrap();

        let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
        let collector = resolution(RuntimeKind::Collector, "collector-v1", &sources.collector);
        let current = manager.activate_core(&core).unwrap();
        assert!(current.join("scripts/_run.py").is_file());
        assert!(!current.join("scripts/douyin_export.mjs").exists());
        assert_empty_real_directory(&current.join("runtime/node-arm64"));
        assert_empty_real_directory(&current.join("runtime/playwright-browsers"));
        assert_empty_real_directory(&current.join("node_modules"));

        let full = manager.activate_collector(&core, &collector).unwrap();
        assert_eq!(full, current);
        assert!(full.join("scripts/_run.py").is_file());
        assert!(full.join("scripts/douyin_export.mjs").is_file());
        assert!(full.join("runtime/node-arm64/node").is_file());
        assert!(full.join("runtime/playwright-browsers/chromium").is_file());
        assert!(full.join("node_modules/package.json").is_file());
        assert_eq!(
            fs::read(full.join("downloads/existing.json")).unwrap(),
            b"state"
        );
        fs::write(full.join("downloads/new.json"), b"new-state").unwrap();
        assert_eq!(
            fs::read(temp.path().join("downloads/new.json")).unwrap(),
            b"new-state"
        );
        assert_eq!(
            fs::read(full.join("package_manifest.json")).unwrap(),
            manifest.signed_bytes()
        );
        assert_eq!(
            fs::read(full.join("scripts/package_public_keys.json")).unwrap(),
            manifest.key_bundle_bytes()
        );
        let marker: Value =
            serde_json::from_slice(&fs::read(full.join(".view-generation.json")).unwrap()).unwrap();
        assert_eq!(marker["core_version"], "core-v1");
        assert_eq!(marker["collector_version"], "collector-v1");
        assert!(marker["core_dev"].as_u64().is_some());
        assert!(marker["core_ino"].as_u64().is_some());
        assert!(marker["collector_dev"].as_u64().is_some());
        assert!(marker["collector_ino"].as_u64().is_some());
        assert!(fs::symlink_metadata(full.join("package_manifest.json"))
            .unwrap()
            .is_file());

        let script = fs::canonicalize(full.join("scripts/sync_feishu_bitable_openapi.py")).unwrap();
        let generation = fs::canonicalize(&full).unwrap();
        assert_eq!(script.parent().unwrap().parent().unwrap(), generation);
        assert!(generation.join("node_modules").exists());
        assert_eq!(
            fs::read_link(generation.join("scripts/domain/helper-link.py")).unwrap(),
            PathBuf::from("helper.py")
        );
        for link in [
            "frontend",
            "runtime/python-arm64",
            "runtime/node-arm64",
            "runtime/playwright-browsers",
            "node_modules",
            "downloads",
            ".auth",
        ] {
            assert!(!fs::read_link(generation.join(link)).unwrap().is_absolute());
        }
        assert!(!fs::symlink_metadata(generation.join("scripts/_run.py"))
            .unwrap()
            .file_type()
            .is_symlink());
    }

    #[test]
    fn late_core_activation_does_not_downgrade_same_core_full_view() {
        let temp = tempfile::tempdir().unwrap();
        let sources = sources(temp.path());
        let manager = ViewManager::new(
            temp.path().to_path_buf(),
            verified_manifest("20260711", "pkg"),
        )
        .unwrap();
        let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
        let collector = resolution(RuntimeKind::Collector, "collector-v1", &sources.collector);
        manager.activate_collector(&core, &collector).unwrap();
        let current = temp.path().join("runtimes/views/20260711/current");
        let full_target = fs::read_link(&current).unwrap();

        manager.activate_core(&core).unwrap();

        assert_eq!(fs::read_link(&current).unwrap(), full_target);
        assert!(current.join("node_modules/package.json").is_file());
    }

    #[test]
    fn same_version_repaired_source_inode_builds_a_new_generation() {
        let temp = tempfile::tempdir().unwrap();
        let first_sources = sources(temp.path());
        let manager = ViewManager::new(
            temp.path().to_path_buf(),
            verified_manifest("20260711", "pkg"),
        )
        .unwrap();
        let first_core = resolution(RuntimeKind::Core, "core-v1", &first_sources.core);
        manager.activate_core(&first_core).unwrap();
        let current = temp.path().join("runtimes/views/20260711/current");
        let before = fs::read_link(&current).unwrap();

        fs::remove_dir_all(&first_sources.core).unwrap();
        let repaired_sources = sources(temp.path());
        fs::write(repaired_sources.core.join("scripts/_run.py"), b"repaired").unwrap();
        let repaired_core = resolution(RuntimeKind::Core, "core-v1", &repaired_sources.core);
        manager.activate_core(&repaired_core).unwrap();

        assert_ne!(fs::read_link(&current).unwrap(), before);
        assert_eq!(
            fs::read(current.join("scripts/_run.py")).unwrap(),
            b"repaired"
        );
    }

    #[test]
    fn file_directory_and_symlink_script_collisions_fail_closed() {
        for collision_kind in ["file", "directory", "symlink"] {
            let temp = tempfile::tempdir().unwrap();
            let sources = sources(temp.path());
            let manager = ViewManager::new(
                temp.path().to_path_buf(),
                verified_manifest("20260711", "pkg"),
            )
            .unwrap();
            let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
            let collector = resolution(RuntimeKind::Collector, "collector-v1", &sources.collector);
            manager.activate_core(&core).unwrap();
            let current = temp.path().join("runtimes/views/20260711/current");
            let before = fs::read_link(&current).unwrap();
            let collision = sources.core.join("scripts/douyin_export.mjs");
            match collision_kind {
                "file" => fs::write(&collision, b"collision").unwrap(),
                "directory" => fs::create_dir(&collision).unwrap(),
                "symlink" => symlink("_run.py", &collision).unwrap(),
                _ => unreachable!(),
            }

            assert_eq!(
                manager.activate_collector(&core, &collector).unwrap_err(),
                "view_script_collision",
                "collision type: {collision_kind}"
            );
            assert_eq!(fs::read_link(&current).unwrap(), before);
            assert!(!fs::read_dir(temp.path().join("runtimes/views/20260711"))
                .unwrap()
                .any(|item| item
                    .unwrap()
                    .file_name()
                    .to_string_lossy()
                    .starts_with("generation.tmp-")));
        }
    }

    #[test]
    fn collector_key_bundle_entry_is_a_collision_not_a_fallback_override() {
        let temp = tempfile::tempdir().unwrap();
        let sources = sources(temp.path());
        let manager = ViewManager::new(
            temp.path().to_path_buf(),
            verified_manifest("20260711", "pkg"),
        )
        .unwrap();
        let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
        let collector = resolution(RuntimeKind::Collector, "collector-v1", &sources.collector);
        manager.activate_core(&core).unwrap();
        let current = temp.path().join("runtimes/views/20260711/current");
        let before = fs::read_link(&current).unwrap();
        fs::write(
            sources.collector.join("scripts/package_public_keys.json"),
            b"collector-key",
        )
        .unwrap();

        assert_eq!(
            manager.activate_collector(&core, &collector).unwrap_err(),
            "view_script_collision"
        );
        assert_eq!(fs::read_link(current).unwrap(), before);
    }

    #[test]
    fn missing_collector_source_leaves_current_and_no_temporary_generation() {
        let temp = tempfile::tempdir().unwrap();
        let sources = sources(temp.path());
        let manager = ViewManager::new(
            temp.path().to_path_buf(),
            verified_manifest("20260711", "pkg"),
        )
        .unwrap();
        let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
        let collector = resolution(RuntimeKind::Collector, "collector-v1", &sources.collector);
        manager.activate_core(&core).unwrap();
        let current = temp.path().join("runtimes/views/20260711/current");
        let before = fs::read_link(&current).unwrap();
        fs::remove_dir_all(sources.collector.join("runtime/node-arm64")).unwrap();

        assert!(manager.activate_collector(&core, &collector).is_err());
        assert_eq!(fs::read_link(&current).unwrap(), before);
        assert!(!fs::read_dir(temp.path().join("runtimes/views/20260711"))
            .unwrap()
            .any(|item| item
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with("generation.tmp-")));
    }

    #[test]
    fn unsafe_current_types_and_targets_are_rejected_without_replacement() {
        for case in ["file", "directory", "absolute", "parent", "non-generation"] {
            let temp = tempfile::tempdir().unwrap();
            let sources = sources(temp.path());
            let manager = ViewManager::new(
                temp.path().to_path_buf(),
                verified_manifest("20260711", "pkg"),
            )
            .unwrap();
            let current = temp.path().join("runtimes/views/20260711/current");
            let outside = temp.path().join("outside");
            fs::create_dir(&outside).unwrap();
            match case {
                "file" => fs::write(&current, b"malicious").unwrap(),
                "directory" => fs::create_dir(&current).unwrap(),
                "absolute" => symlink(&outside, &current).unwrap(),
                "parent" => symlink("../outside", &current).unwrap(),
                "non-generation" => symlink("not-a-generation", &current).unwrap(),
                _ => unreachable!(),
            }
            let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);

            assert_eq!(
                manager.activate_core(&core).unwrap_err(),
                "view_current_unsafe",
                "unsafe case: {case}"
            );
            match case {
                "file" => assert_eq!(fs::read(&current).unwrap(), b"malicious"),
                "directory" => assert!(fs::symlink_metadata(&current).unwrap().is_dir()),
                "absolute" => assert_eq!(fs::read_link(&current).unwrap(), outside),
                "parent" => {
                    assert_eq!(
                        fs::read_link(&current).unwrap(),
                        PathBuf::from("../outside")
                    )
                }
                "non-generation" => assert_eq!(
                    fs::read_link(&current).unwrap(),
                    PathBuf::from("not-a-generation")
                ),
                _ => unreachable!(),
            }
        }
    }

    #[test]
    fn same_build_with_different_signed_manifest_is_a_collision() {
        let temp = tempfile::tempdir().unwrap();
        let sources = sources(temp.path());
        let root = temp.path().to_path_buf();
        let first = ViewManager::new(root.clone(), verified_manifest("20260711", "first")).unwrap();
        let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
        first.activate_core(&core).unwrap();
        let current = root.join("runtimes/views/20260711/current");
        let before = fs::read_link(&current).unwrap();
        let second = ViewManager::new(root, verified_manifest("20260711", "second")).unwrap();

        assert_eq!(
            second.activate_core(&core).unwrap_err(),
            "view_build_collision"
        );
        assert_eq!(fs::read_link(current).unwrap(), before);
    }

    #[test]
    fn finalized_generations_are_never_garbage_collected() {
        let temp = tempfile::tempdir().unwrap();
        let sources = sources(temp.path());
        let manager = ViewManager::new(
            temp.path().to_path_buf(),
            verified_manifest("20260711", "pkg"),
        )
        .unwrap();
        let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
        let collector = resolution(RuntimeKind::Collector, "collector-v1", &sources.collector);
        manager.activate_core(&core).unwrap();
        let current = temp.path().join("runtimes/views/20260711/current");
        let old = fs::read_link(&current).unwrap();
        manager.activate_collector(&core, &collector).unwrap();

        assert!(temp
            .path()
            .join("runtimes/views/20260711")
            .join(&old)
            .is_dir());
        assert!(temp
            .path()
            .join("runtimes/views/20260711")
            .join(old)
            .join("scripts/_run.py")
            .is_file());
    }

    #[test]
    fn cleanup_removes_only_owned_stale_names_and_never_follows_outside_links() {
        let temp = tempfile::tempdir().unwrap();
        let sources = sources(temp.path());
        let manager = ViewManager::new(
            temp.path().to_path_buf(),
            verified_manifest("20260711", "pkg"),
        )
        .unwrap();
        let build = temp.path().join("runtimes/views/20260711");
        let outside = temp.path().join("outside");
        fs::create_dir(&outside).unwrap();
        fs::write(outside.join("sentinel"), b"untouched").unwrap();

        let stale_dir = build.join(format!("generation.tmp-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&stale_dir).unwrap();
        symlink(&outside, stale_dir.join("outside-link")).unwrap();
        let stale_link = build.join(format!("generation.tmp-{}", uuid::Uuid::new_v4()));
        symlink(&outside, &stale_link).unwrap();
        let backup = build.join(format!(".current.backup-{}", uuid::Uuid::new_v4()));
        symlink(&outside, &backup).unwrap();
        symlink(&outside, build.join("current.next")).unwrap();
        let finalized = build.join(format!("generation-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&finalized).unwrap();
        fs::write(finalized.join("keep"), b"keep").unwrap();
        let unknown_tmp = build.join("generation.tmp-not-a-uuid");
        fs::create_dir(&unknown_tmp).unwrap();
        let unknown_backup = build.join(".current.backup-not-a-uuid");
        fs::write(&unknown_backup, b"keep").unwrap();

        manager
            .activate_core(&resolution(RuntimeKind::Core, "core-v1", &sources.core))
            .unwrap();

        assert!(!stale_dir.exists());
        assert!(fs::symlink_metadata(&stale_link).is_err());
        assert!(fs::symlink_metadata(&backup).is_err());
        assert!(fs::symlink_metadata(build.join("current.next")).is_err());
        assert_eq!(fs::read(outside.join("sentinel")).unwrap(), b"untouched");
        assert_eq!(fs::read(finalized.join("keep")).unwrap(), b"keep");
        assert!(unknown_tmp.is_dir());
        assert_eq!(fs::read(unknown_backup).unwrap(), b"keep");
    }

    #[test]
    fn relative_view_links_survive_moving_the_whole_state_tree() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        fs::create_dir(&state).unwrap();
        let sources = sources(&state);
        let manager =
            ViewManager::new(state.clone(), verified_manifest("20260711", "pkg")).unwrap();
        let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
        let collector = resolution(RuntimeKind::Collector, "collector-v1", &sources.collector);
        manager.activate_collector(&core, &collector).unwrap();
        drop(manager);

        let moved = temp.path().join("moved-state");
        fs::rename(&state, &moved).unwrap();
        let current = moved.join("runtimes/views/20260711/current");

        assert!(current.join("frontend/index.html").is_file());
        assert!(current.join("runtime/python-arm64/python").is_file());
        assert!(current.join("runtime/node-arm64/node").is_file());
        assert!(current
            .join("runtime/playwright-browsers/chromium")
            .is_file());
        assert!(current.join("node_modules/package.json").is_file());
        fs::write(current.join("downloads/moved.json"), b"moved").unwrap();
        assert_eq!(
            fs::read(moved.join("downloads/moved.json")).unwrap(),
            b"moved"
        );
    }

    #[test]
    fn runtime_resolution_kind_and_version_basename_are_strictly_validated() {
        let temp = tempfile::tempdir().unwrap();
        let sources = sources(temp.path());
        let manager = ViewManager::new(
            temp.path().to_path_buf(),
            verified_manifest("20260711", "pkg"),
        )
        .unwrap();
        let wrong_kind = resolution(RuntimeKind::Collector, "core-v1", &sources.core);
        assert_eq!(
            manager.activate_core(&wrong_kind).unwrap_err(),
            "view_resolution_invalid"
        );
        let wrong_basename = resolution(RuntimeKind::Core, "other-version", &sources.core);
        assert_eq!(
            manager.activate_core(&wrong_basename).unwrap_err(),
            "view_resolution_invalid"
        );
        let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
        let wrong_collector = resolution(RuntimeKind::Core, "collector-v1", &sources.collector);
        assert_eq!(
            manager
                .activate_collector(&core, &wrong_collector)
                .unwrap_err(),
            "view_resolution_invalid"
        );
        assert!(!temp.path().join("runtimes/views/20260711/current").exists());
    }

    #[test]
    fn independent_managers_publish_atomically_and_converge_on_full_view() {
        let temp = tempfile::tempdir().unwrap();
        let sources = sources(temp.path());
        let manifest = verified_manifest("20260711", "pkg");
        let core_manager =
            Arc::new(ViewManager::new(temp.path().to_path_buf(), manifest.clone()).unwrap());
        let full_manager =
            Arc::new(ViewManager::new(temp.path().to_path_buf(), manifest.clone()).unwrap());
        let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
        let collector = resolution(RuntimeKind::Collector, "collector-v1", &sources.collector);
        core_manager.activate_core(&core).unwrap();
        let build = temp.path().join("runtimes/views/20260711");
        let current = build.join("current");
        let barrier = Arc::new(Barrier::new(3));
        let stop = Arc::new(AtomicBool::new(false));
        let errors = Arc::new(Mutex::new(Vec::new()));

        let reader_barrier = barrier.clone();
        let reader_stop = stop.clone();
        let reader_errors = errors.clone();
        let reader_build = fs::canonicalize(&build).unwrap();
        let reader_current = current.clone();
        let reader = thread::spawn(move || {
            reader_barrier.wait();
            let mut iterations = 0;
            while !reader_stop.load(Ordering::Acquire) || iterations < 1_000 {
                iterations += 1;
                let result = (|| -> Result<(), String> {
                    let generation = fs::canonicalize(&reader_current)
                        .map_err(|error| format!("resolve current: {error}"))?;
                    if generation.parent() != Some(reader_build.as_path())
                        || !generation
                            .file_name()
                            .is_some_and(|name| name.to_string_lossy().starts_with("generation-"))
                    {
                        return Err("unsafe current target".into());
                    }
                    if fs::read(generation.join("package_manifest.json"))
                        .map_err(|error| format!("read manifest: {error}"))?
                        != manifest.signed_bytes()
                    {
                        return Err("manifest mismatch".into());
                    }
                    let marker: Value = serde_json::from_slice(
                        &fs::read(generation.join(".view-generation.json"))
                            .map_err(|error| format!("read marker: {error}"))?,
                    )
                    .map_err(|error| error.to_string())?;
                    if marker["collector_version"].is_null() {
                        if generation.join("scripts/douyin_export.mjs").exists()
                            || !generation.join("node_modules").is_dir()
                        {
                            return Err("mixed core generation".into());
                        }
                    } else if !generation.join("scripts/douyin_export.mjs").is_file()
                        || !generation.join("runtime/node-arm64/node").is_file()
                        || !generation.join("node_modules/package.json").is_file()
                    {
                        return Err("incomplete full generation".into());
                    }
                    Ok(())
                })();
                if let Err(error) = result {
                    reader_errors.lock().unwrap().push(error);
                    break;
                }
                thread::yield_now();
            }
        });

        let core_barrier = barrier.clone();
        let core_worker_manager = core_manager.clone();
        let core_worker_resolution = core.clone();
        let core_worker = thread::spawn(move || {
            core_barrier.wait();
            for _ in 0..25 {
                core_worker_manager
                    .activate_core(&core_worker_resolution)
                    .unwrap();
                thread::yield_now();
            }
        });
        let full_barrier = barrier.clone();
        let full_worker_manager = full_manager.clone();
        let full_core = core.clone();
        let full_collector = collector.clone();
        let full_worker = thread::spawn(move || {
            full_barrier.wait();
            full_worker_manager
                .activate_collector(&full_core, &full_collector)
                .unwrap();
        });

        core_worker.join().unwrap();
        full_worker.join().unwrap();
        stop.store(true, Ordering::Release);
        reader.join().unwrap();

        let errors = errors.lock().unwrap().clone();
        assert!(errors.is_empty(), "{errors:?}");
        assert!(current.join("scripts/douyin_export.mjs").is_file());
        assert!(current.join("node_modules/package.json").is_file());
    }

    #[test]
    fn tampered_marker_key_bundle_mode_or_link_structure_is_never_reused() {
        for case in [
            "marker-schema",
            "marker-version",
            "marker-mode",
            "key-bundle",
            "frontend-type",
        ] {
            let temp = tempfile::tempdir().unwrap();
            let sources = sources(temp.path());
            let manager = ViewManager::new(
                temp.path().to_path_buf(),
                verified_manifest("20260711", "pkg"),
            )
            .unwrap();
            let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
            manager.activate_core(&core).unwrap();
            let build = temp.path().join("runtimes/views/20260711");
            let current = build.join("current");
            let target = fs::read_link(&current).unwrap();
            let generation = build.join(&target);
            let marker_path = generation.join(".view-generation.json");
            match case {
                "marker-schema" => {
                    fs::set_permissions(&marker_path, fs::Permissions::from_mode(0o600)).unwrap();
                    let mut marker: Value =
                        serde_json::from_slice(&fs::read(&marker_path).unwrap()).unwrap();
                    marker["schema_version"] = json!(2);
                    fs::write(&marker_path, serde_json::to_vec(&marker).unwrap()).unwrap();
                }
                "marker-version" => {
                    fs::set_permissions(&marker_path, fs::Permissions::from_mode(0o600)).unwrap();
                    let mut marker: Value =
                        serde_json::from_slice(&fs::read(&marker_path).unwrap()).unwrap();
                    marker["core_version"] = json!("../unsafe");
                    fs::write(&marker_path, serde_json::to_vec(&marker).unwrap()).unwrap();
                }
                "marker-mode" => {
                    fs::set_permissions(&marker_path, fs::Permissions::from_mode(0o644)).unwrap()
                }
                "key-bundle" => {
                    let key = generation.join("scripts/package_public_keys.json");
                    fs::set_permissions(&key, fs::Permissions::from_mode(0o600)).unwrap();
                    fs::write(key, b"tampered").unwrap();
                }
                "frontend-type" => {
                    fs::set_permissions(&generation, fs::Permissions::from_mode(0o700)).unwrap();
                    fs::remove_file(generation.join("frontend")).unwrap();
                    fs::create_dir(generation.join("frontend")).unwrap();
                }
                _ => unreachable!(),
            }

            assert!(manager.activate_core(&core).is_err(), "tamper case: {case}");
            assert_eq!(fs::read_link(current).unwrap(), target);
        }
    }

    #[test]
    fn replacing_visible_state_root_mid_activation_fails_before_any_outside_write() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        fs::create_dir(&state).unwrap();
        let source_root = temp.path().join("sources");
        fs::create_dir(&source_root).unwrap();
        let sources = sources(&source_root);
        let manager =
            ViewManager::new(state.clone(), verified_manifest("20260711", "pkg")).unwrap();
        let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
        let moved = temp.path().join("moved-state");
        let outside = temp.path().join("outside");
        fs::create_dir(&outside).unwrap();

        assert_eq!(
            manager
                .activate_core_with_hook(&core, || {
                    fs::rename(&state, &moved).unwrap();
                    symlink(&outside, &state).unwrap();
                })
                .unwrap_err(),
            "view_state_root_changed"
        );
        assert!(fs::read_dir(&outside).unwrap().next().is_none());
        assert!(!moved.join("runtimes/views/20260711/current").exists());
    }

    #[test]
    fn pre_rename_sync_and_rename_failures_leave_current_and_no_private_links() {
        for fail_point in ["pre-sync", "rename"] {
            let temp = tempfile::tempdir().unwrap();
            let sources = sources(temp.path());
            let manager = ViewManager::new(
                temp.path().to_path_buf(),
                verified_manifest("20260711", "pkg"),
            )
            .unwrap();
            let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
            let collector = resolution(RuntimeKind::Collector, "collector-v1", &sources.collector);
            manager.activate_core(&core).unwrap();
            let build = temp.path().join("runtimes/views/20260711");
            let current = build.join("current");
            let before = fs::read_link(&current).unwrap();
            match fail_point {
                "pre-sync" => manager.fail_next_pre_switch_sync_for_test(),
                "rename" => manager.fail_next_switch_rename_for_test(),
                _ => unreachable!(),
            }

            assert!(manager.activate_collector(&core, &collector).is_err());
            assert_eq!(fs::read_link(&current).unwrap(), before);
            assert!(fs::symlink_metadata(build.join("current.next")).is_err());
            assert!(!fs::read_dir(&build).unwrap().any(|item| item
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with(".current.backup-")));
        }
    }

    #[test]
    fn source_replacement_after_open_fails_closed_without_writing_outside() {
        let temp = tempfile::tempdir().unwrap();
        let sources = sources(temp.path());
        let manager = ViewManager::new(
            temp.path().to_path_buf(),
            verified_manifest("20260711", "pkg"),
        )
        .unwrap();
        let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
        let collector = resolution(RuntimeKind::Collector, "collector-v1", &sources.collector);
        manager.activate_core(&core).unwrap();
        let current = temp.path().join("runtimes/views/20260711/current");
        let before = fs::read_link(&current).unwrap();
        let moved = temp.path().join("collector-moved");
        let outside = temp.path().join("outside");
        fs::create_dir(&outside).unwrap();

        assert!(manager
            .activate_collector_with_hook(&core, &collector, || {
                fs::rename(&sources.collector, &moved).unwrap();
                symlink(&outside, &sources.collector).unwrap();
            })
            .is_err());
        assert!(fs::read_dir(outside).unwrap().next().is_none());
        assert_eq!(fs::read_link(current).unwrap(), before);
    }

    #[test]
    fn switch_sync_failure_restores_previous_current_target() {
        let temp = tempfile::tempdir().unwrap();
        let sources = sources(temp.path());
        let manager = ViewManager::new(
            temp.path().to_path_buf(),
            verified_manifest("20260711", "pkg"),
        )
        .unwrap();
        let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
        let collector = resolution(RuntimeKind::Collector, "collector-v1", &sources.collector);
        manager.activate_core(&core).unwrap();
        let current = temp.path().join("runtimes/views/20260711/current");
        let before = fs::read_link(&current).unwrap();
        manager.fail_next_switch_sync_for_test();

        assert!(manager.activate_collector(&core, &collector).is_err());
        assert_eq!(fs::read_link(current).unwrap(), before);
    }

    #[test]
    fn generation_parent_sync_failure_keeps_a_complete_reusable_generation() {
        let temp = tempfile::tempdir().unwrap();
        let sources = sources(temp.path());
        let manager = ViewManager::new(
            temp.path().to_path_buf(),
            verified_manifest("20260711", "pkg"),
        )
        .unwrap();
        let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
        manager.fail_next_generation_parent_sync_for_test();

        assert_eq!(
            manager.activate_core(&core).unwrap_err(),
            "view_injected_generation_parent_sync_failure"
        );
        let build = temp.path().join("runtimes/views/20260711");
        assert!(!build.join("current").exists());
        let finalized: Vec<_> = fs::read_dir(&build)
            .unwrap()
            .flatten()
            .map(|entry| entry.file_name())
            .filter(|name| name.to_string_lossy().starts_with("generation-"))
            .collect();
        assert_eq!(finalized.len(), 1);
        assert!(!fs::read_dir(&build).unwrap().flatten().any(|entry| entry
            .file_name()
            .to_string_lossy()
            .starts_with("generation.tmp-")));

        manager.activate_core(&core).unwrap();
        assert_eq!(fs::read_link(build.join("current")).unwrap(), finalized[0]);
        assert_eq!(
            fs::read_dir(&build)
                .unwrap()
                .flatten()
                .filter(|entry| entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with("generation-"))
                .count(),
            1
        );
    }

    #[test]
    fn post_switch_visibility_failure_rolls_current_back() {
        let temp = tempfile::tempdir().unwrap();
        let sources = sources(temp.path());
        let manager = ViewManager::new(
            temp.path().to_path_buf(),
            verified_manifest("20260711", "pkg"),
        )
        .unwrap();
        let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
        let collector = resolution(RuntimeKind::Collector, "collector-v1", &sources.collector);
        manager.activate_core(&core).unwrap();
        let build = temp.path().join("runtimes/views/20260711");
        let current = build.join("current");
        let before = fs::read_link(&current).unwrap();
        let downloads = temp.path().join("downloads");
        let anchored = temp.path().join("downloads-anchored");
        let outside = temp.path().join("outside");
        fs::create_dir(&outside).unwrap();

        assert!(manager
            .activate_collector_with_post_switch_hook(&core, &collector, || {
                fs::rename(&downloads, &anchored).unwrap();
                symlink(&outside, &downloads).unwrap();
            })
            .is_err());
        assert_eq!(fs::read_link(&current).unwrap(), before);
        assert!(fs::read_dir(&outside).unwrap().next().is_none());
        assert!(anchored.is_dir());
        assert!(fs::symlink_metadata(build.join("current.next")).is_err());
    }

    #[test]
    fn verified_view_construction_failure_rolls_current_back() {
        let temp = tempfile::tempdir().unwrap();
        let sources = sources(temp.path());
        let manager = ViewManager::new(
            temp.path().to_path_buf(),
            verified_manifest("20260711", "pkg"),
        )
        .unwrap();
        let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
        let collector = resolution(RuntimeKind::Collector, "collector-v1", &sources.collector);
        manager.activate_core(&core).unwrap();
        let current = temp.path().join("runtimes/views/20260711/current");
        let before = fs::read_link(&current).unwrap();
        manager.fail_next_verified_view_for_test();

        assert_eq!(
            manager.activate_collector(&core, &collector).unwrap_err(),
            "view_injected_handle_failure"
        );
        assert_eq!(fs::read_link(current).unwrap(), before);
    }

    #[test]
    fn downloads_and_auth_replacement_fail_closed_without_outside_writes() {
        for child_name in ["downloads", ".auth"] {
            let temp = tempfile::tempdir().unwrap();
            let state = temp.path().join("state");
            fs::create_dir(&state).unwrap();
            let source_root = temp.path().join("sources");
            let sources = sources(&source_root);
            let manager =
                ViewManager::new(state.clone(), verified_manifest("20260711", "pkg")).unwrap();
            let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
            let visible = state.join(child_name);
            let anchored = state.join(format!("{child_name}-anchored"));
            let outside = temp.path().join("outside");
            fs::create_dir(&outside).unwrap();

            assert_eq!(
                manager
                    .activate_core_with_hook(&core, || {
                        fs::rename(&visible, &anchored).unwrap();
                        symlink(&outside, &visible).unwrap();
                    })
                    .unwrap_err(),
                "view_state_child_changed"
            );
            assert!(fs::read_dir(&outside).unwrap().next().is_none());
            assert!(!state.join("runtimes/views/20260711/current").exists());
        }
    }

    #[test]
    fn replacing_a_required_source_subtree_after_open_is_detected() {
        let temp = tempfile::tempdir().unwrap();
        let sources = sources(temp.path());
        let manager = ViewManager::new(
            temp.path().to_path_buf(),
            verified_manifest("20260711", "pkg"),
        )
        .unwrap();
        let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
        let moved = temp.path().join("scripts-anchored");

        assert_eq!(
            manager
                .activate_core_with_hook(&core, || {
                    fs::rename(sources.core.join("scripts"), &moved).unwrap();
                    fs::create_dir(sources.core.join("scripts")).unwrap();
                    fs::write(sources.core.join("scripts/_run.py"), b"replacement").unwrap();
                })
                .unwrap_err(),
            "view_source_subtree_changed"
        );
        assert!(!temp.path().join("runtimes/views/20260711/current").exists());
        assert_eq!(fs::read(moved.join("_run.py")).unwrap(), b"core");
    }

    #[test]
    fn existing_state_permissions_are_tightened_and_lock_identity_is_pinned() {
        let temp = tempfile::tempdir().unwrap();
        let build = temp.path().join("runtimes/views/20260711");
        fs::create_dir_all(&build).unwrap();
        for path in [
            temp.path().join("downloads"),
            temp.path().join(".auth"),
            temp.path().join("runtimes"),
            temp.path().join("runtimes/views"),
            build.clone(),
        ] {
            if !path.exists() {
                fs::create_dir(&path).unwrap();
            }
            fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
        }
        let sources = sources(&temp.path().join("sources"));
        let manager = ViewManager::new(
            temp.path().to_path_buf(),
            verified_manifest("20260711", "pkg"),
        )
        .unwrap();
        for path in [
            temp.path().join("downloads"),
            temp.path().join(".auth"),
            temp.path().join("runtimes"),
            temp.path().join("runtimes/views"),
            build.clone(),
        ] {
            assert_eq!(
                fs::metadata(path).unwrap().permissions().mode() & 0o777,
                0o700
            );
        }
        let original_lock = build.join(".view.lock");
        fs::rename(&original_lock, build.join(".view.lock.old")).unwrap();
        fs::write(&original_lock, b"replacement").unwrap();
        let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
        assert_eq!(
            manager.activate_core(&core).unwrap_err(),
            "view_lock_changed"
        );
        assert!(!build.join("current").exists());
    }

    #[test]
    fn preexisting_hardlinked_lock_is_rejected_before_chmod() {
        let temp = tempfile::tempdir().unwrap();
        let build = temp.path().join("runtimes/views/20260711");
        fs::create_dir_all(&build).unwrap();
        let lock = build.join(".view.lock");
        let outside = temp.path().join("outside-lock");
        fs::write(&lock, b"lock").unwrap();
        fs::set_permissions(&lock, fs::Permissions::from_mode(0o644)).unwrap();
        fs::hard_link(&lock, &outside).unwrap();

        assert!(ViewManager::new(
            temp.path().to_path_buf(),
            verified_manifest("20260711", "pkg")
        )
        .is_err());
        assert_eq!(
            fs::metadata(&outside).unwrap().permissions().mode() & 0o777,
            0o644
        );
    }

    #[test]
    fn view_lock_serializes_a_real_second_process() {
        let temp = tempfile::tempdir().unwrap();
        let manager = ViewManager::new(
            temp.path().to_path_buf(),
            verified_manifest("20260711", "pkg"),
        )
        .unwrap();
        let guard = manager.acquire_file_lock().unwrap();
        let lock = temp.path().join("runtimes/views/20260711/.view.lock");
        let probe = r#"import fcntl, sys
f = open(sys.argv[1], "r+")
try:
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(0)
raise SystemExit(7)
"#;
        let blocked = Command::new("/usr/bin/python3")
            .arg("-c")
            .arg(probe)
            .arg(&lock)
            .status()
            .unwrap();
        assert!(blocked.success());
        drop(guard);
        let acquire_probe = r#"import fcntl, sys
f = open(sys.argv[1], "r+")
try:
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(7)
raise SystemExit(0)
"#;
        let acquired = Command::new("/usr/bin/python3")
            .arg("-c")
            .arg(acquire_probe)
            .arg(lock)
            .status()
            .unwrap();
        assert!(acquired.success());
    }

    #[test]
    fn finalized_manifest_collision_is_rejected_even_without_current() {
        let temp = tempfile::tempdir().unwrap();
        let sources = sources(temp.path());
        let first = ViewManager::new(
            temp.path().to_path_buf(),
            verified_manifest("20260711", "first"),
        )
        .unwrap();
        let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
        first.activate_core(&core).unwrap();
        fs::remove_file(temp.path().join("runtimes/views/20260711/current")).unwrap();
        let second = ViewManager::new(
            temp.path().to_path_buf(),
            verified_manifest("20260711", "second"),
        )
        .unwrap();

        assert_eq!(
            second.activate_core(&core).unwrap_err(),
            "view_build_collision"
        );
        assert!(!temp.path().join("runtimes/views/20260711/current").exists());
    }

    #[test]
    fn desired_components_upgrade_fallbacks_but_never_downgrade() {
        let temp = tempfile::tempdir().unwrap();
        let fallback = sources(&temp.path().join("fallback"));
        let desired_seed = sources(&temp.path().join("desired"));
        let desired_core = desired_seed.core.parent().unwrap().join("core-v2");
        let desired_collector = desired_seed
            .collector
            .parent()
            .unwrap()
            .join("collector-v2");
        fs::rename(&desired_seed.core, &desired_core).unwrap();
        fs::rename(&desired_seed.collector, &desired_collector).unwrap();
        let manager = ViewManager::new(
            temp.path().to_path_buf(),
            verified_manifest("20260711", "pkg"),
        )
        .unwrap();
        let fallback_core = resolution(RuntimeKind::Core, "core-v1", &fallback.core);
        let desired_core_resolution = resolution(RuntimeKind::Core, "core-v2", &desired_core);
        let fallback_collector =
            resolution(RuntimeKind::Collector, "collector-v1", &fallback.collector);
        let desired_collector_resolution =
            resolution(RuntimeKind::Collector, "collector-v2", &desired_collector);
        let current = temp.path().join("runtimes/views/20260711/current");

        manager.activate_core(&fallback_core).unwrap();
        let fallback_target = fs::read_link(&current).unwrap();
        manager.activate_core(&desired_core_resolution).unwrap();
        let desired_target = fs::read_link(&current).unwrap();
        assert_ne!(desired_target, fallback_target);
        manager.activate_core(&fallback_core).unwrap();
        assert_eq!(fs::read_link(&current).unwrap(), desired_target);

        manager
            .activate_collector(&desired_core_resolution, &desired_collector_resolution)
            .unwrap();
        let desired_full = fs::read_link(&current).unwrap();
        manager
            .activate_collector(&desired_core_resolution, &fallback_collector)
            .unwrap();
        assert_eq!(fs::read_link(&current).unwrap(), desired_full);
    }

    #[test]
    fn tampered_script_inventory_is_rebuilt_from_exact_hardlinks() {
        for case in [
            "replace-run",
            "extra-sitecustomize",
            "extra-pycache",
            "escape-link",
        ] {
            let temp = tempfile::tempdir().unwrap();
            let sources = sources(temp.path());
            let manager = ViewManager::new(
                temp.path().to_path_buf(),
                verified_manifest("20260711", "pkg"),
            )
            .unwrap();
            let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
            manager.activate_core(&core).unwrap();
            let build = temp.path().join("runtimes/views/20260711");
            let current = build.join("current");
            let before = fs::read_link(&current).unwrap();
            let scripts = build.join(&before).join("scripts");
            fs::set_permissions(&scripts, fs::Permissions::from_mode(0o755)).unwrap();
            match case {
                "replace-run" => {
                    fs::remove_file(scripts.join("_run.py")).unwrap();
                    fs::write(scripts.join("_run.py"), b"injected").unwrap();
                }
                "extra-sitecustomize" => {
                    fs::write(scripts.join("sitecustomize.py"), b"injected").unwrap();
                }
                "extra-pycache" => {
                    fs::create_dir(scripts.join("__pycache__")).unwrap();
                    fs::write(scripts.join("__pycache__/runner.pyc"), b"injected").unwrap();
                }
                "escape-link" => {
                    fs::remove_file(scripts.join("_run.py")).unwrap();
                    symlink("../../outside.py", scripts.join("_run.py")).unwrap();
                }
                _ => unreachable!(),
            }

            manager.activate_core(&core).unwrap();
            let after = fs::read_link(&current).unwrap();
            assert_ne!(after, before, "tamper case: {case}");
            assert_eq!(fs::read(current.join("scripts/_run.py")).unwrap(), b"core");
            assert!(!current.join("scripts/sitecustomize.py").exists());
            assert!(!current.join("scripts/__pycache__").exists());
        }
    }

    #[test]
    fn missing_collector_script_is_rebuilt_and_script_tree_is_read_only() {
        let temp = tempfile::tempdir().unwrap();
        let sources = sources(temp.path());
        let manager = ViewManager::new(
            temp.path().to_path_buf(),
            verified_manifest("20260711", "pkg"),
        )
        .unwrap();
        let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
        let collector = resolution(RuntimeKind::Collector, "collector-v1", &sources.collector);
        manager.activate_collector(&core, &collector).unwrap();
        let build = temp.path().join("runtimes/views/20260711");
        let current = build.join("current");
        let before = fs::read_link(&current).unwrap();
        let scripts = build.join(&before).join("scripts");
        fs::set_permissions(&scripts, fs::Permissions::from_mode(0o755)).unwrap();
        fs::remove_file(scripts.join("douyin_export.mjs")).unwrap();

        manager.activate_collector(&core, &collector).unwrap();
        assert_ne!(fs::read_link(&current).unwrap(), before);
        assert_eq!(
            fs::read(current.join("scripts/douyin_export.mjs")).unwrap(),
            b"collector"
        );
        assert_eq!(
            fs::metadata(current.join("scripts"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o555
        );
        assert_eq!(
            fs::metadata(current.join("scripts/domain"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o555
        );
        assert!(fs::create_dir(current.join("scripts/__pycache__")).is_err());
    }

    #[test]
    fn cleanup_refuses_a_top_level_inode_swap() {
        let temp = tempfile::tempdir().unwrap();
        let parent = temp.path().join("build");
        let owned = parent.join("generation.tmp-owned");
        let parked = parent.join("parked-owned");
        let victim = parent.join("victim");
        fs::create_dir_all(&owned).unwrap();
        fs::create_dir_all(&victim).unwrap();
        fs::write(owned.join("owned.txt"), b"owned").unwrap();
        fs::write(victim.join("victim.txt"), b"victim").unwrap();
        let parent_fd = open_directory(&parent).unwrap();

        assert_eq!(
            remove_name_with_hook(&parent_fd, "generation.tmp-owned", || {
                fs::rename(&owned, &parked).unwrap();
                fs::rename(&victim, &owned).unwrap();
            })
            .unwrap_err(),
            "view_cleanup_changed"
        );
        assert_eq!(fs::read(owned.join("victim.txt")).unwrap(), b"victim");
        assert_eq!(fs::read(parked.join("owned.txt")).unwrap(), b"owned");
    }

    #[test]
    fn opaque_view_handle_detects_a_later_current_switch() {
        let temp = tempfile::tempdir().unwrap();
        let sources = sources(temp.path());
        let manager = ViewManager::new(
            temp.path().to_path_buf(),
            verified_manifest("20260711", "pkg"),
        )
        .unwrap();
        let core = resolution(RuntimeKind::Core, "core-v1", &sources.core);
        let collector = resolution(RuntimeKind::Collector, "collector-v1", &sources.collector);

        let core_view = manager.activate_core(&core).unwrap();
        core_view.verify_visible().unwrap();
        let full_view = manager.activate_collector(&core, &collector).unwrap();
        assert_eq!(core_view.path(), full_view.path());
        assert_eq!(
            core_view.verify_visible().unwrap_err(),
            "view_handle_changed"
        );
        full_view.verify_visible().unwrap();
    }
}
