import collections
import threading
import time
import unittest

from orchestration.collection_scheduler import PlatformResult, run_bounded


class ConcurrencyProbe:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def run(self, platform):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.08)
        with self.lock:
            self.active -= 1
        return PlatformResult(platform=platform, outcome="success")


class CollectionSchedulerTests(unittest.TestCase):
    def test_scheduler_never_runs_more_than_two_platforms(self):
        probe = ConcurrencyProbe()
        platforms = ["douyin", "xiaohongshu", "bilibili", "kuaishou"]

        results = run_bounded(platforms, probe.run, max_workers=2)

        self.assertEqual(list(results), platforms)
        self.assertEqual(probe.max_active, 2)
        self.assertTrue(all(result.outcome == "success" for result in results.values()))

    def test_retryable_failure_runs_once_after_parallel_phase(self):
        attempts = collections.Counter()

        def run_one(platform):
            attempts[platform] += 1
            if attempts[platform] == 1:
                return PlatformResult(platform=platform, outcome="failed", retryable=True)
            return PlatformResult(platform=platform, outcome="success")

        results = run_bounded(["douyin"], run_one, max_workers=2)

        self.assertEqual(results["douyin"].outcome, "success")
        self.assertEqual(attempts["douyin"], 2)

    def test_non_retryable_failure_is_not_repeated(self):
        attempts = collections.Counter()

        def run_one(platform):
            attempts[platform] += 1
            return PlatformResult(platform=platform, outcome="auth_required", retryable=False)

        results = run_bounded(["douyin"], run_one, max_workers=2)

        self.assertEqual(results["douyin"].outcome, "auth_required")
        self.assertEqual(attempts["douyin"], 1)

    def test_exception_becomes_retryable_failure(self):
        attempts = collections.Counter()

        def run_one(platform):
            attempts[platform] += 1
            if attempts[platform] == 1:
                raise RuntimeError("browser_crashed")
            return PlatformResult(platform=platform, outcome="success")

        results = run_bounded(["douyin"], run_one, max_workers=2)

        self.assertEqual(results["douyin"].outcome, "success")
        self.assertEqual(attempts["douyin"], 2)


if __name__ == "__main__":
    unittest.main()
