import pathlib
import tempfile
import unittest

from orchestration.run_artifacts import ArtifactValidationError, RunWorkspace


class RunArtifactsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp_dir.name)
        self.downloads = self.root / "downloads"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_failed_validation_keeps_previous_official_file(self):
        official = self.downloads / "bilibili_all_videos.xlsx"
        official.parent.mkdir(parents=True)
        official.write_bytes(b"old")
        workspace = RunWorkspace(self.downloads, "run-1", "bilibili")
        staged = workspace.stage_path("bilibili_all_videos.xlsx")
        staged.write_bytes(b"partial")

        def reject():
            raise ArtifactValidationError("coverage_mismatch")

        with self.assertRaisesRegex(ArtifactValidationError, "coverage_mismatch"):
            workspace.promote({staged: official}, validator=reject)

        self.assertEqual(official.read_bytes(), b"old")
        self.assertEqual(staged.read_bytes(), b"partial")

    def test_successful_promotion_replaces_output(self):
        official = self.downloads / "xiaohongshu_rows.json"
        official.parent.mkdir(parents=True)
        official.write_text("old", encoding="utf-8")
        workspace = RunWorkspace(self.downloads, "run-1", "xiaohongshu")
        staged = workspace.stage_path("xiaohongshu_rows.json")
        staged.write_text("new", encoding="utf-8")

        workspace.promote({staged: official}, validator=lambda: None)

        self.assertEqual(official.read_text(encoding="utf-8"), "new")
        self.assertFalse(staged.exists())

    def test_missing_staged_artifact_never_moves_official_file(self):
        official = self.downloads / "kuaishou_rows.json"
        official.parent.mkdir(parents=True)
        official.write_text("old", encoding="utf-8")
        workspace = RunWorkspace(self.downloads, "run-1", "kuaishou")
        staged = workspace.stage_path("missing.json")

        with self.assertRaisesRegex(ArtifactValidationError, "missing_artifact"):
            workspace.promote({staged: official}, validator=lambda: None)

        self.assertEqual(official.read_text(encoding="utf-8"), "old")


if __name__ == "__main__":
    unittest.main()
