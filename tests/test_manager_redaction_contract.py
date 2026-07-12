"""Fail-closed manager ingestion for fields copied into user-visible state."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_reset_manager import ManagerError, _read_job
from tests.test_manager import NOW, credit, write_manifest


class ManagerRedactionContractTests(unittest.TestCase):
    def test_arbitrary_terminal_failure_text_is_rejected_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = write_manifest(
                root,
                credit("opaque-test-credit", NOW + 5_000),
                schema=2,
                state="INDETERMINATE",
                failure_code="private.user@example.test",
            )

            with self.assertRaises(ManagerError) as caught:
                _read_job(path)

        self.assertEqual(caught.exception.code, "MANIFEST_INVALID")

    def test_arbitrary_terminal_timestamp_text_is_rejected_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = write_manifest(
                root,
                credit("opaque-test-credit", NOW + 5_000),
                schema=2,
                state="INDETERMINATE",
                failure_code="POST_DISPATCH_UNCONFIRMED",
            )
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["execution"]["terminalAt"] = "private.user@example.test"
            path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaises(ManagerError) as caught:
                _read_job(path)

        self.assertEqual(caught.exception.code, "MANIFEST_INVALID")


if __name__ == "__main__":
    unittest.main()
