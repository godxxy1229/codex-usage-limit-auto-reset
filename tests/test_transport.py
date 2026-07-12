"""Process-level tests for the JSONL app-server transport.

Only ``tests/fake_app_server.py`` is launched.  These tests never execute the
real Codex binary and never contact an account or consume a reset credit.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from codex_reset_guard import (
    AppServerTransport,
    CONSUME_METHOD,
    ProtocolError,
    RpcError,
    TransportError,
    build_consume_params,
)


FAKE_SERVER = Path(__file__).with_name("fake_app_server.py")
FAKE_CODEX_HOME = Path(r"C:\fake-codex-home")
IDEMPOTENCY_KEY = "4a77335a-4da5-4d0e-a5f6-930d92bc276f"


class AppServerTransportTests(unittest.TestCase):
    def transport(
        self,
        *,
        timeout: float = 1.0,
        env: dict[str, str] | None = None,
    ) -> AppServerTransport:
        instance = AppServerTransport(
            exe=Path(sys.executable),
            codex_home=FAKE_CODEX_HOME,
            request_timeout=timeout,
            command=[sys.executable, str(FAKE_SERVER)],
            extra_env=env,
        )
        self.addCleanup(instance.close)
        return instance

    def test_start_and_reads_demultiplex_interleaved_notifications(self) -> None:
        transport = self.transport(env={"FAKE_INTERLEAVE_NOTIFICATION": "1"})

        initialized = transport.start()
        account = transport.request("account/read", {"refreshToken": False})
        rate_limits = transport.request("account/rateLimits/read")

        self.assertEqual(initialized["codexHome"], str(FAKE_CODEX_HOME))
        self.assertEqual(account["account"]["email"], "guard.test@example.com")
        self.assertEqual(
            rate_limits["rateLimitResetCredits"]["credits"][0]["id"],
            "fake-credit-id",
        )
        self.assertGreaterEqual(transport.notifications_seen, 3)

    def test_non_json_stdout_is_a_protocol_error(self) -> None:
        transport = self.transport(env={"FAKE_NON_JSON_STDOUT": "1"})

        with self.assertRaises(ProtocolError):
            transport.start()

    def test_unsolicited_or_invalid_response_id_is_a_protocol_error(self) -> None:
        transport = self.transport(env={"FAKE_UNSOLICITED_ID": "999"})

        with self.assertRaises(ProtocolError):
            transport.start()

    def test_dropped_consume_times_out_then_replays_with_new_rpc_id(self) -> None:
        transport = self.transport(
            timeout=0.25,
            env={
                "FAKE_DROP_CONSUME_RESPONSES": "1",
                "FAKE_REQUIRE_STABLE_REPLAY": "1",
                "FAKE_EXPECTED_CREDIT_ID": "fake-credit-id",
                "FAKE_EXPECTED_IDEMPOTENCY_KEY": IDEMPOTENCY_KEY,
            },
        )
        transport.start()
        params = build_consume_params("fake-credit-id", IDEMPOTENCY_KEY)

        with self.assertRaises(TransportError) as raised:
            transport.request(CONSUME_METHOD, params)
        self.assertTrue(raised.exception.after_write)

        # The fake rejects changed replay parameters.  A successful second
        # response therefore proves the new RPC request ID reused the same
        # creditId and idempotency key.
        result = transport.request(CONSUME_METHOD, params)
        self.assertEqual(result, {"outcome": "reset"})

    def test_eof_during_consume_is_an_after_write_transport_error(self) -> None:
        transport = self.transport(
            timeout=1.0,
            env={"FAKE_EOF_ON_CONSUME": "1"},
        )
        transport.start()
        params = build_consume_params("fake-credit-id", IDEMPOTENCY_KEY)

        with self.assertRaises(TransportError) as raised:
            transport.request(CONSUME_METHOD, params)

        self.assertTrue(raised.exception.after_write)

    def test_fake_rpc_error_is_not_misclassified_as_transport_success(self) -> None:
        transport = self.transport()
        transport.start()

        # Bypass the guard payload builder solely to make the fake return an
        # RPC error object.  No real app-server is involved.
        with self.assertRaises(RpcError) as raised:
            transport.request(
                CONSUME_METHOD,
                {"creditId": "", "idempotencyKey": IDEMPOTENCY_KEY},
            )

        self.assertTrue(raised.exception.after_write)
        self.assertEqual(raised.exception.method, CONSUME_METHOD)

    def test_large_stderr_is_drained_without_deadlock(self) -> None:
        transport = self.transport(
            timeout=2.0,
            env={"FAKE_STDERR_BYTES": str(2 * 1024 * 1024)},
        )

        transport.start()
        account = transport.request("account/read", {"refreshToken": False})

        self.assertEqual(account["account"]["type"], "chatgpt")


if __name__ == "__main__":
    unittest.main()
