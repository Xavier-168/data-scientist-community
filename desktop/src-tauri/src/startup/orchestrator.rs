use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};

use tauri::{async_runtime::JoinHandle, AppHandle, Emitter};
use tokio::{sync::Mutex, task::JoinSet};

use crate::{
    install::{InstallManager, InstallOutcome},
    manifest::VerifiedPackageManifest,
    runtime::{RuntimeKind, RuntimeManager, RuntimeResolution, VerifiedView, ViewManager},
    sidecar::SidecarSupervisor,
};

use super::{
    metrics::{StartupMetricEvent, StartupMetrics},
    model::{LaneStatus, RecoverableError, RetryStage, StartupPhase, StartupSnapshot},
    store::StartupStore,
};

pub const STARTUP_STATE_EVENT: &str = "startup://state";

fn recancel_if_shutting_down(shutting_down: &AtomicBool, cancel: impl FnOnce()) -> bool {
    if shutting_down.load(Ordering::Acquire) {
        cancel();
        true
    } else {
        false
    }
}

pub trait StartupEventSink: Send + Sync {
    fn emit(&self, snapshot: &StartupSnapshot) -> Result<(), String>;
}

pub struct TauriStartupEventSink {
    app: AppHandle,
}

impl TauriStartupEventSink {
    pub fn new(app: AppHandle) -> Self {
        Self { app }
    }
}

impl StartupEventSink for TauriStartupEventSink {
    fn emit(&self, snapshot: &StartupSnapshot) -> Result<(), String> {
        self.app
            .emit(STARTUP_STATE_EVENT, snapshot.clone())
            .map_err(|error| error.to_string())
    }
}

#[cfg(any(test, feature = "test-harness"))]
#[derive(Default)]
pub struct RecordingSink {
    snapshots: std::sync::Mutex<Vec<StartupSnapshot>>,
}

#[cfg(any(test, feature = "test-harness"))]
impl RecordingSink {
    pub fn snapshots(&self) -> Vec<StartupSnapshot> {
        self.snapshots
            .lock()
            .expect("recording sink lock poisoned")
            .clone()
    }
}

#[cfg(any(test, feature = "test-harness"))]
impl StartupEventSink for RecordingSink {
    fn emit(&self, snapshot: &StartupSnapshot) -> Result<(), String> {
        self.snapshots
            .lock()
            .map_err(|_| "recording_sink_lock_poisoned".to_string())?
            .push(snapshot.clone());
        Ok(())
    }
}

pub struct StartupDependencies {
    pub events: Arc<dyn StartupEventSink>,
    pub store: StartupStore,
    pub runtimes: RuntimeManager,
    pub views: ViewManager,
    pub sidecar: SidecarSupervisor,
    pub installer: InstallManager,
    pub metrics: StartupMetrics,
    pub manifest: Arc<VerifiedPackageManifest>,
}

#[cfg(any(test, feature = "test-harness"))]
pub struct TestRunResult {
    pub snapshot: StartupSnapshot,
    pub no_live_join_handles: bool,
}

pub struct StartupOrchestrator {
    events: Arc<dyn StartupEventSink>,
    store: StartupStore,
    runtimes: RuntimeManager,
    views: ViewManager,
    sidecar: SidecarSupervisor,
    installer: InstallManager,
    metrics: StartupMetrics,
    core: Arc<Mutex<Option<RuntimeResolution>>>,
    collector: Arc<Mutex<Option<RuntimeResolution>>>,
    active_view: Arc<Mutex<Option<VerifiedView>>>,
    lifecycle: Mutex<()>,
    transition: Mutex<()>,
    run_task: std::sync::Mutex<Option<JoinHandle<()>>>,
    collector_tasks: Mutex<JoinSet<Result<RuntimeResolution, String>>>,
    install_tasks: Mutex<JoinSet<InstallOutcome>>,
    shutting_down: AtomicBool,
    #[cfg(test)]
    retry_transition_waiting: tokio::sync::Notify,
    #[cfg(test)]
    emit_lifecycle_waiting: tokio::sync::Notify,
    #[cfg(test)]
    shutdown_lifecycle_waiting: tokio::sync::Notify,
    #[cfg(test)]
    metric_gate_enabled: AtomicBool,
    #[cfg(test)]
    metric_gate_entered: tokio::sync::Notify,
    #[cfg(test)]
    metric_gate_release: tokio::sync::Notify,
}

impl StartupOrchestrator {
    pub fn new(dependencies: StartupDependencies) -> Self {
        Self {
            events: dependencies.events,
            store: dependencies.store,
            runtimes: dependencies.runtimes,
            views: dependencies.views,
            sidecar: dependencies.sidecar,
            installer: dependencies.installer,
            metrics: dependencies.metrics,
            core: Arc::new(Mutex::new(None)),
            collector: Arc::new(Mutex::new(None)),
            active_view: Arc::new(Mutex::new(None)),
            lifecycle: Mutex::new(()),
            transition: Mutex::new(()),
            run_task: std::sync::Mutex::new(None),
            collector_tasks: Mutex::new(JoinSet::new()),
            install_tasks: Mutex::new(JoinSet::new()),
            shutting_down: AtomicBool::new(false),
            #[cfg(test)]
            retry_transition_waiting: tokio::sync::Notify::new(),
            #[cfg(test)]
            emit_lifecycle_waiting: tokio::sync::Notify::new(),
            #[cfg(test)]
            shutdown_lifecycle_waiting: tokio::sync::Notify::new(),
            #[cfg(test)]
            metric_gate_enabled: AtomicBool::new(false),
            #[cfg(test)]
            metric_gate_entered: tokio::sync::Notify::new(),
            #[cfg(test)]
            metric_gate_release: tokio::sync::Notify::new(),
        }
    }

    fn record_metric(&self, event: StartupMetricEvent) {
        if let Err(error) = self.metrics.record(event) {
            tracing::warn!(%error, ?event, "startup metric write failed");
        }
    }

    async fn emit_inner(
        &self,
        mutate: impl FnOnce(&mut StartupSnapshot),
        metric: Option<StartupMetricEvent>,
    ) -> bool {
        #[cfg(test)]
        self.emit_lifecycle_waiting.notify_one();
        let _lifecycle = self.lifecycle.lock().await;
        if self.shutting_down.load(Ordering::Acquire) {
            return false;
        }
        let snapshot = self.store.update(mutate).await;
        if let Err(error) = self.events.emit(&snapshot) {
            tracing::error!(%error, "startup state emit failed");
        }
        if let Some(metric) = metric {
            #[cfg(test)]
            if self.metric_gate_enabled.load(Ordering::Acquire) {
                self.metric_gate_entered.notify_one();
                self.metric_gate_release.notified().await;
            }
            self.record_metric(metric);
        }
        true
    }

    async fn emit(&self, mutate: impl FnOnce(&mut StartupSnapshot)) -> bool {
        self.emit_inner(mutate, None).await
    }

    async fn emit_with_metric(
        &self,
        event: StartupMetricEvent,
        mutate: impl FnOnce(&mut StartupSnapshot),
    ) -> bool {
        self.emit_inner(mutate, Some(event)).await
    }

    async fn prepare_core(&self) -> Result<RuntimeResolution, String> {
        self.emit(|state| {
            state.phase = StartupPhase::CoreChecking;
            state.core.status = LaneStatus::Checking;
            state.core.percent = 5;
            state.core.message = "正在检查核心运行时".into();
        })
        .await;
        self.emit(|state| {
            state.phase = StartupPhase::CorePreparing;
            state.core.status = LaneStatus::Preparing;
            state.core.percent = 20;
            state.core.message = "正在准备核心运行时".into();
        })
        .await;
        self.runtimes.ensure(RuntimeKind::Core).await
    }

    async fn prepare_collector(&self) -> Result<RuntimeResolution, String> {
        self.emit(|state| {
            if !state.api_ready {
                state.phase = StartupPhase::CollectorPreparing;
            }
            state.collector.status = LaneStatus::Preparing;
            state.collector.percent = 10;
            state.collector.message = "正在后台准备采集引擎".into();
        })
        .await;
        self.runtimes.ensure(RuntimeKind::Collector).await
    }

    async fn spawn_collector(self: &Arc<Self>) {
        let this = Arc::clone(self);
        self.collector_tasks
            .lock()
            .await
            .spawn(async move { this.prepare_collector().await });
    }

    async fn spawn_install(&self) {
        self.emit(|state| {
            state.install.status = LaneStatus::Preparing;
            state.install.percent = 10;
            state.install.message = "正在后台安装应用".into();
        })
        .await;
        let installer = self.installer.clone();
        self.install_tasks.lock().await.spawn(async move {
            tokio::task::spawn_blocking(move || installer.install())
                .await
                .unwrap_or_else(|error| {
                    InstallOutcome::Failed(format!("install_task_failed:{error}"))
                })
        });
    }

    async fn next_collector(&self) -> Result<RuntimeResolution, String> {
        match self.collector_tasks.lock().await.join_next().await {
            Some(Ok(result)) => result,
            Some(Err(error)) => Err(format!("collector_task_failed:{error}")),
            None => Err("collector_task_missing".into()),
        }
    }

    async fn next_install(&self) -> Result<InstallOutcome, String> {
        match self.install_tasks.lock().await.join_next().await {
            Some(Ok(outcome)) => Ok(outcome),
            Some(Err(error)) => Err(format!("install_task_failed:{error}")),
            None => Err("install_task_missing".into()),
        }
    }

    async fn fail(&self, stage: RetryStage, code: &str, message: String, preserve_existing: bool) {
        self.emit(|state| {
            let lane = match stage {
                RetryStage::Core | RetryStage::Sidecar => &mut state.core,
                RetryStage::Collector => &mut state.collector,
                RetryStage::Install => &mut state.install,
            };
            lane.status = LaneStatus::Failed;
            lane.message = message.clone();
            state.phase = StartupPhase::Degraded;
            if !preserve_existing || state.recoverable_error.is_none() {
                state.recoverable_error = Some(RecoverableError {
                    stage,
                    code: code.into(),
                    message,
                    retryable: true,
                });
            }
            if matches!(stage, RetryStage::Core | RetryStage::Sidecar) {
                state.api_ready = false;
            }
            state.can_collect = state.api_ready
                && state.collector.status == LaneStatus::Ready
                && !matches!(
                    stage,
                    RetryStage::Core | RetryStage::Collector | RetryStage::Sidecar
                );
        })
        .await;
    }

    async fn mark_collector(
        &self,
        result: Result<RuntimeResolution, String>,
        core: Option<&RuntimeResolution>,
        preserve_error: bool,
    ) {
        match result {
            Ok(collector) => {
                *self.collector.lock().await = Some(collector.clone());
                if let Some(core) = core {
                    match self.views.activate_collector(core, &collector) {
                        Ok(view) => *self.active_view.lock().await = Some(view),
                        Err(error) => {
                            self.fail(
                                RetryStage::Collector,
                                "collector_view_failed",
                                error,
                                preserve_error,
                            )
                            .await;
                            return;
                        }
                    }
                }
                self.emit_with_metric(StartupMetricEvent::CollectorReady, |state| {
                    state.collector.status = LaneStatus::Ready;
                    state.collector.percent = 100;
                    state.collector.message = if core.is_some() {
                        "采集引擎已就绪"
                    } else {
                        "采集引擎已准备，等待核心服务"
                    }
                    .into();
                    state.can_collect = core.is_some() && state.api_ready;
                })
                .await;
            }
            Err(error) => {
                self.fail(
                    RetryStage::Collector,
                    "collector_prepare_failed",
                    error,
                    preserve_error,
                )
                .await;
            }
        }
    }

    async fn mark_install(&self, outcome: Result<InstallOutcome, String>, preserve_error: bool) {
        match outcome {
            Ok(InstallOutcome::Installed(_)) | Ok(InstallOutcome::AlreadyInstalled) => {
                self.emit_with_metric(StartupMetricEvent::AppInstalled, |state| {
                    state.install.status = LaneStatus::Ready;
                    state.install.percent = 100;
                    state.install.message = "应用安装就绪".into();
                })
                .await;
            }
            Ok(InstallOutcome::Failed(error)) | Err(error) => {
                self.fail(
                    RetryStage::Install,
                    "app_install_failed",
                    error,
                    preserve_error,
                )
                .await;
            }
        }
    }

    async fn settle_background(&self, core: Option<&RuntimeResolution>, preserve_error: bool) {
        let collector = self.next_collector().await;
        let install = self.next_install().await;
        self.mark_collector(collector, core, preserve_error).await;
        self.mark_install(
            install,
            preserve_error || self.store.snapshot().recoverable_error.is_some(),
        )
        .await;
    }

    fn next_error(state: &StartupSnapshot) -> Option<RecoverableError> {
        if state.core.status == LaneStatus::Failed {
            Some(RecoverableError {
                stage: RetryStage::Core,
                code: "core_failed".into(),
                message: state.core.message.clone(),
                retryable: true,
            })
        } else if state.collector.status == LaneStatus::Failed {
            Some(RecoverableError {
                stage: RetryStage::Collector,
                code: "collector_failed".into(),
                message: state.collector.message.clone(),
                retryable: true,
            })
        } else if state.install.status == LaneStatus::Failed {
            Some(RecoverableError {
                stage: RetryStage::Install,
                code: "install_failed".into(),
                message: state.install.message.clone(),
                retryable: true,
            })
        } else {
            None
        }
    }

    async fn finish(&self) {
        self.emit(|state| {
            let complete = state.api_ready
                && state.core.status == LaneStatus::Ready
                && state.collector.status == LaneStatus::Ready
                && state.install.status == LaneStatus::Ready;
            state.phase = if complete {
                StartupPhase::Ready
            } else {
                StartupPhase::Degraded
            };
            state.can_collect = state.api_ready && state.collector.status == LaneStatus::Ready;
            if complete {
                state.recoverable_error = None;
            } else if state.recoverable_error.is_none() {
                state.recoverable_error = Self::next_error(state);
            }
        })
        .await;
    }

    async fn run(self: &Arc<Self>) {
        if self.shutting_down.load(Ordering::Acquire) {
            return;
        }
        let _transition = self.transition.lock().await;
        if self.shutting_down.load(Ordering::Acquire) {
            return;
        }
        self.installer.reset_cancellation();
        self.runtimes.reset_cancellation();
        if recancel_if_shutting_down(&self.shutting_down, || {
            self.installer.cancel();
            self.runtimes.cancel();
        }) {
            return;
        }
        self.spawn_collector().await;
        self.spawn_install().await;

        let core = match self.prepare_core().await {
            Ok(core) => core,
            Err(error) => {
                self.fail(RetryStage::Core, "core_prepare_failed", error, false)
                    .await;
                self.settle_background(None, true).await;
                self.finish().await;
                return;
            }
        };
        *self.core.lock().await = Some(core.clone());
        let view = match self.views.activate_core(&core) {
            Ok(view) => view,
            Err(error) => {
                self.fail(RetryStage::Core, "core_view_failed", error, false)
                    .await;
                self.settle_background(None, true).await;
                self.finish().await;
                return;
            }
        };
        *self.active_view.lock().await = Some(view.clone());
        self.emit_with_metric(StartupMetricEvent::CoreReady, |state| {
            state.phase = StartupPhase::ApiStarting;
            state.core.status = LaneStatus::Ready;
            state.core.percent = 100;
            state.core.message = "核心运行时已就绪".into();
        })
        .await;
        if let Err(error) = self.sidecar.start(&view).await {
            self.fail(RetryStage::Sidecar, "sidecar_start_failed", error, false)
                .await;
            self.settle_background(Some(&core), true).await;
            self.finish().await;
            return;
        }
        self.emit_with_metric(StartupMetricEvent::ApiReady, |state| {
            state.phase = StartupPhase::ApiReady;
            state.api_ready = true;
            state.core.status = LaneStatus::Ready;
        })
        .await;
        self.settle_background(Some(&core), false).await;
        self.finish().await;
    }

    pub fn launch(self: &Arc<Self>) {
        if self.shutting_down.load(Ordering::Acquire) {
            return;
        }
        let mut slot = self
            .run_task
            .lock()
            .expect("startup run task lock poisoned");
        if slot
            .as_ref()
            .is_some_and(|task| !task.inner().is_finished())
        {
            return;
        }
        let this = Arc::clone(self);
        *slot = Some(tauri::async_runtime::spawn(async move {
            this.run().await;
        }));
    }

    pub async fn active_view(&self) -> Option<VerifiedView> {
        self.active_view.lock().await.clone()
    }

    async fn clear_error_for(&self, completed: RetryStage) {
        self.emit(|state| {
            if state
                .recoverable_error
                .as_ref()
                .is_some_and(|error| error.stage == completed)
            {
                state.recoverable_error = Self::next_error(state);
            }
        })
        .await;
    }

    pub async fn retry(&self, stage: RetryStage) -> Result<(), String> {
        if self.shutting_down.load(Ordering::Acquire) {
            return Err("startup_shutting_down".into());
        }
        #[cfg(test)]
        self.retry_transition_waiting.notify_one();
        let _transition = self.transition.lock().await;
        if self.shutting_down.load(Ordering::Acquire) {
            return Err("startup_shutting_down".into());
        }
        let result = match stage {
            RetryStage::Core => {
                async {
                    self.emit(|state| {
                        state.api_ready = false;
                        state.can_collect = false;
                    })
                    .await;
                    self.runtimes.reset_cancellation();
                    if recancel_if_shutting_down(&self.shutting_down, || self.runtimes.cancel()) {
                        return Err("startup_shutting_down".into());
                    }
                    let core = self.prepare_core().await?;
                    if recancel_if_shutting_down(&self.shutting_down, || self.runtimes.cancel()) {
                        return Err("startup_shutting_down".into());
                    }
                    let collector = self.collector.lock().await.clone();
                    let view = match collector.as_ref() {
                        Some(collector) => self.views.activate_collector(&core, collector),
                        None => self.views.activate_core(&core),
                    }?;
                    if self.shutting_down.load(Ordering::Acquire) {
                        return Err("startup_shutting_down".into());
                    }
                    *self.core.lock().await = Some(core);
                    *self.active_view.lock().await = Some(view.clone());
                    self.sidecar.stop().await?;
                    if self.shutting_down.load(Ordering::Acquire) {
                        return Err("startup_shutting_down".into());
                    }
                    self.sidecar.start(&view).await?;
                    if self.shutting_down.load(Ordering::Acquire) {
                        self.sidecar.stop().await?;
                        return Err("startup_shutting_down".into());
                    }
                    self.emit(|state| {
                        state.api_ready = true;
                        state.core.status = LaneStatus::Ready;
                        state.core.percent = 100;
                    })
                    .await;
                    Ok(())
                }
                .await
            }
            RetryStage::Collector => {
                async {
                    self.emit(|state| state.can_collect = false).await;
                    self.runtimes.reset_cancellation();
                    if recancel_if_shutting_down(&self.shutting_down, || self.runtimes.cancel()) {
                        return Err("startup_shutting_down".into());
                    }
                    let collector = self.prepare_collector().await?;
                    if recancel_if_shutting_down(&self.shutting_down, || self.runtimes.cancel()) {
                        return Err("startup_shutting_down".into());
                    }
                    let core = self.core.lock().await.clone().ok_or("core_not_ready")?;
                    let view = self.views.activate_collector(&core, &collector)?;
                    *self.collector.lock().await = Some(collector);
                    *self.active_view.lock().await = Some(view);
                    self.emit_with_metric(StartupMetricEvent::CollectorReady, |state| {
                        state.collector.status = LaneStatus::Ready;
                        state.collector.percent = 100;
                        state.can_collect = state.api_ready;
                    })
                    .await;
                    Ok(())
                }
                .await
            }
            RetryStage::Install => {
                async {
                    self.installer.reset_cancellation();
                    if recancel_if_shutting_down(&self.shutting_down, || self.installer.cancel()) {
                        return Err("startup_shutting_down".into());
                    }
                    self.spawn_install().await;
                    let outcome = self.next_install().await?;
                    if recancel_if_shutting_down(&self.shutting_down, || self.installer.cancel()) {
                        return Err("startup_shutting_down".into());
                    }
                    match outcome {
                        ready @ (InstallOutcome::Installed(_)
                        | InstallOutcome::AlreadyInstalled) => {
                            self.mark_install(Ok(ready), false).await;
                            Ok(())
                        }
                        InstallOutcome::Failed(error) => Err(error),
                    }
                }
                .await
            }
            RetryStage::Sidecar => {
                async {
                    self.emit(|state| {
                        state.api_ready = false;
                        state.can_collect = false;
                    })
                    .await;
                    let view = self
                        .active_view
                        .lock()
                        .await
                        .clone()
                        .ok_or("runtime_view_not_ready")?;
                    self.sidecar.stop().await?;
                    if self.shutting_down.load(Ordering::Acquire) {
                        return Err("startup_shutting_down".into());
                    }
                    self.sidecar.start(&view).await?;
                    if self.shutting_down.load(Ordering::Acquire) {
                        self.sidecar.stop().await?;
                        return Err("startup_shutting_down".into());
                    }
                    self.emit(|state| {
                        state.api_ready = true;
                        state.core.status = LaneStatus::Ready;
                        state.core.percent = 100;
                    })
                    .await;
                    Ok(())
                }
                .await
            }
        };

        if self.shutting_down.load(Ordering::Acquire)
            || result
                .as_ref()
                .is_err_and(|error| error == "startup_shutting_down")
        {
            return Err("startup_shutting_down".into());
        }
        if let Err(error) = result {
            self.fail(stage, "retry_failed", error.clone(), false).await;
            self.finish().await;
            return Err(error);
        }
        if self.shutting_down.load(Ordering::Acquire) {
            return Err("startup_shutting_down".into());
        }
        self.clear_error_for(stage).await;
        self.finish().await;
        Ok(())
    }

    async fn abort_join_set<T: Send + 'static>(
        tasks: &Mutex<JoinSet<T>>,
        label: &str,
    ) -> Result<(), String> {
        let mut tasks = tasks.lock().await;
        tasks.abort_all();
        tokio::time::timeout(Duration::from_secs(3), async {
            while let Some(result) = tasks.join_next().await {
                if let Err(error) = result {
                    if !error.is_cancelled() {
                        return Err(format!("{label}_task_failed:{error}"));
                    }
                }
            }
            Ok(())
        })
        .await
        .map_err(|_| format!("{label}_cancel_timeout"))?
    }

    pub async fn shutdown(&self) -> Result<(), String> {
        #[cfg(test)]
        self.shutdown_lifecycle_waiting.notify_one();
        {
            let _lifecycle = self.lifecycle.lock().await;
            self.shutting_down.store(true, Ordering::Release);
        }
        self.installer.cancel();
        self.runtimes.cancel();
        let run = self
            .run_task
            .lock()
            .expect("startup run task lock poisoned")
            .take();
        let mut failures = Vec::new();
        if let Some(handle) = run {
            handle.abort();
            let _ = handle.await;
        }
        if let Err(error) = self.sidecar.stop().await {
            failures.push(error);
        }
        let _transition = self.transition.lock().await;
        if let Err(error) = Self::abort_join_set(&self.collector_tasks, "collector").await {
            failures.push(error);
        }
        if let Err(error) = Self::abort_join_set(&self.install_tasks, "install").await {
            failures.push(error);
        }
        let installer = self.installer.clone();
        match tokio::task::spawn_blocking(move || installer.wait_for_idle(Duration::from_secs(3)))
            .await
        {
            Ok(true) => {}
            Ok(false) => failures.push("install_cancel_timeout".into()),
            Err(error) => failures.push(format!("install_cancel_join_failed:{error}")),
        }
        if let Err(error) = self.sidecar.stop().await {
            failures.push(error);
        }
        if failures.is_empty() {
            Ok(())
        } else {
            Err(failures.join("; "))
        }
    }

    #[cfg(any(test, feature = "test-harness"))]
    pub async fn run_for_test(self: &Arc<Self>) -> TestRunResult {
        self.run().await;
        let no_live_join_handles = self.collector_tasks.lock().await.is_empty()
            && self.install_tasks.lock().await.is_empty()
            && self
                .run_task
                .lock()
                .expect("startup run task lock poisoned")
                .as_ref()
                .is_none_or(|task| task.inner().is_finished());
        TestRunResult {
            snapshot: self.store.snapshot(),
            no_live_join_handles,
        }
    }

    #[cfg(any(test, feature = "test-harness"))]
    pub fn snapshot_for_test(&self) -> StartupSnapshot {
        self.store.snapshot()
    }

    #[cfg(any(test, feature = "test-harness"))]
    pub async fn no_background_tasks_for_test(&self) -> bool {
        self.collector_tasks.lock().await.is_empty() && self.install_tasks.lock().await.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use std::{fs, sync::atomic::AtomicUsize, time::Instant};

    use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
    use ed25519_dalek::{
        pkcs8::{spki::der::pem::LineEnding, EncodePublicKey},
        Signer, SigningKey,
    };
    use serde_json::{json, Value};

    use super::*;

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

    fn test_manifest() -> Arc<VerifiedPackageManifest> {
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
        // 清单架构跟随构建目标（macOS=arm64 / Windows=x86_64）
        let target_arch = if cfg!(windows) { "x86_64" } else { "arm64" };
        let payload = json!({
            "arch": target_arch,
            "build_version": "20260713",
            "key_id": "test-key",
            "package_id": format!(
                "data-scientist-community-{}-{}",
                if cfg!(windows) { "win" } else { "mac" },
                target_arch
            ),
            "runtimes": {
                "core": descriptor("core", vec!["frontend-compat/progress.html", "scripts/_run.py"]),
                "collector": descriptor("collector", vec!["node_modules/playwright/package.json", "scripts/douyin_export.mjs"]),
            }
        });
        let signing = SigningKey::from_bytes(&[61_u8; 32]);
        let mut canonical = String::new();
        canonical_json(&payload, &mut canonical);
        let signature = URL_SAFE_NO_PAD.encode(signing.sign(canonical.as_bytes()).to_bytes());
        let signed = serde_json::to_vec(&json!({
            "payload": payload,
            "signature": signature,
        }))
        .unwrap();
        let public_key = signing
            .verifying_key()
            .to_public_key_pem(LineEnding::LF)
            .unwrap();
        let keys = serde_json::to_vec(&json!({
            "active_key_id": "test-key",
            "keys": [{"key_id": "test-key", "public_key_pem": public_key}],
        }))
        .unwrap();
        Arc::new(VerifiedPackageManifest::from_signed(&signed, &keys).unwrap())
    }

    fn test_orchestrator() -> (
        Arc<StartupOrchestrator>,
        Arc<RecordingSink>,
        StartupStore,
        tempfile::TempDir,
    ) {
        let temp = tempfile::tempdir().unwrap();
        let state_root = temp.path().join("state");
        let resource_root = temp.path().join("resources");
        let home = temp.path().join("home");
        fs::create_dir(&state_root).unwrap();
        fs::create_dir(&resource_root).unwrap();
        fs::create_dir(&home).unwrap();
        let manifest = test_manifest();
        let events = Arc::new(RecordingSink::default());
        let store = StartupStore::default();
        let runtimes = RuntimeManager::new(
            state_root.clone(),
            resource_root.clone(),
            Arc::clone(&manifest),
            crate::fault_injection::FaultInjection::default(),
        )
        .unwrap();
        let views = ViewManager::new(
            state_root.clone(),
            resource_root.clone(),
            Arc::clone(&manifest),
        )
        .unwrap();
        let sidecar = SidecarSupervisor::new(state_root.clone(), Arc::clone(&manifest)).unwrap();
        let installer = InstallManager::new(
            temp.path().join("source.app"),
            home,
            Arc::clone(&manifest),
            crate::fault_injection::FaultInjection::default(),
        );
        let metrics =
            StartupMetrics::new(state_root.join("startup-metrics.jsonl"), Instant::now()).unwrap();
        let orchestrator = Arc::new(StartupOrchestrator::new(StartupDependencies {
            events: events.clone(),
            store: store.clone(),
            runtimes,
            views,
            sidecar,
            installer,
            metrics,
            manifest,
        }));
        (orchestrator, events, store, temp)
    }

    #[test]
    fn uses_the_canonical_startup_state_event_name() {
        assert_eq!(STARTUP_STATE_EVENT, "startup://state");
    }

    #[test]
    fn shutdown_observed_after_reset_recancels_before_background_spawn() {
        let shutting_down = AtomicBool::new(true);
        let cancellations = AtomicUsize::new(0);

        let aborted = recancel_if_shutting_down(&shutting_down, || {
            cancellations.fetch_add(1, Ordering::Relaxed);
        });

        assert!(aborted);
        assert_eq!(cancellations.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn active_state_after_reset_does_not_cancel_or_abort() {
        let shutting_down = AtomicBool::new(false);
        let cancellations = AtomicUsize::new(0);

        let aborted = recancel_if_shutting_down(&shutting_down, || {
            cancellations.fetch_add(1, Ordering::Relaxed);
        });

        assert!(!aborted);
        assert_eq!(cancellations.load(Ordering::Relaxed), 0);
    }

    #[tokio::test]
    async fn shutdown_waits_for_queued_retry_and_freezes_state_after_return() {
        let (orchestrator, events, store, _temp) = test_orchestrator();
        let transition = orchestrator.transition.lock().await;
        let retry_orchestrator = Arc::clone(&orchestrator);
        let retry =
            tokio::spawn(async move { retry_orchestrator.retry(RetryStage::Install).await });
        orchestrator.retry_transition_waiting.notified().await;

        let shutdown_orchestrator = Arc::clone(&orchestrator);
        let mut shutdown = tokio::spawn(async move { shutdown_orchestrator.shutdown().await });
        assert!(
            tokio::time::timeout(Duration::from_millis(50), &mut shutdown)
                .await
                .is_err()
        );

        drop(transition);
        assert_eq!(retry.await.unwrap().unwrap_err(), "startup_shutting_down");
        shutdown.await.unwrap().unwrap();
        let frozen = store.snapshot();
        let emitted = events.snapshots();

        tokio::time::sleep(Duration::from_millis(20)).await;
        assert_eq!(store.snapshot(), frozen);
        assert_eq!(events.snapshots(), emitted);
        assert!(!frozen.api_ready);
        assert_ne!(frozen.core.status, LaneStatus::Ready);
        assert_ne!(frozen.collector.status, LaneStatus::Ready);
        assert_ne!(frozen.install.status, LaneStatus::Ready);
    }

    #[tokio::test]
    async fn shutdown_linearizes_before_queued_ready_emit_and_metric() {
        let (orchestrator, events, store, temp) = test_orchestrator();
        let metrics_path = temp.path().join("state/startup-metrics.jsonl");
        let snapshot_before = store.snapshot();
        let events_before = events.snapshots();
        let metrics_before = fs::read(&metrics_path).unwrap();
        let lifecycle = orchestrator.lifecycle.lock().await;

        let shutdown_orchestrator = Arc::clone(&orchestrator);
        let shutdown = tokio::spawn(async move { shutdown_orchestrator.shutdown().await });
        orchestrator.shutdown_lifecycle_waiting.notified().await;

        let emit_orchestrator = Arc::clone(&orchestrator);
        let emitted = tokio::spawn(async move {
            emit_orchestrator
                .emit_with_metric(StartupMetricEvent::ApiReady, |state| {
                    state.phase = StartupPhase::ApiReady;
                    state.api_ready = true;
                    state.core.status = LaneStatus::Ready;
                })
                .await
        });
        orchestrator.emit_lifecycle_waiting.notified().await;

        drop(lifecycle);
        shutdown.await.unwrap().unwrap();
        assert!(!emitted.await.unwrap());
        assert_eq!(store.snapshot(), snapshot_before);
        assert_eq!(events.snapshots(), events_before);
        assert_eq!(fs::read(metrics_path).unwrap(), metrics_before);
    }

    #[tokio::test]
    async fn ready_event_and_metric_finish_before_queued_shutdown_returns() {
        let (orchestrator, events, store, temp) = test_orchestrator();
        let metrics_path = temp.path().join("state/startup-metrics.jsonl");
        orchestrator
            .metric_gate_enabled
            .store(true, Ordering::Release);

        let emit_orchestrator = Arc::clone(&orchestrator);
        let emitted = tokio::spawn(async move {
            emit_orchestrator
                .emit_with_metric(StartupMetricEvent::ApiReady, |state| {
                    state.phase = StartupPhase::ApiReady;
                    state.api_ready = true;
                    state.core.status = LaneStatus::Ready;
                })
                .await
        });
        orchestrator.metric_gate_entered.notified().await;

        let shutdown_orchestrator = Arc::clone(&orchestrator);
        let mut shutdown = tokio::spawn(async move { shutdown_orchestrator.shutdown().await });
        orchestrator.shutdown_lifecycle_waiting.notified().await;
        assert!(
            tokio::time::timeout(Duration::from_millis(50), &mut shutdown)
                .await
                .is_err()
        );

        orchestrator.metric_gate_release.notify_one();
        assert!(emitted.await.unwrap());
        shutdown.await.unwrap().unwrap();
        let frozen = store.snapshot();
        let emitted_events = events.snapshots();
        let metrics = fs::read_to_string(&metrics_path).unwrap();

        assert!(frozen.api_ready);
        assert_eq!(frozen.core.status, LaneStatus::Ready);
        assert_eq!(emitted_events.last(), Some(&frozen));
        assert!(metrics.contains("\"event\":\"api_ready\""));
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert_eq!(store.snapshot(), frozen);
        assert_eq!(events.snapshots(), emitted_events);
        assert_eq!(fs::read_to_string(metrics_path).unwrap(), metrics);
    }
}
