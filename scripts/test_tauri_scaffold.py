import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class TauriScaffoldTests(unittest.TestCase):
    def test_rust_toolchain_and_generated_outputs_are_declared(self):
        toolchain = (ROOT / "rust-toolchain.toml").read_text(encoding="utf-8")
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn('channel = "stable"', toolchain)
        self.assertIn("/desktop/node_modules/", gitignore)
        self.assertIn("/desktop/dist/", gitignore)
        self.assertIn("/desktop/src-tauri/target/", gitignore)
        self.assertIn("/desktop/src-tauri/gen/schemas/", gitignore)
        self.assertIn("/.venv/", gitignore)
        self.assertEqual(
            (ROOT / ".node-version").read_text(encoding="utf-8").strip(),
            "22.14.0",
        )
        requirements = (ROOT / "requirements-test.txt").read_text(encoding="utf-8")
        self.assertIn("cryptography==50.0.0", requirements)
        self.assertIn("pandas==3.0.3", requirements)

    def test_frontend_has_build_and_test_entrypoints(self):
        self.assertTrue((ROOT / "desktop" / "src" / "main.tsx").is_file())
        self.assertTrue(
            (ROOT / "desktop" / "src" / "test" / "scaffold.test.ts").is_file()
        )

    @unittest.skipIf((ROOT / "COMMUNITY_EDITION").is_file(), "commercial root script contract")
    def test_desktop_workspace_has_one_frontend_and_one_rust_root(self):
        import json

        root_package = json.loads(
            (ROOT / "package.json").read_text(encoding="utf-8")
        )
        root_scripts = root_package["scripts"]
        self.assertEqual(
            root_scripts["test:frontend"],
            "node scripts/test_frontend_security.js && node scripts/test_frontend_request_recovery.js && node scripts/test_progress_workflow_guards.js --reliability-only && node scripts/test_collection_journey_progress.js && node scripts/test_progress_dashboard_copy.js --classification-only && node scripts/test_loading_screen.js && node scripts/test_browser_auth_utils.mjs && node scripts/test_auth_health_transient_pages.mjs && node scripts/test_xiaohongshu_auth_stability.mjs && node scripts/test_kuaishou_export_download.mjs && node scripts/test_activation_worker_security.js && node scripts/test_activation_worker_reliability.mjs && node scripts/test_bilibili_login_guards.js && node scripts/test_bilibili_export_integrity.js && node scripts/test_douyin_result_reuse.js && node scripts/test_monitor_session_guard.js",
        )
        self.assertEqual(
            root_scripts["test:python"],
            ".venv/bin/python -m unittest discover -s scripts -p 'test_*.py'",
        )
        expected_desktop_gates = {
            "test:python:desktop": ".venv/bin/python -m unittest discover -s scripts -p 'test_*.py'",
            "test:legacy": "npm run test:frontend && npm run test:python:desktop",
            "test:desktop:web": "npm --prefix desktop test",
            "test:desktop:build": "npm --prefix desktop run build:web",
            "test:desktop:rust": "npm --prefix desktop run test:rust",
            "test:desktop:smoke": "node scripts/test_tauri_startup_ui.mjs",
        }
        self.assertEqual(
            {name: root_scripts[name] for name in expected_desktop_gates},
            expected_desktop_gates,
        )
        self.assertEqual(
            root_scripts["test"],
            "npm run test:legacy && npm run test:desktop:web && npm run test:desktop:build && npm run test:desktop:rust && npm run test:desktop:smoke",
        )
        smoke_test = ROOT / "scripts" / "test_tauri_startup_ui.mjs"
        rust_tool = ROOT / "scripts" / "run_rust_tool.mjs"
        rust_signal_test = ROOT / "scripts" / "test_rust_tool_signal.mjs"
        self.assertTrue(smoke_test.is_file())
        self.assertTrue(rust_tool.is_file())
        self.assertTrue(rust_signal_test.is_file())

        self.assertTrue((ROOT / "desktop" / "package.json").is_file())
        package = json.loads(
            (ROOT / "desktop" / "package.json").read_text(encoding="utf-8")
        )
        self.assertEqual(package["engines"]["node"], ">=22.12.0 <23")
        self.assertEqual(
            package["scripts"]["dev"],
            '"$npm_node_execpath" ../scripts/run_rust_tool.mjs tauri dev',
        )
        self.assertEqual(
            package["scripts"]["build:app"],
            '"$npm_node_execpath" ../scripts/run_rust_tool.mjs tauri build',
        )
        self.assertEqual(
            package["scripts"]["test:rust"],
            '"$npm_node_execpath" ../scripts/run_rust_tool.mjs cargo test --manifest-path src-tauri/Cargo.toml --locked --all-features && "$npm_node_execpath" ../scripts/test_rust_tool_signal.mjs',
        )
        rust_tool_source = rust_tool.read_text(encoding="utf-8")
        for candidate in (
            "RUSTUP_BIN",
            "/opt/homebrew/bin/rustup",
            "/opt/homebrew/opt/rustup/bin/rustup",
            "/usr/local/bin/rustup",
            "/usr/local/opt/rustup/bin/rustup",
            ".cargo/bin/rustup",
        ):
            self.assertIn(candidate, rust_tool_source)
        for process_contract in (
            "detached: process.platform !== 'win32'",
            "process.kill(-groupPid, signal)",
            "process.kill(-groupPid, 'SIGKILL')",
            "RUST_TOOL_STOP_TIMEOUT_MS",
        ):
            self.assertIn(process_contract, rust_tool_source)
        smoke_source = smoke_test.read_text(encoding="utf-8")
        for contract in (
            "chromium.launchServer",
            "browserServer.process().pid",
            "async function cleanupBrowserServer",
            "browserServer.kill()",
            "browserProcess.exitCode",
            "browserProcessClosed",
            "childDidClose",
            "const ownedGroupPid =",
            "listProcessTreePids",
            "MAX_PORT_RETRIES = 3",
            "allowOccupiedPort",
            "TAURI_SMOKE_OCCUPY_FIRST_PORT",
            "page.on('console'",
            "unexpected_error",
        ):
            self.assertIn(contract, smoke_source)
        self.assertNotIn("chromium.launch({", smoke_source)
        self.assertNotIn("process.kill(browserPid", smoke_source)
        self.assertNotIn("pidExists(browserPid)", smoke_source)
        self.assertNotIn("signalProcessGroup(child.pid", smoke_source)
        self.assertNotIn("processGroupExists(child.pid)", smoke_source)
        cargo_toml = (ROOT / "desktop" / "src-tauri" / "Cargo.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn('name = "data-scientist"', cargo_toml)
        self.assertIn('name = "data_scientist_lib"', cargo_toml)
        self.assertFalse((ROOT / "Cargo.toml").exists())

    def test_tauri_config_uses_system_webview_and_no_remote_frontend(self):
        import json

        config = json.loads(
            (ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(
                encoding="utf-8"
            )
        )
        window = config["app"]["windows"][0]
        self.assertEqual(config["identifier"], "org.datascientist.community")
        self.assertEqual(config["mainBinaryName"], "data-scientist")
        self.assertEqual(config["build"]["devUrl"], "http://127.0.0.1:1420")
        self.assertEqual(config["build"]["frontendDist"], "../dist")
        # macOS .app 与 Windows NSIS 双目标（各平台构建器只认自己的 target）
        self.assertEqual(config["bundle"]["targets"], ["app", "nsis"])
        self.assertEqual(config["app"]["security"]["capabilities"], ["default"])
        self.assertNotIn("https://", str(config["build"]))
        self.assertTrue(window["visible"])
        self.assertEqual(window["label"], "main")
        capability = json.loads(
            (
                ROOT
                / "desktop"
                / "src-tauri"
                / "capabilities"
                / "default.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(capability["windows"], ["main"])
        self.assertEqual(
            set(capability["permissions"]),
            {
                "core:event:allow-listen",
                "core:event:allow-unlisten",
                "allow-get-startup-snapshot",
                "allow-retry-startup-stage",
                "allow-open-startup-log",
                "allow-mark-react-interactive",
                "allow-open-legacy-console",
            },
        )
        vite_config = (ROOT / "desktop" / "vite.config.ts").read_text(
            encoding="utf-8"
        )
        self.assertIn("'TAURI_ENV_*'", vite_config)
        self.assertNotIn("'TAURI_'", vite_config)
        self.assertTrue(
            (ROOT / "desktop" / "src-tauri" / "icons" / "community-icon.png").is_file()
        )
        bridge = ROOT / "desktop" / "src" / "tauri" / "bridge.ts"
        forbidden_imports = []
        for source in (ROOT / "desktop" / "src").rglob("*"):
            if source == bridge or source.suffix not in {".ts", ".tsx"}:
                continue
            if "@tauri-apps/api" in source.read_text(encoding="utf-8"):
                forbidden_imports.append(str(source.relative_to(ROOT)))
        self.assertEqual(forbidden_imports, [])


if __name__ == "__main__":
    unittest.main()
