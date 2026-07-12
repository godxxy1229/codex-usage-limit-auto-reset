"""Read-only Scheduled Task XML contract tests."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import codex_reset_guard as guard


class ScheduledTaskContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.manifest_path = Path(self.temporary.name) / "job.json"
        self.manifest = {
            "schedule": {
                "triggerAtUtc": "2033-05-18T03:28:35Z",
                "cutoffAtUtc": "2033-05-18T03:34:05Z",
            }
        }

    def xml(self, *, command: str | None = None, allow_demand: str = "false") -> str:
        runner = Path(guard.__file__).resolve()
        python = command or str(Path(sys.executable).resolve())
        arguments = f'&quot;{runner}&quot; run --manifest &quot;{self.manifest_path.resolve()}&quot; --live'
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><TimeTrigger><StartBoundary>2033-05-18T03:28:35Z</StartBoundary><EndBoundary>2033-05-18T03:34:05Z</EndBoundary></TimeTrigger></Triggers>
  <Principals><Principal><UserId>S-1-5-21-1</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowStartOnDemand>{allow_demand}</AllowStartOnDemand>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
  </Settings>
  <Actions><Exec><Command>{python}</Command><Arguments>{arguments}</Arguments><WorkingDirectory>{runner.parent.parent}</WorkingDirectory></Exec></Actions>
</Task>"""

    def validate(self, xml: str) -> None:
        completed = SimpleNamespace(returncode=0, stdout=xml, stderr="")
        with mock.patch.object(guard.subprocess, "run", return_value=completed):
            guard._validate_scheduled_task_contract(
                r"\CodexResetCredit\Fake", self.manifest_path, self.manifest
            )

    def test_exact_installer_contract_is_accepted(self) -> None:
        self.validate(self.xml())

    def test_windowless_python_live_contract_is_accepted(self) -> None:
        pythonw = Path(sys.executable).with_name("pythonw.exe").resolve()
        with mock.patch.object(guard.sys, "executable", str(pythonw)):
            self.validate(self.xml(command=str(pythonw)))

    def test_scheduler_local_offset_normalization_is_accepted(self) -> None:
        xml = self.xml().replace(
            "2033-05-18T03:28:35Z", "2033-05-18T12:28:35+09:00"
        ).replace(
            "2033-05-18T03:34:05Z", "2033-05-18T12:34:05+09:00"
        )
        self.validate(xml)

    def test_schema_default_least_privilege_and_enabled_are_accepted(self) -> None:
        xml = self.xml().replace(
            "<RunLevel>LeastPrivilege</RunLevel>", ""
        ).replace("<Enabled>true</Enabled>", "")
        self.validate(xml)

    def test_explicit_highest_run_level_is_rejected(self) -> None:
        xml = self.xml().replace("LeastPrivilege", "HighestAvailable")
        with self.assertRaises(guard.GuardError):
            self.validate(xml)

    def test_boundary_without_explicit_offset_is_rejected(self) -> None:
        xml = self.xml().replace(
            "2033-05-18T03:28:35Z", "2033-05-18T03:28:35"
        )
        with self.assertRaises(guard.GuardError):
            self.validate(xml)

    def test_changed_python_action_is_rejected(self) -> None:
        with self.assertRaises(guard.GuardError):
            self.validate(self.xml(command=r"C:\changed\python.exe"))

    def test_demand_start_drift_is_rejected(self) -> None:
        with self.assertRaises(guard.GuardError):
            self.validate(self.xml(allow_demand="true"))


if __name__ == "__main__":
    unittest.main()
