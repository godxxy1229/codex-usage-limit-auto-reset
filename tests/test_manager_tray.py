"""Focused lifecycle and action tests for the native tray coordinator."""

from __future__ import annotations

import threading
import ctypes
import unittest
from unittest import mock

import codex_reset_manager as manager


class _SimulatedTray(manager.NativeTrayIcon):
    """Exercise the public lifecycle without creating an Explorer icon."""

    def _run_windows(self) -> None:
        self._hwnd = 123
        self._icon_available.set()
        self._ready.set()
        self._stop_requested.wait(2.0)

    def _post_close(self, hwnd: int) -> None:
        self.posted_close = hwnd


class _FailingTray(manager.NativeTrayIcon):
    def _run_windows(self) -> None:
        raise OSError("simulated Explorer failure")


class ManagerTrayTests(unittest.TestCase):
    def make_tray(self, tray_type: type[manager.NativeTrayIcon] = _SimulatedTray):
        actions: list[str] = []
        enabled = {"value": False}
        tray = tray_type(
            on_open=lambda: actions.append("open"),
            on_check=lambda: actions.append("check"),
            on_toggle=lambda: actions.append("toggle"),
            on_exit=lambda: actions.append("exit"),
            is_enabled=lambda: enabled["value"],
        )
        return tray, actions, enabled

    def test_start_stop_owns_one_lifetime_thread(self) -> None:
        tray, _, _ = self.make_tray()
        with mock.patch.object(manager.os, "name", "nt"):
            self.assertTrue(tray.start())
            self.assertTrue(tray.running)
            thread = tray._thread
            self.assertTrue(tray.start())
            self.assertIs(tray._thread, thread)
            tray.stop()

        self.assertFalse(tray.running)
        self.assertEqual(tray.posted_close, 123)

    def test_menu_commands_dispatch_the_expected_ui_requests(self) -> None:
        tray, actions, _ = self.make_tray()
        for command in (
            tray.COMMAND_OPEN,
            tray.COMMAND_CHECK,
            tray.COMMAND_TOGGLE,
            tray.COMMAND_EXIT,
            9999,
        ):
            tray._dispatch_command(command)

        self.assertEqual(actions, ["open", "check", "toggle", "exit"])

    def test_toggle_menu_label_tracks_current_policy_state(self) -> None:
        tray, _, enabled = self.make_tray()
        self.assertEqual(tray.toggle_menu_text, "Start Automatic Use")
        enabled["value"] = True
        self.assertEqual(tray.toggle_menu_text, "Pause Automatic Use")

    def test_callback_failure_does_not_kill_tray_dispatch(self) -> None:
        called = threading.Event()
        tray = _SimulatedTray(
            on_open=lambda: (_ for _ in ()).throw(RuntimeError("UI is closing")),
            on_check=called.set,
            on_toggle=lambda: None,
            on_exit=lambda: None,
            is_enabled=lambda: False,
        )

        tray._dispatch_command(tray.COMMAND_OPEN)
        tray._dispatch_command(tray.COMMAND_CHECK)

        self.assertTrue(called.is_set())

    def test_explorer_start_failure_is_best_effort_and_stoppable(self) -> None:
        tray, _, _ = self.make_tray(_FailingTray)
        with mock.patch.object(manager.os, "name", "nt"):
            self.assertFalse(tray.start())
            tray.stop()

        self.assertFalse(tray.running)

    def test_explorer_readd_failure_immediately_marks_icon_unavailable(self) -> None:
        tray, _, _ = self.make_tray()
        with mock.patch.object(manager.os, "name", "nt"):
            self.assertTrue(tray.start())
            self.assertTrue(tray.running)
            self.assertFalse(tray._replace_icon(lambda: False))
            self.assertFalse(tray.running)
            self.assertTrue(tray._replace_icon(lambda: True))
            self.assertTrue(tray.running)
            tray.stop()

    def test_setversion_failure_removes_partial_icon_registration(self) -> None:
        class Data(ctypes.Structure):
            _fields_ = [("uTimeoutOrVersion", ctypes.c_uint)]

        class Shell:
            def __init__(self) -> None:
                self.operations: list[int] = []

            def Shell_NotifyIconW(self, operation: int, pointer: object) -> int:
                del pointer
                self.operations.append(operation)
                return 0 if operation == 4 else 1

        shell = Shell()
        self.assertFalse(manager.NativeTrayIcon._register_notify_icon(shell, Data()))
        self.assertEqual(shell.operations, [0, 4, 2])

    def test_add_and_setversion_success_marks_registration_usable(self) -> None:
        class Data(ctypes.Structure):
            _fields_ = [("uTimeoutOrVersion", ctypes.c_uint)]

        class Shell:
            def __init__(self) -> None:
                self.operations: list[int] = []

            def Shell_NotifyIconW(self, operation: int, pointer: object) -> int:
                del pointer
                self.operations.append(operation)
                return 1

        shell = Shell()
        data = Data()
        self.assertTrue(manager.NativeTrayIcon._register_notify_icon(shell, data))
        self.assertEqual(data.uTimeoutOrVersion, 4)
        self.assertEqual(shell.operations, [0, 4])


if __name__ == "__main__":
    unittest.main()
