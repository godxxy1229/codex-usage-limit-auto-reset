"""Dependency-injected tests for the automatic-use manager.

No test invokes a real Scheduled Task, app-server, installer, or redemption RPC.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from typing import Any, Mapping

from codex_reset_manager import (
    Controller,
    Job,
    ManagerError,
    ScheduledTask,
    _assert_npm_package_matches_binary,
    _build_parser,
    _read_job,
    _utc_text,
)


NOW = 2_000_000_000
ACCOUNT_HASH = hashlib.sha256(b"person@example.test").hexdigest()
CLI_HASH = "a" * 64


def credit(name: str, expires_at: int, *, granted_at: int | None = None) -> dict[str, Any]:
    return {
        "creditIdSha256": hashlib.sha256(name.encode()).hexdigest(),
        "expiresAt": expires_at,
        "grantedAt": granted_at if granted_at is not None else expires_at - 86_400,
        "resetType": "codexRateLimits",
        "status": "available",
    }


def snapshot(rows: list[dict[str, Any]], *, cli_hash: str = CLI_HASH) -> dict[str, Any]:
    return {
        "compatible": True,
        "binary": {
            "path": r"C:\fake\codex.exe",
            "version": "codex-cli 0.145.0",
            "sha256": cli_hash,
            "signerSubject": "CN=OpenAI, L.L.C.",
        },
        "accountEmailSha256": ACCOUNT_HASH,
        "availableCount": len(rows),
        "credits": rows,
    }


def write_manifest(
    root: Path,
    row: Mapping[str, Any],
    *,
    state: str = "ARMED",
    schema: int = 1,
    job_id: str | None = None,
    suffix: str = "job",
    failure_code: str | None = None,
) -> Path:
    terminal = state in {
        "SUCCEEDED",
        "NO_ACTION",
        "FAILED",
        "INDETERMINATE",
        "DISARMED",
        "CLEANED",
        "SUPERSEDED_CLI",
    }
    expires = int(row["expiresAt"])
    manifest: dict[str, Any] = {
        "schemaVersion": schema,
        "jobId": job_id or str(uuid.uuid4()),
        "createdAtUtc": _utc_text(NOW - 100),
        "armed": state == "ARMED",
        "state": state,
        "target": {
            "creditIdSha256": row["creditIdSha256"],
            "expiresAt": expires,
            "grantedAt": row["grantedAt"],
            "resetType": row["resetType"],
        },
        "account": {"emailSha256": ACCOUNT_HASH},
        "runtime": {
            "codexHome": r"C:\fake\.codex",
            "codexExe": r"C:\fake\codex.exe",
            "codexVersion": "codex-cli 0.145.0",
            "codexSha256": CLI_HASH,
            "signerSubject": "CN=OpenAI, L.L.C.",
        },
        "schedule": {
            "triggerAtUtc": _utc_text(expires - 345),
            "processAtUtc": _utc_text(expires - 300),
            "cutoffAtUtc": _utc_text(expires - 15),
            "expiresAtUtc": _utc_text(expires),
        },
        "task": {"name": rf"\CodexResetCredit\Consume-{suffix}" if state == "ARMED" else None},
    }
    if schema == 1:
        # Adopted legacy v1 manifests keep their original key. New v2
        # manifests intentionally never persist one.
        manifest["idempotencyKey"] = "cb0c64f9-5f27-44f4-9fc5-4e4ae12e522f"
    if schema == 2:
        phase = "postDispatch" if state in {"SUCCEEDED", "NO_ACTION", "INDETERMINATE"} else "preDispatch"
        manifest["execution"] = {
            "phase": phase,
            "result": state if terminal else None,
            "failureCode": failure_code if terminal else None,
            "terminalAt": _utc_text(NOW) if terminal else None,
        }
    path = root / "manifests" / f"{suffix}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def set_terminal(path: Path, state: str, *, failure_code: str | None = None) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["state"] = state
    manifest["armed"] = False
    manifest["task"]["name"] = manifest["task"].get("name")
    if manifest["schemaVersion"] == 2:
        manifest["execution"] = {
            "phase": "postDispatch" if state in {"SUCCEEDED", "NO_ACTION", "INDETERMINATE"} else "preDispatch",
            "result": state,
            "failureCode": failure_code,
            "terminalAt": _utc_text(NOW),
        }
    path.write_text(json.dumps(manifest), encoding="utf-8")


class FakeServices:
    def __init__(self, root: Path, rows: list[dict[str, Any]]) -> None:
        self.root = root
        self.rows = rows
        self.pin_available = True
        self.created = 0
        self.disarmed: list[str] = []
        self.validated_tasks: list[str] = []
        self.notifications: list[tuple[str, str, str]] = []
        self.time_failure: str | None = None
        self.cli_failure: str | None = None
        self.task_failure: str | None = None
        self.extra_tasks: list[ScheduledTask] = []
        self.task_inventory_override: list[ScheduledTask] | None = None
        self.disabled_tasks: list[str] = []
        self.cli_validations = 0

    def validate_cli(self, expected_account_sha256: str | None) -> Mapping[str, Any]:
        from codex_reset_manager import ManagerError

        self.cli_validations += 1
        if self.cli_failure:
            raise ManagerError(self.cli_failure)
        if expected_account_sha256 not in {None, ACCOUNT_HASH}:
            raise ManagerError("ACCOUNT_CHANGED")
        return snapshot(self.rows)

    def time_status(self) -> str:
        from codex_reset_manager import ManagerError

        if self.time_failure:
            raise ManagerError(self.time_failure)
        return "synchronized"

    def binary_pin_available(self, job: Job) -> bool:
        return self.pin_available

    def validate_task(self, job: Job) -> None:
        self.validated_tasks.append(job.job_id)
        if self.task_failure:
            raise ManagerError(self.task_failure)

    def consume_tasks(self) -> list[ScheduledTask]:
        if self.task_inventory_override is not None:
            return list(self.task_inventory_override)
        tasks: list[ScheduledTask] = []
        if (self.root / "manifests").is_dir():
            for path in (self.root / "manifests").glob("*.json"):
                job = _read_job(path)
                if job.task_name:
                    tasks.append(ScheduledTask(job.task_name, not job.terminal))
        return tasks + list(self.extra_tasks)

    def disable_task(self, task_name: str) -> None:
        self.disabled_tasks.append(task_name)
        self.extra_tasks = [
            ScheduledTask(task.name, False) if task.name.casefold() == task_name.casefold() else task
            for task in self.extra_tasks
        ]

    def disarm(self, job: Job) -> None:
        self.disarmed.append(job.job_id)
        if not job.terminal:
            set_terminal(job.path, "DISARMED")

    def create_child(self, installer: Path, codex_path: str, runtime_guard: str | None) -> Mapping[str, Any]:
        policy = json.loads((self.root / "state" / "policy.json").read_text(encoding="utf-8"))
        if policy.get("enabled") is not True:
            raise AssertionError("ManagerChildOnly was called before enabled consent was persisted")
        self.created += 1
        chosen = sorted(self.rows, key=lambda item: item["expiresAt"])[0]
        path = write_manifest(
            self.root,
            chosen,
            schema=2,
            suffix=f"child-{self.created}",
        )
        manifest = json.loads(path.read_text(encoding="utf-8"))
        return {"manifestPath": str(path), "taskName": manifest["task"]["name"], "jobId": manifest["jobId"]}

    def notify(self, title: str, message: str, level: str) -> bool:
        self.notifications.append((title, message, level))
        return True


class ManagerControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "installers").mkdir(parents=True)
        (self.root / "installers" / "install-one.ps1").write_text("# fake", encoding="utf-8")
        self.clock = [float(NOW)]
        self.first = credit("first-secret-id", NOW + 5_000)
        self.second = credit("second-secret-id", NOW + 15_000)
        self.services = FakeServices(self.root, [self.first, self.second])
        self.controller = Controller(self.root, services=self.services, now=lambda: self.clock[0])

    def policy(self) -> dict[str, Any]:
        return json.loads((self.root / "state" / "policy.json").read_text(encoding="utf-8"))

    def test_bootstrap_adopts_existing_armed_v1_without_replacing_it(self) -> None:
        existing = write_manifest(self.root, self.first, schema=1, suffix="existing-v1")

        status = self.controller.bootstrap_status()

        self.assertEqual(status["automation"], "paused")
        self.assertEqual(status["reservationStatus"], "scheduled")
        self.assertEqual(self.services.created, 0)
        stored = self.policy()
        self.assertEqual(stored["currentJob"]["manifestPath"], str(existing.resolve()))
        self.assertFalse(stored["enabled"])

    def test_enable_preserves_existing_job_and_validates_task(self) -> None:
        path = write_manifest(self.root, self.first, schema=1, suffix="existing-v1")
        job_id = _read_job(path).job_id

        status = self.controller.enable()

        self.assertEqual(status["automation"], "on")
        self.assertEqual(status["reservationStatus"], "scheduled")
        self.assertEqual(self.services.created, 0)
        self.assertEqual(self.services.validated_tasks, [job_id])

    def test_enable_without_job_creates_exactly_one_child(self) -> None:
        status = self.controller.enable()

        self.assertEqual(status["reservationStatus"], "scheduled")
        self.assertEqual(self.services.created, 1)
        active = [
            _read_job(path)
            for path in (self.root / "manifests").glob("*.json")
            if not _read_job(path).terminal
        ]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].credit_sha256, self.first["creditIdSha256"])

    def test_success_waits_for_target_absence_before_succession(self) -> None:
        self.controller.enable()
        first_path = next((self.root / "manifests").glob("*.json"))
        set_terminal(first_path, "SUCCEEDED")

        waiting = self.controller.sync()
        self.assertEqual(waiting["reservationStatus"], "waiting")
        self.assertEqual(self.services.created, 1)

        self.services.rows = [self.second]
        scheduled = self.controller.sync()
        self.assertEqual(scheduled["reservationStatus"], "scheduled")
        self.assertEqual(self.services.created, 2)

    def test_no_action_and_indeterminate_wait_for_expiry_and_absence(self) -> None:
        for state in ("NO_ACTION", "INDETERMINATE"):
            with self.subTest(state=state):
                # Reset each subcase without touching real tasks.
                for path in (self.root / "manifests").glob("*.json"):
                    path.unlink()
                for path in (self.root / "state").glob("*.json"):
                    path.unlink()
                self.services.created = 0
                self.services.rows = [self.first, self.second]
                self.clock[0] = float(NOW)
                self.controller.enable()
                first_path = next((self.root / "manifests").glob("*.json"))
                set_terminal(first_path, state, failure_code="POST_DISPATCH_UNCONFIRMED" if state == "INDETERMINATE" else "POST_DISPATCH_NOTHING_TO_RESET")

                self.services.rows = [self.second]
                before_expiry = self.controller.sync()
                self.assertEqual(before_expiry["reservationStatus"], "waiting")
                self.assertEqual(self.services.created, 1)

                self.clock[0] = float(self.first["expiresAt"] + 1)
                after_expiry = self.controller.sync()
                self.assertEqual(after_expiry["reservationStatus"], "scheduled")
                self.assertEqual(self.services.created, 2)

    def test_post_dispatch_no_credit_is_quarantined_until_expiry_and_absence(self) -> None:
        self.controller.enable()
        first_path = next((self.root / "manifests").glob("*.json"))
        set_terminal(first_path, "FAILED", failure_code="POST_DISPATCH_NO_CREDIT")

        self.services.rows = [self.second]
        waiting = self.controller.sync()
        self.assertEqual(waiting["reservationStatus"], "waiting")
        self.assertEqual(self.services.created, 1)

        self.clock[0] = float(self.first["expiresAt"] + 1)
        scheduled = self.controller.sync()
        self.assertEqual(scheduled["reservationStatus"], "scheduled")
        self.assertEqual(self.services.created, 2)

    def test_quarantined_hash_reappearance_blocks_and_disarms_later_job(self) -> None:
        self.controller.enable()
        first_path = next((self.root / "manifests").glob("*.json"))
        set_terminal(first_path, "INDETERMINATE", failure_code="POST_DISPATCH_UNCONFIRMED")
        self.services.rows = [self.second]
        self.controller.sync()  # Records absence, but expiry barrier remains.
        self.clock[0] = float(self.first["expiresAt"] + 1)
        self.controller.sync()  # Schedules the later credit.

        self.services.rows = [self.first, self.second]
        blocked = self.controller.sync()

        self.assertEqual(blocked["automation"], "attention")
        self.assertEqual(blocked["blockedCode"], "QUARANTINED_CREDIT_REAPPEARED")
        self.assertTrue(self.services.disarmed)

    def test_multiple_active_jobs_blocks_without_child_creation(self) -> None:
        write_manifest(self.root, self.first, suffix="one")
        write_manifest(self.root, self.second, suffix="two")

        status = self.controller.enable()

        self.assertEqual(status["automation"], "attention")
        self.assertEqual(status["blockedCode"], "MULTIPLE_ACTIVE_JOBS")
        self.assertEqual(self.services.created, 0)
        self.assertEqual(len(self.services.disarmed), 2)

    def test_unexpected_enabled_consume_task_is_disabled_and_blocks(self) -> None:
        orphan = ScheduledTask(r"\CodexResetCredit\Consume-orphan", True)
        self.services.extra_tasks = [orphan]

        status = self.controller.enable()

        self.assertEqual(status["automation"], "attention")
        self.assertEqual(status["blockedCode"], "TASK_INVENTORY_MISMATCH")
        self.assertEqual(self.services.disabled_tasks, [orphan.name])
        self.assertEqual(self.services.created, 0)

    def test_disabled_orphan_task_is_allowed_as_audit_record(self) -> None:
        self.services.extra_tasks = [
            ScheduledTask(r"\CodexResetCredit\Consume-old-audit", False)
        ]

        status = self.controller.enable()

        self.assertEqual(status["reservationStatus"], "scheduled")
        self.assertEqual(self.services.created, 1)
        self.assertEqual(self.services.disabled_tasks, [])

    def test_disabled_task_cannot_satisfy_an_active_manifest(self) -> None:
        path = write_manifest(self.root, self.first, suffix="unexpected-disabled")
        job = _read_job(path)
        self.services.task_inventory_override = [
            ScheduledTask(str(job.task_name), False)
        ]

        status = self.controller.enable()

        self.assertEqual(status["blockedCode"], "TASK_INVENTORY_MISMATCH")
        self.assertEqual(len(self.services.disarmed), 1)
        self.assertTrue(_read_job(path).terminal)

    def test_missing_active_target_waits_until_expiry_before_later_child(self) -> None:
        path = write_manifest(self.root, self.first, suffix="missing-target")
        self.services.rows = [self.second]

        waiting = self.controller.enable()

        self.assertEqual(waiting["reservationStatus"], "waiting")
        self.assertEqual(self.services.created, 0)
        self.assertEqual(len(self.services.disarmed), 1)
        self.assertTrue(_read_job(path).terminal)

        self.clock[0] = float(self.first["expiresAt"] + 1)
        scheduled = self.controller.sync()
        self.assertEqual(scheduled["reservationStatus"], "scheduled")
        self.assertEqual(self.services.created, 1)

    def test_orphaned_post_dispatch_waiting_job_is_never_reenrolled(self) -> None:
        path = write_manifest(self.root, self.first, schema=2, suffix="waiting")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["state"] = "WAITING"
        manifest["armed"] = False
        manifest["execution"] = {
            "phase": "postDispatch",
            "result": None,
            "failureCode": None,
            "terminalAt": None,
        }
        path.write_text(json.dumps(manifest), encoding="utf-8")

        status = self.controller.enable()

        self.assertEqual(status["reservationStatus"], "waiting")
        self.assertEqual(self.services.created, 0)
        self.assertEqual(len(self.services.disarmed), 1)
        self.assertEqual(self.policy()["lastResult"]["state"], "INDETERMINATE")

    def test_malformed_manifest_blocks_without_unbound_local_error(self) -> None:
        manifests = self.root / "manifests"
        manifests.mkdir(parents=True)
        (manifests / "bad.json").write_text("{}", encoding="utf-8")

        status = self.controller.enable()

        self.assertEqual(status["automation"], "attention")
        self.assertEqual(status["blockedCode"], "MANIFEST_INVALID")

    def test_pause_does_not_require_dispatch_lock(self) -> None:
        self.controller.enable()

        def forbidden_dispatch_lock():
            raise AssertionError("pause attempted to acquire dispatch.lock")

        self.controller._dispatch_lock = forbidden_dispatch_lock  # type: ignore[method-assign]
        status = self.controller.pause()

        self.assertEqual(status["automation"], "paused")

    def test_pause_cancels_terminal_retry_sentinel_too(self) -> None:
        self.controller.enable()
        path = next((self.root / "manifests").glob("*.json"))
        set_terminal(path, "INDETERMINATE", failure_code="POST_DISPATCH_NOTHING_TO_RESET")
        job_id = _read_job(path).job_id

        self.controller.pause()

        self.assertIn(job_id, self.services.disarmed)

    def test_npm_package_version_must_match_binary_version(self) -> None:
        package_root = self.root / "npm" / "node_modules" / "@openai" / "codex"
        exe = package_root / "node_modules" / "@openai" / "codex-win32-x64" / "vendor" / "x" / "bin" / "codex.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"fake")
        (package_root / "package.json").write_text('{"version":"0.145.0"}', encoding="utf-8")
        _assert_npm_package_matches_binary({"path": str(exe), "version": "codex-cli 0.145.0"})

        (package_root / "package.json").write_text('{"version":"0.146.0"}', encoding="utf-8")
        with self.assertRaises(ManagerError) as caught:
            _assert_npm_package_matches_binary({"path": str(exe), "version": "codex-cli 0.145.0"})
        self.assertEqual(caught.exception.code, "CLI_PACKAGE_MISMATCH")

    def test_new_earlier_credit_replaces_only_before_margin(self) -> None:
        existing_later = credit("later", NOW + 20_000)
        write_manifest(self.root, existing_later, suffix="later")
        self.services.rows = [self.first, existing_later]

        status = self.controller.enable()

        self.assertEqual(status["reservationStatus"], "scheduled")
        self.assertEqual(len(self.services.disarmed), 1)
        self.assertEqual(self.services.created, 1)

    def test_cli_update_keeps_existing_pinned_binary_when_it_still_exists(self) -> None:
        write_manifest(self.root, self.first, suffix="pinned")
        self.services.pin_available = True

        self.controller.enable()

        self.assertEqual(self.services.created, 0)
        self.assertEqual(self.services.disarmed, [])

    def test_global_cli_failure_preserves_verified_pinned_job(self) -> None:
        path = write_manifest(self.root, self.first, suffix="pinned-safe")
        job_id = _read_job(path).job_id
        self.services.cli_failure = "CLI_VALIDATION_FAILED"

        status = self.controller.enable()

        self.assertEqual(status["automation"], "attention")
        self.assertEqual(status["blockedCode"], "CLI_VALIDATION_FAILED")
        self.assertEqual(self.services.disarmed, [])
        self.assertFalse(_read_job(path).terminal)
        self.assertEqual(self.services.validated_tasks, [job_id])

    def test_invalid_pinned_task_is_not_preserved_on_cli_failure(self) -> None:
        path = write_manifest(self.root, self.first, suffix="pinned-invalid")
        self.services.cli_failure = "CLI_VALIDATION_FAILED"
        self.services.task_failure = "TASK_CONTRACT_INVALID"

        status = self.controller.enable()

        self.assertEqual(status["blockedCode"], "TASK_CONTRACT_INVALID")
        self.assertEqual(len(self.services.disarmed), 1)
        self.assertTrue(_read_job(path).terminal)
        self.assertEqual(self.services.cli_validations, 0)

    def test_account_change_still_disarms_a_verified_pinned_job(self) -> None:
        path = write_manifest(self.root, self.first, suffix="pinned-other-account")
        self.services.cli_failure = "ACCOUNT_CHANGED"

        status = self.controller.enable()

        self.assertEqual(status["blockedCode"], "ACCOUNT_CHANGED")
        self.assertEqual(len(self.services.disarmed), 1)
        self.assertTrue(_read_job(path).terminal)

    def test_scheduled_sync_revalidates_and_clears_prior_block(self) -> None:
        self.services.cli_failure = "CLI_VALIDATION_FAILED"
        first = self.controller.enable()
        self.assertEqual(first["blockedCode"], "CLI_VALIDATION_FAILED")
        self.assertEqual(self.services.created, 0)

        self.services.cli_failure = None
        recovered = self.controller.sync(scheduled=True)

        self.assertEqual(recovered["automation"], "on")
        self.assertIsNone(recovered["blockedCode"])
        self.assertEqual(recovered["reservationStatus"], "scheduled")
        self.assertEqual(self.services.created, 1)

    def test_scheduled_sync_recovers_cli_block_without_replacing_pinned_job(self) -> None:
        path = write_manifest(self.root, self.first, suffix="pinned-recovery")
        self.services.cli_failure = "CLI_VALIDATION_FAILED"
        blocked = self.controller.enable()
        self.assertEqual(blocked["blockedCode"], "CLI_VALIDATION_FAILED")
        self.assertFalse(_read_job(path).terminal)

        self.services.cli_failure = None
        recovered = self.controller.sync(scheduled=True)

        self.assertIsNone(recovered["blockedCode"])
        self.assertEqual(recovered["reservationStatus"], "scheduled")
        self.assertEqual(self.services.disarmed, [])
        self.assertEqual(self.services.created, 0)

    def test_missing_pinned_binary_is_replaced_when_margin_is_safe(self) -> None:
        write_manifest(self.root, self.first, suffix="pinned")
        self.services.pin_available = False

        status = self.controller.enable()

        self.assertEqual(status["reservationStatus"], "scheduled")
        self.assertEqual(len(self.services.disarmed), 1)
        self.assertEqual(self.services.created, 1)

    def test_pause_disarms_active_job_and_stops_succession(self) -> None:
        self.controller.enable()

        status = self.controller.pause()

        self.assertEqual(status["automation"], "paused")
        self.assertFalse(self.policy()["enabled"])
        self.assertEqual(len(self.services.disarmed), 1)

    def test_notification_is_deduplicated_across_syncs(self) -> None:
        self.controller.enable()
        count = len(self.services.notifications)

        self.controller.sync()

        self.assertEqual(len(self.services.notifications), count)

    def test_policy_and_manager_log_never_copy_manifest_secrets(self) -> None:
        write_manifest(self.root, self.first, suffix="secret-source")
        self.controller.enable()

        rendered = (self.root / "state" / "policy.json").read_text(encoding="utf-8")
        if (self.root / "logs" / "manager.jsonl").is_file():
            rendered += (self.root / "logs" / "manager.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("cb0c64f9-5f27-44f4-9fc5-4e4ae12e522f", rendered)
        self.assertNotIn("first-secret-id", rendered)
        self.assertNotIn("person@example.test", rendered)

    def test_controller_can_run_from_worker_thread(self) -> None:
        results: list[dict[str, Any]] = []
        worker = threading.Thread(target=lambda: results.append(self.controller.bootstrap_status()))
        worker.start()
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["automation"], "paused")

    def test_public_parser_exposes_simplified_commands(self) -> None:
        parser = _build_parser()
        for argv in (["ui"], ["enable"], ["pause"], ["sync", "--scheduled"], ["status", "--json"], ["doctor"]):
            with self.subTest(argv=argv):
                self.assertEqual(parser.parse_args(argv).command, argv[0])


if __name__ == "__main__":
    unittest.main()
