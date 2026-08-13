const COMMANDS: &[&str] = &[
    "get_startup_snapshot",
    "mark_react_interactive",
    "open_legacy_console",
    "open_startup_log",
    "retry_startup_stage",
];

fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(COMMANDS)),
    )
    .expect("failed to build Tauri command manifest");
}
