"""Manager subprocess visibility and Scheduled Task migration tests."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import codex_reset_manager as manager


class WindowlessSubprocessTests(unittest.TestCase):
    def test_platform_policy_uses_create_no_window_only_on_windows(self) -> None:
        expected = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

        self.assertEqual(manager.WINDOWLESS_SUBPROCESS_FLAGS, expected)

    def test_every_direct_subprocess_call_uses_common_windowless_policy(self) -> None:
        source = Path(manager.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.func.attr in {"run", "Popen"}
        ]

        self.assertTrue(calls)
        for call in calls:
            with self.subTest(line=call.lineno, function=call.func.attr):
                creationflags = next(
                    (item.value for item in call.keywords if item.arg == "creationflags"),
                    None,
                )
                self.assertIsInstance(creationflags, ast.Name)
                self.assertEqual(
                    creationflags.id,
                    "WINDOWLESS_SUBPROCESS_FLAGS",
                )


class ManagerTaskPythonContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.runner = self.root / "runners" / "codex_reset_guard.py"
        self.runner.parent.mkdir(parents=True)
        self.runner.write_text("# immutable test runner\n", encoding="utf-8")
        self.manifest = self.root / "manifests" / "job.json"
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text("{}\n", encoding="utf-8")
        self.python_dir = self.root / "python"
        self.python_dir.mkdir()
        self.python = self.python_dir / "python.exe"
        self.pythonw = self.python_dir / "pythonw.exe"
        self.python.write_bytes(b"")
        self.pythonw.write_bytes(b"")
        self.job = SimpleNamespace(
            task_name=r"\CodexResetCredit\Consume-test",
            path=self.manifest,
        )
        self.services = manager.RealServices(self.root)

    def task_xml(self, command: Path) -> str:
        root = ET.Element("Task")
        actions = ET.SubElement(root, "Actions")
        execute = ET.SubElement(actions, "Exec")
        ET.SubElement(execute, "Command").text = str(command.resolve())
        ET.SubElement(execute, "Arguments").text = (
            f'"{self.runner.resolve()}" run --manifest '
            f'"{self.manifest.resolve()}" --live'
        )
        return ET.tostring(root, encoding="unicode")

    def validate(self, command: Path) -> mock.Mock:
        query = SimpleNamespace(
            returncode=0,
            stdout=self.task_xml(command),
            stderr="",
        )
        child = SimpleNamespace(returncode=0, stdout="", stderr="")
        patched = mock.patch.object(
            manager.subprocess,
            "run",
            side_effect=[query, child],
        )
        run = patched.start()
        self.addCleanup(patched.stop)
        with mock.patch.object(manager.sys, "executable", str(self.python)):
            self.services.validate_task(self.job)
        return run

    def test_legacy_python_task_is_accepted(self) -> None:
        run = self.validate(self.python)

        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0][0], str(self.python.resolve()))
        self.assertTrue(
            all(
                call.kwargs["creationflags"]
                == manager.WINDOWLESS_SUBPROCESS_FLAGS
                for call in run.call_args_list
            )
        )

    def test_windowless_python_task_is_accepted(self) -> None:
        run = self.validate(self.pythonw)

        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0][0], str(self.pythonw.resolve()))

    def test_other_executable_in_python_directory_is_rejected(self) -> None:
        other = self.python_dir / "py.exe"
        other.write_bytes(b"")
        query = SimpleNamespace(
            returncode=0,
            stdout=self.task_xml(other),
            stderr="",
        )

        with (
            mock.patch.object(manager.sys, "executable", str(self.python)),
            mock.patch.object(manager.subprocess, "run", return_value=query) as run,
            self.assertRaisesRegex(manager.ManagerError, "TASK_CONTRACT_INVALID"),
        ):
            self.services.validate_task(self.job)

        run.assert_called_once()

    def test_python_executable_from_another_directory_is_rejected(self) -> None:
        other_dir = self.root / "other-python"
        other_dir.mkdir()
        other = other_dir / "pythonw.exe"
        other.write_bytes(b"")
        query = SimpleNamespace(
            returncode=0,
            stdout=self.task_xml(other),
            stderr="",
        )

        with (
            mock.patch.object(manager.sys, "executable", str(self.python)),
            mock.patch.object(manager.subprocess, "run", return_value=query) as run,
            self.assertRaisesRegex(manager.ManagerError, "TASK_CONTRACT_INVALID"),
        ):
            self.services.validate_task(self.job)

        run.assert_called_once()


@unittest.skipUnless(os.name == "nt", "Windows child-installer contract")
class ManagerChildPythonContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        environment = mock.patch.dict(manager.os.environ, {"PATH": "test-path"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.root = Path(self.temporary.name).resolve()
        self.runtime_dir = self.root / "python"
        self.runtime_dir.mkdir()
        self.python = self.runtime_dir / "python.exe"
        self.pythonw = self.runtime_dir / "pythonw.exe"
        self.python.write_bytes(b"")
        self.pythonw.write_bytes(b"")
        self.installer = self.root / "install.ps1"
        self.installer.write_text("# test installer\n", encoding="utf-8")
        self.services = manager.RealServices(self.root)

    def invoke(self, executable: Path) -> mock.Mock:
        completed = SimpleNamespace(
            returncode=0,
            stdout='{"jobId":"test-job"}\n',
            stderr="",
        )
        with (
            mock.patch.object(manager.sys, "executable", str(executable)),
            mock.patch.object(manager.shutil, "which", return_value=r"C:\Program Files\PowerShell\7\pwsh.exe"),
            mock.patch.object(manager.subprocess, "run", return_value=completed) as run,
        ):
            result = self.services.create_child(
                self.installer,
                r"C:\codex\codex.exe",
                None,
            )
        self.assertEqual(result, {"jobId": "test-job"})
        return run

    def assert_exact_console_python(self, run: mock.Mock) -> None:
        command = run.call_args.args[0]
        self.assertEqual(command.count("-PythonPath"), 1)
        index = command.index("-PythonPath")
        self.assertEqual(command[index + 1], str(self.python.resolve()))
        self.assertEqual(
            run.call_args.kwargs["creationflags"],
            manager.WINDOWLESS_SUBPROCESS_FLAGS,
        )
        self.assertEqual(run.call_args.kwargs["env"], {"PATH": "test-path"})

    def test_windowless_manager_passes_exact_sibling_console_python(self) -> None:
        self.assert_exact_console_python(self.invoke(self.pythonw))

    def test_console_manager_passes_its_exact_console_python(self) -> None:
        self.assert_exact_console_python(self.invoke(self.python))

    def test_missing_sibling_pythonw_is_rejected_before_child_process(self) -> None:
        self.pythonw.unlink()

        with (
            mock.patch.object(manager.sys, "executable", str(self.python)),
            mock.patch.object(manager.shutil, "which", return_value="pwsh"),
            mock.patch.object(manager.subprocess, "run") as run,
            self.assertRaisesRegex(manager.ManagerError, "CHILD_INSTALL_FAILED"),
        ):
            self.services.create_child(self.installer, r"C:\codex\codex.exe", None)

        run.assert_not_called()

    def test_resolved_sibling_outside_runtime_directory_is_rejected(self) -> None:
        other_dir = self.root / "other-python"
        other_dir.mkdir()
        other_pythonw = other_dir / "pythonw.exe"
        other_pythonw.write_bytes(b"")
        path_type = type(self.python)
        original_resolve = path_type.resolve

        def resolve(path: Path, strict: bool = False) -> Path:
            if path == self.pythonw:
                return other_pythonw
            return original_resolve(path, strict=strict)

        with (
            mock.patch.object(manager.sys, "executable", str(self.python)),
            mock.patch.object(path_type, "resolve", autospec=True, side_effect=resolve),
            mock.patch.object(manager.shutil, "which", return_value="pwsh"),
            mock.patch.object(manager.subprocess, "run") as run,
            self.assertRaisesRegex(manager.ManagerError, "CHILD_INSTALL_FAILED"),
        ):
            self.services.create_child(self.installer, r"C:\codex\codex.exe", None)

        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
