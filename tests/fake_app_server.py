"""Small JSONL app-server double used by transport and run-loop tests.

The process never contacts Codex or the network. Configure it with environment
variables so tests can deterministically inject notifications, dropped consume
responses, EOF, and outcome sequences.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any


DEFAULT_ACCOUNT = {
    "requiresOpenaiAuth": True,
    "account": {
        "type": "chatgpt",
        "email": "guard.test@example.com",
        "planType": "plus",
    },
}

DEFAULT_RATE_LIMITS = {
    "rateLimits": {},
    "rateLimitsByLimitId": None,
    "rateLimitResetCredits": {
        "availableCount": 1,
        "credits": [
            {
                "id": "fake-credit-id",
                "expiresAt": 2_000_000_000,
                "grantedAt": 1_900_000_000,
                "resetType": "codexRateLimits",
                "status": "available",
                "title": None,
                "description": None,
            }
        ],
    },
}


def env_json(name: str, default: Any) -> Any:
    raw = os.environ.get(name)
    return default if raw is None else json.loads(raw)


def emit(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def respond(request_id: object, result: object) -> None:
    emit({"id": request_id, "result": result})


def fail(request_id: object, code: int, message: str) -> None:
    emit(
        {
            "id": request_id,
            "error": {"code": code, "message": message},
        }
    )


def main() -> int:
    account = env_json("FAKE_ACCOUNT_RESPONSE", DEFAULT_ACCOUNT)
    rate_limits = env_json("FAKE_RATE_LIMITS_RESPONSE", DEFAULT_RATE_LIMITS)
    rate_limit_sequence = env_json("FAKE_RATE_LIMITS_RESPONSES", None)
    outcomes = env_json("FAKE_CONSUME_OUTCOMES", ["reset"])
    expected_credit_id = os.environ.get("FAKE_EXPECTED_CREDIT_ID")
    expected_key = os.environ.get("FAKE_EXPECTED_IDEMPOTENCY_KEY")
    drop_consume_responses = int(os.environ.get("FAKE_DROP_CONSUME_RESPONSES", "0"))
    eof_on_consume = int(os.environ.get("FAKE_EOF_ON_CONSUME", "0"))
    interleave_notification = os.environ.get("FAKE_INTERLEAVE_NOTIFICATION") == "1"
    non_json_stdout = os.environ.get("FAKE_NON_JSON_STDOUT") == "1"
    unsolicited_id = os.environ.get("FAKE_UNSOLICITED_ID")
    duplicate_responses = os.environ.get("FAKE_DUPLICATE_RESPONSES") == "1"
    require_stable_replay = os.environ.get("FAKE_REQUIRE_STABLE_REPLAY") == "1"
    stderr_bytes = int(os.environ.get("FAKE_STDERR_BYTES", "0"))
    consume_count = 0
    rate_limit_read_count = 0
    first_consume_params: dict[str, Any] | None = None

    # A large stderr write catches transports that only drain stdout.  Keep the
    # content synthetic so the fake never puts credentials in diagnostic text.
    while stderr_bytes > 0:
        chunk_size = min(stderr_bytes, 16_384)
        sys.stderr.write("x" * chunk_size)
        sys.stderr.flush()
        stderr_bytes -= chunk_size

    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        if "id" not in request:
            # The real handshake includes an `initialized` notification after
            # the initialize response.  Notifications need no reply.
            continue
        request_id = request["id"]
        method = request.get("method")

        if interleave_notification:
            emit(
                {
                    "method": "account/updated",
                    "params": {"authMode": "chatgpt", "planType": "plus"},
                }
            )
        if non_json_stdout:
            sys.stdout.write("this is not a protocol message\n")
            sys.stdout.flush()
        if unsolicited_id is not None:
            respond(unsolicited_id, {"unsolicited": True})

        if method == "initialize":
            respond(
                request_id,
                {
                    "userAgent": "fake-codex-app-server/0",
                    "platformFamily": "windows",
                    "platformOs": "windows",
                    "codexHome": r"C:\fake-codex-home",
                },
            )
        elif method == "account/read":
            respond(request_id, account)
        elif method == "account/rateLimits/read":
            if rate_limit_sequence is None:
                current_rate_limits = rate_limits
            else:
                index = min(rate_limit_read_count, len(rate_limit_sequence) - 1)
                current_rate_limits = rate_limit_sequence[index]
                rate_limit_read_count += 1
            respond(request_id, current_rate_limits)
        elif method == "account/rateLimitResetCredit/consume":
            consume_count += 1
            params = request.get("params")
            if not isinstance(params, dict):
                fail(request_id, -32602, "params must be an object")
                continue
            if not isinstance(params.get("creditId"), str) or not params["creditId"].strip():
                fail(request_id, -32602, "creditId is required by the guard test double")
                continue
            if not isinstance(params.get("idempotencyKey"), str) or not params[
                "idempotencyKey"
            ].strip():
                fail(request_id, -32602, "idempotencyKey is required")
                continue
            if expected_credit_id is not None and params["creditId"] != expected_credit_id:
                fail(request_id, -32602, "unexpected creditId")
                continue
            if expected_key is not None and params["idempotencyKey"] != expected_key:
                fail(request_id, -32602, "unexpected idempotencyKey")
                continue
            if first_consume_params is None:
                first_consume_params = dict(params)
            elif require_stable_replay and params != first_consume_params:
                fail(request_id, -32602, "consume replay params changed")
                continue
            if eof_on_consume and consume_count == eof_on_consume:
                return 0
            if consume_count <= drop_consume_responses:
                continue
            outcome_index = min(
                consume_count - drop_consume_responses - 1,
                len(outcomes) - 1,
            )
            result = {"outcome": outcomes[outcome_index]}
            respond(request_id, result)
            if duplicate_responses:
                respond(request_id, result)
        else:
            fail(request_id, -32601, f"unknown method: {method}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
