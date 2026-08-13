import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


MODULE_PATH = pathlib.Path(__file__).with_name("update_manager.py")
SPEC = importlib.util.spec_from_file_location("update_manager_security_module", MODULE_PATH)
update_manager = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = update_manager
SPEC.loader.exec_module(update_manager)
import package_identity


class UpdateMetadataSecurityTests(unittest.TestCase):
    def setUp(self):
        self.release = {
            "package_id": "data-scientist-community-mac-arm64",
            "version": "20260710",
            "platform": "mac",
            "arch": "arm64",
            "download_url": "https://downloads.example.test/yirengongis-20260710.dmg",
            "sha256": "a" * 64,
            "size_bytes": 1024,
        }

    def test_release_requires_https_and_sha256(self):
        with self.assertRaisesRegex(update_manager.UpdateValidationError, "https_required"):
            update_manager.validate_release(
                {**self.release, "download_url": "http://downloads.example.test/a.dmg"}
            )
        with self.assertRaisesRegex(update_manager.UpdateValidationError, "sha256_required"):
            update_manager.validate_release({**self.release, "sha256": ""})

    def test_release_identity_must_match_current_package(self):
        with self.assertRaisesRegex(update_manager.UpdateValidationError, "package_id_mismatch"):
            update_manager.validate_release(
                {**self.release, "package_id": "other-package"},
                expected_package_id="data-scientist-community-mac-arm64",
                expected_arch="arm64",
            )

    def test_release_requires_arm64_mac_and_date_version(self):
        with self.assertRaisesRegex(update_manager.UpdateValidationError, "platform_arch_mismatch"):
            update_manager.validate_release({**self.release, "arch": "x86_64"})
        with self.assertRaisesRegex(update_manager.UpdateValidationError, "invalid_build_version"):
            update_manager.validate_release({**self.release, "version": "1.0.7"})
        with self.assertRaisesRegex(update_manager.UpdateValidationError, "invalid_build_version"):
            update_manager.validate_release(
                {
                    **self.release,
                    "version": "20260711.18446744073709551616",
                }
            )
        with self.assertRaisesRegex(update_manager.UpdateValidationError, "invalid_build_version"):
            update_manager.validate_release({**self.release, "version": "２０２６０７１１"})

    def test_download_rejects_client_release_without_trusted_check_result(self):
        with patch.object(
            update_manager,
            "_load_trusted_release",
            return_value={},
        ):
            result = update_manager.download_update(
                "/tmp/update-state",
                self.release,
                base_dir="/tmp/current-app",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "trusted_release_required")

    def test_production_update_check_ignores_query_override(self):
        current = {
            "package_id": "data-scientist-community-mac-arm64",
            "build_version": "20260709",
            "platform": "mac",
            "arch": "arm64",
            "activation_servers": ["https://trusted.example.test"],
        }
        seen_urls = []

        def fake_fetch(url):
            seen_urls.append(url)
            return {"ok": True, "latest": None, "update_available": False}

        with (
            patch.object(update_manager, "current_package_info", return_value=current),
            patch.object(update_manager, "_fetch_json", side_effect=fake_fetch),
            patch.dict(update_manager.os.environ, {}, clear=True),
        ):
            result = update_manager.check_for_update(
                "/tmp/current-app",
                override_url="https://attacker.example.test",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(len(seen_urls), 1)
        self.assertTrue(seen_urls[0].startswith("https://trusted.example.test/updates/latest?"))

    def test_legacy_display_version_uses_internal_date_version_for_comparison(self):
        current = {"package_id": "data-scientist-community-mac-arm64", "build_version": "20260609"}
        normalized = update_manager._normalize_latest_payload(
            {
                "ok": True,
                "update_available": False,
                "latest": {
                    **self.release,
                    "version": "1.0.0",
                    "internal_version": "20260610",
                },
            },
            current,
        )

        self.assertEqual(normalized["version"], "20260610")
        self.assertTrue(normalized["update_available"])


class UpdateReplacementSecurityTests(unittest.TestCase):
    def test_tauri_resource_layout_is_accepted_by_shared_update_validator(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = pathlib.Path(temp_dir) / "数据科学家 Community.app"
            resources = app / "Contents" / "Resources"
            (app / "Contents" / "MacOS").mkdir(parents=True)
            (resources / "runtime-packs").mkdir(parents=True)
            (app / "Contents" / "MacOS" / "data-scientist").write_bytes(b"binary")
            (resources / "package_manifest.json").write_text("{}", encoding="utf-8")
            for archive in ("core-runtime.tar.zst", "collector-runtime.tar.zst"):
                (resources / "runtime-packs" / archive).write_bytes(b"pack")
            payload = {
                "package_id": "data-scientist-community-mac-arm64",
                "build_version": "20260711",
                "platform": "mac",
                "arch": "arm64",
                "runtimes": {
                    "core": {
                        "archive": "core-runtime.tar.zst",
                        "sha256": update_manager.hashlib.sha256(b"pack").hexdigest(),
                        "size_bytes": len(b"pack"),
                    },
                    "collector": {
                        "archive": "collector-runtime.tar.zst",
                        "sha256": update_manager.hashlib.sha256(b"pack").hexdigest(),
                        "size_bytes": len(b"pack"),
                    },
                },
            }

            with patch.object(
                update_manager,
                "verify_package_manifest",
                return_value={"ok": True, "present": True, "payload": payload},
            ):
                result = update_manager.validate_mounted_app(
                    app,
                    {
                        "package_id": "data-scientist-community-mac-arm64",
                        "version": "20260711",
                        "platform": "mac",
                        "arch": "arm64",
                    },
                )

            self.assertEqual(result["build_version"], "20260711")

    def test_candidate_embedded_key_cannot_self_authorize_update(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            app = root / "数据科学家 Community.app"
            resources = app / "Contents" / "Resources"
            packs = resources / "runtime-packs"
            keys_dir = resources / "scripts"
            (app / "Contents" / "MacOS").mkdir(parents=True)
            packs.mkdir(parents=True)
            keys_dir.mkdir()
            (app / "Contents" / "MacOS" / "data-scientist").write_bytes(b"binary")
            core = b"core"
            collector = b"collector"
            (packs / "core-runtime.tar.zst").write_bytes(core)
            (packs / "collector-runtime.tar.zst").write_bytes(collector)

            attacker = Ed25519PrivateKey.generate()
            attacker_private = attacker.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode("utf-8")
            attacker_public = attacker.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode("utf-8")
            payload = {
                "package_id": "data-scientist-community-mac-arm64",
                "build_version": "20260711",
                "key_id": "attacker-key",
                "platform": "mac",
                "arch": "arm64",
                "supported_architectures": ["arm64"],
                "runtimes": {
                    "core": {
                        "archive": "core-runtime.tar.zst",
                        "sha256": update_manager.hashlib.sha256(core).hexdigest(),
                        "size_bytes": len(core),
                    },
                    "collector": {
                        "archive": "collector-runtime.tar.zst",
                        "sha256": update_manager.hashlib.sha256(collector).hexdigest(),
                        "size_bytes": len(collector),
                    },
                },
            }
            signed = package_identity.sign_package_manifest(payload, attacker_private)
            (resources / "package_manifest.json").write_text(
                json.dumps(signed),
                encoding="utf-8",
            )
            (keys_dir / "package_public_keys.json").write_text(
                json.dumps(
                    {
                        "active_key_id": "attacker-key",
                        "keys": [
                            {
                                "key_id": "attacker-key",
                                "public_key_pem": attacker_public,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                update_manager.UpdateValidationError,
                "package_manifest_invalid",
            ):
                update_manager.validate_mounted_app(
                    app,
                    {
                        "package_id": "data-scientist-community-mac-arm64",
                        "version": "20260711",
                        "platform": "mac",
                        "arch": "arm64",
                    },
                )

    def test_tauri_runtime_pack_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = pathlib.Path(temp_dir) / "数据科学家 Community.app"
            resources = app / "Contents" / "Resources"
            packs = resources / "runtime-packs"
            (app / "Contents" / "MacOS").mkdir(parents=True)
            packs.mkdir(parents=True)
            (app / "Contents" / "MacOS" / "data-scientist").write_bytes(b"binary")
            (resources / "package_manifest.json").write_text("{}", encoding="utf-8")
            (packs / "core-runtime.tar.zst").write_bytes(b"evil")
            (packs / "collector-runtime.tar.zst").write_bytes(b"collector")
            payload = {
                "package_id": "data-scientist-community-mac-arm64",
                "build_version": "20260711",
                "platform": "mac",
                "arch": "arm64",
                "runtimes": {
                    "core": {
                        "archive": "core-runtime.tar.zst",
                        "sha256": update_manager.hashlib.sha256(b"core").hexdigest(),
                        "size_bytes": len(b"core"),
                    },
                    "collector": {
                        "archive": "collector-runtime.tar.zst",
                        "sha256": update_manager.hashlib.sha256(b"collector").hexdigest(),
                        "size_bytes": len(b"collector"),
                    },
                },
            }
            with patch.object(
                update_manager,
                "verify_package_manifest",
                return_value={"ok": True, "present": True, "payload": payload},
            ):
                with self.assertRaisesRegex(
                    update_manager.UpdateValidationError,
                    "runtime_pack_checksum_mismatch",
                ):
                    update_manager.validate_mounted_app(app)

    def test_legacy_payload_layout_is_accepted_by_shared_update_validator(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = pathlib.Path(temp_dir) / "数据科学家 Community.app"
            resources = app / "Contents" / "Resources"
            payload = resources / "app"
            (app / "Contents" / "MacOS").mkdir(parents=True)
            (payload / "scripts").mkdir(parents=True)
            (app / "Contents" / "MacOS" / "launcher").write_bytes(b"launcher")
            (payload / "package_manifest.json").write_text("{}", encoding="utf-8")
            (payload / "scripts" / "start_monitor.py").write_text("", encoding="utf-8")
            (payload / "scripts" / "_run.py").write_text("", encoding="utf-8")
            manifest_payload = {
                "package_id": "data-scientist-community-mac-arm64",
                "build_version": "20260711",
                "platform": "mac",
                "arch": "arm64",
            }

            def verify_layout(candidate, **_kwargs):
                if pathlib.Path(candidate) == payload:
                    return {
                        "ok": True,
                        "present": True,
                        "payload": manifest_payload,
                    }
                return {"ok": False, "present": False, "payload": {}}

            with patch.object(
                update_manager,
                "verify_package_manifest",
                side_effect=verify_layout,
            ):
                result = update_manager.validate_mounted_app(
                    app,
                    {
                        "package_id": "data-scientist-community-mac-arm64",
                        "version": "20260711",
                        "platform": "mac",
                        "arch": "arm64",
                    },
                )

            self.assertEqual(result, manifest_payload)

    def test_python_installer_uses_the_same_real_cross_process_lock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = pathlib.Path(temp_dir) / "Applications" / "数据科学家 Community.app"
            target.parent.mkdir(parents=True)
            code = f"""
import pathlib, sys
sys.path.insert(0, {str(MODULE_PATH.parent)!r})
from update_manager import install_transaction_lock
try:
    with install_transaction_lock(pathlib.Path(sys.argv[1]), blocking=False):
        print('acquired')
except RuntimeError as exc:
    print(str(exc))
"""
            with update_manager.install_transaction_lock(target):
                child = subprocess.run(
                    [sys.executable, "-c", code, str(target)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )

            self.assertEqual(child.returncode, 0)
            self.assertEqual(child.stdout.strip(), "install_already_running")

    def test_install_lock_rejects_ancestor_symlink_without_writing_outside(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            external = root / "external"
            external.mkdir()
            linked_parent = root / "linked-home"
            linked_parent.symlink_to(external, target_is_directory=True)
            target = linked_parent / "Applications" / "数据科学家 Community.app"

            with self.assertRaisesRegex(
                RuntimeError,
                "install_applications_invalid",
            ):
                with update_manager.install_transaction_lock(target):
                    self.fail("ancestor symlink must not be followed")

            self.assertFalse((external / "Applications").exists())
            self.assertEqual(list(external.iterdir()), [])

    def test_journal_write_stays_on_locked_parent_after_path_is_replaced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            applications = root / "Applications"
            applications.mkdir()
            target = applications / "数据科学家 Community.app"
            transaction_id = str(update_manager.uuid.uuid4())
            staging, backup = update_manager._install_transaction_paths(
                target,
                transaction_id,
            )
            journal = {
                "schema_version": 1,
                "phase": "prepared",
                "transaction_id": transaction_id,
                "staging_name": staging.name,
                "backup_name": backup.name,
                "had_previous": False,
                "package_id": "data-scientist-community-mac-arm64",
                "build_version": "20260711",
            }
            moved_applications = root / "Applications-held"
            attacker = root / "attacker"
            attacker.mkdir()

            with update_manager.install_transaction_lock(target) as directory:
                applications.rename(moved_applications)
                applications.symlink_to(attacker, target_is_directory=True)
                update_manager._write_install_journal(
                    target,
                    journal,
                    directory=directory,
                )

            self.assertTrue(
                (moved_applications / ".数据科学家 Community.app.install.json").is_file()
            )
            self.assertFalse(
                (attacker / ".数据科学家 Community.app.install.json").exists()
            )

    def test_journal_name_swap_during_read_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            target = root / "Applications" / "数据科学家 Community.app"
            target.parent.mkdir(parents=True)
            transaction_id = str(update_manager.uuid.uuid4())
            staging, backup = update_manager._install_transaction_paths(
                target,
                transaction_id,
            )
            journal = {
                "schema_version": 1,
                "phase": "prepared",
                "transaction_id": transaction_id,
                "staging_name": staging.name,
                "backup_name": backup.name,
                "had_previous": False,
                "package_id": "data-scientist-community-mac-arm64",
                "build_version": "20260711",
            }
            update_manager._write_install_journal(target, journal)
            journal_path = update_manager._install_journal_path(target)
            saved = root / "saved-journal.json"
            replacement = root / "replacement-journal.json"
            replacement.write_text(json.dumps(journal), encoding="utf-8")
            replacement.chmod(0o600)
            original_load = update_manager.json.load

            def load_then_swap(handle):
                payload = original_load(handle)
                journal_path.rename(saved)
                update_manager.shutil.copy2(replacement, journal_path)
                return payload

            with patch.object(update_manager.json, "load", side_effect=load_then_swap):
                with self.assertRaisesRegex(RuntimeError, "install_journal_invalid"):
                    update_manager._read_install_journal(target)

            self.assertTrue(saved.is_file())
            self.assertTrue(journal_path.is_file())

    def test_orphan_install_artifact_without_journal_fails_closed_and_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = pathlib.Path(temp_dir) / "Applications" / "数据科学家 Community.app"
            target.parent.mkdir(parents=True)
            orphan = target.with_name(
                f".{target.name}.installing-{update_manager.uuid.uuid4()}"
            )
            orphan.mkdir()
            (orphan / "sentinel").write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "install_orphan_artifact"):
                update_manager._recover_install_transaction(target)

            self.assertEqual(
                (orphan / "sentinel").read_text(encoding="utf-8"),
                "keep",
            )

    def test_orphan_journal_temporary_fails_closed_and_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = pathlib.Path(temp_dir) / "Applications" / "数据科学家 Community.app"
            target.parent.mkdir(parents=True)
            orphan = target.with_name(
                f".{target.name}.install.json.tmp-{update_manager.uuid.uuid4()}"
            )
            orphan.write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "install_orphan_artifact"):
                update_manager._recover_install_transaction(target)

            self.assertEqual(orphan.read_text(encoding="utf-8"), "keep")

    def test_journal_with_unreferenced_install_artifact_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = pathlib.Path(temp_dir) / "Applications" / "数据科学家 Community.app"
            target.parent.mkdir(parents=True)
            transaction_id = str(update_manager.uuid.uuid4())
            staging, backup = update_manager._install_transaction_paths(
                target,
                transaction_id,
            )
            journal = {
                "schema_version": 1,
                "phase": "prepared",
                "transaction_id": transaction_id,
                "staging_name": staging.name,
                "backup_name": backup.name,
                "had_previous": False,
                "package_id": "data-scientist-community-mac-arm64",
                "build_version": "20260711",
            }
            update_manager._write_install_journal(target, journal)
            orphan = target.with_name(
                f".{target.name}.previous-{update_manager.uuid.uuid4()}"
            )
            orphan.mkdir()
            (orphan / "sentinel").write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "install_orphan_artifact"):
                update_manager._recover_install_transaction(target)

            self.assertTrue(update_manager._install_journal_path(target).exists())
            self.assertEqual(
                (orphan / "sentinel").read_text(encoding="utf-8"),
                "keep",
            )

    def test_replace_uses_locked_parent_capability_after_path_swap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            source = root / "source" / "数据科学家 Community.app"
            applications = root / "Applications"
            target = applications / "数据科学家 Community.app"
            moved_applications = root / "Applications-held"
            attacker = root / "attacker"
            source.mkdir(parents=True)
            target.mkdir(parents=True)
            attacker.mkdir()
            (source / "marker").write_text("new", encoding="utf-8")
            (target / "marker").write_text("old", encoding="utf-8")

            def copy_then_swap(source_path, staging_path):
                update_manager.shutil.copytree(source_path, staging_path)
                applications.rename(moved_applications)
                applications.symlink_to(attacker, target_is_directory=True)

            def manifest_for(path, expected_release=None):
                marker = (pathlib.Path(path) / "marker").read_text(encoding="utf-8")
                return {
                    "package_id": "data-scientist-community-mac-arm64",
                    "build_version": "20260711" if marker == "new" else "20260710",
                    "platform": "mac",
                    "arch": "arm64",
                }

            with patch.object(
                update_manager,
                "validate_mounted_app",
                side_effect=manifest_for,
            ):
                result = update_manager.replace_app_staged(
                    source,
                    target,
                    expected_release={
                        "package_id": "data-scientist-community-mac-arm64",
                        "version": "20260711",
                        "platform": "mac",
                        "arch": "arm64",
                    },
                    copy_fn=copy_then_swap,
                )

            self.assertEqual(result, str(target))
            self.assertEqual(
                (moved_applications / target.name / "marker").read_text(
                    encoding="utf-8"
                ),
                "new",
            )
            self.assertEqual(list(attacker.iterdir()), [])

    def test_older_update_never_calls_copy_for_newer_installed_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            source = root / "source" / "数据科学家 Community.app"
            target = root / "Applications" / "数据科学家 Community.app"
            source.mkdir(parents=True)
            target.mkdir(parents=True)
            (target / "marker").write_text("newer", encoding="utf-8")

            def manifest_for(path, expected_release=None):
                version = "20260710" if pathlib.Path(path) == source else "20260711"
                return {
                    "package_id": "data-scientist-community-mac-arm64",
                    "build_version": version,
                    "platform": "mac",
                    "arch": "arm64",
                }

            with (
                patch.object(update_manager, "validate_mounted_app", side_effect=manifest_for),
                patch.object(update_manager, "copy_app") as copy,
            ):
                result = update_manager.replace_app_staged(
                    source,
                    target,
                    expected_release={
                        "package_id": "data-scientist-community-mac-arm64",
                        "version": "20260710",
                        "platform": "mac",
                        "arch": "arm64",
                    },
                )

            self.assertEqual(result, str(target))
            copy.assert_not_called()
            self.assertEqual((target / "marker").read_text(encoding="utf-8"), "newer")

    def test_older_committed_journal_cleanup_does_not_skip_newer_update(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            source = root / "source" / "数据科学家 Community.app"
            target = root / "Applications" / "数据科学家 Community.app"
            source.mkdir(parents=True)
            target.mkdir(parents=True)
            (source / "marker").write_text("new", encoding="utf-8")
            (target / "marker").write_text("old", encoding="utf-8")
            transaction_id = str(update_manager.uuid.uuid4())
            staging, backup = update_manager._install_transaction_paths(target, transaction_id)
            journal = {
                "schema_version": 1,
                "phase": "committed",
                "transaction_id": transaction_id,
                "staging_name": staging.name,
                "backup_name": backup.name,
                "had_previous": False,
                "package_id": "data-scientist-community-mac-arm64",
                "build_version": "20260711",
            }
            update_manager._write_install_journal(target, journal)

            def manifest_for(path, expected_release=None):
                marker = (pathlib.Path(path) / "marker").read_text(encoding="utf-8")
                return {
                    "package_id": "data-scientist-community-mac-arm64",
                    "build_version": "20260712" if marker == "new" else "20260711",
                    "platform": "mac",
                    "arch": "arm64",
                }

            with patch.object(
                update_manager,
                "validate_mounted_app",
                side_effect=manifest_for,
            ):
                result = update_manager.replace_app_staged(
                    source,
                    target,
                    expected_release={
                        "package_id": "data-scientist-community-mac-arm64",
                        "version": "20260712",
                        "platform": "mac",
                        "arch": "arm64",
                    },
                    copy_fn=update_manager.shutil.copytree,
                )

            self.assertEqual(result, str(target))
            self.assertEqual((target / "marker").read_text(encoding="utf-8"), "new")
            self.assertFalse(update_manager._install_journal_path(target).exists())

    def test_incomplete_old_committed_cleanup_cannot_start_a_new_transaction(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            source = root / "source" / "数据科学家 Community.app"
            target = root / "Applications" / "数据科学家 Community.app"
            source.mkdir(parents=True)
            target.mkdir(parents=True)
            (source / "marker").write_text("new", encoding="utf-8")
            (target / "marker").write_text("old", encoding="utf-8")
            transaction_id = str(update_manager.uuid.uuid4())
            staging, backup = update_manager._install_transaction_paths(target, transaction_id)
            backup.mkdir()
            (backup / "marker").write_text("older", encoding="utf-8")
            journal = {
                "schema_version": 1,
                "phase": "committed",
                "transaction_id": transaction_id,
                "staging_name": staging.name,
                "backup_name": backup.name,
                "had_previous": True,
                "package_id": "data-scientist-community-mac-arm64",
                "build_version": "20260711",
            }
            update_manager._write_install_journal(target, journal)

            def manifest_for(path, expected_release=None):
                marker = (pathlib.Path(path) / "marker").read_text(encoding="utf-8")
                return {
                    "package_id": "data-scientist-community-mac-arm64",
                    "build_version": {
                        "new": "20260712",
                        "old": "20260711",
                        "older": "20260710",
                    }[marker],
                    "platform": "mac",
                    "arch": "arm64",
                }

            with (
                patch.object(update_manager, "validate_mounted_app", side_effect=manifest_for),
                patch.object(
                    update_manager,
                    "_remove_known_app",
                    side_effect=RuntimeError("cleanup failed"),
                ),
                patch.object(update_manager, "copy_app") as copy,
            ):
                with self.assertRaisesRegex(RuntimeError, "install_orphan_artifact"):
                    update_manager.replace_app_staged(
                        source,
                        target,
                        expected_release={
                            "package_id": "data-scientist-community-mac-arm64",
                            "version": "20260712",
                            "platform": "mac",
                            "arch": "arm64",
                        },
                    )

            copy.assert_not_called()
            self.assertTrue(backup.exists())
            self.assertEqual(
                update_manager._read_install_journal(target)["transaction_id"],
                transaction_id,
            )

    def test_intentional_restart_helper_strips_sidecar_owner_marker(self):
        captured = {}

        def fake_popen(command, **kwargs):
            captured.update(kwargs)
            return object()

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.dict(
                update_manager.os.environ,
                {
                    "PATH": "/usr/bin:/bin",
                    **{
                        key: f"secret-{index}"
                        for index, key in enumerate(
                            update_manager.DETACHED_HELPER_STRIP_ENV
                        )
                    },
                },
                clear=True,
            ),
            patch.object(update_manager.subprocess, "Popen", side_effect=fake_popen),
        ):
            root = pathlib.Path(temp_dir)
            dmg = root / "update.dmg"
            dmg.write_bytes(b"update")
            digest = update_manager.hashlib.sha256(b"update").hexdigest()
            release = {
                "package_id": "data-scientist-community-mac-arm64",
                "version": "20260710",
                "platform": "mac",
                "arch": "arm64",
                "download_url": "https://downloads.example.test/update.dmg",
                "sha256": digest,
                "size_bytes": len(b"update"),
            }
            with update_manager._DOWNLOAD_LOCK:
                previous = dict(update_manager._DOWNLOAD_STATE)
                update_manager._DOWNLOAD_STATE.update(
                    {"status": "completed", "path": str(dmg), "release": release}
                )
            try:
                with (
                    patch.object(update_manager, "sys_platform_is_macos", return_value=True),
                    patch.object(
                        update_manager,
                        "current_package_info",
                        return_value={
                            "package_id": "data-scientist-community-mac-arm64",
                            "arch": "arm64",
                        },
                    ),
                ):
                    result = update_manager.install_downloaded_update(
                        str(root), base_dir=str(root / "current-app")
                    )
            finally:
                with update_manager._DOWNLOAD_LOCK:
                    update_manager._DOWNLOAD_STATE.clear()
                    update_manager._DOWNLOAD_STATE.update(previous)

        self.assertTrue(result["ok"])
        self.assertEqual(captured["env"]["PATH"], "/usr/bin:/bin")
        for key in update_manager.DETACHED_HELPER_STRIP_ENV:
            self.assertNotIn(key, captured["env"])

    def test_generated_install_helper_executes_main_and_delegates_callbacks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            mount = root / "mounted"
            source = mount / "数据科学家 Community.app"
            target = root / "Applications" / "数据科学家 Community.app"
            source.mkdir(parents=True)
            dmg_bytes = b"trusted-dmg-fixture"
            (root / "update.dmg").write_bytes(dmg_bytes)
            expected_release = {
                "package_id": "data-scientist-community-mac-arm64",
                "version": "20260710",
                "platform": "mac",
                "arch": "arm64",
                "sha256": update_manager.hashlib.sha256(dmg_bytes).hexdigest(),
                "size_bytes": len(dmg_bytes),
            }
            script = update_manager._install_helper_script(
                dmg_path=str(root / "update.dmg"),
                install_app=str(target),
                log_path=str(root / "update.log"),
                expected_release=expected_release,
                module_base_dir=str(root / "current-app"),
            )
            namespace = {"__name__": "generated_helper_test"}
            exec(compile(script, "install_downloaded_update.py", "exec"), namespace)
            events = []
            captured = {}

            def fake_run(command, **kwargs):
                events.append(("run", tuple(command)))
                if len(command) > 1 and command[1] == "attach":
                    payload = {"system-entities": [{"mount-point": str(mount)}]}
                    return types.SimpleNamespace(
                        returncode=0,
                        stdout=update_manager.plistlib.dumps(payload),
                        stderr=b"",
                    )
                return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

            def fake_replace(source_path, target_path, **kwargs):
                captured.update(
                    source=pathlib.Path(source_path),
                    target=pathlib.Path(target_path),
                    expected_release=kwargs["expected_release"],
                )
                staging = root / "controlled-staging"
                kwargs["copy_fn"](source_path, staging)
                kwargs["prepare_staging"](staging)
                kwargs["verify_candidate_code_signature"](staging)
                kwargs["before_switch"]()
                kwargs["after_switch"](target_path)

            namespace.update(
                run=fake_run,
                replace_app_staged=fake_replace,
                copy_with_ditto=lambda source_path, staging: events.append(
                    ("copy", pathlib.Path(source_path), pathlib.Path(staging))
                ),
                prepare_staging=lambda staging: events.append(
                    ("prepare", pathlib.Path(staging))
                ),
                verify_code_signature=lambda staging: events.append(
                    ("codesign", pathlib.Path(staging))
                ),
                verify_candidate_code_signature=lambda staging: events.append(
                    ("codesign", pathlib.Path(staging))
                ),
                source_signature_identity=lambda _source: "a" * 40,
                stop_installed_app_processes=lambda: events.append(("stop",)),
                after_switch=lambda installed: events.append(
                    ("after", pathlib.Path(installed))
                ),
                time=types.SimpleNamespace(
                    sleep=lambda _seconds: None,
                    strftime=update_manager.time.strftime,
                ),
            )

            namespace["main"]()

            self.assertEqual(captured["source"], source)
            self.assertEqual(captured["target"], target)
            self.assertEqual(captured["expected_release"], expected_release)
            self.assertEqual(
                [event[0] for event in events],
                ["run", "copy", "prepare", "codesign", "stop", "after", "run"],
            )

    def test_generated_helper_prompts_before_ditto_and_alerts_copy_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            script = update_manager._install_helper_script(
                dmg_path=str(root / "update.dmg"),
                install_app=str(root / "Applications" / "数据科学家 Community.app"),
                log_path=str(root / "update.log"),
                expected_release={
                    "package_id": "data-scientist-community-mac-arm64",
                    "version": "20260710",
                    "platform": "mac",
                    "arch": "arm64",
                },
                module_base_dir=str(root / "current-app"),
            )
            namespace = {"__name__": "generated_helper_test"}
            exec(compile(script, "install_downloaded_update.py", "exec"), namespace)
            commands = []

            def failing_ditto(command, **kwargs):
                commands.append(tuple(command))
                if command[0] == "/usr/bin/ditto":
                    return types.SimpleNamespace(
                        returncode=7,
                        stdout=b"",
                        stderr=b"disk full",
                    )
                return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

            namespace["run"] = failing_ditto
            parent_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                staging = update_manager._InstallEntryCapability(
                    root / "staging.app",
                    parent_fd,
                    "staging.app",
                )
                with self.assertRaisesRegex(RuntimeError, "disk full"):
                    namespace["copy_with_ditto"](root / "source.app", staging)
            finally:
                os.close(parent_fd)

            self.assertEqual(
                [command[0] for command in commands],
                ["/usr/bin/osascript", "/usr/bin/ditto", "/usr/bin/osascript"],
            )
            self.assertIn("正在安装", " ".join(commands[0]))
            self.assertIn("giving up after 2", " ".join(commands[0]))
            self.assertIn("安装失败", " ".join(commands[2]))
            self.assertIn("as critical", " ".join(commands[2]))

    def test_generated_helper_notice_failure_stops_before_ditto(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            script = update_manager._install_helper_script(
                dmg_path=str(root / "update.dmg"),
                install_app=str(root / "Applications" / "数据科学家 Community.app"),
                log_path=str(root / "update.log"),
                expected_release={
                    "package_id": "data-scientist-community-mac-arm64",
                    "version": "20260710",
                    "platform": "mac",
                    "arch": "arm64",
                },
                module_base_dir=str(root / "current-app"),
            )
            namespace = {"__name__": "generated_helper_test"}
            exec(compile(script, "install_downloaded_update.py", "exec"), namespace)
            commands = []

            def failed_notice(command, **kwargs):
                commands.append(tuple(command))
                return types.SimpleNamespace(
                    returncode=5,
                    stdout=b"",
                    stderr=b"osascript unavailable",
                )

            namespace["run"] = failed_notice
            with self.assertRaisesRegex(RuntimeError, "osascript unavailable"):
                namespace["copy_with_ditto"](
                    root / "source.app",
                    root / "staging.app",
                )

            self.assertEqual(
                [command[0] for command in commands],
                ["/usr/bin/osascript"],
            )

    def test_generated_helper_codesign_failure_rejects_staging(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            script = update_manager._install_helper_script(
                dmg_path=str(root / "update.dmg"),
                install_app=str(root / "Applications" / "数据科学家 Community.app"),
                log_path=str(root / "update.log"),
                expected_release={
                    "package_id": "data-scientist-community-mac-arm64",
                    "version": "20260710",
                    "platform": "mac",
                    "arch": "arm64",
                },
                module_base_dir=str(root / "current-app"),
            )
            namespace = {"__name__": "generated_helper_test"}
            exec(compile(script, "install_downloaded_update.py", "exec"), namespace)
            commands = []

            def fail_codesign(command, **kwargs):
                commands.append(tuple(command))
                return types.SimpleNamespace(
                    returncode=9 if command[0] == "/usr/bin/codesign" else 0,
                    stdout=b"",
                    stderr=b"invalid signature",
                )

            namespace["run"] = fail_codesign
            parent_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                staging = update_manager._InstallEntryCapability(
                    root / "staging.app",
                    parent_fd,
                    "staging.app",
                )
                with self.assertRaisesRegex(RuntimeError, "invalid signature"):
                    namespace["verify_code_signature"](staging)
            finally:
                os.close(parent_fd)

            self.assertEqual(
                [command[0] for command in commands],
                ["/usr/bin/codesign"],
            )

    def test_generated_helper_rejects_valid_adhoc_signature_with_different_cdhash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            script = update_manager._install_helper_script(
                dmg_path=str(root / "update.dmg"),
                install_app=str(root / "Applications" / "数据科学家 Community.app"),
                log_path=str(root / "update.log"),
                expected_release={
                    "package_id": "data-scientist-community-mac-arm64",
                    "version": "20260710",
                    "platform": "mac",
                    "arch": "arm64",
                },
                module_base_dir=str(root / "current-app"),
            )
            namespace = {"__name__": "generated_helper_test"}
            exec(compile(script, "install_downloaded_update.py", "exec"), namespace)
            namespace["source_code_signature_identity"] = "a" * 40

            def valid_but_different_signature(command, **kwargs):
                detail = b"Executable=/tmp/staging\nCDHash=" + (b"b" * 40) + b"\n"
                return types.SimpleNamespace(returncode=0, stdout=b"", stderr=detail)

            namespace["run"] = valid_but_different_signature
            parent_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                staging = update_manager._InstallEntryCapability(
                    root / "staging.app",
                    parent_fd,
                    "staging.app",
                )
                with self.assertRaisesRegex(RuntimeError, "codesign identity mismatch"):
                    namespace["verify_candidate_code_signature"](staging)
            finally:
                os.close(parent_fd)

    def test_generated_helper_child_cwd_stays_on_locked_parent_after_path_swap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            visible = root / "Applications"
            held = root / "Applications-held"
            outside = root / "outside"
            visible.mkdir()
            outside.mkdir()
            script = update_manager._install_helper_script(
                dmg_path=str(root / "update.dmg"),
                install_app=str(visible / "数据科学家 Community.app"),
                log_path=str(root / "update.log"),
                expected_release={
                    "package_id": "data-scientist-community-mac-arm64",
                    "version": "20260710",
                    "platform": "mac",
                    "arch": "arm64",
                },
                module_base_dir=str(root / "current-app"),
            )
            namespace = {"__name__": "generated_helper_test"}
            exec(compile(script, "install_downloaded_update.py", "exec"), namespace)
            parent_fd = os.open(visible, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                capability = update_manager._InstallEntryCapability(
                    visible / "proof",
                    parent_fd,
                    "proof",
                )
                visible.rename(held)
                visible.symlink_to(outside, target_is_directory=True)
                result = namespace["run_at_install_entry"](
                    ["/usr/bin/touch"],
                    capability,
                )
            finally:
                os.close(parent_fd)

            self.assertEqual(result.returncode, 0)
            self.assertTrue((held / "proof").is_file())
            self.assertFalse((outside / "proof").exists())

    def test_generated_helper_reverifies_and_pins_exact_dmg_inode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            dmg = root / "update.dmg"
            trusted = b"trusted-dmg"
            dmg.write_bytes(trusted)
            release = {
                "package_id": "data-scientist-community-mac-arm64",
                "version": "20260710",
                "platform": "mac",
                "arch": "arm64",
                "sha256": update_manager.hashlib.sha256(trusted).hexdigest(),
                "size_bytes": len(trusted),
            }
            script = update_manager._install_helper_script(
                dmg_path=str(dmg),
                install_app=str(root / "Applications" / "数据科学家 Community.app"),
                log_path=str(root / "update.log"),
                expected_release=release,
                module_base_dir=str(root / "current-app"),
            )
            namespace = {"__name__": "generated_helper_test"}
            exec(compile(script, "install_downloaded_update.py", "exec"), namespace)

            dmg.write_bytes(b"malicious!!")
            with self.assertRaisesRegex(RuntimeError, "DMG checksum mismatch"):
                namespace["prepare_verified_dmg_snapshot"]()

            dmg.write_bytes(trusted)
            directory_descriptor, snapshot_path = namespace[
                "prepare_verified_dmg_snapshot"
            ]()
            try:
                held = root / "verified.dmg"
                dmg.rename(held)
                dmg.write_bytes(b"malicious!!")
                descriptor = os.open("verified.dmg", os.O_RDONLY, dir_fd=directory_descriptor)
                try:
                    self.assertEqual(os.read(descriptor, len(trusted)), trusted)
                finally:
                    os.close(descriptor)
            finally:
                os.unlink("verified.dmg", dir_fd=directory_descriptor)
                os.close(directory_descriptor)
                os.rmdir(snapshot_path)

    def test_interrupted_backup_transaction_restores_old_app_before_retry_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            source = root / "source" / "数据科学家 Community.app"
            target = root / "Applications" / "数据科学家 Community.app"
            source.mkdir(parents=True)
            target.mkdir(parents=True)
            (target / "marker").write_text("old", encoding="utf-8")
            transaction_id = str(update_manager.uuid.uuid4())
            staging, backup = update_manager._install_transaction_paths(target, transaction_id)
            staging.mkdir()
            (staging / "marker").write_text("new", encoding="utf-8")
            os.replace(target, backup)
            journal = {
                "schema_version": 1,
                "phase": "backup_moved",
                "transaction_id": transaction_id,
                "staging_name": staging.name,
                "backup_name": backup.name,
                "had_previous": True,
                "package_id": "data-scientist-community-mac-arm64",
                "build_version": "20260711",
            }
            update_manager._write_install_journal(target, journal)

            def manifest_for(path, expected_release=None):
                path = pathlib.Path(path)
                marker = (path / "marker").read_text(encoding="utf-8")
                return {
                    "package_id": "data-scientist-community-mac-arm64",
                    "build_version": "20260711" if marker == "new" else "20260710",
                    "platform": "mac",
                    "arch": "arm64",
                }

            with (
                patch.object(update_manager, "validate_mounted_app", side_effect=manifest_for),
                patch.object(update_manager, "copy_app", side_effect=RuntimeError("disk_full")),
            ):
                with self.assertRaisesRegex(RuntimeError, "disk_full"):
                    update_manager.replace_app_staged(
                        source,
                        target,
                        expected_release={
                            "package_id": "data-scientist-community-mac-arm64",
                            "version": "20260711",
                            "platform": "mac",
                            "arch": "arm64",
                        },
                    )

            self.assertEqual((target / "marker").read_text(encoding="utf-8"), "old")
            self.assertFalse(staging.exists())
            self.assertFalse(backup.exists())
            self.assertFalse(update_manager._install_journal_path(target).exists())

    def test_target_switched_transaction_restores_old_app_on_recovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            target = root / "Applications" / "数据科学家 Community.app"
            target.mkdir(parents=True)
            (target / "marker").write_text("new", encoding="utf-8")
            transaction_id = str(update_manager.uuid.uuid4())
            staging, backup = update_manager._install_transaction_paths(
                target,
                transaction_id,
            )
            backup.mkdir()
            (backup / "marker").write_text("old", encoding="utf-8")
            journal = {
                "schema_version": 1,
                "phase": "target_switched",
                "transaction_id": transaction_id,
                "staging_name": staging.name,
                "backup_name": backup.name,
                "had_previous": True,
                "package_id": "data-scientist-community-mac-arm64",
                "build_version": "20260711",
            }
            update_manager._write_install_journal(target, journal)

            def manifest_for(path, expected_release=None):
                marker = (pathlib.Path(path) / "marker").read_text(encoding="utf-8")
                return {
                    "package_id": "data-scientist-community-mac-arm64",
                    "build_version": "20260711" if marker == "new" else "20260710",
                    "platform": "mac",
                    "arch": "arm64",
                }

            with patch.object(
                update_manager,
                "validate_mounted_app",
                side_effect=manifest_for,
            ):
                recovered_committed = update_manager._recover_install_transaction(target)

            self.assertFalse(recovered_committed)
            self.assertEqual((target / "marker").read_text(encoding="utf-8"), "old")
            self.assertFalse(backup.exists())
            self.assertFalse(update_manager._install_journal_path(target).exists())

    def test_target_switched_corrupt_target_still_restores_valid_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            target = root / "Applications" / "数据科学家 Community.app"
            target.mkdir(parents=True)
            (target / "marker").write_text("corrupt", encoding="utf-8")
            transaction_id = str(update_manager.uuid.uuid4())
            staging, backup = update_manager._install_transaction_paths(
                target,
                transaction_id,
            )
            backup.mkdir()
            (backup / "marker").write_text("old", encoding="utf-8")
            journal = {
                "schema_version": 1,
                "phase": "target_switched",
                "transaction_id": transaction_id,
                "staging_name": staging.name,
                "backup_name": backup.name,
                "had_previous": True,
                "package_id": "data-scientist-community-mac-arm64",
                "build_version": "20260711",
            }
            update_manager._write_install_journal(target, journal)

            def manifest_for(path, expected_release=None):
                marker = (pathlib.Path(path) / "marker").read_text(encoding="utf-8")
                if marker == "corrupt":
                    raise update_manager.UpdateValidationError(
                        "package_manifest_invalid"
                    )
                return {
                    "package_id": "data-scientist-community-mac-arm64",
                    "build_version": "20260710",
                    "platform": "mac",
                    "arch": "arm64",
                }

            with patch.object(
                update_manager,
                "validate_mounted_app",
                side_effect=manifest_for,
            ):
                recovered_committed = update_manager._recover_install_transaction(target)

            self.assertFalse(recovered_committed)
            self.assertEqual((target / "marker").read_text(encoding="utf-8"), "old")
            self.assertFalse(backup.exists())
            self.assertFalse(update_manager._install_journal_path(target).exists())

    def test_fresh_target_switched_corrupt_target_is_removed_on_recovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            target = root / "Applications" / "数据科学家 Community.app"
            target.mkdir(parents=True)
            (target / "marker").write_text("corrupt", encoding="utf-8")
            transaction_id = str(update_manager.uuid.uuid4())
            staging, backup = update_manager._install_transaction_paths(
                target,
                transaction_id,
            )
            journal = {
                "schema_version": 1,
                "phase": "target_switched",
                "transaction_id": transaction_id,
                "staging_name": staging.name,
                "backup_name": backup.name,
                "had_previous": False,
                "package_id": "data-scientist-community-mac-arm64",
                "build_version": "20260711",
            }
            update_manager._write_install_journal(target, journal)

            with patch.object(
                update_manager,
                "validate_mounted_app",
                side_effect=update_manager.UpdateValidationError(
                    "package_manifest_invalid"
                ),
            ):
                recovered_committed = update_manager._recover_install_transaction(target)

            self.assertFalse(recovered_committed)
            self.assertFalse(target.exists())
            self.assertFalse(update_manager._install_journal_path(target).exists())

    def test_fresh_target_switched_symlink_fails_closed_without_external_delete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            target = root / "Applications" / "数据科学家 Community.app"
            target.parent.mkdir(parents=True)
            external = root / "external-app"
            external.mkdir()
            (external / "sentinel").write_text("keep", encoding="utf-8")
            target.symlink_to(external, target_is_directory=True)
            transaction_id = str(update_manager.uuid.uuid4())
            staging, backup = update_manager._install_transaction_paths(
                target,
                transaction_id,
            )
            journal = {
                "schema_version": 1,
                "phase": "target_switched",
                "transaction_id": transaction_id,
                "staging_name": staging.name,
                "backup_name": backup.name,
                "had_previous": False,
                "package_id": "data-scientist-community-mac-arm64",
                "build_version": "20260711",
            }
            update_manager._write_install_journal(target, journal)

            with self.assertRaisesRegex(RuntimeError, "install_entry_invalid"):
                update_manager._recover_install_transaction(target)

            self.assertTrue(target.is_symlink())
            self.assertEqual(
                (external / "sentinel").read_text(encoding="utf-8"),
                "keep",
            )
            self.assertTrue(update_manager._install_journal_path(target).exists())

    def test_copy_failure_preserves_previous_application(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            source = root / "mounted" / "数据科学家 Community.app"
            target = root / "Applications" / "数据科学家 Community.app"
            source.mkdir(parents=True)
            target.mkdir(parents=True)
            (target / "marker").write_text("old", encoding="utf-8")

            with patch.object(
                update_manager,
                "copy_app",
                side_effect=RuntimeError("disk_full"),
            ):
                with self.assertRaisesRegex(RuntimeError, "disk_full"):
                    update_manager.replace_app_staged(source, target)

            self.assertEqual((target / "marker").read_text(encoding="utf-8"), "old")

    def test_install_uses_completed_canonical_download_not_client_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            canonical = root / "updates" / "canonical.dmg"
            attacker = root / "attacker.dmg"
            canonical.parent.mkdir(parents=True)
            canonical.write_bytes(b"canonical")
            attacker.write_bytes(b"attacker")
            digest = update_manager.hashlib.sha256(b"canonical").hexdigest()
            release = {
                "package_id": "data-scientist-community-mac-arm64",
                "version": "20260710",
                "platform": "mac",
                "arch": "arm64",
                "download_url": "https://downloads.example.test/canonical.dmg",
                "sha256": digest,
                "size_bytes": len(b"canonical"),
            }
            with update_manager._DOWNLOAD_LOCK:
                previous_state = dict(update_manager._DOWNLOAD_STATE)
                update_manager._DOWNLOAD_STATE.update(
                    {
                        "ok": True,
                        "status": "completed",
                        "path": str(canonical),
                        "sha256": digest,
                        "size_bytes": len(b"canonical"),
                        "release": release,
                    }
                )
            try:
                with patch.object(update_manager, "sys_platform_is_macos", return_value=False):
                    result = update_manager.install_downloaded_update(
                        str(root),
                        str(attacker),
                        base_dir=str(root / "current-app"),
                    )
            finally:
                with update_manager._DOWNLOAD_LOCK:
                    update_manager._DOWNLOAD_STATE.clear()
                    update_manager._DOWNLOAD_STATE.update(previous_state)

            self.assertTrue(result["ok"])
            self.assertEqual(result["path"], str(canonical))


if __name__ == "__main__":
    unittest.main()
