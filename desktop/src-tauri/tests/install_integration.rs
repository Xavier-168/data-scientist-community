use std::{
    fs,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
};

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use data_scientist_lib::{
    fault_injection::FaultInjection,
    install::{CopyBackend, InstallCancellation, InstallManager, InstallOutcome},
    manifest::{RuntimeDescriptor, RuntimeSet, VerifiedPackageManifest},
};
use ed25519_dalek::{
    pkcs8::{spki::der::pem::LineEnding, EncodePublicKey},
    Signer, SigningKey,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const VERIFIED_FILES: [&str; 4] = [
    "Contents/MacOS/data-scientist",
    "Contents/Resources/package_manifest.json",
    "Contents/Resources/runtime-packs/core-runtime.tar.zst",
    "Contents/Resources/runtime-packs/collector-runtime.tar.zst",
];

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

fn descriptor(kind: &str, version: &str, bytes: &[u8]) -> RuntimeDescriptor {
    RuntimeDescriptor {
        version: format!("{kind}-{version}"),
        archive: format!("{kind}-runtime.tar.zst"),
        sha256: hex::encode(Sha256::digest(bytes)),
        tree_sha256: hex::encode(Sha256::digest(format!("{kind}-{version}-tree"))),
        size_bytes: bytes.len() as u64,
        required_files: vec![format!("scripts/{kind}.json")],
    }
}

fn signed_bytes(
    signing: &SigningKey,
    build_version: &str,
    core: &[u8],
    collector: &[u8],
) -> Vec<u8> {
    let payload = json!({
        "arch": "arm64",
        "build_version": build_version,
        "key_id": "install-test-key",
        "package_id": "data-scientist-community-mac-arm64",
        "supported_architectures": ["arm64"],
        "runtimes": RuntimeSet {
            core: descriptor("core", build_version, core),
            collector: descriptor("collector", build_version, collector),
        },
    });
    let mut canonical = String::new();
    canonical_json(&payload, &mut canonical);
    let signature = URL_SAFE_NO_PAD.encode(signing.sign(canonical.as_bytes()).to_bytes());
    serde_json::to_vec(&json!({"payload": payload, "signature": signature})).unwrap()
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

#[derive(Clone, Copy)]
enum CopyMode {
    Success,
    CorruptCollector,
}

struct TestCopyBackend {
    mode: CopyMode,
    copied: Arc<AtomicBool>,
    corrupted: Arc<AtomicBool>,
}

impl CopyBackend for TestCopyBackend {
    fn copy_app(
        &self,
        source: &Path,
        target: &Path,
        cancellation: &InstallCancellation,
    ) -> Result<(), String> {
        if cancellation.is_cancelled() {
            return Err("install_cancelled".into());
        }
        copy_tree(source, target)?;
        self.copied.store(true, Ordering::Release);
        if matches!(self.mode, CopyMode::CorruptCollector) {
            fs::write(
                target.join("Contents/Resources/runtime-packs/collector-runtime.tar.zst"),
                b"bad-collector",
            )
            .map_err(|error| error.to_string())?;
            self.corrupted.store(true, Ordering::Release);
        }
        Ok(())
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

struct InstallHarness {
    _temp: tempfile::TempDir,
    source: PathBuf,
    home: PathBuf,
    manifest: Arc<VerifiedPackageManifest>,
    signing: SigningKey,
}

impl InstallHarness {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let source = temp.path().join("Volumes/fixture/数据科学家 Community.app");
        let home = temp.path().join("home");
        fs::create_dir_all(&home).unwrap();
        let signing = SigningKey::from_bytes(&[79_u8; 32]);
        let public_key = signing
            .verifying_key()
            .to_public_key_pem(LineEnding::LF)
            .unwrap();
        let keys = serde_json::to_vec(&json!({
            "active_key_id": "install-test-key",
            "keys": [{"key_id": "install-test-key", "public_key_pem": public_key}],
        }))
        .unwrap();
        let core = b"new-core";
        let collector = b"new-collector";
        let signed = signed_bytes(&signing, "20260710", core, collector);
        let manifest = Arc::new(VerifiedPackageManifest::from_signed(&signed, &keys).unwrap());
        Self::write_bundle(&source, b"new-binary", &signed, core, collector);
        Self {
            _temp: temp,
            source,
            home,
            manifest,
            signing,
        }
    }

    fn write_bundle(root: &Path, binary: &[u8], manifest: &[u8], core: &[u8], collector: &[u8]) {
        fs::create_dir_all(root.join("Contents/MacOS")).unwrap();
        fs::create_dir_all(root.join("Contents/Resources/runtime-packs")).unwrap();
        fs::write(root.join(VERIFIED_FILES[0]), binary).unwrap();
        fs::write(root.join(VERIFIED_FILES[1]), manifest).unwrap();
        fs::write(root.join(VERIFIED_FILES[2]), core).unwrap();
        fs::write(root.join(VERIFIED_FILES[3]), collector).unwrap();
    }

    fn write_older_target(&self) -> Vec<Vec<u8>> {
        let binary = b"old-binary";
        let core = b"old-core";
        let collector = b"old-collector";
        let signed = signed_bytes(&self.signing, "20260709", core, collector);
        Self::write_bundle(&self.target(), binary, &signed, core, collector);
        vec![binary.to_vec(), signed, core.to_vec(), collector.to_vec()]
    }

    fn target(&self) -> PathBuf {
        self.home.join("Applications/数据科学家 Community.app")
    }

    fn manager(&self, mode: CopyMode) -> (InstallManager, Arc<AtomicBool>, Arc<AtomicBool>) {
        let copied = Arc::new(AtomicBool::new(false));
        let corrupted = Arc::new(AtomicBool::new(false));
        let manager = InstallManager::with_backend(
            self.source.clone(),
            self.home.clone(),
            self.manifest.clone(),
            FaultInjection::default(),
            Arc::new(TestCopyBackend {
                mode,
                copied: copied.clone(),
                corrupted: corrupted.clone(),
            }),
        );
        (manager, copied, corrupted)
    }
}

#[test]
fn successful_staged_install_preserves_all_verified_bytes() {
    let fixture = InstallHarness::new();
    let (manager, copied, _) = fixture.manager(CopyMode::Success);

    assert!(matches!(
        manager.install(),
        InstallOutcome::Installed(path) if path == fixture.target()
    ));
    assert!(copied.load(Ordering::Acquire));
    for relative in VERIFIED_FILES {
        assert_eq!(
            fs::read(fixture.source.join(relative)).unwrap(),
            fs::read(fixture.target().join(relative)).unwrap()
        );
    }
}

#[test]
fn corrupted_staging_keeps_old_target_and_removes_temporary_tree() {
    let fixture = InstallHarness::new();
    let old_bytes = fixture.write_older_target();
    let (manager, copied, corrupted) = fixture.manager(CopyMode::CorruptCollector);

    let error = match manager.install() {
        InstallOutcome::Failed(error) => error,
        outcome => panic!("corrupt staging unexpectedly succeeded: {outcome:?}"),
    };
    assert!(copied.load(Ordering::Acquire));
    assert!(corrupted.load(Ordering::Acquire));
    assert_eq!(
        error,
        "installed_checksum_mismatch:Contents/Resources/runtime-packs/collector-runtime.tar.zst"
    );
    for (relative, expected) in VERIFIED_FILES.into_iter().zip(old_bytes) {
        assert_eq!(fs::read(fixture.target().join(relative)).unwrap(), expected);
    }
    assert!(!fs::read_dir(fixture.home.join("Applications"))
        .unwrap()
        .flatten()
        .any(|entry| entry.file_name().to_string_lossy().contains(".installing-")));
}
