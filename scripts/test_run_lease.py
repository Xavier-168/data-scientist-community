import pathlib
import tempfile
import threading
import unittest

from orchestration.run_lease import RunLeaseStore


class RunLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.temp_dir.name) / "runner.lock"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_live_owner_does_not_expire_only_because_timestamp_is_old(self):
        store = RunLeaseStore(self.path, ttl_seconds=1, pid_alive=lambda pid: pid == 42)

        token = store.acquire("run_all", owner_pid=42, now=10)

        self.assertIsNotNone(token)
        self.assertTrue(store.is_active(now=999))
        self.assertTrue(self.path.exists())

    def test_dead_owner_with_expired_heartbeat_is_reclaimed(self):
        store = RunLeaseStore(self.path, ttl_seconds=10, pid_alive=lambda _pid: False)
        token = store.acquire("run_all", owner_pid=42, now=10)

        self.assertIsNotNone(token)
        self.assertFalse(store.is_active(now=21))
        self.assertFalse(self.path.exists())

    def test_old_owner_cannot_release_replacement_lease(self):
        store = RunLeaseStore(self.path, ttl_seconds=1, pid_alive=lambda _pid: False)
        old = store.acquire("run_all", owner_pid=1, now=1)
        new = store.acquire("run_all", owner_pid=2, now=3)

        self.assertIsNotNone(old)
        self.assertIsNotNone(new)
        self.assertNotEqual(old.run_id, new.run_id)
        self.assertFalse(store.release(old.run_id))
        self.assertTrue(store.is_active(now=3))

    def test_heartbeat_requires_matching_run_id(self):
        store = RunLeaseStore(self.path, ttl_seconds=10, pid_alive=lambda _pid: True)
        token = store.acquire("run_all", owner_pid=42, now=1)

        self.assertIsNotNone(token)
        self.assertFalse(store.heartbeat("wrong", now=2))
        self.assertTrue(store.heartbeat(token.run_id, now=2))
        self.assertEqual(store.read_payload()["heartbeat_at"], 2)

    def test_concurrent_acquire_has_single_winner(self):
        store = RunLeaseStore(self.path, ttl_seconds=30, pid_alive=lambda _pid: True)
        barrier = threading.Barrier(3)
        tokens = []

        def acquire(owner_pid):
            barrier.wait()
            tokens.append(store.acquire("run_all", owner_pid=owner_pid, now=1))

        threads = [threading.Thread(target=acquire, args=(owner,)) for owner in (41, 42)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(sum(token is not None for token in tokens), 1)


if __name__ == "__main__":
    unittest.main()
