use std::{
    collections::BTreeSet,
    fs,
    io::Cursor,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use data_scientist_lib::{
    fault_injection::FaultInjection,
    manifest::{RuntimeDescriptor, RuntimeSet, VerifiedPackageManifest},
    runtime::{archive, RuntimeKind, RuntimeManager, VerifiedView, ViewManager},
    sidecar::SidecarSupervisor,
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

fn python_runtime() -> (Vec<u8>, Vec<u8>, Vec<u8>) {
    let configured = std::env::var_os("YRG_TEST_PYTHON")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../.venv/bin/python")
        });
    let python = fs::canonicalize(&configured)
        .unwrap_or_else(|_| panic!("test python missing: {}", configured.display()));
    let root = python.parent().unwrap().parent().unwrap();
    let pyvenv = format!(
        "home = {}\ninclude-system-site-packages = false\nversion = 3.12.4\n",
        root.join("bin").display()
    )
    .into_bytes();
    (
        fs::read(&python).unwrap(),
        fs::read(root.join("lib/libpython3.12.dylib")).unwrap(),
        pyvenv,
    )
}

fn write_pack(
    resources: &Path,
    kind: &str,
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
    for directory in directories {
        entries.push((directory, Vec::new(), 0o755, tar::EntryType::Directory));
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
        let mut header = tar::Header::new_ustar();
        header.set_path(name).unwrap();
        header.set_entry_type(entry_type);
        header.set_size(bytes.len() as u64);
        header.set_mode(mode);
        header.set_uid(0);
        header.set_gid(0);
        header.set_mtime(0);
        header.set_cksum();
        builder.append(&header, Cursor::new(bytes)).unwrap();
    }
    let compressed =
        zstd::stream::encode_all(Cursor::new(builder.into_inner().unwrap()), 1).unwrap();
    fs::create_dir_all(resources).unwrap();
    let archive = format!("{kind}-runtime.tar.zst");
    fs::write(resources.join(&archive), &compressed).unwrap();
    RuntimeDescriptor {
        version: format!("{kind}-v1"),
        archive,
        sha256: hex::encode(Sha256::digest(&compressed)),
        tree_sha256,
        size_bytes: compressed.len() as u64,
        required_files,
    }
}

fn verified_manifest(
    core: RuntimeDescriptor,
    collector: RuntimeDescriptor,
) -> Arc<VerifiedPackageManifest> {
    let signing = SigningKey::from_bytes(&[73_u8; 32]);
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

async fn real_sidecar(
    temp: &tempfile::TempDir,
    ready_delay_ms: u64,
    preferred_port: u16,
) -> (SidecarSupervisor, VerifiedView, PathBuf) {
    let state = temp.path().join("state");
    let resources = temp.path().join("resources/runtime-packs");
    fs::create_dir_all(&state).unwrap();
    if ready_delay_ms != 0 {
        fs::write(state.join("ready-delay-ms"), ready_delay_ms.to_string()).unwrap();
    }
    let (python, libpython, pyvenv) = python_runtime();
    let core = write_pack(
        &resources,
        "core",
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
    let manifest = verified_manifest(core, collector);
    let manager = RuntimeManager::new(
        state.clone(),
        resources,
        manifest.clone(),
        FaultInjection::default(),
    )
    .unwrap();
    let core = manager.ensure(RuntimeKind::Core).await.unwrap();
    let collector = manager.ensure(RuntimeKind::Collector).await.unwrap();
    let views = ViewManager::new(state.clone(), manifest.clone()).unwrap();
    let view = views.activate_collector(&core, &collector).unwrap();
    let supervisor =
        SidecarSupervisor::new_with_preferred_port(state.clone(), manifest, preferred_port)
            .unwrap();
    (supervisor, view, state)
}

#[tokio::test]
async fn occupied_dynamic_preferred_port_uses_another_loopback_port_and_stops_child() {
    let temp = tempfile::tempdir().unwrap();
    let occupied = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let preferred = occupied.local_addr().unwrap().port();
    let (supervisor, view, _) = real_sidecar(&temp, 0, preferred).await;

    let exercise: Result<(u16, bool), String> = async {
        let port = supervisor.start(&view).await?;
        let connection = supervisor
            .connection()
            .await
            .ok_or_else(|| "sidecar_connection_missing".to_string())?;
        let response = reqwest::Client::new()
            .get(format!("http://127.0.0.1:{port}/progress"))
            .header("X-YRG-Session", connection.token())
            .send()
            .await
            .map_err(|error| error.to_string())?;
        Ok((port, response.status().is_success()))
    }
    .await;
    let cleanup = supervisor.stop().await;
    drop(occupied);

    cleanup.unwrap();
    let (port, response_ok) = exercise.unwrap();
    assert_ne!(port, preferred);
    assert!(response_ok);
    assert!(std::net::TcpStream::connect(("127.0.0.1", port)).is_err());
}

#[tokio::test]
async fn stop_waits_for_inflight_start_and_reaps_the_managed_child() {
    let temp = tempfile::tempdir().unwrap();
    let (supervisor, view, state) = real_sidecar(&temp, 250, 0).await;
    let starting = {
        let supervisor = supervisor.clone();
        tokio::spawn(async move { supervisor.start(&view).await })
    };
    let identity = state.join("runtimes/sidecar.json");
    let identity_ready = tokio::time::timeout(Duration::from_secs(2), async {
        while !identity.exists() {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await;
    let stopping = {
        let supervisor = supervisor.clone();
        tokio::spawn(async move { supervisor.stop().await })
    };

    let start_result = starting.await;
    let stop_result = stopping.await;

    assert!(
        identity_ready.is_ok(),
        "sidecar identity was not written before timeout"
    );
    let port = start_result.unwrap().unwrap();
    stop_result.unwrap().unwrap();
    assert!(!identity.exists());
    assert!(std::net::TcpStream::connect(("127.0.0.1", port)).is_err());
}
