"""Secret-safe logging contracts."""

from __future__ import annotations

import copy
import unittest

from codex_reset_guard import redact_for_log


class RedactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.credit_id = "credit-opaque-secret-123"
        self.email = "private.user@example.com"
        self.idempotency_key = "4a77335a-4da5-4d0e-a5f6-930d92bc276f"

    def assert_secrets_absent(self, rendered: str) -> None:
        for secret in (self.credit_id, self.email, self.idempotency_key):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, rendered)

    def test_nested_wire_payload_is_sanitized_without_mutating_input(self) -> None:
        event = {
            "method": "account/rateLimitResetCredit/consume",
            "params": {
                "creditId": self.credit_id,
                "idempotencyKey": self.idempotency_key,
            },
            "account": {"email": self.email},
            "safe": {"outcome": "reset"},
        }
        original = copy.deepcopy(event)

        rendered = redact_for_log(
            event,
            secrets=(self.credit_id, self.email, self.idempotency_key),
        )

        self.assertIsInstance(rendered, str)
        self.assert_secrets_absent(rendered)
        self.assertIn("reset", rendered)
        self.assertEqual(event, original)

    def test_sensitive_field_names_are_redacted_even_without_explicit_secrets(self) -> None:
        rendered = redact_for_log(
            {
                "creditId": self.credit_id,
                "email": self.email,
                "idempotencyKey": self.idempotency_key,
                "authorization": "Bearer token-secret",
                "accessToken": "token-secret",
            }
        )

        self.assert_secrets_absent(rendered)
        self.assertNotIn("token-secret", rendered)

    def test_secret_substrings_in_exception_text_are_removed(self) -> None:
        message = (
            f"request for {self.credit_id} and {self.email} failed; "
            f"idempotency={self.idempotency_key}"
        )
        rendered = redact_for_log(
            message,
            secrets=(self.credit_id, self.email, self.idempotency_key),
        )
        self.assert_secrets_absent(rendered)

    def test_empty_secret_does_not_erase_the_entire_log(self) -> None:
        rendered = redact_for_log(
            {"status": "dry-run"},
            secrets=("", "   "),
        )
        self.assertIn("dry-run", rendered)


if __name__ == "__main__":
    unittest.main()
