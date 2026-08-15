#![cfg(unix)]

use std::{
    collections::BTreeSet,
    fs,
    io::Cursor,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    sync::{Arc, OnceLock},
    time::{Duration, Instant},
};

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use data_scientist_lib::{
    fault_injection::FaultInjection,
    install::InstallManager,
    manifest::{RuntimeDescriptor, RuntimeSet, VerifiedPackageManifest},
    runtime::{archive, RuntimeKind, RuntimeManager, ViewManager},
    sidecar::SidecarSupervisor,
    startup::{
        metrics::StartupMetrics,
        model::{LaneStatus, RetryStage},
        orchestrator::{RecordingSink, StartupDependencies, StartupOrchestrator},
        store::StartupStore,
    },
};
use ed25519_dalek::{
    pkcs8::{spki::der::pem::LineEnding, EncodePublicKey},
    Signer, SigningKey,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const BUILD_VERSION: &str = "20260710";
const PACKAGE_ID: &str = "data-scientist-community-mac-arm64";
const PYTHON_RELATIVE: &str = "runtime/python-arm64/python/bin/python3";
const NODE_RELATIVE: &str = "runtime/node-arm64/node-v20.15.1-darwin-arm64/bin/node";

async fn test_guard() -> tokio::sync::MutexGuard<'static, ()> {
    static GATE: OnceLock<tokio::sync::Mutex<()>> = OnceLock::new();
    GATE.get_or_init(|| tokio::sync::Mutex::new(()))
        .lock()
        .await
}

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

fn test_python_runtime() -> (Vec<u8>, Vec<u8>, Vec<u8>) {
    let configured = std::env::var_os("YRG_TEST_PYTHON")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../.venv/bin/python")
        });
    let python = fs::canonicalize(&configured)
        .unwrap_or_else(|_| panic!("test python missing: {}", configured.display()));
    let output = std::process::Command::new("file")
        .arg(&python)
        .output()
        .expect("file must inspect the test Python");
    assert!(
        String::from_utf8_lossy(&output.stdout).contains("arm64"),
        "test python is not arm64: {}",
        python.display()
    );
    let runtime_root = python.parent().unwrap().parent().unwrap();
    let library = runtime_root.join("lib/libpython3.12.dylib");
    let pyvenv = format!(
        "home = {}\ninclude-system-site-packages = false\nversion = 3.12.4\n",
        runtime_root.join("bin").display()
    )
    .into_bytes();
    (
        fs::read(python).unwrap(),
        fs::read(library).unwrap(),
        pyvenv,
    )
}

fn append_entry(
    builder: &mut tar::Builder<Vec<u8>>,
    path: &str,
    bytes: &[u8],
    mode: u32,
    entry_type: tar::EntryType,
) {
    let mut header = tar::Header::new_ustar();
    header.set_path(path).unwrap();
    header.set_entry_type(entry_type);
    header.set_size(bytes.len() as u64);
    header.set_mode(mode);
    header.set_uid(0);
    header.set_gid(0);
    header.set_mtime(0);
    header.set_cksum();
    builder.append(&header, Cursor::new(bytes)).unwrap();
}

fn write_pack(
    resources: &Path,
    kind: &str,
    version: &str,
    files: &[(&str, Vec<u8>, u32)],
    required_files: Vec<String>,
) -> RuntimeDescriptor {
    let tree = tempfile::tempdir().unwrap();
    let mut directories = BTreeSet::new();
    for (name, bytes, mode) in files {
        let path = tree.path().join(name);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, bytes).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(*mode)).unwrap();
        let mut parent = Path::new(name).parent();
        while let Some(directory) = parent {
            if directory.as_os_str().is_empty() {
                break;
            }
            directories.insert(directory.to_string_lossy().into_owned());
            parent = directory.parent();
        }
    }
    for directory in &directories {
        fs::set_permissions(
            tree.path().join(directory),
            fs::Permissions::from_mode(0o755),
        )
        .unwrap();
    }
    let tree_sha256 =
        archive::runtime_tree_sha256(&fs::canonicalize(tree.path()).unwrap()).unwrap();

    let mut entries = Vec::new();
    for directory in &directories {
        entries.push((
            directory.clone(),
            Vec::new(),
            0o755,
            tar::EntryType::Directory,
        ));
    }
    for (name, bytes, mode) in files {
        entries.push((
            (*name).to_string(),
            bytes.clone(),
            *mode,
            tar::EntryType::Regular,
        ));
    }
    entries.sort_by(|left, right| left.0.as_bytes().cmp(right.0.as_bytes()));
    let mut builder = tar::Builder::new(Vec::new());
    for (name, bytes, mode, entry_type) in entries {
        append_entry(&mut builder, &name, &bytes, mode, entry_type);
    }
    let tar = builder.into_inner().unwrap();
    let compressed = zstd::stream::encode_all(Cursor::new(tar), 1).unwrap();
    fs::create_dir_all(resources).unwrap();
    let archive = format!("{kind}-runtime.tar.zst");
    fs::write(resources.join(&archive), &compressed).unwrap();
    RuntimeDescriptor {
        version: version.into(),
        archive,
        sha256: hex::encode(Sha256::digest(&compressed)),
        tree_sha256,
        size_bytes: compressed.len() as u64,
        required_files,
    }
}

fn signed_manifest(
    signing: &SigningKey,
    keys: &[u8],
    core: RuntimeDescriptor,
    collector: RuntimeDescriptor,
) -> Arc<VerifiedPackageManifest> {
    let payload = json!({
        "arch": "arm64",
        "build_version": BUILD_VERSION,
        "key_id": "test-key",
        "package_id": PACKAGE_ID,
        "supported_architectures": ["arm64"],
        "runtimes": RuntimeSet { core, collector },
    });
    let mut canonical = String::new();
    canonical_json(&payload, &mut canonical);
    let signature = URL_SAFE_NO_PAD.encode(signing.sign(canonical.as_bytes()).to_bytes());
    let signed = serde_json::to_vec(&json!({"payload": payload, "signature": signature})).unwrap();
    Arc::new(VerifiedPackageManifest::from_signed(&signed, keys).unwrap())
}

struct RuntimeHarness {
    _temp: tempfile::TempDir,
    state_root: PathBuf,
    resources: PathBuf,
    home: PathBuf,
    signing: SigningKey,
    keys: Vec<u8>,
    collector: RuntimeDescriptor,
    manifest: Arc<VerifiedPackageManifest>,
}

impl RuntimeHarness {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let state_root = temp.path().join("state");
        let resources = temp.path().join("resources/runtime-packs");
        let home = temp.path().join("home");
        fs::create_dir_all(&state_root).unwrap();
        fs::create_dir_all(home.join("Applications/数据科学家 Community.app")).unwrap();
        let (python, libpython, pyvenv) = test_python_runtime();
        let core = write_pack(
            &resources,
            "core",
            "core-v2",
            &[
                ("frontend-compat/progress.html", b"fixture".to_vec(), 0o644),
                (PYTHON_RELATIVE, python.clone(), 0o755),
                (
                    "runtime/python-arm64/python/lib/libpython3.12.dylib",
                    libpython.clone(),
                    0o755,
                ),
                (
                    "runtime/python-arm64/python/pyvenv.cfg",
                    pyvenv.clone(),
                    0o644,
                ),
                (
                    "scripts/_run.py",
                    include_bytes!("fixtures/fake_runner.py").to_vec(),
                    0o644,
                ),
            ],
            vec![PYTHON_RELATIVE.into(), "scripts/_run.py".into()],
        );
        let collector = write_pack(
            &resources,
            "collector",
            "collector-v1",
            &[
                ("node_modules/.keep", Vec::new(), 0o644),
                (NODE_RELATIVE, python, 0o755),
                (
                    "runtime/node-arm64/node-v20.15.1-darwin-arm64/lib/libpython3.12.dylib",
                    libpython,
                    0o755,
                ),
                (
                    "runtime/node-arm64/node-v20.15.1-darwin-arm64/pyvenv.cfg",
                    pyvenv,
                    0o644,
                ),
                ("runtime/playwright-browsers/.keep", Vec::new(), 0o644),
                ("scripts/douyin_export.mjs", b"export {}".to_vec(), 0o644),
            ],
            vec![NODE_RELATIVE.into(), "scripts/douyin_export.mjs".into()],
        );
        let signing = SigningKey::from_bytes(&[71_u8; 32]);
        let public_key = signing
            .verifying_key()
            .to_public_key_pem(LineEnding::LF)
            .unwrap();
        let keys = serde_json::to_vec(&json!({
            "active_key_id": "test-key",
            "keys": [{"key_id": "test-key", "public_key_pem": public_key}],
        }))
        .unwrap();
        let manifest = signed_manifest(&signing, &keys, core.clone(), collector.clone());
        Self {
            _temp: temp,
            state_root,
            resources,
            home,
            signing,
            keys,
            collector,
            manifest,
        }
    }

    fn manifest_for(
        &self,
        core: RuntimeDescriptor,
        collector: RuntimeDescriptor,
    ) -> Arc<VerifiedPackageManifest> {
        signed_manifest(&self.signing, &self.keys, core, collector)
    }

    fn manager(
        &self,
        manifest: Arc<VerifiedPackageManifest>,
        faults: FaultInjection,
    ) -> RuntimeManager {
        RuntimeManager::new(
            self.state_root.clone(),
            self.resources.clone(),
            manifest,
            faults,
        )
        .unwrap()
    }

    fn orchestrator(&self, faults: FaultInjection) -> Arc<StartupOrchestrator> {
        let manager = self.manager(self.manifest.clone(), faults.clone());
        let views = ViewManager::new(self.state_root.clone(), self.manifest.clone()).unwrap();
        let sidecar = SidecarSupervisor::new_with_preferred_port(
            self.state_root.clone(),
            self.manifest.clone(),
            0,
        )
        .unwrap();
        let source_app = self.home.join("Applications/数据科学家 Community.app");
        let installer =
            InstallManager::new(source_app, self.home.clone(), self.manifest.clone(), faults);
        Arc::new(StartupOrchestrator::new(StartupDependencies {
            events: Arc::new(RecordingSink::default()),
            store: StartupStore::default(),
            runtimes: manager,
            views,
            sidecar,
            installer,
            metrics: StartupMetrics::new(
                self.state_root.join("startup-metrics.jsonl"),
                Instant::now(),
            )
            .unwrap(),
            manifest: self.manifest.clone(),
        }))
    }
}

#[tokio::test]
async fn stale_tmp_is_deleted_before_successful_retry() {
    let _guard = test_guard().await;
    let fixture = RuntimeHarness::new();
    let manager = fixture.manager(fixture.manifest.clone(), FaultInjection::default());
    let stale = fixture
        .state_root
        .join("runtimes/core/core-v2.tmp-interrupted");
    fs::create_dir_all(&stale).unwrap();
    fs::write(stale.join("partial"), b"partial").unwrap();

    let resolution = manager.ensure(RuntimeKind::Core).await.unwrap();

    assert_eq!(resolution.version(), "core-v2");
    assert!(!stale.exists());
}

#[tokio::test]
async fn bad_new_core_hash_keeps_verified_core_v1() {
    let _guard = test_guard().await;
    let fixture = RuntimeHarness::new();
    let (python, libpython, pyvenv) = test_python_runtime();
    let core_v1 = write_pack(
        &fixture.resources,
        "core",
        "core-v1",
        &[
            (
                "frontend-compat/progress.html",
                b"fixture-v1".to_vec(),
                0o644,
            ),
            (PYTHON_RELATIVE, python.clone(), 0o755),
            (
                "runtime/python-arm64/python/lib/libpython3.12.dylib",
                libpython.clone(),
                0o755,
            ),
            (
                "runtime/python-arm64/python/pyvenv.cfg",
                pyvenv.clone(),
                0o644,
            ),
            (
                "scripts/_run.py",
                include_bytes!("fixtures/fake_runner.py").to_vec(),
                0o644,
            ),
        ],
        vec![PYTHON_RELATIVE.into(), "scripts/_run.py".into()],
    );
    let manifest_v1 = fixture.manifest_for(core_v1.clone(), fixture.collector.clone());
    fixture
        .manager(manifest_v1, FaultInjection::default())
        .ensure(RuntimeKind::Core)
        .await
        .unwrap();
    let core_v2 = write_pack(
        &fixture.resources,
        "core",
        "core-v2",
        &[
            (
                "frontend-compat/progress.html",
                b"fixture-v2".to_vec(),
                0o644,
            ),
            (PYTHON_RELATIVE, python, 0o755),
            (
                "runtime/python-arm64/python/lib/libpython3.12.dylib",
                libpython,
                0o755,
            ),
            ("runtime/python-arm64/python/pyvenv.cfg", pyvenv, 0o644),
            (
                "scripts/_run.py",
                include_bytes!("fixtures/fake_runner.py").to_vec(),
                0o644,
            ),
        ],
        vec![PYTHON_RELATIVE.into(), "scripts/_run.py".into()],
    );
    fs::write(
        fixture.resources.join("core-runtime.tar.zst"),
        b"corrupt-v2",
    )
    .unwrap();
    let manifest_v2 = fixture.manifest_for(core_v2, fixture.collector.clone());

    let resolution = fixture
        .manager(manifest_v2, FaultInjection::default())
        .ensure(RuntimeKind::Core)
        .await
        .unwrap();

    assert_eq!(resolution.version(), "core-v1");
    assert!(resolution.used_fallback());
    assert!(
        resolution.root().is_dir(),
        "fallback root disappeared: {}",
        resolution.root().display()
    );
    for required in &core_v1.required_files {
        assert!(resolution.root().join(required).is_file());
    }
    let canonical_root = fs::canonicalize(resolution.root()).unwrap();
    assert_eq!(
        archive::runtime_tree_sha256(&canonical_root).unwrap(),
        core_v1.tree_sha256
    );
    archive::verify_installed_runtime(&canonical_root, &core_v1).unwrap();
}

#[tokio::test]
async fn unrecoverable_core_failure_still_settles_collector_and_install_lanes() {
    let _guard = test_guard().await;
    let fixture = RuntimeHarness::new();
    let orchestrator = fixture.orchestrator(FaultInjection {
        core_hash: true,
        ..FaultInjection::default()
    });

    let result = orchestrator.run_for_test().await;

    assert_eq!(result.snapshot.core.status, LaneStatus::Failed);
    assert_eq!(result.snapshot.collector.status, LaneStatus::Ready);
    assert_eq!(result.snapshot.install.status, LaneStatus::Ready);
    assert!(!result.snapshot.api_ready);
    assert!(!result.snapshot.can_collect);
    assert!(result.no_live_join_handles);
}

#[tokio::test]
async fn collector_failure_does_not_remove_core_or_api_ready_state() {
    let _guard = test_guard().await;
    let fixture = RuntimeHarness::new();
    fs::write(
        fixture.resources.join("collector-runtime.tar.zst"),
        b"corrupt-collector",
    )
    .unwrap();
    let orchestrator = fixture.orchestrator(FaultInjection::default());

    let result = orchestrator.run_for_test().await;
    let active_view_present = orchestrator.active_view().await.is_some();
    let shutdown = orchestrator.shutdown().await;

    shutdown.unwrap();
    assert_eq!(
        result.snapshot.core.status,
        LaneStatus::Ready,
        "{:?}",
        result.snapshot
    );
    assert_eq!(result.snapshot.collector.status, LaneStatus::Failed);
    assert!(result.snapshot.api_ready);
    assert!(!result.snapshot.can_collect);
    assert!(active_view_present);
}

#[tokio::test]
async fn shutdown_cancels_slow_collector_without_waiting_for_full_delay() {
    let _guard = test_guard().await;
    let fixture = RuntimeHarness::new();
    fixture
        .manager(fixture.manifest.clone(), FaultInjection::default())
        .ensure(RuntimeKind::Core)
        .await
        .unwrap();
    let orchestrator = fixture.orchestrator(FaultInjection {
        collector_delay_ms: 30_000,
        ..FaultInjection::default()
    });
    orchestrator.launch();
    let api_ready = tokio::time::timeout(Duration::from_secs(3), async {
        while !orchestrator.snapshot_for_test().api_ready {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await;

    let started = Instant::now();
    let shutdown = orchestrator.shutdown().await;
    let elapsed = started.elapsed();

    shutdown.unwrap();
    assert!(api_ready.is_ok(), "API did not become ready before timeout");
    assert!(elapsed < Duration::from_secs(3));
    assert!(!fixture.state_root.join("runtimes/sidecar.json").exists());
    assert!(orchestrator.no_background_tasks_for_test().await);
}

#[tokio::test]
async fn failed_sidecar_retry_never_restores_stale_api_state() {
    let _guard = test_guard().await;
    let fixture = RuntimeHarness::new();
    let orchestrator = fixture.orchestrator(FaultInjection::default());
    let result = orchestrator.run_for_test().await;
    let exercise: Result<(), String> = async {
        if !result.snapshot.api_ready {
            return Err("initial_api_not_ready".into());
        }
        let view = orchestrator
            .active_view()
            .await
            .ok_or_else(|| "active_view_missing".to_string())?;
        fs::remove_file(view.path().join(PYTHON_RELATIVE)).map_err(|error| error.to_string())?;
        orchestrator
            .retry(RetryStage::Sidecar)
            .await
            .err()
            .ok_or_else(|| "sidecar_retry_unexpectedly_succeeded".to_string())?;
        Ok(())
    }
    .await;
    let snapshot = orchestrator.snapshot_for_test();
    let shutdown = orchestrator.shutdown().await;

    shutdown.unwrap();
    exercise.unwrap();
    assert_eq!(snapshot.core.status, LaneStatus::Failed);
    assert!(!snapshot.api_ready);
    assert!(!snapshot.can_collect);
}

#[tokio::test]
async fn successful_retry_preserves_another_failed_lane() {
    let _guard = test_guard().await;
    let fixture = RuntimeHarness::new();
    let collector_path = fixture.resources.join("collector-runtime.tar.zst");
    let saved_collector = fs::read(&collector_path).unwrap();
    fs::write(&collector_path, b"corrupt-collector").unwrap();
    let orchestrator = fixture.orchestrator(FaultInjection {
        install: true,
        ..FaultInjection::default()
    });
    let initial = orchestrator.run_for_test().await;
    let restore = fs::write(&collector_path, saved_collector).map_err(|error| error.to_string());
    let retry = match &restore {
        Ok(()) => orchestrator.retry(RetryStage::Collector).await,
        Err(error) => Err(error.clone()),
    };
    let snapshot = orchestrator.snapshot_for_test();
    let shutdown = orchestrator.shutdown().await;

    shutdown.unwrap();
    restore.unwrap();
    retry.unwrap();
    assert_eq!(initial.snapshot.collector.status, LaneStatus::Failed);
    assert_eq!(initial.snapshot.install.status, LaneStatus::Failed);
    assert_eq!(snapshot.collector.status, LaneStatus::Ready);
    assert_eq!(snapshot.install.status, LaneStatus::Failed);
    assert_eq!(
        snapshot.recoverable_error.as_ref().map(|error| error.stage),
        Some(RetryStage::Install)
    );
}
