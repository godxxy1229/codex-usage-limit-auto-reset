"""Fail-closed validation contracts for reset-credit enrollment and execution."""

from __future__ import annotations

import copy
import unittest

from codex_reset_guard import (
    GuardError,
    hash_account_email,
    make_target_pin,
    resolve_pinned_credit,
    select_unique_earliest_credit,
    validate_account_pin,
    validate_binary_pin,
)


def credit(
    credit_id: str,
    expires_at: int = 2_000_000_000,
    *,
    granted_at: int = 1_900_000_000,
    reset_type: str = "codexRateLimits",
    status: str = "available",
) -> dict[str, object]:
    return {
        "id": credit_id,
        "expiresAt": expires_at,
        "grantedAt": granted_at,
        "resetType": reset_type,
        "status": status,
        "title": None,
        "description": None,
    }


def rate_limits_response(
    credits: list[dict[str, object]] | None,
    *,
    available_count: int | None = None,
) -> dict[str, object]:
    if available_count is None:
        available_count = len(credits) if credits is not None else 1
    return {
        "rateLimits": {},
        "rateLimitsByLimitId": None,
        "rateLimitResetCredits": {
            "availableCount": available_count,
            "credits": credits,
        },
    }


class InventoryValidationTests(unittest.TestCase):
    def test_selects_the_only_unique_earliest_credit(self) -> None:
        first = credit("credit-first", 2_000_000_000)
        later = credit("credit-later", 2_000_000_100)

        selected = select_unique_earliest_credit(
            rate_limits_response([later, first])
        )

        self.assertEqual(selected, first)

    def test_missing_or_null_summary_fails_closed(self) -> None:
        with self.assertRaises(GuardError):
            select_unique_earliest_credit({"rateLimits": {}})
        with self.assertRaises(GuardError):
            select_unique_earliest_credit(
                {"rateLimits": {}, "rateLimitResetCredits": None}
            )

    def test_null_detail_list_fails_closed(self) -> None:
        with self.assertRaises(GuardError):
            select_unique_earliest_credit(rate_limits_response(None))

        with self.assertRaises(GuardError):
            select_unique_earliest_credit(
                {
                    "rateLimits": {},
                    "rateLimitResetCredits": {"availableCount": 1},
                }
            )

    def test_capped_detail_list_fails_closed(self) -> None:
        with self.assertRaises(GuardError):
            select_unique_earliest_credit(
                rate_limits_response([credit("one")], available_count=2)
            )

    def test_boolean_is_not_accepted_as_an_integer_protocol_field(self) -> None:
        summary = rate_limits_response([credit("one")])
        summary["rateLimitResetCredits"]["availableCount"] = True
        with self.assertRaises(GuardError):
            select_unique_earliest_credit(summary)

        for field in ("expiresAt", "grantedAt"):
            with self.subTest(field=field):
                row = credit("one")
                row[field] = True
                with self.assertRaises(GuardError):
                    select_unique_earliest_credit(rate_limits_response([row]))

    def test_blank_or_duplicate_ids_fail_closed(self) -> None:
        for blank in ("", " ", "\t"):
            with self.subTest(blank=repr(blank)):
                with self.assertRaises(GuardError):
                    select_unique_earliest_credit(
                        rate_limits_response([credit(blank)])
                    )

        duplicate = [credit("same", 2_000_000_000), credit("same", 2_000_000_100)]
        with self.assertRaises(GuardError):
            select_unique_earliest_credit(rate_limits_response(duplicate))

    def test_unknown_type_or_status_fails_closed(self) -> None:
        with self.assertRaises(GuardError):
            select_unique_earliest_credit(
                rate_limits_response([credit("one", reset_type="unknown")])
            )
        with self.assertRaises(GuardError):
            select_unique_earliest_credit(
                rate_limits_response([credit("one", status="unknown")])
            )

        # Unknown values anywhere in the complete list are a contract change,
        # even when the unknown row would not otherwise be selected.
        with self.assertRaises(GuardError):
            select_unique_earliest_credit(
                rate_limits_response(
                    [
                        credit("known-earliest", 2_000_000_000),
                        credit("unknown-later", 2_000_000_100, status="unknown"),
                    ]
                )
            )

    def test_non_expiring_credit_is_not_safe_for_scheduled_enrollment(self) -> None:
        row = credit("one")
        row["expiresAt"] = None
        with self.assertRaises(GuardError):
            select_unique_earliest_credit(rate_limits_response([row]))

    def test_non_expiring_credit_sorts_after_a_finite_candidate(self) -> None:
        finite = credit("finite", 2_000_000_000)
        non_expiring = credit("non-expiring")
        non_expiring["expiresAt"] = None

        selected = select_unique_earliest_credit(
            rate_limits_response([non_expiring, finite])
        )

        self.assertEqual(selected, finite)

    def test_tied_earliest_expiry_fails_closed(self) -> None:
        with self.assertRaises(GuardError):
            select_unique_earliest_credit(
                rate_limits_response(
                    [credit("one", 2_000_000_000), credit("two", 2_000_000_000)]
                )
            )


class TargetPinValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = credit("opaque-target-id", 2_000_000_000)
        self.pin = make_target_pin(self.target)

    def test_pin_contains_hash_and_metadata_but_not_raw_id(self) -> None:
        self.assertEqual(
            set(self.pin),
            {"creditIdSha256", "expiresAt", "grantedAt", "resetType"},
        )
        self.assertNotIn(self.target["id"], repr(self.pin))

    def test_exact_target_with_only_later_new_credit_is_allowed(self) -> None:
        response = rate_limits_response(
            [self.target, credit("new-later", 2_000_000_500)]
        )

        resolved = resolve_pinned_credit(response, self.pin)

        self.assertEqual(resolved, self.target["id"])

    def test_new_non_expiring_credit_is_treated_as_later_and_allowed(self) -> None:
        non_expiring = credit("new-non-expiring")
        non_expiring["expiresAt"] = None

        resolved = resolve_pinned_credit(
            rate_limits_response([self.target, non_expiring]),
            self.pin,
        )

        self.assertEqual(resolved, self.target["id"])

    def test_new_earlier_credit_fails_closed(self) -> None:
        response = rate_limits_response(
            [self.target, credit("new-earlier", 1_999_999_999)]
        )
        with self.assertRaises(GuardError):
            resolve_pinned_credit(response, self.pin)

    def test_new_credit_tied_with_target_fails_closed(self) -> None:
        response = rate_limits_response(
            [self.target, credit("same-expiry", 2_000_000_000)]
        )
        with self.assertRaises(GuardError):
            resolve_pinned_credit(response, self.pin)

    def test_target_metadata_mismatch_fails_closed(self) -> None:
        mutations = {
            "expiresAt": 2_000_000_001,
            "grantedAt": 1_900_000_001,
            "resetType": "unknown",
            "status": "redeeming",
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                changed = copy.deepcopy(self.target)
                changed[field] = value
                with self.assertRaises(GuardError):
                    resolve_pinned_credit(rate_limits_response([changed]), self.pin)

    def test_missing_target_fails_closed(self) -> None:
        with self.assertRaises(GuardError):
            resolve_pinned_credit(
                rate_limits_response([credit("not-the-target", 2_000_000_500)]),
                self.pin,
            )


class AccountAndBinaryPinTests(unittest.TestCase):
    def test_email_hash_is_normalized_for_case_and_surrounding_space(self) -> None:
        self.assertEqual(
            hash_account_email(" User@Example.COM "),
            hash_account_email("user@example.com"),
        )

    def test_matching_chatgpt_account_is_accepted_but_plan_is_not_pinned(self) -> None:
        expected = hash_account_email("user@example.com")
        response = {
            "requiresOpenaiAuth": True,
            "account": {
                "type": "chatgpt",
                "email": "User@Example.com",
                "planType": "enterprise",
            },
        }
        validate_account_pin(response, expected)

        # A normal plan change must not invalidate the account identity pin.
        response["account"]["planType"] = "plus"
        validate_account_pin(response, expected)

    def test_account_identity_mismatch_or_non_chatgpt_account_fails(self) -> None:
        expected = hash_account_email("user@example.com")
        wrong_email = {
            "requiresOpenaiAuth": True,
            "account": {
                "type": "chatgpt",
                "email": "someone-else@example.com",
                "planType": "plus",
            },
        }
        api_key = {
            "requiresOpenaiAuth": True,
            "account": {"type": "apiKey"},
        }
        for response in (wrong_email, api_key, {"requiresOpenaiAuth": True, "account": None}):
            with self.subTest(response=response):
                with self.assertRaises(GuardError):
                    validate_account_pin(response, expected)

    def test_any_pinned_binary_field_mismatch_fails_closed(self) -> None:
        expected = {
            "path": r"C:\tools\codex.exe",
            "version": "codex-cli 0.144.1",
            "sha256": "a" * 64,
        }
        validate_binary_pin(expected, dict(expected))

        for field, value in {
            "path": r"C:\other\codex.exe",
            "version": "codex-cli 0.145.0",
            "sha256": "b" * 64,
        }.items():
            with self.subTest(field=field):
                observed = dict(expected)
                observed[field] = value
                with self.assertRaises(GuardError):
                    validate_binary_pin(expected, observed)


if __name__ == "__main__":
    unittest.main()
