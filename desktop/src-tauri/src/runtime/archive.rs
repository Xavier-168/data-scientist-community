use std::{
    collections::{BTreeMap, HashSet},
    fs::{File, OpenOptions},
    io::{self, Read, Seek, SeekFrom, Write},
    path::Path,
    process::{Command, Stdio},
    str,
    sync::atomic::{AtomicBool, Ordering},
    time::{Duration, Instant},
};

#[cfg(unix)]
use std::os::unix::{ffi::OsStrExt, fs::OpenOptionsExt, process::CommandExt};

#[cfg(windows)]
use std::path::PathBuf;

use thiserror::Error;

use crate::manifest::RuntimeDescriptor;
#[cfg(unix)]
use object::{
    read::{
        macho::{FatArch as _, MachOFatFile32, MachOFatFile64},
        ReadCache,
    },
    Architecture, FileKind, Object as _,
};
#[cfg(windows)]
use object::{read::ReadCache, Architecture, FileKind, Object as _};
use sha2::{Digest, Sha256};

/// 已锚定的目录引用：Unix 为已打开的目录 fd（fd-at 锚定）；
/// Windows 为校验过的目录路径（std 无法持有目录句柄，依赖唯一临时名
/// + rename 保证原子性）。
#[cfg(unix)]
type DirectoryRef = File;
#[cfg(windows)]
type DirectoryRef = PathBuf;

#[derive(Debug, Error)]
pub enum ArchiveError {
    #[error("archive_io: {0}")]
    Io(#[from] io::Error),
    #[error("archive_unsafe_path: {0}")]
    UnsafePath(String),
    #[error("archive_unsafe_entry: {0}")]
    UnsafeEntry(String),
    #[error("archive_limit_exceeded: {0}")]
    Limit(String),
    #[error("archive_size_mismatch")]
    SizeMismatch,
    #[error("archive_hash_mismatch")]
    HashMismatch,
    #[error("disk_space_insufficient")]
    DiskSpace,
    #[error("runtime_required_file_missing: {0}")]
    RequiredFile(String),
    #[error("runtime_smoke_failed: {path}: exit={exit_code}")]
    SmokeFailed { path: String, exit_code: i32 },
    #[error("runtime_smoke_timeout: {0}")]
    SmokeTimeout(String),
    #[error("runtime_cancelled")]
    Cancelled,
    #[error("{primary}; runtime_cleanup_failed: {cleanup}")]
    Cleanup {
        primary: Box<ArchiveError>,
        cleanup: String,
    },
}

const MAX_LINK_HOPS: usize = 256;
const MAX_TREE_DEPTH: usize = 256;
const MAX_ENTRIES: usize = 200_000;
const MAX_FILE_BYTES: u64 = 8 * 1024 * 1024 * 1024;
const MAX_TOTAL_BYTES: u64 = 16 * 1024 * 1024 * 1024;
const MAX_PATH_BYTES: usize = 4096;
const MAX_EXPANSION_RATIO: u64 = 512;
const EXPANSION_ALLOWANCE: u64 = 64 * 1024 * 1024;
const MAX_LOCAL_PAX_BYTES: u64 = 16 * 1024;
pub(crate) const RUNTIME_MARKER_NAME: &str = ".runtime-verified.json";
pub(crate) const RUNTIME_PROVENANCE_NAME: &str = ".runtime-provenance.json";

#[derive(Clone, Copy, Debug)]
struct ArchiveLimits {
    entries: usize,
    file_bytes: u64,
    total_bytes: u64,
    path_bytes: usize,
    expansion_ratio: u64,
    expansion_allowance: u64,
}

impl Default for ArchiveLimits {
    fn default() -> Self {
        Self {
            entries: MAX_ENTRIES,
            file_bytes: MAX_FILE_BYTES,
            total_bytes: MAX_TOTAL_BYTES,
            path_bytes: MAX_PATH_BYTES,
            expansion_ratio: MAX_EXPANSION_RATIO,
            expansion_allowance: EXPANSION_ALLOWANCE,
        }
    }
}

struct CancellableReader<'a, R> {
    inner: R,
    cancellation: &'a AtomicBool,
    read_bytes: u64,
    maximum: u64,
}

impl<'a, R> CancellableReader<'a, R> {
    fn new(inner: R, cancellation: &'a AtomicBool, maximum: u64) -> Self {
        Self {
            inner,
            cancellation,
            read_bytes: 0,
            maximum,
        }
    }
}

impl<R: Read> Read for CancellableReader<'_, R> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        if self.cancellation.load(Ordering::Acquire) {
            return Err(io::Error::new(
                io::ErrorKind::Interrupted,
                "runtime_cancelled",
            ));
        }
        let read = self.inner.read(buffer)?;
        self.read_bytes = self
            .read_bytes
            .checked_add(read as u64)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "archive_limit"))?;
        if self.read_bytes > self.maximum {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "archive_limit"));
        }
        if self.cancellation.load(Ordering::Acquire) {
            return Err(io::Error::new(
                io::ErrorKind::Interrupted,
                "runtime_cancelled",
            ));
        }
        Ok(read)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum LayoutKind {
    Regular { size: u64 },
    Directory,
    Symlink { target: String },
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct LayoutRecord {
    path: String,
    kind: LayoutKind,
    mode: u32,
}

impl LayoutRecord {
    fn regular(path: &str, size: u64, mode: u32) -> Self {
        Self {
            path: path.into(),
            kind: LayoutKind::Regular { size },
            mode,
        }
    }

    fn directory(path: &str, mode: u32) -> Self {
        Self {
            path: path.into(),
            kind: LayoutKind::Directory,
            mode,
        }
    }

    fn symlink(path: &str, target: &str, mode: u32) -> Self {
        Self {
            path: path.into(),
            kind: LayoutKind::Symlink {
                target: target.into(),
            },
            mode,
        }
    }
}

#[derive(Clone, Debug)]
struct ArchiveLayout {
    entries: BTreeMap<String, LayoutRecord>,
    symlinks: BTreeMap<String, String>,
}

impl ArchiveLayout {
    fn from_records(records: Vec<LayoutRecord>) -> Result<Self, ArchiveError> {
        let mut entries = BTreeMap::new();
        let mut symlinks = BTreeMap::new();
        for record in records {
            let path = canonical_posix_path(record.path.as_bytes())?;
            if entries.insert(path.clone(), record.clone()).is_some() {
                return Err(ArchiveError::UnsafePath(format!("duplicate:{path}")));
            }
            if let LayoutKind::Symlink { target } = &record.kind {
                symlinks.insert(
                    path.clone(),
                    normalized_link_target(&path, target.as_bytes())?,
                );
            }
        }
        for path in entries.keys() {
            let parts: Vec<_> = path.split('/').collect();
            for index in 1..parts.len() {
                let ancestor = parts[..index].join("/");
                match entries.get(&ancestor).map(|record| &record.kind) {
                    Some(LayoutKind::Directory) => {}
                    Some(LayoutKind::Symlink { .. }) => {
                        return Err(ArchiveError::UnsafePath(format!(
                            "symlink_ancestor:{ancestor}:{path}"
                        )))
                    }
                    Some(LayoutKind::Regular { .. }) => {
                        return Err(ArchiveError::UnsafePath(format!(
                            "regular_ancestor:{ancestor}:{path}"
                        )))
                    }
                    None => {
                        return Err(ArchiveError::UnsafePath(format!(
                            "missing_directory:{ancestor}:{path}"
                        )))
                    }
                }
            }
        }
        let layout = Self { entries, symlinks };
        for link in layout.symlinks.keys() {
            layout.resolve(link)?;
        }
        Ok(layout)
    }

    fn resolve(&self, path: &str) -> Result<String, ArchiveError> {
        let mut current = path.to_owned();
        let mut visited = HashSet::new();
        for _ in 0..=MAX_LINK_HOPS {
            let parts: Vec<_> = current.split('/').collect();
            let mut matched = None;
            for index in 1..=parts.len() {
                let prefix = parts[..index].join("/");
                if let Some(target) = self.symlinks.get(&prefix) {
                    matched = Some((prefix, target.clone(), parts[index..].join("/")));
                    break;
                }
            }
            let Some((link, target, suffix)) = matched else {
                if self.entries.contains_key(&current) {
                    return Ok(current);
                }
                return Err(ArchiveError::UnsafePath(format!(
                    "dangling:{path}:{current}"
                )));
            };
            if !visited.insert(link.clone()) {
                return Err(ArchiveError::UnsafePath(format!("link_cycle:{link}")));
            }
            current = if suffix.is_empty() {
                target
            } else {
                format!("{target}/{suffix}")
            };
        }
        Err(ArchiveError::UnsafePath(format!("link_hops:{path}")))
    }

    fn resolve_regular(&self, path: &str) -> Result<String, ArchiveError> {
        let resolved = self.resolve(path)?;
        if matches!(
            self.entries.get(&resolved).map(|record| &record.kind),
            Some(LayoutKind::Regular { .. })
        ) {
            Ok(resolved)
        } else {
            Err(ArchiveError::UnsafePath(format!("not_regular:{path}")))
        }
    }
}

fn canonical_posix_path(raw: &[u8]) -> Result<String, ArchiveError> {
    let value = str::from_utf8(raw).map_err(|_| ArchiveError::UnsafePath("non_utf8".into()))?;
    if value.is_empty()
        || value.starts_with('/')
        || value.ends_with('/')
        || value.contains(['\\', '\0'])
        || value
            .split('/')
            .any(|part| part.is_empty() || part == "." || part == "..")
        || value.split('/').count() > MAX_TREE_DEPTH
    {
        return Err(ArchiveError::UnsafePath(value.escape_default().to_string()));
    }
    Ok(value.to_owned())
}

fn normalized_link_target(entry_path: &str, raw: &[u8]) -> Result<String, ArchiveError> {
    let target = str::from_utf8(raw)
        .map_err(|_| ArchiveError::UnsafePath(format!("link_non_utf8:{entry_path}")))?;
    if target.is_empty()
        || target.starts_with('/')
        || target.ends_with('/')
        || target.contains(['\\', '\0'])
    {
        return Err(ArchiveError::UnsafePath(format!(
            "link_target:{entry_path}"
        )));
    }
    let mut parts: Vec<&str> = entry_path.split('/').collect();
    parts.pop();
    for part in target.split('/') {
        match part {
            "" | "." => {
                return Err(ArchiveError::UnsafePath(format!(
                    "link_target:{entry_path}"
                )))
            }
            ".." => {
                if parts.pop().is_none() {
                    return Err(ArchiveError::UnsafePath(format!(
                        "link_escape:{entry_path}"
                    )));
                }
            }
            value => parts.push(value),
        }
    }
    if parts.is_empty() {
        return Err(ArchiveError::UnsafePath(format!(
            "link_target:{entry_path}"
        )));
    }
    let normalized = parts.join("/");
    if normalized == entry_path {
        return Err(ArchiveError::UnsafePath(format!("link_self:{entry_path}")));
    }
    Ok(normalized)
}

fn check_cancelled(cancellation: &AtomicBool) -> Result<(), ArchiveError> {
    if cancellation.load(Ordering::Acquire) {
        Err(ArchiveError::Cancelled)
    } else {
        Ok(())
    }
}

#[cfg(unix)]
fn open_archive_file(path: &Path) -> Result<File, ArchiveError> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(nix::libc::O_NOFOLLOW | nix::libc::O_CLOEXEC | nix::libc::O_NONBLOCK)
        .open(path)?;
    if !file.metadata()?.file_type().is_file() {
        return Err(ArchiveError::UnsafeEntry(path.display().to_string()));
    }
    Ok(file)
}

/// Windows 版 `open_archive_file`：std 没有 O_NOFOLLOW，先用
/// symlink_metadata 拒绝符号链接与非常规文件再打开；残余的 TOCTOU
/// 窗口由后续 sha256/size 描述符校验兜底。
#[cfg(windows)]
fn open_archive_file(path: &Path) -> Result<File, ArchiveError> {
    let metadata = std::fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(ArchiveError::UnsafeEntry(path.display().to_string()));
    }
    Ok(File::open(path)?)
}

#[cfg(unix)]
fn open_directory_file(path: &Path) -> Result<File, ArchiveError> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(
            nix::libc::O_DIRECTORY
                | nix::libc::O_NOFOLLOW
                | nix::libc::O_CLOEXEC
                | nix::libc::O_NONBLOCK,
        )
        .open(path)?;
    if !file.metadata()?.file_type().is_dir() {
        return Err(ArchiveError::UnsafePath(path.display().to_string()));
    }
    Ok(file)
}

/// Windows 版 `open_directory_file`：校验目标为非符号链接的真实目录，
/// 返回目录路径作为锚定引用。
#[cfg(windows)]
fn open_directory_file(path: &Path) -> Result<DirectoryRef, ArchiveError> {
    let metadata = std::fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(ArchiveError::UnsafePath(path.display().to_string()));
    }
    Ok(path.to_path_buf())
}

#[cfg(unix)]
fn create_verified_snapshot(
    parent: &File,
    path: &Path,
    descriptor: &RuntimeDescriptor,
    cancellation: &AtomicBool,
) -> Result<File, ArchiveError> {
    use rustix::fs::{openat, unlinkat, AtFlags, Mode, OFlags};

    check_cancelled(cancellation)?;
    let mut source = open_archive_file(path)?;
    if source.metadata()?.len() != descriptor.size_bytes {
        return Err(ArchiveError::SizeMismatch);
    }
    let name = format!(".runtime-snapshot-{}", uuid::Uuid::new_v4());
    let snapshot_fd = openat(
        parent,
        name.as_str(),
        OFlags::RDWR
            | OFlags::CREATE
            | OFlags::EXCL
            | OFlags::NOFOLLOW
            | OFlags::CLOEXEC
            | OFlags::NONBLOCK,
        Mode::RUSR | Mode::WUSR,
    )
    .map_err(io::Error::from)?;
    unlinkat(parent, name.as_str(), AtFlags::empty()).map_err(io::Error::from)?;
    let mut snapshot = File::from(snapshot_fd);
    let mut digest = Sha256::new();
    let mut copied = 0_u64;
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        check_cancelled(cancellation)?;
        let read = source.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        copied = copied
            .checked_add(read as u64)
            .ok_or(ArchiveError::SizeMismatch)?;
        if copied > descriptor.size_bytes {
            return Err(ArchiveError::SizeMismatch);
        }
        digest.update(&buffer[..read]);
        io::Write::write_all(&mut snapshot, &buffer[..read])?;
    }
    if copied != descriptor.size_bytes || snapshot.metadata()?.len() != descriptor.size_bytes {
        return Err(ArchiveError::SizeMismatch);
    }
    if hex::encode(digest.finalize()) != descriptor.sha256 {
        return Err(ArchiveError::HashMismatch);
    }
    snapshot.seek(SeekFrom::Start(0))?;
    Ok(snapshot)
}

/// Windows 版 `create_verified_snapshot`：以 create_new 写入唯一临时名，
/// 校验 sha256/size 后立即删除临时文件（std 以 FILE_SHARE_DELETE 打开，
/// 删除挂起状态下句柄仍可读），随后所有扫描/解压都基于该私有快照。
#[cfg(windows)]
fn create_verified_snapshot(
    parent: &Path,
    path: &Path,
    descriptor: &RuntimeDescriptor,
    cancellation: &AtomicBool,
) -> Result<File, ArchiveError> {
    check_cancelled(cancellation)?;
    let mut source = open_archive_file(path)?;
    if source.metadata()?.len() != descriptor.size_bytes {
        return Err(ArchiveError::SizeMismatch);
    }
    let temporary =
        parent.join(format!(".runtime-snapshot-{}", uuid::Uuid::new_v4()));
    let mut snapshot = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(&temporary)?;
    std::fs::remove_file(&temporary)?;
    let mut digest = Sha256::new();
    let mut copied = 0_u64;
    // 堆分配缓冲：1MB 栈数组在受限线程（tokio worker 2MB 栈）上会溢出
    let mut buffer = vec![0_u8; 1024 * 1024];
    loop {
        check_cancelled(cancellation)?;
        let read = source.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        copied = copied
            .checked_add(read as u64)
            .ok_or(ArchiveError::SizeMismatch)?;
        if copied > descriptor.size_bytes {
            return Err(ArchiveError::SizeMismatch);
        }
        digest.update(&buffer[..read]);
        snapshot.write_all(&buffer[..read])?;
    }
    if copied != descriptor.size_bytes || snapshot.metadata()?.len() != descriptor.size_bytes {
        return Err(ArchiveError::SizeMismatch);
    }
    if hex::encode(digest.finalize()) != descriptor.sha256 {
        return Err(ArchiveError::HashMismatch);
    }
    snapshot.seek(SeekFrom::Start(0))?;
    Ok(snapshot)
}

fn stream_error(error: io::Error) -> ArchiveError {
    let message = error.to_string();
    if message.contains("runtime_cancelled") {
        ArchiveError::Cancelled
    } else if message.contains("archive_limit") {
        ArchiveError::Limit("decompressed_stream".into())
    } else {
        ArchiveError::Io(error)
    }
}

fn tar_octal(field: &[u8]) -> Result<u64, ArchiveError> {
    if field.first().is_some_and(|byte| byte & 0x80 != 0) {
        return Err(ArchiveError::UnsafeEntry("tar_base256".into()));
    }
    let trimmed = field
        .iter()
        .copied()
        .skip_while(|byte| matches!(byte, b' ' | 0))
        .take_while(|byte| !matches!(byte, b' ' | 0));
    let mut value = 0_u64;
    for byte in trimmed {
        if !(b'0'..=b'7').contains(&byte) {
            return Err(ArchiveError::UnsafeEntry("tar_number".into()));
        }
        value = value
            .checked_mul(8)
            .and_then(|current| current.checked_add((byte - b'0') as u64))
            .ok_or_else(|| ArchiveError::Limit("tar_number".into()))?;
    }
    Ok(value)
}

fn validate_tar_header_checksum(header: &[u8; 512]) -> Result<(), ArchiveError> {
    let expected = tar_octal(&header[148..156])?;
    let actual = header[..148]
        .iter()
        .chain([b' '; 8].iter())
        .chain(header[156..].iter())
        .try_fold(0_u64, |sum, byte| sum.checked_add(*byte as u64))
        .ok_or_else(|| ArchiveError::Limit("tar_checksum".into()))?;
    if actual != expected {
        return Err(ArchiveError::UnsafeEntry("tar_checksum".into()));
    }
    Ok(())
}

fn read_exact_stream<R: Read>(reader: &mut R, buffer: &mut [u8]) -> Result<(), ArchiveError> {
    reader.read_exact(buffer).map_err(stream_error)
}

fn skip_exact_stream<R: Read>(reader: &mut R, length: u64) -> Result<(), ArchiveError> {
    let copied = io::copy(&mut reader.take(length), &mut io::sink()).map_err(stream_error)?;
    if copied != length {
        return Err(ArchiveError::UnsafeEntry("truncated_tar_entry".into()));
    }
    Ok(())
}

fn validate_local_pax(data: &[u8]) -> Result<(), ArchiveError> {
    let mut offset = 0_usize;
    let mut keys = HashSet::new();
    while offset < data.len() {
        let space = data[offset..]
            .iter()
            .position(|byte| *byte == b' ')
            .ok_or_else(|| ArchiveError::UnsafeEntry("malformed_pax".into()))?
            + offset;
        let length_text = str::from_utf8(&data[offset..space])
            .map_err(|_| ArchiveError::UnsafeEntry("malformed_pax".into()))?;
        let length: usize = length_text
            .parse()
            .map_err(|_| ArchiveError::UnsafeEntry("malformed_pax".into()))?;
        let end = offset
            .checked_add(length)
            .filter(|end| *end <= data.len())
            .ok_or_else(|| ArchiveError::UnsafeEntry("malformed_pax".into()))?;
        if length == 0 || data.get(end.wrapping_sub(1)) != Some(&b'\n') {
            return Err(ArchiveError::UnsafeEntry("malformed_pax".into()));
        }
        let record = &data[space + 1..end - 1];
        let equals = record
            .iter()
            .position(|byte| *byte == b'=')
            .ok_or_else(|| ArchiveError::UnsafeEntry("malformed_pax".into()))?;
        let key = &record[..equals];
        let value = &record[equals + 1..];
        if !matches!(key, b"path" | b"linkpath") || !keys.insert(key.to_vec()) {
            return Err(ArchiveError::UnsafeEntry(format!(
                "pax:{}",
                String::from_utf8_lossy(key)
            )));
        }
        if value.len() > MAX_PATH_BYTES {
            return Err(ArchiveError::Limit("pax_path_length".into()));
        }
        str::from_utf8(value).map_err(|_| ArchiveError::UnsafeEntry("pax_non_utf8".into()))?;
        offset = end;
    }
    Ok(())
}

fn raw_tar_preflight(
    file: &mut File,
    compressed_size: u64,
    cancellation: &AtomicBool,
    limits: ArchiveLimits,
) -> Result<(), ArchiveError> {
    file.seek(SeekFrom::Start(0))?;
    let maximum = compressed_size
        .checked_mul(limits.expansion_ratio)
        .and_then(|value| value.checked_add(limits.expansion_allowance))
        .ok_or_else(|| ArchiveError::Limit("expansion_ratio".into()))?;
    let decoder = zstd::stream::read::Decoder::new(file).map_err(stream_error)?;
    let mut reader = CancellableReader::new(decoder, cancellation, maximum);
    let mut header = [0_u8; 512];
    let mut pending_pax = false;
    let mut zero_blocks = 0_u8;
    let mut raw_entries = 0_usize;
    let mut logical_entries = 0_usize;
    let mut total_file_bytes = 0_u64;
    loop {
        check_cancelled(cancellation)?;
        read_exact_stream(&mut reader, &mut header)?;
        if header.iter().all(|byte| *byte == 0) {
            zero_blocks += 1;
            if zero_blocks == 2 {
                break;
            }
            continue;
        }
        zero_blocks = 0;
        validate_tar_header_checksum(&header)?;
        raw_entries = raw_entries
            .checked_add(1)
            .ok_or_else(|| ArchiveError::Limit("entry_count".into()))?;
        if raw_entries > limits.entries.saturating_mul(2) {
            return Err(ArchiveError::Limit("entry_count".into()));
        }
        let entry_type = header[156];
        let size = tar_octal(&header[124..136])?;
        let padded = size
            .checked_add(511)
            .map(|value| value / 512 * 512)
            .ok_or_else(|| ArchiveError::Limit("tar_size".into()))?;
        match entry_type {
            b'x' => {
                if pending_pax || size > MAX_LOCAL_PAX_BYTES {
                    return Err(ArchiveError::Limit("pax_size".into()));
                }
                let mut data = vec![0_u8; size as usize];
                read_exact_stream(&mut reader, &mut data)?;
                validate_local_pax(&data)?;
                skip_exact_stream(&mut reader, padded - size)?;
                pending_pax = true;
            }
            0 | b'0' | b'2' | b'5' => {
                logical_entries = logical_entries
                    .checked_add(1)
                    .ok_or_else(|| ArchiveError::Limit("entry_count".into()))?;
                if logical_entries > limits.entries {
                    return Err(ArchiveError::Limit("entry_count".into()));
                }
                if matches!(entry_type, 0 | b'0') {
                    if size > limits.file_bytes {
                        return Err(ArchiveError::Limit("file_size".into()));
                    }
                    total_file_bytes = total_file_bytes
                        .checked_add(size)
                        .ok_or_else(|| ArchiveError::Limit("total_size".into()))?;
                    if total_file_bytes > limits.total_bytes {
                        return Err(ArchiveError::Limit("total_size".into()));
                    }
                } else if size != 0 {
                    return Err(ArchiveError::UnsafeEntry("non_file_size".into()));
                }
                pending_pax = false;
                skip_exact_stream(&mut reader, padded)?;
            }
            b'g' | b'L' | b'K' | b'S' => {
                return Err(ArchiveError::UnsafeEntry(format!(
                    "tar_extension:{entry_type}"
                )))
            }
            _ => return Err(ArchiveError::UnsafeEntry(format!("tar_type:{entry_type}"))),
        }
    }
    if pending_pax {
        return Err(ArchiveError::UnsafeEntry("orphan_pax".into()));
    }
    io::copy(&mut reader, &mut io::sink()).map_err(stream_error)?;
    Ok(())
}

#[derive(Debug)]
struct ScanResult {
    layout: ArchiveLayout,
    unpacked_bytes: u64,
    entry_count: usize,
}

fn scan_open_archive(
    file: &mut File,
    compressed_size: u64,
    cancellation: &AtomicBool,
    limits: ArchiveLimits,
) -> Result<ScanResult, ArchiveError> {
    check_cancelled(cancellation)?;
    raw_tar_preflight(file, compressed_size, cancellation, limits)?;
    file.seek(SeekFrom::Start(0))?;
    let ratio_limit = compressed_size
        .checked_mul(limits.expansion_ratio)
        .and_then(|value| value.checked_add(limits.expansion_allowance))
        .ok_or_else(|| ArchiveError::Limit("expansion_ratio".into()))?;
    let structural_allowance = (limits.entries as u64)
        .checked_mul(1024)
        .and_then(|value| value.checked_add(limits.expansion_allowance))
        .ok_or_else(|| ArchiveError::Limit("archive_structure".into()))?;
    let absolute_limit = limits
        .total_bytes
        .checked_add(structural_allowance)
        .ok_or_else(|| ArchiveError::Limit("archive_structure".into()))?;
    let decoder = zstd::stream::read::Decoder::new(file).map_err(stream_error)?;
    let reader = CancellableReader::new(decoder, cancellation, ratio_limit.min(absolute_limit));
    let mut archive = tar::Archive::new(reader);
    let mut records = Vec::new();
    let mut total = 0_u64;
    let mut previous_path: Option<String> = None;
    {
        let entries = archive.entries().map_err(stream_error)?;
        for item in entries {
            check_cancelled(cancellation)?;
            if records.len() >= limits.entries {
                return Err(ArchiveError::Limit("entry_count".into()));
            }
            let mut entry = item.map_err(stream_error)?;
            let mut pax_path: Option<Vec<u8>> = None;
            let mut pax_link: Option<Vec<u8>> = None;
            if let Some(extensions) = entry.pax_extensions().map_err(stream_error)? {
                for extension in extensions {
                    let extension = extension.map_err(stream_error)?;
                    match extension.key_bytes() {
                        b"path" if pax_path.is_none() => {
                            pax_path = Some(extension.value_bytes().to_vec())
                        }
                        b"linkpath" if pax_link.is_none() => {
                            pax_link = Some(extension.value_bytes().to_vec())
                        }
                        key => {
                            return Err(ArchiveError::UnsafeEntry(format!(
                                "pax:{}",
                                String::from_utf8_lossy(key)
                            )))
                        }
                    }
                }
            }

            let entry_type = entry.header().entry_type();
            let effective_path = entry.path_bytes().into_owned();
            if effective_path.len() > limits.path_bytes {
                return Err(ArchiveError::Limit("path_length".into()));
            }
            let canonical_path_bytes = if entry_type.is_dir() && effective_path.ends_with(b"/") {
                if effective_path.ends_with(b"//") {
                    return Err(ArchiveError::UnsafePath("directory_trailing_slash".into()));
                }
                &effective_path[..effective_path.len() - 1]
            } else {
                effective_path.as_slice()
            };
            let path = canonical_posix_path(canonical_path_bytes)?;
            if let Some(pax) = &pax_path {
                if pax != &effective_path {
                    return Err(ArchiveError::UnsafeEntry(format!("pax_path:{path}")));
                }
                let pax_canonical = if entry_type.is_dir() && pax.ends_with(b"/") {
                    &pax[..pax.len() - 1]
                } else {
                    pax.as_slice()
                };
                canonical_posix_path(pax_canonical)?;
            } else if entry.header().path_bytes().as_ref() != effective_path.as_slice() {
                return Err(ArchiveError::UnsafeEntry(format!("non_pax_path:{path}")));
            }
            if previous_path
                .as_ref()
                .is_some_and(|previous| previous.as_bytes() >= path.as_bytes())
            {
                return Err(ArchiveError::UnsafeEntry(format!("member_order:{path}")));
            }
            previous_path = Some(path.clone());

            let header = entry.header();
            if header.uid().map_err(stream_error)? != 0
                || header.gid().map_err(stream_error)? != 0
                || header.mtime().map_err(stream_error)? != 0
                || header
                    .username_bytes()
                    .is_some_and(|value| !value.is_empty())
                || header
                    .groupname_bytes()
                    .is_some_and(|value| !value.is_empty())
            {
                return Err(ArchiveError::UnsafeEntry(format!("metadata:{path}")));
            }
            let mode = header.mode().map_err(stream_error)? & 0o7777;
            let size = header.size().map_err(stream_error)?;
            let record = if entry_type.is_file() {
                if !matches!(mode, 0o644 | 0o755) {
                    return Err(ArchiveError::UnsafeEntry(format!("mode:{path}")));
                }
                if size > limits.file_bytes {
                    return Err(ArchiveError::Limit(format!("file_size:{path}")));
                }
                total = total
                    .checked_add(size)
                    .ok_or_else(|| ArchiveError::Limit("total_size".into()))?;
                if total > limits.total_bytes {
                    return Err(ArchiveError::Limit("total_size".into()));
                }
                if entry.link_name_bytes().is_some() || pax_link.is_some() {
                    return Err(ArchiveError::UnsafeEntry(format!("file_link:{path}")));
                }
                LayoutRecord::regular(&path, size, mode)
            } else if entry_type.is_dir() {
                if mode != 0o755 || size != 0 || entry.link_name_bytes().is_some() {
                    return Err(ArchiveError::UnsafeEntry(format!("directory:{path}")));
                }
                LayoutRecord::directory(&path, mode)
            } else if entry_type.is_symlink() {
                if mode != 0o777 || size != 0 {
                    return Err(ArchiveError::UnsafeEntry(format!("symlink:{path}")));
                }
                let effective_link = entry
                    .link_name_bytes()
                    .ok_or_else(|| ArchiveError::UnsafeEntry(format!("symlink:{path}")))?
                    .into_owned();
                if effective_link.len() > limits.path_bytes {
                    return Err(ArchiveError::Limit("link_length".into()));
                }
                if let Some(pax) = &pax_link {
                    if pax != &effective_link {
                        return Err(ArchiveError::UnsafeEntry(format!("pax_link:{path}")));
                    }
                } else if header.link_name_bytes().map(|value| value.into_owned())
                    != Some(effective_link.clone())
                {
                    return Err(ArchiveError::UnsafeEntry(format!("non_pax_link:{path}")));
                }
                let target = str::from_utf8(&effective_link)
                    .map_err(|_| ArchiveError::UnsafePath(format!("link_non_utf8:{path}")))?;
                normalized_link_target(&path, target.as_bytes())?;
                LayoutRecord::symlink(&path, target, mode)
            } else {
                return Err(ArchiveError::UnsafeEntry(format!(
                    "type:{path}:{}",
                    entry_type.as_byte()
                )));
            };
            records.push(record);
        }
    }
    let mut reader = archive.into_inner();
    io::copy(&mut reader, &mut io::sink()).map_err(stream_error)?;
    check_cancelled(cancellation)?;
    let layout = ArchiveLayout::from_records(records)?;
    Ok(ScanResult {
        entry_count: layout.entries.len(),
        layout,
        unpacked_bytes: total,
    })
}

fn scan_archive_with_hook<F: FnOnce()>(
    path: &Path,
    cancellation: &AtomicBool,
    after_snapshot: F,
) -> Result<u64, ArchiveError> {
    let mut source = open_archive_file(path)?;
    let compressed_size = source.metadata()?.len();
    let mut file = tempfile::tempfile()?;
    let mut copied = 0_u64;
    // 堆分配缓冲：1MB 栈数组在受限线程（tokio worker 2MB 栈）上会溢出
    let mut buffer = vec![0_u8; 1024 * 1024];
    loop {
        check_cancelled(cancellation)?;
        let read = source.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        copied = copied
            .checked_add(read as u64)
            .ok_or(ArchiveError::SizeMismatch)?;
        if copied > compressed_size {
            return Err(ArchiveError::SizeMismatch);
        }
        file.write_all(&buffer[..read])?;
    }
    if copied != compressed_size {
        return Err(ArchiveError::SizeMismatch);
    }
    file.seek(SeekFrom::Start(0))?;
    after_snapshot();
    Ok(scan_open_archive(
        &mut file,
        compressed_size,
        cancellation,
        ArchiveLimits::default(),
    )?
    .unpacked_bytes)
}

pub fn scan_archive(path: &Path, cancellation: &AtomicBool) -> Result<u64, ArchiveError> {
    scan_archive_with_hook(path, cancellation, || {})
}

fn verify_open_bytes(
    file: &mut File,
    descriptor: &RuntimeDescriptor,
    cancellation: &AtomicBool,
) -> Result<(), ArchiveError> {
    check_cancelled(cancellation)?;
    if file.metadata()?.len() != descriptor.size_bytes {
        return Err(ArchiveError::SizeMismatch);
    }
    file.seek(SeekFrom::Start(0))?;
    let mut digest = Sha256::new();
    let mut read_bytes = 0_u64;
    // 堆分配缓冲：1MB 栈数组在受限线程（tokio worker 2MB 栈）上会溢出
    let mut buffer = vec![0_u8; 1024 * 1024];
    loop {
        check_cancelled(cancellation)?;
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        read_bytes = read_bytes
            .checked_add(read as u64)
            .ok_or(ArchiveError::SizeMismatch)?;
        if read_bytes > descriptor.size_bytes {
            return Err(ArchiveError::SizeMismatch);
        }
        digest.update(&buffer[..read]);
    }
    if read_bytes != descriptor.size_bytes {
        return Err(ArchiveError::SizeMismatch);
    }
    if hex::encode(digest.finalize()) != descriptor.sha256 {
        return Err(ArchiveError::HashMismatch);
    }
    Ok(())
}

pub fn verify_bytes(
    path: &Path,
    descriptor: &RuntimeDescriptor,
    cancellation: &AtomicBool,
) -> Result<(), ArchiveError> {
    let mut file = open_archive_file(path)?;
    verify_open_bytes(&mut file, descriptor, cancellation)
}

fn extraction_record<R: Read>(entry: &mut tar::Entry<'_, R>) -> Result<LayoutRecord, ArchiveError> {
    let raw_path = entry.path_bytes().into_owned();
    let entry_type = entry.header().entry_type();
    let normalized_raw = if entry_type.is_dir() && raw_path.ends_with(b"/") {
        if raw_path.ends_with(b"//") {
            return Err(ArchiveError::UnsafePath("directory_trailing_slash".into()));
        }
        &raw_path[..raw_path.len() - 1]
    } else {
        raw_path.as_slice()
    };
    let path = canonical_posix_path(normalized_raw)?;
    let mode = entry.header().mode().map_err(stream_error)? & 0o7777;
    let size = entry.header().size().map_err(stream_error)?;
    if entry_type.is_file() {
        Ok(LayoutRecord::regular(&path, size, mode))
    } else if entry_type.is_dir() {
        Ok(LayoutRecord::directory(&path, mode))
    } else if entry_type.is_symlink() {
        let link = entry
            .link_name_bytes()
            .ok_or_else(|| ArchiveError::UnsafeEntry(format!("symlink:{path}")))?;
        let target = str::from_utf8(&link)
            .map_err(|_| ArchiveError::UnsafePath(format!("link_non_utf8:{path}")))?;
        Ok(LayoutRecord::symlink(&path, target, mode))
    } else {
        Err(ArchiveError::UnsafeEntry(format!("type:{path}")))
    }
}

#[cfg(unix)]
fn open_child_directory(parent: &File, name: &str) -> Result<File, ArchiveError> {
    use rustix::fs::{openat, Mode, OFlags};

    let fd = openat(
        parent,
        name,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC | OFlags::NONBLOCK,
        Mode::empty(),
    )
    .map_err(io::Error::from)?;
    let directory = File::from(fd);
    if !directory.metadata()?.is_dir() {
        return Err(ArchiveError::UnsafePath(name.into()));
    }
    Ok(directory)
}

/// Windows 版 `open_child_directory`：拒绝符号链接后返回子目录路径。
#[cfg(windows)]
fn open_child_directory(parent: &Path, name: &str) -> Result<DirectoryRef, ArchiveError> {
    let child = parent.join(name);
    let metadata = std::fs::symlink_metadata(&child)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(ArchiveError::UnsafePath(name.into()));
    }
    Ok(child)
}

#[cfg(unix)]
fn open_relative_parent(root: &File, path: &str) -> Result<(File, String), ArchiveError> {
    canonical_posix_path(path.as_bytes())?;
    let (parent_path, name) = path.rsplit_once('/').unwrap_or(("", path));
    let mut parent = File::from(rustix::io::dup(root).map_err(io::Error::from)?);
    if !parent_path.is_empty() {
        for component in parent_path.split('/') {
            parent = open_child_directory(&parent, component)?;
        }
    }
    Ok((parent, name.to_owned()))
}

#[cfg(unix)]
fn same_stat_identity(left: &rustix::fs::Stat, right: &rustix::fs::Stat) -> bool {
    left.st_dev == right.st_dev && left.st_ino == right.st_ino
}

/// 拒绝 Windows 驱动器号形式的相对路径（如 "C:foo"）：PathBuf::push
/// 遇到带前缀的组件会替换整个路径，必须在此挡下。
#[cfg(windows)]
fn reject_drive_prefix(path: &str) -> Result<(), ArchiveError> {
    if path.split('/').any(|part| {
        part.len() == 2
            && part.as_bytes()[1] == b':'
            && part.as_bytes()[0].is_ascii_alphabetic()
    }) {
        return Err(ArchiveError::UnsafePath(format!("drive:{path}")));
    }
    Ok(())
}

#[cfg(unix)]
fn extract_record_at<R: Read>(
    entry: &mut tar::Entry<'_, R>,
    record: &LayoutRecord,
    destination: &File,
    cancellation: &AtomicBool,
) -> Result<(), ArchiveError> {
    use rustix::fs::{
        chmodat, fchmod, fstat, mkdirat, openat, readlinkat, statat, symlinkat, AtFlags, FileType,
        Mode, OFlags,
    };

    let (parent, name) = open_relative_parent(destination, &record.path)?;
    match &record.kind {
        LayoutKind::Directory => {
            mkdirat(&parent, name.as_str(), Mode::from_raw_mode(0o700)).map_err(io::Error::from)?;
            let directory = open_child_directory(&parent, &name)?;
            fchmod(&directory, Mode::from_raw_mode(record.mode as _)).map_err(io::Error::from)?;
            let opened = fstat(&directory).map_err(io::Error::from)?;
            let visible = statat(&parent, name.as_str(), AtFlags::SYMLINK_NOFOLLOW)
                .map_err(io::Error::from)?;
            if !same_stat_identity(&opened, &visible)
                || FileType::from_raw_mode(visible.st_mode) != FileType::Directory
            {
                return Err(ArchiveError::UnsafeEntry(format!(
                    "changed:{}",
                    record.path
                )));
            }
        }
        LayoutKind::Regular { size } => {
            let fd = openat(
                &parent,
                name.as_str(),
                OFlags::WRONLY
                    | OFlags::CREATE
                    | OFlags::EXCL
                    | OFlags::NOFOLLOW
                    | OFlags::CLOEXEC
                    | OFlags::NONBLOCK,
                Mode::from_raw_mode(0o600),
            )
            .map_err(io::Error::from)?;
            let mut output = File::from(fd);
            let mut remaining = *size;
            let mut buffer = [0_u8; 1024 * 1024];
            while remaining != 0 {
                check_cancelled(cancellation)?;
                let maximum = usize::try_from(remaining.min(buffer.len() as u64))
                    .map_err(|_| ArchiveError::Limit(format!("file_size:{}", record.path)))?;
                let read = entry.read(&mut buffer[..maximum]).map_err(stream_error)?;
                if read == 0 {
                    return Err(ArchiveError::UnsafeEntry(format!(
                        "truncated:{}",
                        record.path
                    )));
                }
                output.write_all(&buffer[..read])?;
                remaining -= read as u64;
            }
            if entry.read(&mut [0_u8; 1]).map_err(stream_error)? != 0 {
                return Err(ArchiveError::UnsafeEntry(format!(
                    "oversized:{}",
                    record.path
                )));
            }
            fchmod(&output, Mode::from_raw_mode(record.mode as _)).map_err(io::Error::from)?;
            let opened = fstat(&output).map_err(io::Error::from)?;
            let visible = statat(&parent, name.as_str(), AtFlags::SYMLINK_NOFOLLOW)
                .map_err(io::Error::from)?;
            if !same_stat_identity(&opened, &visible)
                || FileType::from_raw_mode(visible.st_mode) != FileType::RegularFile
                || visible.st_size < 0
                || visible.st_size as u64 != *size
            {
                return Err(ArchiveError::UnsafeEntry(format!(
                    "changed:{}",
                    record.path
                )));
            }
        }
        LayoutKind::Symlink { target } => {
            symlinkat(target.as_str(), &parent, name.as_str()).map_err(io::Error::from)?;
            chmodat(
                &parent,
                name.as_str(),
                Mode::from_raw_mode(record.mode as _),
                AtFlags::SYMLINK_NOFOLLOW,
            )
            .map_err(io::Error::from)?;
            let visible = statat(&parent, name.as_str(), AtFlags::SYMLINK_NOFOLLOW)
                .map_err(io::Error::from)?;
            let actual = readlinkat(&parent, name.as_str(), Vec::new()).map_err(io::Error::from)?;
            if FileType::from_raw_mode(visible.st_mode) != FileType::Symlink
                || actual.as_bytes() != target.as_bytes()
            {
                return Err(ArchiveError::UnsafeEntry(format!(
                    "changed:{}",
                    record.path
                )));
            }
        }
    }
    Ok(())
}

/// Windows 版 `extract_record_at`：不使用 fd-at 锚定（std 无目录句柄），
/// 路径安全性由 scan 阶段的 canonical_posix_path + 驱动器号检查保证；
/// 不设置 unix mode（树哈希校验端使用与构建工具一致的规范化 mode）。
/// 文件内容在写入目标的同时喂入树哈希 digest。
#[cfg(windows)]
fn extract_record_at<R: Read>(
    entry: &mut tar::Entry<'_, R>,
    record: &LayoutRecord,
    destination: &Path,
    cancellation: &AtomicBool,
    digest: &mut Sha256,
) -> Result<(), ArchiveError> {
    reject_drive_prefix(&record.path)?;
    // 正斜杠相对路径可直接 join（Windows 同时接受 / 与 \）。
    let target = destination.join(&record.path);
    match &record.kind {
        LayoutKind::Directory => {
            if let Err(error) = std::fs::create_dir(&target) {
                return Err(ArchiveError::Io(error));
            }
        }
        LayoutKind::Regular { size } => {
            let mut output = OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&target)?;
            let mut remaining = *size;
            // 堆分配缓冲：1MB 栈数组在受限线程（tokio worker 2MB 栈）上会溢出
            let mut buffer = vec![0_u8; 1024 * 1024];
            while remaining != 0 {
                check_cancelled(cancellation)?;
                let maximum = usize::try_from(remaining.min(buffer.len() as u64))
                    .map_err(|_| ArchiveError::Limit(format!("file_size:{}", record.path)))?;
                let read = entry.read(&mut buffer[..maximum]).map_err(stream_error)?;
                if read == 0 {
                    return Err(ArchiveError::UnsafeEntry(format!(
                        "truncated:{}",
                        record.path
                    )));
                }
                output.write_all(&buffer[..read])?;
                digest.update(&buffer[..read]);
                remaining -= read as u64;
            }
            if entry.read(&mut [0_u8; 1]).map_err(stream_error)? != 0 {
                return Err(ArchiveError::UnsafeEntry(format!(
                    "oversized:{}",
                    record.path
                )));
            }
            output.sync_all()?;
            if output.metadata()?.len() != *size {
                return Err(ArchiveError::UnsafeEntry(format!(
                    "changed:{}",
                    record.path
                )));
            }
        }
        LayoutKind::Symlink { .. } => {
            // Windows 运行时包不包含符号链接（构建脚本仅写入文件/目录）；
            // 创建符号链接还需要特权，因此直接拒绝。
            return Err(ArchiveError::UnsafeEntry(format!(
                "symlink_unsupported:{}",
                record.path
            )));
        }
    }
    Ok(())
}

#[cfg(unix)]
fn extract_open_archive(
    file: &mut File,
    compressed_size: u64,
    scan: &ScanResult,
    destination: &File,
    cancellation: &AtomicBool,
    limits: ArchiveLimits,
) -> Result<(), ArchiveError> {
    file.seek(SeekFrom::Start(0))?;
    let maximum = compressed_size
        .checked_mul(limits.expansion_ratio)
        .and_then(|value| value.checked_add(limits.expansion_allowance))
        .ok_or_else(|| ArchiveError::Limit("expansion_ratio".into()))?;
    let decoder = zstd::stream::read::Decoder::new(file).map_err(stream_error)?;
    let reader = CancellableReader::new(decoder, cancellation, maximum);
    let mut archive = tar::Archive::new(reader);
    let mut extracted = HashSet::new();
    {
        let entries = archive.entries().map_err(stream_error)?;
        for item in entries {
            check_cancelled(cancellation)?;
            let mut entry = item.map_err(stream_error)?;
            let record = extraction_record(&mut entry)?;
            let expected = scan
                .layout
                .entries
                .get(&record.path)
                .ok_or_else(|| ArchiveError::UnsafeEntry(format!("changed:{}", record.path)))?;
            if expected != &record || !extracted.insert(record.path.clone()) {
                return Err(ArchiveError::UnsafeEntry(format!(
                    "changed:{}",
                    record.path
                )));
            }
            extract_record_at(&mut entry, &record, destination, cancellation)?;
        }
    }
    let mut reader = archive.into_inner();
    io::copy(&mut reader, &mut io::sink()).map_err(stream_error)?;
    if extracted.len() != scan.entry_count {
        return Err(ArchiveError::UnsafeEntry("entry_count_changed".into()));
    }
    Ok(())
}

/// Windows 版 `extract_open_archive`：解压循环与 Unix 相同，但树哈希在
/// 写入时同步累计——kind/mode/length 取自 tar 条目（'D' 目录 / 'F' 文件），
/// 文件内容边写边喂入 digest。返回最终树哈希（hex）。
#[cfg(windows)]
fn extract_open_archive(
    file: &mut File,
    compressed_size: u64,
    scan: &ScanResult,
    destination: &Path,
    cancellation: &AtomicBool,
    limits: ArchiveLimits,
) -> Result<String, ArchiveError> {
    file.seek(SeekFrom::Start(0))?;
    let maximum = compressed_size
        .checked_mul(limits.expansion_ratio)
        .and_then(|value| value.checked_add(limits.expansion_allowance))
        .ok_or_else(|| ArchiveError::Limit("expansion_ratio".into()))?;
    let decoder = zstd::stream::read::Decoder::new(file).map_err(stream_error)?;
    let reader = CancellableReader::new(decoder, cancellation, maximum);
    let mut archive = tar::Archive::new(reader);
    let mut extracted = HashSet::new();
    let mut digest = Sha256::new();
    {
        let entries = archive.entries().map_err(stream_error)?;
        for item in entries {
            check_cancelled(cancellation)?;
            let mut entry = item.map_err(stream_error)?;
            let record = extraction_record(&mut entry)?;
            let expected = scan
                .layout
                .entries
                .get(&record.path)
                .ok_or_else(|| ArchiveError::UnsafeEntry(format!("changed:{}", record.path)))?;
            if expected != &record || !extracted.insert(record.path.clone()) {
                return Err(ArchiveError::UnsafeEntry(format!(
                    "changed:{}",
                    record.path
                )));
            }
            let relative = record.path.as_bytes();
            let (kind, payload_length) = match &record.kind {
                LayoutKind::Symlink { target } => (b'L', target.len() as u64),
                LayoutKind::Directory => (b'D', 0),
                LayoutKind::Regular { size } => (b'F', *size),
            };
            digest.update([kind]);
            hash_length(&mut digest, relative.len() as u64);
            digest.update(relative);
            digest.update(record.mode.to_le_bytes());
            hash_length(&mut digest, payload_length);
            match &record.kind {
                // 目录在 extract_record_at 内创建；文件内容写入时喂入 digest。
                LayoutKind::Symlink { target } => digest.update(target.as_bytes()),
                _ => {
                    extract_record_at(&mut entry, &record, destination, cancellation, &mut digest)?;
                }
            }
        }
    }
    let mut reader = archive.into_inner();
    io::copy(&mut reader, &mut io::sink()).map_err(stream_error)?;
    if extracted.len() != scan.entry_count {
        return Err(ArchiveError::UnsafeEntry("entry_count_changed".into()));
    }
    Ok(hex::encode(digest.finalize()))
}

fn validate_single_name(name: &str) -> Result<(), ArchiveError> {
    if canonical_posix_path(name.as_bytes())? != name || name.contains('/') {
        return Err(ArchiveError::UnsafePath(name.into()));
    }
    Ok(())
}

#[cfg(unix)]
fn remove_directory_contents_at(directory: &File, depth: usize) -> Result<(), ArchiveError> {
    use rustix::fs::{fstat, openat, statat, unlinkat, AtFlags, Dir, FileType, Mode, OFlags};

    if depth > MAX_TREE_DEPTH {
        return Err(ArchiveError::Limit("cleanup_depth".into()));
    }
    let mut names = Vec::new();
    for item in Dir::read_from(directory).map_err(io::Error::from)? {
        let item = item.map_err(io::Error::from)?;
        if !matches!(item.file_name().to_bytes(), b"." | b"..") {
            names.push(item.file_name().to_owned());
        }
    }
    for name in names {
        let before =
            statat(directory, &name, AtFlags::SYMLINK_NOFOLLOW).map_err(io::Error::from)?;
        if FileType::from_raw_mode(before.st_mode) == FileType::Directory {
            let child_fd = openat(
                directory,
                &name,
                OFlags::RDONLY
                    | OFlags::DIRECTORY
                    | OFlags::NOFOLLOW
                    | OFlags::CLOEXEC
                    | OFlags::NONBLOCK,
                Mode::empty(),
            )
            .map_err(io::Error::from)?;
            let child = File::from(child_fd);
            let opened = fstat(&child).map_err(io::Error::from)?;
            if !same_stat_identity(&before, &opened) {
                return Err(ArchiveError::UnsafeEntry("cleanup_changed".into()));
            }
            remove_directory_contents_at(&child, depth + 1)?;
            let after =
                statat(directory, &name, AtFlags::SYMLINK_NOFOLLOW).map_err(io::Error::from)?;
            if !same_stat_identity(&opened, &after)
                || FileType::from_raw_mode(after.st_mode) != FileType::Directory
            {
                return Err(ArchiveError::UnsafeEntry("cleanup_changed".into()));
            }
            unlinkat(directory, &name, AtFlags::REMOVEDIR).map_err(io::Error::from)?;
        } else {
            unlinkat(directory, &name, AtFlags::empty()).map_err(io::Error::from)?;
        }
    }
    Ok(())
}

#[cfg(unix)]
fn cleanup_destination_at(
    trusted_parent: &File,
    destination_name: &str,
    destination: &File,
    expected_identity: &rustix::fs::Stat,
) -> Result<(), ArchiveError> {
    use rustix::fs::{fstat, statat, unlinkat, AtFlags, FileType};

    let opened = fstat(destination).map_err(io::Error::from)?;
    let visible = statat(trusted_parent, destination_name, AtFlags::SYMLINK_NOFOLLOW)
        .map_err(io::Error::from)?;
    if !same_stat_identity(expected_identity, &opened)
        || !same_stat_identity(&opened, &visible)
        || FileType::from_raw_mode(visible.st_mode) != FileType::Directory
    {
        return Err(ArchiveError::UnsafeEntry(
            "cleanup_destination_changed".into(),
        ));
    }
    remove_directory_contents_at(destination, 0)?;
    let after = statat(trusted_parent, destination_name, AtFlags::SYMLINK_NOFOLLOW)
        .map_err(io::Error::from)?;
    if !same_stat_identity(&opened, &after)
        || FileType::from_raw_mode(after.st_mode) != FileType::Directory
    {
        return Err(ArchiveError::UnsafeEntry(
            "cleanup_destination_changed".into(),
        ));
    }
    unlinkat(trusted_parent, destination_name, AtFlags::REMOVEDIR).map_err(io::Error::from)?;
    Ok(())
}

#[cfg(unix)]
fn setup_failure_with_empty_cleanup(
    trusted_parent: &File,
    destination_name: &str,
    primary: ArchiveError,
) -> ArchiveError {
    match rustix::fs::unlinkat(
        trusted_parent,
        destination_name,
        rustix::fs::AtFlags::REMOVEDIR,
    ) {
        Ok(()) => primary,
        Err(cleanup) => ArchiveError::Cleanup {
            primary: Box::new(primary),
            cleanup: io::Error::from(cleanup).to_string(),
        },
    }
}

/// Windows 版递归清空目录（无 inode 锚定，直接按 readdir 逐项删除）。
#[cfg(windows)]
fn remove_directory_contents_at(directory: &Path, depth: usize) -> Result<(), ArchiveError> {
    if depth > MAX_TREE_DEPTH {
        return Err(ArchiveError::Limit("cleanup_depth".into()));
    }
    for item in std::fs::read_dir(directory)? {
        let item = item?;
        let full = directory.join(item.file_name());
        // DirEntry::file_type 不跟随符号链接（删除链接本身）。
        if item.file_type()?.is_dir() {
            remove_directory_contents_at(&full, depth + 1)?;
            std::fs::remove_dir(&full)?;
        } else {
            std::fs::remove_file(&full)?;
        }
    }
    Ok(())
}

/// Windows 版 `cleanup_destination_at`：解压失败后递归移除 staging 目录。
#[cfg(windows)]
fn cleanup_destination_at(destination: &Path) -> Result<(), ArchiveError> {
    remove_directory_contents_at(destination, 0)?;
    std::fs::remove_dir(destination)?;
    Ok(())
}

/// Windows 版 `setup_failure_with_empty_cleanup`：staging 目录创建后、
/// 写入前的失败按空目录回收。
#[cfg(windows)]
fn setup_failure_with_empty_cleanup(
    primary: ArchiveError,
    destination: &Path,
) -> ArchiveError {
    match std::fs::remove_dir(destination) {
        Ok(()) => primary,
        Err(cleanup) => ArchiveError::Cleanup {
            primary: Box::new(primary),
            cleanup: cleanup.to_string(),
        },
    }
}

#[cfg(unix)]
fn extract_verified_at_with_hooks<F: FnOnce(), G: FnOnce()>(
    path: &Path,
    descriptor: &RuntimeDescriptor,
    trusted_parent: &File,
    destination_name: &str,
    cancellation: &AtomicBool,
    after_verify: F,
    after_destination_open: G,
) -> Result<(), ArchiveError> {
    use rustix::fs::{fchmod, fstat, fstatvfs, mkdirat, openat, Mode, OFlags};

    let limits = ArchiveLimits::default();
    validate_single_name(destination_name)?;
    if !trusted_parent.metadata()?.is_dir() {
        return Err(ArchiveError::UnsafePath(destination_name.into()));
    }
    let mut file = create_verified_snapshot(trusted_parent, path, descriptor, cancellation)?;
    after_verify();
    let scan = scan_open_archive(&mut file, descriptor.size_bytes, cancellation, limits)?;
    for required in &descriptor.required_files {
        canonical_posix_path(required.as_bytes())?;
        scan.layout
            .resolve_regular(required)
            .map_err(|_| ArchiveError::RequiredFile(required.clone()))?;
    }
    check_cancelled(cancellation)?;
    let entry_overhead = (scan.entry_count as u64)
        .checked_mul(4096)
        .ok_or(ArchiveError::DiskSpace)?;
    let required_space = scan
        .unpacked_bytes
        .checked_add(entry_overhead)
        .and_then(|value| value.checked_add(EXPANSION_ALLOWANCE))
        .ok_or(ArchiveError::DiskSpace)?;
    let filesystem = fstatvfs(trusted_parent).map_err(io::Error::from)?;
    let available_space = filesystem
        .f_bavail
        .checked_mul(filesystem.f_frsize)
        .ok_or(ArchiveError::DiskSpace)?;
    if available_space < required_space {
        return Err(ArchiveError::DiskSpace);
    }
    mkdirat(trusted_parent, destination_name, Mode::from_raw_mode(0o700))
        .map_err(io::Error::from)?;
    let root_fd = match openat(
        trusted_parent,
        destination_name,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC | OFlags::NONBLOCK,
        Mode::empty(),
    ) {
        Ok(fd) => fd,
        Err(error) => {
            return Err(setup_failure_with_empty_cleanup(
                trusted_parent,
                destination_name,
                ArchiveError::Io(io::Error::from(error)),
            ));
        }
    };
    let destination = File::from(root_fd);
    let destination_identity = match fstat(&destination) {
        Ok(identity) => identity,
        Err(error) => {
            return Err(setup_failure_with_empty_cleanup(
                trusted_parent,
                destination_name,
                ArchiveError::Io(io::Error::from(error)),
            ));
        }
    };
    let result = (|| {
        fchmod(&destination, Mode::from_raw_mode(0o700)).map_err(io::Error::from)?;
        after_destination_open();
        extract_open_archive(
            &mut file,
            descriptor.size_bytes,
            &scan,
            &destination,
            cancellation,
            limits,
        )?;
        verify_extracted_layout(&destination, &scan.layout, cancellation)?;
        validate_runtime_tree_at(&destination, &descriptor.required_files, cancellation)?;
        if runtime_tree_sha256_at(&destination, cancellation)? != descriptor.tree_sha256 {
            return Err(ArchiveError::HashMismatch);
        }
        let visible = open_child_directory(trusted_parent, destination_name)?;
        let visible_identity = fstat(&visible).map_err(io::Error::from)?;
        if !same_stat_identity(&destination_identity, &visible_identity) {
            return Err(ArchiveError::UnsafeEntry(format!(
                "destination_replaced:{destination_name}"
            )));
        }
        Ok(())
    })();
    match result {
        Ok(()) => Ok(()),
        Err(primary) => match cleanup_destination_at(
            trusted_parent,
            destination_name,
            &destination,
            &destination_identity,
        ) {
            Ok(()) => Err(primary),
            Err(cleanup) => Err(ArchiveError::Cleanup {
                primary: Box::new(primary),
                cleanup: cleanup.to_string(),
            }),
        },
    }
}

#[cfg(unix)]
pub fn extract_verified_at(
    path: &Path,
    descriptor: &RuntimeDescriptor,
    trusted_parent: &File,
    destination_name: &str,
    cancellation: &AtomicBool,
) -> Result<(), ArchiveError> {
    extract_verified_at_with_hooks(
        path,
        descriptor,
        trusted_parent,
        destination_name,
        cancellation,
        || {},
        || {},
    )
}

/// Windows 版 `extract_verified_at`：trusted_parent 为目录路径。
#[cfg(windows)]
pub fn extract_verified_at(
    path: &Path,
    descriptor: &RuntimeDescriptor,
    trusted_parent: &Path,
    destination_name: &str,
    cancellation: &AtomicBool,
) -> Result<(), ArchiveError> {
    extract_verified_at_with_hooks(
        path,
        descriptor,
        trusted_parent,
        destination_name,
        cancellation,
        || {},
        || {},
    )
}

/// Windows 版验证解压：与 Unix 流程同序——
/// 1. 先校验包级 sha256/size（写入私有快照后基于快照解压）；
/// 2. scan 预检 + required_files 图解析；
/// 3. 创建 staging 目录，边解压边计算树哈希（mode 取自 tar 头）；
/// 4. 树哈希与 descriptor.tree_sha256 比对，失配则清空 staging 并报
///    HashMismatch。
/// Windows 没有 fd 锚定 / fstatvfs，安全性依赖“唯一临时名 + rename”与
/// 上述哈希校验。
#[cfg(windows)]
fn extract_verified_at_with_hooks<F: FnOnce(), G: FnOnce()>(
    path: &Path,
    descriptor: &RuntimeDescriptor,
    trusted_parent: &Path,
    destination_name: &str,
    cancellation: &AtomicBool,
    after_verify: F,
    after_destination_open: G,
) -> Result<(), ArchiveError> {
    let limits = ArchiveLimits::default();
    validate_single_name(destination_name)?;
    reject_drive_prefix(destination_name)?;
    let parent_metadata = std::fs::symlink_metadata(trusted_parent)?;
    if parent_metadata.file_type().is_symlink() || !parent_metadata.is_dir() {
        return Err(ArchiveError::UnsafePath(destination_name.into()));
    }
    let mut file = create_verified_snapshot(trusted_parent, path, descriptor, cancellation)?;
    after_verify();
    let scan = scan_open_archive(&mut file, descriptor.size_bytes, cancellation, limits)?;
    for required in &descriptor.required_files {
        let required = required.as_str();
        canonical_posix_path(required.as_bytes())?;
        reject_drive_prefix(required)?;
        scan.layout
            .resolve_regular(required)
            .map_err(|_| ArchiveError::RequiredFile(required.to_owned()))?;
    }
    check_cancelled(cancellation)?;
    let destination = trusted_parent.join(destination_name);
    if let Err(error) = std::fs::create_dir(&destination) {
        return Err(ArchiveError::Io(error));
    }
    let result = (|| {
        after_destination_open();
        let tree_sha = extract_open_archive(
            &mut file,
            descriptor.size_bytes,
            &scan,
            &destination,
            cancellation,
            limits,
        )?;
        if tree_sha != descriptor.tree_sha256 {
            return Err(ArchiveError::HashMismatch);
        }
        verify_extracted_layout(&destination, &scan.layout, cancellation)?;
        validate_runtime_tree_at(&destination, &descriptor.required_files, cancellation)?;
        Ok(())
    })();
    match result {
        Ok(()) => Ok(()),
        Err(primary) => match cleanup_destination_at(&destination) {
            Ok(()) => Err(primary),
            Err(cleanup) => Err(ArchiveError::Cleanup {
                primary: Box::new(primary),
                cleanup: cleanup.to_string(),
            }),
        },
    }
}

#[cfg(unix)]
fn open_absolute_directory_nofollow(path: &Path) -> Result<File, ArchiveError> {
    use std::path::Component;

    if !path.is_absolute() {
        return Err(ArchiveError::UnsafePath(path.display().to_string()));
    }
    let mut directory = open_directory_file(Path::new("/"))?;
    for component in path.components() {
        match component {
            Component::RootDir => {}
            Component::Normal(name) => {
                let name = str::from_utf8(name.as_bytes())
                    .map_err(|_| ArchiveError::UnsafePath("non_utf8".into()))?;
                directory = open_child_directory(&directory, name)?;
            }
            _ => return Err(ArchiveError::UnsafePath(path.display().to_string())),
        }
    }
    Ok(directory)
}

/// Windows 版 `open_absolute_directory_nofollow`：逐组件校验路径上没有
/// 符号链接（reparse point），返回规范化路径作为锚定引用。
#[cfg(windows)]
fn open_absolute_directory_nofollow(path: &Path) -> Result<DirectoryRef, ArchiveError> {
    use std::path::Component;

    if !path.is_absolute() {
        return Err(ArchiveError::UnsafePath(path.display().to_string()));
    }
    let mut directory = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Prefix(_) | Component::RootDir => {
                directory.push(component.as_os_str());
            }
            Component::Normal(name) => {
                let name = name
                    .to_str()
                    .ok_or_else(|| ArchiveError::UnsafePath("non_utf8".into()))?;
                directory.push(name);
                let metadata = std::fs::symlink_metadata(&directory)?;
                if metadata.file_type().is_symlink() || !metadata.is_dir() {
                    return Err(ArchiveError::UnsafePath(path.display().to_string()));
                }
            }
            _ => return Err(ArchiveError::UnsafePath(path.display().to_string())),
        }
    }
    Ok(directory)
}

#[cfg(test)]
fn extract_verified_with_hook<F: FnOnce()>(
    path: &Path,
    descriptor: &RuntimeDescriptor,
    destination: &Path,
    cancellation: &AtomicBool,
    after_verify: F,
) -> Result<(), ArchiveError> {
    let parent = destination
        .parent()
        .ok_or_else(|| ArchiveError::UnsafePath(destination.display().to_string()))?;
    let name = destination
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or_else(|| ArchiveError::UnsafePath(destination.display().to_string()))?;
    let trusted_parent = open_absolute_directory_nofollow(parent)?;
    extract_verified_at_with_hooks(
        path,
        descriptor,
        &trusted_parent,
        name,
        cancellation,
        after_verify,
        || {},
    )
}

#[cfg(test)]
fn extract_verified(
    path: &Path,
    descriptor: &RuntimeDescriptor,
    destination: &Path,
    cancellation: &AtomicBool,
) -> Result<(), ArchiveError> {
    extract_verified_with_hook(path, descriptor, destination, cancellation, || {})
}

#[cfg(unix)]
#[derive(Clone, Debug)]
struct AnchoredEntry {
    record: LayoutRecord,
    device: u64,
    inode: u64,
}

#[cfg(unix)]
fn enumerate_directory_at(
    directory: &File,
    prefix: &str,
    depth: usize,
    ignore_install_marker: bool,
    cancellation: &AtomicBool,
    entries: &mut BTreeMap<String, AnchoredEntry>,
) -> Result<(), ArchiveError> {
    use rustix::fs::{fstat, openat, readlinkat, statat, AtFlags, Dir, FileType, Mode, OFlags};

    if depth > MAX_TREE_DEPTH {
        return Err(ArchiveError::Limit("tree_depth".into()));
    }
    let iterator = Dir::read_from(directory).map_err(io::Error::from)?;
    for item in iterator {
        check_cancelled(cancellation)?;
        let item = item.map_err(io::Error::from)?;
        let raw_name = item.file_name().to_bytes();
        if matches!(raw_name, b"." | b"..") {
            continue;
        }
        let name = str::from_utf8(raw_name)
            .map_err(|_| ArchiveError::UnsafePath("tree_non_utf8".into()))?;
        validate_single_name(name)?;
        let path = if prefix.is_empty() {
            name.to_owned()
        } else {
            format!("{prefix}/{name}")
        };
        if ignore_install_marker
            && matches!(path.as_str(), RUNTIME_MARKER_NAME | RUNTIME_PROVENANCE_NAME)
        {
            continue;
        }
        if entries.len() >= MAX_ENTRIES || path.len() > MAX_PATH_BYTES {
            return Err(ArchiveError::Limit("tree_entries".into()));
        }
        let before = statat(directory, item.file_name(), AtFlags::SYMLINK_NOFOLLOW)
            .map_err(io::Error::from)?;
        let file_type = FileType::from_raw_mode(before.st_mode);
        let mode = (before.st_mode as u32) & 0o7777;
        let record = match file_type {
            FileType::Directory => {
                let fd = openat(
                    directory,
                    item.file_name(),
                    OFlags::RDONLY
                        | OFlags::DIRECTORY
                        | OFlags::NOFOLLOW
                        | OFlags::CLOEXEC
                        | OFlags::NONBLOCK,
                    Mode::empty(),
                )
                .map_err(io::Error::from)?;
                let child = File::from(fd);
                let opened = fstat(&child).map_err(io::Error::from)?;
                if !same_stat_identity(&before, &opened) {
                    return Err(ArchiveError::UnsafeEntry(format!("tree_changed:{path}")));
                }
                enumerate_directory_at(
                    &child,
                    &path,
                    depth + 1,
                    ignore_install_marker,
                    cancellation,
                    entries,
                )?;
                let after = statat(directory, item.file_name(), AtFlags::SYMLINK_NOFOLLOW)
                    .map_err(io::Error::from)?;
                if !same_stat_identity(&opened, &after)
                    || FileType::from_raw_mode(after.st_mode) != FileType::Directory
                {
                    return Err(ArchiveError::UnsafeEntry(format!("tree_changed:{path}")));
                }
                LayoutRecord::directory(&path, mode)
            }
            FileType::RegularFile => {
                if before.st_size < 0 {
                    return Err(ArchiveError::UnsafeEntry(format!("tree_size:{path}")));
                }
                LayoutRecord::regular(&path, before.st_size as u64, mode)
            }
            FileType::Symlink => {
                let target =
                    readlinkat(directory, item.file_name(), Vec::new()).map_err(io::Error::from)?;
                let target = str::from_utf8(target.as_bytes())
                    .map_err(|_| ArchiveError::UnsafePath(format!("tree_link_non_utf8:{path}")))?;
                let after = statat(directory, item.file_name(), AtFlags::SYMLINK_NOFOLLOW)
                    .map_err(io::Error::from)?;
                if !same_stat_identity(&before, &after)
                    || FileType::from_raw_mode(after.st_mode) != FileType::Symlink
                {
                    return Err(ArchiveError::UnsafeEntry(format!("tree_changed:{path}")));
                }
                LayoutRecord::symlink(&path, target, mode)
            }
            _ => return Err(ArchiveError::UnsafeEntry(format!("tree_type:{path}"))),
        };
        let anchored = AnchoredEntry {
            record,
            device: before.st_dev as u64,
            inode: before.st_ino as u64,
        };
        if entries.insert(path.clone(), anchored).is_some() {
            return Err(ArchiveError::UnsafeEntry(format!("tree_duplicate:{path}")));
        }
    }
    Ok(())
}

#[cfg(unix)]
fn enumerate_tree_at(
    root: &File,
    ignore_install_marker: bool,
    cancellation: &AtomicBool,
) -> Result<BTreeMap<String, AnchoredEntry>, ArchiveError> {
    if !root.metadata()?.is_dir() {
        return Err(ArchiveError::UnsafePath("runtime_root".into()));
    }
    let mut entries = BTreeMap::new();
    enumerate_directory_at(
        root,
        "",
        0,
        ignore_install_marker,
        cancellation,
        &mut entries,
    )?;
    Ok(entries)
}

/// Windows 树哈希 mode 契约（与 scripts/build_windows_runtime.py 的
/// EXECUTABLE_SUFFIXES 规则一致）：目录 0o755；文件名（小写）以
/// .exe/.dll/.bat/.cmd/.ps1 结尾 0o755；其余文件 0o644。
/// 提取阶段忽略 tar mode，因此校验端必须使用同一规范化规则。
#[cfg(windows)]
fn canonical_windows_mode(relative_path: &str, is_directory: bool) -> u32 {
    if is_directory {
        return 0o755;
    }
    let lower = relative_path.to_ascii_lowercase();
    if [".exe", ".dll", ".bat", ".cmd", ".ps1"]
        .iter()
        .any(|suffix| lower.ends_with(suffix))
    {
        0o755
    } else {
        0o644
    }
}

/// Windows 目录枚举：std::fs 递归遍历，按相对路径字节序（正斜杠）排序
/// 写入 BTreeMap；mode 取自上面的规范化契约。
#[cfg(windows)]
fn enumerate_directory_windows(
    directory: &Path,
    prefix: &str,
    depth: usize,
    ignore_install_marker: bool,
    cancellation: &AtomicBool,
    entries: &mut BTreeMap<String, LayoutRecord>,
) -> Result<(), ArchiveError> {
    if depth > MAX_TREE_DEPTH {
        return Err(ArchiveError::Limit("tree_depth".into()));
    }
    let mut names = Vec::new();
    for item in std::fs::read_dir(directory)? {
        check_cancelled(cancellation)?;
        let item = item?;
        let name = item
            .file_name()
            .into_string()
            .map_err(|_| ArchiveError::UnsafePath("tree_non_utf8".into()))?;
        validate_single_name(&name)?;
        names.push(name);
    }
    names.sort();
    for name in names {
        check_cancelled(cancellation)?;
        let path = if prefix.is_empty() {
            name.clone()
        } else {
            format!("{prefix}/{name}")
        };
        if ignore_install_marker
            && matches!(path.as_str(), RUNTIME_MARKER_NAME | RUNTIME_PROVENANCE_NAME)
        {
            continue;
        }
        if entries.len() >= MAX_ENTRIES || path.len() > MAX_PATH_BYTES {
            return Err(ArchiveError::Limit("tree_entries".into()));
        }
        let full = directory.join(&name);
        let metadata = std::fs::symlink_metadata(&full)?;
        let file_type = metadata.file_type();
        let record = if file_type.is_symlink() {
            let target = std::fs::read_link(&full)?;
            let target = target
                .to_str()
                .ok_or_else(|| {
                    ArchiveError::UnsafePath(format!("tree_link_non_utf8:{path}"))
                })?
                .to_owned();
            LayoutRecord::symlink(&path, &target, canonical_windows_mode(&path, false))
        } else if file_type.is_dir() {
            enumerate_directory_windows(
                &full,
                &path,
                depth + 1,
                ignore_install_marker,
                cancellation,
                entries,
            )?;
            LayoutRecord::directory(&path, canonical_windows_mode(&path, true))
        } else if file_type.is_file() {
            LayoutRecord::regular(&path, metadata.len(), canonical_windows_mode(&path, false))
        } else {
            return Err(ArchiveError::UnsafeEntry(format!("tree_type:{path}")));
        };
        if entries.insert(path.clone(), record).is_some() {
            return Err(ArchiveError::UnsafeEntry(format!("tree_duplicate:{path}")));
        }
    }
    Ok(())
}

#[cfg(windows)]
fn enumerate_tree_at(
    root: &Path,
    ignore_install_marker: bool,
    cancellation: &AtomicBool,
) -> Result<BTreeMap<String, LayoutRecord>, ArchiveError> {
    let metadata = std::fs::symlink_metadata(root)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(ArchiveError::UnsafePath("runtime_root".into()));
    }
    let mut entries = BTreeMap::new();
    enumerate_directory_windows(root, "", 0, ignore_install_marker, cancellation, &mut entries)?;
    Ok(entries)
}

#[cfg(unix)]
fn verify_extracted_layout(
    destination: &File,
    expected: &ArchiveLayout,
    cancellation: &AtomicBool,
) -> Result<(), ArchiveError> {
    let actual = enumerate_tree_at(destination, false, cancellation)?;
    if actual.len() != expected.entries.len() {
        return Err(ArchiveError::UnsafeEntry("post_extract_entry_count".into()));
    }
    for (path, expected_record) in &expected.entries {
        let actual_record = actual
            .get(path)
            .ok_or_else(|| ArchiveError::UnsafeEntry(format!("post_extract_missing:{path}")))?;
        if &actual_record.record != expected_record {
            return Err(ArchiveError::UnsafeEntry(format!(
                "post_extract_changed:{path}"
            )));
        }
    }
    Ok(())
}

/// Windows 版 `verify_extracted_layout`：重新枚举（规范化 mode）与
/// scan 布局比对；一致构建（构建工具保证 tar mode == 规范化 mode）。
#[cfg(windows)]
fn verify_extracted_layout(
    destination: &Path,
    expected: &ArchiveLayout,
    cancellation: &AtomicBool,
) -> Result<(), ArchiveError> {
    let actual = enumerate_tree_at(destination, false, cancellation)?;
    if actual.len() != expected.entries.len() {
        return Err(ArchiveError::UnsafeEntry("post_extract_entry_count".into()));
    }
    for (path, expected_record) in &expected.entries {
        let actual_record = actual
            .get(path)
            .ok_or_else(|| ArchiveError::UnsafeEntry(format!("post_extract_missing:{path}")))?;
        if actual_record != expected_record {
            return Err(ArchiveError::UnsafeEntry(format!(
                "post_extract_changed:{path}"
            )));
        }
    }
    Ok(())
}

#[cfg(unix)]
fn open_regular_at(root: &File, path: &str) -> Result<File, ArchiveError> {
    use rustix::fs::{fstat, openat, FileType, Mode, OFlags};

    let (parent, name) = open_relative_parent(root, path)?;
    let fd = openat(
        &parent,
        name.as_str(),
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC | OFlags::NONBLOCK,
        Mode::empty(),
    )
    .map_err(io::Error::from)?;
    let file = File::from(fd);
    let stat = fstat(&file).map_err(io::Error::from)?;
    if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile {
        return Err(ArchiveError::UnsafeEntry(format!("not_regular:{path}")));
    }
    Ok(file)
}

/// Windows 版 `open_regular_at`：拒绝符号链接后以只读打开普通文件。
#[cfg(windows)]
fn open_regular_at(root: &Path, path: &str) -> Result<File, ArchiveError> {
    let full = root.join(path);
    let metadata = std::fs::symlink_metadata(&full).map_err(ArchiveError::Io)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(ArchiveError::UnsafeEntry(format!("not_regular:{path}")));
    }
    File::open(&full).map_err(ArchiveError::Io)
}

#[cfg(unix)]
fn object_error(error: object::Error) -> ArchiveError {
    ArchiveError::UnsafeEntry(format!("macho:{error}"))
}

#[cfg(windows)]
fn object_error(error: object::Error) -> ArchiveError {
    ArchiveError::UnsafeEntry(format!("pe:{error}"))
}

#[cfg(unix)]
fn macho_arm64_status(mut file: File) -> Result<Option<bool>, ArchiveError> {
    let mut magic = [0_u8; 4];
    let read = file.read(&mut magic)?;
    file.seek(SeekFrom::Start(0))?;
    if read != magic.len()
        || !matches!(
            magic,
            [0xfe, 0xed, 0xfa, 0xce]
                | [0xce, 0xfa, 0xed, 0xfe]
                | [0xfe, 0xed, 0xfa, 0xcf]
                | [0xcf, 0xfa, 0xed, 0xfe]
                | [0xca, 0xfe, 0xba, 0xbe]
                | [0xbe, 0xba, 0xfe, 0xca]
                | [0xca, 0xfe, 0xba, 0xbf]
                | [0xbf, 0xba, 0xfe, 0xca]
        )
    {
        return Ok(None);
    }
    let length = file.metadata()?.len();
    let cache = ReadCache::new(file);
    match FileKind::parse(&cache).map_err(object_error)? {
        FileKind::MachO32 | FileKind::MachO64 => Ok(Some(
            object::File::parse(&cache)
                .map_err(object_error)?
                .architecture()
                == Architecture::Aarch64,
        )),
        FileKind::MachOFat32 => {
            let fat = MachOFatFile32::parse(&cache).map_err(object_error)?;
            let mut arm64 = false;
            for architecture in fat.arches() {
                let (offset, size) = architecture.file_range();
                if offset.checked_add(size).is_none_or(|end| end > length) {
                    return Err(ArchiveError::UnsafeEntry("macho:fat_range".into()));
                }
                let object =
                    object::File::parse(cache.range(offset, size)).map_err(object_error)?;
                if object.architecture() != architecture.architecture() {
                    return Err(ArchiveError::UnsafeEntry("macho:fat_architecture".into()));
                }
                arm64 |= object.architecture() == Architecture::Aarch64;
            }
            Ok(Some(arm64))
        }
        FileKind::MachOFat64 => {
            let fat = MachOFatFile64::parse(&cache).map_err(object_error)?;
            let mut arm64 = false;
            for architecture in fat.arches() {
                let (offset, size) = architecture.file_range();
                if offset.checked_add(size).is_none_or(|end| end > length) {
                    return Err(ArchiveError::UnsafeEntry("macho:fat_range".into()));
                }
                let object =
                    object::File::parse(cache.range(offset, size)).map_err(object_error)?;
                if object.architecture() != architecture.architecture() {
                    return Err(ArchiveError::UnsafeEntry("macho:fat_architecture".into()));
                }
                arm64 |= object.architecture() == Architecture::Aarch64;
            }
            Ok(Some(arm64))
        }
        _ => Err(ArchiveError::UnsafeEntry("macho:kind".into())),
    }
}

#[cfg(all(test, unix))]
fn is_arm64_macho(path: &Path) -> Result<bool, ArchiveError> {
    Ok(macho_arm64_status(open_archive_file(path)?)?.unwrap_or(false))
}

/// Windows 架构门禁：读取 PE 头判断 Machine 是否为
/// IMAGE_FILE_MACHINE_AMD64（0x8664）。非 PE 文件（如脚本/资源）返回
/// Ok(None)，不参与门禁；是 PE 但解析失败则报错。
#[cfg(windows)]
fn pe_x86_64_status(mut file: File) -> Result<Option<bool>, ArchiveError> {
    let mut magic = [0_u8; 2];
    let read = file.read(&mut magic)?;
    file.seek(SeekFrom::Start(0))?;
    // PE 以 DOS 头 'MZ' 开头。
    if read != magic.len() || magic != [b'M', b'Z'] {
        return Ok(None);
    }
    let cache = ReadCache::new(file);
    match FileKind::parse(&cache).map_err(object_error)? {
        FileKind::Pe32 | FileKind::Pe64 => Ok(Some(
            object::File::parse(&cache)
                .map_err(object_error)?
                .architecture()
                == Architecture::X86_64,
        )),
        _ => Err(ArchiveError::UnsafeEntry("pe:kind".into())),
    }
}

#[cfg(unix)]
fn terminate_process_group(child: &mut std::process::Child) {
    use nix::{
        sys::signal::{killpg, Signal},
        unistd::Pid,
    };

    let group = Pid::from_raw(child.id() as i32);
    let _ = killpg(group, Signal::SIGTERM);
    let deadline = Instant::now() + Duration::from_millis(250);
    while Instant::now() < deadline {
        if child.try_wait().ok().flatten().is_some() {
            break;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    let _ = killpg(group, Signal::SIGKILL);
    let _ = child.wait();
}

/// Windows 版终止：直接 kill()（无进程组；Job Object 由平台层负责）。
#[cfg(windows)]
fn terminate_process_group(child: &mut std::process::Child) {
    let _ = child.kill();
    let _ = child.wait();
}

fn smoke_version_with_timeout(
    path: &Path,
    cancellation: &AtomicBool,
    timeout: Duration,
) -> Result<i32, ArchiveError> {
    check_cancelled(cancellation)?;
    let mut command = Command::new(path);
    command
        .arg("--version")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(unix)]
    command.process_group(0);
    #[cfg(windows)]
    {
        // CREATE_NO_WINDOW：避免控制台窗口闪现。
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    let mut child = command.spawn()?;
    let deadline = Instant::now() + timeout;
    loop {
        if cancellation.load(Ordering::Acquire) {
            terminate_process_group(&mut child);
            return Err(ArchiveError::Cancelled);
        }
        if let Some(status) = child.try_wait()? {
            let code = status.code().unwrap_or(-1);
            #[cfg(unix)]
            {
                // A --version command must not leave descendants behind.
                let _ = nix::sys::signal::killpg(
                    nix::unistd::Pid::from_raw(child.id() as i32),
                    nix::sys::signal::Signal::SIGKILL,
                );
            }
            return Ok(code);
        }
        if Instant::now() >= deadline {
            terminate_process_group(&mut child);
            return Err(ArchiveError::SmokeTimeout(path.display().to_string()));
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[cfg(unix)]
fn is_required_binary_name(name: &str) -> bool {
    matches!(
        name,
        "python3" | "node" | "Chromium" | "Google Chrome for Testing" | "chrome"
    )
}

/// Windows 必需二进制名。仅覆盖解释器入口：
/// - 冒烟测试只运行 python.exe / node.exe；
/// - chrome.exe / headless_shell.exe 不参与（Chrome for Testing 在 Windows 上
///   `--version` 不会快速退出而会拉起完整浏览器组件，且从 AppData 拉起
///   浏览器可执行文件会触发 Defender 干预，曾导致整个进程冻结）。
///   浏览器完整性由包级 sha256 + 树哈希保证。
#[cfg(windows)]
fn is_required_binary_name(name: &str) -> bool {
    matches!(name, "python.exe" | "node.exe")
}

pub fn validate_runtime_tree(root: &Path, required_files: &[String]) -> Result<(), ArchiveError> {
    validate_runtime_tree_cancellable(root, required_files, &AtomicBool::new(false))
}

#[cfg(unix)]
fn validate_opened_entry(
    file: &File,
    entry: &AnchoredEntry,
    path: &str,
) -> Result<(), ArchiveError> {
    use rustix::fs::{fstat, FileType};

    let stat = fstat(file).map_err(io::Error::from)?;
    let LayoutKind::Regular { size } = entry.record.kind else {
        return Err(ArchiveError::UnsafeEntry(format!("not_regular:{path}")));
    };
    if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile
        || stat.st_dev as u64 != entry.device
        || stat.st_ino as u64 != entry.inode
        || stat.st_size < 0
        || stat.st_size as u64 != size
        || (stat.st_mode as u32) & 0o7777 != entry.record.mode
    {
        return Err(ArchiveError::UnsafeEntry(format!("tree_changed:{path}")));
    }
    Ok(())
}

#[cfg(unix)]
fn validate_runtime_tree_at(
    root: &File,
    required_files: &[String],
    cancellation: &AtomicBool,
) -> Result<(), ArchiveError> {
    let entries = enumerate_tree_at(root, true, cancellation)?;
    let layout =
        ArchiveLayout::from_records(entries.values().map(|entry| entry.record.clone()).collect())?;

    for (path, entry) in &entries {
        check_cancelled(cancellation)?;
        if !matches!(entry.record.kind, LayoutKind::Regular { .. }) {
            continue;
        }
        let file = open_regular_at(root, path)?;
        validate_opened_entry(&file, entry, path)?;
        if macho_arm64_status(file)? == Some(false) {
            return Err(ArchiveError::RequiredFile(format!("{path}:not_arm64")));
        }
    }

    for required in required_files {
        check_cancelled(cancellation)?;
        canonical_posix_path(required.as_bytes())?;
        let resolved = layout
            .resolve_regular(required)
            .map_err(|_| ArchiveError::RequiredFile(required.clone()))?;
        let entry = entries
            .get(&resolved)
            .ok_or_else(|| ArchiveError::RequiredFile(required.clone()))?;
        let name = required.rsplit('/').next().unwrap_or_default();
        let executable = is_required_binary_name(name) || required.ends_with(".sh");
        if executable && entry.record.mode & 0o111 == 0 {
            return Err(ArchiveError::RequiredFile(format!(
                "{required}:not_executable"
            )));
        }
        if is_required_binary_name(name) {
            let file = open_regular_at(root, &resolved)?;
            validate_opened_entry(&file, entry, &resolved)?;
            let smoke_file = open_regular_at(root, &resolved)?;
            validate_opened_entry(&smoke_file, entry, &resolved)?;
            if macho_arm64_status(file)? != Some(true) {
                return Err(ArchiveError::RequiredFile(format!("{required}:not_arm64")));
            }
            let smoke_path = rustix::fs::getpath(&smoke_file).map_err(io::Error::from)?;
            let exit_code = smoke_version_with_timeout(
                Path::new(std::ffi::OsStr::from_bytes(smoke_path.as_bytes())),
                cancellation,
                Duration::from_secs(5),
            )?;
            validate_opened_entry(&smoke_file, entry, &resolved)?;
            if exit_code != 0 {
                return Err(ArchiveError::SmokeFailed {
                    path: required.clone(),
                    exit_code,
                });
            }
        }
    }
    Ok(())
}

/// Windows 版运行时树校验：
/// - 枚举（规范化 mode）并重建链接图；
/// - 所有普通文件过 PE 架构门禁（非 PE 跳过；PE 必须为 x86_64）；
/// - required_files 必须解析到内部普通文件；可执行入口（python.exe /
///   node.exe / chrome.exe / headless_shell.exe）执行 `--version` 冒烟
///   （CREATE_NO_WINDOW + 超时 kill）。
#[cfg(windows)]
fn validate_runtime_tree_at(
    root: &Path,
    required_files: &[String],
    cancellation: &AtomicBool,
) -> Result<(), ArchiveError> {
    let entries = enumerate_tree_at(root, true, cancellation)?;
    let layout =
        ArchiveLayout::from_records(entries.values().map(|record| record.clone()).collect())?;

    for (path, record) in &entries {
        check_cancelled(cancellation)?;
        if !matches!(record.kind, LayoutKind::Regular { .. }) {
            continue;
        }
        // 仅对已知主二进制做 PE 架构门禁（与 macOS 的命名门禁对齐）。
        // 依赖包内可能携带 32 位辅助程序（如 pip 的 distlib/t32.exe），
        // 它们不是运行时入口，不参与门禁。
        let name = path.rsplit('/').next().unwrap_or_default();
        if !is_required_binary_name(name) {
            continue;
        }
        if pe_x86_64_status(open_regular_at(root, path)?)? == Some(false) {
            return Err(ArchiveError::RequiredFile(format!("{path}:not_x86_64")));
        }
    }

    for required in required_files {
        check_cancelled(cancellation)?;
        let required = required.as_str();
        canonical_posix_path(required.as_bytes())?;
        reject_drive_prefix(required)?;
        let resolved = layout
            .resolve_regular(required)
            .map_err(|_| ArchiveError::RequiredFile(required.to_owned()))?;
        let entry = entries
            .get(&resolved)
            .ok_or_else(|| ArchiveError::RequiredFile(required.to_owned()))?;
        let name = required.rsplit('/').next().unwrap_or_default();
        let executable = is_required_binary_name(name) || required.ends_with(".exe");
        if executable && entry.mode & 0o111 == 0 {
            return Err(ArchiveError::RequiredFile(format!(
                "{required}:not_executable"
            )));
        }
        if is_required_binary_name(name) {
            let smoke_path = root.join(&resolved);
            let exit_code = smoke_version_with_timeout(
                &smoke_path,
                cancellation,
                Duration::from_secs(5),
            )?;
            if exit_code != 0 {
                return Err(ArchiveError::SmokeFailed {
                    path: required.to_owned(),
                    exit_code,
                });
            }
        }
    }
    Ok(())
}

pub fn validate_runtime_tree_cancellable(
    root: &Path,
    required_files: &[String],
    cancellation: &AtomicBool,
) -> Result<(), ArchiveError> {
    let root = open_absolute_directory_nofollow(root)?;
    validate_runtime_tree_at(&root, required_files, cancellation)
}

fn hash_length(digest: &mut Sha256, value: u64) {
    digest.update(value.to_le_bytes());
}

#[cfg(test)]
fn validate_tree_utf8(raw: &[u8], label: &str) -> Result<(), ArchiveError> {
    str::from_utf8(raw)
        .map(|_| ())
        .map_err(|_| ArchiveError::UnsafePath(label.into()))
}

pub fn runtime_tree_sha256(root: &Path) -> Result<String, ArchiveError> {
    runtime_tree_sha256_cancellable(root, &AtomicBool::new(false))
}

pub fn runtime_tree_sha256_cancellable(
    root: &Path,
    cancellation: &AtomicBool,
) -> Result<String, ArchiveError> {
    let root = open_absolute_directory_nofollow(root)?;
    runtime_tree_sha256_at(&root, cancellation)
}

#[cfg(unix)]
fn runtime_tree_sha256_at(root: &File, cancellation: &AtomicBool) -> Result<String, ArchiveError> {
    use rustix::fs::fstat;

    let entries = enumerate_tree_at(root, true, cancellation)?;
    ArchiveLayout::from_records(entries.values().map(|entry| entry.record.clone()).collect())?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 1024 * 1024];
    for (path, entry) in entries {
        check_cancelled(cancellation)?;
        let relative = path.as_bytes();
        let (kind, payload_length) = match &entry.record.kind {
            LayoutKind::Symlink { target } => (b'L', target.len() as u64),
            LayoutKind::Directory => (b'D', 0),
            LayoutKind::Regular { size } => (b'F', *size),
        };
        digest.update([kind]);
        hash_length(&mut digest, relative.len() as u64);
        digest.update(relative);
        digest.update(entry.record.mode.to_le_bytes());
        hash_length(&mut digest, payload_length);
        match &entry.record.kind {
            LayoutKind::Symlink { target } => digest.update(target.as_bytes()),
            LayoutKind::Directory => {}
            LayoutKind::Regular { size } => {
                let mut file = open_regular_at(root, &path)?;
                validate_opened_entry(&file, &entry, &path)?;
                let mut remaining = *size;
                while remaining != 0 {
                    check_cancelled(cancellation)?;
                    let maximum = usize::try_from(remaining.min(buffer.len() as u64))
                        .map_err(|_| ArchiveError::Limit(format!("tree_size:{path}")))?;
                    let read = file.read(&mut buffer[..maximum])?;
                    if read == 0 {
                        return Err(ArchiveError::UnsafeEntry(format!("tree_truncated:{path}")));
                    }
                    digest.update(&buffer[..read]);
                    remaining -= read as u64;
                }
                if file.read(&mut [0_u8; 1])? != 0 {
                    return Err(ArchiveError::UnsafeEntry(format!("tree_oversized:{path}")));
                }
                let after = fstat(&file).map_err(io::Error::from)?;
                if after.st_dev as u64 != entry.device
                    || after.st_ino as u64 != entry.inode
                    || after.st_size < 0
                    || after.st_size as u64 != *size
                {
                    return Err(ArchiveError::UnsafeEntry(format!("tree_changed:{path}")));
                }
            }
        }
    }
    Ok(hex::encode(digest.finalize()))
}

/// Windows 版树哈希：对真实目录做 std::fs 遍历，条目按相对路径字节序
/// （正斜杠）排序；每项写入 类型字节 + u64le(路径长) + 路径 +
/// u32le(mode) + u64le(负载长) + 负载。mode 使用与
/// scripts/build_windows_runtime.py 一致的规范化规则（见
/// canonical_windows_mode），因为提取阶段不保留 unix mode。
#[cfg(windows)]
fn runtime_tree_sha256_at(
    root: &Path,
    cancellation: &AtomicBool,
) -> Result<String, ArchiveError> {
    let entries = enumerate_tree_at(root, true, cancellation)?;
    ArchiveLayout::from_records(entries.values().map(|record| record.clone()).collect())?;
    let mut digest = Sha256::new();
    // 堆分配缓冲：避免受限线程栈溢出
    let mut buffer = vec![0_u8; 1024 * 1024];
    for (path, record) in entries {
        check_cancelled(cancellation)?;
        let relative = path.as_bytes();
        let (kind, payload_length) = match &record.kind {
            LayoutKind::Symlink { target } => (b'L', target.len() as u64),
            LayoutKind::Directory => (b'D', 0),
            LayoutKind::Regular { size } => (b'F', *size),
        };
        digest.update([kind]);
        hash_length(&mut digest, relative.len() as u64);
        digest.update(relative);
        digest.update(record.mode.to_le_bytes());
        hash_length(&mut digest, payload_length);
        match &record.kind {
            LayoutKind::Symlink { target } => digest.update(target.as_bytes()),
            LayoutKind::Directory => {}
            LayoutKind::Regular { size } => {
                let mut file = open_regular_at(root, &path)?;
                let mut remaining = *size;
                while remaining != 0 {
                    check_cancelled(cancellation)?;
                    let maximum = usize::try_from(remaining.min(buffer.len() as u64))
                        .map_err(|_| ArchiveError::Limit(format!("tree_size:{path}")))?;
                    let read = file.read(&mut buffer[..maximum])?;
                    if read == 0 {
                        return Err(ArchiveError::UnsafeEntry(format!("tree_truncated:{path}")));
                    }
                    digest.update(&buffer[..read]);
                    remaining -= read as u64;
                }
                if file.read(&mut [0_u8; 1])? != 0 {
                    return Err(ArchiveError::UnsafeEntry(format!("tree_oversized:{path}")));
                }
            }
        }
    }
    Ok(hex::encode(digest.finalize()))
}

pub fn verify_installed_runtime(
    root: &Path,
    descriptor: &RuntimeDescriptor,
) -> Result<(), ArchiveError> {
    verify_installed_runtime_cancellable(root, descriptor, &AtomicBool::new(false))
}

pub fn verify_installed_runtime_cancellable(
    root: &Path,
    descriptor: &RuntimeDescriptor,
    cancellation: &AtomicBool,
) -> Result<(), ArchiveError> {
    let root = open_absolute_directory_nofollow(root)?;
    verify_installed_runtime_at(&root, descriptor, cancellation)
}

pub(super) fn verify_installed_runtime_at(
    root: &DirectoryRef,
    descriptor: &RuntimeDescriptor,
    cancellation: &AtomicBool,
) -> Result<(), ArchiveError> {
    if descriptor.tree_sha256.len() != 64
        || !descriptor
            .tree_sha256
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(ArchiveError::HashMismatch);
    }
    if runtime_tree_sha256_at(root, cancellation)? != descriptor.tree_sha256 {
        return Err(ArchiveError::HashMismatch);
    }
    validate_runtime_tree_at(root, &descriptor.required_files, cancellation)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use crate::manifest::RuntimeDescriptor;
    use sha2::{Digest, Sha256};
    use std::{io::Cursor, path::Path, time::{Duration, Instant}};

    #[cfg(unix)]
    use std::os::unix::fs::PermissionsExt;

    fn append_fixture(
        builder: &mut tar::Builder<Vec<u8>>,
        path: &str,
        entry_type: tar::EntryType,
        data: &[u8],
        link: Option<&str>,
    ) {
        append_fixture_mode(builder, path, entry_type, data, link, None);
    }

    fn append_fixture_mode(
        builder: &mut tar::Builder<Vec<u8>>,
        path: &str,
        entry_type: tar::EntryType,
        data: &[u8],
        link: Option<&str>,
        mode: Option<u32>,
    ) {
        let mut header = tar::Header::new_ustar();
        header.set_path(path).unwrap();
        header.set_entry_type(entry_type);
        header.set_mode(mode.unwrap_or_else(|| {
            if entry_type.is_dir() {
                0o755
            } else if entry_type.is_symlink() {
                0o777
            } else {
                0o644
            }
        }));
        header.set_uid(0);
        header.set_gid(0);
        header.set_mtime(0);
        header.set_username("").unwrap();
        header.set_groupname("").unwrap();
        header.set_size(if entry_type.is_file() {
            data.len() as u64
        } else {
            0
        });
        if let Some(target) = link {
            header.set_link_name(target).unwrap();
        }
        header.set_cksum();
        builder.append(&header, Cursor::new(data)).unwrap();
    }

    fn write_archive(path: &Path, entries: &[(&str, tar::EntryType, &[u8], Option<&str>)]) {
        let mut builder = tar::Builder::new(Vec::new());
        let mut emitted = HashSet::new();
        for (name, entry_type, data, link) in entries {
            let parts: Vec<_> = name.split('/').collect();
            for index in 1..parts.len() {
                let ancestor = parts[..index].join("/");
                if emitted.insert(ancestor.clone()) {
                    append_fixture(
                        &mut builder,
                        &ancestor,
                        tar::EntryType::Directory,
                        b"",
                        None,
                    );
                }
            }
            append_fixture(&mut builder, name, *entry_type, data, *link);
            emitted.insert((*name).to_owned());
        }
        let tar = builder.into_inner().unwrap();
        let compressed = zstd::stream::encode_all(Cursor::new(tar), 1).unwrap();
        std::fs::write(path, compressed).unwrap();
    }

    fn descriptor(path: &Path, required_files: Vec<String>) -> RuntimeDescriptor {
        let bytes = std::fs::read(path).unwrap();
        let decoder = zstd::stream::read::Decoder::new(Cursor::new(&bytes)).unwrap();
        let mut archive = tar::Archive::new(decoder);
        let mut records = BTreeMap::new();
        for item in archive.entries().unwrap() {
            let mut entry = item.unwrap();
            let record = extraction_record(&mut entry).unwrap();
            let mut payload = Vec::new();
            if matches!(record.kind, LayoutKind::Regular { .. }) {
                entry.read_to_end(&mut payload).unwrap();
            }
            records.insert(record.path.clone(), (record, payload));
        }
        let mut tree_digest = Sha256::new();
        for (name, (record, payload)) in records {
            let (kind, length) = match &record.kind {
                LayoutKind::Regular { size } => (b'F', *size),
                LayoutKind::Directory => (b'D', 0),
                LayoutKind::Symlink { target } => (b'L', target.len() as u64),
            };
            tree_digest.update([kind]);
            hash_length(&mut tree_digest, name.len() as u64);
            tree_digest.update(name.as_bytes());
            tree_digest.update(record.mode.to_le_bytes());
            hash_length(&mut tree_digest, length);
            match record.kind {
                LayoutKind::Regular { .. } => tree_digest.update(payload),
                LayoutKind::Directory => {}
                LayoutKind::Symlink { target } => tree_digest.update(target.as_bytes()),
            }
        }
        RuntimeDescriptor {
            version: "core-v1".into(),
            archive: "core-runtime.tar.zst".into(),
            sha256: hex::encode(Sha256::digest(&bytes)),
            tree_sha256: hex::encode(tree_digest.finalize()),
            size_bytes: bytes.len() as u64,
            required_files,
        }
    }

    #[cfg(unix)]
    fn thin_macho(cpu_type: u32) -> Vec<u8> {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&0xfeedfacfu32.to_le_bytes());
        bytes.extend_from_slice(&cpu_type.to_le_bytes());
        bytes.extend_from_slice(&0u32.to_le_bytes());
        bytes.extend_from_slice(&2u32.to_le_bytes());
        bytes.extend_from_slice(&0u32.to_le_bytes());
        bytes.extend_from_slice(&0u32.to_le_bytes());
        bytes.extend_from_slice(&0u32.to_le_bytes());
        bytes.extend_from_slice(&0u32.to_le_bytes());
        bytes
    }

    #[cfg(unix)]
    fn fat_macho(is_64: bool, cpu_type: u32, slice: &[u8]) -> Vec<u8> {
        let header_size = if is_64 { 40_u64 } else { 28_u64 };
        let mut bytes = Vec::new();
        let magic = if is_64 { 0xcafebabfu32 } else { 0xcafebabeu32 };
        bytes.extend_from_slice(&magic.to_be_bytes());
        bytes.extend_from_slice(&1u32.to_be_bytes());
        bytes.extend_from_slice(&cpu_type.to_be_bytes());
        bytes.extend_from_slice(&0u32.to_be_bytes());
        if is_64 {
            bytes.extend_from_slice(&header_size.to_be_bytes());
            bytes.extend_from_slice(&(slice.len() as u64).to_be_bytes());
            bytes.extend_from_slice(&0u32.to_be_bytes());
            bytes.extend_from_slice(&0u32.to_be_bytes());
        } else {
            bytes.extend_from_slice(&(header_size as u32).to_be_bytes());
            bytes.extend_from_slice(&(slice.len() as u32).to_be_bytes());
            bytes.extend_from_slice(&0u32.to_be_bytes());
        }
        bytes.extend_from_slice(slice);
        bytes
    }

    fn regular(path: &str) -> LayoutRecord {
        LayoutRecord::regular(path, 4, 0o644)
    }

    fn directory(path: &str) -> LayoutRecord {
        LayoutRecord::directory(path, 0o755)
    }

    fn symlink(path: &str, target: &str) -> LayoutRecord {
        LayoutRecord::symlink(path, target, 0o777)
    }

    #[test]
    fn canonical_paths_reject_ambiguous_or_non_utf8_bytes() {
        assert_eq!(
            canonical_posix_path(b"scripts/_run.py").unwrap(),
            "scripts/_run.py"
        );
        for invalid in [
            b"".as_slice(),
            b"/absolute",
            b"../parent",
            b"scripts/./runner.py",
            b"scripts//runner.py",
            b"scripts/runner.py/",
            b"scripts\\runner.py",
            b"scripts/\0runner.py",
            b"scripts/\xffrunner.py",
        ] {
            assert!(canonical_posix_path(invalid).is_err(), "{invalid:?}");
        }
        let too_deep = format!("{}file", "a/".repeat(MAX_TREE_DEPTH));
        assert!(canonical_posix_path(too_deep.as_bytes()).is_err());
    }

    #[test]
    fn link_graph_accepts_framework_component_chains_and_python_link() {
        let framework = "runtime/browsers/Chromium.app/Contents/Frameworks/Chromium.framework";
        let layout = ArchiveLayout::from_records(vec![
            directory("runtime"),
            directory("runtime/browsers"),
            directory("runtime/browsers/Chromium.app"),
            directory("runtime/browsers/Chromium.app/Contents"),
            directory("runtime/browsers/Chromium.app/Contents/Frameworks"),
            directory(framework),
            directory(&format!("{framework}/Versions")),
            directory(&format!("{framework}/Versions/145.0.7632.6")),
            regular(&format!(
                "{framework}/Versions/145.0.7632.6/Chromium Framework"
            )),
            symlink(&format!("{framework}/Versions/Current"), "145.0.7632.6"),
            symlink(
                &format!("{framework}/Chromium Framework"),
                "Versions/Current/Chromium Framework",
            ),
            directory("runtime/python"),
            directory("runtime/python/bin"),
            regular("runtime/python/bin/python3.12"),
            symlink("runtime/python/bin/python3", "python3.12"),
        ])
        .unwrap();

        assert_eq!(
            layout
                .resolve_regular(&format!("{framework}/Chromium Framework"))
                .unwrap(),
            format!("{framework}/Versions/145.0.7632.6/Chromium Framework")
        );
        assert_eq!(
            layout
                .resolve_regular("runtime/python/bin/python3")
                .unwrap(),
            "runtime/python/bin/python3.12"
        );
    }

    #[test]
    fn link_graph_rejects_escape_dangling_self_cycle_and_symlink_ancestor() {
        let cases = [
            vec![regular("target"), symlink("escape", "../outside")],
            vec![symlink("dangling", "missing")],
            vec![symlink("self", "self")],
            vec![symlink("first", "second"), symlink("second", "first")],
            vec![
                regular("target"),
                symlink("alias", "target"),
                regular("alias/child"),
            ],
            vec![regular("file"), regular("file/child")],
            vec![regular("missing/child")],
        ];
        for records in cases {
            assert!(ArchiveLayout::from_records(records).is_err());
        }
    }

    #[test]
    fn scanner_accepts_only_canonical_python_entry_types_and_metadata() {
        let temp = tempfile::tempdir().unwrap();
        let archive = temp.path().join("runtime.tar.zst");
        write_archive(
            &archive,
            &[
                ("runtime", tar::EntryType::Directory, b"", None),
                (
                    "runtime/python3",
                    tar::EntryType::Symlink,
                    b"",
                    Some("python3.12"),
                ),
                ("runtime/python3.12", tar::EntryType::Regular, b"mach", None),
            ],
        );
        let cancelled = std::sync::atomic::AtomicBool::new(false);
        assert_eq!(scan_archive(&archive, &cancelled).unwrap(), 4);

        for entry_type in [
            tar::EntryType::Link,
            tar::EntryType::Fifo,
            tar::EntryType::Char,
            tar::EntryType::Block,
            tar::EntryType::Continuous,
            tar::EntryType::GNUSparse,
        ] {
            write_archive(
                &archive,
                &[(
                    "runtime/unsafe",
                    entry_type,
                    b"",
                    Some("runtime/python3.12"),
                )],
            );
            let result = scan_archive(&archive, &cancelled);
            if entry_type == tar::EntryType::GNUSparse {
                assert!(result.is_err(), "{entry_type:?}: {result:?}");
            } else {
                assert!(
                    matches!(result, Err(ArchiveError::UnsafeEntry(_))),
                    "{entry_type:?}: {result:?}"
                );
            }
        }
    }

    #[test]
    fn scanner_rejects_unknown_pax_extensions() {
        let temp = tempfile::tempdir().unwrap();
        let archive = temp.path().join("runtime.tar.zst");
        let mut builder = tar::Builder::new(Vec::new());
        builder
            .append_pax_extensions([("comment", b"unsigned metadata".as_slice())])
            .unwrap();
        append_fixture(
            &mut builder,
            "scripts/_run.py",
            tar::EntryType::Regular,
            b"run",
            None,
        );
        let compressed =
            zstd::stream::encode_all(Cursor::new(builder.into_inner().unwrap()), 1).unwrap();
        std::fs::write(&archive, compressed).unwrap();
        let cancelled = std::sync::atomic::AtomicBool::new(false);
        assert!(matches!(
            scan_archive(&archive, &cancelled),
            Err(ArchiveError::UnsafeEntry(_))
        ));
    }

    #[test]
    fn raw_preflight_rejects_oversized_or_global_pax_before_tar_parser_buffers_it() {
        let temp = tempfile::tempdir().unwrap();
        let archive = temp.path().join("runtime.tar.zst");
        let mut builder = tar::Builder::new(Vec::new());
        let oversized = vec![b'a'; 16 * 1024 + 1];
        builder
            .append_pax_extensions([("path", oversized.as_slice())])
            .unwrap();
        append_fixture(
            &mut builder,
            "placeholder",
            tar::EntryType::Regular,
            b"x",
            None,
        );
        let compressed =
            zstd::stream::encode_all(Cursor::new(builder.into_inner().unwrap()), 1).unwrap();
        std::fs::write(&archive, compressed).unwrap();
        let cancellation = AtomicBool::new(false);
        assert!(matches!(
            scan_archive(&archive, &cancellation),
            Err(ArchiveError::Limit(_))
        ));

        let mut builder = tar::Builder::new(Vec::new());
        let mut header = tar::Header::new_ustar();
        header.set_entry_type(tar::EntryType::XGlobalHeader);
        header.set_size(0);
        header.set_mode(0o644);
        header.set_uid(0);
        header.set_gid(0);
        header.set_mtime(0);
        header.set_cksum();
        builder.append(&header, Cursor::new([])).unwrap();
        let compressed =
            zstd::stream::encode_all(Cursor::new(builder.into_inner().unwrap()), 1).unwrap();
        std::fs::write(&archive, compressed).unwrap();
        assert!(matches!(
            scan_archive(&archive, &cancellation),
            Err(ArchiveError::UnsafeEntry(_))
        ));

        write_archive(&archive, &[("safe", tar::EntryType::Regular, b"x", None)]);
        let compressed = std::fs::read(&archive).unwrap();
        let mut raw = zstd::stream::decode_all(Cursor::new(compressed)).unwrap();
        raw[148..156].fill(b'0');
        std::fs::write(
            &archive,
            zstd::stream::encode_all(Cursor::new(raw), 1).unwrap(),
        )
        .unwrap();
        let error = scan_archive(&archive, &cancellation).unwrap_err();
        assert!(error.to_string().contains("tar_checksum"));
    }

    #[test]
    fn scanner_uses_a_private_snapshot_between_raw_preflight_and_tar_parsing() {
        let temp = tempfile::tempdir().unwrap();
        let archive = temp.path().join("runtime.tar.zst");
        let replacement = temp.path().join("replacement.tar.zst");
        write_archive(
            &archive,
            &[("safe", tar::EntryType::Regular, b"SAFE", None)],
        );
        write_archive(
            &replacement,
            &[("evil", tar::EntryType::Regular, b"MALICIOUS", None)],
        );
        let replacement_bytes = std::fs::read(&replacement).unwrap();
        let cancellation = AtomicBool::new(false);

        let unpacked = scan_archive_with_hook(&archive, &cancellation, || {
            std::fs::write(&archive, replacement_bytes).unwrap();
        })
        .unwrap();

        assert_eq!(unpacked, 4);
    }

    #[test]
    fn scanner_accepts_python_pax_long_directory_with_one_structural_slash() {
        let temp = tempfile::tempdir().unwrap();
        let archive = temp.path().join("runtime.tar.zst");
        let long_directory = format!("runtime/{}/", "framework".repeat(20));
        let mut builder = tar::Builder::new(Vec::new());
        append_fixture(
            &mut builder,
            "runtime",
            tar::EntryType::Directory,
            b"",
            None,
        );
        builder
            .append_pax_extensions([("path", long_directory.as_bytes())])
            .unwrap();
        append_fixture(
            &mut builder,
            "runtime/pax-placeholder",
            tar::EntryType::Directory,
            b"",
            None,
        );
        let compressed =
            zstd::stream::encode_all(Cursor::new(builder.into_inner().unwrap()), 1).unwrap();
        std::fs::write(&archive, compressed).unwrap();
        let cancelled = AtomicBool::new(false);
        assert_eq!(scan_archive(&archive, &cancelled).unwrap(), 0);
    }

    #[test]
    fn scanner_enforces_limits_cancellation_and_canonical_member_order() {
        let temp = tempfile::tempdir().unwrap();
        let archive = temp.path().join("runtime.tar.zst");
        write_archive(
            &archive,
            &[
                ("scripts/b.py", tar::EntryType::Regular, b"BBBB", None),
                ("scripts/a.py", tar::EntryType::Regular, b"AAAA", None),
            ],
        );
        let cancelled = AtomicBool::new(false);
        assert!(matches!(
            scan_archive(&archive, &cancelled),
            Err(ArchiveError::UnsafeEntry(_))
        ));

        write_archive(
            &archive,
            &[
                ("scripts/a.py", tar::EntryType::Regular, b"AAAA", None),
                ("scripts/b.py", tar::EntryType::Regular, b"BBBB", None),
            ],
        );
        let mut file = open_archive_file(&archive).unwrap();
        let size = file.metadata().unwrap().len();
        let limits = ArchiveLimits {
            entries: 1,
            ..ArchiveLimits::default()
        };
        assert!(matches!(
            scan_open_archive(&mut file, size, &cancelled, limits),
            Err(ArchiveError::Limit(_))
        ));
        let limits = ArchiveLimits {
            file_bytes: 3,
            ..ArchiveLimits::default()
        };
        assert!(matches!(
            scan_open_archive(&mut file, size, &cancelled, limits),
            Err(ArchiveError::Limit(_))
        ));
        let limits = ArchiveLimits {
            total_bytes: 7,
            ..ArchiveLimits::default()
        };
        assert!(matches!(
            scan_open_archive(&mut file, size, &cancelled, limits),
            Err(ArchiveError::Limit(_))
        ));
        let limits = ArchiveLimits {
            expansion_ratio: 0,
            expansion_allowance: 0,
            ..ArchiveLimits::default()
        };
        assert!(matches!(
            scan_open_archive(&mut file, size, &cancelled, limits),
            Err(ArchiveError::Limit(_))
        ));

        cancelled.store(true, Ordering::Release);
        assert!(matches!(
            scan_open_archive(&mut file, size, &cancelled, ArchiveLimits::default()),
            Err(ArchiveError::Cancelled)
        ));
        let mut cancelled_descriptor = descriptor(&archive, vec!["scripts/a.py".into()]);
        cancelled_descriptor.sha256 = "0".repeat(64);
        assert!(matches!(
            verify_bytes(&archive, &cancelled_descriptor, &cancelled),
            Err(ArchiveError::Cancelled)
        ));
    }

    #[test]
    fn extract_uses_the_already_verified_file_descriptor_after_path_replacement() {
        let temp = tempfile::tempdir().unwrap();
        let archive = temp.path().join("core-runtime.tar.zst");
        let replacement = temp.path().join("replacement.tar.zst");
        let displaced = temp.path().join("verified-but-renamed.tar.zst");
        let destination = std::fs::canonicalize(temp.path()).unwrap().join("runtime");
        write_archive(
            &archive,
            &[("scripts/_run.py", tar::EntryType::Regular, b"SAFE", None)],
        );
        write_archive(
            &replacement,
            &[("scripts/evil.py", tar::EntryType::Regular, b"EVIL", None)],
        );
        let descriptor = descriptor(&archive, vec!["scripts/_run.py".into()]);
        let cancelled = AtomicBool::new(false);

        extract_verified_with_hook(&archive, &descriptor, &destination, &cancelled, || {
            std::fs::rename(&archive, &displaced).unwrap();
            std::fs::rename(&replacement, &archive).unwrap();
        })
        .unwrap();

        assert_eq!(
            std::fs::read(destination.join("scripts/_run.py")).unwrap(),
            b"SAFE"
        );
        assert!(!destination.join("scripts/evil.py").exists());
    }

    #[test]
    fn extract_uses_a_private_snapshot_when_archive_inode_is_rewritten_in_place() {
        let temp = tempfile::tempdir().unwrap();
        let archive = temp.path().join("core-runtime.tar.zst");
        let replacement = temp.path().join("replacement.tar.zst");
        let destination = std::fs::canonicalize(temp.path()).unwrap().join("runtime");
        write_archive(
            &archive,
            &[("scripts/_run.py", tar::EntryType::Regular, b"SAFE", None)],
        );
        write_archive(
            &replacement,
            &[("scripts/_run.py", tar::EntryType::Regular, b"EVIL", None)],
        );
        let descriptor = descriptor(&archive, vec!["scripts/_run.py".into()]);
        let replacement_bytes = std::fs::read(&replacement).unwrap();
        let cancelled = AtomicBool::new(false);
        extract_verified_with_hook(&archive, &descriptor, &destination, &cancelled, || {
            std::fs::write(&archive, &replacement_bytes).unwrap();
        })
        .unwrap();
        assert_eq!(
            std::fs::read(destination.join("scripts/_run.py")).unwrap(),
            b"SAFE"
        );
    }

    #[cfg(unix)]
    #[test]
    fn extraction_rejects_a_symlink_in_the_destination_ancestor_chain() {
        use std::os::unix::fs::symlink;

        let temp = tempfile::tempdir().unwrap();
        let root = std::fs::canonicalize(temp.path()).unwrap();
        let archive = root.join("core-runtime.tar.zst");
        let outside = root.join("outside");
        let redirected_parent = root.join("redirected-parent");
        std::fs::create_dir(&outside).unwrap();
        symlink(&outside, &redirected_parent).unwrap();
        write_archive(
            &archive,
            &[("scripts/_run.py", tar::EntryType::Regular, b"SAFE", None)],
        );
        let descriptor = descriptor(&archive, vec!["scripts/_run.py".into()]);
        let cancellation = AtomicBool::new(false);

        assert!(extract_verified(
            &archive,
            &descriptor,
            &redirected_parent.join("runtime"),
            &cancellation,
        )
        .is_err());
        assert!(!outside.join("runtime").exists());
    }

    #[cfg(unix)]
    #[test]
    fn extraction_stays_anchored_when_the_destination_name_is_replaced() {
        use std::os::unix::fs::symlink;

        let temp = tempfile::tempdir().unwrap();
        let root = std::fs::canonicalize(temp.path()).unwrap();
        let archive = root.join("core-runtime.tar.zst");
        let moved = root.join("runtime-moved");
        let outside = root.join("outside");
        std::fs::create_dir(&outside).unwrap();
        write_archive(
            &archive,
            &[("scripts/_run.py", tar::EntryType::Regular, b"SAFE", None)],
        );
        let descriptor = descriptor(&archive, vec!["scripts/_run.py".into()]);
        let parent = open_directory_file(&root).unwrap();
        let cancellation = AtomicBool::new(false);

        let result = extract_verified_at_with_hooks(
            &archive,
            &descriptor,
            &parent,
            "runtime",
            &cancellation,
            || {},
            || {
                std::fs::rename(root.join("runtime"), &moved).unwrap();
                symlink(&outside, root.join("runtime")).unwrap();
            },
        );

        assert!(result.is_err());
        assert_eq!(
            std::fs::read(moved.join("scripts/_run.py")).unwrap(),
            b"SAFE"
        );
        assert!(!outside.join("scripts").exists());
    }

    #[test]
    fn post_extraction_enumeration_rejects_unexpected_entries() {
        let temp = tempfile::tempdir().unwrap();
        let root = std::fs::canonicalize(temp.path()).unwrap();
        std::fs::write(root.join("expected"), b"safe").unwrap();
        std::fs::write(root.join("unexpected"), b"extra").unwrap();
        let directory = open_directory_file(&root).unwrap();
        let expected =
            ArchiveLayout::from_records(vec![LayoutRecord::regular("expected", 4, 0o644)]).unwrap();
        let cancellation = AtomicBool::new(false);

        assert!(matches!(
            verify_extracted_layout(&directory, &expected, &cancellation),
            Err(ArchiveError::UnsafeEntry(_))
        ));
    }

    #[cfg(unix)]
    #[test]
    fn archive_fifo_is_rejected_without_blocking() {
        let temp = tempfile::tempdir().unwrap();
        let fifo = temp.path().join("runtime.tar.zst");
        let path = std::ffi::CString::new(fifo.as_os_str().as_bytes()).unwrap();
        assert_eq!(unsafe { nix::libc::mkfifo(path.as_ptr(), 0o600) }, 0);
        let (sender, receiver) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            sender.send(open_archive_file(&fifo).is_err()).unwrap();
        });
        assert!(receiver.recv_timeout(Duration::from_millis(500)).unwrap());
    }

    #[cfg(unix)]
    #[test]
    fn extraction_rejects_required_symlink_unless_it_resolves_to_internal_regular_file() {
        let temp = tempfile::tempdir().unwrap();
        let archive = temp.path().join("core-runtime.tar.zst");
        let destination = std::fs::canonicalize(temp.path()).unwrap().join("runtime");
        let executable = std::fs::read("/usr/bin/true").unwrap();
        let mut builder = tar::Builder::new(Vec::new());
        append_fixture(
            &mut builder,
            "runtime",
            tar::EntryType::Directory,
            b"",
            None,
        );
        append_fixture(
            &mut builder,
            "runtime/python3",
            tar::EntryType::Symlink,
            b"",
            Some("python3.12"),
        );
        append_fixture_mode(
            &mut builder,
            "runtime/python3.12",
            tar::EntryType::Regular,
            &executable,
            None,
            Some(0o755),
        );
        let compressed =
            zstd::stream::encode_all(Cursor::new(builder.into_inner().unwrap()), 1).unwrap();
        std::fs::write(&archive, compressed).unwrap();
        let descriptor = descriptor(&archive, vec!["runtime/python3".into()]);
        let cancelled = AtomicBool::new(false);
        extract_verified(&archive, &descriptor, &destination, &cancelled).unwrap();
        assert_eq!(
            std::fs::read(destination.join("runtime/python3")).unwrap(),
            executable
        );
    }

    #[cfg(unix)]
    #[test]
    fn extraction_runs_the_required_executable_gate_before_success() {
        let temp = tempfile::tempdir().unwrap();
        let archive = temp.path().join("collector-runtime.tar.zst");
        let destination = std::fs::canonicalize(temp.path()).unwrap().join("runtime");
        write_archive(
            &archive,
            &[(
                "scripts/run_export.sh",
                tar::EntryType::Regular,
                b"#!/bin/sh\nexit 0\n",
                None,
            )],
        );
        let descriptor = descriptor(&archive, vec!["scripts/run_export.sh".into()]);
        let cancelled = AtomicBool::new(false);
        assert!(matches!(
            extract_verified(&archive, &descriptor, &destination, &cancelled),
            Err(ArchiveError::RequiredFile(_))
        ));
        assert!(
            !destination.exists(),
            "failed extraction must clean staging"
        );
    }

    #[cfg(unix)]
    #[test]
    fn macho_parser_accepts_thin_fat32_and_fat64_arm64_and_rejects_malformed() {
        let temp = tempfile::tempdir().unwrap();
        let binary = temp.path().join("binary");
        let arm64 = thin_macho(0x0100_000c);
        for bytes in [
            arm64.clone(),
            fat_macho(false, 0x0100_000c, &arm64),
            fat_macho(true, 0x0100_000c, &arm64),
        ] {
            std::fs::write(&binary, bytes).unwrap();
            assert!(is_arm64_macho(&binary).unwrap());
        }

        std::fs::write(&binary, thin_macho(0x0100_0007)).unwrap();
        assert!(!is_arm64_macho(&binary).unwrap());
        std::fs::write(&binary, 0xcafebabfu32.to_be_bytes()).unwrap();
        assert!(is_arm64_macho(&binary).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn runtime_validation_checks_dylibs_and_propagates_walk_errors() {
        let temp = tempfile::tempdir().unwrap();
        let root = std::fs::canonicalize(temp.path()).unwrap();
        let wrapper = root.join("scripts/run_export.sh");
        std::fs::create_dir_all(wrapper.parent().unwrap()).unwrap();
        std::fs::write(&wrapper, b"#!/bin/sh\nexit 0\n").unwrap();
        std::fs::set_permissions(&wrapper, std::fs::Permissions::from_mode(0o755)).unwrap();
        let library = root.join("runtime/libcollector.dylib");
        std::fs::create_dir_all(library.parent().unwrap()).unwrap();
        std::fs::write(&library, thin_macho(0x0100_000c)).unwrap();
        validate_runtime_tree(&root, &["scripts/run_export.sh".into()]).unwrap();

        std::fs::write(&library, thin_macho(0x0100_0007)).unwrap();
        assert!(matches!(
            validate_runtime_tree(&root, &["scripts/run_export.sh".into()]),
            Err(ArchiveError::RequiredFile(_))
        ));

        std::fs::write(&library, thin_macho(0x0100_000c)).unwrap();
        let unreadable = root.join("unreadable");
        std::fs::create_dir(&unreadable).unwrap();
        std::fs::set_permissions(&unreadable, std::fs::Permissions::from_mode(0o000)).unwrap();
        let result = validate_runtime_tree(&root, &["scripts/run_export.sh".into()]);
        std::fs::set_permissions(&unreadable, std::fs::Permissions::from_mode(0o755)).unwrap();
        assert!(result.is_err());
    }

    #[cfg(unix)]
    #[test]
    fn runtime_validation_checks_macho_magic_for_every_regular_file() {
        for name in ["extensionless", "addon.node", "plugin.bundle"] {
            let temp = tempfile::tempdir().unwrap();
            let root = std::fs::canonicalize(temp.path()).unwrap();
            let binary = root.join(name);
            std::fs::write(&binary, thin_macho(0x0100_000c)).unwrap();
            validate_runtime_tree(&root, &[]).unwrap();

            std::fs::write(&binary, thin_macho(0x0100_0007)).unwrap();
            assert!(matches!(
                validate_runtime_tree(&root, &[]),
                Err(ArchiveError::RequiredFile(_))
            ));
        }
    }

    #[cfg(unix)]
    #[test]
    fn smoke_timeout_terminates_the_entire_process_group() {
        let temp = tempfile::tempdir().unwrap();
        let script = temp.path().join("hang.sh");
        let child_pid = temp.path().join("child.pid");
        std::fs::write(
            &script,
            format!(
                "#!/bin/sh\ntrap '' TERM\nsleep 30 &\necho $! > '{}'\nwait\n",
                child_pid.display()
            ),
        )
        .unwrap();
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
        let cancelled = AtomicBool::new(false);
        let started = Instant::now();
        assert!(matches!(
            smoke_version_with_timeout(&script, &cancelled, Duration::from_secs(2)),
            Err(ArchiveError::SmokeTimeout(_))
        ));
        assert!(started.elapsed() < Duration::from_secs(5));
        let pid: i32 = std::fs::read_to_string(&child_pid)
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        std::thread::sleep(Duration::from_millis(50));
        assert_eq!(unsafe { nix::libc::kill(pid, 0) }, -1);
    }

    #[test]
    fn tree_hash_is_unambiguous_and_rejects_non_utf8_paths() {
        let first = tempfile::tempdir().unwrap();
        let second = tempfile::tempdir().unwrap();
        std::fs::write(first.path().join("a"), b"x\0b\0").unwrap();
        std::fs::write(second.path().join("a"), b"x").unwrap();
        std::fs::write(second.path().join("b"), b"").unwrap();
        assert_ne!(
            runtime_tree_sha256(&std::fs::canonicalize(first.path()).unwrap()).unwrap(),
            runtime_tree_sha256(&std::fs::canonicalize(second.path()).unwrap()).unwrap()
        );

        assert!(validate_tree_utf8(b"bad\xffname", "tree_non_utf8").is_err());
    }

    #[test]
    fn cancellable_reader_interrupts_inside_a_stream_read() {
        struct CancelsOnRead<'a> {
            cancellation: &'a AtomicBool,
        }

        impl Read for CancelsOnRead<'_> {
            fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
                buffer[0] = b'x';
                self.cancellation.store(true, Ordering::Release);
                Ok(1)
            }
        }

        let cancellation = AtomicBool::new(false);
        let mut reader = CancellableReader::new(
            CancelsOnRead {
                cancellation: &cancellation,
            },
            &cancellation,
            16,
        );
        let error = reader.read(&mut [0_u8; 8]).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::Interrupted);
    }
}
