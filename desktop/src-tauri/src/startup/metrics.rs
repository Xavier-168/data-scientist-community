use serde::Serialize;
#[cfg(unix)]
use std::fs::File;
use std::{
    collections::HashSet,
    ffi::OsString,
    io::Write,
    path::PathBuf,
    sync::{Arc, Mutex},
    time::Instant,
};
use time::{format_description::well_known::Rfc3339, OffsetDateTime};

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StartupMetricEvent {
    ProcessStarted,
    WindowCreated,
    ReactInteractive,
    CoreReady,
    ApiReady,
    CollectorReady,
    AppInstalled,
}

#[derive(Serialize)]
struct StartupMetricRecord {
    event: StartupMetricEvent,
    elapsed_ms: u128,
    occurred_at: String,
}

#[derive(Clone)]
pub struct StartupMetrics {
    #[cfg(unix)]
    directory: Arc<File>,
    #[cfg(windows)]
    directory: Arc<std::path::PathBuf>,
    file_name: OsString,
    started: Instant,
    seen: Arc<Mutex<HashSet<StartupMetricEvent>>>,
}

impl StartupMetrics {
    #[cfg(unix)]
    pub fn new(path: PathBuf, process_started: Instant) -> Result<Self, String> {
        use rustix::fs::{fstat, open, FileType, Mode, OFlags};

        let parent = path
            .parent()
            .ok_or_else(|| "startup_metrics_parent_missing".to_string())?;
        let file_name = path
            .file_name()
            .ok_or_else(|| "startup_metrics_name_missing".to_string())?
            .to_os_string();
        let directory = File::from(
            open(
                parent,
                OFlags::RDONLY
                    | OFlags::DIRECTORY
                    | OFlags::NOFOLLOW
                    | OFlags::NONBLOCK
                    | OFlags::CLOEXEC,
                Mode::empty(),
            )
            .map_err(|error| std::io::Error::from(error).to_string())?,
        );
        let stat = fstat(&directory).map_err(|error| std::io::Error::from(error).to_string())?;
        if FileType::from_raw_mode(stat.st_mode) != FileType::Directory {
            return Err("startup_metrics_parent_invalid".into());
        }
        let metrics = Self {
            directory: Arc::new(directory),
            file_name,
            started: process_started,
            seen: Arc::new(Mutex::new(HashSet::new())),
        };
        metrics.record(StartupMetricEvent::ProcessStarted)?;
        Ok(metrics)
    }

    /// Windows：以路径保存父目录，追加写 JSONL（无 fd 锚定语义）。
    #[cfg(windows)]
    pub fn new(path: PathBuf, process_started: Instant) -> Result<Self, String> {
        let parent = path
            .parent()
            .ok_or_else(|| "startup_metrics_parent_missing".to_string())?
            .to_path_buf();
        let file_name = path
            .file_name()
            .ok_or_else(|| "startup_metrics_name_missing".to_string())?
            .to_os_string();
        if !parent.is_dir() {
            return Err("startup_metrics_parent_invalid".into());
        }
        let metrics = Self {
            directory: Arc::new(parent),
            file_name,
            started: process_started,
            seen: Arc::new(Mutex::new(HashSet::new())),
        };
        metrics.record(StartupMetricEvent::ProcessStarted)?;
        Ok(metrics)
    }

    pub fn record(&self, event: StartupMetricEvent) -> Result<(), String> {
        let mut seen = self
            .seen
            .lock()
            .map_err(|_| "startup_metrics_lock_poisoned".to_string())?;
        if !seen.insert(event) {
            return Ok(());
        }
        let record = StartupMetricRecord {
            event,
            elapsed_ms: self.started.elapsed().as_millis(),
            occurred_at: OffsetDateTime::now_utc()
                .format(&Rfc3339)
                .map_err(|error| error.to_string())?,
        };
        #[cfg(unix)]
        {
            use rustix::fs::{fchmod, fstat, openat, FileType, Mode, OFlags};
            let mut file = File::from(
                openat(
                    &*self.directory,
                    &self.file_name,
                    OFlags::WRONLY
                        | OFlags::CREATE
                        | OFlags::APPEND
                        | OFlags::NOFOLLOW
                        | OFlags::NONBLOCK
                        | OFlags::CLOEXEC,
                    Mode::from_raw_mode(0o600),
                )
                .map_err(|error| std::io::Error::from(error).to_string())?,
            );
            let stat =
                fstat(&file).map_err(|error| std::io::Error::from(error).to_string())?;
            if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile || stat.st_nlink != 1
            {
                return Err("startup_metrics_file_invalid".into());
            }
            fchmod(&file, Mode::from_raw_mode(0o600))
                .map_err(|error| std::io::Error::from(error).to_string())?;
            serde_json::to_writer(&mut file, &record).map_err(|error| error.to_string())?;
            file.write_all(b"\n").map_err(|error| error.to_string())?;
            file.flush().map_err(|error| error.to_string())
        }
        #[cfg(windows)]
        {
            use std::fs::OpenOptions;
            let target = self.directory.join(&self.file_name);
            let mut file = OpenOptions::new()
                .create(true)
                .append(true)
                .open(&target)
                .map_err(|error| error.to_string())?;
            serde_json::to_writer(&mut file, &record).map_err(|error| error.to_string())?;
            file.write_all(b"\n").map_err(|error| error.to_string())?;
            file.flush().map_err(|error| error.to_string())
        }
    }
}

// symlink 权限断言依赖 Unix 语义
#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use serde_json::Value;
    use std::os::unix::fs::symlink;

    #[test]
    fn writes_path_safe_privacy_jsonl_and_deduplicates_events() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("startup-metrics.jsonl");
        let metrics = StartupMetrics::new(path.clone(), Instant::now()).unwrap();
        metrics.record(StartupMetricEvent::WindowCreated).unwrap();
        metrics.record(StartupMetricEvent::WindowCreated).unwrap();
        let rows: Vec<Value> = std::fs::read_to_string(path)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0]["event"], "process_started");
        assert_eq!(rows[1]["event"], "window_created");
        assert!(rows[1]["elapsed_ms"].as_u64().unwrap() >= rows[0]["elapsed_ms"].as_u64().unwrap());
        for row in rows {
            let object = row.as_object().unwrap();
            assert_eq!(
                object
                    .keys()
                    .cloned()
                    .collect::<std::collections::BTreeSet<_>>(),
                ["elapsed_ms", "event", "occurred_at"]
                    .into_iter()
                    .map(String::from)
                    .collect()
            );
            assert!(
                OffsetDateTime::parse(object["occurred_at"].as_str().unwrap(), &Rfc3339).is_ok()
            );
            let encoded = serde_json::to_string(object).unwrap();
            for forbidden in [
                "http://", "https://", "session", "/Users/", "license", "feishu",
            ] {
                assert!(!encoded.contains(forbidden));
            }
        }
    }

    #[test]
    fn rejects_a_metrics_file_symlink_without_touching_its_target() {
        let temp = tempfile::tempdir().unwrap();
        let escaped = temp.path().join("escaped.jsonl");
        let path = temp.path().join("startup-metrics.jsonl");
        std::fs::write(&escaped, b"private\n").unwrap();
        symlink(&escaped, &path).unwrap();

        assert!(StartupMetrics::new(path, Instant::now()).is_err());
        assert_eq!(std::fs::read(&escaped).unwrap(), b"private\n");
    }
}
