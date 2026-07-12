"""Manager CLI output behavior with console and windowless interpreters."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import codex_reset_manager as manager


class ManagerConsoleOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.controller = mock.Mock()
        self.controller.sync.return_value = {"automation": "on"}
        self.controller.bootstrap_status.return_value = {"automation": "on"}
        self.controller.doctor.return_value = {
            "healthy": True,
            "issues": [],
            "status": {"automation": "on"},
        }

    def run_main(self, argv: list[str]) -> int:
        with mock.patch.object(manager, "Controller", return_value=self.controller):
            return manager.main(["--root", str(self.root), *argv])

    def test_scheduled_sync_succeeds_without_stdout_or_stderr(self) -> None:
        with (
            mock.patch.object(manager.sys, "stdout", None),
            mock.patch.object(manager.sys, "stderr", None),
        ):
            exit_code = self.run_main(["sync", "--scheduled"])

        self.assertEqual(exit_code, 0)
        self.controller.sync.assert_called_once_with(scheduled=True)

    def test_status_succeeds_without_stdout(self) -> None:
        with mock.patch.object(manager.sys, "stdout", None):
            exit_code = self.run_main(["status", "--json"])

        self.assertEqual(exit_code, 0)
        self.controller.bootstrap_status.assert_called_once_with()

    def test_doctor_preserves_result_exit_code_without_stdout(self) -> None:
        with mock.patch.object(manager.sys, "stdout", None):
            healthy_exit = self.run_main(["doctor"])

        self.controller.doctor.return_value = {
            "healthy": False,
            "issues": ["TASK_CONTRACT_INVALID"],
            "status": {"automation": "attention"},
        }
        with mock.patch.object(manager.sys, "stdout", None):
            unhealthy_exit = self.run_main(["doctor"])

        self.assertEqual(healthy_exit, 0)
        self.assertEqual(unhealthy_exit, 1)

    def test_manager_error_preserves_failure_exit_code_without_stderr(self) -> None:
        self.controller.sync.side_effect = manager.ManagerError("CONTROLLER_BUSY")

        with mock.patch.object(manager.sys, "stderr", None):
            exit_code = self.run_main(["sync", "--scheduled"])

        self.assertEqual(exit_code, 1)

    def test_console_status_output_is_preserved(self) -> None:
        output = io.StringIO()

        with mock.patch.object(manager.sys, "stdout", output):
            exit_code = self.run_main(["status"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            output.getvalue(),
            "Automatic use: On\nCodex CLI: Needs attention\nWindows time: Needs attention\n",
        )

    def test_console_status_uses_reset_terminology(self) -> None:
        self.controller.bootstrap_status.return_value = {
            "automation": "on",
            "nextExpiresAtUtc": "2030-01-01T01:00:00Z",
        }
        output = io.StringIO()

        with mock.patch.object(manager.sys, "stdout", output):
            exit_code = self.run_main(["status"])

        self.assertEqual(exit_code, 0)
        expected_time = manager._local_time(manager._utc_epoch("2030-01-01T01:00:00Z"))
        self.assertIn(f"Next reset expires: {expected_time}", output.getvalue())
        self.assertNotIn("credit", output.getvalue().casefold())

    def test_cli_help_uses_usage_limit_reset_product_name(self) -> None:
        self.assertEqual(manager.APP_VERSION, "2.4.0")
        self.assertEqual(
            manager._build_parser().description,
            "Manage automatic use of Codex usage limit resets",
        )

    def test_console_error_output_is_preserved(self) -> None:
        self.controller.sync.side_effect = manager.ManagerError("CONTROLLER_BUSY")
        output = io.StringIO()

        with mock.patch.object(manager.sys, "stderr", output):
            exit_code = self.run_main(["sync", "--scheduled"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(output.getvalue(), "error: CONTROLLER_BUSY\n")


if __name__ == "__main__":
    unittest.main()
