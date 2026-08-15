//! 对准应用真实状态路径（中文目录 + \\?\ 前缀）解压 collector 包。
//! 本地构建后有 resources/ 才运行；CI（无运行时包）自动跳过。
#![cfg(windows)]

// resources_missing_skip：无运行时包的环境（如 CI）直接通过

use std::fs;
use std::path::PathBuf;

use data_scientist_lib::manifest::VerifiedPackageManifest;

#[test]
fn extracts_collector_to_real_state_path() {
    let payload = PathBuf::from(env!(\"CARGO_MANIFEST_DIR\")).join(\"resources\");
    if !payload.join(\"package_manifest.json\").is_file() {
        eprintln!(\"[skip] resources/ 不存在（CI 环境）\");
        return;
    }
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let payload = root.join("resources");
    let manifest_bytes = fs::read(payload.join("package_manifest.json")).expect("manifest");
    let keys = fs::read(payload.join("scripts/package_public_keys.json")).expect("keys");
    let manifest =
        VerifiedPackageManifest::from_signed(&manifest_bytes, &keys).expect("manifest verify");

    // 复刻应用的真实目标：APPDATA 下的中文目录 + canonicalize 产生的 \\?\ 前缀
    let appdata = std::env::var("APPDATA").unwrap();
    let target_root = PathBuf::from(appdata).join("数据科学家-diag").join("runtimes").join("collector");
    fs::create_dir_all(&target_root).unwrap();
    let trusted_parent = std::fs::canonicalize(&target_root).unwrap();
    eprintln!("[diag] canonical parent: {trusted_parent:?}");

    let pack_path = payload
        .join("runtime-packs")
        .join(&manifest.manifest().runtimes.collector.archive);
    let descriptor = manifest.manifest().runtimes.collector.clone();
    let destination = format!("{}-diag", descriptor.version);

    let started = std::time::Instant::now();
    data_scientist_lib::runtime::archive::extract_verified_at(
        &pack_path,
        &descriptor,
        &trusted_parent,
        &destination,
        &std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false)),
    )
    .expect("extract collector pack to real path");
    eprintln!("[diag] done in {:?}", started.elapsed());
}
