import json
import importlib.util
import os
import pathlib
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_IMPORT_RUNTIME = tempfile.TemporaryDirectory()
_PREVIOUS_STATE_DIR = os.environ.get("YIRENGONGIS_STATE_DIR")
os.environ["YIRENGONGIS_STATE_DIR"] = _IMPORT_RUNTIME.name
try:
    MODULE_PATH = pathlib.Path(__file__).with_name("runner.py")
    SPEC = importlib.util.spec_from_file_location("auth_health_runner_module", MODULE_PATH)
    runner = importlib.util.module_from_spec(SPEC)
    assert SPEC and SPEC.loader
    SPEC.loader.exec_module(runner)
finally:
    if _PREVIOUS_STATE_DIR is None:
        os.environ.pop("YIRENGONGIS_STATE_DIR", None)
    else:
        os.environ["YIRENGONGIS_STATE_DIR"] = _PREVIOUS_STATE_DIR
RunLeaseStore = runner.RunLeaseStore


class AuthHealthMonitorTests(unittest.TestCase):
    @contextmanager
    def health_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            auth_dir = root / ".auth"
            downloads_dir = root / "downloads"
            backup_dir = auth_dir / "profile_backups"
            profile_dir = auth_dir / "profiles" / "douyin-chrome"
            profile_dir.mkdir(parents=True)
            backup_dir.mkdir(parents=True)
            downloads_dir.mkdir(parents=True)
            fake_script = root / "run_export.sh"
            fake_script.write_text("#!/bin/sh\n", encoding="utf-8")
            store = RunLeaseStore(downloads_dir / "runner.lock", ttl_seconds=120)
            config = {
                **runner.DEFAULT_CONFIG,
                "enabled_platforms": ["douyin"],
                "browser_channel": "chrome",
            }
            with (
                patch.object(runner, "AUTH_DIR", str(auth_dir)),
                patch.object(runner, "AUTH_STATE_FILE", str(auth_dir / "auth_state.json")),
                patch.object(runner, "AUTH_HEALTH_FILE", str(auth_dir / "auth_health.json")),
                patch.object(runner, "AUTH_HEALTH_PROBE_DIR", str(downloads_dir / "auth_health")),
                patch.object(runner, "AUTH_PROFILE_BACKUP_DIR", str(backup_dir)),
                patch.object(runner, "DOWNLOADS_DIR", str(downloads_dir)),
                patch.object(runner, "AUTH_SINGLE_PLATFORM_MAP", {"douyin": (str(fake_script), str(downloads_dir / "douyin.json"))}),
                patch.object(runner, "_RUN_LEASE_STORE", store),
                patch.object(runner, "load_saved_config", return_value=config),
                patch.object(runner, "active_auth_locks", return_value=[]),
            ):
                runner.save_auth_state({
                    "douyin": {
                        "status": "authorized",
                        "reason": "",
                        "updated_at": "2026-08-13T12:00:00+0800",
                    }
                })
                yield root, profile_dir, store
                store.release("", force=True)

    def _fake_build_env(self, _query, *, platform_id, progress_path, is_xhs=False):
        return {
            "PATH": "",
            "PROGRESS_PATH": progress_path,
            "PLATFORM": platform_id,
        }

    def test_successful_probe_is_healthy_and_preserves_singleton(self):
        with self.health_runtime() as (_root, profile_dir, _store):
            singleton = profile_dir / "SingletonLock"
            singleton.write_text("active-owner", encoding="utf-8")

            def run_success(_command, env, timeout, **_kwargs):
                self.assertEqual(env["CLEAN_PROFILE_LOCKS"], "false")
                self.assertTrue(singleton.is_file())
                self.assertEqual(pathlib.Path(env["DOWNLOAD_DIR"]), pathlib.Path(env["PROGRESS_PATH"]).parent)
                pathlib.Path(env["PROGRESS_PATH"]).parent.mkdir(parents=True, exist_ok=True)
                pathlib.Path(env["DOWNLOAD_DIR"], "error-account-page.png").write_bytes(b"fixture")
                pathlib.Path(env["PROGRESS_PATH"]).write_text(json.dumps({
                    "status": "completed",
                    "auth_status": "authorized",
                    "auth_reason": "",
                }), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout_text="", stderr_text="")

            with (
                patch.object(runner, "_build_env", side_effect=self._fake_build_env),
                patch.object(runner, "_run_script", side_effect=run_success),
                patch.object(runner, "_profile_browser_pids", return_value=[]),
                patch.object(runner, "_backup_platform_profile") as backup_profile,
                patch.object(runner, "_terminate_profile_browsers") as terminate_profile,
                patch.object(runner, "_append_log"),
            ):
                result = runner._run_auth_health_cycle(platform_id="douyin", force=True, now=1_786_614_000)

            self.assertEqual(result["status"], "healthy")
            health = runner.load_auth_health_state()["douyin"]
            self.assertEqual(health["status"], "healthy")
            self.assertTrue(health["last_success_at"])
            self.assertEqual(runner.load_auth_state()["douyin"]["status"], "authorized")
            self.assertTrue(singleton.is_file())
            self.assertEqual(list(pathlib.Path(runner.AUTH_HEALTH_PROBE_DIR).iterdir()), [])
            backup_profile.assert_not_called()
            terminate_profile.assert_not_called()

    def test_startup_removes_stale_probe_workspace_and_loose_files(self):
        with self.health_runtime():
            probe_root = pathlib.Path(runner.AUTH_HEALTH_PROBE_DIR)
            stale_dir = probe_root / "douyin-stale"
            stale_dir.mkdir(parents=True)
            (stale_dir / "error-login.png").write_bytes(b"fixture")
            (probe_root / "legacy.health-probe.json").write_text("{}", encoding="utf-8")
            runner._cleanup_auth_health_probe_entries()
            self.assertEqual(list(probe_root.iterdir()), [])

    def test_explicit_login_page_marks_previous_authorization_expired(self):
        with self.health_runtime():
            def run_expired(_command, env, timeout, **_kwargs):
                pathlib.Path(env["PROGRESS_PATH"]).parent.mkdir(parents=True, exist_ok=True)
                pathlib.Path(env["PROGRESS_PATH"]).write_text(json.dumps({
                    "status": "failed",
                    "message": "导出失败：抖音未登录（headless=true 无法扫码登录）",
                }), encoding="utf-8")
                return SimpleNamespace(
                    returncode=1,
                    stdout_text="",
                    stderr_text="抖音未登录（headless=true 无法扫码登录）",
                )

            with (
                patch.object(runner, "_build_env", side_effect=self._fake_build_env),
                patch.object(runner, "_run_script", side_effect=run_expired),
                patch.object(runner, "_profile_browser_pids", return_value=[]),
                patch.object(runner, "_append_log"),
            ):
                result = runner._run_auth_health_cycle(platform_id="douyin", force=True)

            self.assertEqual(result["status"], "expired")
            self.assertEqual(runner.load_auth_state()["douyin"]["status"], "expired")
            self.assertEqual(runner.load_auth_health_state()["douyin"]["reason"], "expired_cookie")

    def test_transient_probe_failure_keeps_previous_authorized_state(self):
        with self.health_runtime():
            def run_failed(_command, env, timeout, **_kwargs):
                pathlib.Path(env["PROGRESS_PATH"]).parent.mkdir(parents=True, exist_ok=True)
                pathlib.Path(env["PROGRESS_PATH"]).write_text(json.dumps({
                    "status": "failed",
                    "message": "平台暂时返回 503",
                }), encoding="utf-8")
                return SimpleNamespace(returncode=1, stdout_text="", stderr_text="upstream 503")

            with (
                patch.object(runner, "_build_env", side_effect=self._fake_build_env),
                patch.object(runner, "_run_script", side_effect=run_failed),
                patch.object(runner, "_profile_browser_pids", return_value=[]),
                patch.object(runner, "_append_log"),
            ):
                result = runner._run_auth_health_cycle(platform_id="douyin", force=True)

            self.assertEqual(result["status"], "unknown")
            self.assertEqual(runner.load_auth_state()["douyin"]["status"], "authorized")
            self.assertEqual(runner.load_auth_health_state()["douyin"]["error_code"], "probe_failed")

    def test_busy_run_lease_skips_without_starting_browser(self):
        with self.health_runtime() as (_root, _profile, store):
            existing = store.acquire("run_all")
            self.assertIsNotNone(existing)
            with patch.object(runner, "_run_auth_health_probe") as probe:
                result = runner._run_auth_health_cycle(platform_id="douyin", force=True)
            self.assertEqual(result["status"], "skipped")
            probe.assert_not_called()
            self.assertEqual(store.read_payload()["kind"], "run_all")

    def test_revoke_cannot_interleave_with_health_lease(self):
        with self.health_runtime() as (_root, _profile, store):
            token = store.acquire("auth_health")
            self.assertIsNotNone(token)
            with runner.RUN_MUTEX:
                blocked = runner.is_locked()
                revoke_token = None if blocked else runner.lock(kind="auth_revoke")
            self.assertTrue(blocked)
            self.assertIsNone(revoke_token)
            self.assertEqual(runner.load_auth_state()["douyin"]["status"], "authorized")

    def test_probe_result_cannot_resurrect_auth_after_revoke(self):
        with self.health_runtime() as (_root, profile_dir, _store):
            entered = threading.Event()
            release = threading.Event()
            result = {}

            def delayed_probe(_platform_id):
                entered.set()
                self.assertTrue(release.wait(2))
                return {"status": "healthy", "reason": "", "error_code": ""}

            def run_cycle():
                result.update(runner._run_auth_health_cycle(platform_id="douyin", force=True))

            with (
                patch.object(runner, "_run_auth_health_probe", side_effect=delayed_probe),
                patch.object(runner, "_append_log"),
            ):
                worker = threading.Thread(target=run_cycle)
                worker.start()
                self.assertTrue(entered.wait(2))
                runner.revoke_platform_auth("douyin")
                release.set()
                worker.join(timeout=3)

            self.assertFalse(worker.is_alive())
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(runner.load_auth_state()["douyin"]["status"], "unauthorized")
            self.assertFalse(profile_dir.exists())

    def test_active_profile_owner_skips_without_modifying_profile(self):
        with self.health_runtime() as (_root, profile_dir, _store):
            singleton = profile_dir / "SingletonCookie"
            singleton.write_text("live", encoding="utf-8")
            with (
                patch.object(runner, "_profile_browser_pids", return_value=[4321]),
                patch.object(runner, "_run_script") as run_script,
                patch.object(runner, "_append_log"),
            ):
                result = runner._run_auth_health_cycle(platform_id="douyin", force=True)
            self.assertEqual(result["status"], "skipped")
            run_script.assert_not_called()
            self.assertEqual(singleton.read_text(encoding="utf-8"), "live")

    def test_reauthorization_resets_stale_health_for_immediate_recheck(self):
        with self.health_runtime():
            runner.save_auth_state({
                "douyin": {
                    "status": "expired",
                    "reason": "expired_cookie",
                    "updated_at": "2026-08-13T12:00:00+0800",
                }
            })
            runner.save_auth_health_state({
                "douyin": {
                    "status": "expired",
                    "checked_at": "2026-08-13T12:00:00+0800",
                    "last_success_at": "2026-08-12T12:00:00+0800",
                    "next_check_at": "2026-08-14T12:00:00+0800",
                    "reason": "expired_cookie",
                    "failure_count": 1,
                    "error_code": "login_required",
                }
            })
            runner._set_platform_auth_state("douyin", "authorized", "")
            health = runner.load_auth_health_state()["douyin"]
            self.assertEqual(health["status"], "unknown")
            self.assertEqual(health["reason"], "new_authorization")
            self.assertEqual(health["next_check_at"], "")
            self.assertEqual(runner._auth_health_platforms(), ["douyin"])

    def test_authorized_to_authorized_reauth_is_explicitly_marked_pending(self):
        with self.health_runtime():
            runner.save_auth_health_state({
                "douyin": {
                    "status": "unknown",
                    "checked_at": "2026-08-13T12:00:00+0800",
                    "last_success_at": "",
                    "next_check_at": "2026-08-13T18:00:00+0800",
                    "reason": "probe_unavailable",
                    "failure_count": 1,
                    "error_code": "probe_failed",
                }
            })
            runner._mark_auth_health_pending("douyin")
            health = runner.load_auth_health_state()["douyin"]
            self.assertEqual(health["status"], "unknown")
            self.assertEqual(health["reason"], "new_authorization")
            self.assertEqual(health["next_check_at"], "")

    def test_explicit_expiry_overrides_previous_healthy_snapshot(self):
        with self.health_runtime():
            runner._record_auth_health_result("douyin", "healthy")
            runner._set_platform_auth_state("douyin", "expired", "expired_cookie")
            decorated = runner._decorate_progress("douyin", runner.default_progress("douyin"), ["douyin"])
            self.assertEqual(decorated["auth_status"], "expired")
            self.assertEqual(decorated["auth_health_status"], "expired")
            self.assertEqual(decorated["auth_check_reason"], "expired_cookie")

    def test_revoke_clears_healthy_status_and_profile(self):
        with self.health_runtime() as (_root, profile_dir, _store):
            backup = pathlib.Path(runner.AUTH_PROFILE_BACKUP_DIR) / "douyin-chrome-manual_reauth-old"
            backup.mkdir(parents=True)
            (backup / "Cookies").write_bytes(b"old-cookie-db")
            runner._record_auth_health_result("douyin", "healthy")
            result = runner.revoke_platform_auth("douyin")
            self.assertTrue(result["ok"])
            self.assertFalse(profile_dir.exists())
            self.assertFalse(backup.exists())
            self.assertEqual(runner.load_auth_state()["douyin"]["status"], "unauthorized")
            self.assertEqual(runner.load_auth_health_state()["douyin"]["status"], "needs_auth")

    def test_profile_backup_retention_keeps_only_current_copy(self):
        with self.health_runtime() as (_root, profile_dir, _store):
            old_backup = pathlib.Path(runner.AUTH_PROFILE_BACKUP_DIR) / "douyin-chrome-startup_failed-old"
            old_backup.mkdir(parents=True)
            (old_backup / "Cookies").write_bytes(b"older-cookie-db")
            (profile_dir / "Cookies").write_bytes(b"current-cookie-db")
            current_backup = pathlib.Path(
                runner._backup_platform_profile("douyin", "chrome", reason="manual_reauth")
            )
            self.assertFalse(profile_dir.exists())
            self.assertFalse(old_backup.exists())
            self.assertTrue(current_backup.is_dir())
            self.assertEqual(runner._platform_profile_backup_dirs("douyin", "chrome"), [str(current_backup)])

    def test_reset_clear_auth_removes_health_snapshot(self):
        with self.health_runtime() as (root, _profile_dir, _store):
            backup = pathlib.Path(runner.AUTH_PROFILE_BACKUP_DIR) / "douyin-chrome-manual_reauth-old"
            backup.mkdir(parents=True)
            (backup / "Cookies").write_bytes(b"old-cookie-db")
            runner._record_auth_health_result("douyin", "healthy")
            config_file = root / ".auth" / "customer_config.json"
            history_file = root / "downloads" / "run_history.json"
            with (
                patch.object(runner, "CONFIG_FILE", str(config_file)),
                patch.object(runner, "RUN_HISTORY_FILE", str(history_file)),
                patch.object(runner, "PLATFORM_PROGRESS_FILES", {}),
            ):
                result = runner.reset_onboarding_state(clear_auth=True)
            self.assertTrue(result["ok"])
            self.assertFalse(pathlib.Path(runner.AUTH_HEALTH_FILE).exists())
            self.assertFalse(pathlib.Path(runner.AUTH_STATE_FILE).exists())
            self.assertTrue(pathlib.Path(runner.AUTH_PROFILE_BACKUP_DIR).is_dir())
            self.assertEqual(list(pathlib.Path(runner.AUTH_PROFILE_BACKUP_DIR).iterdir()), [])

    def test_due_selection_respects_next_check(self):
        with self.health_runtime():
            now = time.time()
            runner.save_auth_health_state({
                "douyin": {
                    "status": "healthy",
                    "checked_at": runner._auth_health_time_text(now),
                    "last_success_at": runner._auth_health_time_text(now),
                    "next_check_at": runner._auth_health_time_text(now + 3600),
                    "reason": "",
                    "failure_count": 0,
                    "error_code": "",
                }
            })
            self.assertEqual(runner._auth_health_platforms(now=now), [])
            self.assertEqual(runner._auth_health_platforms(force=True, now=now), ["douyin"])

    def test_progress_exposes_health_and_auth_checking_stage(self):
        handler = runner.Handler.__new__(runner.Handler)
        handler._send_json = MagicMock()
        platform = {
            "enabled": True,
            "auth_status": "authorized",
            "auth_health_status": "healthy",
            "auth_checked_at": "2026-08-13T16:20:00+0800",
            "ui_status": "idle",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunLeaseStore(pathlib.Path(temp_dir) / "runner.lock", ttl_seconds=120)
            token = store.acquire("auth_health")
            self.assertIsNotNone(token)
            with (
                patch.object(runner, "_RUN_LEASE_STORE", store),
                patch.object(runner, "_AUTH_HEALTH_ACTIVE_PLATFORM", "douyin"),
                patch.object(runner, "load_saved_config", return_value={"enabled_platforms": ["douyin"]}),
                patch.object(runner, "config_summary", return_value={
                    "enabled_platforms": ["douyin"],
                    "enabled_platform_count": 1,
                    "onboarding_completed": True,
                }),
                patch.object(runner, "_decorate_progress", return_value=platform),
                patch.object(runner, "_read_run_history", return_value=[]),
                patch.object(runner, "_build_feishu_runtime_summary", return_value={"status": "disabled"}),
            ):
                handler._handle_progress()

        status, payload = handler._send_json.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"]["current_stage"], "auth_checking")
        self.assertTrue(payload["summary"]["auth_health"]["running"])
        self.assertEqual(payload["douyin"]["auth_health_status"], "healthy")

    def test_monitor_cycle_exception_is_contained(self):
        with (
            patch.object(runner, "_run_auth_health_cycle", side_effect=RuntimeError("fixture secret should not leak")),
            patch.object(runner, "_append_log") as append_log,
        ):
            result = runner._run_auth_health_cycle_safely(platform_id="douyin", force=True)
        self.assertEqual(result["status"], "unknown")
        log_args = "\n".join(str(item) for item in append_log.call_args.args)
        self.assertIn("RuntimeError", log_args)
        self.assertNotIn("fixture secret", log_args)

    def test_collectors_honor_non_destructive_profile_probe_switch(self):
        scripts_dir = pathlib.Path(runner.SCRIPT_DIR)
        for name in (
            "douyin_export.mjs",
            "xiaohongshu_export.mjs",
            "bilibili_export.mjs",
            "kuaishou_export.mjs",
        ):
            source = (scripts_dir / name).read_text(encoding="utf-8-sig")
            self.assertIn("CLEAN_PROFILE_LOCKS", source, name)
            self.assertIn("if (!CONFIG.cleanProfileLocks) return", source, name)

    def test_bilibili_and_kuaishou_require_stable_visible_markers(self):
        scripts_dir = pathlib.Path(runner.SCRIPT_DIR)
        bilibili = (scripts_dir / "bilibili_export.mjs").read_text(encoding="utf-8")
        kuaishou = (scripts_dir / "kuaishou_export.mjs").read_text(encoding="utf-8")
        self.assertIn("async function isDashboardStable", bilibili)
        self.assertNotIn("if (currentUrl.includes('/platform/data-up/video')) return true", bilibili)
        self.assertNotIn("return isCreatorPlatformUrl(currentUrl)", bilibili)
        self.assertIn("async function visibleMarkerExists", kuaishou)
        self.assertIn("async function hasStableLogin", kuaishou)
        self.assertNotIn("const markers = ['text=首页'", kuaishou)

    def test_monitor_start_is_singleton_and_stop_is_cooperative(self):
        started = threading.Event()

        def fake_loop(stop_event):
            started.set()
            stop_event.wait()

        runner._stop_auth_health_monitor(join_timeout=0.1)
        with (
            patch.object(runner, "AUTH_HEALTH_ENABLED", True),
            patch.object(runner, "_auth_health_monitor_loop", side_effect=fake_loop),
        ):
            first = runner._start_auth_health_monitor()
            self.assertTrue(started.wait(1))
            second = runner._start_auth_health_monitor()
            self.assertIs(first, second)
            runner._stop_auth_health_monitor(join_timeout=1)
            self.assertFalse(first.is_alive())

    @unittest.skipIf(os.name == "nt", "POSIX process groups are required")
    def test_stop_monitor_terminates_inflight_probe_process_group(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = pathlib.Path(temp_dir) / "orphan-marker"
            env = os.environ.copy()
            command = ["/bin/sh", "-c", f"sleep 2; printf orphan > {marker}"]
            worker = threading.Thread(
                target=runner._run_script,
                args=(command, env),
                kwargs={"timeout": 30, "process_slot": "auth_health"},
                daemon=True,
            )
            worker.start()
            deadline = time.time() + 2
            while time.time() < deadline:
                with runner._AUTH_HEALTH_PROCESS_LOCK:
                    if runner._AUTH_HEALTH_PROCESS is not None:
                        break
                time.sleep(0.01)
            runner._terminate_auth_health_process()
            worker.join(timeout=3)
            time.sleep(2.1)
            self.assertFalse(worker.is_alive())
            self.assertFalse(marker.exists())

    def test_health_state_contains_metadata_not_cookie_values(self):
        with self.health_runtime():
            runner._record_auth_health_result("douyin", "healthy")
            payload = json.loads(pathlib.Path(runner.AUTH_HEALTH_FILE).read_text(encoding="utf-8"))
        serialized = json.dumps(payload, ensure_ascii=False).lower()
        self.assertNotIn("cookie_value", serialized)
        self.assertNotIn("session_token", serialized)
        self.assertEqual(set(payload["douyin"]), set(runner.AUTH_HEALTH_PUBLIC_FIELDS))


if __name__ == "__main__":
    unittest.main()
