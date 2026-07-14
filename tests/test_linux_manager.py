"""Linux manager adapter tests that do not require a running systemd session."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import codex_reset_manager as manager


class FakeGuard:
    class SystemdTriggerInProgressError(Exception):
        pass

    class SystemdTriggerElapsedError(Exception):
        pass

    def __init__(self, binary: dict[str, str] | None = None) -> None:
        self.binary = binary
        self.compatibility_calls: list[dict[str, object]] = []
        self.observe_calls: list[object] = []
        self.validated_task: tuple[str, Path, object] | None = None
        self.disabled: list[str] = []
        self.task_error: Exception | None = None

    def validate_cli_compatibility(self, **kwargs: object) -> dict[str, object]:
        self.compatibility_calls.append(kwargs)
        assert self.binary is not None
        return {
            "compatible": True,
            "binary": self.binary,
            "accountEmailSha256": "a" * 64,
            "availableCount": 0,
            "credits": [],
        }

    def observe_cli_pin(self, *, codex_path: object) -> dict[str, str]:
        self.observe_calls.append(codex_path)
        assert self.binary is not None
        return {
            "path": self.binary["path"],
            "version": self.binary["version"],
            "sha256": self.binary["sha256"],
            "packageVersion": self.binary["version"].removeprefix("codex-cli "),
        }

    def observe_pinned_cli_pin(self, exact_path: object) -> dict[str, str]:
        return self.observe_cli_pin(codex_path=exact_path)

    @staticmethod
    def _load_json(path: Path) -> dict[str, object]:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _validate_manifest(manifest: object) -> None:
        if not isinstance(manifest, dict):
            raise ValueError

    def _validate_scheduled_task_contract(
        self, task_name: str, path: Path, manifest: object
    ) -> None:
        if self.task_error is not None:
            raise self.task_error
        self.validated_task = (task_name, path, manifest)

    def _disable_task_best_effort(self, task_name: str) -> bool:
        self.disabled.append(task_name)
        return True


class FailingChildLinuxServices(manager.LinuxServices):
    def __init__(
        self,
        root: Path,
        *,
        inventories: list[list[manager.ScheduledTask]],
        on_create: object,
        child_error: BaseException,
    ) -> None:
        super().__init__(root)
        self.inventories = inventories
        self.on_create = on_create
        self.child_error = child_error
        self.disabled: list[str] = []
        self.disarmed: list[manager.Job] = []

    def consume_tasks(self) -> list[manager.ScheduledTask]:
        if len(self.inventories) > 1:
            return self.inventories.pop(0)
        return list(self.inventories[0])

    def create_child(
        self, installer: Path, codex_path: str, runtime_guard: str | None
    ) -> object:
        del installer, codex_path, runtime_guard
        callback = self.on_create
        if callable(callback):
            callback()
        if isinstance(self.child_error, subprocess.TimeoutExpired):
            raise manager.ManagerError("CHILD_INSTALL_FAILED") from self.child_error
        raise self.child_error

    def disable_task(self, task_name: str) -> None:
        self.disabled.append(task_name)

    def disarm(self, job: manager.Job) -> None:
        self.disarmed.append(job)


class LinuxPathAndCliTests(unittest.TestCase):
    def test_default_root_uses_xdg_data_home(self) -> None:
        with (
            mock.patch.object(manager.sys, "platform", "linux"),
            mock.patch.dict(
                manager.os.environ,
                {"XDG_DATA_HOME": "/tmp/xdg-data"},
                clear=True,
            ),
        ):
            root = manager._default_root()

        self.assertTrue(
            root.as_posix().endswith("/tmp/xdg-data/codex-usage-limit-auto-reset")
        )

    def test_default_root_falls_back_to_local_share(self) -> None:
        with (
            mock.patch.object(manager.sys, "platform", "linux"),
            mock.patch.dict(manager.os.environ, {}, clear=True),
            mock.patch.object(manager.Path, "home", return_value=Path("/home/tester")),
        ):
            root = manager._default_root()

        self.assertTrue(
            root.as_posix().endswith(
                "/home/tester/.local/share/codex-usage-limit-auto-reset"
            )
        )

    def test_controller_selects_linux_services(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            manager.sys, "platform", "linux"
        ):
            controller = manager.Controller(Path(directory))

        self.assertIsInstance(controller.services, manager.LinuxServices)

    def test_linux_console_python_accepts_final_base_cpython(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "python3.13"
            executable.write_bytes(b"python")
            version = SimpleNamespace(
                major=3,
                minor=13,
                releaselevel="final",
            )
            implementation = SimpleNamespace(name="cpython")
            with (
                mock.patch.object(manager.sys, "platform", "linux"),
                mock.patch.object(manager.sys, "executable", str(executable)),
                mock.patch.object(manager.sys, "version_info", version),
                mock.patch.object(manager.sys, "implementation", implementation),
                mock.patch.object(manager.sys, "prefix", "/usr"),
                mock.patch.object(manager.sys, "base_prefix", "/usr"),
                mock.patch.object(manager.sysconfig, "get_config_var", return_value=0),
            ):
                observed = manager._manager_console_python()

        self.assertEqual(observed, executable.resolve())

    def test_linux_console_python_accepts_canonical_python_basename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "python"
            executable.write_bytes(b"python")
            version = SimpleNamespace(major=3, minor=13, releaselevel="final")
            with (
                mock.patch.object(manager.sys, "platform", "linux"),
                mock.patch.object(manager.sys, "executable", str(executable)),
                mock.patch.object(manager.sys, "version_info", version),
                mock.patch.object(
                    manager.sys, "implementation", SimpleNamespace(name="cpython")
                ),
                mock.patch.object(manager.sys, "prefix", "/usr"),
                mock.patch.object(manager.sys, "base_prefix", "/usr"),
                mock.patch.object(manager.sysconfig, "get_config_var", return_value=0),
            ):
                observed = manager._manager_console_python()

        self.assertEqual(observed, executable.resolve())

    def test_linux_console_python_rejects_venv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "python3.13"
            executable.write_bytes(b"python")
            version = SimpleNamespace(major=3, minor=13, releaselevel="final")
            with (
                mock.patch.object(manager.sys, "platform", "linux"),
                mock.patch.object(manager.sys, "executable", str(executable)),
                mock.patch.object(manager.sys, "version_info", version),
                mock.patch.object(
                    manager.sys, "implementation", SimpleNamespace(name="cpython")
                ),
                mock.patch.object(manager.sys, "prefix", "/tmp/venv"),
                mock.patch.object(manager.sys, "base_prefix", "/usr"),
                self.assertRaisesRegex(manager.ManagerError, "CHILD_INSTALL_FAILED"),
            ):
                manager._manager_console_python()

    def test_ui_is_explicitly_unavailable_on_linux(self) -> None:
        with (
            mock.patch.object(manager.sys, "platform", "linux"),
            self.assertRaisesRegex(manager.ManagerError, "UI_UNAVAILABLE_ON_LINUX"),
        ):
            manager.run_ui(mock.Mock())

    def test_linux_cli_validation_passes_approved_pin_to_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "node_modules" / "@openai" / "codex"
            binary = (
                package
                / "node_modules"
                / "@openai"
                / "codex-linux-x64"
                / "vendor"
                / "x86_64-unknown-linux-musl"
                / "codex"
                / "codex"
            )
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"codex")
            (package / "package.json").write_text(
                json.dumps({"version": "0.145.0"}), encoding="utf-8"
            )
            result_binary = {
                "path": str(binary),
                "version": "codex-cli 0.145.0",
                "sha256": hashlib.sha256(b"codex").hexdigest(),
                "signerSubject": "npm:openai/codex@rust-v0.145.0:abc123",
            }
            guard = FakeGuard(result_binary)
            services = manager.LinuxServices(Path(directory))
            services._guard = guard  # type: ignore[assignment]
            approved = {
                "codexExe": str(binary),
                "codexVersion": "codex-cli 0.145.0",
                "codexSha256": result_binary["sha256"],
                "signerSubject": result_binary["signerSubject"],
                "approvedAtUtc": "2030-01-01T00:00:00Z",
            }

            snapshot = services.validate_cli("a" * 64, approved)

        self.assertTrue(snapshot["compatible"])
        self.assertIs(guard.compatibility_calls[0]["trusted_binary"], approved)

    def test_active_job_uses_exact_path_old_pin_observer(self) -> None:
        binary = {
            "path": "/opt/old-npm/node_modules/@openai/codex/native/codex",
            "version": "codex-cli 0.145.0",
            "sha256": "2" * 64,
            "signerSubject": "npm-provenance:test",
        }
        guard = FakeGuard(binary)
        services = manager.LinuxServices(Path("/tmp/test-root"))
        services._guard = guard  # type: ignore[assignment]
        job = SimpleNamespace(
            codex_exe=binary["path"],
            codex_version=binary["version"],
            codex_sha256=binary["sha256"],
        )

        self.assertTrue(services.binary_pin_available(job))
        self.assertEqual(guard.observe_calls, [binary["path"]])

    def test_linux_cli_change_runs_full_provenance_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "node_modules" / "@openai" / "codex"
            binary = (
                package
                / "node_modules"
                / "@openai"
                / "codex-linux-x64"
                / "vendor"
                / "x86_64-unknown-linux-musl"
                / "codex"
                / "codex"
            )
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"new-codex")
            (package / "package.json").write_text(
                json.dumps({"version": "0.146.0"}), encoding="utf-8"
            )
            result_binary = {
                "path": str(binary),
                "version": "codex-cli 0.146.0",
                "sha256": hashlib.sha256(b"new-codex").hexdigest(),
                "signerSubject": "npm:openai/codex@rust-v0.146.0:def456",
            }
            guard = FakeGuard(result_binary)
            services = manager.LinuxServices(Path(directory))
            services._guard = guard  # type: ignore[assignment]
            previous = {
                "codexExe": str(binary),
                "codexVersion": "codex-cli 0.145.0",
                "codexSha256": "0" * 64,
                "signerSubject": "npm:openai/codex@rust-v0.145.0:abc123",
                "approvedAtUtc": "2030-01-01T00:00:00Z",
            }

            snapshot = services.validate_cli("a" * 64, previous)

        self.assertTrue(snapshot["compatible"])
        self.assertIsNone(guard.compatibility_calls[0]["trusted_binary"])

    def test_linux_first_validation_runs_full_provenance_validation(self) -> None:
        guard = FakeGuard(
            {
                "path": "/usr/lib/node_modules/@openai/codex/native/codex",
                "version": "codex-cli 0.146.0",
                "sha256": "1" * 64,
                "signerSubject": "npm:openai/codex@rust-v0.146.0:def456",
            }
        )
        services = manager.LinuxServices(Path("/tmp/test-root"))
        services._guard = guard  # type: ignore[assignment]
        with mock.patch.object(manager, "_assert_npm_package_matches_binary"):
            services.validate_cli(None, None)

        self.assertEqual(guard.observe_calls, [])
        self.assertIsNone(guard.compatibility_calls[0]["trusted_binary"])

    def test_cached_validation_preserves_original_approval_time(self) -> None:
        binary = {
            "path": "/usr/lib/node_modules/@openai/codex/native/codex",
            "version": "codex-cli 0.146.0",
            "sha256": "1" * 64,
            "signerSubject": "npm-provenance:test",
        }
        services = mock.Mock()
        services.supports_approved_cli_cache = True
        services.validate_cli.return_value = {
            "compatible": True,
            "binary": binary,
            "accountEmailSha256": "a" * 64,
            "availableCount": 0,
            "credits": [],
        }
        controller = manager.Controller(
            Path("/tmp/test-root"), services=services, now=lambda: 2_000_000_000
        )
        policy = manager._default_policy(1_900_000_000)
        policy["approvedCli"] = {
            "codexExe": binary["path"],
            "codexVersion": binary["version"],
            "codexSha256": binary["sha256"],
            "signerSubject": binary["signerSubject"],
            "approvedAtUtc": "2030-01-01T00:00:00Z",
        }

        controller._validate_runtime(policy)

        self.assertEqual(
            policy["approvedCli"]["approvedAtUtc"], "2030-01-01T00:00:00Z"
        )


class LinuxSystemdServicesTests(unittest.TestCase):
    TASK = "codex-reset-consume-0123456789ab-89abcdef.timer"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.services = manager.LinuxServices(self.root)

    def test_consume_inventory_treats_active_timer_as_enabled(self) -> None:
        listed = SimpleNamespace(
            returncode=0,
            stdout=f"{self.TASK} disabled enabled\n",
            stderr="",
        )
        loaded = SimpleNamespace(
            returncode=0,
            stdout=f"{self.TASK} loaded active waiting test\n",
            stderr="",
        )
        shown = SimpleNamespace(
            returncode=0,
            stdout="UnitFileState=disabled\nActiveState=active\n",
            stderr="",
        )
        with (
            mock.patch.object(manager.shutil, "which", return_value="/usr/bin/systemctl"),
            mock.patch.object(
                manager.subprocess, "run", side_effect=[listed, loaded, shown]
            ) as run,
        ):
            tasks = self.services.consume_tasks()

        self.assertEqual(tasks, [manager.ScheduledTask(self.TASK, True)])
        self.assertEqual(run.call_count, 3)
        for call in run.call_args_list:
            self.assertEqual(
                call.kwargs["creationflags"], manager.WINDOWLESS_SUBPROCESS_FLAGS
            )
            self.assertEqual(call.kwargs["env"]["LC_ALL"], "C")

    def test_consume_inventory_rejects_unexpected_unit_name(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout="codex-reset-consume-not-safe.timer enabled enabled\n",
            stderr="",
        )
        with (
            mock.patch.object(manager.shutil, "which", return_value="systemctl"),
            mock.patch.object(
                manager.subprocess,
                "run",
                side_effect=[
                    completed,
                    SimpleNamespace(returncode=0, stdout="", stderr=""),
                ],
            ),
            self.assertRaisesRegex(manager.ManagerError, "TASK_ENUMERATION_FAILED"),
        ):
            self.services.consume_tasks()

    def test_consume_inventory_ignores_unrelated_timers(self) -> None:
        listed = SimpleNamespace(
            returncode=0,
            stdout="systemd-tmpfiles-clean.timer static -\n",
            stderr="",
        )
        loaded = SimpleNamespace(
            returncode=0,
            stdout="dnf-makecache.timer loaded inactive dead test\n",
            stderr="",
        )
        with (
            mock.patch.object(manager.shutil, "which", return_value="/usr/bin/systemctl"),
            mock.patch.object(
                manager.subprocess, "run", side_effect=[listed, loaded]
            ) as run,
        ):
            tasks = self.services.consume_tasks()

        self.assertEqual(tasks, [])
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertNotIn("codex-reset-consume-*.timer", call.args[0])

    def test_consume_inventory_keeps_nonzero_enumeration_fail_closed(self) -> None:
        failed = SimpleNamespace(returncode=1, stdout="", stderr="")
        with (
            mock.patch.object(manager.shutil, "which", return_value="systemctl"),
            mock.patch.object(manager.subprocess, "run", return_value=failed),
            self.assertRaisesRegex(manager.ManagerError, "TASK_ENUMERATION_FAILED"),
        ):
            self.services.consume_tasks()

    def test_task_validation_and_disable_delegate_to_guard(self) -> None:
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text("{}", encoding="utf-8")
        guard = FakeGuard()
        self.services._guard = guard  # type: ignore[assignment]
        job = SimpleNamespace(task_name=self.TASK, path=manifest_path)

        self.services.validate_task(job)
        self.services.disable_task(self.TASK)

        self.assertEqual(guard.validated_task, (self.TASK, manifest_path, {}))
        self.assertEqual(guard.disabled, [self.TASK])

    def test_task_validation_preserves_trigger_state_classification(self) -> None:
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text("{}", encoding="utf-8")
        guard = FakeGuard()
        self.services._guard = guard  # type: ignore[assignment]
        job = SimpleNamespace(task_name=self.TASK, path=manifest_path)

        guard.task_error = guard.SystemdTriggerInProgressError()
        with self.assertRaises(manager.ManagerError) as raised:
            self.services.validate_task(job)
        self.assertEqual(raised.exception.code, "TASK_TRIGGER_IN_PROGRESS")

        guard.task_error = guard.SystemdTriggerElapsedError()
        with self.assertRaises(manager.ManagerError) as raised:
            self.services.validate_task(job)
        self.assertEqual(raised.exception.code, "PRE_DISPATCH_TRIGGER_ELAPSED")

    def test_child_installer_receives_exact_linux_runtime_paths(self) -> None:
        python = self.root / "python3.13"
        installer = self.root / "install_linux.py"
        guard = self.root / "codex_reset_guard.py"
        for path in (python, installer, guard):
            path.write_text("# test\n", encoding="utf-8")
        result = {
            "manifestPath": str(self.root / "manifests" / "job.json"),
            "taskName": self.TASK,
            "jobId": "12345678-1234-4234-9234-123456789abc",
        }
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(result) + "\n",
            stderr="",
        )
        with (
            mock.patch.object(manager, "_manager_console_python", return_value=python),
            mock.patch.object(manager.subprocess, "run", return_value=completed) as run,
        ):
            observed = self.services.create_child(
                installer, "/opt/npm/codex", str(guard)
            )

        self.assertEqual(observed, result)
        command = run.call_args.args[0]
        self.assertEqual(
            command,
            [
                str(python),
                "-I",
                str(installer),
                "--manager-child-only",
                "--install-root",
                str(self.root.resolve()),
                "--python-path",
                str(python),
                "--codex-path",
                "/opt/npm/codex",
                "--runtime-guard",
                str(guard.resolve()),
            ],
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 600)


class LinuxElapsedTimerControllerTests(unittest.TestCase):
    TASK = "codex-reset-consume-aaaaaaaaaaaa-bbbbbbbb.timer"
    NOW = 2_000_000_000

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.job = manager.Job(
            path=self.root / "manifests" / "job.json",
            schema_version=2,
            job_id="12345678-1234-4234-9234-123456789abc",
            state="ARMED",
            armed=True,
            credit_sha256="a" * 64,
            expires_at=self.NOW + 3600,
            granted_at=self.NOW - 3600,
            reset_type="codexRateLimits",
            account_sha256="b" * 64,
            codex_exe="/usr/lib/node_modules/@openai/codex/native/codex",
            codex_version="codex-cli 0.145.0",
            codex_sha256="c" * 64,
            signer_subject="npm-provenance:test",
            task_name=self.TASK,
            trigger_at=self.NOW + 3255,
            process_at=self.NOW + 3300,
            phase="preDispatch",
            result=None,
            failure_code=None,
            terminal_at=None,
        )
        self.policy = manager._default_policy(self.NOW)
        self.policy["enabled"] = True
        self.services = mock.Mock()
        self.services.supports_approved_cli_cache = True
        self.services.consume_tasks.return_value = [
            manager.ScheduledTask(self.TASK, True)
        ]
        self.services.time_status.return_value = "NTPSynchronized=yes"
        self.services.binary_pin_available.return_value = True
        self.services.notify.return_value = False
        self.services.validate_cli.return_value = {
            "compatible": True,
            "binary": {
                "path": self.job.codex_exe,
                "version": self.job.codex_version,
                "sha256": self.job.codex_sha256,
                "signerSubject": self.job.signer_subject,
            },
            "accountEmailSha256": self.job.account_sha256,
            "availableCount": 1,
            "credits": [
                {
                    "creditIdSha256": self.job.credit_sha256,
                    "expiresAt": self.job.expires_at,
                    "grantedAt": self.job.granted_at,
                    "resetType": self.job.reset_type,
                    "status": "available",
                }
            ],
        }
        self.controller = manager.Controller(
            self.root, services=self.services, now=lambda: self.NOW
        )

    def test_trigger_in_progress_keeps_the_existing_job_without_blocking(self) -> None:
        self.services.validate_task.side_effect = manager.ManagerError(
            "TASK_TRIGGER_IN_PROGRESS"
        )

        status = self.controller._sync_locked(self.policy, [self.job])

        self.assertEqual(status["automation"], "on")
        self.assertEqual(status["reservationStatus"], "scheduled")
        self.assertIsNone(status["blockedCode"])
        self.services.disarm.assert_not_called()
        self.services.validate_cli.assert_not_called()

    def test_elapsed_trigger_is_disarmed_and_quarantined_until_expiry(self) -> None:
        self.services.validate_task.side_effect = manager.ManagerError(
            "PRE_DISPATCH_TRIGGER_ELAPSED"
        )

        status = self.controller._sync_locked(self.policy, [self.job])

        self.services.disarm.assert_called_once_with(self.job)
        self.assertIsNone(self.policy["currentJob"])
        self.assertEqual(
            self.policy["lastResult"],
            {
                "state": "NO_ACTION",
                "atUtc": manager._utc_text(self.NOW),
                "expiresAtUtc": manager._utc_text(self.job.expires_at),
                "failureCode": "PRE_DISPATCH_TRIGGER_ELAPSED",
            },
        )
        self.assertEqual(
            self.policy["quarantine"][0]["reason"],
            "PRE_DISPATCH_TRIGGER_ELAPSED",
        )
        self.assertEqual(status["reservationStatus"], "waiting")
        self.assertIsNone(status["blockedCode"])


class LinuxChildCleanupTests(unittest.TestCase):
    TASK = "codex-reset-consume-111111111111-22222222.timer"
    NOW = 2_000_000_000

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.installer = self.root / "install_linux.py"
        self.installer.write_text("# test\n", encoding="utf-8")
        self.credit = manager.Credit(
            credit_sha256="c" * 64,
            expires_at=self.NOW + 7200,
            granted_at=self.NOW - 3600,
            reset_type="codexRateLimits",
            status="available",
        )

    def _policy(self) -> dict[str, object]:
        policy = manager._default_policy(self.NOW)
        policy["runtimeInstaller"] = str(self.installer)
        policy["approvedCli"] = {
            "codexExe": "/opt/npm/codex",
            "codexVersion": "codex-cli 0.145.0",
            "codexSha256": "a" * 64,
            "signerSubject": "npm-provenance:test",
            "approvedAtUtc": manager._utc_text(self.NOW - 60),
        }
        return policy

    def _write_valid_manifest(self) -> Path:
        manifest = {
            "schemaVersion": 2,
            "jobId": str(uuid.uuid4()),
            "createdAtUtc": manager._utc_text(self.NOW),
            "armed": True,
            "state": "ARMED",
            "target": {
                "creditIdSha256": self.credit.credit_sha256,
                "expiresAt": self.credit.expires_at,
                "grantedAt": self.credit.granted_at,
                "resetType": self.credit.reset_type,
            },
            "account": {"emailSha256": "b" * 64},
            "runtime": {
                "codexHome": "/home/tester/.codex",
                "codexExe": "/opt/npm/codex",
                "codexVersion": "codex-cli 0.145.0",
                "codexSha256": "a" * 64,
                "signerSubject": "npm-provenance:test",
            },
            "schedule": {
                "triggerAtUtc": manager._utc_text(self.credit.expires_at - 345),
                "processAtUtc": manager._utc_text(self.credit.expires_at - 300),
                "cutoffAtUtc": manager._utc_text(self.credit.expires_at - 15),
                "expiresAtUtc": manager._utc_text(self.credit.expires_at),
            },
            "task": {"name": self.TASK},
            "execution": {
                "phase": "preDispatch",
                "result": None,
                "failureCode": None,
                "terminalAt": None,
            },
        }
        path = self.root / "manifests" / "child.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def _run_failed_child(
        self, *, on_create: object, child_error: BaseException
    ) -> tuple[FailingChildLinuxServices, manager.ManagerError]:
        services = FailingChildLinuxServices(
            self.root,
            inventories=[[], [manager.ScheduledTask(self.TASK, True)]],
            on_create=on_create,
            child_error=child_error,
        )
        controller = manager.Controller(
            self.root, services=services, now=lambda: self.NOW
        )
        with (
            mock.patch.object(manager.sys, "platform", "linux"),
            self.assertRaises(manager.ManagerError) as raised,
        ):
            controller._create_child(self._policy(), [self.credit])
        return services, raised.exception

    def test_timeout_disables_new_timer_and_disarms_valid_manifest(self) -> None:
        services, error = self._run_failed_child(
            on_create=self._write_valid_manifest,
            child_error=subprocess.TimeoutExpired(["install_linux.py"], 600),
        )

        self.assertEqual(error.code, "CHILD_INSTALL_FAILED")
        self.assertIsInstance(error.__cause__, subprocess.TimeoutExpired)
        self.assertEqual(services.disabled, [self.TASK])
        self.assertEqual(len(services.disarmed), 1)
        self.assertEqual(services.disarmed[0].task_name, self.TASK)

    def test_failure_disables_timer_despite_malformed_manifest(self) -> None:
        def write_partial_manifest() -> None:
            path = self.root / "manifests" / "partial.json"
            path.parent.mkdir(parents=True)
            path.write_text('{"schemaVersion": 2,', encoding="utf-8")

        services, error = self._run_failed_child(
            on_create=write_partial_manifest,
            child_error=manager.ManagerError("CHILD_INSTALL_FAILED"),
        )

        self.assertEqual(error.code, "CHILD_INSTALL_FAILED")
        self.assertEqual(services.disabled, [self.TASK])
        self.assertEqual(services.disarmed, [])

    def test_failure_disables_task_only_enrollment(self) -> None:
        services, error = self._run_failed_child(
            on_create=lambda: None,
            child_error=manager.ManagerError("CHILD_INSTALL_OUTPUT_INVALID"),
        )

        self.assertEqual(error.code, "CHILD_INSTALL_OUTPUT_INVALID")
        self.assertEqual(services.disabled, [self.TASK])
        self.assertEqual(services.disarmed, [])


if __name__ == "__main__":
    unittest.main()
