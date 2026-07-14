"""Linux-only guard adapter tests that do not require a Linux host."""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import codex_reset_guard as guard
import install_linux


VERSION = "0.144.3"
COMMIT = "7" * 40
TIMER = "codex-reset-consume-123456789abc-deadbeef.timer"


def provenance_item(location: str, package_version: str, commit: str = COMMIT) -> dict[str, object]:
    statement = {
        "predicateType": "https://slsa.dev/provenance/v1",
        "subject": [
            {
                "name": f"pkg:npm/%40openai/codex@{package_version}",
                "digest": {"sha512": "a" * 128},
            }
        ],
        "predicate": {
            "buildDefinition": {
                "externalParameters": {
                    "workflow": {
                        "repository": "https://github.com/openai/codex",
                        "path": ".github/workflows/rust-release.yml",
                        "ref": f"refs/tags/rust-v{VERSION}",
                    }
                },
                "resolvedDependencies": [
                    {
                        "uri": (
                            "git+https://github.com/openai/codex@"
                            f"refs/tags/rust-v{VERSION}"
                        ),
                        "digest": {"gitCommit": commit},
                    }
                ],
            }
        },
    }
    payload = base64.b64encode(
        json.dumps(statement, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return {
        "name": "@openai/codex",
        "version": package_version,
        "location": location,
        "registry": "https://registry.npmjs.org/",
        "attestations": {
            "provenance": {"predicateType": "https://slsa.dev/provenance/v1"}
        },
        "attestationBundles": [
            {
                "predicateType": "https://slsa.dev/provenance/v1",
                "bundle": {"dsseEnvelope": {"payload": payload}},
            }
        ],
    }


def audit_payload(*, native_commit: str = COMMIT) -> dict[str, object]:
    return {
        "invalid": [],
        "missing": [],
        "verified": [
            provenance_item("node_modules/@openai/codex", VERSION),
            provenance_item(
                "node_modules/@openai/codex-linux-x64",
                f"{VERSION}-linux-x64",
                native_commit,
            ),
        ],
    }


class PlatformContractTests(unittest.TestCase):
    def test_linux_requires_exact_unix_linux_pair(self) -> None:
        with (
            mock.patch.object(guard, "_is_linux", return_value=True),
            mock.patch.object(guard, "_assert_supported_linux_host"),
        ):
            guard._validate_app_server_platform(
                {"platformFamily": "unix", "platformOs": "linux"}
            )
            with self.assertRaisesRegex(guard.ProtocolError, "unix/linux"):
                guard._validate_app_server_platform(
                    {"platformFamily": "windows", "platformOs": "linux"}
                )

    def test_wsl_is_explicitly_rejected(self) -> None:
        with (
            mock.patch.object(guard, "_is_linux", return_value=True),
            mock.patch.object(guard, "_is_wsl", return_value=True),
        ):
            with self.assertRaisesRegex(guard.GuardError, "WSL"):
                guard._assert_supported_linux_host()


class NpmLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.bin = Path(self.temporary.name) / "bin"
        self.bin.mkdir()
        self.npm = self.bin / "npm"
        self.node = self.bin / "node"
        self.npm.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        self.node.write_bytes(b"synthetic-node")

    def test_explicit_pinned_npm_wins_when_ambient_path_has_none(self) -> None:
        with (
            mock.patch.object(guard, "_assert_supported_linux_host"),
            mock.patch.object(guard, "_validate_safe_linux_path"),
            mock.patch.object(guard, "_validate_npm_launcher_link"),
            mock.patch.object(
                guard.shutil,
                "which",
                side_effect=AssertionError("ambient PATH must not be consulted"),
            ),
            mock.patch.dict(
                guard.os.environ,
                {"CODEX_RESET_NPM": str(self.npm), "PATH": ""},
            ),
        ):
            self.assertEqual(guard._validated_npm_launcher(), self.npm.resolve())

    def test_global_root_uses_exact_validated_launcher_and_pins_node_path(self) -> None:
        global_root = Path(self.temporary.name) / "global" / "node_modules"
        completed = subprocess.CompletedProcess(
            [], 0, stdout=str(global_root) + "\n", stderr=""
        )
        with (
            mock.patch.object(
                guard, "_validated_npm_launcher", return_value=self.npm.resolve()
            ),
            mock.patch.object(guard.subprocess, "run", return_value=completed) as runner,
            mock.patch.dict(guard.os.environ, {"PATH": "/ambient/different"}),
        ):
            root = guard._npm_global_root()
        self.assertEqual(root, global_root.resolve())
        self.assertEqual(runner.call_args.args[0], [str(self.npm.resolve()), "root", "-g"])
        invocation_path = runner.call_args.kwargs["env"]["PATH"].split(os.pathsep)
        self.assertEqual(invocation_path[0], str(self.npm.resolve().parent))

    def test_relative_or_wrong_name_explicit_launcher_is_rejected(self) -> None:
        for configured in ("npm", str(self.bin / "npm-cli.js")):
            with self.subTest(configured=configured):
                with (
                    mock.patch.object(guard, "_assert_supported_linux_host"),
                    mock.patch.dict(
                        guard.os.environ, {"CODEX_RESET_NPM": configured}
                    ),
                ):
                    with self.assertRaisesRegex(guard.GuardError, "absolute path named npm"):
                        guard._validated_npm_launcher()

    def test_owner_and_write_permissions_fail_closed(self) -> None:
        synthetic = mock.Mock()
        synthetic.is_symlink.return_value = False
        with mock.patch.object(guard.os, "getuid", return_value=1000, create=True):
            synthetic.stat.return_value = mock.Mock(
                st_mode=stat.S_IFREG | 0o755, st_uid=2000
            )
            with self.assertRaisesRegex(guard.GuardError, "unexpected owner"):
                guard._validate_safe_linux_path(
                    synthetic, label="synthetic", directory=False
                )
            synthetic.stat.return_value = mock.Mock(
                st_mode=stat.S_IFREG | 0o775, st_uid=1000
            )
            with self.assertRaisesRegex(guard.GuardError, "group- or world-writable"):
                guard._validate_safe_linux_path(
                    synthetic, label="synthetic", directory=False
                )


class LinuxNativeDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.global_root = Path(self.temporary.name) / "lib" / "node_modules"
        self.package_root = self.global_root / "@openai" / "codex"
        self.package_root.mkdir(parents=True)
        (self.package_root / "package.json").write_text(
            json.dumps({"name": "@openai/codex", "version": VERSION}),
            encoding="utf-8",
        )

    def native(self, arch: str = "x64", target: str | None = None) -> Path:
        target = target or (
            "x86_64-unknown-linux-musl"
            if arch == "x64"
            else "aarch64-unknown-linux-musl"
        )
        native_root = (
            self.package_root
            / "node_modules"
            / "@openai"
            / f"codex-linux-{arch}"
        )
        native_root.mkdir(parents=True, exist_ok=True)
        (native_root / "package.json").write_text(
            json.dumps(
                {
                    "name": "@openai/codex",
                    "version": f"{VERSION}-linux-{arch}",
                }
            ),
            encoding="utf-8",
        )
        path = (
            native_root
            / "vendor"
            / target
            / "bin"
            / "codex"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic-codex")
        path.chmod(0o700)
        return path

    def linux_patches(self):
        return (
            mock.patch.object(guard, "_is_linux", return_value=True),
            mock.patch.object(guard, "_assert_supported_linux_host"),
            mock.patch.object(guard, "_npm_global_root", return_value=self.global_root),
            mock.patch.object(guard, "_linux_arch_label", return_value="x64"),
            mock.patch.object(guard, "_validate_linux_package_tree"),
        )

    def test_discovers_exact_global_x64_native_and_package_version(self) -> None:
        native = self.native()
        patches = self.linux_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            found = guard._find_native_codex()
            self.assertEqual(found, native.resolve())
            self.assertEqual(guard._npm_package_version_for_native(found), VERSION)

    def test_rejects_wrong_vendor_target_and_multiple_native_binaries(self) -> None:
        self.native(target="x86_64-unknown-linux-gnu")
        patches = self.linux_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with self.assertRaisesRegex(guard.GuardError, "not found"):
                guard._find_native_codex()
        selected = self.native()
        self.native("arm64")
        patches = self.linux_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with self.assertRaisesRegex(guard.GuardError, "multiple"):
                guard._find_native_codex()
            with self.assertRaisesRegex(guard.GuardError, "multiple"):
                guard._find_native_codex(str(selected))

    def test_old_pinned_root_is_observed_without_current_global_npm_lookup(self) -> None:
        native = self.native()
        current_root = mock.Mock(side_effect=AssertionError("global npm root must not be read"))
        patches = self.linux_patches()
        with (
            patches[0],
            patches[1],
            mock.patch.object(guard, "_npm_global_root", current_root),
            patches[3],
            patches[4],
            mock.patch.object(
                guard, "_codex_version", return_value=f"codex-cli {VERSION}"
            ),
        ):
            observed = guard.observe_pinned_cli_pin(native)
        current_root.assert_not_called()
        self.assertEqual(observed["path"], str(native.resolve()))
        self.assertEqual(observed["packageVersion"], VERSION)
        self.assertEqual(observed["sha256"], guard._sha256_file(native))

    def test_old_pinned_root_rejects_root_or_native_metadata_mismatch(self) -> None:
        native = self.native()
        root_metadata = self.package_root / "package.json"
        native_metadata = (
            self.package_root
            / "node_modules"
            / "@openai"
            / "codex-linux-x64"
            / "package.json"
        )
        patches = self.linux_patches()
        with (
            patches[0],
            patches[1],
            mock.patch.object(
                guard, "_npm_global_root", side_effect=AssertionError("must not run")
            ),
            patches[3],
            patches[4],
            mock.patch.object(
                guard, "_codex_version", return_value=f"codex-cli {VERSION}"
            ),
        ):
            root_metadata.write_text(
                json.dumps({"name": "not-openai", "version": VERSION}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(guard.GuardError, "root npm package identity"):
                guard.observe_pinned_cli_pin(native)
            root_metadata.write_text(
                json.dumps({"name": "@openai/codex", "version": VERSION}),
                encoding="utf-8",
            )
            native_metadata.write_text(
                json.dumps(
                    {"name": "@openai/codex", "version": f"{VERSION}-linux-arm64"}
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(guard.GuardError, "does not match"):
                guard.observe_pinned_cli_pin(native)


class NpmProvenanceTests(unittest.TestCase):
    def test_exact_root_and_native_provenance_produces_canonical_subject(self) -> None:
        subject = guard._validate_npm_audit_payload(audit_payload(), VERSION, "x64")
        self.assertEqual(
            subject,
            "npm-provenance:repo=openai/codex;"
            "workflow=.github/workflows/rust-release.yml;"
            f"tag=rust-v{VERSION};commit={COMMIT}",
        )
        self.assertEqual(
            guard._validate_linux_provenance_subject(subject, VERSION), COMMIT
        )

    def test_missing_attestation_and_different_source_commit_fail_closed(self) -> None:
        missing = audit_payload()
        missing["missing"] = [{"name": "@openai/codex"}]
        with self.assertRaises(guard.GuardError):
            guard._validate_npm_audit_payload(missing, VERSION, "x64")
        with self.assertRaisesRegex(guard.GuardError, "different source commits"):
            guard._validate_npm_audit_payload(
                audit_payload(native_commit="8" * 40), VERSION, "x64"
            )

    def test_cached_pin_requires_exact_observation_and_canonical_subject(self) -> None:
        observed = {
            "path": str(Path("/global/codex").resolve()),
            "version": f"codex-cli {VERSION}",
            "sha256": "a" * 64,
            "packageVersion": VERSION,
        }
        approved = {
            "codexExe": observed["path"],
            "codexVersion": observed["version"],
            "codexSha256": observed["sha256"],
            "signerSubject": guard._linux_provenance_identity(VERSION, COMMIT),
            "approvedAtUtc": "2030-01-01T00:00:00Z",
        }
        with mock.patch.object(guard, "observe_cli_pin", return_value=observed):
            result = guard._trusted_linux_binary_info(Path(observed["path"]), approved)
            self.assertEqual(result.sha256, "a" * 64)
            changed = dict(approved, codexSha256="b" * 64)
            with self.assertRaises(guard.GuardError):
                guard._trusted_linux_binary_info(Path(observed["path"]), changed)
            changed = dict(approved, signerSubject="npm-provenance:untrusted")
            with self.assertRaises(guard.GuardError):
                guard._trusted_linux_binary_info(Path(observed["path"]), changed)

    def test_cached_compatibility_skips_only_network_audit(self) -> None:
        binary = guard.BinaryInfo(
            path=str(Path("/global/codex").resolve()),
            version=f"codex-cli {VERSION}",
            sha256="a" * 64,
            signer_subject=guard._linux_provenance_identity(VERSION, COMMIT),
        )
        approved = {
            "codexExe": binary.path,
            "codexVersion": binary.version,
            "codexSha256": binary.sha256,
            "signerSubject": binary.signer_subject,
            "approvedAtUtc": "2030-01-01T00:00:00Z",
        }
        observed = {
            "path": binary.path,
            "version": binary.version,
            "sha256": binary.sha256,
            "packageVersion": VERSION,
        }

        class ReadOnlyTransport:
            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def request(self, method: str, _params: object = None):
                if method == "account/read":
                    return {
                        "account": {
                            "type": "chatgpt",
                            "email": "linux@example.com",
                            "planType": "plus",
                        }
                    }
                if method == "account/rateLimits/read":
                    return {
                        "rateLimitResetCredits": {
                            "availableCount": 1,
                            "credits": [
                                {
                                    "id": "linux-credit",
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
                raise AssertionError(method)

        full_audit = mock.Mock(side_effect=AssertionError("network audit must be skipped"))
        schema = mock.Mock()
        with (
            mock.patch.object(guard, "_is_linux", return_value=True),
            mock.patch.object(guard, "_find_native_codex", return_value=Path(binary.path)),
            mock.patch.object(
                guard, "_npm_package_version_for_native", return_value=VERSION
            ),
            mock.patch.object(guard, "observe_cli_pin", return_value=observed),
            mock.patch.object(guard, "_validate_cli_schema", schema),
            mock.patch.object(guard, "AppServerTransport", return_value=ReadOnlyTransport()),
            mock.patch.object(guard, "validate_linux_cli_trust", full_audit),
        ):
            result = guard.validate_cli_compatibility(
                trusted_binary=approved,
                codex_home=Path("/codex-home"),
            )
        self.assertTrue(result["compatible"])
        schema.assert_called_once()
        full_audit.assert_not_called()


class LinuxTimeTests(unittest.TestCase):
    def test_only_exact_timedatectl_yes_is_accepted(self) -> None:
        linger_disabled = subprocess.CompletedProcess([], 0, stdout="no\n", stderr="")
        synchronized = subprocess.CompletedProcess([], 0, stdout="yes\n", stderr="")
        unsynchronized = subprocess.CompletedProcess([], 0, stdout="no\n", stderr="")
        runner = mock.Mock(side_effect=[linger_disabled, synchronized])
        with (
            mock.patch.object(guard, "_is_linux", return_value=True),
            mock.patch.object(guard, "_assert_supported_linux_host"),
            mock.patch.object(
                guard.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"
            ),
            mock.patch.object(guard.os, "geteuid", return_value=1000, create=True),
            mock.patch.object(guard.subprocess, "run", runner),
        ):
            self.assertEqual(guard._time_status(), "NTPSynchronized=yes")
        self.assertEqual(
            runner.call_args_list[0].args[0],
            [
                "/usr/bin/loginctl",
                "show-user",
                "1000",
                "-p",
                "Linger",
                "--value",
            ],
        )
        with (
            mock.patch.object(guard, "_is_linux", return_value=True),
            mock.patch.object(guard, "_assert_supported_linux_host"),
            mock.patch.object(
                guard.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"
            ),
            mock.patch.object(guard.os, "geteuid", return_value=1000, create=True),
            mock.patch.object(
                guard.subprocess,
                "run",
                side_effect=[linger_disabled, unsynchronized],
            ),
        ):
            with self.assertRaisesRegex(guard.GuardError, "not NTP synchronized"):
                guard._time_status()

    def test_linger_enabled_or_command_failure_is_rejected(self) -> None:
        linger_enabled = subprocess.CompletedProcess([], 0, stdout="yes\n", stderr="")
        command_failed = subprocess.CompletedProcess([], 1, stdout="", stderr="failed")
        for result in (linger_enabled, command_failed):
            with self.subTest(returncode=result.returncode, output=result.stdout.strip()):
                with (
                    mock.patch.object(guard, "_is_linux", return_value=True),
                    mock.patch.object(guard, "_assert_supported_linux_host"),
                    mock.patch.object(
                        guard.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"
                    ),
                    mock.patch.object(guard.os, "geteuid", return_value=1000, create=True),
                    mock.patch.object(guard.subprocess, "run", return_value=result) as runner,
                ):
                    with self.assertRaisesRegex(guard.GuardError, "lingering must be disabled"):
                        guard._time_status()
                self.assertEqual(runner.call_count, 1)


class SystemdContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.units = self.root / "systemd" / "user"
        self.units.mkdir(parents=True)
        self.manifest_path = self.root / "manifests" / "job.json"
        self.manifest_path.parent.mkdir()
        self.manifest = {
            "armed": True,
            "state": "ARMED",
            "schedule": {"triggerAtUtc": "2030-01-01T00:00:00Z"},
        }
        self.service_name = TIMER.removesuffix(".timer") + ".service"
        runner = Path(guard.__file__).resolve()
        command = [
            str(Path(sys.executable).resolve()),
            "-I",
            str(runner),
            "run",
            "--manifest",
            str(self.manifest_path.resolve()),
            "--live",
        ]
        exec_start = " ".join(shlex.quote(item.replace("%", "%%")) for item in command)
        working = str(runner.parent.parent.resolve())
        self.timer_text = f"""[Unit]
Description=Codex consume timer

[Timer]
OnCalendar=2030-01-01 00:00:00 UTC
Persistent=true
RemainAfterElapse=true
AccuracySec=1s
RandomizedDelaySec=0
WakeSystem=false
Unit={self.service_name}

[Install]
WantedBy=timers.target
"""
        self.service_text = f"""[Unit]
Description=Codex consume service
RefuseManualStart=yes

[Service]
Type=oneshot
ExecStart={exec_start}
WorkingDirectory={working}
Environment="PATH=/usr/bin:/bin"
Restart=no
TimeoutStartSec=10min
UMask=0077
NoNewPrivileges=yes
"""
        self.write_units()

    def write_units(self) -> None:
        for name, value in (
            (TIMER, self.timer_text),
            (self.service_name, self.service_text),
        ):
            path = self.units / name
            path.write_text(value, encoding="utf-8")
            path.chmod(0o600)

    def show(self, unit_name: str, properties: set[str]) -> dict[str, str]:
        is_timer = unit_name.endswith(".timer")
        all_values = {
            "LoadState": "loaded",
            "ActiveState": "active" if is_timer else "activating",
            "UnitFileState": "enabled" if is_timer else "static",
            "FragmentPath": str(self.units / unit_name),
            "DropInPaths": "",
            "NeedDaemonReload": "no",
            "MainPID": str(os.getpid()),
            "InvocationID": "1" * 32,
            "SubState": "running" if is_timer else "start",
            "LastTriggerUSec": (
                "Tue 2030-01-01 09:00:00 KST" if is_timer else ""
            ),
            "NextElapseUSecRealtime": "",
            "NextElapseUSecMonotonic": "infinity" if is_timer else "",
        }
        return {key: all_values[key] for key in properties}

    def patches(self):
        return (
            mock.patch.object(guard, "_is_linux", return_value=True),
            mock.patch.object(guard, "_assert_supported_linux_host"),
            mock.patch.object(
                guard, "_systemd_user_unit_directory", return_value=self.units
            ),
            mock.patch.object(guard, "_systemd_show", side_effect=self.show),
            mock.patch.object(guard, "_secure_systemd_unit_file"),
            mock.patch.object(guard, "_secure_systemd_unit_directory"),
        )

    def test_exact_live_contract_is_accepted(self) -> None:
        patches = self.patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            with self.assertRaisesRegex(guard.GuardError, "already running"):
                guard._validate_scheduled_task_contract(
                    TIMER, self.manifest_path, self.manifest
                )
            guard._validate_scheduled_task_contract(
                TIMER,
                self.manifest_path,
                self.manifest,
                require_live_identity=True,
            )

    def test_real_installer_output_matches_guard_contract(self) -> None:
        runner = Path(guard.__file__).resolve()
        layout = install_linux.Layout(
            root=runner.parent.parent,
            runners=self.root / "runners",
            installers=self.root / "installers",
            manifests=self.manifest_path.parent,
            state=self.root / "state",
            logs=self.root / "logs",
            unit_dir=self.units,
            wrapper=self.root / "bin" / "codex-reset-manager",
        )
        runtime = install_linux.RuntimeFiles(
            python=Path(sys.executable).resolve(),
            codex=self.root / "codex",
            codex_home=self.root / "codex-home",
            guard=runner,
            manager=None,
            installer=Path(install_linux.__file__).resolve(),
        )
        with mock.patch.object(
            install_linux, "_unit_scalar_path", side_effect=lambda value: os.fspath(value)
        ):
            (self.units / self.service_name).write_bytes(
                install_linux._consume_service(runtime, layout, self.manifest_path)
            )
        (self.units / TIMER).write_bytes(
            install_linux._consume_timer(
                self.service_name,
                dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc),
            )
        )
        for path in (self.units / TIMER, self.units / self.service_name):
            path.chmod(0o600)
        patches = self.patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            guard._validate_scheduled_task_contract(
                TIMER,
                self.manifest_path,
                self.manifest,
                require_live_identity=True,
            )

    def test_quoted_and_relative_working_directories_are_rejected(self) -> None:
        original = self.service_text
        working_line = next(
            line for line in original.splitlines() if line.startswith("WorkingDirectory=")
        )
        working = working_line.split("=", 1)[1]
        for mutation in (f'WorkingDirectory="{working}"', "WorkingDirectory=relative"):
            with self.subTest(mutation=mutation):
                self.service_text = original.replace(working_line, mutation)
                self.write_units()
                patches = self.patches()
                with (
                    patches[0],
                    patches[1],
                    patches[2],
                    patches[3],
                    patches[4],
                    patches[5],
                    self.assertRaisesRegex(guard.GuardError, "working directory changed"),
                ):
                    guard._validate_scheduled_task_contract(
                        TIMER,
                        self.manifest_path,
                        self.manifest,
                        require_live_identity=True,
                    )
        self.service_text = original
        self.write_units()

    def test_pre_arm_requires_inactive_service(self) -> None:
        manifest = dict(self.manifest, armed=False, state="UNARMED")

        def prearm_show(unit_name: str, properties: set[str]) -> dict[str, str]:
            result = self.show(unit_name, properties)
            if unit_name.endswith(".timer"):
                result["SubState"] = "waiting"
                result["LastTriggerUSec"] = ""
                result["NextElapseUSecRealtime"] = "Tue 2030-01-01 09:00:00 KST"
                result["NextElapseUSecMonotonic"] = "0"
            else:
                result["ActiveState"] = "inactive"
                result["MainPID"] = "0"
                result["InvocationID"] = ""
            return result

        patches = self.patches()
        with (
            patches[0],
            patches[1],
            patches[2],
            mock.patch.object(guard, "_systemd_show", side_effect=prearm_show),
            patches[4],
            patches[5],
        ):
            guard._validate_scheduled_task_contract(
                TIMER, self.manifest_path, manifest
            )
            guard._validate_scheduled_task_contract(
                TIMER, self.manifest_path, dict(manifest, armed=True, state="ARMED")
            )
            for inconsistent in (
                dict(manifest, armed=True, state="UNARMED"),
                dict(manifest, armed=False, state="ARMED"),
            ):
                with self.assertRaisesRegex(guard.GuardError, "invalid manifest phase"):
                    guard._validate_scheduled_task_contract(
                        TIMER, self.manifest_path, inconsistent
                    )

    def test_elapsed_timer_is_distinguished_from_a_trigger_in_progress(self) -> None:
        trigger = guard._parse_iso_utc(self.manifest["schedule"]["triggerAtUtc"])

        def elapsed_show(unit_name: str, properties: set[str]) -> dict[str, str]:
            result = self.show(unit_name, properties)
            if unit_name.endswith(".timer"):
                result["SubState"] = "elapsed"
            else:
                result["ActiveState"] = "inactive"
                result["MainPID"] = "0"
            return result

        patches = self.patches()
        with (
            patches[0],
            patches[1],
            patches[2],
            mock.patch.object(guard, "_systemd_show", side_effect=elapsed_show),
            patches[4],
            patches[5],
            mock.patch.object(
                guard.time,
                "time",
                return_value=trigger + guard.SYSTEMD_TRIGGER_SETTLE_SECONDS - 1,
            ),
            self.assertRaises(guard.SystemdTriggerInProgressError),
        ):
            guard._validate_scheduled_task_contract(
                TIMER, self.manifest_path, self.manifest
            )

        patches = self.patches()
        with (
            patches[0],
            patches[1],
            patches[2],
            mock.patch.object(guard, "_systemd_show", side_effect=elapsed_show),
            patches[4],
            patches[5],
            mock.patch.object(
                guard.time,
                "time",
                return_value=trigger + guard.SYSTEMD_TRIGGER_SETTLE_SECONDS,
            ),
            self.assertRaises(guard.SystemdTriggerElapsedError),
        ):
            guard._validate_scheduled_task_contract(
                TIMER, self.manifest_path, self.manifest
            )

    def test_manual_start_specifier_pid_and_reload_mutations_are_rejected(self) -> None:
        original = self.service_text
        self.service_text = original.replace("RefuseManualStart=yes\n", "")
        self.write_units()
        patches = self.patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            with self.assertRaises(guard.GuardError):
                guard._validate_scheduled_task_contract(
                    TIMER,
                    self.manifest_path,
                    self.manifest,
                    require_live_identity=True,
                )
        self.service_text = original
        self.write_units()

        self.service_text = original.replace(
            'Environment="PATH=/usr/bin:/bin"',
            'Environment="PATH=/tmp/bin"',
        )
        self.write_units()
        patches = self.patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            with self.assertRaisesRegex(guard.GuardError, "environment changed"):
                guard._validate_scheduled_task_contract(
                    TIMER,
                    self.manifest_path,
                    self.manifest,
                    require_live_identity=True,
                )
        self.service_text = original
        self.write_units()

        def wrong_pid(unit_name: str, properties: set[str]) -> dict[str, str]:
            result = self.show(unit_name, properties)
            if unit_name.endswith(".service"):
                result["MainPID"] = str(os.getpid() + 1)
            return result

        patches = self.patches()
        with (
            patches[0], patches[1], patches[2],
            mock.patch.object(guard, "_systemd_show", side_effect=wrong_pid),
            patches[4],
            patches[5],
        ):
            with self.assertRaisesRegex(guard.GuardError, "identity"):
                guard._validate_scheduled_task_contract(
                    TIMER,
                    self.manifest_path,
                    self.manifest,
                    require_live_identity=True,
                )

        def reload_needed(unit_name: str, properties: set[str]) -> dict[str, str]:
            result = self.show(unit_name, properties)
            result["NeedDaemonReload"] = "yes"
            return result

        patches = self.patches()
        with (
            patches[0], patches[1], patches[2],
            mock.patch.object(guard, "_systemd_show", side_effect=reload_needed),
            patches[4],
            patches[5],
        ):
            with self.assertRaisesRegex(guard.GuardError, "not loaded"):
                guard._validate_scheduled_task_contract(
                    TIMER,
                    self.manifest_path,
                    self.manifest,
                    require_live_identity=True,
                )

    def test_unescaped_percent_specifier_is_rejected(self) -> None:
        with self.assertRaisesRegex(guard.GuardError, "percent specifier"):
            guard._decode_systemd_literal_percent("/tmp/%n/codex", "ExecStart")
        self.assertEqual(
            guard._decode_systemd_literal_percent("/tmp/%%name/codex", "ExecStart"),
            "/tmp/%name/codex",
        )

    def test_unit_directory_ignores_ambient_xdg_config_home(self) -> None:
        fake_home = self.root / "home"
        with (
            mock.patch.object(guard.Path, "home", return_value=fake_home),
            mock.patch.dict(
                guard.os.environ,
                {"XDG_CONFIG_HOME": str(self.root / "redirected")},
            ),
        ):
            self.assertEqual(
                guard._systemd_user_unit_directory(),
                (fake_home / ".config" / "systemd" / "user").resolve(),
            )

    def test_unit_directory_must_be_owned_and_not_writable_by_others(self) -> None:
        directory = mock.Mock()
        directory.is_symlink.return_value = False
        directory.is_dir.return_value = True
        directory.stat.return_value = mock.Mock(st_uid=1000, st_mode=0o40755)
        with mock.patch.object(guard.os, "getuid", return_value=1000, create=True):
            guard._secure_systemd_unit_directory(directory)

            directory.stat.return_value = mock.Mock(st_uid=1000, st_mode=0o40777)
            with self.assertRaisesRegex(guard.GuardError, "group- or world-writable"):
                guard._secure_systemd_unit_directory(directory)

            directory.stat.return_value = mock.Mock(st_uid=1001, st_mode=0o40755)
            with self.assertRaisesRegex(guard.GuardError, "unexpected owner"):
                guard._secure_systemd_unit_directory(directory)

            directory.is_symlink.return_value = True
            with self.assertRaisesRegex(guard.GuardError, "symbolic link"):
                guard._secure_systemd_unit_directory(directory)

    def test_disable_and_delete_touch_only_exact_timer_and_service(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        systemctl = mock.Mock(return_value=completed)
        patches = self.patches()
        with (
            patches[0],
            patches[1],
            patches[2],
            mock.patch.object(guard, "_systemctl_user", systemctl),
            patches[4],
            patches[5],
        ):
            self.assertTrue(guard._disable_task_best_effort(TIMER))
            systemctl.assert_called_with(["disable", "--now", TIMER])
            systemctl.reset_mock()
            self.assertTrue(guard._delete_task_best_effort(TIMER))
            self.assertFalse((self.units / TIMER).exists())
            self.assertFalse((self.units / self.service_name).exists())
            self.assertEqual(
                systemctl.call_args_list,
                [
                    mock.call(["disable", "--now", TIMER]),
                    mock.call(["daemon-reload"]),
                ],
            )
            self.assertFalse(guard._disable_task_best_effort("--system"))


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux fcntl")
class LinuxDispatchLockProcessTests(unittest.TestCase):
    def test_dispatch_lock_conflicts_across_real_processes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifests" / "job.json"
            script = (
                "import sys,time; from pathlib import Path; "
                "from codex_reset_guard import DispatchLock; "
                "lock=DispatchLock(Path(sys.argv[1])); lock.__enter__(); "
                "print('ready',flush=True); time.sleep(60)"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(manifest)],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.addCleanup(lambda: process.poll() is None and process.kill())
            assert process.stdout is not None
            self.assertEqual(process.stdout.readline().strip(), "ready")
            with self.assertRaisesRegex(
                guard.GuardError, "another live guard is already dispatching"
            ):
                with guard.DispatchLock(manifest):
                    self.fail("the second process must not acquire dispatch.lock")
            process.kill()
            process.communicate(timeout=10)

            with guard.DispatchLock(manifest):
                pass


if __name__ == "__main__":
    unittest.main()
