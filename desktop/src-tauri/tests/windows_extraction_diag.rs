//! Windows 解压链路隔离验证：用真实 core 包驱动 snapshot→scan→extract。
//! 诊断 2026-08-15 首启 tokio worker 栈溢出问题。

#![cfg(windows)]

// resources_missing_skip：无运行时包的环境（如 CI）直接通过

use std::fs;
use std::path::PathBuf;

use data_scientist_lib::manifest::VerifiedPackageManifest;

#[test]
fn extracts_real_core_pack_end_to_end() {
    let payload = PathBuf::from(env!(\"CARGO_MANIFEST_DIR\")).join(\"resources\");
    if !payload.join(\"package_manifest.json\").is_file() {
        eprintln!(\"[skip] resources/ 不存在（CI 环境）\");
        return;
    }
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let payload = root.join("resources");
    let manifest_bytes = fs::read(payload.join("package_manifest.json")).expect("manifest");
    let keys = fs::read(payload.join("scripts/package_public_keys.json")).expect("keys");
    let manifest = VerifiedPackageManifest::from_signed(&manifest_bytes, &keys)
        .expect("manifest verify");

    let temp = tempfile::tempdir().unwrap();
    let pack_path = payload
        .join("runtime-packs")
        .join(&manifest.manifest().runtimes.core.archive);
    eprintln!("[diag] pack: {pack_path:?}");
    let descriptor = manifest.manifest().runtimes.core.clone();
    let destination = format!("{}-diag", descriptor.version);

    let started = std::time::Instant::now();
    data_scientist_lib::runtime::archive::extract_verified_at(
        &pack_path,
        &descriptor,
        temp.path(),
        &destination,
        &std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false)),
    )
    .expect("extract core pack");
    eprintln!("[diag] extracted in {:?}", started.elapsed());

    let python = temp
        .path()
        .join(&destination)
        .join("runtime/python-x86_64");
    let count = walk_count(&python);
    assert!(count > 1000, "expected python tree, got {count} files");
    eprintln!("[diag] python tree files: {count}");
}

#[test]
fn extracts_real_collector_pack_end_to_end() {
    let payload = PathBuf::from(env!(\"CARGO_MANIFEST_DIR\")).join(\"resources\");
    if !payload.join(\"package_manifest.json\").is_file() {
        eprintln!(\"[skip] resources/ 不存在（CI 环境）\");
        return;
    }
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let payload = root.join("resources");
    let manifest_bytes = fs::read(payload.join("package_manifest.json")).expect("manifest");
    let keys = fs::read(payload.join("scripts/package_public_keys.json")).expect("keys");
    let manifest = VerifiedPackageManifest::from_signed(&manifest_bytes, &keys)
        .expect("manifest verify");

    let temp = tempfile::tempdir().unwrap();
    let pack_path = payload
        .join("runtime-packs")
        .join(&manifest.manifest().runtimes.collector.archive);
    eprintln!("[diag] pack: {pack_path:?}");
    let descriptor = manifest.manifest().runtimes.collector.clone();
    let destination = format!("{}-diag", descriptor.version);

    let started = std::time::Instant::now();
    data_scientist_lib::runtime::archive::extract_verified_at(
        &pack_path,
        &descriptor,
        temp.path(),
        &destination,
        &std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false)),
    )
    .expect("extract collector pack");
    eprintln!("[diag] extracted in {:?}", started.elapsed());

    let node = temp.path().join(&destination).join("runtime/node-x86_64");
    let count = walk_count(&node);
    assert!(count > 20, "expected node tree, got {count} files");
    eprintln!("[diag] node tree files: {count}");
}

fn walk_count(root: &std::path::Path) -> usize {
    let mut total = 0;
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        for entry in fs::read_dir(&dir).unwrap() {
            let entry = entry.unwrap();
            if entry.file_type().unwrap().is_dir() {
                stack.push(entry.path());
            } else {
                total += 1;
            }
        }
    }
    total
}
