"""Guard child-process console-hiding contract tests."""

from __future__ import annotations

import ast
import io
import json
import unittest
from pathlib import Path
from unittest import mock

import codex_reset_guard as guard


class GuardSubprocessPolicyTests(unittest.TestCase):
    def test_windows_uses_create_no_window(self) -> None:
        expected = 0x08000000
        with (
            mock.patch.object(guard.os, "name", "nt"),
            mock.patch.object(
                guard.subprocess, "CREATE_NO_WINDOW", expected, create=True
            ),
        ):
            self.assertEqual(guard._subprocess_creationflags(), expected)

    def test_non_windows_uses_zero(self) -> None:
        with mock.patch.object(guard.os, "name", "posix"):
            self.assertEqual(guard._subprocess_creationflags(), 0)

    def test_every_guard_subprocess_call_uses_common_policy(self) -> None:
        source_path = Path(guard.__file__).resolve()
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        calls: list[ast.Call] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "subprocess"
                and function.attr in {"run", "Popen"}
            ):
                calls.append(node)

        self.assertGreater(len(calls), 0)
        for call in calls:
            with self.subTest(line=call.lineno):
                keyword = next(
                    (item for item in call.keywords if item.arg == "creationflags"),
                    None,
                )
                self.assertIsNotNone(keyword)
                self.assertIsInstance(keyword.value, ast.Call)
                self.assertIsInstance(keyword.value.func, ast.Name)
                self.assertEqual(
                    keyword.value.func.id,
                    "_subprocess_creationflags",
                )
                self.assertEqual(keyword.value.args, [])
                self.assertEqual(keyword.value.keywords, [])


class GuardOutputPolicyTests(unittest.TestCase):
    def test_scheduled_run_succeeds_without_standard_streams(self) -> None:
        result = guard.RunResult("SUCCEEDED", outcome="reset")
        with (
            mock.patch.object(guard, "run_guard", return_value=result),
            mock.patch.object(guard.sys, "stdout", None),
            mock.patch.object(guard.sys, "stderr", None),
        ):
            exit_code = guard.main(
                ["run", "--manifest", "scheduled-job.json", "--live"]
            )

        self.assertEqual(exit_code, 0)

    def test_guard_error_keeps_exit_code_without_standard_streams(self) -> None:
        with (
            mock.patch.object(
                guard,
                "run_guard",
                side_effect=guard.GuardError("scheduled failure"),
            ),
            mock.patch.object(guard.sys, "stdout", None),
            mock.patch.object(guard.sys, "stderr", None),
        ):
            exit_code = guard.main(
                ["run", "--manifest", "scheduled-job.json", "--live"]
            )

        self.assertEqual(exit_code, 1)

    def test_cli_json_output_is_unchanged_when_stdout_exists(self) -> None:
        output = io.StringIO()
        value = {"state": "SUCCEEDED", "outcome": "reset"}
        with mock.patch.object(guard.sys, "stdout", output):
            guard._print_json(value)

        self.assertEqual(
            output.getvalue(),
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    def test_cli_error_output_is_unchanged_when_stderr_exists(self) -> None:
        output = io.StringIO()
        with mock.patch.object(guard.sys, "stderr", output):
            guard._print_error(guard.GuardError("scheduled failure"))

        self.assertEqual(output.getvalue(), "error: scheduled failure\n")


if __name__ == "__main__":
    unittest.main()
