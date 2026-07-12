"""Windows notification lifecycle tests without displaying a real banner."""

from __future__ import annotations

import types
import unittest
from unittest import mock

import codex_reset_manager as manager


class _FakeShell32:
    def __init__(self) -> None:
        self.operations: list[int] = []

    def Shell_NotifyIconW(self, operation: int, _data: object) -> int:
        self.operations.append(operation)
        return 1


class _FakeUser32:
    def __init__(self, *, fail_during_pump: bool = False) -> None:
        self.destroyed: list[int] = []
        self.fail_during_pump = fail_during_pump

    def CreateWindowExW(self, *args: object) -> int:
        del args
        return 123

    def PeekMessageW(self, *args: object) -> int:
        del args
        if self.fail_during_pump:
            raise RuntimeError("fake message-pump failure")
        return 0

    def TranslateMessage(self, *args: object) -> int:
        del args
        return 1

    def DispatchMessageW(self, *args: object) -> int:
        del args
        return 1

    def DestroyWindow(self, hwnd: int) -> int:
        self.destroyed.append(hwnd)
        return 1


class ManagerNotificationTests(unittest.TestCase):
    def _run(self, *, fail_during_pump: bool = False) -> tuple[bool, _FakeShell32, _FakeUser32]:
        shell32 = _FakeShell32()
        user32 = _FakeUser32(fail_during_pump=fail_during_pump)
        windll = types.SimpleNamespace(shell32=shell32, user32=user32)
        with (
            mock.patch.object(manager.os, "name", "nt"),
            mock.patch.object(manager.ctypes, "windll", windll, create=True),
            mock.patch.object(
                manager.time,
                "monotonic",
                side_effect=[100.0, 100.0, 109.0],
            ),
            mock.patch.object(manager.time, "sleep"),
        ):
            result = manager._shell_notification("safe title", "safe message")
        return result, shell32, user32

    def test_success_keeps_owner_alive_then_always_removes_icon_and_window(self) -> None:
        result, shell32, user32 = self._run()

        self.assertTrue(result)
        self.assertEqual(shell32.operations, [0, 2])
        self.assertEqual(user32.destroyed, [123])

    def test_message_pump_failure_is_best_effort_and_still_cleans_up(self) -> None:
        result, shell32, user32 = self._run(fail_during_pump=True)

        self.assertFalse(result)
        self.assertEqual(shell32.operations, [0, 2])
        self.assertEqual(user32.destroyed, [123])


if __name__ == "__main__":
    unittest.main()
