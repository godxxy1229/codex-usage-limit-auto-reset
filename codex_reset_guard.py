#!/usr/bin/env python3
"""Fail-closed guard for one-shot Codex usage limit reset use.

The guard only talks to the local Codex app-server.  It never reads
``auth.json`` and never persists a raw usage limit reset identifier.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


UTC = timezone.utc
APP_VERSION = "2.1.0"
LEGACY_MANIFEST_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 2
SUPPORTED_MANIFEST_SCHEMA_VERSIONS = {
    LEGACY_MANIFEST_SCHEMA_VERSION,
    MANIFEST_SCHEMA_VERSION,
}
MINIMUM_CODEX_VERSION = (0, 144, 1)
TASK_START_LEAD_SECONDS = 345
PROCESS_LEAD_SECONDS = 300
CUTOFF_LEAD_SECONDS = 15
ARM_MINIMUM_MARGIN_SECONDS = 600
NOTHING_RETRY_SECONDS = 15
MAX_AMBIGUOUS_REPLAYS = 2
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0
CONSUME_METHOD = "account/rateLimitResetCredit/consume"
SENSITIVE_KEYS = {
    "authorization",
    "accesstoken",
    "access_token",
    "refreshtoken",
    "refresh_token",
    "creditid",
    "credit_id",
    "idempotencykey",
    "idempotency_key",
    "email",
    "token",
}
TERMINAL_STATES = {
    "SUCCEEDED",
    "NO_ACTION",
    "FAILED",
    "INDETERMINATE",
    "DISARMED",
    "CLEANED",
}
EXECUTION_PHASES = {"preDispatch", "postDispatch"}
EXECUTION_KEYS = {"phase", "result", "failureCode", "terminalAt"}

# These values are persisted and consumed by the manager. Keep them stable and
# deliberately less specific than exception messages, which may change.
FAILURE_PRE_DISPATCH_GUARD = "PRE_DISPATCH_GUARD_ERROR"
FAILURE_PRE_DISPATCH_PROTOCOL = "PRE_DISPATCH_PROTOCOL_ERROR"
FAILURE_PRE_DISPATCH_RPC = "PRE_DISPATCH_RPC_ERROR"
FAILURE_PRE_DISPATCH_TRANSPORT = "PRE_DISPATCH_TRANSPORT_ERROR"
FAILURE_PRE_DISPATCH_CLI = "PRE_DISPATCH_CLI_VALIDATION"
FAILURE_PRE_DISPATCH_TASK = "PRE_DISPATCH_TASK_CONTRACT"
FAILURE_PRE_DISPATCH_TIME = "PRE_DISPATCH_TIME_SYNC"
FAILURE_PRE_DISPATCH_ACCOUNT = "PRE_DISPATCH_ACCOUNT_MISMATCH"
FAILURE_PRE_DISPATCH_INVENTORY = "PRE_DISPATCH_INVENTORY_INVALID"
FAILURE_PRE_DISPATCH_CUTOFF = "PRE_DISPATCH_CUTOFF"
FAILURE_POST_DISPATCH_UNCONFIRMED = "POST_DISPATCH_UNCONFIRMED"
FAILURE_POST_DISPATCH_TRANSPORT = "POST_DISPATCH_TRANSPORT_AMBIGUOUS"
FAILURE_POST_DISPATCH_RPC = "POST_DISPATCH_RPC_AMBIGUOUS"
FAILURE_POST_DISPATCH_PROTOCOL = "POST_DISPATCH_PROTOCOL_ERROR"
FAILURE_POST_DISPATCH_RESPONSE = "POST_DISPATCH_RESPONSE_CONTRACT"
FAILURE_POST_DISPATCH_NO_CREDIT = "POST_DISPATCH_NO_CREDIT"
FAILURE_POST_DISPATCH_NOTHING = "POST_DISPATCH_NOTHING_TO_RESET"
FAILURE_POST_DISPATCH_TIME = "POST_DISPATCH_TIME_SYNC"
FAILURE_POST_DISPATCH_ACCOUNT = "POST_DISPATCH_ACCOUNT_MISMATCH"
FAILURE_POST_DISPATCH_INVENTORY = "POST_DISPATCH_INVENTORY_INVALID"
FAILURE_USER_CANCELLED = "USER_CANCELLED"


def _subprocess_creationflags() -> int:
    """Hide child-process console windows while remaining portable."""
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class GuardError(Exception):
    """A deterministic fail-closed guard error."""


class ProtocolError(GuardError):
    """The app-server protocol was malformed or changed."""


class SafetyCheckError(GuardError):
    """A guard error tagged with a stable safety-check category."""

    def __init__(self, check: str, message: str) -> None:
        super().__init__(message)
        self.check = check


class RpcError(GuardError):
    """The app-server returned an RPC error."""

    def __init__(self, method: str, error: object, *, after_write: bool = True) -> None:
        super().__init__(f"app-server RPC failed for {method}")
        self.method = method
        self.error = error
        self.after_write = after_write


def _is_ambiguous_consume_timeout(error: RpcError) -> bool:
    """Recognize only the exact Codex 0.144.1 consume timeout contract."""
    payload = error.error
    return (
        error.method == CONSUME_METHOD
        and error.after_write
        and isinstance(payload, Mapping)
        and set(payload) == {"code", "message"}
        and type(payload.get("code")) is int
        and payload.get("code") == -32603
        and payload.get("message") == "rate limit reset consume timed out"
    )


class TransportError(GuardError):
    """The app-server transport ended or timed out."""

    def __init__(self, message: str, *, after_write: bool) -> None:
        super().__init__(message)
        self.after_write = after_write


@dataclass(frozen=True)
class CreditRecord:
    raw_id: str = field(repr=False)
    id_sha256: str
    expires_at: int | None
    granted_at: int
    reset_type: str
    status: str


@dataclass(frozen=True)
class AccountIdentity:
    raw_email: str = field(repr=False)
    email_sha256: str


@dataclass(frozen=True)
class BinaryInfo:
    path: str
    version: str
    sha256: str
    signer_subject: str | None = None


@dataclass(frozen=True)
class RunResult:
    state: str
    outcome: str | None = None
    message: str | None = None


class _Omit:
    pass


OMIT = _Omit()


def _is_int(value: object) -> bool:
    return type(value) is int


def sha256_text(value: str) -> str:
    if not isinstance(value, str):
        raise GuardError("value to hash must be text")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise GuardError(f"unable to hash pinned executable: {path}") from error
    return digest.hexdigest()


def hash_account_email(email: str) -> str:
    if not isinstance(email, str) or not email.strip():
        raise GuardError("ChatGPT account email is unavailable")
    return sha256_text(email.strip().casefold())


def _expect_exact_keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise GuardError(f"{label} contains unsupported fields")


def _credit_records(full_response: Mapping[str, Any]) -> list[CreditRecord]:
    if not isinstance(full_response, Mapping):
        raise GuardError("rate-limit response must be an object")
    summary = full_response.get("rateLimitResetCredits")
    if not isinstance(summary, Mapping):
        raise GuardError("usage limit reset summary is missing")
    _expect_exact_keys(dict(summary), {"availableCount", "credits"}, "usage limit reset summary")
    available_count = summary.get("availableCount")
    credits = summary.get("credits")
    if not _is_int(available_count) or available_count < 0:
        raise GuardError("availableCount must be a non-negative integer")
    if not isinstance(credits, list):
        raise GuardError("usage limit reset details are unavailable")
    if len(credits) != available_count:
        raise GuardError("usage limit reset details are incomplete or capped")

    result: list[CreditRecord] = []
    ids: set[str] = set()
    allowed_keys = {
        "id",
        "expiresAt",
        "grantedAt",
        "resetType",
        "status",
        "title",
        "description",
    }
    for row in credits:
        if not isinstance(row, Mapping):
            raise GuardError("usage limit reset detail must be an object")
        _expect_exact_keys(dict(row), allowed_keys, "usage limit reset detail")
        raw_id = row.get("id")
        granted_at = row.get("grantedAt")
        expires_at = row.get("expiresAt")
        reset_type = row.get("resetType")
        status = row.get("status")
        title = row.get("title")
        description = row.get("description")
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise GuardError("usage limit reset ID is missing or blank")
        if raw_id in ids:
            raise GuardError("usage limit reset IDs are not unique")
        ids.add(raw_id)
        if not _is_int(granted_at) or granted_at <= 0:
            raise GuardError("grantedAt must be a positive integer")
        if expires_at is not None and (not _is_int(expires_at) or expires_at <= 0):
            raise GuardError("expiresAt must be null or a positive integer")
        if reset_type != "codexRateLimits":
            raise GuardError("unknown usage limit reset type")
        if status != "available":
            raise GuardError("usage limit reset is not available")
        if title is not None and not isinstance(title, str):
            raise GuardError("usage limit reset title has an unexpected type")
        if description is not None and not isinstance(description, str):
            raise GuardError("usage limit reset description has an unexpected type")
        result.append(
            CreditRecord(
                raw_id=raw_id,
                id_sha256=sha256_text(raw_id),
                expires_at=expires_at,
                granted_at=granted_at,
                reset_type=reset_type,
                status=status,
            )
        )
    return result


def select_unique_earliest_credit(full_rate_limits_response: Mapping[str, Any]) -> dict[str, Any]:
    records = _credit_records(full_rate_limits_response)
    finite = [record for record in records if record.expires_at is not None]
    if not finite:
        raise GuardError("no expiring usage limit reset is available")
    earliest_expiry = min(record.expires_at for record in finite)
    earliest = [record for record in finite if record.expires_at == earliest_expiry]
    if len(earliest) != 1:
        raise GuardError("the earliest expiration is not unique")
    chosen = earliest[0]
    return {
        "id": chosen.raw_id,
        "expiresAt": chosen.expires_at,
        "grantedAt": chosen.granted_at,
        "resetType": chosen.reset_type,
        "status": chosen.status,
        "title": None,
        "description": None,
    }


def make_target_pin(credit: Mapping[str, Any]) -> dict[str, Any]:
    raw_id = credit.get("id")
    expires_at = credit.get("expiresAt")
    granted_at = credit.get("grantedAt")
    reset_type = credit.get("resetType")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise GuardError("target usage limit reset ID is missing")
    if not _is_int(expires_at) or expires_at <= 0:
        raise GuardError("target usage limit reset must have a finite expiration")
    if not _is_int(granted_at) or granted_at <= 0:
        raise GuardError("target grantedAt is invalid")
    if reset_type != "codexRateLimits":
        raise GuardError("target reset type is unsupported")
    return {
        "creditIdSha256": sha256_text(raw_id),
        "expiresAt": expires_at,
        "grantedAt": granted_at,
        "resetType": reset_type,
    }


def _validate_target_pin(pin: Mapping[str, Any]) -> None:
    if set(pin) != {"creditIdSha256", "expiresAt", "grantedAt", "resetType"}:
        raise GuardError("target pin shape is invalid")
    digest = pin.get("creditIdSha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise GuardError("target usage limit reset hash is invalid")
    if not _is_int(pin.get("expiresAt")) or pin["expiresAt"] <= 0:
        raise GuardError("target expiration is invalid")
    if not _is_int(pin.get("grantedAt")) or pin["grantedAt"] <= 0:
        raise GuardError("target grant time is invalid")
    if pin.get("resetType") != "codexRateLimits":
        raise GuardError("target reset type is invalid")


def resolve_pinned_credit(full_rate_limits_response: Mapping[str, Any], pin: Mapping[str, Any]) -> str:
    _validate_target_pin(pin)
    records = _credit_records(full_rate_limits_response)
    matches = [record for record in records if record.id_sha256 == pin["creditIdSha256"]]
    if len(matches) != 1:
        raise GuardError("the pinned usage limit reset is missing or ambiguous")
    target = matches[0]
    if (
        target.expires_at != pin["expiresAt"]
        or target.granted_at != pin["grantedAt"]
        or target.reset_type != pin["resetType"]
        or target.status != "available"
    ):
        raise GuardError("the pinned usage limit reset metadata changed")
    finite = [record for record in records if record.expires_at is not None]
    if not finite:
        raise GuardError("no finite usage limit reset expiration is available")
    minimum = min(record.expires_at for record in finite)
    if target.expires_at != minimum:
        raise GuardError("a different usage limit reset expires earlier")
    if sum(1 for record in finite if record.expires_at == minimum) != 1:
        raise GuardError("the earliest expiration is tied")
    return target.raw_id


def _validate_replay_inventory(
    full_rate_limits_response: Mapping[str, Any],
    pin: Mapping[str, Any],
    raw_credit_id: str,
) -> None:
    """Validate an ambiguous replay without requiring the target to remain listed."""
    _validate_target_pin(pin)
    records = _credit_records(full_rate_limits_response)
    target_hash = sha256_text(raw_credit_id)
    if target_hash != pin["creditIdSha256"]:
        raise GuardError("in-memory target no longer matches the pin")
    matches = [record for record in records if record.id_sha256 == target_hash]
    if len(matches) > 1:
        raise GuardError("pinned target is duplicated during replay")
    if not matches:
        return
    target = matches[0]
    if (
        target.expires_at != pin["expiresAt"]
        or target.granted_at != pin["grantedAt"]
        or target.reset_type != pin["resetType"]
    ):
        raise GuardError("pinned target metadata changed during replay")
    finite = [record for record in records if record.expires_at is not None]
    minimum = min(record.expires_at for record in finite)
    if target.expires_at != minimum or sum(
        1 for record in finite if record.expires_at == minimum
    ) != 1:
        raise GuardError("target is no longer uniquely earliest during replay")


def validate_account_pin(account_response: Mapping[str, Any], expected_email_sha256: str) -> None:
    if not isinstance(account_response, Mapping):
        raise GuardError("account response must be an object")
    account = account_response.get("account")
    if not isinstance(account, Mapping) or account.get("type") != "chatgpt":
        raise GuardError("the active account is not a ChatGPT account")
    email = account.get("email")
    if hash_account_email(email) != expected_email_sha256:
        raise GuardError("the active ChatGPT account changed")
    plan_type = account.get("planType")
    if plan_type is not None and not isinstance(plan_type, str):
        raise GuardError("account plan type has an unexpected shape")


def _account_identity(account_response: Mapping[str, Any]) -> AccountIdentity:
    if not isinstance(account_response, Mapping):
        raise GuardError("account response must be an object")
    account = account_response.get("account")
    if not isinstance(account, Mapping) or account.get("type") != "chatgpt":
        raise GuardError("ChatGPT login is required")
    email = account.get("email")
    if not isinstance(email, str) or not email.strip():
        raise GuardError("ChatGPT account email is unavailable")
    return AccountIdentity(raw_email=email, email_sha256=hash_account_email(email))


def validate_binary_pin(expected: Mapping[str, Any], observed: Mapping[str, Any]) -> None:
    required = {"path", "version", "sha256"}
    if not required.issubset(expected) or not required.issubset(observed):
        raise GuardError("binary pin is incomplete")
    expected_path = os.path.normcase(os.path.normpath(str(expected["path"])))
    observed_path = os.path.normcase(os.path.normpath(str(observed["path"])))
    if expected_path != observed_path:
        raise GuardError("Codex executable path changed")
    if expected["version"] != observed["version"]:
        raise GuardError("Codex CLI version changed")
    if str(expected["sha256"]).casefold() != str(observed["sha256"]).casefold():
        raise GuardError("Codex executable hash changed")


def build_consume_params(credit_id: str, idempotency_key: str) -> dict[str, str]:
    if not isinstance(credit_id, str) or not credit_id.strip():
        raise GuardError("creditId is required and cannot be blank")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise GuardError("idempotencyKey is required and cannot be blank")
    try:
        parsed = uuid.UUID(idempotency_key)
    except (ValueError, AttributeError) as error:
        raise GuardError("idempotencyKey must be a UUID") from error
    if parsed.version != 4:
        raise GuardError("idempotencyKey must be a UUIDv4")
    return {"creditId": credit_id, "idempotencyKey": idempotency_key}


def classify_consume_result(
    result: Mapping[str, Any], *, had_ambiguous_transport: bool = False
) -> str:
    if not isinstance(result, Mapping) or set(result) != {"outcome"}:
        raise GuardError("consume response shape changed")
    outcome = result.get("outcome")
    if outcome in {"reset", "alreadyRedeemed"}:
        return "success"
    if outcome == "nothingToReset":
        return "retry"
    if outcome == "noCredit":
        return "indeterminate" if had_ambiguous_transport else "abort"
    if had_ambiguous_transport:
        return "indeterminate"
    raise GuardError("consume returned an unknown outcome")


def next_retry_at(now: datetime, expires_at: datetime) -> datetime | None:
    if now.tzinfo is None or expires_at.tzinfo is None:
        raise GuardError("retry timestamps must be timezone-aware")
    now_utc = now.astimezone(UTC)
    expires_utc = expires_at.astimezone(UTC)
    cutoff = expires_utc - timedelta(seconds=CUTOFF_LEAD_SECONDS)
    next_epoch = (math.floor(now_utc.timestamp() / NOTHING_RETRY_SECONDS) + 1) * NOTHING_RETRY_SECONDS
    candidate = datetime.fromtimestamp(next_epoch, UTC)
    if candidate >= cutoff:
        return None
    return candidate


def transport_failure_action(
    replays_used: int,
    *,
    have_in_memory_credit_id: bool,
    now: datetime,
    expires_at: datetime,
) -> str:
    if now.tzinfo is None or expires_at.tzinfo is None:
        raise GuardError("transport timestamps must be timezone-aware")
    cutoff = expires_at.astimezone(UTC) - timedelta(seconds=CUTOFF_LEAD_SECONDS)
    if (
        not have_in_memory_credit_id
        or replays_used >= MAX_AMBIGUOUS_REPLAYS
        or now.astimezone(UTC) >= cutoff
    ):
        return "indeterminate"
    return "replay"


def _redact_object(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).replace("-", "").replace("_", "").casefold()
            sensitive = {
                name.replace("-", "").replace("_", "").casefold()
                for name in SENSITIVE_KEYS
            }
            result[str(key)] = "[REDACTED]" if normalized in sensitive else _redact_object(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact_object(item) for item in value]
    return value


def redact_for_log(value: Any, secrets: Iterable[str] = ()) -> str:
    if isinstance(value, str):
        rendered = value
    else:
        rendered = json.dumps(_redact_object(copy.deepcopy(value)), ensure_ascii=False, sort_keys=True)
    for secret in secrets:
        if isinstance(secret, str) and secret.strip():
            rendered = rendered.replace(secret, "[REDACTED]")
    return rendered


def _iso_utc(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_utc(value: str) -> int:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise GuardError("UTC schedule timestamp is invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise GuardError("UTC schedule timestamp is invalid") from error
    return int(parsed.timestamp())


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError as error:
        raise GuardError(f"manifest not found: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise GuardError(f"unable to read manifest: {path}") from error
    if not isinstance(value, dict):
        raise GuardError("manifest must contain an object")
    return value


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    base_required = {
        "schemaVersion",
        "jobId",
        "createdAtUtc",
        "armed",
        "state",
        "target",
        "account",
        "runtime",
        "schedule",
        "task",
    }
    schema_version = manifest.get("schemaVersion")
    if schema_version not in SUPPORTED_MANIFEST_SCHEMA_VERSIONS:
        raise GuardError("manifest schema version is unsupported")
    required = set(base_required)
    if schema_version == LEGACY_MANIFEST_SCHEMA_VERSION:
        required.add("idempotencyKey")
    else:
        required.add("execution")
    if set(manifest) != required:
        raise GuardError("manifest shape changed")
    try:
        uuid.UUID(str(manifest.get("jobId")))
    except ValueError as error:
        raise GuardError("manifest job identifier is invalid") from error
    if schema_version == LEGACY_MANIFEST_SCHEMA_VERSION:
        try:
            key = uuid.UUID(str(manifest.get("idempotencyKey")))
        except ValueError as error:
            raise GuardError("manifest idempotency key is invalid") from error
        if key.version != 4:
            raise GuardError("manifest idempotency key is not UUIDv4")
    if type(manifest.get("armed")) is not bool:
        raise GuardError("manifest armed flag is invalid")
    if not isinstance(manifest.get("state"), str):
        raise GuardError("manifest state is invalid")
    target = manifest.get("target")
    if not isinstance(target, Mapping):
        raise GuardError("manifest target is invalid")
    _validate_target_pin(target)
    account = manifest.get("account")
    if not isinstance(account, Mapping) or set(account) != {"emailSha256"}:
        raise GuardError("manifest account pin is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(account.get("emailSha256"))):
        raise GuardError("manifest account hash is invalid")
    runtime = manifest.get("runtime")
    runtime_keys = {"codexHome", "codexExe", "codexVersion", "codexSha256", "signerSubject"}
    if not isinstance(runtime, Mapping) or set(runtime) != runtime_keys:
        raise GuardError("manifest runtime pin is invalid")
    if not all(isinstance(runtime.get(key), str) and runtime[key] for key in runtime_keys):
        raise GuardError("manifest runtime pin is incomplete")
    schedule = manifest.get("schedule")
    schedule_keys = {
        "triggerAtUtc",
        "processAtUtc",
        "cutoffAtUtc",
        "expiresAtUtc",
    }
    if not isinstance(schedule, Mapping) or set(schedule) != schedule_keys:
        raise GuardError("manifest schedule is invalid")
    trigger = _parse_iso_utc(schedule["triggerAtUtc"])
    process = _parse_iso_utc(schedule["processAtUtc"])
    cutoff = _parse_iso_utc(schedule["cutoffAtUtc"])
    expires = _parse_iso_utc(schedule["expiresAtUtc"])
    if (
        trigger != expires - TASK_START_LEAD_SECONDS
        or process != expires - PROCESS_LEAD_SECONDS
        or cutoff != expires - CUTOFF_LEAD_SECONDS
        or expires != target["expiresAt"]
    ):
        raise GuardError("manifest schedule does not match target expiration")
    task = manifest.get("task")
    if not isinstance(task, Mapping) or set(task) != {"name"}:
        raise GuardError("manifest task record is invalid")
    if task.get("name") is not None and not isinstance(task.get("name"), str):
        raise GuardError("manifest task name is invalid")
    if manifest.get("armed") and not isinstance(task.get("name"), str):
        raise GuardError("armed manifest has no Scheduled Task name")
    if schema_version == MANIFEST_SCHEMA_VERSION:
        execution = manifest.get("execution")
        if not isinstance(execution, Mapping) or set(execution) != EXECUTION_KEYS:
            raise GuardError("manifest execution metadata is invalid")
        phase = execution.get("phase")
        result = execution.get("result")
        failure_code = execution.get("failureCode")
        terminal_at = execution.get("terminalAt")
        if phase not in EXECUTION_PHASES:
            raise GuardError("manifest execution phase is invalid")
        if result is not None and result not in TERMINAL_STATES:
            raise GuardError("manifest execution result is invalid")
        if failure_code is not None and (
            not isinstance(failure_code, str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", failure_code) is None
        ):
            raise GuardError("manifest execution failure code is invalid")
        if result is None:
            if failure_code is not None or terminal_at is not None:
                raise GuardError("nonterminal execution metadata contains a terminal value")
            if manifest.get("state") in TERMINAL_STATES:
                raise GuardError("terminal manifest has no execution result")
        else:
            if result != manifest.get("state") or not isinstance(terminal_at, str):
                raise GuardError("terminal execution metadata does not match manifest state")
            _parse_iso_utc(terminal_at)


def _manifest_runtime_pin(manifest: Mapping[str, Any]) -> dict[str, str]:
    runtime = manifest["runtime"]
    return {
        "path": runtime["codexExe"],
        "version": runtime["codexVersion"],
        "sha256": runtime["codexSha256"],
    }


def _log_path(manifest_path: Path, manifest: Mapping[str, Any]) -> Path:
    base = (
        manifest_path.parent.parent
        if manifest_path.parent.name.casefold() in {"config", "manifests"}
        else manifest_path.parent
    )
    return base / "logs" / f"{manifest['jobId']}.jsonl"


def _cancel_path(manifest_path: Path) -> Path:
    return manifest_path.with_suffix(manifest_path.suffix + ".cancel")


def _request_cancellation(manifest_path: Path, manifest: Mapping[str, Any]) -> None:
    _atomic_write_json(
        _cancel_path(manifest_path),
        {
            "schemaVersion": 1,
            "jobId": manifest.get("jobId"),
            "requestedAtUtc": _iso_utc(int(time.time())),
        },
    )


def _cancellation_requested(manifest_path: Path, manifest: Mapping[str, Any]) -> bool:
    path = _cancel_path(manifest_path)
    if not path.exists():
        return False
    try:
        marker = _load_json(path)
    except GuardError:
        return True
    return marker.get("jobId") == manifest.get("jobId")


def _log_event(manifest_path: Path, manifest: Mapping[str, Any], event: str, **fields: Any) -> None:
    payload = {
        "atUtc": _iso_utc(int(time.time())),
        "jobId": manifest.get("jobId"),
        "event": event,
        **fields,
    }
    rendered = redact_for_log(payload, secrets=(str(manifest.get("idempotencyKey", "")),))
    path = _log_path(manifest_path, manifest)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(rendered + "\n")


def _update_manifest_state(
    path: Path,
    manifest: dict[str, Any],
    state: str,
    *,
    armed: bool | None = None,
    phase: str | None = None,
    failure_code: str | None = None,
) -> None:
    manifest["state"] = state
    if armed is not None:
        manifest["armed"] = armed
    if manifest.get("schemaVersion") == MANIFEST_SCHEMA_VERSION:
        execution = manifest["execution"]
        if phase is not None:
            if phase not in EXECUTION_PHASES:
                raise GuardError("internal execution phase is invalid")
            execution["phase"] = phase
        if state in TERMINAL_STATES:
            execution["result"] = state
            execution["failureCode"] = failure_code
            execution["terminalAt"] = _iso_utc(int(time.time()))
        else:
            execution["result"] = None
            execution["failureCode"] = None
            execution["terminalAt"] = None
    _atomic_write_json(path, manifest)


class ManifestLock:
    def __init__(self, manifest_path: Path) -> None:
        self.path = manifest_path.with_suffix(manifest_path.suffix + ".lock")
        self.stream: Any = None

    def __enter__(self) -> "ManifestLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0, os.SEEK_END)
        if self.stream.tell() == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.stream.close()
            raise GuardError("another guard instance is already running") from error
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.stream is None:
            return
        with contextlib.suppress(OSError):
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()


def dispatch_lock_path(manifest_path: Path) -> Path:
    """Return the global live-dispatch lock path for an installation root."""
    resolved = manifest_path.resolve()
    base = (
        resolved.parent.parent
        if resolved.parent.name.casefold() in {"config", "manifests"}
        else resolved.parent
    )
    return base / "state" / "dispatch.lock"


class DispatchLock:
    """Cross-process lock shared by every manifest in one installation."""

    def __init__(self, manifest_path: Path) -> None:
        self.path = dispatch_lock_path(manifest_path)
        self.stream: Any = None

    def __enter__(self) -> "DispatchLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0, os.SEEK_END)
        if self.stream.tell() == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.stream.close()
            self.stream = None
            raise GuardError("another live guard is already dispatching") from error
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.stream is None:
            return
        with contextlib.suppress(OSError):
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()
        self.stream = None


class AppServerTransport:
    """Newline-delimited Codex app-server client with response-ID demux."""

    def __init__(
        self,
        exe: Path,
        codex_home: Path,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        *,
        command: Sequence[str] | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        self.exe = exe
        self.codex_home = codex_home
        self.request_timeout = request_timeout
        self.command = list(command) if command is not None else [str(exe), "app-server", "--stdio"]
        self.extra_env = dict(extra_env or {})
        self.process: subprocess.Popen[str] | None = None
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, queue.Queue[Any]] = {}
        self._expired_ids: set[int] = set()
        self._next_id = 1
        self._fatal: Exception | None = None
        self._stderr_tail: list[str] = []
        self.notifications_seen = 0

    def start(self) -> Mapping[str, Any]:
        if self.process is not None:
            raise GuardError("app-server is already running")
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.codex_home)
        env.update(self.extra_env)
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=_subprocess_creationflags(),
            )
        except OSError as error:
            raise TransportError("unable to start Codex app-server", after_write=False) from error
        threading.Thread(target=self._stdout_loop, name="codex-guard-stdout", daemon=True).start()
        threading.Thread(target=self._stderr_loop, name="codex-guard-stderr", daemon=True).start()
        initialized = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex-reset-credit-guard",
                    "title": "Codex Usage Limit Reset Guard",
                    "version": APP_VERSION,
                }
            },
        )
        if not isinstance(initialized, Mapping):
            raise ProtocolError("initialize result must be an object")
        if initialized.get("platformFamily") != "windows" or initialized.get("platformOs") != "windows":
            raise ProtocolError("app-server platform is not Windows")
        if os.path.normcase(os.path.normpath(str(initialized.get("codexHome")))) != os.path.normcase(
            os.path.normpath(str(self.codex_home))
        ):
            raise ProtocolError("app-server CODEX_HOME changed")
        self.notify("initialized")
        return initialized

    def _stdout_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as error:
                    self._set_fatal(ProtocolError("app-server emitted non-JSON stdout"))
                    return
                if not isinstance(message, dict):
                    self._set_fatal(ProtocolError("app-server emitted a non-object message"))
                    return
                if "id" in message and "method" not in message:
                    request_id = message.get("id")
                    if not _is_int(request_id):
                        self._set_fatal(ProtocolError("app-server response ID is invalid"))
                        return
                    with self._pending_lock:
                        response_queue = self._pending.get(request_id)
                        if response_queue is None and request_id in self._expired_ids:
                            self._expired_ids.discard(request_id)
                            continue
                    if response_queue is None:
                        self._set_fatal(ProtocolError("app-server returned an unsolicited response"))
                        return
                    response_queue.put(message)
                elif "method" in message and "id" not in message:
                    self.notifications_seen += 1
                else:
                    self._set_fatal(ProtocolError("unexpected app-server message shape"))
                    return
        except Exception as error:  # pragma: no cover - OS pipe failure
            self._set_fatal(ProtocolError("app-server stdout reader failed"))
        finally:
            if self.process is not None and self.process.poll() is not None:
                self._set_fatal(TransportError("app-server stdout closed", after_write=True))

    def _stderr_loop(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        for line in self.process.stderr:
            self._stderr_tail.append(line.rstrip())
            if len(self._stderr_tail) > 20:
                del self._stderr_tail[0]

    def _set_fatal(self, error: Exception) -> None:
        if self._fatal is None:
            self._fatal = error
        with self._pending_lock:
            pending = list(self._pending.values())
        for response_queue in pending:
            with contextlib.suppress(queue.Full):
                response_queue.put_nowait(error)

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None | _Omit = OMIT,
        *,
        timeout: float | None = None,
    ) -> Any:
        if self.process is None or self.process.stdin is None:
            raise TransportError("app-server is not running", after_write=False)
        if self._fatal is not None:
            raise TransportError("app-server transport is unhealthy", after_write=False)
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            response_queue: queue.Queue[Any] = queue.Queue(maxsize=2)
            self._pending[request_id] = response_queue
        message: dict[str, Any] = {"id": request_id, "method": method}
        if not isinstance(params, _Omit):
            message["params"] = params
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        write_started = False
        try:
            with self._write_lock:
                write_started = True
                self.process.stdin.write(encoded + "\n")
                self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise TransportError("app-server request write failed", after_write=write_started) from error
        try:
            response = response_queue.get(timeout=timeout or self.request_timeout)
        except queue.Empty as error:
            with self._pending_lock:
                self._pending.pop(request_id, None)
                self._expired_ids.add(request_id)
            raise TransportError("app-server request timed out", after_write=True) from error
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
        if isinstance(response, Exception):
            if isinstance(response, GuardError):
                raise response
            raise ProtocolError("app-server transport failed") from response
        if not isinstance(response, Mapping):
            raise ProtocolError("app-server response must be an object")
        if "error" in response:
            if set(response) != {"id", "error"}:
                raise ProtocolError("app-server RPC error response shape changed")
            raise RpcError(method, response.get("error"), after_write=True)
        if set(response) != {"id", "result"}:
            raise ProtocolError("app-server response shape changed")
        return response["result"]

    def notify(self, method: str, params: Mapping[str, Any] | None | _Omit = OMIT) -> None:
        if self.process is None or self.process.stdin is None:
            raise TransportError("app-server is not running", after_write=False)
        message: dict[str, Any] = {"method": method}
        if not isinstance(params, _Omit):
            message["params"] = params
        try:
            with self._write_lock:
                self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
                self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise TransportError("app-server notification write failed", after_write=True) from error

    def restart(self) -> Mapping[str, Any]:
        self.close()
        self._pending.clear()
        self._expired_ids.clear()
        self._fatal = None
        return self.start()

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.stdin is not None:
            with contextlib.suppress(OSError):
                process.stdin.close()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()

    def __enter__(self) -> "AppServerTransport":
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


def _read_account(transport: AppServerTransport) -> Mapping[str, Any]:
    result = transport.request("account/read", {"refreshToken": False})
    if not isinstance(result, Mapping):
        raise ProtocolError("account/read result must be an object")
    return result


def _read_rate_limits(transport: AppServerTransport) -> Mapping[str, Any]:
    result = transport.request("account/rateLimits/read")
    if not isinstance(result, Mapping):
        raise ProtocolError("account/rateLimits/read result must be an object")
    return result


def _consume_exact(
    transport: AppServerTransport, raw_credit_id: str, idempotency_key: str
) -> Mapping[str, Any]:
    params = build_consume_params(raw_credit_id, idempotency_key)
    result = transport.request(CONSUME_METHOD, params, timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS)
    if not isinstance(result, Mapping):
        raise ProtocolError("consume result must be an object")
    return result


def _find_native_codex(explicit: str | None = None) -> Path:
    candidate = explicit or os.environ.get("CODEX_RESET_GUARD_CODEX_PATH")
    if candidate:
        path = Path(candidate).expanduser().resolve()
        if not path.is_file():
            raise GuardError(f"Codex executable not found: {path}")
        return path
    roots: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "npm" / "node_modules" / "@openai" / "codex")
    for root in roots:
        if not root.is_dir():
            continue
        matches = [
            path
            for path in root.rglob("codex.exe")
            if "codex-win32" in str(path).casefold() and "vendor" in str(path).casefold()
        ]
        if len(matches) == 1:
            return matches[0].resolve()
        if len(matches) > 1:
            raise GuardError("multiple npm Codex native executables were found")
    raise GuardError("npm Codex native executable was not found")


def _codex_version(exe: Path) -> str:
    try:
        completed = subprocess.run(
            [str(exe), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            creationflags=_subprocess_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GuardError("unable to read Codex CLI version") from error
    version = completed.stdout.strip()
    if completed.returncode != 0 or not re.fullmatch(r"codex-cli \d+\.\d+\.\d+(?:[-.][A-Za-z0-9.]+)?", version):
        raise GuardError("Codex CLI version output is unexpected")
    return version


def _authenticode_subject(exe: Path) -> str:
    if os.name != "nt":
        raise GuardError("Authenticode verification requires Windows")
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        raise GuardError("PowerShell is required for Authenticode verification")
    env = os.environ.copy()
    env["CODEX_GUARD_VERIFY_PATH"] = str(exe)
    command = (
        "$s=Get-AuthenticodeSignature -LiteralPath $env:CODEX_GUARD_VERIFY_PATH;"
        "[pscustomobject]@{Status=[string]$s.Status;Subject=[string]$s.SignerCertificate.Subject}"
        "|ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        env=env,
        creationflags=_subprocess_creationflags(),
    )
    if completed.returncode != 0:
        raise GuardError("Authenticode verification failed")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise GuardError("Authenticode result was malformed") from error
    status = result.get("Status") if isinstance(result, dict) else None
    subject = result.get("Subject") if isinstance(result, dict) else None
    if status != "Valid" or not isinstance(subject, str):
        raise GuardError("Codex executable does not have a valid OpenAI signature")
    _validate_openai_publisher_subject(subject)
    return subject


def _validate_openai_publisher_subject(subject: str) -> None:
    official_names = r"(?:OpenAI OpCo, LLC|OpenAI, L\.L\.C\.)"
    if not isinstance(subject, str) or re.search(
        rf"(?:^|,\s*)(?:CN|O)\s*=\s*\"?{official_names}\"?(?=\s*,|$)",
        subject,
        re.IGNORECASE,
    ) is None:
        raise GuardError("Codex Authenticode publisher is not an approved OpenAI identity")


def _npm_package_version_for_native(exe: Path) -> str:
    resolved = exe.resolve()
    parts = resolved.parts
    folded = [part.casefold() for part in parts]
    package_root: Path | None = None
    for index in range(len(parts) - 2):
        if folded[index : index + 3] == ["node_modules", "@openai", "codex"]:
            package_root = Path(*parts[: index + 3])
            break
    if package_root is None:
        raise GuardError("Codex executable is not inside the global @openai/codex package")
    relative = str(resolved.relative_to(package_root)).replace("\\", "/")
    if re.fullmatch(
        r"node_modules/@openai/codex-win32-(?:x64|arm64)/vendor/.+/bin/codex\.exe",
        relative,
        re.IGNORECASE,
    ) is None:
        raise GuardError("Codex executable is not the npm package native binary")
    try:
        package = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GuardError("Codex npm package metadata could not be read") from error
    version = package.get("version") if isinstance(package, Mapping) else None
    if not isinstance(version, str) or re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
        raise GuardError("Codex npm package version is not a stable semantic version")
    return version


def _binary_info(exe: Path, *, verify_signature: bool = True) -> BinaryInfo:
    resolved = exe.resolve()
    return BinaryInfo(
        path=str(resolved),
        version=_codex_version(resolved),
        sha256=_sha256_file(resolved),
        signer_subject=_authenticode_subject(resolved) if verify_signature else None,
    )


def _validate_cli_schema(exe: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="codex-reset-guard-schema-") as directory:
        completed = subprocess.run(
            [str(exe), "app-server", "generate-json-schema", "--out", directory],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
            creationflags=_subprocess_creationflags(),
        )
        if completed.returncode != 0:
            raise GuardError("Codex app-server schema generation failed")
        base = Path(directory) / "v2"
        try:
            consume_params = json.loads(
                (base / "ConsumeAccountRateLimitResetCreditParams.json").read_text(encoding="utf-8")
            )
            consume_response = json.loads(
                (base / "ConsumeAccountRateLimitResetCreditResponse.json").read_text(encoding="utf-8")
            )
            rate_response = json.loads(
                (base / "GetAccountRateLimitsResponse.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            raise GuardError("Codex app-server schema bundle is incomplete") from error
        properties = consume_params.get("properties", {})
        required = consume_params.get("required", [])
        if "creditId" not in properties or "idempotencyKey" not in required:
            raise GuardError("Codex consume contract no longer supports exact creditId values")
        serialized_consume = json.dumps(consume_response, sort_keys=True)
        for outcome in ("reset", "nothingToReset", "noCredit", "alreadyRedeemed"):
            if outcome not in serialized_consume:
                raise GuardError("Codex consume outcomes changed")
        serialized_rates = json.dumps(rate_response, sort_keys=True)
        for field_name in ("RateLimitResetCredit", "availableCount", "credits", "expiresAt", "grantedAt"):
            if field_name not in serialized_rates:
                raise GuardError("Codex usage limit reset detail contract changed")


def _time_status() -> str:
    if os.name != "nt":
        raise GuardError("Windows Time verification requires Windows")
    completed = subprocess.run(
        ["w32tm", "/query", "/status"],
        capture_output=True,
        text=True,
        encoding=None,
        errors="replace",
        timeout=20,
        check=False,
        creationflags=_subprocess_creationflags(),
    )
    output = (completed.stdout + "\n" + completed.stderr).strip()
    lowered = output.casefold()
    first_status_line = next((line for line in output.splitlines() if line.strip()), "")
    rejected = (
        "local cmos clock",
        "free-running system clock",
        "unsynchronized",
        "not synchronized",
        "동기화되지 않음",
        "마지막으로 동기화한 시간: 지정되지 않음",
    )
    leap_match = re.search(r":\s*([0-3])(?:\D|$)", first_status_line)
    leap_unsynchronized = leap_match is not None and leap_match.group(1) == "3"
    if (
        completed.returncode != 0
        or leap_match is None
        or leap_unsynchronized
        or "time.windows.com" not in lowered
        or any(marker in lowered for marker in rejected)
    ):
        raise GuardError("Windows Time is not synchronized to a trusted source")
    return output


def _observe_pinned_binary(manifest: Mapping[str, Any]) -> dict[str, str]:
    runtime = manifest["runtime"]
    exe = Path(runtime["codexExe"])
    info = _binary_info(exe, verify_signature=False)
    return {"path": info.path, "version": info.version, "sha256": info.sha256}


def _safe_probe_rows(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for record in _credit_records(response):
        rows.append(
            {
                "creditHashPrefix": record.id_sha256[:12],
                "expiresAtUtc": _iso_utc(record.expires_at) if record.expires_at else None,
                "grantedAtUtc": _iso_utc(record.granted_at),
                "resetType": record.reset_type,
                "status": record.status,
            }
        )
    return rows


def _safe_compatibility_rows(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in _credit_records(response):
        rows.append(
            {
                "creditIdSha256": record.id_sha256,
                "expiresAt": record.expires_at,
                "expiresAtUtc": (
                    _iso_utc(record.expires_at) if record.expires_at is not None else None
                ),
                "processAtUtc": (
                    _iso_utc(record.expires_at - PROCESS_LEAD_SECONDS)
                    if record.expires_at is not None
                    else None
                ),
                "grantedAt": record.granted_at,
                "grantedAtUtc": _iso_utc(record.granted_at),
                "resetType": record.reset_type,
                "status": record.status,
            }
        )
    return rows


def _stable_codex_version(version_output: str) -> tuple[int, int, int]:
    match = re.fullmatch(
        r"codex-cli (0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)",
        version_output,
    )
    if match is None:
        raise GuardError("Codex CLI must be a stable semantic version")
    version = tuple(int(part) for part in match.groups())
    if version < MINIMUM_CODEX_VERSION:
        raise GuardError("Codex CLI is older than the minimum supported version")
    return version


def validate_cli_compatibility(
    codex_path: str | None = None,
    codex_home: Path | None = None,
    expected_account_email_sha256: str | None = None,
) -> dict[str, Any]:
    """Perform a complete read-only CLI/account compatibility check.

    This helper intentionally has no consume path. With ``codex_path`` omitted,
    discovery also requires exactly one native executable in the global npm
    installation.
    """
    exe = _find_native_codex(codex_path)
    home = (codex_home or _default_codex_home()).expanduser().resolve()
    package_version_before = _npm_package_version_for_native(exe)
    before = _binary_info(exe)
    _stable_codex_version(before.version)
    if before.version != f"codex-cli {package_version_before}":
        raise GuardError("Codex npm package and native binary versions differ")
    _validate_cli_schema(exe)
    with AppServerTransport(exe, home) as transport:
        account = _read_account(transport)
        identity = _account_identity(account)
        if expected_account_email_sha256 is not None:
            if re.fullmatch(r"[0-9a-f]{64}", expected_account_email_sha256) is None:
                raise GuardError("expected account hash is invalid")
            validate_account_pin(account, expected_account_email_sha256)
        rates = _read_rate_limits(transport)
        rows = _safe_compatibility_rows(rates)
    after = _binary_info(exe)
    package_version_after = _npm_package_version_for_native(exe)
    _stable_codex_version(after.version)
    if package_version_after != package_version_before:
        raise GuardError("Codex npm package version changed during validation")
    if after.version != f"codex-cli {package_version_after}":
        raise GuardError("Codex npm package and native binary versions differ")
    validate_binary_pin(
        {"path": before.path, "version": before.version, "sha256": before.sha256},
        {"path": after.path, "version": after.version, "sha256": after.sha256},
    )
    if before.signer_subject != after.signer_subject:
        raise GuardError("Codex executable signature changed during validation")
    return {
        "compatible": True,
        "binary": {
            "path": after.path,
            "version": after.version,
            "sha256": after.sha256,
            "signerSubject": after.signer_subject,
        },
        "accountEmailSha256": identity.email_sha256,
        "availableCount": len(rows),
        "credits": rows,
    }


def _new_transport(exe: Path, codex_home: Path) -> AppServerTransport:
    return AppServerTransport(exe, codex_home)


def _disable_task_best_effort(task_name: str | None) -> bool:
    if not task_name or os.name != "nt":
        return False
    completed = subprocess.run(
        ["schtasks", "/Change", "/TN", task_name, "/Disable"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=20,
        check=False,
        creationflags=_subprocess_creationflags(),
    )
    return completed.returncode == 0


def _validate_task_exists(task_name: str) -> None:
    if os.name != "nt":
        raise GuardError("Scheduled Tasks require Windows")
    completed = subprocess.run(
        ["schtasks", "/Query", "/TN", task_name],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=20,
        check=False,
        creationflags=_subprocess_creationflags(),
    )
    if completed.returncode != 0:
        raise GuardError("the one-shot Scheduled Task was not found")


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_one(parent: ET.Element, name: str) -> ET.Element:
    matches = [child for child in list(parent) if _xml_local_name(child.tag) == name]
    if len(matches) != 1:
        raise GuardError(f"Scheduled Task XML must contain exactly one {name}")
    return matches[0]


def _xml_text(parent: ET.Element, name: str) -> str:
    node = _xml_one(parent, name)
    if node.text is None or not node.text.strip():
        raise GuardError(f"Scheduled Task XML {name} is empty")
    return node.text.strip()


def _xml_optional_text(parent: ET.Element, name: str) -> str | None:
    matches = [child for child in list(parent) if _xml_local_name(child.tag) == name]
    if len(matches) > 1:
        raise GuardError(f"Scheduled Task XML contains multiple {name} elements")
    if not matches:
        return None
    if matches[0].text is None or not matches[0].text.strip():
        raise GuardError(f"Scheduled Task XML {name} is empty")
    return matches[0].text.strip()


def _parse_task_boundary(value: str) -> int:
    if not isinstance(value, str) or not re.search(r"(?:Z|[+-]\d{2}:\d{2})$", value):
        raise GuardError("Scheduled Task boundary has no explicit UTC offset")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise GuardError("Scheduled Task boundary is invalid") from error
    if parsed.tzinfo is None:
        raise GuardError("Scheduled Task boundary has no timezone")
    return int(parsed.astimezone(UTC).timestamp())


def _validate_scheduled_task_contract(
    task_name: str, manifest_path: Path, manifest: Mapping[str, Any]
) -> None:
    """Revalidate the installed one-shot task immediately before live work."""
    if os.name != "nt":
        raise GuardError("Scheduled Tasks require Windows")
    completed = subprocess.run(
        ["schtasks", "/Query", "/TN", task_name, "/XML"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=20,
        check=False,
        creationflags=_subprocess_creationflags(),
    )
    if completed.returncode != 0:
        raise GuardError("the one-shot Scheduled Task was not found")
    try:
        root = ET.fromstring(completed.stdout)
    except ET.ParseError as error:
        raise GuardError("Scheduled Task XML could not be parsed") from error
    if _xml_local_name(root.tag) != "Task":
        raise GuardError("Scheduled Task XML root changed")

    actions = _xml_one(root, "Actions")
    action = _xml_one(actions, "Exec")
    runner = Path(__file__).resolve()
    expected_command = Path(sys.executable).resolve()
    actual_command = Path(_xml_text(action, "Command")).resolve()
    if os.path.normcase(str(actual_command)) != os.path.normcase(str(expected_command)):
        raise GuardError("Scheduled Task Python executable changed")
    expected_arguments = (
        f'"{runner}" run --manifest "{manifest_path.resolve()}" --live'
    )
    if _xml_text(action, "Arguments") != expected_arguments:
        raise GuardError("Scheduled Task arguments changed")
    expected_working_directory = runner.parent.parent
    actual_working_directory = Path(
        _xml_text(action, "WorkingDirectory")
    ).resolve()
    if os.path.normcase(str(actual_working_directory)) != os.path.normcase(
        str(expected_working_directory)
    ):
        raise GuardError("Scheduled Task working directory changed")

    triggers = _xml_one(root, "Triggers")
    trigger = _xml_one(triggers, "TimeTrigger")
    start = _parse_task_boundary(_xml_text(trigger, "StartBoundary"))
    end = _parse_task_boundary(_xml_text(trigger, "EndBoundary"))
    if (
        start != _parse_iso_utc(manifest["schedule"]["triggerAtUtc"])
        or end != _parse_iso_utc(manifest["schedule"]["cutoffAtUtc"])
    ):
        raise GuardError("Scheduled Task time boundaries changed")

    principals = _xml_one(root, "Principals")
    principal = _xml_one(principals, "Principal")
    if _xml_text(principal, "LogonType") != "InteractiveToken":
        raise GuardError("Scheduled Task logon type changed")
    run_level = _xml_optional_text(principal, "RunLevel")
    if run_level not in {None, "LeastPrivilege"}:
        raise GuardError("Scheduled Task run level changed")

    settings = _xml_one(root, "Settings")
    required_settings = {
        "WakeToRun": "true",
        "StartWhenAvailable": "true",
        "AllowStartOnDemand": "false",
        "MultipleInstancesPolicy": "IgnoreNew",
        "DisallowStartIfOnBatteries": "false",
        "StopIfGoingOnBatteries": "false",
        "ExecutionTimeLimit": "PT10M",
    }
    for name, expected in required_settings.items():
        if _xml_text(settings, name) != expected:
            raise GuardError(f"Scheduled Task {name} changed")
    enabled = _xml_optional_text(settings, "Enabled")
    if enabled not in {None, "true"}:
        raise GuardError("Scheduled Task Enabled changed")


def _finish_cancelled(manifest_path: Path, manifest: dict[str, Any]) -> RunResult:
    _update_manifest_state(
        manifest_path,
        manifest,
        "DISARMED",
        armed=False,
        failure_code=FAILURE_USER_CANCELLED,
    )
    _log_event(manifest_path, manifest, "disarmed", reason="cancellation-requested")
    _disable_task_best_effort(manifest["task"]["name"])
    return RunResult("DISARMED", message="cancellation requested")


def _wait_until(
    epoch_seconds: int,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    now_func: Callable[[], float] = time.time,
) -> None:
    while True:
        remaining = epoch_seconds - now_func()
        if remaining <= 0:
            return
        sleeper(min(remaining, 30.0))


def _validate_live_context(
    manifest: Mapping[str, Any],
    account_response: Mapping[str, Any],
    rate_response: Mapping[str, Any],
) -> str:
    try:
        validate_account_pin(account_response, manifest["account"]["emailSha256"])
    except GuardError as error:
        raise SafetyCheckError("account", str(error)) from error
    try:
        return resolve_pinned_credit(rate_response, manifest["target"])
    except GuardError as error:
        raise SafetyCheckError("inventory", str(error)) from error


def _guard_failure_code(error: GuardError, *, post_dispatch: bool) -> str:
    if isinstance(error, SafetyCheckError):
        codes = {
            ("cli", False): FAILURE_PRE_DISPATCH_CLI,
            ("task", False): FAILURE_PRE_DISPATCH_TASK,
            ("time", False): FAILURE_PRE_DISPATCH_TIME,
            ("time", True): FAILURE_POST_DISPATCH_TIME,
            ("account", False): FAILURE_PRE_DISPATCH_ACCOUNT,
            ("account", True): FAILURE_POST_DISPATCH_ACCOUNT,
            ("inventory", False): FAILURE_PRE_DISPATCH_INVENTORY,
            ("inventory", True): FAILURE_POST_DISPATCH_INVENTORY,
        }
        return codes.get(
            (error.check, post_dispatch),
            FAILURE_POST_DISPATCH_UNCONFIRMED
            if post_dispatch
            else FAILURE_PRE_DISPATCH_GUARD,
        )
    if isinstance(error, TransportError):
        return (
            FAILURE_POST_DISPATCH_TRANSPORT
            if post_dispatch
            else FAILURE_PRE_DISPATCH_TRANSPORT
        )
    if isinstance(error, RpcError):
        return FAILURE_POST_DISPATCH_RPC if post_dispatch else FAILURE_PRE_DISPATCH_RPC
    if isinstance(error, ProtocolError):
        return (
            FAILURE_POST_DISPATCH_PROTOCOL
            if post_dispatch
            else FAILURE_PRE_DISPATCH_PROTOCOL
        )
    return (
        FAILURE_POST_DISPATCH_UNCONFIRMED
        if post_dispatch
        else FAILURE_PRE_DISPATCH_GUARD
    )


def _run_guard_impl(
    manifest_path: Path,
    *,
    live: bool,
    transport_factory: Callable[[Path, Path], AppServerTransport] = _new_transport,
    sleeper: Callable[[float], None] = time.sleep,
    now_func: Callable[[], float] = time.time,
    binary_observer: Callable[[Mapping[str, Any]], Mapping[str, str]] = _observe_pinned_binary,
    time_verifier: Callable[[], str] = _time_status,
    task_verifier: Callable[[str, Path, Mapping[str, Any]], None] = _validate_scheduled_task_contract,
) -> RunResult:
    manifest_path = manifest_path.resolve()
    with ManifestLock(manifest_path):
        manifest = _load_json(manifest_path)
        _validate_manifest(manifest)
        # v1 persisted a UUIDv4 in the manifest. New v2 jobs keep it only in
        # this live process: all retries reuse it, while a process/PC loss is
        # already crash-safely terminal and must never be resumed.
        idempotency_key = (
            str(manifest["idempotencyKey"])
            if manifest["schemaVersion"] == LEGACY_MANIFEST_SCHEMA_VERSION
            else str(uuid.uuid4())
        )
        try:
            observed = binary_observer(manifest)
            validate_binary_pin(_manifest_runtime_pin(manifest), observed)
        except GuardError as error:
            if live:
                raise SafetyCheckError("cli", str(error)) from error
            raise
        codex_home = Path(manifest["runtime"]["codexHome"])
        exe = Path(manifest["runtime"]["codexExe"])

        if live:
            if not manifest["armed"] or manifest["state"] != "ARMED":
                if manifest["state"] in {"DISPATCHING", "INDETERMINATE"}:
                    _update_manifest_state(
                        manifest_path,
                        manifest,
                        "INDETERMINATE",
                        armed=False,
                        phase="postDispatch",
                        failure_code=FAILURE_POST_DISPATCH_UNCONFIRMED,
                    )
                    return RunResult("INDETERMINATE", message="prior dispatch did not finish")
                raise GuardError("manifest is not armed for live execution")
            if _cancellation_requested(manifest_path, manifest):
                return _finish_cancelled(manifest_path, manifest)
            try:
                task_verifier(manifest["task"]["name"], manifest_path, manifest)
            except GuardError as error:
                raise SafetyCheckError("task", str(error)) from error
            try:
                time_verifier()
            except GuardError as error:
                raise SafetyCheckError("time", str(error)) from error
            wall_baseline = now_func()
            monotonic_baseline = time.monotonic()

            def verify_live_clock() -> None:
                try:
                    time_verifier()
                except GuardError as error:
                    raise SafetyCheckError("time", str(error)) from error
                if now_func is time.time and sleeper is time.sleep:
                    wall_elapsed = now_func() - wall_baseline
                    monotonic_elapsed = time.monotonic() - monotonic_baseline
                    if abs(wall_elapsed - monotonic_elapsed) > 2.0:
                        raise SafetyCheckError(
                            "time", "system clock changed during the live window"
                        )
            trigger = _parse_iso_utc(manifest["schedule"]["triggerAtUtc"])
            process_at = _parse_iso_utc(manifest["schedule"]["processAtUtc"])
            cutoff = _parse_iso_utc(manifest["schedule"]["cutoffAtUtc"])
            now = int(now_func())
            if now < trigger:
                raise GuardError("live execution started before the allowed trigger")
            if now >= cutoff:
                _update_manifest_state(
                    manifest_path,
                    manifest,
                    "NO_ACTION",
                    armed=False,
                    failure_code=FAILURE_PRE_DISPATCH_CUTOFF,
                )
                _log_event(manifest_path, manifest, "no-action", reason="cutoff-reached")
                _disable_task_best_effort(manifest["task"]["name"])
                return RunResult("NO_ACTION", message="cutoff reached")
        else:
            process_at = _parse_iso_utc(manifest["schedule"]["processAtUtc"])
            cutoff = _parse_iso_utc(manifest["schedule"]["cutoffAtUtc"])

            def verify_live_clock() -> None:
                return None

        transport = transport_factory(exe, codex_home)
        try:
            transport.start()
            account = _read_account(transport)
            rate_limits = _read_rate_limits(transport)
            raw_credit_id = _validate_live_context(manifest, account, rate_limits)
            if not live:
                _log_event(
                    manifest_path,
                    manifest,
                    "dry-run-ok",
                    targetHashPrefix=manifest["target"]["creditIdSha256"][:12],
                )
                return RunResult("DRY_RUN_OK", message="target and account validated")

            if now_func() < process_at:
                _wait_until(process_at, sleeper=sleeper, now_func=now_func)

            had_ambiguous = False
            while True:
                if _cancellation_requested(manifest_path, manifest):
                    return _finish_cancelled(manifest_path, manifest)
                if now_func() >= cutoff:
                    _update_manifest_state(
                        manifest_path,
                        manifest,
                        "NO_ACTION",
                        armed=False,
                        failure_code=FAILURE_PRE_DISPATCH_CUTOFF,
                    )
                    _log_event(manifest_path, manifest, "no-action", reason="nothing-to-reset-before-cutoff")
                    _disable_task_best_effort(manifest["task"]["name"])
                    return RunResult("NO_ACTION", outcome="nothingToReset")

                account = _read_account(transport)
                rate_limits = _read_rate_limits(transport)
                raw_credit_id = _validate_live_context(manifest, account, rate_limits)
                verify_live_clock()
                if _cancellation_requested(manifest_path, manifest):
                    return _finish_cancelled(manifest_path, manifest)
                if now_func() >= cutoff:
                    _update_manifest_state(
                        manifest_path,
                        manifest,
                        "NO_ACTION",
                        armed=False,
                        failure_code=FAILURE_PRE_DISPATCH_CUTOFF,
                    )
                    _log_event(manifest_path, manifest, "no-action", reason="cutoff-after-validation")
                    _disable_task_best_effort(manifest["task"]["name"])
                    return RunResult("NO_ACTION", message="cutoff reached before dispatch")
                # Persist the crash-safe terminal interpretation before the
                # raw ID leaves this process. A successful response may
                # replace it, but a process/PC loss leaves INDETERMINATE.
                _update_manifest_state(
                    manifest_path,
                    manifest,
                    "INDETERMINATE",
                    armed=False,
                    phase="postDispatch",
                    failure_code=FAILURE_POST_DISPATCH_UNCONFIRMED,
                )
                consume_result: Mapping[str, Any] | None = None
                indeterminate_reason = "unconfirmed-dispatch"
                indeterminate_failure_code = FAILURE_POST_DISPATCH_UNCONFIRMED
                try:
                    consume_result = _consume_exact(
                        transport, raw_credit_id, idempotency_key
                    )
                except TransportError as error:
                    classification = "ambiguous" if error.after_write else "indeterminate"
                    had_ambiguous = error.after_write
                    indeterminate_reason = "transport-error"
                    indeterminate_failure_code = FAILURE_POST_DISPATCH_TRANSPORT
                except RpcError as error:
                    had_ambiguous = _is_ambiguous_consume_timeout(error)
                    classification = "ambiguous" if had_ambiguous else "indeterminate"
                    indeterminate_reason = "rpc-error"
                    indeterminate_failure_code = FAILURE_POST_DISPATCH_RPC
                except ProtocolError:
                    classification = "indeterminate"
                    indeterminate_reason = "response-schema"
                    indeterminate_failure_code = FAILURE_POST_DISPATCH_PROTOCOL
                else:
                    try:
                        classification = classify_consume_result(
                            consume_result, had_ambiguous_transport=had_ambiguous
                        )
                    except GuardError:
                        classification = "indeterminate"
                        indeterminate_reason = "response-contract"
                        indeterminate_failure_code = FAILURE_POST_DISPATCH_RESPONSE

                if classification == "ambiguous":
                    resolved = False
                    for replay_index in range(MAX_AMBIGUOUS_REPLAYS):
                        action = transport_failure_action(
                            replay_index,
                            have_in_memory_credit_id=bool(raw_credit_id),
                            now=datetime.fromtimestamp(now_func(), UTC),
                            expires_at=datetime.fromtimestamp(
                                manifest["target"]["expiresAt"], UTC
                            ),
                        )
                        if action != "replay":
                            break
                        sleeper(2.0 if replay_index == 0 else 4.0)
                        try:
                            transport.restart()
                            replay_account = _read_account(transport)
                            try:
                                validate_account_pin(
                                    replay_account, manifest["account"]["emailSha256"]
                                )
                            except GuardError as error:
                                raise SafetyCheckError("account", str(error)) from error
                            replay_rates = _read_rate_limits(transport)
                            try:
                                _validate_replay_inventory(
                                    replay_rates, manifest["target"], raw_credit_id
                                )
                            except GuardError as error:
                                raise SafetyCheckError("inventory", str(error)) from error
                            verify_live_clock()
                            if (
                                _cancellation_requested(manifest_path, manifest)
                                or now_func() >= cutoff
                            ):
                                break
                            replay_result = _consume_exact(
                                transport, raw_credit_id, idempotency_key
                            )
                            replay_class = classify_consume_result(
                                replay_result, had_ambiguous_transport=True
                            )
                            if replay_class == "success":
                                consume_result = replay_result
                                classification = "success"
                                resolved = True
                                break
                            classification = "indeterminate"
                            indeterminate_failure_code = (
                                FAILURE_POST_DISPATCH_NO_CREDIT
                                if replay_result.get("outcome") == "noCredit"
                                else FAILURE_POST_DISPATCH_RESPONSE
                            )
                            break
                        except TransportError as error:
                            if error.after_write:
                                continue
                            break
                        except RpcError as error:
                            if _is_ambiguous_consume_timeout(error):
                                continue
                            break
                        except ProtocolError:
                            indeterminate_failure_code = FAILURE_POST_DISPATCH_PROTOCOL
                            break
                        except GuardError as error:
                            indeterminate_failure_code = _guard_failure_code(
                                error, post_dispatch=True
                            )
                            break
                    if not resolved and classification != "success":
                        _update_manifest_state(
                            manifest_path,
                            manifest,
                            "INDETERMINATE",
                            armed=False,
                            phase="postDispatch",
                            failure_code=indeterminate_failure_code,
                        )
                        _log_event(manifest_path, manifest, "indeterminate", reason="ambiguous-dispatch")
                        _disable_task_best_effort(manifest["task"]["name"])
                        return RunResult("INDETERMINATE", message="consume result is ambiguous")

                if classification == "success":
                    assert consume_result is not None
                    outcome = str(consume_result["outcome"])
                    _update_manifest_state(
                        manifest_path,
                        manifest,
                        "SUCCEEDED",
                        armed=False,
                        phase="postDispatch",
                    )
                    _log_event(manifest_path, manifest, "succeeded", outcome=outcome)
                    _disable_task_best_effort(manifest["task"]["name"])
                    return RunResult("SUCCEEDED", outcome=outcome)
                if classification == "abort":
                    _update_manifest_state(
                        manifest_path,
                        manifest,
                        "FAILED",
                        armed=False,
                        phase="postDispatch",
                        failure_code=FAILURE_POST_DISPATCH_NO_CREDIT,
                    )
                    _log_event(manifest_path, manifest, "failed", outcome="noCredit")
                    _disable_task_best_effort(manifest["task"]["name"])
                    return RunResult("FAILED", outcome="noCredit")
                if classification == "indeterminate":
                    _update_manifest_state(
                        manifest_path,
                        manifest,
                        "INDETERMINATE",
                        armed=False,
                        phase="postDispatch",
                        failure_code=indeterminate_failure_code,
                    )
                    _log_event(
                        manifest_path,
                        manifest,
                        "indeterminate",
                        reason=indeterminate_reason,
                    )
                    _disable_task_best_effort(manifest["task"]["name"])
                    return RunResult(
                        "INDETERMINATE", message="consume result could not be confirmed"
                    )
                if classification == "retry":
                    # The raw credit ID exists only in this process.  Keep a
                    # crash-safe terminal interpretation while sleeping so a
                    # process/PC loss can never leave a resumable-looking job
                    # that a later controller might recreate with a new key.
                    _update_manifest_state(
                        manifest_path,
                        manifest,
                        "INDETERMINATE",
                        armed=False,
                        phase="postDispatch",
                        failure_code=FAILURE_POST_DISPATCH_NOTHING,
                    )
                    retry_at = next_retry_at(
                        datetime.fromtimestamp(now_func(), UTC),
                        datetime.fromtimestamp(manifest["target"]["expiresAt"], UTC),
                    )
                    if retry_at is None:
                        _update_manifest_state(
                            manifest_path,
                            manifest,
                            "NO_ACTION",
                            armed=False,
                            phase="postDispatch",
                            failure_code=FAILURE_POST_DISPATCH_NOTHING,
                        )
                        _log_event(manifest_path, manifest, "no-action", reason="retry-cutoff")
                        _disable_task_best_effort(manifest["task"]["name"])
                        return RunResult("NO_ACTION", outcome="nothingToReset")
                    _wait_until(
                        int(retry_at.timestamp()), sleeper=sleeper, now_func=now_func
                    )
                    had_ambiguous = False
                    continue
                raise GuardError("internal consume state is invalid")
        except GuardError:
            if live and manifest.get("state") == "DISPATCHING":
                _update_manifest_state(
                    manifest_path,
                    manifest,
                    "INDETERMINATE",
                    armed=False,
                    phase="postDispatch",
                    failure_code=FAILURE_POST_DISPATCH_UNCONFIRMED,
                )
            raise
        finally:
            transport.close()


def _record_live_guard_failure(manifest_path: Path, error: GuardError) -> None:
    """Make an armed one-shot job terminal when a live safety check fails."""
    task_name: str | None = None
    try:
        with ManifestLock(manifest_path):
            manifest = _load_json(manifest_path)
            _validate_manifest(manifest)
            if manifest.get("state") not in {"ARMED", "WAITING", "DISPATCHING", "INDETERMINATE"}:
                return
            execution = manifest.get("execution")
            post_dispatch = manifest.get("state") in {"DISPATCHING", "INDETERMINATE"} or (
                isinstance(execution, Mapping)
                and execution.get("phase") == "postDispatch"
            )
            failure_code = _guard_failure_code(error, post_dispatch=post_dispatch)
            if manifest.get("state") != "INDETERMINATE" or (
                isinstance(execution, Mapping)
                and execution.get("failureCode") is None
            ):
                terminal_state = "INDETERMINATE" if post_dispatch else "FAILED"
                _update_manifest_state(
                    manifest_path,
                    manifest,
                    terminal_state,
                    armed=False,
                    phase="postDispatch" if post_dispatch else "preDispatch",
                    failure_code=failure_code,
                )
            task_name = manifest["task"]["name"]
            _log_event(
                manifest_path,
                manifest,
                "guard-error",
                failureType=type(error).__name__,
                failureCode=failure_code,
            )
    except GuardError:
        return
    _disable_task_best_effort(task_name)


def run_guard(
    manifest_path: Path,
    *,
    live: bool,
    transport_factory: Callable[[Path, Path], AppServerTransport] = _new_transport,
    sleeper: Callable[[float], None] = time.sleep,
    now_func: Callable[[], float] = time.time,
    binary_observer: Callable[[Mapping[str, Any]], Mapping[str, str]] = _observe_pinned_binary,
    time_verifier: Callable[[], str] = _time_status,
    task_verifier: Callable[[str, Path, Mapping[str, Any]], None] = _validate_scheduled_task_contract,
) -> RunResult:
    path = manifest_path.resolve()

    def execute() -> RunResult:
        try:
            return _run_guard_impl(
                path,
                live=live,
                transport_factory=transport_factory,
                sleeper=sleeper,
                now_func=now_func,
                binary_observer=binary_observer,
                time_verifier=time_verifier,
                task_verifier=task_verifier,
            )
        except GuardError as error:
            if live:
                _record_live_guard_failure(path, error)
            raise

    if live:
        # Lock ordering is a public invariant used by the manager:
        # controller.lock -> dispatch.lock -> manifest.lock.
        with DispatchLock(path):
            return execute()
    return execute()


def _probe(exe: Path, codex_home: Path) -> dict[str, Any]:
    _validate_cli_schema(exe)
    binary = _binary_info(exe)
    with AppServerTransport(exe, codex_home) as transport:
        account = _read_account(transport)
        identity = _account_identity(account)
        rates = _read_rate_limits(transport)
        earliest = select_unique_earliest_credit(rates)
        pin = make_target_pin(earliest)
        rows = _safe_probe_rows(rates)
    return {
        "binary": {
            "path": binary.path,
            "version": binary.version,
            "sha256": binary.sha256,
            "signerSubject": binary.signer_subject,
        },
        "accountEmailSha256": identity.email_sha256,
        "availableCount": len(rows),
        "credits": rows,
        "recommended": {
            "creditHashPrefix": pin["creditIdSha256"][:12],
            "expiresAtUtc": _iso_utc(pin["expiresAt"]),
            "processAtUtc": _iso_utc(pin["expiresAt"] - PROCESS_LEAD_SECONDS),
        },
    }


def _enroll_unlocked(
    exe: Path, codex_home: Path, manifest_path: Path, *, force: bool
) -> dict[str, Any]:
    if manifest_path.exists() and not force:
        raise GuardError(f"manifest already exists: {manifest_path}")
    _validate_cli_schema(exe)
    binary = _binary_info(exe)
    with AppServerTransport(exe, codex_home) as transport:
        identity = _account_identity(_read_account(transport))
        rates = _read_rate_limits(transport)
        selected = select_unique_earliest_credit(rates)
        target = make_target_pin(selected)
    expires = target["expiresAt"]
    manifest: dict[str, Any] = {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "jobId": str(uuid.uuid4()),
        "createdAtUtc": _iso_utc(int(time.time())),
        "armed": False,
        "state": "UNARMED",
        "target": target,
        "account": {"emailSha256": identity.email_sha256},
        "runtime": {
            "codexHome": str(codex_home.resolve()),
            "codexExe": binary.path,
            "codexVersion": binary.version,
            "codexSha256": binary.sha256,
            "signerSubject": binary.signer_subject,
        },
        "schedule": {
            "triggerAtUtc": _iso_utc(expires - TASK_START_LEAD_SECONDS),
            "processAtUtc": _iso_utc(expires - PROCESS_LEAD_SECONDS),
            "cutoffAtUtc": _iso_utc(expires - CUTOFF_LEAD_SECONDS),
            "expiresAtUtc": _iso_utc(expires),
        },
        "task": {"name": None},
        "execution": {
            "phase": "preDispatch",
            "result": None,
            "failureCode": None,
            "terminalAt": None,
        },
    }
    _validate_manifest(manifest)
    _atomic_write_json(manifest_path, manifest)
    _log_event(
        manifest_path,
        manifest,
        "enrolled",
        targetHashPrefix=target["creditIdSha256"][:12],
        expiresAtUtc=manifest["schedule"]["expiresAtUtc"],
    )
    return manifest


def _enroll(
    exe: Path, codex_home: Path, manifest_path: Path, *, force: bool
) -> dict[str, Any]:
    with ManifestLock(manifest_path):
        manifest = _enroll_unlocked(
            exe, codex_home, manifest_path, force=force
        )
        with contextlib.suppress(FileNotFoundError):
            _cancel_path(manifest_path).unlink()
        return manifest


def _arm_unlocked(manifest_path: Path, task_name: str) -> dict[str, Any]:
    manifest = _load_json(manifest_path)
    _validate_manifest(manifest)
    if manifest["armed"] or manifest["state"] != "UNARMED":
        raise GuardError("manifest is not in the unarmed enrollment state")
    if not isinstance(task_name, str) or not task_name.strip():
        raise GuardError("Scheduled Task name is required")
    trigger = _parse_iso_utc(manifest["schedule"]["triggerAtUtc"])
    if time.time() > trigger - ARM_MINIMUM_MARGIN_SECONDS:
        raise GuardError("less than ten minutes remain before the task trigger")
    _time_status()
    observed = _observe_pinned_binary(manifest)
    validate_binary_pin(_manifest_runtime_pin(manifest), observed)
    _validate_task_exists(task_name)
    with AppServerTransport(
        Path(manifest["runtime"]["codexExe"]), Path(manifest["runtime"]["codexHome"])
    ) as transport:
        account = _read_account(transport)
        rates = _read_rate_limits(transport)
        _validate_live_context(manifest, account, rates)
    manifest["task"]["name"] = task_name
    _update_manifest_state(manifest_path, manifest, "ARMED", armed=True)
    _log_event(manifest_path, manifest, "armed", taskName=task_name)
    return manifest


def _arm(manifest_path: Path, task_name: str) -> dict[str, Any]:
    with ManifestLock(manifest_path):
        if _cancel_path(manifest_path).exists():
            raise GuardError("manifest has a cancellation request")
        return _arm_unlocked(manifest_path, task_name)


def _disarm(manifest_path: Path) -> tuple[dict[str, Any], bool]:
    """Request cancellation, returning (manifest, still_running)."""
    initial = _load_json(manifest_path)
    _validate_manifest(initial)
    _request_cancellation(manifest_path, initial)
    _disable_task_best_effort(initial["task"]["name"])
    try:
        with ManifestLock(manifest_path):
            manifest = _load_json(manifest_path)
            _validate_manifest(manifest)
            if manifest["state"] not in TERMINAL_STATES:
                _update_manifest_state(
                    manifest_path, manifest, "DISARMED", armed=False
                )
                _log_event(
                    manifest_path,
                    manifest,
                    "disarmed",
                    reason="user-requested",
                )
            return manifest, False
    except GuardError as error:
        if str(error) != "another guard instance is already running":
            raise
        return initial, True


def _status_view(manifest: Mapping[str, Any]) -> dict[str, Any]:
    view = {
        "jobId": manifest["jobId"],
        "armed": manifest["armed"],
        "state": manifest["state"],
        "targetHashPrefix": manifest["target"]["creditIdSha256"][:12],
        "schedule": manifest["schedule"],
        "codexVersion": manifest["runtime"]["codexVersion"],
        "taskName": manifest["task"]["name"],
    }
    if manifest.get("schemaVersion") == MANIFEST_SCHEMA_VERSION:
        view["execution"] = dict(manifest["execution"])
    return view


def _print_line_if_available(value: str, stream: Any | None) -> None:
    if stream is not None:
        print(value, file=stream)


def _print_json(value: object) -> None:
    _print_line_if_available(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        sys.stdout,
    )


def _print_error(error: GuardError) -> None:
    _print_line_if_available(
        f"error: {redact_for_log(str(error))}",
        sys.stderr,
    )


def _default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fail-closed Codex usage-limit-reset guard")
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_runtime_options(command: argparse.ArgumentParser) -> None:
        command.add_argument("--codex-path", help="absolute npm native codex.exe path")
        command.add_argument("--codex-home", type=Path, default=_default_codex_home())

    probe = subparsers.add_parser("probe", help="read-only account and usage-limit-reset probe")
    add_runtime_options(probe)
    probe.add_argument("--json", action="store_true", help="emit sanitized JSON")

    enroll = subparsers.add_parser("enroll", help="pin the unique earliest usage limit reset")
    add_runtime_options(enroll)
    enroll.add_argument("--earliest", action="store_true", required=True)
    enroll.add_argument("--manifest", type=Path, required=True)
    enroll.add_argument("--force", action="store_true")

    run = subparsers.add_parser("run", help="validate or execute an enrolled job")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--live", action="store_true")

    arm = subparsers.add_parser("arm", help="arm a registered one-shot task")
    arm.add_argument("--manifest", type=Path, required=True)
    arm.add_argument("--task-name", required=True)
    arm.add_argument("--codex-path", help=argparse.SUPPRESS)

    status = subparsers.add_parser("status", help="show sanitized job status")
    status.add_argument("--manifest", type=Path, required=True)

    disarm = subparsers.add_parser("disarm", help="disarm and disable a job")
    disarm.add_argument("--manifest", type=Path, required=True)

    cleanup = subparsers.add_parser("cleanup", help="delete the scheduled task")
    cleanup.add_argument("--manifest", type=Path, required=True)
    cleanup.add_argument("--purge", action="store_true", help="also delete manifest and log")

    revalidate = subparsers.add_parser(
        "revalidate-cli", help="read-only validation after a Codex CLI update"
    )
    add_runtime_options(revalidate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "probe":
            exe = _find_native_codex(args.codex_path)
            result = _probe(exe, args.codex_home)
            _print_json(result)
            return 0
        if args.command == "enroll":
            exe = _find_native_codex(args.codex_path)
            manifest = _enroll(
                exe, args.codex_home, args.manifest.resolve(), force=args.force
            )
            _print_json(_status_view(manifest))
            return 0
        if args.command == "run":
            result = run_guard(args.manifest, live=args.live)
            _print_json({"state": result.state, "outcome": result.outcome, "message": result.message})
            return 0 if result.state in {"DRY_RUN_OK", "SUCCEEDED", "NO_ACTION", "DISARMED"} else 2
        if args.command == "arm":
            manifest = _arm(args.manifest.resolve(), args.task_name)
            _print_json(_status_view(manifest))
            return 0
        if args.command == "status":
            manifest = _load_json(args.manifest.resolve())
            _validate_manifest(manifest)
            _print_json(_status_view(manifest))
            return 0
        if args.command == "disarm":
            path = args.manifest.resolve()
            manifest, still_running = _disarm(path)
            view = _status_view(manifest)
            view["cancellationRequested"] = True
            if still_running:
                view["state"] = "CANCEL_REQUESTED"
            _print_json(view)
            return 0
        if args.command == "cleanup":
            path = args.manifest.resolve()
            initial = _load_json(path)
            _validate_manifest(initial)
            _request_cancellation(path, initial)
            _disable_task_best_effort(initial["task"]["name"])
            try:
                lock = ManifestLock(path)
                lock.__enter__()
            except GuardError as error:
                if str(error) == "another guard instance is already running":
                    raise GuardError(
                        "cancellation was requested; rerun cleanup after the live guard exits"
                    ) from error
                raise
            try:
                manifest = _load_json(path)
                _validate_manifest(manifest)
                task_name = manifest["task"]["name"]
                if task_name:
                    subprocess.run(
                        ["schtasks", "/Delete", "/TN", task_name, "/F"],
                        capture_output=True,
                        text=True,
                        errors="replace",
                        timeout=20,
                        check=False,
                        creationflags=_subprocess_creationflags(),
                    )
                _update_manifest_state(path, manifest, "CLEANED", armed=False)
                _log_event(path, manifest, "cleaned", purge=bool(args.purge))
                log_path = _log_path(path, manifest)
            finally:
                lock.__exit__(None, None, None)
            if args.purge:
                with contextlib.suppress(FileNotFoundError):
                    log_path.unlink()
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                with contextlib.suppress(FileNotFoundError):
                    _cancel_path(path).unlink()
                with contextlib.suppress(FileNotFoundError):
                    path.with_suffix(path.suffix + ".lock").unlink()
            else:
                _print_json(_status_view(manifest))
            return 0
        if args.command == "revalidate-cli":
            exe = _find_native_codex(args.codex_path)
            result = _probe(exe, args.codex_home)
            _print_json(
                {
                    "compatible": True,
                    "binary": result["binary"],
                    "availableCount": result["availableCount"],
                }
            )
            return 0
        raise GuardError("unknown command")
    except GuardError as error:
        _print_error(error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
