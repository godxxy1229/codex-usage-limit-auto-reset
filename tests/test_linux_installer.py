from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from unittest import mock

import install_linux as installer


UTC = timezone.utc


class LinuxInstallerUnitTests(unittest.TestCase):
    def test_unit_directory_is_stable_and_ignores_xdg_config_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            with mock.patch.object(installer.Path, "home", return_value=home), mock.patch.dict(
                os.environ, {"XDG_CONFIG_HOME": str(Path(temporary) / "other-config")}
            ):
                layout = installer._resolve_layout()
            self.assertEqual(layout.unit_dir, home / ".config" / "systemd" / "user")

    def test_exact_unit_directory_rejects_insecure_mode_without_repair(self) -> None:
        path = Path("/home/test/.config/systemd/user")
        secure = types.SimpleNamespace(st_uid=1000, st_mode=stat.S_IFDIR | 0o755)
        insecure = types.SimpleNamespace(st_uid=1000, st_mode=stat.S_IFDIR | 0o775)
        with mock.patch.object(installer.Path, "mkdir"), mock.patch.object(
            installer.Path, "is_symlink", return_value=False
        ), mock.patch.object(
            installer.Path, "is_dir", return_value=True
        ), mock.patch.object(
            installer.Path, "stat", return_value=secure
        ), mock.patch.object(
            installer.os, "geteuid", create=True, return_value=1000
        ), mock.patch.object(installer.os, "chmod") as chmod:
            installer._ensure_unit_directory(path)
            chmod.assert_not_called()
        with mock.patch.object(installer.Path, "mkdir"), mock.patch.object(
            installer.Path, "is_symlink", return_value=False
        ), mock.patch.object(
            installer.Path, "is_dir", return_value=True
        ), mock.patch.object(
            installer.Path, "stat", return_value=insecure
        ), mock.patch.object(
            installer.os, "geteuid", create=True, return_value=1000
        ), mock.patch.object(installer.os, "chmod") as chmod:
            with self.assertRaisesRegex(installer.InstallError, "world-writable"):
                installer._ensure_unit_directory(path)
            chmod.assert_not_called()

    def test_systemd_argument_quoting_blocks_specifier_and_line_injection(self) -> None:
        self.assertEqual(installer._unit_quote("/tmp/a b%q"), '"/tmp/a b%%q"')
        with self.assertRaises(installer.InstallError):
            installer._unit_quote("bad\nUnit=other.service")
        with self.assertRaises(installer.InstallError):
            installer._unit_quote("")

    def test_systemd_scalar_paths_are_unquoted_and_fail_closed(self) -> None:
        self.assertEqual(installer._unit_scalar_path("/tmp/a-b"), "/tmp/a-b")
        for value in (
            "relative/path",
            "/tmp/a b",
            "/tmp/a/../b",
            "/tmp/a%q",
            "/tmp/a\\b",
            "/tmp/a'b",
            '/tmp/a"b',
            "/tmp/a\nUnit=other.service",
        ):
            with self.subTest(value=value), self.assertRaises(installer.InstallError):
                installer._unit_scalar_path(value)

    def test_manager_sync_contract_is_startup_plus_exact_thirty_minutes(self) -> None:
        timer = installer._manager_timer().decode()
        self.assertIn("OnStartupSec=1min\n", timer)
        self.assertIn("OnUnitActiveSec=30min\n", timer)
        self.assertIn("Persistent=true\n", timer)
        self.assertNotIn("RemainAfterElapse=true\n", timer)
        self.assertIn("RandomizedDelaySec=0\n", timer)
        self.assertIn("WakeSystem=false\n", timer)
        self.assertIn(f"Unit={installer.MANAGER_SERVICE}\n", timer)

    def test_one_shot_contract_is_exact_nonrestarting_user_service(self) -> None:
        with tempfile.TemporaryDirectory() as _temporary:
            root = PurePosixPath("/home/test/.local/share/codex-usage-limit-auto-reset")
            layout = installer.Layout(
                root=root,
                runners=root / "runners",
                installers=root / "installers",
                manifests=root / "manifests",
                state=root / "state",
                logs=root / "logs",
                unit_dir=root / "units",
                wrapper=root / "bin" / "manager",
            )
            runtime = installer.RuntimeFiles(
                python=root / "python3",
                codex=root / "codex",
                codex_home=root / ".codex",
                guard=root / "guard.py",
                manager=root / "manager.py",
                installer=root / "install.py",
            )
            manifest = root / "manifests" / "job.json"
            service = installer._consume_service(runtime, layout, manifest).decode()
            self.assertIn("Type=oneshot\n", service)
            self.assertIn(f"WorkingDirectory={root}\n", service)
            self.assertNotIn(f'WorkingDirectory="{root}"\n', service)
            self.assertIn("RefuseManualStart=yes\n", service)
            self.assertIn("Restart=no\n", service)
            self.assertIn("TimeoutStartSec=10min\n", service)
            self.assertIn("UMask=0077\n", service)
            self.assertIn("NoNewPrivileges=yes\n", service)
            self.assertIn('"run" "--manifest"', service)
            self.assertIn('"--live"', service)
            self.assertNotIn("CODEX_RESET_NPM", service)
            self.assertIn('Environment="PATH=/usr/bin:/bin"\n', service)

            timer = installer._consume_timer(
                "codex-reset-consume-0123456789ab-01234567.service",
                datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC),
            ).decode()
            self.assertIn("OnCalendar=2030-01-02 03:04:05 UTC\n", timer)
            self.assertIn("Persistent=true\n", timer)
            self.assertIn("RemainAfterElapse=true\n", timer)
            self.assertIn("AccuracySec=1s\n", timer)
            self.assertIn("RandomizedDelaySec=0\n", timer)
            self.assertIn("WakeSystem=false\n", timer)

    def test_manager_service_pins_exact_content_addressed_paths(self) -> None:
        root = PurePosixPath("/home/test/.local/share/codex-usage-limit-auto-reset")
        layout = installer.Layout(
            root=root,
            runners=root / "runners",
            installers=root / "installers",
            manifests=root / "manifests",
            state=root / "state",
            logs=root / "logs",
            unit_dir=Path("/home/test/.config/systemd/user"),
            wrapper=Path("/home/test/.local/bin/codex-reset-manager"),
        )
        runtime = installer.RuntimeFiles(
            python=Path("/usr/bin/python3.13"),
            codex=Path("/npm/codex"),
            codex_home=Path("/home/test/.codex"),
            guard=root / "runners" / ("codex_reset_guard-" + "a" * 64 + ".py"),
            manager=root / "runners" / ("codex_reset_manager-" + "b" * 64 + ".py"),
            installer=root / "installers" / ("install_linux-" + "c" * 64 + ".py"),
            npm=Path("/opt/node/bin/npm"),
        )
        service = installer._manager_service(runtime, layout).decode()
        self.assertIn(f'{installer._unit_quote(runtime.python)} "-I"', service)
        self.assertIn(f"WorkingDirectory={root}\n", service)
        self.assertNotIn(f'WorkingDirectory="{root}"\n', service)
        self.assertIn(
            f'{installer._unit_quote(runtime.manager)} "--root" '
            f'{installer._unit_quote(root)} "sync" "--scheduled"',
            service,
        )
        self.assertIn("Type=oneshot\n", service)
        self.assertIn("NoNewPrivileges=yes\n", service)
        self.assertIn("TimeoutStartSec=30min\n", service)
        self.assertIn(
            f"Environment={installer._unit_quote('CODEX_HOME=' + str(runtime.codex_home))}\n",
            service,
        )
        deterministic_path = installer._deterministic_path(runtime.npm)
        self.assertIn(
            f"Environment={installer._unit_quote('CODEX_RESET_NPM=' + str(runtime.npm))} "
            f"{installer._unit_quote('PATH=' + deterministic_path)}\n",
            service,
        )
        self.assertEqual(service.count("Environment="), 2)
        wrapper = installer._wrapper(runtime, layout).decode()
        self.assertIn(
            f"export CODEX_HOME={installer.shlex.quote(str(runtime.codex_home))}\n",
            wrapper,
        )
        self.assertIn(
            f"export CODEX_RESET_NPM={installer.shlex.quote(str(runtime.npm))}\n",
            wrapper,
        )
        self.assertIn(
            f"export PATH={installer.shlex.quote(deterministic_path)}\n", wrapper
        )
        with mock.patch.dict(
            os.environ,
            {"PATH": "/interactive/bin", "CODEX_RESET_NPM": "/interactive/bin/npm"},
        ):
            environment = installer._manager_environment(layout, runtime)
        self.assertEqual(environment["CODEX_RESET_NPM"], str(runtime.npm))
        self.assertEqual(environment["PATH"], deterministic_path)

    def test_codex_home_selection_prefers_explicit_then_existing_pin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = installer.Layout(
                root=root,
                runners=root / "runners",
                installers=root / "installers",
                manifests=root / "manifests",
                state=root / "state",
                logs=root / "logs",
                unit_dir=root / "units",
                wrapper=root / "bin" / "manager",
            )
            layout.unit_dir.mkdir()
            pinned = root / "pinned-codex-home"
            explicit = root / "explicit-codex-home"
            (layout.unit_dir / installer.MANAGER_SERVICE).write_text(
                "[Service]\n"
                f"Environment={installer._unit_quote('CODEX_HOME=' + str(pinned))}\n"
                f"Environment={installer._unit_quote('CODEX_RESET_NPM=/opt/node/bin/npm')} "
                f"{installer._unit_quote('PATH=/opt/node/bin:' + installer.SYSTEM_PATH)}\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                installer, "_validate_codex_home", side_effect=lambda path: path.resolve()
            ):
                self.assertEqual(installer._select_codex_home(layout, None), pinned.resolve())
                self.assertEqual(
                    installer._select_codex_home(layout, explicit), explicit.resolve()
                )

    def test_existing_manager_environment_rejects_missing_or_unknown_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = installer.Layout(
                root=root,
                runners=root / "runners",
                installers=root / "installers",
                manifests=root / "manifests",
                state=root / "state",
                logs=root / "logs",
                unit_dir=root / "units",
                wrapper=root / "bin" / "manager",
            )
            layout.unit_dir.mkdir()
            service = layout.unit_dir / installer.MANAGER_SERVICE
            common = (
                "[Service]\n"
                f"Environment={installer._unit_quote('CODEX_HOME=/home/test/.codex')}\n"
                f"Environment={installer._unit_quote('CODEX_RESET_NPM=/opt/node/bin/npm')}"
            )
            variants = {
                "missing": common + "\n",
                "unknown": (
                    common
                    + f" {installer._unit_quote('PATH=/opt/node/bin:' + installer.SYSTEM_PATH)}\n"
                    + f"Environment={installer._unit_quote('INJECTED=value')}\n"
                ),
            }
            for label, content in variants.items():
                with self.subTest(label=label):
                    service.write_text(content, encoding="utf-8")
                    with self.assertRaisesRegex(installer.InstallError, "exactly"):
                        installer._existing_codex_home_pin(layout)

    def test_npm_selection_prefers_explicit_then_verified_service_pin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = installer.Layout(
                root=root,
                runners=root / "runners",
                installers=root / "installers",
                manifests=root / "manifests",
                state=root / "state",
                logs=root / "logs",
                unit_dir=root / "units",
                wrapper=root / "bin" / "manager",
            )
            layout.unit_dir.mkdir()
            pinned = Path("/opt/pinned/bin/npm")
            explicit = Path("/opt/explicit/bin/npm")
            service = layout.unit_dir / installer.MANAGER_SERVICE
            service.write_text(
                "[Service]\n"
                f"Environment={installer._unit_quote('CODEX_HOME=/home/test/.codex')}\n"
                f"Environment={installer._unit_quote('CODEX_RESET_NPM=' + str(pinned))} "
                f"{installer._unit_quote('PATH=' + installer._deterministic_path(pinned))}\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                installer, "_validate_npm_launcher", side_effect=lambda path: path
            ) as validate, mock.patch.object(
                installer, "_validate_loaded_unit"
            ) as validate_loaded, mock.patch.object(
                installer.shutil, "which", return_value="/interactive/bin/npm"
            ) as which:
                self.assertEqual(installer._select_npm(layout, None), pinned)
                validate_loaded.assert_called_once_with(
                    installer.MANAGER_SERVICE, service, service.read_bytes()
                )
                which.assert_not_called()
                self.assertEqual(installer._select_npm(layout, explicit), explicit)
            self.assertEqual(validate.call_args_list[-1], mock.call(explicit))

    def test_fresh_npm_selection_uses_interactive_path_then_pins_fixed_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = installer.Layout(
                root=root,
                runners=root / "runners",
                installers=root / "installers",
                manifests=root / "manifests",
                state=root / "state",
                logs=root / "logs",
                unit_dir=root / "units",
                wrapper=root / "bin" / "manager",
            )
            layout.unit_dir.mkdir()
            interactive = Path("/home/test/node/bin/npm")
            with mock.patch.object(
                installer.shutil, "which", return_value=str(interactive)
            ), mock.patch.object(
                installer, "_validate_npm_launcher", return_value=interactive
            ):
                self.assertEqual(installer._select_npm(layout, None), interactive)
            with mock.patch.dict(os.environ, {"PATH": "/different/interactive/path"}):
                environment = installer._pinned_npm_environment(interactive)
            self.assertEqual(environment["CODEX_RESET_NPM"], str(interactive))
            self.assertEqual(
                environment["PATH"], installer._deterministic_path(interactive)
            )

    def test_npm_launcher_keeps_launcher_path_and_requires_same_dir_node(self) -> None:
        with self.assertRaisesRegex(installer.InstallError, "absolute path named npm"):
            installer._validate_npm_launcher(Path("npm"))
        with self.assertRaisesRegex(installer.InstallError, "absolute path named npm"):
            installer._validate_npm_launcher(Path("/opt/node/bin/npm-cli.js"))
        directory = Path(Path.cwd().anchor) / "opt" / "node" / "bin"
        with mock.patch.object(
            installer.Path, "resolve", return_value=directory
        ), mock.patch.object(installer, "_validate_safe_linux_path") as safe, mock.patch.object(
            installer, "_validate_npm_launcher_link"
        ) as link:
            launcher = installer._validate_npm_launcher(directory / "npm")
        self.assertEqual(launcher, directory / "npm")
        safe.assert_called_once_with(
            directory, description="npm launcher directory", directory=True
        )
        self.assertEqual(
            link.call_args_list,
            [
                mock.call(directory / "npm", "npm launcher"),
                mock.call(directory / "node", "npm node runtime"),
            ],
        )

    def test_child_npm_context_must_match_loaded_service_pins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = installer.Layout(
                root=root,
                runners=root / "runners",
                installers=root / "installers",
                manifests=root / "manifests",
                state=root / "state",
                logs=root / "logs",
                unit_dir=root / "units",
                wrapper=root / "bin" / "manager",
            )
            layout.unit_dir.mkdir()
            pinned = Path("/opt/node/bin/npm")
            (layout.unit_dir / installer.MANAGER_SERVICE).write_text(
                "[Service]\n"
                f"Environment={installer._unit_quote('CODEX_HOME=/home/test/.codex')}\n"
                f"Environment={installer._unit_quote('CODEX_RESET_NPM=' + str(pinned))} "
                f"{installer._unit_quote('PATH=' + installer._deterministic_path(pinned))}\n",
                encoding="utf-8",
            )
            exact = {
                "CODEX_RESET_NPM": str(pinned),
                "PATH": installer._deterministic_path(pinned),
            }
            with mock.patch.object(
                installer, "_validate_npm_launcher", return_value=pinned
            ), mock.patch.object(installer, "_validate_loaded_unit") as loaded, mock.patch.dict(
                os.environ, exact
            ):
                self.assertEqual(installer._require_child_npm_context(layout), pinned)
                loaded.assert_called_once()
            with mock.patch.object(
                installer, "_validate_npm_launcher", return_value=pinned
            ), mock.patch.object(installer, "_validate_loaded_unit"), mock.patch.dict(
                os.environ, {**exact, "PATH": "/interactive/bin"}
            ):
                with self.assertRaisesRegex(installer.InstallError, "differs"):
                    installer._require_child_npm_context(layout)

    def test_npm_root_command_uses_only_the_pinned_runtime_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            npm = Path("/opt/node/bin/npm")
            completed = types.SimpleNamespace(stdout=str(root) + "\n")
            with mock.patch.object(installer, "_run", return_value=completed) as run:
                self.assertEqual(installer._npm_root(npm), root.resolve())
            environment = run.call_args.kwargs["env"]
            self.assertEqual(environment["CODEX_RESET_NPM"], str(npm))
            self.assertEqual(environment["PATH"], installer._deterministic_path(npm))

    def test_python_admission_accepts_final_gil_base_311_and_future(self) -> None:
        base = {
            "implementation": "CPython",
            "version": [3, 11, 0],
            "releaselevel": "final",
            "prefix": "/usr",
            "basePrefix": "/usr",
            "gilDisabled": False,
            "gilEnabled": True,
        }
        with mock.patch.object(installer, "_canonical_file", return_value=Path("/usr/bin/python3.11")), mock.patch.object(
            installer, "_python_probe", return_value=base
        ):
            self.assertEqual(installer._validate_python(Path("/usr/bin/python3.11")), Path("/usr/bin/python3.11"))
        future = dict(base, version=[3, 15, 1])
        with mock.patch.object(installer, "_canonical_file", return_value=Path("/usr/bin/python3.15")), mock.patch.object(
            installer, "_python_probe", return_value=future
        ):
            self.assertEqual(installer._validate_python(Path("/usr/bin/python3.15")), Path("/usr/bin/python3.15"))

    def test_python_admission_rejects_unsupported_variants(self) -> None:
        good = {
            "implementation": "CPython",
            "version": [3, 11, 0],
            "releaselevel": "final",
            "prefix": "/usr",
            "basePrefix": "/usr",
            "gilDisabled": False,
            "gilEnabled": True,
        }
        variants = (
            dict(good, version=[3, 10, 14]),
            dict(good, releaselevel="candidate"),
            dict(good, implementation="PyPy"),
            dict(good, prefix="/tmp/venv"),
            dict(good, gilDisabled=True, gilEnabled=False),
        )
        for probe in variants:
            with self.subTest(probe=probe), mock.patch.object(
                installer, "_canonical_file", return_value=Path("/usr/bin/python3")
            ), mock.patch.object(installer, "_python_probe", return_value=probe):
                with self.assertRaises(installer.InstallError):
                    installer._validate_python(Path("/usr/bin/python3"))

    def test_content_addressed_copy_is_verified_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.py"
            source.write_bytes(b"print('safe')\n")
            destination = installer._install_immutable(source, root / "runners", "runner")
            digest = installer._sha256(source)
            self.assertEqual(destination.name, f"runner-{digest}.py")
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertEqual(installer._install_immutable(source, root / "runners", "runner"), destination)
            os.chmod(destination, 0o600)
            destination.write_bytes(b"changed")
            with self.assertRaises(installer.InstallError):
                installer._install_immutable(source, root / "runners", "runner")

    def test_codex_discovery_rejects_two_supported_native_binaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            npm_root = Path(temporary)
            package = npm_root / "@openai" / "codex"
            x64 = (
                package
                / "node_modules"
                / "@openai"
                / "codex-linux-x64"
                / "vendor"
                / "x86_64-unknown-linux-musl"
                / "bin"
                / "codex"
            )
            arm64 = (
                package
                / "node_modules"
                / "@openai"
                / "codex-linux-arm64"
                / "vendor"
                / "aarch64-unknown-linux-musl"
                / "bin"
                / "codex"
            )
            for path in (x64, arm64):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"binary")
            with mock.patch.object(installer, "_npm_root", return_value=npm_root), mock.patch.object(
                installer.platform, "machine", return_value="x86_64"
            ):
                with self.assertRaisesRegex(installer.InstallError, "exactly one"):
                    installer._discover_codex(x64, Path("/opt/node/bin/npm"))

    def test_file_snapshot_restores_bytes_mode_and_absence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "policy.json"
            path.write_bytes(b"before")
            os.chmod(path, 0o600)
            snapshot = installer._snapshot(path)
            path.write_bytes(b"after")
            installer._restore(path, snapshot)
            self.assertEqual(path.read_bytes(), b"before")
            absent = installer._snapshot(Path(temporary) / "missing")
            missing = Path(temporary) / "missing"
            missing.write_bytes(b"new")
            installer._restore(missing, absent)
            self.assertFalse(missing.exists())

    def test_full_trust_result_is_strict_and_bound_to_exact_path(self) -> None:
        codex = Path("/npm/codex")
        valid = {
            "path": str(codex),
            "version": "0.144.3",
            "sha256": "a" * 64,
            "signerSubject": "npm-provenance:openai/codex:commit",
        }
        module = types.SimpleNamespace(validate_linux_cli_trust=lambda path: valid)
        with mock.patch.object(installer, "_load_module", return_value=module):
            self.assertEqual(installer._validate_full_cli_trust(Path("guard.py"), codex), valid)
        invalid = dict(valid, extra="unknown")
        module = types.SimpleNamespace(validate_linux_cli_trust=lambda path: invalid)
        with mock.patch.object(installer, "_load_module", return_value=module):
            with self.assertRaises(installer.InstallError):
                installer._validate_full_cli_trust(Path("guard.py"), codex)

    def test_full_trust_runs_with_the_pinned_npm_environment(self) -> None:
        npm = Path("/opt/node/bin/npm")
        observed: dict[str, str | None] = {}

        def validate(_guard: Path, _codex: Path) -> dict[str, str]:
            observed["npm"] = os.environ.get("CODEX_RESET_NPM")
            observed["path"] = os.environ.get("PATH")
            return {"trusted": "yes"}

        with mock.patch.dict(
            os.environ,
            {"CODEX_RESET_NPM": "/interactive/npm", "PATH": "/interactive/bin"},
        ), mock.patch.object(
            installer, "_validate_full_cli_trust", side_effect=validate
        ):
            result = installer._validate_full_cli_trust_pinned(
                Path("guard.py"), Path("codex"), npm
            )
            self.assertEqual(os.environ["CODEX_RESET_NPM"], "/interactive/npm")
            self.assertEqual(os.environ["PATH"], "/interactive/bin")
        self.assertEqual(result, {"trusted": "yes"})
        self.assertEqual(observed["npm"], str(npm))
        self.assertEqual(observed["path"], installer._deterministic_path(npm))

    def test_loaded_unit_rejects_pending_daemon_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "unit.service"
            expected = b"[Service]\nType=oneshot\n"
            path.write_bytes(expected)
            properties = {
                "LoadState": "loaded",
                "FragmentPath": str(path),
                "DropInPaths": "",
                "NeedDaemonReload": "no",
            }
            with mock.patch.object(
                installer, "_loaded_unit_properties", return_value=properties
            ):
                installer._validate_loaded_unit("unit.service", path, expected)
            properties["NeedDaemonReload"] = "yes"
            with mock.patch.object(
                installer, "_loaded_unit_properties", return_value=properties
            ):
                with self.assertRaises(installer.InstallError):
                    installer._validate_loaded_unit("unit.service", path, expected)

    def test_policy_is_validated_by_the_exact_immutable_manager(self) -> None:
        calls: list[object] = []
        module = types.SimpleNamespace(_validate_policy=lambda policy: calls.append(policy))
        policy = {"schemaVersion": 1}
        with mock.patch.object(installer, "_load_module", return_value=module):
            installer._validate_policy_with_manager(Path("manager.py"), policy)
        self.assertEqual(calls, [policy])
        rejecting = types.SimpleNamespace(
            _validate_policy=lambda policy: (_ for _ in ()).throw(ValueError("bad"))
        )
        with mock.patch.object(installer, "_load_module", return_value=rejecting):
            with self.assertRaises(installer.InstallError):
                installer._validate_policy_with_manager(Path("manager.py"), policy)

    def test_child_approved_pin_observation_requires_all_fields(self) -> None:
        codex = Path("/npm/codex")
        approved = {
            "codexExe": str(codex),
            "codexVersion": "0.144.3",
            "codexSha256": "a" * 64,
            "signerSubject": "npm-provenance:openai/codex:commit",
            "approvedAtUtc": "2030-01-01T00:00:00Z",
        }
        module = types.SimpleNamespace(
            observe_cli_pin=lambda path: {
                "path": str(path),
                "version": "0.144.3",
                "sha256": "a" * 64,
                "signerSubject": "npm-provenance:openai/codex:commit",
            }
        )
        installer._observe_approved_cli(module, {"approvedCli": approved}, codex)
        changed = types.SimpleNamespace(
            observe_cli_pin=lambda path: {
                "path": str(path),
                "version": "0.144.4",
                "sha256": "a" * 64,
                "signerSubject": "npm-provenance:openai/codex:commit",
            }
        )
        with self.assertRaises(installer.InstallError):
            installer._observe_approved_cli(changed, {"approvedCli": approved}, codex)

    def test_child_cli_contract_and_unit_name_are_stable(self) -> None:
        args = installer._build_parser().parse_args(
            [
                "--manager-child-only",
                "--install-root",
                "/home/test/.local/share/codex-usage-limit-auto-reset",
                "--python-path",
                "/usr/bin/python3.13",
                "--codex-path",
                "/npm/codex",
                "--runtime-guard",
                "/root/runners/codex_reset_guard-" + "a" * 64 + ".py",
            ]
        )
        self.assertTrue(args.manager_child_only)
        self.assertEqual(args.python_path, Path("/usr/bin/python3.13"))
        self.assertIsNotNone(
            installer.CONSUME_UNIT_RE.fullmatch(
                "codex-reset-consume-0123456789ab-01234567.timer"
            )
        )

    def test_shell_entrypoint_is_thin_and_never_escalates(self) -> None:
        source = (Path(__file__).parents[1] / "setup-linux.sh").read_text(encoding="utf-8")
        self.assertIn('exec "$PYTHON_BIN" -I "$SCRIPT_DIR/install_linux.py" "$@"', source)
        self.assertNotIn('["sudo"', source)
        self.assertNotIn("loginctl", source)

    def test_installer_has_no_linger_or_system_unit_mutation(self) -> None:
        source = (Path(__file__).parents[1] / "install_linux.py").read_text(encoding="utf-8")
        self.assertNotIn("enable-linger", source)
        self.assertNotIn("/etc/systemd", source)
        self.assertNotIn('["sudo"', source)
        self.assertIn('["systemctl", "--user", *arguments]', source)

    def test_update_waits_for_manager_without_stopping_service(self) -> None:
        responses = [
            types.SimpleNamespace(returncode=0),
            types.SimpleNamespace(returncode=0),
            types.SimpleNamespace(returncode=3),
        ]
        calls: list[tuple[object, ...]] = []

        def systemctl(*arguments: str, check: bool = True) -> object:
            del check
            calls.append(arguments)
            return responses.pop(0)

        clock = iter((0.0, 0.1, 0.2))
        with mock.patch.object(installer, "_systemctl", side_effect=systemctl):
            installer._wait_manager_inactive(now=lambda: next(clock), sleeper=lambda _: None)
        self.assertEqual(
            calls,
            [
                ("is-active", "--quiet", installer.MANAGER_SERVICE),
                ("is-active", "--quiet", installer.MANAGER_SERVICE),
                ("is-active", "--quiet", installer.MANAGER_SERVICE),
            ],
        )
        self.assertFalse(any(call and call[0] == "stop" for call in calls))

    def test_rollback_skips_stale_policy_when_active_one_shot_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = installer.Layout(
                root=root,
                runners=root / "runners",
                installers=root / "installers",
                manifests=root / "manifests",
                state=root / "state",
                logs=root / "logs",
                unit_dir=root / "units",
                wrapper=root / "bin" / "manager",
            )
            policy = layout.state / "policy.json"
            service = layout.unit_dir / installer.MANAGER_SERVICE
            snapshots = {
                policy: installer.FileSnapshot(True, b"{}", 0o600),
                service: installer.FileSnapshot(True, b"old-service", 0o600),
            }
            restored: list[Path] = []

            def write(path: Path, data: bytes, mode: int) -> None:
                del mode
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)

            with mock.patch.object(
                installer,
                "_assert_snapshots_unchanged",
                side_effect=installer.InstallError("changed"),
            ), mock.patch.object(
                installer,
                "_restore",
                side_effect=lambda path, snapshot: restored.append(path),
            ), mock.patch.object(installer, "_atomic_write", side_effect=write), mock.patch.object(
                installer, "_validate_policy_with_manager"
            ), mock.patch.object(installer, "_systemctl"), mock.patch.object(
                installer, "_restore_unit_state"
            ):
                with self.assertRaisesRegex(installer.InstallError, "paused and blocked"):
                    installer._rollback_normal_install(
                        layout,
                        snapshots,
                        installer.UnitState(False, False),
                        {root / "active.json": "a" * 64},
                        {"consume.timer": installer.UnitState(True, True)},
                        root / "manager.py",
                    )
            self.assertNotIn(policy, restored)
            self.assertIn(service, restored)


if __name__ == "__main__":
    unittest.main()
