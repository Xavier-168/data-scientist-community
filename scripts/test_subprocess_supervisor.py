import os
import pathlib
import sys
import tempfile
import time
import unittest

from orchestration.subprocess_supervisor import run_supervised


class SubprocessSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp_dir.name)
        self.log_path = self.root / "platform.log"
        self.progress_path = self.root / "progress.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_active_process_can_run_longer_than_inactivity_timeout(self):
        code = (
            "import time\n"
            "for index in range(24):\n"
            " print(index, flush=True)\n"
            " time.sleep(0.1)\n"
        )

        result = run_supervised(
            [sys.executable, "-c", code],
            env=os.environ.copy(),
            cwd=self.root,
            log_path=self.log_path,
            inactivity_timeout=1,
        )

        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.returncode, 0)
        self.assertGreater(result.duration_seconds, 2)
        self.assertIn("23", result.stdout_tail)

    def test_silent_process_is_killed_after_inactivity(self):
        started = time.monotonic()

        result = run_supervised(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            env=os.environ.copy(),
            cwd=self.root,
            log_path=self.log_path,
            inactivity_timeout=0.3,
        )

        self.assertEqual(result.outcome, "stalled")
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 2)

    def test_progress_file_changes_count_as_activity(self):
        code = (
            "import pathlib,time\n"
            f"path = pathlib.Path({str(self.progress_path)!r})\n"
            "for index in range(18):\n"
            " path.write_text(str(index), encoding='utf-8')\n"
            " time.sleep(0.1)\n"
        )

        result = run_supervised(
            [sys.executable, "-c", code],
            env=os.environ.copy(),
            cwd=self.root,
            log_path=self.log_path,
            progress_path=self.progress_path,
            inactivity_timeout=1,
        )

        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.returncode, 0)

    def test_heartbeat_runs_while_process_is_alive(self):
        calls = []
        heartbeat_path = self.root / "heartbeat.txt"
        code = (
            "import pathlib,time\n"
            f"path = pathlib.Path({str(heartbeat_path)!r})\n"
            "deadline = time.monotonic() + 5\n"
            "while time.monotonic() < deadline:\n"
            " if path.exists() and len(path.read_text(encoding='utf-8').splitlines()) >= 2:\n"
            "  break\n"
            " time.sleep(0.01)\n"
            "else:\n"
            " raise SystemExit(2)\n"
        )

        def heartbeat():
            calls.append(time.monotonic())
            with heartbeat_path.open("a", encoding="utf-8") as handle:
                handle.write("beat\n")

        result = run_supervised(
            [sys.executable, "-c", code],
            env=os.environ.copy(),
            cwd=self.root,
            log_path=self.log_path,
            inactivity_timeout=2,
            heartbeat=heartbeat,
            heartbeat_interval=0.03,
            poll_interval=0.03,
        )

        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.returncode, 0)
        self.assertGreaterEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
