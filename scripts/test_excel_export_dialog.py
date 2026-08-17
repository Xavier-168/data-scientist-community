import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
_TEST_SANDBOX = tempfile.TemporaryDirectory(prefix="excel-export-runner-test-")
TEST_ROOT = pathlib.Path(_TEST_SANDBOX.name)
(TEST_ROOT / "home").mkdir()
(TEST_ROOT / "state").mkdir()
os.environ["HOME"] = str(TEST_ROOT / "home")
os.environ["YIRENGONGIS_STATE_DIR"] = str(TEST_ROOT / "state")
os.environ["YIRENGONGIS_BASE_DIR"] = str(ROOT)
os.environ["YIRENGONGIS_LICENSE_BYPASS"] = "1"
os.environ["YIRENGONGIS_AUTH_HEALTH_ENABLED"] = "0"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(ROOT / "scripts"))

MODULE_PATH = ROOT / "scripts" / "runner.py"
SPEC = importlib.util.spec_from_file_location("runner_excel_export_module", MODULE_PATH)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(runner)


def _write_minimal_xlsx(path: pathlib.Path, marker: str) -> bytes:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as workbook:
        workbook.writestr("[Content_Types].xml", "<Types/>")
        workbook.writestr("xl/workbook.xml", "<workbook/>")
        workbook.writestr("xl/marker.txt", marker)
    return path.read_bytes()


class ExcelExportDialogTests(unittest.TestCase):
    def test_native_save_dialog_returns_user_selected_path_without_interpolation(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="/tmp/community-user/Downloads/数据汇总.xlsx\n",
            stderr="",
        )
        with patch.object(runner.subprocess, "run", return_value=completed) as run:
            result = runner._run_excel_save_dialog("../../unsafe/name.xlsx")

        self.assertTrue(result["ok"])
        self.assertFalse(result["cancelled"])
        self.assertEqual(result["path"], "/tmp/community-user/Downloads/数据汇总.xlsx")
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/osascript")
        self.assertEqual(command[-1], "name.xlsx")
        self.assertNotIn("unsafe", command[-2])

    def test_native_save_dialog_reports_user_cancellation(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="execution error: User canceled. (-128)\n",
        )
        with patch.object(runner.subprocess, "run", return_value=completed):
            result = runner._run_excel_save_dialog("all_channels_enriched.xlsx")

        self.assertTrue(result["ok"])
        self.assertTrue(result["cancelled"])
        self.assertNotIn("path", result)

    def test_excel_copy_adds_extension_and_replaces_atomically(self):
        with tempfile.TemporaryDirectory(prefix="excel-save-test-") as temp_dir:
            root = pathlib.Path(temp_dir)
            source = root / "source.xlsx"
            expected_bytes = _write_minimal_xlsx(source, "new")
            selected = root / "用户选择的报表"
            expected = root / "用户选择的报表.xlsx"
            _write_minimal_xlsx(expected, "old")

            saved_path = runner._save_excel_to_selected_path(str(source), str(selected))

            self.assertEqual(saved_path, str(expected))
            self.assertEqual(expected.read_bytes(), expected_bytes)
            self.assertEqual(list(root.glob(".excel-export-*.tmp")), [])

    def test_invalid_source_never_replaces_existing_destination(self):
        with tempfile.TemporaryDirectory(prefix="excel-save-invalid-") as temp_dir:
            root = pathlib.Path(temp_dir)
            source = root / "source.xlsx"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("payload.json", '{"ok": false, "error": "session_required"}')
            destination = root / "existing.xlsx"
            original = _write_minimal_xlsx(destination, "existing")

            with self.assertRaisesRegex(ValueError, "excel_source_invalid"):
                runner._save_excel_to_selected_path(str(source), str(destination))

            self.assertEqual(destination.read_bytes(), original)
            self.assertEqual(list(root.glob(".excel-export-*.tmp")), [])

    def _handler(self, payload):
        handler = runner.Handler.__new__(runner.Handler)
        handler.path = "/export-excel"
        handler.headers = {}
        handler.client_address = ("127.0.0.1", 12345)
        handler._require_request_security = lambda: True
        handler._read_json_body = lambda: payload
        handler._append_log = lambda *args, **kwargs: None
        responses = []
        handler._send_json = lambda status, body: responses.append((status, body))
        return handler, responses

    def test_export_endpoint_returns_selected_save_path(self):
        handler, responses = self._handler({
            "file": "all",
            "platforms": ["douyin", "xiaohongshu"],
        })
        handler._prepare_excel_export_file = lambda which, platforms: (
            "/state/downloads/all_channels_enriched.xlsx",
            None,
        )

        with (
            patch.object(
                runner,
                "_run_excel_save_dialog",
                return_value={
                    "ok": True,
                    "cancelled": False,
                    "path": "/tmp/community-user/Desktop/客户数据.xlsx",
                },
            ),
            patch.object(
                runner,
                "_save_excel_to_selected_path",
                return_value="/tmp/community-user/Desktop/客户数据.xlsx",
            ) as save,
        ):
            handler.do_POST()

        self.assertEqual(responses[0][0], 200)
        self.assertEqual(responses[0][1]["path"], "/tmp/community-user/Desktop/客户数据.xlsx")
        self.assertFalse(responses[0][1]["cancelled"])
        save.assert_called_once_with(
            "/state/downloads/all_channels_enriched.xlsx",
            "/tmp/community-user/Desktop/客户数据.xlsx",
        )

    def test_export_endpoint_rejects_unknown_scope(self):
        handler, responses = self._handler({"file": "../customer-secrets.json"})
        handler.do_POST()
        self.assertEqual(responses[0][0], 400)
        self.assertEqual(responses[0][1]["error"], "excel_scope_invalid")

    def test_export_endpoint_stops_when_session_guard_rejects_request(self):
        handler = runner.Handler.__new__(runner.Handler)
        handler.path = "/export-excel"
        handler._require_request_security = lambda: False
        handler._read_json_body = lambda: self.fail("body must not be read after security rejection")
        handler.do_POST()


if __name__ == "__main__":
    unittest.main()
