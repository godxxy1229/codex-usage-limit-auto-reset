"""Deterministic run-loop tests using only injected clocks and transports."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from codex_reset_guard import (
    CONSUME_METHOD,
    GuardError,
    RpcError,
    TransportError,
    hash_account_email,
    run_guard,
    sha256_text,
)


UTC = timezone.utc
RAW_CREDIT_ID = "run-loop-fake-credit"
ACCOUNT_EMAIL = "guard.run.loop@example.com"
IDEMPOTENCY_KEY = "4a77335a-4da5-4d0e-a5f6-930d92bc276f"
EXPIRY = 2_000_000_100  # Exactly aligned to a 15-second UTC retry slot.
GRANTED_AT = 1_900_000_000


def iso_utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def account_response() -> dict[str, Any]:
    return {
        "requiresOpenaiAuth": True,
        "account": {
            "type": "chatgpt",
            "email": ACCOUNT_EMAIL,
            "planType": "plus",
        },
    }


def rate_response(*, target_present: bool = True) -> dict[str, Any]:
    credits: list[dict[str, Any]] = []
    if target_present:
        credits.append(
            {
                "id": RAW_CREDIT_ID,
                "expiresAt": EXPIRY,
                "grantedAt": GRANTED_AT,
                "resetType": "codexRateLimits",
                "status": "available",
                "title": None,
                "description": None,
            }
        )
    return {
        "rateLimits": {},
        "rateLimitsByLimitId": None,
        "rateLimitResetCredits": {
            "availableCount": len(credits),
            "credits": credits,
        },
    }


class FakeClock:
    def __init__(self, now: float) -> None:
        self.value = float(now)
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class FakeTransport:
    def __init__(
        self,
        *,
        consume_actions: list[object] | None = None,
        rate_responses: list[Mapping[str, Any]] | None = None,
    ) -> None:
        self.consume_actions = list(consume_actions or [])
        self.rate_responses = [copy.deepcopy(item) for item in (rate_responses or [rate_response()])]
        self.requests: list[tuple[str, object, float | None]] = []
        self.starts = 0
        self.restarts = 0
        self.closes = 0
        self.rate_reads = 0

    def start(self) -> Mapping[str, Any]:
        self.starts += 1
        return {
            "platformFamily": "windows",
            "platformOs": "windows",
            "codexHome": r"C:\fake-codex-home",
            "userAgent": "fake/run-loop",
        }

    def restart(self) -> Mapping[str, Any]:
        self.restarts += 1
        return self.start()

    def close(self) -> None:
        self.closes += 1

    def request(
        self,
        method: str,
        params: object = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        self.requests.append((method, copy.deepcopy(params), timeout))
        if method == "account/read":
            return account_response()
        if method == "account/rateLimits/read":
            index = min(self.rate_reads, len(self.rate_responses) - 1)
            self.rate_reads += 1
            return copy.deepcopy(self.rate_responses[index])
        if method == CONSUME_METHOD:
            if not self.consume_actions:
                raise AssertionError("unexpected consume request")
            action = self.consume_actions.pop(0)
            if isinstance(action, BaseException):
                raise action
            return copy.deepcopy(action)
        raise AssertionError(f"unexpected method: {method}")

    def consume_params(self) -> list[dict[str, str]]:
        return [
            params
            for method, params, _timeout in self.requests
            if method == CONSUME_METHOD
        ]


class RunLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest_path = self.root / "config" / "job.json"

    def write_manifest(
        self, *, armed: bool, state: str, schema_version: int = 1
    ) -> dict[str, Any]:
        manifest = {
            "schemaVersion": schema_version,
            "jobId": "5f03547e-c2af-46a1-8a50-b8d2f1151b8a",
            "createdAtUtc": iso_utc(EXPIRY - 600),
            "armed": armed,
            "state": state,
            "target": {
                "creditIdSha256": sha256_text(RAW_CREDIT_ID),
                "expiresAt": EXPIRY,
                "grantedAt": GRANTED_AT,
                "resetType": "codexRateLimits",
            },
            "account": {"emailSha256": hash_account_email(ACCOUNT_EMAIL)},
            "runtime": {
                "codexHome": r"C:\fake-codex-home",
                "codexExe": r"C:\fake-codex.exe",
                "codexVersion": "codex-cli 0.144.1",
                "codexSha256": "a" * 64,
                "signerSubject": "CN=OpenAI, L.L.C.",
            },
            "schedule": {
                "triggerAtUtc": iso_utc(EXPIRY - 345),
                "processAtUtc": iso_utc(EXPIRY - 300),
                "cutoffAtUtc": iso_utc(EXPIRY - 15),
                "expiresAtUtc": iso_utc(EXPIRY),
            },
            "task": {
                "name": (
                    r"\CodexResetCredit\Fake-Job" if armed else None
                )
            },
        }
        if schema_version == 1:
            manifest["idempotencyKey"] = IDEMPOTENCY_KEY
        if schema_version == 2:
            manifest["execution"] = {
                "phase": "preDispatch",
                "result": None,
                "failureCode": None,
                "terminalAt": None,
            }
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        return manifest

    def read_manifest(self) -> dict[str, Any]:
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    @staticmethod
    def binary_observer(manifest: Mapping[str, Any]) -> Mapping[str, str]:
        runtime = manifest["runtime"]
        return {
            "path": runtime["codexExe"],
            "version": runtime["codexVersion"],
            "sha256": runtime["codexSha256"],
        }

    def run_with(
        self,
        fake: FakeTransport,
        clock: FakeClock,
        *,
        live: bool,
        time_verifier=None,
        sleeper=None,
    ):
        created: list[tuple[Path, Path]] = []

        def factory(exe: Path, codex_home: Path) -> FakeTransport:
            created.append((exe, codex_home))
            return fake

        result = run_guard(
            self.manifest_path,
            live=live,
            transport_factory=factory,
            sleeper=sleeper if sleeper is not None else clock.sleep,
            now_func=clock.now,
            binary_observer=self.binary_observer,
            time_verifier=(
                time_verifier
                if time_verifier is not None
                else lambda: "synchronized to time.windows.com"
            ),
            task_verifier=lambda _name, _path, _manifest: None,
        )
        return result, created

    def assert_exact_consume_params(
        self, fake: FakeTransport, count: int, *, expected_key: str | None = IDEMPOTENCY_KEY
    ) -> None:
        params = fake.consume_params()
        self.assertEqual(len(params), count)
        self.assertTrue(params)
        for item in params:
            self.assertEqual(item["creditId"], RAW_CREDIT_ID)
            self.assertEqual(uuid.UUID(item["idempotencyKey"]).version, 4)
            if expected_key is not None:
                self.assertEqual(item["idempotencyKey"], expected_key)
        self.assertTrue(all(item == params[0] for item in params))

    def test_dry_run_validates_but_never_sends_consume(self) -> None:
        self.write_manifest(armed=False, state="ENROLLED")
        fake = FakeTransport()
        clock = FakeClock(EXPIRY - 1_000)

        result, created = self.run_with(fake, clock, live=False)

        self.assertEqual(result.state, "DRY_RUN_OK")
        self.assertEqual(fake.consume_params(), [])
        self.assertEqual(fake.starts, 1)
        self.assertEqual(fake.closes, 1)
        self.assertEqual(len(created), 1)
        self.assertEqual(self.read_manifest()["state"], "ENROLLED")

    def test_live_reset_sends_exact_payload_and_marks_succeeded(self) -> None:
        self.write_manifest(armed=True, state="ARMED")
        fake = FakeTransport(consume_actions=[{"outcome": "reset"}])
        clock = FakeClock(EXPIRY - 300)

        result, _created = self.run_with(fake, clock, live=True)

        self.assertEqual((result.state, result.outcome), ("SUCCEEDED", "reset"))
        self.assert_exact_consume_params(fake, 1)
        stored = self.read_manifest()
        self.assertEqual(stored["state"], "SUCCEEDED")
        self.assertFalse(stored["armed"])

    def test_nothing_to_reset_revalidates_then_retries_on_absolute_slot(self) -> None:
        self.write_manifest(armed=True, state="ARMED")
        fake = FakeTransport(
            consume_actions=[
                {"outcome": "nothingToReset"},
                {"outcome": "reset"},
            ]
        )
        # One second after a slot means the next absolute slot is 14 seconds
        # away, rather than 15 seconds after the response.
        clock = FakeClock(EXPIRY - 299)

        result, _created = self.run_with(fake, clock, live=True)

        self.assertEqual((result.state, result.outcome), ("SUCCEEDED", "reset"))
        self.assertEqual(clock.sleeps, [14.0])
        self.assertEqual(fake.rate_reads, 3)  # initial + before each POST
        self.assert_exact_consume_params(fake, 2)

    def test_ambiguous_timeout_then_already_redeemed_is_success(self) -> None:
        self.write_manifest(armed=True, state="ARMED")
        fake = FakeTransport(
            consume_actions=[
                TransportError("fake timeout", after_write=True),
                {"outcome": "alreadyRedeemed"},
            ],
            # The ambiguous replay is allowed to use the in-memory target even
            # after that target disappears from the refreshed detail list.
            rate_responses=[rate_response(), rate_response(), rate_response(target_present=False)],
        )
        clock = FakeClock(EXPIRY - 300)

        result, _created = self.run_with(fake, clock, live=True)

        self.assertEqual(
            (result.state, result.outcome), ("SUCCEEDED", "alreadyRedeemed")
        )
        self.assertEqual(clock.sleeps, [2.0])
        self.assertEqual(fake.restarts, 1)
        self.assert_exact_consume_params(fake, 2)

    def test_exact_internal_timeout_rpc_error_is_the_only_rpc_replay(self) -> None:
        self.write_manifest(armed=True, state="ARMED")
        exact_timeout = RpcError(
            CONSUME_METHOD,
            {
                "code": -32603,
                "message": "rate limit reset consume timed out",
            },
        )
        fake = FakeTransport(
            consume_actions=[exact_timeout, {"outcome": "alreadyRedeemed"}]
        )
        clock = FakeClock(EXPIRY - 300)

        result, _created = self.run_with(fake, clock, live=True)

        self.assertEqual((result.state, result.outcome), ("SUCCEEDED", "alreadyRedeemed"))
        self.assertEqual(fake.restarts, 1)
        self.assert_exact_consume_params(fake, 2)

    def test_non_allowlisted_rpc_error_is_not_replayed(self) -> None:
        self.write_manifest(armed=True, state="ARMED")
        fake = FakeTransport(
            consume_actions=[
                RpcError(
                    CONSUME_METHOD,
                    {
                        "code": -32603,
                        "message": "rate limit reset consume timed out ",
                    },
                )
            ]
        )
        clock = FakeClock(EXPIRY - 300)

        result, _created = self.run_with(fake, clock, live=True)

        self.assertEqual(result.state, "INDETERMINATE")
        self.assertEqual(fake.restarts, 0)
        self.assertEqual(clock.sleeps, [])
        self.assert_exact_consume_params(fake, 1)

    def test_unknown_first_response_is_not_replayed(self) -> None:
        self.write_manifest(armed=True, state="ARMED")
        fake = FakeTransport(consume_actions=[{"outcome": "futureOutcome"}])
        clock = FakeClock(EXPIRY - 300)

        result, _created = self.run_with(fake, clock, live=True)

        self.assertEqual(result.state, "INDETERMINATE")
        self.assertEqual(fake.restarts, 0)
        self.assertEqual(clock.sleeps, [])
        self.assert_exact_consume_params(fake, 1)

    def test_process_loss_after_dispatch_leaves_crash_safe_indeterminate(self) -> None:
        self.write_manifest(armed=True, state="ARMED")
        fake = FakeTransport(consume_actions=[SystemExit("fake process loss")])
        clock = FakeClock(EXPIRY - 300)

        with self.assertRaises(SystemExit):
            self.run_with(fake, clock, live=True)

        stored = self.read_manifest()
        self.assertEqual(stored["state"], "INDETERMINATE")
        self.assertFalse(stored["armed"])
        self.assert_exact_consume_params(fake, 1)

    def test_process_loss_during_nothing_retry_wait_stays_indeterminate(self) -> None:
        self.write_manifest(armed=True, state="ARMED", schema_version=2)
        fake = FakeTransport(consume_actions=[{"outcome": "nothingToReset"}])
        clock = FakeClock(EXPIRY - 299)

        def lose_process(_seconds: float) -> None:
            raise SystemExit("fake process loss during retry wait")

        with self.assertRaises(SystemExit):
            self.run_with(fake, clock, live=True, sleeper=lose_process)

        stored = self.read_manifest()
        self.assertEqual(stored["state"], "INDETERMINATE")
        self.assertFalse(stored["armed"])
        self.assertEqual(stored["execution"]["phase"], "postDispatch")
        self.assertEqual(
            stored["execution"]["failureCode"],
            "POST_DISPATCH_NOTHING_TO_RESET",
        )
        self.assert_exact_consume_params(fake, 1, expected_key=None)

    def test_pause_marker_during_nothing_retry_prevents_every_future_post(self) -> None:
        manifest = self.write_manifest(armed=True, state="ARMED", schema_version=2)
        fake = FakeTransport(
            consume_actions=[
                {"outcome": "nothingToReset"},
                {"outcome": "reset"},
            ]
        )
        clock = FakeClock(EXPIRY - 299)

        def request_pause(seconds: float) -> None:
            marker = self.manifest_path.with_suffix(
                self.manifest_path.suffix + ".cancel"
            )
            marker.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "jobId": manifest["jobId"],
                        "requestedAtUtc": iso_utc(int(clock.now())),
                    }
                ),
                encoding="utf-8",
            )
            clock.sleep(seconds)

        result, _created = self.run_with(
            fake, clock, live=True, sleeper=request_pause
        )

        self.assertEqual(result.state, "DISARMED")
        self.assert_exact_consume_params(fake, 1, expected_key=None)
        stored = self.read_manifest()
        self.assertEqual(stored["state"], "DISARMED")
        self.assertFalse(stored["armed"])
        self.assertEqual(stored["execution"]["failureCode"], "USER_CANCELLED")

    def test_unresolved_ambiguous_outcomes_are_indeterminate(self) -> None:
        scenarios = {
            "noCredit": [
                TransportError("fake timeout", after_write=True),
                {"outcome": "noCredit"},
            ],
            "unknown": [
                TransportError("fake timeout", after_write=True),
                {"outcome": "futureOutcome"},
            ],
            "repeated-timeout": [
                TransportError("initial timeout", after_write=True),
                TransportError("first replay timeout", after_write=True),
                TransportError("second replay timeout", after_write=True),
            ],
        }
        for label, actions in scenarios.items():
            with self.subTest(label=label):
                self.write_manifest(armed=True, state="ARMED")
                fake = FakeTransport(consume_actions=actions)
                clock = FakeClock(EXPIRY - 300)

                result, _created = self.run_with(fake, clock, live=True)

                self.assertEqual(result.state, "INDETERMINATE")
                stored = self.read_manifest()
                self.assertEqual(stored["state"], "INDETERMINATE")
                self.assertFalse(stored["armed"])
                expected_count = 3 if label == "repeated-timeout" else 2
                self.assert_exact_consume_params(fake, expected_count)
                self.assertEqual(
                    clock.sleeps,
                    [2.0, 4.0] if label == "repeated-timeout" else [2.0],
                )

    def test_cutoff_returns_no_action_without_starting_transport(self) -> None:
        self.write_manifest(armed=True, state="ARMED")
        fake = FakeTransport(consume_actions=[{"outcome": "reset"}])
        clock = FakeClock(EXPIRY - 15)

        result, created = self.run_with(fake, clock, live=True)

        self.assertEqual(result.state, "NO_ACTION")
        self.assertEqual(created, [])
        self.assertEqual(fake.consume_params(), [])
        stored = self.read_manifest()
        self.assertEqual(stored["state"], "NO_ACTION")
        self.assertFalse(stored["armed"])

    def test_cutoff_crossed_during_validation_prevents_first_post(self) -> None:
        self.write_manifest(armed=True, state="ARMED")
        fake = FakeTransport(consume_actions=[{"outcome": "reset"}])
        clock = FakeClock(EXPIRY - 16)
        calls = 0

        def delayed_time_verifier() -> str:
            nonlocal calls
            calls += 1
            if calls == 2:
                clock.value += 2
            return "synchronized to time.windows.com"

        result, _created = self.run_with(
            fake, clock, live=True, time_verifier=delayed_time_verifier
        )

        self.assertEqual(result.state, "NO_ACTION")
        self.assertEqual(fake.consume_params(), [])

    def test_cutoff_crossed_during_replay_validation_prevents_replay(self) -> None:
        self.write_manifest(armed=True, state="ARMED")
        fake = FakeTransport(
            consume_actions=[TransportError("fake timeout", after_write=True)]
        )
        clock = FakeClock(EXPIRY - 18)
        calls = 0

        def delayed_replay_time_verifier() -> str:
            nonlocal calls
            calls += 1
            if calls == 3:
                clock.value += 2
            return "synchronized to time.windows.com"

        result, _created = self.run_with(
            fake, clock, live=True, time_verifier=delayed_replay_time_verifier
        )

        self.assertEqual(result.state, "INDETERMINATE")
        self.assertEqual(clock.sleeps, [2.0])
        self.assert_exact_consume_params(fake, 1)

    def test_cancellation_marker_seen_before_post_disarms_without_consume(self) -> None:
        manifest = self.write_manifest(armed=True, state="ARMED")
        fake = FakeTransport(consume_actions=[{"outcome": "reset"}])
        clock = FakeClock(EXPIRY - 300)
        calls = 0

        def cancelling_time_verifier() -> str:
            nonlocal calls
            calls += 1
            if calls == 2:
                marker = self.manifest_path.with_suffix(
                    self.manifest_path.suffix + ".cancel"
                )
                marker.write_text(
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "jobId": manifest["jobId"],
                            "requestedAtUtc": iso_utc(int(clock.now())),
                        }
                    ),
                    encoding="utf-8",
                )
            return "synchronized to time.windows.com"

        result, _created = self.run_with(
            fake, clock, live=True, time_verifier=cancelling_time_verifier
        )

        self.assertEqual(result.state, "DISARMED")
        self.assertEqual(fake.consume_params(), [])
        self.assertFalse(self.read_manifest()["armed"])

    def test_live_pre_dispatch_guard_error_becomes_terminal_failed(self) -> None:
        self.write_manifest(armed=True, state="ARMED")
        fake = FakeTransport()
        clock = FakeClock(EXPIRY - 300)

        def failing_time_verifier() -> str:
            raise GuardError("fake unsynchronized clock")

        with self.assertRaises(GuardError):
            self.run_with(
                fake, clock, live=True, time_verifier=failing_time_verifier
            )

        stored = self.read_manifest()
        self.assertEqual(stored["state"], "FAILED")
        self.assertFalse(stored["armed"])

    def test_v2_success_records_post_dispatch_terminal_metadata(self) -> None:
        self.write_manifest(armed=True, state="ARMED", schema_version=2)
        fake = FakeTransport(consume_actions=[{"outcome": "reset"}])
        clock = FakeClock(EXPIRY - 300)

        result, _created = self.run_with(fake, clock, live=True)

        self.assertEqual(result.state, "SUCCEEDED")
        stored = self.read_manifest()
        self.assertNotIn("idempotencyKey", stored)
        execution = stored["execution"]
        self.assertEqual(execution["phase"], "postDispatch")
        self.assertEqual(execution["result"], "SUCCEEDED")
        self.assertIsNone(execution["failureCode"])
        self.assertRegex(execution["terminalAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_v2_pre_dispatch_failure_has_stable_failure_code(self) -> None:
        self.write_manifest(armed=True, state="ARMED", schema_version=2)
        fake = FakeTransport()
        clock = FakeClock(EXPIRY - 300)

        with self.assertRaises(GuardError):
            self.run_with(
                fake,
                clock,
                live=True,
                time_verifier=lambda: (_ for _ in ()).throw(
                    GuardError("message text is intentionally unstable")
                ),
            )

        execution = self.read_manifest()["execution"]
        self.assertEqual(execution["phase"], "preDispatch")
        self.assertEqual(execution["result"], "FAILED")
        self.assertEqual(execution["failureCode"], "PRE_DISPATCH_TIME_SYNC")
        self.assertIsNotNone(execution["terminalAt"])

    def test_v2_process_loss_keeps_crash_safe_post_dispatch_result(self) -> None:
        self.write_manifest(armed=True, state="ARMED", schema_version=2)
        fake = FakeTransport(consume_actions=[SystemExit("fake process loss")])
        clock = FakeClock(EXPIRY - 300)

        with self.assertRaises(SystemExit):
            self.run_with(fake, clock, live=True)

        stored = self.read_manifest()
        self.assertEqual(stored["state"], "INDETERMINATE")
        self.assertEqual(
            stored["execution"],
            {
                "phase": "postDispatch",
                "result": "INDETERMINATE",
                "failureCode": "POST_DISPATCH_UNCONFIRMED",
                "terminalAt": stored["execution"]["terminalAt"],
            },
        )
        self.assertIsNotNone(stored["execution"]["terminalAt"])


if __name__ == "__main__":
    unittest.main()
