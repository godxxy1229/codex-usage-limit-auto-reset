"""Mocked-Tk integration tests for manager window and tray wiring."""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import codex_reset_manager as manager


class _Variable:
    def __init__(self, value: object = None) -> None:
        self.value = value

    def get(self) -> object:
        return self.value

    def set(self, value: object) -> None:
        self.value = value


class _Widget:
    def __init__(self, *args: object, **kwargs: object) -> None:
        del args
        self.options = dict(kwargs)
        self.states: list[list[str]] = []

    def pack(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def grid(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def columnconfigure(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def configure(self, **kwargs: object) -> None:
        self.options.update(kwargs)

    def state(self, values: list[str]) -> None:
        self.states.append(values)


class _FakeRoot:
    def __init__(self, state_dir: Path, *, tray_starts: bool, lose_tray: bool = False) -> None:
        self.state_dir = state_dir
        self.tray_starts = tray_starts
        self.lose_tray = lose_tray
        self.protocols: dict[str, object] = {}
        self.scheduled: list[tuple[int, object]] = []
        self.hidden = False
        self.destroyed = False
        self.withdraw_calls = 0
        self.restore_calls = 0
        self.focus_calls = 0

    def title(self, value: str) -> None:
        del value

    def geometry(self, value: str) -> None:
        del value

    def minsize(self, *values: object) -> None:
        del values

    def option_add(self, *values: object) -> None:
        del values

    def protocol(self, name: str, callback: object) -> None:
        self.protocols[name] = callback

    def after(self, delay: int, callback: object) -> None:
        self.scheduled.append((delay, callback))

    def after_idle(self, callback: object) -> None:
        callback()

    def withdraw(self) -> None:
        self.withdraw_calls += 1
        self.hidden = True

    def deiconify(self) -> None:
        self.restore_calls += 1
        self.hidden = False

    def state(self, value: str | None = None) -> str:
        del value
        return "normal"

    def lift(self) -> None:
        pass

    def focus_force(self) -> None:
        self.focus_calls += 1

    def destroy(self) -> None:
        self.destroyed = True

    def _run_named(self, name: str) -> None:
        for index, (_, callback) in enumerate(self.scheduled):
            if getattr(callback, "__name__", "") == name:
                self.scheduled.pop(index)
                callback()
                return
        raise AssertionError(f"scheduled callback not found: {name}")

    def mainloop(self) -> None:
        ready = self.state_dir / manager.UI_READY_FILENAME
        tray = _FakeTray.instance
        assert tray is not None
        if not self.tray_starts:
            self.protocols["WM_DELETE_WINDOW"]()
            self.ready_during_loop = ready.exists()
            return

        self.ready_during_loop = ready.exists()
        if self.lose_tray:
            self.withdraw()
            tray.running = False
            self._run_named("poll_tray_events")
            self.visible_after_tray_loss = not self.hidden
            self.ready_after_tray_loss = ready.exists()
            self.protocols["WM_DELETE_WINDOW"]()
            return
        self.protocols["WM_DELETE_WINDOW"]()
        self.hidden_after_x = self.hidden

        tray.on_open()
        self._run_named("poll_tray_events")
        self.visible_after_tray_open = not self.hidden

        self.withdraw()
        secondary = manager.UiInstanceLease(self.state_dir)
        assert not secondary.acquire()
        self._run_named("poll_show_request")
        self.visible_after_second_launch = not self.hidden

        tray.on_exit()
        self._run_named("poll_tray_events")


class _FakeTray:
    instance: "_FakeTray | None" = None
    start_result = True

    def __init__(
        self,
        *,
        on_open: object,
        on_check: object,
        on_toggle: object,
        on_exit: object,
        is_enabled: object,
    ) -> None:
        self.on_open = on_open
        self.on_check = on_check
        self.on_toggle = on_toggle
        self.on_exit = on_exit
        self.is_enabled = is_enabled
        self.running = False
        self.stop_calls = 0
        _FakeTray.instance = self

    def start(self) -> bool:
        self.running = self.start_result
        return self.running

    def stop(self) -> None:
        self.stop_calls += 1
        self.running = False


class _Controller:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.state_dir = root / "state"
        self.operations: list[str] = []

    def __getattr__(self, name: str) -> object:
        if name in {"enable", "pause", "sync", "doctor", "bootstrap_status"}:
            def unexpected() -> object:
                self.operations.append(name)
                raise AssertionError(f"unexpected controller operation: {name}")

            return unexpected
        raise AttributeError(name)


def _tk_modules(root: _FakeRoot) -> dict[str, types.ModuleType]:
    tkinter = types.ModuleType("tkinter")
    ttk = types.ModuleType("tkinter.ttk")
    messagebox = types.ModuleType("tkinter.messagebox")
    tkinter.Tk = lambda: root
    tkinter.StringVar = _Variable
    tkinter.BooleanVar = _Variable
    tkinter.TclError = RuntimeError
    for name in ("Frame", "Label", "Button"):
        setattr(ttk, name, _Widget)
    for name in ("showerror", "showinfo", "showwarning"):
        setattr(messagebox, name, lambda *args, **kwargs: None)
    tkinter.ttk = ttk
    tkinter.messagebox = messagebox
    return {
        "tkinter": tkinter,
        "tkinter.ttk": ttk,
        "tkinter.messagebox": messagebox,
    }


class ManagerUiIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root_path = Path(self.temporary.name)
        _FakeTray.instance = None

    def _run(
        self,
        *,
        tray_starts: bool,
        lose_tray: bool = False,
    ) -> tuple[_FakeRoot, _Controller, _FakeTray]:
        root = _FakeRoot(
            self.root_path / "state",
            tray_starts=tray_starts,
            lose_tray=lose_tray,
        )
        controller = _Controller(self.root_path)
        _FakeTray.start_result = tray_starts
        with (
            mock.patch.dict(sys.modules, _tk_modules(root)),
            mock.patch.object(manager, "NativeTrayIcon", _FakeTray),
        ):
            self.assertEqual(manager.run_ui(controller), 0)
        assert _FakeTray.instance is not None
        return root, controller, _FakeTray.instance

    def test_x_restore_second_launch_and_exit_ui_lifecycle(self) -> None:
        root, controller, tray = self._run(tray_starts=True)

        self.assertTrue(root.ready_during_loop)
        self.assertTrue(root.hidden_after_x)
        self.assertTrue(root.visible_after_tray_open)
        self.assertTrue(root.visible_after_second_launch)
        self.assertTrue(root.destroyed)
        self.assertGreaterEqual(tray.stop_calls, 1)
        self.assertEqual(controller.operations, [])
        self.assertFalse((self.root_path / "state" / manager.UI_READY_FILENAME).exists())

        replacement = manager.UiInstanceLease(self.root_path / "state")
        self.addCleanup(replacement.release)
        self.assertTrue(replacement.acquire())

    def test_tray_failure_keeps_visible_fallback_and_publishes_no_ready_marker(self) -> None:
        root, controller, tray = self._run(tray_starts=False)

        self.assertFalse(root.ready_during_loop)
        self.assertEqual(root.withdraw_calls, 0)
        self.assertTrue(root.destroyed)
        self.assertFalse(tray.running)
        self.assertEqual(controller.operations, [])

    def test_tray_loss_withdraws_readiness_and_restores_hidden_window(self) -> None:
        root, controller, tray = self._run(tray_starts=True, lose_tray=True)

        self.assertTrue(root.ready_during_loop)
        self.assertTrue(root.visible_after_tray_loss)
        self.assertFalse(root.ready_after_tray_loss)
        self.assertTrue(root.destroyed)
        self.assertFalse(tray.running)
        self.assertEqual(controller.operations, [])


if __name__ == "__main__":
    unittest.main()
