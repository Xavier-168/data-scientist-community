//! Windows 安装管理：NSIS 承担安装，应用内 install() 为即成功路径。
//!
//! NSIS 安装器（tauri bundle targets 含 "nsis"，installMode=currentUser）
//! 负责把应用放进 %LOCALAPPDATA%\Programs 并注册卸载信息；本模块仅保留
//! 与启动编排器约定的取消与空闲接口，install() 直接返回 AlreadyInstalled。

use std::{
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering as AtomicOrdering},
        Arc, Condvar, Mutex as StdMutex,
    },
    time::Duration,
};

use crate::{fault_injection::FaultInjection, manifest::VerifiedPackageManifest};

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

#[derive(Clone)]
pub struct InstallManager {
    #[allow(dead_code)]
    source_app: PathBuf,
    #[allow(dead_code)]
    home: PathBuf,
    #[allow(dead_code)]
    manifest: Arc<VerifiedPackageManifest>,
    #[allow(dead_code)]
    faults: FaultInjection,
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
        Self {
            source_app,
            home,
            manifest,
            faults,
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
        let Ok(result) = self.in_flight.1.wait_timeout_while(running, timeout, |running| {
            *running
        }) else {
            return false;
        };
        !*result.0
    }

    pub fn install(&self) -> InstallOutcome {
        if self.cancellation.is_cancelled() {
            return InstallOutcome::Failed("install_cancelled".into());
        }
        InstallOutcome::AlreadyInstalled
    }
}
