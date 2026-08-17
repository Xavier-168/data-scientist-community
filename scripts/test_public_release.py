#!/usr/bin/env python3
"""Static release-contract tests for the exported community repository."""

from __future__ import annotations

import json
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless((ROOT / "COMMUNITY_EDITION").is_file(), "community export only")
class PublicReleaseContractTests(unittest.TestCase):
    def _exported_paths(self):
        provenance = json.loads((ROOT / "EXPORT_PROVENANCE.json").read_text(encoding="utf-8"))
        return set((provenance.get("output_files") or {}).keys())

    def test_required_public_documents_exist(self):
        required = {
            "AUDIT_REPORT.md",
            "CHANGELOG.md",
            "CLA.md",
            "CODE_OF_CONDUCT.md",
            "COMMERCIAL_LICENSE.md",
            "COMMERCIAL_TERMS_TEMPLATE.md",
            "CONTRIBUTING.md",
            "DISCLAIMER.md",
            "LICENSE",
            "NOTICE",
            "PLATFORM_COMPLIANCE.md",
            "PRIVACY.md",
            "README.md",
            "README_EN.md",
            "RELEASE_GATE.md",
            "requirements-audit.txt",
            "ROADMAP.md",
            "SECURITY.md",
            "SUPPORT.md",
            "THIRD_PARTY_NOTICES.md",
            "TRADEMARKS.md",
            "SBOM.spdx.json",
        }
        missing = sorted(name for name in required if not (ROOT / name).is_file())
        self.assertEqual(missing, [])

    def test_community_identity_and_license(self):
        self.assertTrue((ROOT / "COMMUNITY_EDITION").is_file())
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["name"], "data-scientist-community")
        self.assertEqual(package["license"], "AGPL-3.0-only")
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", license_text)
        self.assertIn("Version 3, 19 November 2007", license_text)

    def test_commercial_and_private_artifacts_are_absent(self):
        forbidden = {
            ".auth",
            ".claude",
            ".runtime-cache",
            ".worktrees",
            "AGENTS.md",
            "CLAUDE.md",
            "SESSION_HANDOFF.md",
            "dist",
            "downloads",
            "package_manifest.json",
            "scripts/build_release.py",
            "scripts/build_runtime_packs.py",
            "scripts/create_customer_dmg.py",
            "scripts/generate_license_signing_keypair.py",
            "scripts/generate_package_signing_keypair.py",
            "scripts/shipment_ledger.py",
            "scripts/validate_arm64_bundle.py",
        }
        exported = self._exported_paths()
        present = sorted(path for path in forbidden if path in exported or any(item.startswith(path + "/") for item in exported))
        self.assertEqual(present, [])

    def test_no_nuitka_or_compiled_python_payloads(self):
        bad_files = [
            path
            for path in self._exported_paths()
            if Path(path).suffix.lower() in {".so", ".pyc", ".pyo"}
        ]
        self.assertEqual(bad_files, [])
        for path in (ROOT / "scripts").rglob("*"):
            if path == Path(__file__).resolve():
                continue
            if path.is_file() and path.suffix in {".py", ".js", ".mjs", ".sh"}:
                forbidden_builder = "nuit" + "ka"
                self.assertNotIn(forbidden_builder, path.read_text(encoding="utf-8", errors="ignore").lower())

    def test_production_services_are_not_defaults(self):
        client = (ROOT / "scripts/client_license.py").read_text(encoding="utf-8")
        updater = (ROOT / "scripts/update_manager.py").read_text(encoding="utf-8")
        self.assertRegex(client, r'DEFAULT_ACTIVATION_SERVER\s*=\s*""')
        self.assertRegex(client, r"LEGACY_ACTIVATION_SERVERS\s*=\s*\(\)")
        self.assertRegex(client, r"TRIAL_ENABLED\s*=\s*False")
        self.assertRegex(updater, r'DEFAULT_UPDATE_SERVER\s*=\s*""')
        runner = (ROOT / "scripts/runner.py").read_text(encoding="utf-8")
        self.assertIn("COMMUNITY_EDITION_ENABLED", runner)
        self.assertIn('"access_mode": "community"', runner)
        frontend = (ROOT / "frontend/progress.html").read_text(encoding="utf-8")
        self.assertNotIn("试用中", frontend)
        self.assertNotIn("需要联网登记试用", frontend)

    def test_export_provenance_identifies_the_exact_working_tree_snapshot(self):
        provenance = json.loads((ROOT / "EXPORT_PROVENANCE.json").read_text(encoding="utf-8"))
        self.assertEqual(provenance["schema_version"], 2)
        snapshot = provenance["source_snapshot"]
        self.assertEqual(snapshot["base_commit"], provenance["source_commit"])
        self.assertEqual(snapshot["kind"], "working_tree")
        self.assertIsInstance(snapshot["inputs_dirty_against_base_commit"], bool)
        self.assertRegex(snapshot["content_sha256"], r"^[0-9a-f]{64}$")

    def test_default_sbom_resolution_uses_cargo_metadata_licenses(self):
        module_path = ROOT / "scripts" / "generate_sbom.py"
        spec = importlib.util.spec_from_file_location("community_generate_sbom", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        payload = {
            "packages": [
                {
                    "name": "example-crate",
                    "version": "1.2.3",
                    "source": "registry+https://github.com/rust-lang/crates.io-index",
                    "license": "MIT OR Apache-2.0",
                }
            ]
        }
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
        with (
            patch.dict(os.environ, {"npm_node_execpath": "/usr/bin/node"}),
            patch.object(module.subprocess, "run", return_value=completed) as run,
        ):
            records = module.load_cargo_metadata_from_tool(ROOT)
        self.assertEqual(records[0]["licenseDeclared"], "MIT OR Apache-2.0")
        self.assertIn("metadata", run.call_args.args[0])

    def test_only_verified_figma_font_is_distributed(self):
        css = (ROOT / "frontend/assets/progress-apple-theme.css").read_text(encoding="utf-8")
        self.assertNotIn("@font-face", css)
        self.assertNotRegex(css, re.compile(r"fonts/.*\.(?:otf|ttf|woff2?)", re.I))
        figma_css = (ROOT / "frontend/assets/progress-figma-dashboard.css").read_text(encoding="utf-8")
        self.assertIn('@font-face', figma_css)
        self.assertIn('fonts/NotoSerifSC-Variable.ttf', figma_css)
        font_files = [
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "frontend").rglob("*")
            if path.suffix.lower() in {".otf", ".ttf", ".woff", ".woff2"}
        ]
        self.assertEqual(font_files, ["frontend/assets/fonts/NotoSerifSC-Variable.ttf"])
        self.assertTrue((ROOT / "frontend/assets/fonts/NotoSerifSC-OFL.txt").is_file())

    def test_original_community_icons_cover_source_and_macos_bundle(self):
        for name in ("community-icon.png", "community-icon.icns"):
            self.assertTrue((ROOT / "desktop" / "src-tauri" / "icons" / name).is_file(), name)

    def test_four_platform_entrypoints_are_present(self):
        for name in (
            "douyin_export.mjs",
            "xiaohongshu_export.mjs",
            "bilibili_export.mjs",
            "kuaishou_export.mjs",
        ):
            self.assertTrue((ROOT / "scripts" / name).is_file(), name)
        self.assertTrue((ROOT / "scripts" / "runtime_paths.mjs").is_file())

    def test_collectors_do_not_fallback_to_source_runtime_directories(self):
        for name in (
            "douyin_export.mjs",
            "xiaohongshu_export.mjs",
            "bilibili_export.mjs",
            "kuaishou_export.mjs",
        ):
            source = (ROOT / "scripts" / name).read_text(encoding="utf-8-sig")
            self.assertNotIn("path.resolve('downloads'", source, name)
            self.assertNotIn("path.resolve('.auth'", source, name)
            self.assertIn("./runtime_paths.mjs", source, name)
        for name in (
            "run_export.sh",
            "run_xhs_export.sh",
            "run_bili_export.sh",
            "run_ks_export.sh",
        ):
            source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertNotIn(".auth/profiles", source, name)

    def test_source_runtime_state_defaults_outside_checkout(self):
        from core import paths as core_paths

        with tempfile.TemporaryDirectory(prefix="community-state-contract-") as temp_dir:
            fake_home = Path(temp_dir) / "home"
            with (
                patch.object(core_paths.sys, "platform", "darwin"),
                patch.object(core_paths.Path, "home", return_value=fake_home),
                patch.dict(os.environ, {"YIRENGONGIS_STATE_DIR": ""}, clear=False),
            ):
                state_dir = Path(core_paths.resolve_state_dir(ROOT))

        self.assertEqual(
            state_dir,
            fake_home / "Library" / "Application Support" / "数据科学家 Community",
        )
        self.assertNotEqual(state_dir, ROOT)

    def test_startup_and_audit_dependency_contracts_are_consistent(self):
        start_script = (ROOT / "start.sh").read_text(encoding="utf-8")
        self.assertIn("Node.js >= 22.12 且 < 23", start_script)
        self.assertNotIn('PLAYWRIGHT_BROWSERS_PATH="${SCRIPT_DIR}/.playwright-browsers"', start_script)
        self.assertNotIn('.write_test', start_script)

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertLess(readme.index("npm ci --prefix desktop"), readme.index("npm test"))

        audit_requirements = (ROOT / "requirements-audit.txt").read_text(encoding="utf-8")
        self.assertIn("pip-audit==2.10.1", audit_requirements)
        self.assertIn("spdx-tools==0.8.5", audit_requirements)

        sync_source = (ROOT / "scripts" / "sync_feishu_bitable_openapi.py").read_text(encoding="utf-8")
        self.assertNotIn('Path.home() / ".cherrystudio"', sync_source)
        self.assertNotIn('Path(base_dir) / ".auth"', sync_source)
        self.assertNotIn('Path(base_dir) / "downloads"', sync_source)
        self.assertIn('LARK_CLI_TEMP_DIR = AUTH_DIR / "lark-cli-tmp"', sync_source)

    def test_release_scanner_reports_no_tree_findings(self):
        with tempfile.TemporaryDirectory(prefix="community-public-scan-") as temp_dir:
            report = Path(temp_dir) / "scan.json"
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "public_audit_scan.py"),
                    "--root",
                    str(ROOT),
                    "--json-output",
                    str(report),
                    "--fail-on",
                    "medium",
                ],
                check=True,
            )
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(payload["counts"], {"critical": 0, "high": 0, "medium": 0, "low": 0})


if __name__ == "__main__":
    unittest.main()
