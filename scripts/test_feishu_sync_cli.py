import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch


MODULE_PATH = pathlib.Path(__file__).with_name("sync_feishu_bitable_openapi.py")
SPEC = importlib.util.spec_from_file_location("sync_feishu_openapi_module", MODULE_PATH)
sync_feishu = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(sync_feishu)


class FeishuCliPagingTests(unittest.TestCase):
    def test_list_records_cli_pages_with_offset(self):
        with patch.object(
            sync_feishu,
            "cli_record_list_json",
            side_effect=[
                {
                    "data": {
                        "fields": ["同步键", "标题"],
                        "data": [
                            ["key-1", "作品 1"],
                            ["key-2", "作品 2"],
                        ],
                        "record_id_list": ["rec-1", "rec-2"],
                        "has_more": True,
                    }
                },
                {
                    "data": {
                        "fields": ["同步键", "标题"],
                        "data": [["key-3", "作品 3"]],
                        "record_id_list": ["rec-3"],
                        "has_more": False,
                    }
                },
            ],
        ) as cli_mock:
            rows = sync_feishu.list_records("app_token_123", "tbl_detail", None, use_cli=True)

        self.assertEqual(
            rows,
            [
                {"record_id": "rec-1", "fields": {"同步键": "key-1", "标题": "作品 1"}},
                {"record_id": "rec-2", "fields": {"同步键": "key-2", "标题": "作品 2"}},
                {"record_id": "rec-3", "fields": {"同步键": "key-3", "标题": "作品 3"}},
            ],
        )
        self.assertEqual(cli_mock.call_count, 2)
        self.assertEqual(cli_mock.call_args_list[0].kwargs["offset"], 0)
        self.assertEqual(cli_mock.call_args_list[1].kwargs["offset"], 2)


class FeishuCliSyncTableTests(unittest.TestCase):
    def test_sync_table_cli_mode_degrades_when_field_creation_is_limited(self):
        table_definition = {
            "name": "平台明细V2",
            "fields": [
                {"field_name": "同步键"},
                {"field_name": "标题"},
                {"field_name": "点赞量"},
            ],
        }
        table_map = {"平台明细V2": {"table_id": "tbl_detail", "name": "平台明细V2"}}
        create_calls = []

        with (
            patch.object(sync_feishu, "cli_list_fields", return_value={"同步键": {}, "标题": {}}),
            patch.object(
                sync_feishu,
                "cli_create_field",
                side_effect=RuntimeError(
                    'lark-cli 命令失败 (exit 1): {"ok":false,"error":{"code":800004135,"message":"API call failed: [800004135] the method：OpenAPIAddField limited"}}'
                ),
            ),
            patch.object(sync_feishu, "list_records", return_value=[]),
            patch.object(
                sync_feishu,
                "batch_create_records",
                side_effect=lambda app_token, table_id, token, records, use_cli=False: create_calls.extend(
                    item["fields"] for item in records
                ) or {"data": {"records": [{"record_id": "rec-1"}]}},
            ),
            patch.object(sync_feishu.time, "sleep"),
        ):
            result = sync_feishu.sync_table(
                "app_token_123",
                table_definition,
                [{"同步键": "sync-key-1", "标题": "作品 1", "点赞量": 100}],
                None,
                table_map,
                use_cli=True,
            )

        self.assertEqual(result["created"], 1)
        self.assertEqual(create_calls, [{"同步键": "sync-key-1", "标题": "作品 1"}])
        self.assertIn("以下字段在飞书表中不存在", result["warnings"][0])
        self.assertEqual(result["missing_fields"], ["点赞量"])

    def test_sync_table_retries_limited_field_then_writes_complete_row(self):
        table_definition = {
            "name": "平台明细V2",
            "fields": [
                {"field_name": "同步键"},
                {"field_name": "标题"},
                {"field_name": "3s跳出率"},
            ],
        }
        table_map = {"平台明细V2": {"table_id": "tbl_detail", "name": "平台明细V2"}}
        limited = RuntimeError(
            'lark-cli 命令失败 (exit 1): {"error":{"code":800004135,"message":"OpenAPIAddField limited"}}'
        )
        create_calls = []

        with (
            patch.object(
                sync_feishu,
                "cli_list_fields",
                side_effect=[
                    {"同步键": {}, "标题": {}},
                    {"同步键": {}, "标题": {}},
                    {"同步键": {}, "标题": {}},
                    {"同步键": {}, "标题": {}, "3s跳出率": {}},
                ],
            ),
            patch.object(sync_feishu, "cli_create_field", side_effect=[limited, "fld-rate"]) as field_mock,
            patch.object(sync_feishu, "cli_set_visible_fields", return_value={}),
            patch.object(sync_feishu, "mark_schema_state_current"),
            patch.object(sync_feishu, "list_records", return_value=[]),
            patch.object(
                sync_feishu,
                "batch_create_records",
                side_effect=lambda app_token, table_id, token, records, use_cli=False: create_calls.extend(
                    item["fields"] for item in records
                ) or {"data": {"records": [{"record_id": "rec-1"}]}},
            ),
            patch.object(sync_feishu.time, "sleep"),
        ):
            result = sync_feishu.sync_table(
                "app_token_123",
                table_definition,
                [{"同步键": "sync-key-1", "标题": "作品 1", "3s跳出率": 0.25}],
                None,
                table_map,
                use_cli=True,
                strict_schema=True,
            )

        self.assertEqual(field_mock.call_count, 2)
        self.assertEqual(result["created"], 1)
        self.assertEqual(create_calls, [{"同步键": "sync-key-1", "标题": "作品 1", "3s跳出率": 0.25}])
        self.assertEqual(result["missing_fields"], [])

    def test_sync_table_accepts_field_that_appears_after_limited_response(self):
        table_definition = {
            "name": "平台明细V2",
            "fields": [{"field_name": "同步键"}, {"field_name": "3s跳出率"}],
        }
        table_map = {"平台明细V2": {"table_id": "tbl_detail", "name": "平台明细V2"}}
        limited = RuntimeError(
            'lark-cli 命令失败 (exit 1): {"error":{"code":800004135,"message":"OpenAPIAddField limited"}}'
        )

        with (
            patch.object(
                sync_feishu,
                "cli_list_fields",
                side_effect=[
                    {"同步键": {}},
                    {"同步键": {}, "3s跳出率": {}},
                    {"同步键": {}, "3s跳出率": {}},
                ],
            ),
            patch.object(sync_feishu, "cli_create_field", side_effect=limited) as field_mock,
            patch.object(sync_feishu, "cli_set_visible_fields", return_value={}),
            patch.object(sync_feishu, "mark_schema_state_current"),
            patch.object(sync_feishu, "list_records", return_value=[]),
            patch.object(sync_feishu.time, "sleep"),
        ):
            result = sync_feishu.sync_table(
                "app_token_123",
                table_definition,
                [],
                None,
                table_map,
                use_cli=True,
                strict_schema=True,
            )

        self.assertEqual(field_mock.call_count, 1)
        self.assertEqual(result["missing_fields"], [])

    def test_sync_table_skips_rows_already_recorded_in_checkpoint(self):
        table_definition = {
            "name": "平台明细V2",
            "fields": [
                {"field_name": "同步键"},
                {"field_name": "标题"},
            ],
        }
        table_map = {"平台明细V2": {"table_id": "tbl_detail", "name": "平台明细V2"}}
        row1 = {"同步键": "sync-key-1", "标题": "作品 1"}
        row2 = {"同步键": "sync-key-2", "标题": "作品 2"}
        checkpoint = {
            "version": sync_feishu.CHECKPOINT_VERSION,
            "tables": {
                "平台明细V2": {
                    "sync-key-1": sync_feishu.row_checkpoint_hash(row1),
                }
            },
        }
        create_calls = []

        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint_path = pathlib.Path(temp_dir) / "checkpoint.json"
            with (
                patch.object(sync_feishu, "list_records", return_value=[
                    {"record_id": "rec-1", "fields": {"同步键": "sync-key-1", "标题": "作品 1"}},
                ]),
                patch.object(
                    sync_feishu,
                    "create_record",
                    side_effect=lambda app_token, table_id, token, fields, use_cli=False: create_calls.append(fields) or {"data": {"record_id": "rec-new"}},
                ),
                patch.object(
                    sync_feishu,
                    "batch_create_records",
                    return_value={"data": {"records": [{"record_id": "rec-new"}]}},
                ),
            ):
                result = sync_feishu.sync_table(
                    "app_token_123",
                    table_definition,
                    [row1, row2],
                    "tenant-token",
                    table_map,
                    use_cli=False,
                    checkpoint=checkpoint,
                    checkpoint_path=checkpoint_path,
                )

            written = json.loads(checkpoint_path.read_text(encoding="utf-8"))

        self.assertEqual(result["created"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(
            written["tables"]["平台明细V2"]["sync-key-2"],
            sync_feishu.row_checkpoint_hash(row2),
        )


class FeishuSyncTransactionTests(unittest.TestCase):
    def test_table_timeout_returns_failure_and_keeps_checkpoint(self):
        definitions = [{"name": "平台明细V2"}, {"name": "作品总表V2"}]
        tables = {"平台明细V2": [], "作品总表V2": []}
        table_map = {
            "平台明细V2": {"table_id": "tbl_detail"},
            "作品总表V2": {"table_id": "tbl_work"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint_path = pathlib.Path(temp_dir) / "checkpoint.json"
            checkpoint_path.write_text('{"version":1,"tables":{}}', encoding="utf-8")
            with (
                patch.object(
                    sync_feishu,
                    "sync_table",
                    side_effect=[
                        {
                            "table": "平台明细V2",
                            "created": 1,
                            "updated": 0,
                            "deleted": 0,
                            "skipped": 0,
                            "warnings": [],
                        },
                        sync_feishu.SyncTimeoutError("同步超时"),
                    ],
                ),
                patch.object(sync_feishu.signal, "signal"),
                patch.object(sync_feishu.signal, "alarm", create=True),
            ):
                result = sync_feishu.sync_all_tables(
                    "app-token",
                    definitions,
                    tables,
                    None,
                    table_map,
                    use_cli=True,
                    checkpoint={"version": 1, "tables": {}},
                    checkpoint_path=checkpoint_path,
                    strict_schema=False,
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "sync_timeout")
            self.assertEqual(result["failed_table"], "作品总表V2")
            self.assertTrue(checkpoint_path.exists())

    def test_all_tables_success_removes_checkpoint(self):
        definitions = [{"name": "平台明细V2"}]
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint_path = pathlib.Path(temp_dir) / "checkpoint.json"
            checkpoint_path.write_text('{"version":1,"tables":{}}', encoding="utf-8")
            with (
                patch.object(
                    sync_feishu,
                    "sync_table",
                    return_value={
                        "table": "平台明细V2",
                        "created": 1,
                        "updated": 0,
                        "deleted": 0,
                        "skipped": 0,
                        "warnings": [],
                    },
                ),
                patch.object(sync_feishu.signal, "signal"),
                patch.object(sync_feishu.signal, "alarm", create=True),
            ):
                result = sync_feishu.sync_all_tables(
                    "app-token",
                    definitions,
                    {"平台明细V2": []},
                    None,
                    {"平台明细V2": {"table_id": "tbl_detail"}},
                    use_cli=True,
                    checkpoint={"version": 1, "tables": {}},
                    checkpoint_path=checkpoint_path,
                    strict_schema=False,
                )

            self.assertTrue(result["ok"])
            self.assertFalse(checkpoint_path.exists())


class LarkCliBinaryResolutionTests(unittest.TestCase):
    def test_resolve_lark_cli_runner_prefers_cached_binary(self):
        with (
            patch.object(sync_feishu, "resolve_lark_cli_bin", return_value="/tmp/lark-cli"),
            patch.object(sync_feishu, "resolve_npx_bin", return_value="/usr/local/bin/npx"),
        ):
            runner = sync_feishu.resolve_lark_cli_runner()

        self.assertEqual(runner, ["/tmp/lark-cli"])

    def test_batch_create_records_uses_openapi_http_path_for_app_mode(self):
        records = [
            {"key": "sync-key-1", "fields": {"同步键": "sync-key-1", "标题": "作品 1"}},
            {"key": "sync-key-2", "fields": {"同步键": "sync-key-2", "标题": "作品 2"}},
        ]

        with patch.object(sync_feishu, "http_json", return_value={"ok": True}) as http_mock:
            sync_feishu.batch_create_records("app_token_123", "tbl_detail", "tenant-token", records, use_cli=False)

        self.assertEqual(http_mock.call_count, 1)
        self.assertIn("/records/batch_create?ignore_consistency_check=true", http_mock.call_args.args[1])
        self.assertEqual(len(http_mock.call_args.kwargs["payload"]["records"]), 2)


if __name__ == "__main__":
    unittest.main()
