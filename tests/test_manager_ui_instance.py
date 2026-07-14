"""Single-instance lock and show-request marker tests for the manager UI."""

from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import codex_reset_manager as manager


class ManagerUiInstanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "state"

    def test_ready_marker_is_secret_free_and_binds_runtime_pid_and_tray(self) -> None:
        runtime = Path(self.temporary.name) / "manager-runtime.py"
        runtime.write_bytes(b"immutable manager fixture\n")
        marker_path = manager._publish_ui_ready(
            self.state_dir,
            manager_path=runtime,
            pid=4242,
            now=lambda: 2_000_000_000,
        )
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(marker),
            {"schemaVersion", "pid", "readyAtUtc", "managerSha256", "trayReady"},
        )
        self.assertEqual(marker["schemaVersion"], 1)
        self.assertEqual(marker["pid"], 4242)
        self.assertTrue(marker["trayReady"])
        self.assertEqual(marker["readyAtUtc"], manager._utc_text(2_000_000_000))
        self.assertEqual(
            marker["managerSha256"], hashlib.sha256(runtime.read_bytes()).hexdigest()
        )
        rendered = json.dumps(marker).casefold()
        for secret_name in ("creditid", "email", "token", "idempotency"):
            self.assertNotIn(secret_name, rendered)

    def test_primary_publishes_ready_and_release_removes_it(self) -> None:
        runtime = Path(self.temporary.name) / "manager-runtime.py"
        runtime.write_bytes(b"immutable manager fixture\n")
        lease = manager.UiInstanceLease(self.state_dir, now=lambda: 2_000_000_000)
        self.assertTrue(lease.acquire())

        lease.publish_ready(runtime)
        self.assertTrue(lease.ready_published)
        self.assertTrue(lease.ready_path.is_file())
        lease.release()

        self.assertFalse(lease.ready_published)
        self.assertFalse(lease.ready_path.exists())

    def test_new_primary_removes_stale_ready_marker_before_startup(self) -> None:
        self.state_dir.mkdir(parents=True)
        ready = self.state_dir / manager.UI_READY_FILENAME
        ready.write_text('{"trayReady":true}', encoding="utf-8")

        lease = manager.UiInstanceLease(self.state_dir)
        self.addCleanup(lease.release)
        self.assertTrue(lease.acquire())
        self.assertFalse(ready.exists())

    def test_ready_publish_error_leaves_no_marker(self) -> None:
        runtime = Path(self.temporary.name) / "manager-runtime.py"
        runtime.write_bytes(b"immutable manager fixture\n")
        lease = manager.UiInstanceLease(self.state_dir)
        self.addCleanup(lease.release)
        self.assertTrue(lease.acquire())

        with mock.patch.object(manager, "_atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(manager.ManagerError) as caught:
                lease.publish_ready(runtime)

        self.assertEqual(caught.exception.code, "UI_READY_PUBLISH_FAILED")
        self.assertFalse(lease.ready_published)
        self.assertFalse(lease.ready_path.exists())

    def test_ready_cannot_be_published_without_primary_lock(self) -> None:
        runtime = Path(self.temporary.name) / "manager-runtime.py"
        runtime.write_bytes(b"immutable manager fixture\n")
        lease = manager.UiInstanceLease(self.state_dir)

        with self.assertRaises(manager.ManagerError) as caught:
            lease.publish_ready(runtime)

        self.assertEqual(caught.exception.code, "UI_READY_WITHOUT_LOCK")

    def test_second_instance_requests_show_and_exits_without_stealing_lock(self) -> None:
        primary = manager.UiInstanceLease(self.state_dir, now=lambda: 2_000_000_000)
        secondary = manager.UiInstanceLease(self.state_dir, now=lambda: 2_000_000_001)
        self.addCleanup(primary.release)

        self.assertTrue(primary.acquire())
        self.assertFalse(secondary.acquire())
        marker = json.loads(secondary.show_request_path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(marker),
            {"schemaVersion", "request", "requestedAtUtc", "requestId"},
        )
        self.assertEqual(marker["request"], "show")
        self.assertTrue(primary.consume_show_request())
        self.assertFalse(primary.show_request_path.exists())

    def test_new_primary_removes_a_stale_show_marker(self) -> None:
        stale = manager.UiInstanceLease(self.state_dir)
        stale.request_show()
        self.assertTrue(stale.show_request_path.is_file())

        primary = manager.UiInstanceLease(self.state_dir)
        self.addCleanup(primary.release)
        self.assertTrue(primary.acquire())
        self.assertFalse(primary.show_request_path.exists())
        self.assertFalse(primary.consume_show_request())

    def test_invalid_show_marker_is_removed_without_triggering_restore(self) -> None:
        lease = manager.UiInstanceLease(self.state_dir)
        self.state_dir.mkdir(parents=True)
        lease.show_request_path.write_text('{"request":"invalid"}', encoding="utf-8")

        self.assertFalse(lease.consume_show_request())
        self.assertFalse(lease.show_request_path.exists())

    def test_invalid_uuid_is_removed_without_triggering_restore(self) -> None:
        lease = manager.UiInstanceLease(self.state_dir, now=lambda: 2_000_000_000)
        lease.request_show()
        marker = json.loads(lease.show_request_path.read_text(encoding="utf-8"))
        marker["requestId"] = "not-a-uuid"
        lease.show_request_path.write_text(json.dumps(marker), encoding="utf-8")

        self.assertFalse(lease.consume_show_request())
        self.assertFalse(lease.show_request_path.exists())

    def test_non_v4_or_noncanonical_uuid_is_rejected(self) -> None:
        for request_id in (
            "67e55044-10b1-426f-9247-bb680e5fe0c8".upper(),
            "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
        ):
            with self.subTest(request_id=request_id):
                lease = manager.UiInstanceLease(self.state_dir, now=lambda: 2_000_000_000)
                lease.request_show()
                marker = json.loads(lease.show_request_path.read_text(encoding="utf-8"))
                marker["requestId"] = request_id
                lease.show_request_path.write_text(json.dumps(marker), encoding="utf-8")
                self.assertFalse(lease.consume_show_request())

    def test_invalid_utc_timestamp_is_removed_without_focus(self) -> None:
        lease = manager.UiInstanceLease(self.state_dir, now=lambda: 2_000_000_000)
        lease.request_show()
        marker = json.loads(lease.show_request_path.read_text(encoding="utf-8"))
        marker["requestedAtUtc"] = "2033-05-18T03:33:20+00:00"
        lease.show_request_path.write_text(json.dumps(marker), encoding="utf-8")

        self.assertFalse(lease.consume_show_request())
        self.assertFalse(lease.show_request_path.exists())

    def test_stale_and_excessively_future_markers_are_removed(self) -> None:
        for offset in (
            -(manager.UI_SHOW_REQUEST_MAX_AGE_SECONDS + 1),
            manager.UI_SHOW_REQUEST_FUTURE_SKEW_SECONDS + 1,
        ):
            with self.subTest(offset=offset):
                issued_at = 2_000_000_000 + offset
                writer = manager.UiInstanceLease(self.state_dir, now=lambda: issued_at)
                writer.request_show()
                reader = manager.UiInstanceLease(
                    self.state_dir,
                    now=lambda: 2_000_000_000,
                )
                self.assertFalse(reader.consume_show_request())
                self.assertFalse(reader.show_request_path.exists())

    def test_small_future_clock_skew_is_accepted(self) -> None:
        writer = manager.UiInstanceLease(
            self.state_dir,
            now=lambda: 2_000_000_000 + manager.UI_SHOW_REQUEST_FUTURE_SKEW_SECONDS,
        )
        writer.request_show()
        reader = manager.UiInstanceLease(self.state_dir, now=lambda: 2_000_000_000)

        self.assertTrue(reader.consume_show_request())

    def test_lock_is_released_when_owning_process_terminates(self) -> None:
        lock_path = self.state_dir / manager.UI_LOCK_FILENAME
        script = (
            "import sys,time; from pathlib import Path; "
            "from codex_reset_manager import FileLock; "
            "lock=FileLock(Path(sys.argv[1]),busy_code='BUSY'); "
            "lock.__enter__(); print('ready',flush=True); time.sleep(60)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(lock_path)],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=manager.WINDOWLESS_SUBPROCESS_FLAGS,
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.assertEqual(process.stdout.readline().strip(), "ready")
        contender = manager.UiInstanceLease(self.state_dir)
        self.assertFalse(contender.acquire())
        process.kill()
        process.communicate(timeout=10)

        replacement = manager.UiInstanceLease(self.state_dir)
        self.addCleanup(replacement.release)
        self.assertTrue(replacement.acquire())

    def test_release_removes_marker_and_allows_next_instance(self) -> None:
        first = manager.UiInstanceLease(self.state_dir)
        self.assertTrue(first.acquire())
        first.request_show()
        first.release()

        second = manager.UiInstanceLease(self.state_dir)
        self.addCleanup(second.release)
        self.assertTrue(second.acquire())
        self.assertFalse(second.show_request_path.exists())


if __name__ == "__main__":
    unittest.main()
