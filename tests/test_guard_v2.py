"""Manifest-v2 locking and read-only compatibility checks."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import codex_reset_guard as guard


RAW_CREDIT_ID = "compatibility-only-credit"
ACCOUNT_EMAIL = "compatibility@example.com"


def account_response() -> dict[str, object]:
    return {
        "account": {
            "type": "chatgpt",
            "email": ACCOUNT_EMAIL,
            "planType": "plus",
        }
    }


def rate_response() -> dict[str, object]:
    return {
        "rateLimitResetCredits": {
            "availableCount": 1,
            "credits": [
                {
                    "id": RAW_CREDIT_ID,
                    "expiresAt": 2_000_000_100,
                    "grantedAt": 1_900_000_000,
                    "resetType": "codexRateLimits",
                    "status": "available",
                    "title": None,
                    "description": None,
                }
            ],
        }
    }


class ReadOnlyTransport:
    def __init__(self) -> None:
        self.methods: list[str] = []

    def __enter__(self) -> "ReadOnlyTransport":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def request(self, method: str, _params: object = None) -> dict[str, object]:
        self.methods.append(method)
        if method == "account/read":
            return account_response()
        if method == "account/rateLimits/read":
            return rate_response()
        raise AssertionError(f"unexpected method: {method}")


class DispatchLockTests(unittest.TestCase):
    def test_all_manifests_under_one_root_share_the_dispatch_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "manifests" / "first.json"
            second = root / "manifests" / "second.json"
            self.assertEqual(
                guard.dispatch_lock_path(first), root / "state" / "dispatch.lock"
            )
            self.assertEqual(
                guard.dispatch_lock_path(first), guard.dispatch_lock_path(second)
            )
            with guard.DispatchLock(first):
                with self.assertRaisesRegex(
                    guard.GuardError, "another live guard is already dispatching"
                ):
                    with guard.DispatchLock(second):
                        self.fail("the shared lock must not be acquired twice")


class ManifestV2Tests(unittest.TestCase):
    def test_new_manifest_never_persists_idempotency_key(self) -> None:
        transport = ReadOnlyTransport()
        binary = guard.BinaryInfo(
            path=r"C:\npm\@openai\codex\vendor\codex.exe",
            version="codex-cli 0.144.1",
            sha256="a" * 64,
            signer_subject="CN=OpenAI, L.L.C.",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifests" / "job.json"
            with (
                mock.patch.object(guard, "_validate_cli_schema"),
                mock.patch.object(guard, "_binary_info", return_value=binary),
                mock.patch.object(guard, "AppServerTransport", return_value=transport),
            ):
                manifest = guard._enroll_unlocked(
                    Path(binary.path), Path(r"C:\fake-codex-home"), path, force=False
                )

            self.assertEqual(manifest["schemaVersion"], 2)
            self.assertNotIn("idempotencyKey", manifest)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("idempotencyKey", stored)
            guard._validate_manifest(stored)

            stored["idempotencyKey"] = "4a77335a-4da5-4d0e-a5f6-930d92bc276f"
            with self.assertRaisesRegex(guard.GuardError, "manifest shape changed"):
                guard._validate_manifest(stored)


class CompatibilityTests(unittest.TestCase):
    def binary(self, *, sha256: str = "a" * 64) -> guard.BinaryInfo:
        return guard.BinaryInfo(
            path=r"C:\npm\@openai\codex\vendor\codex.exe",
            version="codex-cli 0.144.1",
            sha256=sha256,
            signer_subject="CN=OpenAI, L.L.C.",
        )

    def test_helper_is_read_only_sanitized_and_rehashes_binary(self) -> None:
        transport = ReadOnlyTransport()
        binary = self.binary()
        with (
            mock.patch.object(guard, "_find_native_codex", return_value=Path(binary.path)),
            mock.patch.object(
                guard, "_npm_package_version_for_native", return_value="0.144.1"
            ),
            mock.patch.object(guard, "_binary_info", side_effect=[binary, binary]) as info,
            mock.patch.object(guard, "_validate_cli_schema") as schema,
            mock.patch.object(guard, "AppServerTransport", return_value=transport),
        ):
            result = guard.validate_cli_compatibility(
                codex_home=Path(r"C:\fake-codex-home"),
                expected_account_email_sha256=guard.hash_account_email(ACCOUNT_EMAIL),
            )

        self.assertEqual(info.call_count, 2)
        schema.assert_called_once()
        self.assertEqual(
            transport.methods, ["account/read", "account/rateLimits/read"]
        )
        row = result["credits"][0]
        self.assertEqual(row["creditIdSha256"], guard.sha256_text(RAW_CREDIT_ID))
        self.assertEqual(row["expiresAt"], 2_000_000_100)
        self.assertEqual(row["grantedAt"], 1_900_000_000)
        self.assertNotIn(RAW_CREDIT_ID, json.dumps(result, sort_keys=True))

    def test_helper_rejects_binary_change_during_read_only_validation(self) -> None:
        transport = ReadOnlyTransport()
        with (
            mock.patch.object(guard, "_find_native_codex", return_value=Path("codex.exe")),
            mock.patch.object(
                guard, "_npm_package_version_for_native", return_value="0.144.1"
            ),
            mock.patch.object(
                guard,
                "_binary_info",
                side_effect=[self.binary(sha256="a" * 64), self.binary(sha256="b" * 64)],
            ),
            mock.patch.object(guard, "_validate_cli_schema"),
            mock.patch.object(guard, "AppServerTransport", return_value=transport),
        ):
            with self.assertRaisesRegex(guard.GuardError, "executable hash changed"):
                guard.validate_cli_compatibility(codex_home=Path("."))

    def test_stable_minimum_semver_is_enforced(self) -> None:
        self.assertEqual(guard._stable_codex_version("codex-cli 0.144.1"), (0, 144, 1))
        with self.assertRaises(guard.GuardError):
            guard._stable_codex_version("codex-cli 0.144.0")
        with self.assertRaises(guard.GuardError):
            guard._stable_codex_version("codex-cli 0.145.0-beta.1")

    def test_authenticode_publisher_is_exact_not_substring_based(self) -> None:
        guard._validate_openai_publisher_subject(
            'CN="OpenAI OpCo, LLC", O="OpenAI OpCo, LLC", C=US'
        )
        guard._validate_openai_publisher_subject("CN=OpenAI, L.L.C.")
        with self.assertRaises(guard.GuardError):
            guard._validate_openai_publisher_subject("CN=Not OpenAI Support LLC")


if __name__ == "__main__":
    unittest.main()
