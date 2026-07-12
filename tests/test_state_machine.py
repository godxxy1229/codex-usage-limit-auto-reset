"""Consume request, outcome, retry-slot, and ambiguity state contracts."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from codex_reset_guard import (
    GuardError,
    build_consume_params,
    classify_consume_result,
    next_retry_at,
    transport_failure_action,
)


UTC = timezone.utc
IDEMPOTENCY_KEY = "4a77335a-4da5-4d0e-a5f6-930d92bc276f"


class ConsumePayloadTests(unittest.TestCase):
    def test_payload_always_contains_exact_credit_id_and_idempotency_key(self) -> None:
        params = build_consume_params("opaque-credit-id", IDEMPOTENCY_KEY)

        self.assertEqual(
            params,
            {
                "creditId": "opaque-credit-id",
                "idempotencyKey": IDEMPOTENCY_KEY,
            },
        )

    def test_same_inputs_make_stable_payload_for_replay(self) -> None:
        first = build_consume_params("opaque-credit-id", IDEMPOTENCY_KEY)
        second = build_consume_params("opaque-credit-id", IDEMPOTENCY_KEY)
        self.assertEqual(first, second)

    def test_blank_credit_id_or_idempotency_key_is_never_serialized(self) -> None:
        for credit_id, key in (
            ("", IDEMPOTENCY_KEY),
            ("  ", IDEMPOTENCY_KEY),
            ("opaque-credit-id", ""),
            ("opaque-credit-id", "\t"),
        ):
            with self.subTest(credit_id=repr(credit_id), key=repr(key)):
                with self.assertRaises(GuardError):
                    build_consume_params(credit_id, key)


class ConsumeOutcomeTests(unittest.TestCase):
    def test_reset_and_already_redeemed_are_terminal_success(self) -> None:
        self.assertEqual(classify_consume_result({"outcome": "reset"}), "success")
        self.assertEqual(
            classify_consume_result({"outcome": "alreadyRedeemed"}), "success"
        )

    def test_nothing_to_reset_is_retryable(self) -> None:
        self.assertEqual(
            classify_consume_result({"outcome": "nothingToReset"}), "retry"
        )

    def test_no_credit_is_abort_unless_a_post_response_was_ambiguous(self) -> None:
        self.assertEqual(
            classify_consume_result({"outcome": "noCredit"}), "abort"
        )
        self.assertEqual(
            classify_consume_result(
                {"outcome": "noCredit"}, had_ambiguous_transport=True
            ),
            "indeterminate",
        )

    def test_already_redeemed_resolves_an_ambiguous_post_as_success(self) -> None:
        self.assertEqual(
            classify_consume_result(
                {"outcome": "alreadyRedeemed"}, had_ambiguous_transport=True
            ),
            "success",
        )

    def test_unknown_outcome_after_an_ambiguous_post_is_indeterminate(self) -> None:
        self.assertEqual(
            classify_consume_result(
                {"outcome": "futureOutcome"}, had_ambiguous_transport=True
            ),
            "indeterminate",
        )

    def test_unknown_or_changed_response_shape_fails_closed(self) -> None:
        invalid_results = (
            {},
            {"outcome": None},
            {"outcome": "futureOutcome"},
            {"outcome": "reset", "unexpected": True},
            "reset",
        )
        for result in invalid_results:
            with self.subTest(result=result):
                with self.assertRaises(GuardError):
                    classify_consume_result(result)  # type: ignore[arg-type]


class RetryScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.expires_at = datetime(2033, 5, 18, 12, 10, 0, tzinfo=UTC)

    def test_retry_uses_the_next_absolute_fifteen_second_slot(self) -> None:
        now = datetime(2033, 5, 18, 12, 5, 1, tzinfo=UTC)
        self.assertEqual(
            next_retry_at(now, self.expires_at),
            datetime(2033, 5, 18, 12, 5, 15, tzinfo=UTC),
        )

    def test_exact_slot_advances_to_the_following_slot(self) -> None:
        now = datetime(2033, 5, 18, 12, 5, 15, tzinfo=UTC)
        self.assertEqual(
            next_retry_at(now, self.expires_at),
            datetime(2033, 5, 18, 12, 5, 30, tzinfo=UTC),
        )

    def test_no_retry_is_scheduled_at_or_beyond_expiry_minus_fifteen(self) -> None:
        cutoff = self.expires_at - timedelta(seconds=15)
        for now in (
            cutoff - timedelta(seconds=15),  # next slot equals the cutoff
            cutoff - timedelta(seconds=1),
            cutoff,
            cutoff + timedelta(seconds=1),
        ):
            with self.subTest(now=now):
                self.assertIsNone(next_retry_at(now, self.expires_at))

    def test_last_permitted_slot_is_strictly_before_cutoff(self) -> None:
        now = self.expires_at - timedelta(seconds=31)
        self.assertEqual(
            next_retry_at(now, self.expires_at),
            self.expires_at - timedelta(seconds=30),
        )

    def test_naive_datetimes_fail_closed(self) -> None:
        with self.assertRaises(GuardError):
            next_retry_at(
                datetime(2033, 5, 18, 12, 5, 1),
                self.expires_at,
            )


class AmbiguousTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.expires_at = datetime(2033, 5, 18, 12, 10, 0, tzinfo=UTC)
        self.before_cutoff = self.expires_at - timedelta(minutes=1)

    def test_live_process_may_replay_twice_with_in_memory_target(self) -> None:
        for replays_used in (0, 1):
            with self.subTest(replays_used=replays_used):
                self.assertEqual(
                    transport_failure_action(
                        replays_used,
                        have_in_memory_credit_id=True,
                        now=self.before_cutoff,
                        expires_at=self.expires_at,
                    ),
                    "replay",
                )

    def test_ambiguous_replay_keeps_saved_params_when_target_disappears(self) -> None:
        original = build_consume_params("target-now-absent", IDEMPOTENCY_KEY)
        current_inventory_ids: set[str] = set()
        self.assertNotIn(original["creditId"], current_inventory_ids)

        action = transport_failure_action(
            0,
            have_in_memory_credit_id=True,
            now=self.before_cutoff,
            expires_at=self.expires_at,
        )
        replay = build_consume_params(
            original["creditId"], original["idempotencyKey"]
        )

        self.assertEqual(action, "replay")
        self.assertEqual(replay, original)

    def test_third_ambiguous_failure_is_indeterminate(self) -> None:
        self.assertEqual(
            transport_failure_action(
                2,
                have_in_memory_credit_id=True,
                now=self.before_cutoff,
                expires_at=self.expires_at,
            ),
            "indeterminate",
        )

    def test_restart_without_raw_target_cannot_replay(self) -> None:
        self.assertEqual(
            transport_failure_action(
                0,
                have_in_memory_credit_id=False,
                now=self.before_cutoff,
                expires_at=self.expires_at,
            ),
            "indeterminate",
        )

    def test_cutoff_prevents_even_an_otherwise_permitted_replay(self) -> None:
        cutoff = self.expires_at - timedelta(seconds=15)
        self.assertEqual(
            transport_failure_action(
                0,
                have_in_memory_credit_id=True,
                now=cutoff,
                expires_at=self.expires_at,
            ),
            "indeterminate",
        )


if __name__ == "__main__":
    unittest.main()
