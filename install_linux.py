#!/usr/bin/env python3
"""Transactional per-user installer for the Linux systemd backend.

This installer deliberately has no privileged mode.  It installs immutable,
content-addressed Python runtimes beneath the current user's XDG data tree and
only writes systemd *user* units.  The manager owns continuous reconciliation;
the internal ``--manager-child-only`` mode creates one exact-ID one-shot job.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import hashlib
import importlib.util
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator, Mapping, Sequence


APP_VERSION = "1.0.0"
MINIMUM_CODEX_VERSION = (0, 144, 1)
MANAGER_SERVICE = "codex-reset-manager-sync.service"
MANAGER_TIMER = "codex-reset-manager-sync.timer"
MANAGER_ENVIRONMENT_KEYS = {"CODEX_HOME", "CODEX_RESET_NPM", "PATH"}
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
LIVE_SYSTEM_PATH = "/usr/bin:/bin"
CONSUME_UNIT_RE = re.compile(
    r"codex-reset-consume-(?P<credit>[0-9a-f]{12})-(?P<job>[0-9a-f]{8})\.timer"
)
TERMINAL_STATES = {
    "SUCCEEDED",
    "NO_ACTION",
    "FAILED",
    "INDETERMINATE",
    "DISARMED",
    "CLEANED",
    "SUPERSEDED_CLI",
}
UTC = dt.timezone.utc


class InstallError(RuntimeError):
    """A fail-closed installer error safe to show to the user."""


@dataclasses.dataclass(frozen=True)
class Layout:
    root: Path
    runners: Path
    installers: Path
    manifests: Path
    state: Path
    logs: Path
    unit_dir: Path
    wrapper: Path


@dataclasses.dataclass(frozen=True)
class RuntimeFiles:
    python: Path
    codex: Path
    codex_home: Path
    guard: Path
    manager: Path | None
    installer: Path
    npm: Path | None = None


@dataclasses.dataclass(frozen=True)
class FileSnapshot:
    exists: bool
    data: bytes = b""
    mode: int = 0


@dataclasses.dataclass(frozen=True)
class UnitState:
    enabled: bool
    active: bool


def _run(
    command: Sequence[str | os.PathLike[str]],
    *,
    timeout: float = 60,
    env: Mapping[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            [os.fspath(item) for item in command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=dict(env) if env is not None else None,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError(f"command failed to run: {command[0]}") from error
    if check and completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise InstallError(f"command failed: {command[0]}{suffix}")
    return completed


def _require_linux_user_session() -> None:
    if sys.platform != "linux" or os.name != "posix":
        raise InstallError("The Linux installer requires native Linux.")
    if os.geteuid() == 0:
        raise InstallError("Do not run this installer with sudo or as root.")
    with contextlib.suppress(OSError):
        kernel_release = Path("/proc/sys/kernel/osrelease").read_text(encoding="ascii").casefold()
        if "microsoft" in kernel_release:
            raise InstallError("WSL is not supported by the Linux installer.")
    if os.environ.get("WSL_INTEROP") or os.environ.get("WSL_DISTRO_NAME"):
        raise InstallError("WSL is not supported by the Linux installer.")
    if not Path("/run/systemd/system").is_dir():
        raise InstallError("A systemd-based Linux installation is required.")
    if not os.environ.get("XDG_RUNTIME_DIR"):
        raise InstallError("A logged-in systemd user session is required.")
    _run(["systemctl", "--user", "show-environment"], timeout=20)
    linger = _run(
        ["loginctl", "show-user", str(os.geteuid()), "-p", "Linger", "--value"],
        timeout=20,
    )
    if linger.stdout.strip() != "no":
        raise InstallError(
            "User lingering must be disabled; this tool runs only while the user is logged in."
        )


def _resolve_layout(install_root: Path | None = None) -> Layout:
    home = Path.home().resolve()
    data_home = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    root = (install_root or data_home / "codex-usage-limit-auto-reset").expanduser().resolve()
    return Layout(
        root=root,
        runners=root / "runners",
        installers=root / "installers",
        manifests=root / "manifests",
        state=root / "state",
        logs=root / "logs",
        # Keep the scheduler path stable even if a shell changes
        # XDG_CONFIG_HOME between installation and a later manager run.
        unit_dir=home / ".config" / "systemd" / "user",
        wrapper=home / ".local" / "bin" / "codex-reset-manager",
    )


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise InstallError(f"Expected a private directory: {path}")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid():
        raise InstallError(f"Directory is not owned by the current user: {path}")
    os.chmod(path, 0o700)
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise InstallError(f"Could not secure directory permissions: {path}")


def _ensure_shared_directory(path: Path) -> None:
    """Create a conventional user directory without tightening shared parents."""
    path.mkdir(mode=0o755, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise InstallError(f"Expected a user directory: {path}")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid():
        raise InstallError(f"Directory is not owned by the current user: {path}")


def _ensure_unit_directory(path: Path) -> None:
    """Create but never repair the exact systemd user-unit security boundary."""
    path.mkdir(mode=0o755, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise InstallError("The systemd user-unit path must be a non-symbolic-link directory.")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
        raise InstallError(
            "The systemd user-unit directory must be user-owned and not group- or world-writable."
        )


def _prepare_directories(layout: Layout) -> None:
    for directory in (
        layout.root,
        layout.runners,
        layout.installers,
        layout.manifests,
        layout.state,
        layout.logs,
    ):
        _ensure_private_directory(directory)
    _ensure_unit_directory(layout.unit_dir)
    _ensure_shared_directory(layout.wrapper.parent)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_file(path: Path, description: str, *, suffix: str | None = None) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise InstallError(f"{description} is not a regular file: {resolved}")
    if suffix is not None and resolved.suffix != suffix:
        raise InstallError(f"{description} must end with {suffix}.")
    return resolved


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _snapshot(path: Path) -> FileSnapshot:
    if not path.exists():
        return FileSnapshot(False)
    if path.is_symlink() or not path.is_file():
        raise InstallError(f"Refusing to replace a non-regular path: {path}")
    metadata = path.stat()
    current_uid = getattr(os, "geteuid", lambda: metadata.st_uid)()
    if metadata.st_uid != current_uid:
        raise InstallError(f"File is not owned by the current user: {path}")
    return FileSnapshot(True, path.read_bytes(), stat.S_IMODE(metadata.st_mode))


def _restore(path: Path, snapshot: FileSnapshot) -> None:
    if snapshot.exists:
        _atomic_write(path, snapshot.data, snapshot.mode)
    else:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def _install_immutable(source: Path, directory: Path, stem: str) -> Path:
    digest = _sha256(source)
    destination = directory / f"{stem}-{digest}.py"
    if destination.exists():
        if destination.is_symlink() or not destination.is_file() or _sha256(destination) != digest:
            raise InstallError(f"Immutable runtime path has unexpected content: {destination}")
    else:
        _atomic_write(destination, source.read_bytes(), 0o500)
    if _sha256(destination) != digest:
        raise InstallError(f"Installed runtime hash differs from source: {destination}")
    os.chmod(destination, 0o500)
    return destination.resolve()


def _python_probe(path: Path) -> Mapping[str, Any]:
    script = (
        "import json,platform,sys,sysconfig;"
        "print(json.dumps({"
        "'implementation':platform.python_implementation(),"
        "'version':list(sys.version_info[:3]),"
        "'releaselevel':sys.version_info.releaselevel,"
        "'prefix':sys.prefix,'basePrefix':sys.base_prefix,"
        "'gilDisabled':bool(sysconfig.get_config_var('Py_GIL_DISABLED') or 0),"
        "'gilEnabled':bool(getattr(sys,'_is_gil_enabled',lambda:True)())"
        "},sort_keys=True))"
    )
    completed = _run([path, "-I", "-c", script], timeout=20)
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise InstallError("Python capability probe did not return valid JSON.") from error
    if not isinstance(value, Mapping):
        raise InstallError("Python capability probe returned an invalid document.")
    return value


def _validate_python(requested: Path | None) -> Path:
    candidate = _canonical_file((requested or Path(sys.executable)), "Python runtime")
    probe = _python_probe(candidate)
    version = probe.get("version")
    if (
        probe.get("implementation") != "CPython"
        or not isinstance(version, list)
        or len(version) != 3
        or any(type(item) is not int for item in version)
        or tuple(version[:2]) < (3, 11)
        or probe.get("releaselevel") != "final"
        or probe.get("prefix") != probe.get("basePrefix")
        or probe.get("gilDisabled") is not False
        or probe.get("gilEnabled") is not True
    ):
        raise InstallError(
            "A final, GIL-enabled base CPython 3.11+ installation is required."
        )
    return candidate


def _parse_manager_environment(data: bytes) -> dict[str, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeError as error:
        raise InstallError("Existing ManagerSync service is not valid UTF-8.") from error
    environment: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("Environment="):
            continue
        try:
            words = shlex.split(line.removeprefix("Environment="), posix=True)
        except ValueError as error:
            raise InstallError("Existing ManagerSync environment pin is malformed.") from error
        if not words:
            raise InstallError("Existing ManagerSync environment pin is malformed.")
        for word in words:
            if "=" not in word:
                raise InstallError("Existing ManagerSync environment pin is malformed.")
            name, value = word.split("=", 1)
            if re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) is None or name in environment:
                raise InstallError("Existing ManagerSync environment pin is ambiguous.")
            environment[name] = value.replace("%%", "%")
    if set(environment) != MANAGER_ENVIRONMENT_KEYS:
        raise InstallError(
            "Existing ManagerSync environment must contain exactly CODEX_HOME, "
            "CODEX_RESET_NPM, and PATH."
        )
    return environment


def _existing_manager_environment(layout: Layout) -> dict[str, str]:
    service_path = layout.unit_dir / MANAGER_SERVICE
    if not service_path.exists():
        return {}
    if service_path.is_symlink() or not service_path.is_file():
        raise InstallError("Existing ManagerSync service is not a regular file.")
    return _parse_manager_environment(service_path.read_bytes())


def _existing_codex_home_pin(layout: Layout) -> Path | None:
    value = _existing_manager_environment(layout).get("CODEX_HOME")
    if value is None:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise InstallError("Existing ManagerSync CODEX_HOME pin is not absolute.")
    return path.resolve()


def _safe_linux_owner(metadata: os.stat_result, description: str) -> None:
    getuid = getattr(os, "getuid", None)
    current_uid = getuid() if callable(getuid) else metadata.st_uid
    if metadata.st_uid not in {0, current_uid}:
        raise InstallError(f"{description} has an unexpected owner.")


def _validate_safe_linux_path(
    path: Path, *, description: str, directory: bool, executable: bool = False
) -> None:
    if path.is_symlink():
        raise InstallError(f"{description} must not be a symbolic link.")
    try:
        metadata = path.stat()
    except OSError as error:
        raise InstallError(f"{description} could not be inspected.") from error
    if directory:
        if not stat.S_ISDIR(metadata.st_mode):
            raise InstallError(f"{description} is not a directory.")
    elif not stat.S_ISREG(metadata.st_mode):
        raise InstallError(f"{description} is not a regular file.")
    _safe_linux_owner(metadata, description)
    if metadata.st_mode & 0o022:
        raise InstallError(f"{description} is group- or world-writable.")
    if executable and metadata.st_mode & 0o111 == 0:
        raise InstallError(f"{description} is not executable.")


def _validate_npm_launcher_link(path: Path, description: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise InstallError(f"{description} could not be inspected.") from error
    _safe_linux_owner(metadata, description)
    if path.is_symlink():
        try:
            target = path.resolve(strict=True)
        except OSError as error:
            raise InstallError(f"{description} symbolic-link target is invalid.") from error
        _validate_safe_linux_path(
            target,
            description=f"{description} symbolic-link target",
            directory=False,
            executable=True,
        )
        return
    _validate_safe_linux_path(
        path, description=description, directory=False, executable=True
    )


def _validate_npm_launcher(path: Path) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute() or candidate.name != "npm":
        raise InstallError("npm launcher must be an absolute path named npm.")
    if any(character in os.fspath(candidate) for character in "\x00\r\n"):
        raise InstallError("npm launcher path contains a control character.")
    try:
        launcher_directory = candidate.parent.resolve(strict=True)
    except OSError as error:
        raise InstallError("npm launcher directory could not be resolved.") from error
    _validate_safe_linux_path(
        launcher_directory,
        description="npm launcher directory",
        directory=True,
    )
    launcher = launcher_directory / "npm"
    _validate_npm_launcher_link(launcher, "npm launcher")
    _validate_npm_launcher_link(launcher_directory / "node", "npm node runtime")
    return launcher


def _deterministic_path(npm: Path) -> str:
    return f"{npm.parent}:{SYSTEM_PATH}"


def _pinned_npm_environment(npm: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["CODEX_RESET_NPM"] = str(npm)
    environment["PATH"] = _deterministic_path(npm)
    return environment


@contextlib.contextmanager
def _pinned_npm_process_environment(npm: Path) -> Iterator[None]:
    previous_npm = os.environ.get("CODEX_RESET_NPM")
    previous_path = os.environ.get("PATH")
    os.environ["CODEX_RESET_NPM"] = str(npm)
    os.environ["PATH"] = _deterministic_path(npm)
    try:
        yield
    finally:
        if previous_npm is None:
            os.environ.pop("CODEX_RESET_NPM", None)
        else:
            os.environ["CODEX_RESET_NPM"] = previous_npm
        if previous_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous_path


def _existing_npm_pin(layout: Layout, *, verify_loaded: bool) -> Path | None:
    service_path = layout.unit_dir / MANAGER_SERVICE
    if not service_path.exists():
        return None
    if service_path.is_symlink() or not service_path.is_file():
        raise InstallError("Existing ManagerSync service is not a regular file.")
    service_bytes = service_path.read_bytes()
    environment = _parse_manager_environment(service_bytes)
    npm_value = environment.get("CODEX_RESET_NPM")
    path_value = environment.get("PATH")
    if npm_value is None and path_value is None:
        return None
    if npm_value is None or path_value is None:
        raise InstallError("Existing ManagerSync npm pin is incomplete.")
    npm = _validate_npm_launcher(Path(npm_value))
    if path_value != _deterministic_path(npm):
        raise InstallError("Existing ManagerSync PATH does not match its npm pin.")
    if verify_loaded:
        _validate_loaded_unit(MANAGER_SERVICE, service_path, service_bytes)
    return npm


def _select_npm(layout: Layout, requested: Path | None) -> Path:
    if requested is not None:
        return _validate_npm_launcher(requested)
    existing = _existing_npm_pin(layout, verify_loaded=True)
    if existing is not None:
        return existing
    discovered = shutil.which("npm")
    if not discovered:
        raise InstallError("npm was not found on the interactive PATH.")
    return _validate_npm_launcher(Path(discovered))


def _require_child_npm_context(layout: Layout) -> Path:
    pinned = _existing_npm_pin(layout, verify_loaded=True)
    if pinned is None:
        raise InstallError("ManagerSync has no verified npm environment pin.")
    inherited_npm = os.environ.get("CODEX_RESET_NPM")
    inherited_path = os.environ.get("PATH")
    if inherited_npm != str(pinned) or inherited_path != _deterministic_path(pinned):
        raise InstallError("Child npm environment differs from the loaded ManagerSync pin.")
    return pinned


def _validate_codex_home(path: Path) -> Path:
    if any(character in os.fspath(path) for character in "\x00\r\n"):
        raise InstallError("CODEX_HOME contains a control character.")
    unresolved = path.expanduser()
    if not unresolved.is_absolute() or unresolved.is_symlink():
        raise InstallError("CODEX_HOME must be an absolute, non-symbolic-link directory.")
    resolved = unresolved.resolve(strict=True)
    if not resolved.is_dir():
        raise InstallError("CODEX_HOME is not a directory.")
    metadata = resolved.stat()
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
        raise InstallError("CODEX_HOME must be user-owned and not group- or world-writable.")
    return resolved


def _select_codex_home(layout: Layout, requested: Path | None) -> Path:
    if requested is not None:
        return _validate_codex_home(requested)
    existing = _existing_codex_home_pin(layout)
    if existing is not None:
        return _validate_codex_home(existing)
    configured = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return _validate_codex_home(configured)


def _read_json(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InstallError(f"{description} is not valid JSON: {path}") from error
    if not isinstance(value, dict):
        raise InstallError(f"{description} must be a JSON object: {path}")
    return value


def _version_tuple(text: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(?:codex-cli )?(\d+)\.(\d+)\.(\d+)", text.strip())
    if match is None:
        raise InstallError("Codex CLI returned an unsupported version string.")
    return tuple(int(part) for part in match.groups())


def _npm_root(npm: Path) -> Path:
    completed = _run(
        [npm, "root", "-g"], timeout=30, env=_pinned_npm_environment(npm)
    )
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise InstallError("npm root -g returned an ambiguous path.")
    return Path(lines[0]).expanduser().resolve(strict=True)


def _discover_codex(requested: Path | None, npm: Path) -> Path:
    root = _npm_root(npm)
    machine = platform.machine().lower()
    mapping = {
        "x86_64": ("codex-linux-x64", "x86_64-unknown-linux-musl"),
        "amd64": ("codex-linux-x64", "x86_64-unknown-linux-musl"),
        "aarch64": ("codex-linux-arm64", "aarch64-unknown-linux-musl"),
        "arm64": ("codex-linux-arm64", "aarch64-unknown-linux-musl"),
    }
    if machine not in mapping:
        raise InstallError(f"Unsupported Linux architecture: {machine}")
    package, triple = mapping[machine]
    root_package = root / "@openai" / "codex"
    platform_package = root_package / "node_modules" / "@openai" / package
    expected = platform_package / "vendor" / triple / "bin" / "codex"
    expected = _canonical_file(expected, "npm-native Codex executable")
    supported: list[Path] = []
    for candidate in root_package.rglob("codex"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        relative = str(candidate.relative_to(root_package)).replace("\\", "/")
        if re.fullmatch(
            r"node_modules/@openai/codex-linux-x64/vendor/"
            r"x86_64-unknown-linux-musl/bin/codex"
            r"|node_modules/@openai/codex-linux-arm64/vendor/"
            r"aarch64-unknown-linux-musl/bin/codex",
            relative,
        ):
            supported.append(candidate.resolve())
    if len(supported) != 1 or supported[0] != expected:
        raise InstallError("The global npm package must contain exactly one supported native Codex binary.")
    package_json = _read_json(root_package / "package.json", "Codex npm package")
    platform_json = _read_json(platform_package / "package.json", "Codex platform npm package")
    version = package_json.get("version")
    if (
        package_json.get("name") != "@openai/codex"
        or platform_json.get("name") != "@openai/codex"
        or not isinstance(version, str)
        or platform_json.get("version") != f"{version}-linux-{'x64' if 'x64' in package else 'arm64'}"
        or _version_tuple(version) < MINIMUM_CODEX_VERSION
    ):
        raise InstallError("The global Codex npm package layout or version is invalid.")
    if requested is not None and _canonical_file(requested, "Requested Codex executable") != expected:
        raise InstallError("The requested Codex path is not the unique global npm-native executable.")
    completed = _run(
        [expected, "--version"], timeout=20, env=_pinned_npm_environment(npm)
    )
    if _version_tuple(completed.stdout.strip()) != _version_tuple(version):
        raise InstallError("Codex package and native executable versions differ.")
    return expected


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise InstallError(f"Could not load immutable runtime: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        raise InstallError(f"Immutable runtime could not be loaded: {path}") from error
    return module


def _validate_full_cli_trust(guard_path: Path, codex_path: Path) -> Mapping[str, Any]:
    guard = _load_module(guard_path, f"codex_reset_guard_install_{uuid.uuid4().hex}")
    helper = getattr(guard, "validate_linux_cli_trust", None)
    if not callable(helper):
        raise InstallError("The guard does not provide Linux npm provenance validation.")
    try:
        result = helper(codex_path)
    except Exception as error:
        raise InstallError("Codex npm provenance validation failed.") from error
    if not isinstance(result, Mapping):
        raise InstallError("Codex npm provenance validation returned an invalid result.")
    required = {"path", "version", "sha256", "signerSubject"}
    if set(result) != required:
        raise InstallError("Codex npm provenance validation returned an unknown schema.")
    if Path(str(result["path"])).resolve() != codex_path.resolve():
        raise InstallError("Codex npm provenance validation returned another executable.")
    if not re.fullmatch(r"[0-9a-f]{64}", str(result["sha256"])):
        raise InstallError("Codex npm provenance validation returned an invalid hash.")
    if not isinstance(result["signerSubject"], str) or not result["signerSubject"]:
        raise InstallError("Codex npm provenance identity is missing.")
    return result


def _validate_full_cli_trust_pinned(
    guard_path: Path, codex_path: Path, npm: Path
) -> Mapping[str, Any]:
    with _pinned_npm_process_environment(npm):
        return _validate_full_cli_trust(guard_path, codex_path)


def _validate_policy_with_manager(manager_path: Path, policy: Mapping[str, Any]) -> None:
    manager = _load_module(
        manager_path, f"codex_reset_manager_install_{uuid.uuid4().hex}"
    )
    validator = getattr(manager, "_validate_policy", None)
    if not callable(validator):
        raise InstallError("The manager does not expose its strict policy validator.")
    try:
        validator(policy)
    except Exception as error:
        raise InstallError("The manager rejected the installed policy schema.") from error


def _time_synchronized() -> None:
    completed = _run(
        ["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=20
    )
    if completed.stdout.strip() != "yes":
        raise InstallError("Linux system time is not synchronized.")


def _unit_quote(value: str | os.PathLike[str]) -> str:
    text = os.fspath(value)
    if not text or any(character in text for character in "\x00\r\n"):
        raise InstallError("A systemd unit argument is empty or contains a control character.")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _unit_scalar_path(value: str | os.PathLike[str]) -> str:
    """Render a path directive accepted by old and new systemd parsers."""
    text = os.fspath(value)
    if (
        not text
        or not text.startswith("/")
        or ".." in text.split("/")
        or any(
            character.isspace()
            or ord(character) < 0x20
            or ord(character) == 0x7F
            or character in "\\\"'%"
            for character in text
        )
    ):
        raise InstallError(
            "A systemd scalar path must be absolute and contain no whitespace, "
            "control characters, quotes, backslashes, or percent signs."
        )
    return text


def _runtime_npm(runtime: RuntimeFiles) -> Path:
    if runtime.npm is None:
        raise InstallError("The Linux manager runtime has no pinned npm launcher.")
    return runtime.npm


def _manager_service(runtime: RuntimeFiles, layout: Layout) -> bytes:
    assert runtime.manager is not None
    npm = _runtime_npm(runtime)
    arguments = " ".join(
        _unit_quote(item)
        for item in (
            runtime.python,
            "-I",
            runtime.manager,
            "--root",
            layout.root,
            "sync",
            "--scheduled",
        )
    )
    return (
        "[Unit]\n"
        "Description=Codex usage-limit reset manager synchronization\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={arguments}\n"
        f"WorkingDirectory={_unit_scalar_path(layout.root)}\n"
        f"Environment={_unit_quote('CODEX_HOME=' + str(runtime.codex_home))}\n"
        f"Environment={_unit_quote('CODEX_RESET_NPM=' + str(npm))} "
        f"{_unit_quote('PATH=' + _deterministic_path(npm))}\n"
        "UMask=0077\n"
        "NoNewPrivileges=yes\n"
        # A changed CLI may require a full npm provenance audit followed by a
        # child one-shot installation, with cleanup headroom on either path.
        "TimeoutStartSec=30min\n"
    ).encode()


def _manager_timer() -> bytes:
    return (
        "[Unit]\n"
        "Description=Check Codex usage-limit resets every 30 minutes\n\n"
        "[Timer]\n"
        "OnStartupSec=1min\n"
        "OnUnitActiveSec=30min\n"
        "Persistent=true\n"
        "AccuracySec=1s\n"
        "RandomizedDelaySec=0\n"
        "WakeSystem=false\n"
        f"Unit={MANAGER_SERVICE}\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    ).encode()


def _consume_service(runtime: RuntimeFiles, layout: Layout, manifest: Path) -> bytes:
    arguments = " ".join(
        _unit_quote(item)
        for item in (
            runtime.python,
            "-I",
            runtime.guard,
            "run",
            "--manifest",
            manifest,
            "--live",
        )
    )
    return (
        "[Unit]\n"
        "Description=Use one exact Codex usage-limit reset\n"
        "RefuseManualStart=yes\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={arguments}\n"
        f"WorkingDirectory={_unit_scalar_path(layout.root)}\n"
        f"Environment={_unit_quote('PATH=' + LIVE_SYSTEM_PATH)}\n"
        "Restart=no\n"
        "TimeoutStartSec=10min\n"
        "UMask=0077\n"
        "NoNewPrivileges=yes\n"
    ).encode()


def _parse_utc(value: object, field: str) -> dt.datetime:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise InstallError(f"Manifest {field} is not strict UTC.")
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise InstallError(f"Manifest {field} is invalid.") from error


def _consume_timer(service_name: str, trigger: dt.datetime) -> bytes:
    calendar = trigger.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        "[Unit]\n"
        "Description=Schedule one exact Codex usage-limit reset\n\n"
        "[Timer]\n"
        f"OnCalendar={calendar}\n"
        "Persistent=true\n"
        "RemainAfterElapse=true\n"
        "AccuracySec=1s\n"
        "RandomizedDelaySec=0\n"
        "WakeSystem=false\n"
        f"Unit={service_name}\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    ).encode()


def _systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["systemctl", "--user", *arguments], timeout=30, check=check)


def _unit_state(name: str) -> UnitState:
    enabled = _systemctl("is-enabled", "--quiet", name, check=False).returncode == 0
    active = _systemctl("is-active", "--quiet", name, check=False).returncode == 0
    return UnitState(enabled=enabled, active=active)


def _loaded_unit_properties(name: str) -> dict[str, str]:
    completed = _systemctl(
        "show",
        "--no-pager",
        "--property=LoadState",
        "--property=FragmentPath",
        "--property=DropInPaths",
        "--property=NeedDaemonReload",
        name,
    )
    properties: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in properties:
            raise InstallError(f"systemd returned duplicate metadata for {name}.")
        properties[key] = value
    if set(properties) != {
        "LoadState",
        "FragmentPath",
        "DropInPaths",
        "NeedDaemonReload",
    }:
        raise InstallError(f"systemd returned incomplete metadata for {name}.")
    return properties


def _validate_loaded_unit(name: str, path: Path, expected: bytes) -> None:
    properties = _loaded_unit_properties(name)
    if (
        properties["LoadState"] != "loaded"
        or Path(properties["FragmentPath"]).resolve() != path.resolve()
        or properties["DropInPaths"] != ""
        or properties["NeedDaemonReload"] != "no"
        or path.is_symlink()
        or path.read_bytes() != expected
    ):
        raise InstallError(f"Loaded systemd unit contract differs from the installed file: {name}")


def _restore_unit_state(name: str, state: UnitState) -> None:
    if state.enabled:
        _systemctl("enable", name)
    else:
        _systemctl("disable", name, check=False)
    if state.active:
        _systemctl("start", name)
    else:
        _systemctl("stop", name, check=False)


def _wait_manager_inactive(
    *,
    timeout: float = 30.0,
    now: Any = time.monotonic,
    sleeper: Any = time.sleep,
) -> None:
    deadline = now() + timeout
    while _systemctl("is-active", "--quiet", MANAGER_SERVICE, check=False).returncode == 0:
        if now() >= deadline:
            raise InstallError(
                "ManagerSync is still running; retry the update after it finishes."
            )
        sleeper(0.1)


@contextlib.contextmanager
def _installer_locks(layout: Layout) -> Iterator[None]:
    import fcntl

    streams = []
    try:
        for filename in ("controller.lock", "dispatch.lock"):
            path = layout.state / filename
            stream = path.open("a+b")
            if path.stat().st_size == 0:
                stream.write(b"0")
                stream.flush()
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                stream.close()
                raise InstallError(f"Another process owns {filename}.") from error
            streams.append(stream)
        yield
    finally:
        for stream in reversed(streams):
            with contextlib.suppress(OSError):
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()


def _assert_parent_locks_held(layout: Layout) -> None:
    """Prove child mode was called inside the controller's lock ordering."""
    import fcntl

    for filename in ("controller.lock", "dispatch.lock"):
        path = layout.state / filename
        if not path.is_file():
            raise InstallError(f"Manager child mode requires an owned {filename}.")
        with path.open("a+b") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                raise InstallError(f"Manager child mode requires an owned {filename}.")


def _manifest_inventory(layout: Layout) -> list[tuple[Path, dict[str, Any]]]:
    result = []
    if not layout.manifests.exists():
        return result
    for path in sorted(layout.manifests.glob("*.json")):
        manifest = _read_json(path, "Manifest")
        state = manifest.get("state")
        if not isinstance(state, str):
            raise InstallError(f"Manifest state is invalid: {path}")
        result.append((path.resolve(), manifest))
    return result


def _active_manifest_snapshot(
    layout: Layout, python: Path
) -> tuple[dict[Path, str], dict[str, UnitState]]:
    active = [(path, value) for path, value in _manifest_inventory(layout) if value["state"] not in TERMINAL_STATES]
    if len(active) > 1:
        raise InstallError("More than one nonterminal manifest exists.")
    snapshots: dict[Path, str] = {}
    states: dict[str, UnitState] = {}
    for path, manifest in active:
        task = manifest.get("task")
        task_name = task.get("name") if isinstance(task, Mapping) else None
        if not isinstance(task_name, str) or CONSUME_UNIT_RE.fullmatch(task_name) is None:
            raise InstallError("The active manifest has an invalid Linux timer name.")
        service = task_name.removesuffix(".timer") + ".service"
        service_path = layout.unit_dir / service
        if not service_path.is_file() or service_path.is_symlink():
            raise InstallError("The active one-shot service is incomplete.")
        if f"ExecStart={_unit_quote(python)} ".encode() not in service_path.read_bytes():
            raise InstallError("The selected Python differs from the active one-shot runtime.")
        for item in (path, layout.unit_dir / task_name, layout.unit_dir / service):
            if not item.is_file() or item.is_symlink():
                raise InstallError("The active one-shot installation is incomplete.")
            snapshots[item.resolve()] = _sha256(item)
        states[task_name] = _unit_state(task_name)
    return snapshots, states


def _assert_snapshots_unchanged(
    snapshots: Mapping[Path, str], states: Mapping[str, UnitState]
) -> None:
    for path, digest in snapshots.items():
        if not path.is_file() or _sha256(path) != digest:
            raise InstallError("An active one-shot changed during installation.")
    for name, expected in states.items():
        if _unit_state(name) != expected:
            raise InstallError("An active one-shot timer state changed during installation.")


def _manager_environment(layout: Layout, runtime: RuntimeFiles) -> dict[str, str]:
    npm = _runtime_npm(runtime)
    environment = _pinned_npm_environment(npm)
    environment["CODEX_RESET_MANAGER_ROOT"] = str(layout.root)
    environment["CODEX_RESET_MANAGER_RUNTIME_INSTALLER"] = str(runtime.installer)
    environment["CODEX_RESET_MANAGER_RUNTIME_GUARD"] = str(runtime.guard)
    environment["CODEX_HOME"] = str(runtime.codex_home)
    return environment


def _invoke_manager(
    runtime: RuntimeFiles, layout: Layout, arguments: Sequence[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    if runtime.manager is None:
        raise InstallError("Manager runtime is unavailable.")
    return _run(
        [runtime.python, "-I", runtime.manager, "--root", layout.root, *arguments],
        timeout=180,
        env=_manager_environment(layout, runtime),
        check=check,
    )


def _wrapper(runtime: RuntimeFiles, layout: Layout) -> bytes:
    assert runtime.manager is not None
    npm = _runtime_npm(runtime)
    command = " ".join(
        shlex.quote(os.fspath(item))
        for item in (
            runtime.python,
            "-I",
            runtime.manager,
            "--root",
            layout.root,
        )
    )
    return (
        f"#!/bin/sh\nexport CODEX_HOME={shlex.quote(str(runtime.codex_home))}\n"
        f"export CODEX_RESET_NPM={shlex.quote(str(npm))}\n"
        f"export PATH={shlex.quote(_deterministic_path(npm))}\n"
        f"exec {command} \"$@\"\n"
    ).encode()


def _rollback_normal_install(
    layout: Layout,
    snapshots: Mapping[Path, FileSnapshot],
    prior_state: UnitState,
    active_files: Mapping[Path, str],
    active_states: Mapping[str, UnitState],
    manager_path: Path,
) -> None:
    policy_path = layout.state / "policy.json"
    active_unchanged = True
    try:
        _assert_snapshots_unchanged(active_files, active_states)
    except InstallError:
        active_unchanged = False
    errors: list[str] = []
    for path, snapshot in snapshots.items():
        if path == policy_path and not active_unchanged:
            continue
        try:
            _restore(path, snapshot)
        except Exception as error:
            errors.append(f"{path.name}: {type(error).__name__}")
    if not active_unchanged:
        # Restoring a policy snapshot with a stale currentJob reference could
        # allow succession after a one-shot changed concurrently.  Keep the
        # restored runtime pins, but force a schema-valid paused/block state.
        try:
            policy_snapshot = snapshots[policy_path]
            if policy_snapshot.exists:
                safe_policy = json.loads(policy_snapshot.data)
            else:
                safe_policy = _read_json(policy_path, "Partially installed policy")
            if not isinstance(safe_policy, dict):
                raise InstallError("Rollback policy is not an object.")
            timestamp = dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            safe_policy["enabled"] = False
            safe_policy["currentJob"] = None
            safe_policy["blocked"] = {
                "code": "INSTALL_ROLLBACK_INCOMPLETE",
                "atUtc": timestamp,
            }
            safe_policy["updatedAtUtc"] = timestamp
            _validate_policy_with_manager(manager_path, safe_policy)
            rendered = (json.dumps(safe_policy, indent=2, sort_keys=True) + "\n").encode()
            _atomic_write(policy_path, rendered, 0o600)
            if policy_path.read_bytes() != rendered:
                raise InstallError("Safe rollback policy readback changed.")
        except Exception as error:
            errors.append(f"safe blocked policy: {type(error).__name__}")
    try:
        _systemctl("daemon-reload")
        _restore_unit_state(MANAGER_TIMER, prior_state)
    except Exception as error:
        errors.append(f"ManagerSync state: {type(error).__name__}")
    if not active_unchanged:
        errors.append("active one-shot changed; automation was paused and blocked")
    if errors:
        raise InstallError("Installation rollback was incomplete: " + "; ".join(errors))


def _install_normal(args: argparse.Namespace) -> dict[str, Any]:
    _require_linux_user_session()
    layout = _resolve_layout(args.install_root)
    _unit_scalar_path(layout.root)
    _prepare_directories(layout)
    python = _validate_python(args.python_path)
    npm = _select_npm(layout, args.npm_path)
    codex = _discover_codex(args.codex_path, npm)
    codex_home = _select_codex_home(layout, args.codex_home)
    _time_synchronized()

    base = Path(__file__).resolve().parent
    source_guard = _canonical_file(args.source_guard or base / "codex_reset_guard.py", "Guard source", suffix=".py")
    source_manager = _canonical_file(args.source_manager or base / "codex_reset_manager.py", "Manager source", suffix=".py")
    source_installer = _canonical_file(Path(__file__), "Installer source", suffix=".py")
    guard = _install_immutable(source_guard, layout.runners, "codex_reset_guard")
    manager = _install_immutable(source_manager, layout.runners, "codex_reset_manager")
    installer = _install_immutable(source_installer, layout.installers, "install_linux")
    runtime = RuntimeFiles(
        python, codex, codex_home, guard, manager, installer, npm=npm
    )

    # A fresh or changed CLI must be proven before it can be pinned into a
    # controller policy.  The immutable guard owns the npm/Sigstore parser.
    trusted_cli = _validate_full_cli_trust_pinned(guard, codex, npm)

    policy_path = layout.state / "policy.json"
    service_path = layout.unit_dir / MANAGER_SERVICE
    timer_path = layout.unit_dir / MANAGER_TIMER
    paths = (policy_path, service_path, timer_path, layout.wrapper)
    snapshots = {path: _snapshot(path) for path in paths}
    prior_state = _unit_state(MANAGER_TIMER)
    active_files, active_states = _active_manifest_snapshot(layout, python)

    _systemctl("stop", MANAGER_TIMER, check=False)
    try:
        _wait_manager_inactive()
        # Bootstrap through the manager before retaining its own locks; a
        # manager process must acquire those same locks to write a valid
        # schema.  ManagerSync is stopped and active one-shot bytes/state are
        # checked on both sides of this short handoff.
        _assert_snapshots_unchanged(active_files, active_states)
        _invoke_manager(runtime, layout, ["status", "--json"])
        with _installer_locks(layout):
            _assert_snapshots_unchanged(active_files, active_states)
            policy = _read_json(policy_path, "Manager policy")
            if policy.get("runtimeInstaller") != str(installer) or policy.get("runtimeGuard") != str(guard):
                raise InstallError("Manager policy did not pin the installed immutable runtimes.")
            if snapshots[policy_path].exists:
                old_policy = json.loads(snapshots[policy_path].data)
                if policy.get("enabled") is not old_policy.get("enabled"):
                    raise InstallError("Installation changed the existing automatic-use state.")
            elif policy.get("enabled") is not False:
                raise InstallError("A new Linux installation must start paused.")
            policy["approvedCli"] = {
                "codexExe": str(Path(str(trusted_cli["path"])).resolve()),
                "codexVersion": str(trusted_cli["version"]),
                "codexSha256": str(trusted_cli["sha256"]).lower(),
                "signerSubject": str(trusted_cli["signerSubject"]),
                "approvedAtUtc": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            policy["updatedAtUtc"] = dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            _validate_policy_with_manager(manager, policy)
            rendered_policy = (json.dumps(policy, indent=2, sort_keys=True) + "\n").encode()
            _atomic_write(
                policy_path,
                rendered_policy,
                0o600,
            )
            if policy_path.read_bytes() != rendered_policy:
                raise InstallError("Manager policy readback differs from the committed bytes.")
            _validate_policy_with_manager(manager, _read_json(policy_path, "Manager policy"))
            expected_service = _manager_service(runtime, layout)
            expected_timer = _manager_timer()
            _atomic_write(service_path, expected_service, 0o600)
            _atomic_write(timer_path, expected_timer, 0o600)
            _atomic_write(layout.wrapper, _wrapper(runtime, layout), 0o700)
            _systemctl("daemon-reload")
            _validate_loaded_unit(MANAGER_SERVICE, service_path, expected_service)
            _validate_loaded_unit(MANAGER_TIMER, timer_path, expected_timer)
            _systemctl("enable", "--now", MANAGER_TIMER)
            if _unit_state(MANAGER_TIMER) != UnitState(enabled=True, active=True):
                raise InstallError("ManagerSync timer was not enabled and active.")
            _assert_snapshots_unchanged(active_files, active_states)
    except Exception as installation_error:
        try:
            _rollback_normal_install(
                layout,
                snapshots,
                prior_state,
                active_files,
                active_states,
                manager,
            )
        except InstallError as rollback_error:
            raise rollback_error from installation_error
        raise

    return {
        "installRoot": str(layout.root),
        "managerTimer": MANAGER_TIMER,
        "managerRuntime": str(manager),
        "guardRuntime": str(guard),
        "automaticUse": "paused" if not policy["enabled"] else "enabled",
    }


def _strict_child_policy(layout: Layout, installer: Path, guard: Path, codex: Path) -> dict[str, Any]:
    policy = _read_json(layout.state / "policy.json", "Manager policy")
    if policy.get("enabled") is not True or policy.get("blocked") is not None:
        raise InstallError("Child enrollment requires enabled, unblocked automatic operation.")
    if Path(str(policy.get("runtimeInstaller", ""))).resolve() != installer.resolve():
        raise InstallError("Executing installer does not match the policy runtime pin.")
    if Path(str(policy.get("runtimeGuard", ""))).resolve() != guard.resolve():
        raise InstallError("Guard runtime does not match the policy runtime pin.")
    approved = policy.get("approvedCli")
    if not isinstance(approved, Mapping) or set(approved) != {
        "codexExe",
        "codexVersion",
        "codexSha256",
        "signerSubject",
        "approvedAtUtc",
    }:
        raise InstallError("Manager policy has no valid approved CLI pin.")
    if Path(str(approved["codexExe"])).resolve() != codex.resolve():
        raise InstallError("Child Codex path differs from the approved CLI pin.")
    signer = str(approved["signerSubject"])
    signer_match = re.fullmatch(
        r"npm-provenance:repo=openai/codex;workflow=\.github/workflows/rust-release\.yml;"
        r"tag=rust-v(?P<version>\d+\.\d+\.\d+);commit=[0-9a-f]{40}",
        signer,
    )
    approved_version = str(approved["codexVersion"]).removeprefix("codex-cli ")
    if (
        signer_match is None
        or signer_match.group("version") != approved_version
        or re.fullmatch(r"\d+\.\d+\.\d+", approved_version) is None
        or not re.fullmatch(r"[0-9a-f]{64}", str(approved["codexSha256"]))
    ):
        raise InstallError("The cached npm provenance identity is not canonical.")
    _parse_utc(approved["approvedAtUtc"], "approvedAtUtc")
    return policy


def _observe_approved_cli(guard: ModuleType, policy: Mapping[str, Any], codex: Path) -> None:
    helper = getattr(guard, "observe_cli_pin", None)
    if not callable(helper):
        raise InstallError("The guard does not provide the Linux CLI pin observer.")
    try:
        observed = helper(codex)
    except Exception as error:
        raise InstallError("The approved Codex binary could not be observed.") from error
    if not isinstance(observed, Mapping):
        raise InstallError("The CLI pin observer returned an invalid result.")
    approved = policy["approvedCli"]
    expected = {
        "path": str(Path(str(approved["codexExe"])).resolve()),
        "version": str(approved["codexVersion"]),
        "sha256": str(approved["codexSha256"]).lower(),
    }
    actual = {
        "path": str(Path(str(observed.get("path", ""))).resolve()),
        "version": str(observed.get("version", "")),
        "sha256": str(observed.get("sha256", "")).lower(),
    }
    if actual != expected:
        raise InstallError("The Codex CLI no longer matches its approved policy pin.")


def _invoke_guard_cli(
    runtime: RuntimeFiles, arguments: Sequence[str], *, timeout: float = 120, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return _run(
        [runtime.python, "-I", runtime.guard, *arguments],
        timeout=timeout,
        check=check,
    )


def _install_child(args: argparse.Namespace) -> dict[str, Any]:
    _require_linux_user_session()
    if not all((args.install_root, args.python_path, args.codex_path, args.runtime_guard)):
        raise InstallError(
            "--manager-child-only requires --install-root, --python-path, --codex-path, and --runtime-guard."
        )
    layout = _resolve_layout(args.install_root)
    _unit_scalar_path(layout.root)
    _assert_parent_locks_held(layout)
    _prepare_directories(layout)
    npm = _require_child_npm_context(layout)
    inherited_codex_home = _validate_codex_home(
        Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    )
    pinned_codex_home = _existing_codex_home_pin(layout)
    if pinned_codex_home is None or inherited_codex_home != _validate_codex_home(pinned_codex_home):
        raise InstallError("Child CODEX_HOME differs from the ManagerSync environment pin.")
    python = _validate_python(args.python_path)
    if python.resolve() != Path(sys.executable).resolve():
        raise InstallError("Child installer must run under the explicitly pinned Python runtime.")
    codex = _discover_codex(args.codex_path, npm)
    guard_path = _canonical_file(args.runtime_guard, "Immutable guard", suffix=".py")
    installer = _canonical_file(Path(__file__), "Executing installer", suffix=".py")
    if guard_path.parent != layout.runners or installer.parent != layout.installers:
        raise InstallError("Child mode requires content-addressed installed runtimes.")
    if re.fullmatch(r"codex_reset_guard-[0-9a-f]{64}\.py", guard_path.name) is None:
        raise InstallError("Guard is not an immutable content-addressed runtime.")
    if re.fullmatch(r"install_linux-[0-9a-f]{64}\.py", installer.name) is None:
        raise InstallError("Installer is not an immutable content-addressed runtime.")
    if _sha256(guard_path) != guard_path.stem.rsplit("-", 1)[1] or _sha256(installer) != installer.stem.rsplit("-", 1)[1]:
        raise InstallError("A content-addressed runtime hash does not match its filename.")

    policy = _strict_child_policy(layout, installer, guard_path, codex)
    guard = _load_module(guard_path, f"codex_reset_guard_child_{uuid.uuid4().hex}")
    _observe_approved_cli(guard, policy, codex)
    _time_synchronized()
    if any(value["state"] not in TERMINAL_STATES for _, value in _manifest_inventory(layout)):
        raise InstallError("A nonterminal one-shot already exists.")

    manifest_path = layout.manifests / f"job-{uuid.uuid4()}.json"
    runtime = RuntimeFiles(
        python, codex, inherited_codex_home, guard_path, None, installer, npm=npm
    )
    task_name: str | None = None
    service_name: str | None = None
    try:
        # The guard internally validates the already-approved pin without
        # exposing a command-line trust bypass.  This seam is intentionally
        # callable only in this in-process, policy-validated child path.
        enroll = getattr(guard, "enroll_with_approved_cli_pin", None)
        try:
            codex_home = inherited_codex_home
            if callable(enroll):
                enroll(
                    codex_path=codex,
                    codex_home=codex_home,
                    manifest_path=manifest_path,
                    approved_pin=dict(policy["approvedCli"]),
                )
            else:
                internal_enroll = getattr(guard, "_enroll", None)
                if not callable(internal_enroll):
                    raise InstallError("The guard does not provide approved-pin enrollment.")
                internal_enroll(
                    codex,
                    codex_home,
                    manifest_path,
                    force=False,
                    trusted_binary_info=dict(policy["approvedCli"]),
                )
        except Exception as error:
            if isinstance(error, InstallError):
                raise
            raise InstallError("Guard enrollment failed.") from error
        manifest = _read_json(manifest_path, "Enrolled manifest")
        if manifest.get("armed") is not False or manifest.get("state") != "UNARMED":
            raise InstallError("Enrollment did not produce one unarmed manifest.")
        target = manifest.get("target")
        schedule = manifest.get("schedule")
        if not isinstance(target, Mapping) or not isinstance(schedule, Mapping):
            raise InstallError("Enrolled manifest schema is incomplete.")
        credit_hash = str(target.get("creditIdSha256", ""))
        job_id = uuid.UUID(str(manifest.get("jobId", "")))
        if not re.fullmatch(r"[0-9a-f]{64}", credit_hash):
            raise InstallError("Enrolled target hash is invalid.")
        trigger = _parse_utc(schedule.get("triggerAtUtc"), "triggerAtUtc")
        cutoff = _parse_utc(schedule.get("cutoffAtUtc"), "cutoffAtUtc")
        if cutoff <= trigger or trigger <= dt.datetime.now(UTC) + dt.timedelta(minutes=10):
            raise InstallError("Less than ten minutes remain before the one-shot trigger.")
        base_name = f"codex-reset-consume-{credit_hash[:12]}-{job_id.hex[:8]}"
        task_name = base_name + ".timer"
        service_name = base_name + ".service"
        timer_path = layout.unit_dir / task_name
        service_path = layout.unit_dir / service_name
        if timer_path.exists() or service_path.exists():
            raise InstallError("The derived one-shot systemd unit already exists.")
        _atomic_write(service_path, _consume_service(runtime, layout, manifest_path), 0o600)
        _atomic_write(timer_path, _consume_timer(service_name, trigger), 0o600)
        _systemctl("daemon-reload")
        _validate_loaded_unit(
            service_name,
            service_path,
            _consume_service(runtime, layout, manifest_path),
        )
        _validate_loaded_unit(
            task_name,
            timer_path,
            _consume_timer(service_name, trigger),
        )
        _systemctl("enable", "--now", task_name)
        if _unit_state(task_name) != UnitState(enabled=True, active=True):
            raise InstallError("The one-shot timer was not enabled and active.")
        validate_contract = getattr(guard, "_validate_scheduled_task_contract", None)
        if not callable(validate_contract):
            raise InstallError("The guard does not provide the Linux task contract validator.")
        try:
            validate_contract(task_name, manifest_path, manifest)
        except Exception as error:
            raise InstallError("The guard rejected the loaded one-shot task contract.") from error
        _invoke_guard_cli(
            runtime,
            ["arm", "--manifest", str(manifest_path), "--task-name", task_name],
        )
        armed = _read_json(manifest_path, "Armed manifest")
        if armed.get("armed") is not True or armed.get("state") != "ARMED":
            raise InstallError("The guard did not arm the one-shot manifest.")
        if armed.get("task", {}).get("name") != task_name:
            raise InstallError("The armed manifest did not pin the exact timer unit.")
        return {
            "manifestPath": str(manifest_path.resolve()),
            "taskName": task_name,
            "jobId": str(job_id),
        }
    except Exception as installation_error:
        cleanup_errors: list[str] = []
        if task_name:
            try:
                _systemctl("disable", "--now", task_name, check=False)
                if _unit_state(task_name) != UnitState(enabled=False, active=False):
                    raise InstallError("the partial timer remained enabled or active")
            except Exception as error:
                cleanup_errors.append(f"timer cleanup: {type(error).__name__}")
        if manifest_path.exists():
            try:
                disarmed = _invoke_guard_cli(
                    runtime,
                    ["disarm", "--manifest", str(manifest_path)],
                    check=False,
                )
                manifest_after = _read_json(manifest_path, "Disarmed manifest")
                if disarmed.returncode != 0 or manifest_after.get("state") != "DISARMED":
                    raise InstallError("the partial manifest did not become DISARMED")
            except Exception as error:
                cleanup_errors.append(f"manifest disarm: {type(error).__name__}")
        # Preserve the manifest and disabled unit files as an audit record.
        try:
            _systemctl("daemon-reload")
        except Exception as error:
            cleanup_errors.append(f"daemon reload: {type(error).__name__}")
        if cleanup_errors:
            raise InstallError(
                "Child installation failed and cleanup was incomplete: "
                + "; ".join(cleanup_errors)
            ) from installation_error
        raise


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install the Codex usage-limit auto-reset manager for systemd --user"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    parser.add_argument("--manager-child-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--install-root", type=Path)
    parser.add_argument("--python-path", type=Path)
    parser.add_argument("--npm-path", type=Path)
    parser.add_argument("--codex-path", type=Path)
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--source-guard", type=Path)
    parser.add_argument("--source-manager", type=Path)
    parser.add_argument("--runtime-guard", type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.manager_child_only:
            if (
                args.source_guard is not None
                or args.source_manager is not None
                or args.npm_path is not None
            ):
                raise InstallError(
                    "Child mode does not accept source runtime paths or --npm-path."
                )
            result = _install_child(args)
        else:
            if args.runtime_guard is not None:
                raise InstallError("--runtime-guard is reserved for child mode.")
            result = _install_normal(args)
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
        return 0
    except InstallError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
