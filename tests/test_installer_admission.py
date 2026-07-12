from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
import hashlib
import html
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import codex_reset_manager as manager


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_SOURCE = PROJECT_ROOT / "install.ps1"


HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:INSTALLER_SOURCE,
    [ref] $tokens,
    [ref] $parseErrors
)
if ($parseErrors.Count -ne 0) { throw 'Installer source did not parse.' }
foreach ($name in @('Resolve-CanonicalFile', 'Get-ManifestInventory', 'Assert-ManagerChildAdmission')) {
    $node = $ast.FindAll({
        param($candidate)
        $candidate -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $candidate.Name -eq $name
    }, $true) | Select-Object -First 1
    if ($null -eq $node) { throw "Missing installer function: $name" }
    . ([scriptblock]::Create($node.Extent.Text))
}
$TerminalManifestStates = @(
    'SUCCEEDED', 'NO_ACTION', 'FAILED', 'INDETERMINATE', 'DISARMED',
    'CLEANED', 'SUPERSEDED_CLI', 'CANCELLED'
)
$observed = $env:OBSERVED_CODEX | ConvertFrom-Json -Depth 20
Assert-ManagerChildAdmission `
    -PolicyPath $env:POLICY_PATH `
    -ManifestsDirectory $env:MANIFESTS_DIRECTORY `
    -InstallRoot $env:INSTALL_ROOT `
    -ExecutingInstaller $env:EXECUTING_INSTALLER `
    -GuardPath $env:GUARD_PATH `
    -GuardSha256 $env:GUARD_SHA256 `
    -ObservedCodex $observed
"""


MANAGER_TASK_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:INSTALLER_SOURCE,
    [ref] $tokens,
    [ref] $parseErrors
)
if ($parseErrors.Count -ne 0) { throw 'Installer source did not parse.' }
foreach ($name in @('Resolve-AccountSid', 'Get-RequiredXmlText', 'Assert-ManagerTask')) {
    $node = $ast.FindAll({
        param($candidate)
        $candidate -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $candidate.Name -eq $name
    }, $true) | Select-Object -First 1
    if ($null -eq $node) { throw "Missing installer function: $name" }
    . ([scriptblock]::Create($node.Extent.Text))
}
$ManagerTaskName = 'ManagerSync'
$TaskFolder = '\CodexResetCredit\'
$ManagerSyncIntervalMinutes = 30
function Get-ScheduledTask {
    [pscustomobject]@{
        State = 'Ready'
        Principal = [pscustomobject]@{
            RunLevel = 'Limited'
            LogonType = 'Interactive'
            UserId = 'S-1-5-21-12345'
        }
    }
}
function Export-ScheduledTask { return $env:MANAGER_TASK_XML }
Assert-ManagerTask `
    -ExpectedUser 'S-1-5-21-12345' `
    -ExpectedPython $env:EXPECTED_PYTHON `
    -ExpectedArguments $env:EXPECTED_ARGUMENTS `
    -ExpectedWorkingDirectory $env:EXPECTED_WORKING
"""


FILE_ROLLBACK_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:INSTALLER_SOURCE,
    [ref] $tokens,
    [ref] $parseErrors
)
if ($parseErrors.Count -ne 0) { throw 'Installer source did not parse.' }
foreach ($name in @('Get-FileByteSnapshot', 'Restore-FileByteSnapshot')) {
    $node = $ast.FindAll({
        param($candidate)
        $candidate -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $candidate.Name -eq $name
    }, $true) | Select-Object -First 1
    if ($null -eq $node) { throw "Missing installer function: $name" }
    . ([scriptblock]::Create($node.Extent.Text))
}
$snapshot = Get-FileByteSnapshot -Path $env:SNAPSHOT_PATH
[IO.File]::WriteAllBytes($env:SNAPSHOT_PATH, [byte[]] @(9, 8, 7, 6))
Restore-FileByteSnapshot -Path $env:SNAPSHOT_PATH -Snapshot $snapshot
"""


TASK_ROLLBACK_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:INSTALLER_SOURCE,
    [ref] $tokens,
    [ref] $parseErrors
)
if ($parseErrors.Count -ne 0) { throw 'Installer source did not parse.' }
foreach ($name in @('Get-ManagerTaskSnapshot', 'Restore-ManagerTaskSnapshot')) {
    $node = $ast.FindAll({
        param($candidate)
        $candidate -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $candidate.Name -eq $name
    }, $true) | Select-Object -First 1
    if ($null -eq $node) { throw "Missing installer function: $name" }
    . ([scriptblock]::Create($node.Extent.Text))
}
$ManagerTaskName = 'ManagerSync'
$TaskFolder = '\CodexResetCredit\'
$script:TaskExists = $env:PRIOR_TASK_EXISTS -eq 'true'
$script:TaskXml = if ($script:TaskExists) { $env:PRIOR_TASK_XML } else { $null }
function Get-ScheduledTask {
    if ($script:TaskExists) { return [pscustomobject]@{ State = 'Ready' } }
    return $null
}
function Export-ScheduledTask {
    if (-not $script:TaskExists) { throw 'No task exists.' }
    return $script:TaskXml
}
function Register-ScheduledTask {
    param($TaskName, $TaskPath, $Xml, [switch] $Force)
    $script:TaskExists = $true
    $script:TaskXml = [string] $Xml
}
function Unregister-ScheduledTask {
    param($TaskName, $TaskPath, [switch] $Confirm, $ErrorAction)
    $script:TaskExists = $false
    $script:TaskXml = $null
}
function Ensure-TaskFolder { param($Path) }

$snapshot = Get-ManagerTaskSnapshot
$script:TaskExists = $true
$script:TaskXml = '<Task>replacement</Task>'
Restore-ManagerTaskSnapshot -Snapshot $snapshot
[pscustomobject]@{
    Exists = $script:TaskExists
    Xml = $script:TaskXml
} | ConvertTo-Json -Compress
"""


ACTIVE_ONE_SHOT_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:INSTALLER_SOURCE,
    [ref] $tokens,
    [ref] $parseErrors
)
if ($parseErrors.Count -ne 0) { throw 'Installer source did not parse.' }
foreach ($name in @(
    'Get-RequiredXmlText', 'Get-FileByteSnapshot', 'Get-Sha256Hex',
    'Get-ManifestInventory', 'Get-OneShotTaskContract', 'Get-ActiveOneShotSnapshot',
    'Assert-ActiveOneShotUnchanged'
)) {
    $node = $ast.FindAll({
        param($candidate)
        $candidate -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $candidate.Name -eq $name
    }, $true) | Select-Object -First 1
    if ($null -eq $node) { throw "Missing installer function: $name" }
    . ([scriptblock]::Create($node.Extent.Text))
}
$TaskFolder = '\CodexResetCredit\'
$TerminalManifestStates = @(
    'SUCCEEDED', 'NO_ACTION', 'FAILED', 'INDETERMINATE', 'DISARMED',
    'CLEANED', 'SUPERSEDED_CLI', 'CANCELLED'
)
$script:TaskXml = $env:ACTIVE_TASK_XML
$script:TaskState = $env:ACTIVE_TASK_STATE
function Get-ScheduledTask {
    [pscustomobject]@{
        TaskName = 'Consume-test'
        TaskPath = '\CodexResetCredit\'
        State = $script:TaskState
    }
}
function Export-ScheduledTask { return $script:TaskXml }

if ($env:ACTIVE_EMPTY -eq 'true') {
    Remove-Item -LiteralPath $env:ACTIVE_MANIFEST -Force -ErrorAction SilentlyContinue
    $snapshot = Get-ActiveOneShotSnapshot -NonterminalInventory @() -ManifestDirectory $env:ACTIVE_MANIFEST_DIRECTORY
    if ([bool] $snapshot.Exists) { throw 'Empty inventory produced an active snapshot.' }
    if ($env:CREATE_ACTIVE_AFTER_EMPTY -eq 'true') {
        [IO.File]::WriteAllText($env:ACTIVE_MANIFEST, $env:ACTIVE_MANIFEST_JSON, [Text.Encoding]::UTF8)
    }
    Assert-ActiveOneShotUnchanged -Snapshot $snapshot
    return
}
$inventory = @([pscustomobject]@{ Path = $env:ACTIVE_MANIFEST })
$snapshot = Get-ActiveOneShotSnapshot -NonterminalInventory $inventory -ManifestDirectory $env:ACTIVE_MANIFEST_DIRECTORY
if ($env:MUTATE_ACTIVE_MANIFEST -eq 'true') {
    [IO.File]::AppendAllText($env:ACTIVE_MANIFEST, " ")
}
if ($env:MUTATE_ACTIVE_TASK -eq 'true') {
    $script:TaskXml = $script:TaskXml.Replace('pythonw.exe', 'other.exe')
}
Assert-ActiveOneShotUnchanged -Snapshot $snapshot
"""


LOCK_HOLDER_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:INSTALLER_SOURCE,
    [ref] $tokens,
    [ref] $parseErrors
)
foreach ($name in @('Enter-InstallerByteRangeLock', 'Exit-InstallerByteRangeLock')) {
    $node = $ast.FindAll({
        param($candidate)
        $candidate -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $candidate.Name -eq $name
    }, $true) | Select-Object -First 1
    . ([scriptblock]::Create($node.Extent.Text))
}
$stream = Enter-InstallerByteRangeLock -Path $env:LOCK_PATH -Description 'test lock'
try {
    Write-Output 'ready'
    [Console]::Out.Flush()
    Start-Sleep -Seconds 60
}
finally { Exit-InstallerByteRangeLock -Stream $stream }
"""


READY_MARKER_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:INSTALLER_SOURCE,
    [ref] $tokens,
    [ref] $parseErrors
)
$node = $ast.FindAll({
    param($candidate)
    $candidate -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $candidate.Name -eq 'Read-ManagerUiReadyMarker'
}, $true) | Select-Object -First 1
. ([scriptblock]::Create($node.Extent.Text))
$marker = Read-ManagerUiReadyMarker -Path $env:READY_MARKER_PATH
Write-Output $marker.ReadyAtUtc
Write-Output $marker.ReadyAtUtc.GetType().FullName
"""


PYTHON_VALIDATOR_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:INSTALLER_SOURCE,
    [ref] $tokens,
    [ref] $parseErrors
)
if ($parseErrors.Count -ne 0) { throw 'Installer source did not parse.' }
foreach ($name in @('Resolve-CanonicalFile', 'ConvertTo-CompatiblePythonRuntime')) {
    $node = $ast.FindAll({
        param($candidate)
        $candidate -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $candidate.Name -eq $name
    }, $true) | Select-Object -First 1
    if ($null -eq $node) { throw "Missing installer function: $name" }
    . ([scriptblock]::Create($node.Extent.Text))
}
$probe = $env:PYTHON_PROBE | ConvertFrom-Json -Depth 20
ConvertTo-CompatiblePythonRuntime -Path $env:PYTHON_PATH -Probe $probe |
    Select-Object Path, WindowlessPath, Version |
    ConvertTo-Json -Compress
"""


PYTHON_REAL_PROBE_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:INSTALLER_SOURCE,
    [ref] $tokens,
    [ref] $parseErrors
)
if ($parseErrors.Count -ne 0) { throw 'Installer source did not parse.' }
foreach ($name in @(
    'Resolve-CanonicalFile', 'Get-PythonRuntimeProbe',
    'ConvertTo-CompatiblePythonRuntime', 'Get-CompatiblePythonRuntime'
)) {
    $node = $ast.FindAll({
        param($candidate)
        $candidate -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $candidate.Name -eq $name
    }, $true) | Select-Object -First 1
    if ($null -eq $node) { throw "Missing installer function: $name" }
    . ([scriptblock]::Create($node.Extent.Text))
}
Get-CompatiblePythonRuntime -Path $env:PYTHON_PATH |
    Select-Object Path, WindowlessPath, Version |
    ConvertTo-Json -Compress
"""


PYTHON_PREFLIGHT_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:INSTALLER_SOURCE,
    [ref] $tokens,
    [ref] $parseErrors
)
foreach ($name in @('Resolve-CanonicalFile', 'Get-CompatiblePythonRuntime')) {
    $node = $ast.FindAll({
        param($candidate)
        $candidate -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $candidate.Name -eq $name
    }, $true) | Select-Object -First 1
    . ([scriptblock]::Create($node.Extent.Text))
}
function Get-PythonRuntimeProbe {
    param($Path)
    [IO.File]::WriteAllText($env:PROBE_SENTINEL, 'executed')
    throw 'probe should not execute'
}
function ConvertTo-CompatiblePythonRuntime { throw 'converter should not execute' }
Get-CompatiblePythonRuntime -Path $env:PYTHON_PATH
"""


PYTHON_SELECTION_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:INSTALLER_SOURCE,
    [ref] $tokens,
    [ref] $parseErrors
)
$node = $ast.FindAll({
    param($candidate)
    $candidate -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $candidate.Name -eq 'Select-CompatiblePythonRuntime'
}, $true) | Select-Object -First 1
if ($null -eq $node) { throw 'Missing Python selector.' }
. ([scriptblock]::Create($node.Extent.Text))
$script:hints = $env:PYTHON_HINTS | ConvertFrom-Json -Depth 20
$script:runtimes = @($env:PYTHON_RUNTIMES | ConvertFrom-Json -Depth 20)
$script:candidates = @($env:PYTHON_CANDIDATES | ConvertFrom-Json -Depth 20)
function Get-ExistingPythonRuntimeHints {
    param($ManifestDirectory)
    if ($env:BROKEN_MANAGER_HINT -eq 'true') { throw 'mock corrupt ManagerSync XML' }
    return $script:hints
}
function Get-ActivePythonRuntimeHint {
    param($ManifestDirectory)
    return $script:hints.ActivePython
}
function Get-InstalledPythonCandidates { return @($script:candidates) }
function Get-CompatiblePythonRuntime {
    param($Path)
    $runtime = @($script:runtimes | Where-Object {
        [StringComparer]::OrdinalIgnoreCase.Equals([string] $_.Path, [string] $Path)
    }) | Select-Object -First 1
    if ($null -eq $runtime -or -not [bool] $runtime.Compatible) {
        throw "mock incompatible runtime: $Path"
    }
    return [pscustomobject]@{
        Path = [string] $runtime.Path
        WindowlessPath = [string] $runtime.WindowlessPath
        Version = [string] $runtime.Version
        VersionObject = [version] ([string] $runtime.Version)
    }
}
$selected = Select-CompatiblePythonRuntime `
    -RequestedPath $env:REQUESTED_PYTHON `
    -ManifestDirectory 'C:\manifests' `
    -ChildOnly:($env:CHILD_ONLY -eq 'true')
$selected | Select-Object Path, WindowlessPath, Version | ConvertTo-Json -Compress
"""


def _write_content_addressed(directory: Path, prefix: str, suffix: str, content: bytes) -> Path:
    digest = hashlib.sha256(content).hexdigest()
    path = directory / f"{prefix}{digest}{suffix}"
    path.write_bytes(content)
    return path


class InstallerPythonRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pwsh = shutil.which("pwsh")
        if not self.pwsh:
            self.skipTest("PowerShell 7 is unavailable")
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        if hasattr(self, "temporary"):
            self.temporary.cleanup()

    def make_python_files(self, directory: Path | None = None) -> tuple[Path, Path]:
        runtime = directory or (self.root / "Python")
        runtime.mkdir(parents=True, exist_ok=True)
        python = runtime / "python.exe"
        pythonw = runtime / "pythonw.exe"
        python.write_bytes(b"fixture")
        pythonw.write_bytes(b"fixture")
        return python, pythonw

    @staticmethod
    def valid_probe(path: Path, *, minor: int = 13) -> dict[str, object]:
        return {
            "implementation": "CPython",
            "major": 3,
            "minor": minor,
            "micro": 7,
            "releaselevel": "final",
            "executable": str(path),
            "baseInstallation": True,
            "gilDisabledBuild": False,
            "capabilities": {
                "tkinterTtk": True,
                "ctypesWindows": True,
                "msvcrt": True,
                "createNoWindow": True,
            },
        }

    def invoke_validator(
        self, path: Path, probe: dict[str, object]
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "INSTALLER_SOURCE": str(INSTALLER_SOURCE),
                "PYTHON_PATH": str(path),
                "PYTHON_PROBE": json.dumps(probe),
            }
        )
        return subprocess.run(
            [self.pwsh, "-NoProfile", "-NonInteractive", "-Command", PYTHON_VALIDATOR_HARNESS],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            env=environment,
        )

    def invoke_real_probe(self, path: Path) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "INSTALLER_SOURCE": str(INSTALLER_SOURCE),
                "PYTHON_PATH": str(path),
            }
        )
        return subprocess.run(
            [self.pwsh, "-NoProfile", "-NonInteractive", "-Command", PYTHON_REAL_PROBE_HARNESS],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            env=environment,
        )

    def launcher_runtimes(self) -> list[tuple[Path, tuple[int, int, int, str]]]:
        launcher = shutil.which("py")
        if not launcher:
            return []
        completed = subprocess.run(
            [launcher, "-0p"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        result: list[tuple[Path, tuple[int, int, int, str]]] = []
        for line in completed.stdout.splitlines():
            marker = line.lower().find(":\\")
            if marker <= 0:
                continue
            path = Path(line[marker - 1 :].strip().rstrip("*").strip())
            if not path.is_file() or path.name.lower() != "python.exe":
                continue
            version = subprocess.run(
                [str(path), "-I", "-c", "import sys; print(sys.version_info.major, sys.version_info.minor, sys.version_info.micro, sys.version_info.releaselevel)"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=True,
            ).stdout.strip().split()
            result.append((path, (int(version[0]), int(version[1]), int(version[2]), version[3])))
        return result

    def test_real_probe_accepts_installed_313_and_rejects_installed_314_rc(self) -> None:
        runtimes = self.launcher_runtimes()
        final_313 = next(
            (path for path, version in runtimes if version[:2] == (3, 13) and version[3] == "final"),
            None,
        )
        prerelease_314 = next(
            (path for path, version in runtimes if version[:2] == (3, 14) and version[3] != "final"),
            None,
        )
        if final_313 is None or prerelease_314 is None:
            self.skipTest("installed final CPython 3.13 and prerelease CPython 3.14 are required")
        accepted = self.invoke_real_probe(final_313)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(json.loads(accepted.stdout.strip().splitlines()[-1])["Version"][:5], "3.13.")
        rejected = self.invoke_real_probe(prerelease_314)
        self.assertNotEqual(rejected.returncode, 0, rejected.stdout)
        self.assertIn("Prerelease", rejected.stderr)

    def test_accepts_final_gil_cpython_311_and_all_future_3x_minors(self) -> None:
        python, _ = self.make_python_files()
        for minor in (11, 12, 13, 14, 15, 99):
            with self.subTest(minor=minor):
                completed = self.invoke_validator(python, self.valid_probe(python, minor=minor))
                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(completed.stdout.strip().splitlines()[-1])
                self.assertEqual(result["Version"], f"3.{minor}.7")

    def test_rejects_unsupported_implementation_release_build_and_environment(self) -> None:
        python, _ = self.make_python_files()
        mutations: dict[str, tuple[str, object, str]] = {
            "old": ("minor", 10, "3.11 or newer"),
            "prerelease": ("releaselevel", "candidate", "Prerelease"),
            "free-threaded": ("gilDisabledBuild", True, "Free-threaded"),
            "pypy": ("implementation", "PyPy", "must be CPython"),
            "venv": ("baseInstallation", False, "Virtual environments"),
        }
        for label, (field, value, message) in mutations.items():
            with self.subTest(label=label):
                probe = self.valid_probe(python)
                probe[field] = value
                completed = self.invoke_validator(python, probe)
                self.assertNotEqual(completed.returncode, 0, completed.stdout)
                self.assertIn(message, completed.stderr)

    def test_rejects_windowsapps_alias_missing_pythonw_and_missing_capabilities(self) -> None:
        alias, _ = self.make_python_files(self.root / "Microsoft" / "WindowsApps")
        completed = self.invoke_validator(alias, self.valid_probe(alias))
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("WindowsApps", completed.stderr)

        python, pythonw = self.make_python_files(self.root / "NoPythonw")
        pythonw.unlink()
        completed = self.invoke_validator(python, self.valid_probe(python))
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("pythonw.exe", completed.stderr)

        python, _ = self.make_python_files(self.root / "Capabilities")
        for capability in ("tkinterTtk", "ctypesWindows", "msvcrt", "createNoWindow"):
            with self.subTest(capability=capability):
                probe = self.valid_probe(python)
                probe["capabilities"][capability] = False  # type: ignore[index]
                completed = self.invoke_validator(python, probe)
                self.assertNotEqual(completed.returncode, 0, completed.stdout)
                self.assertIn(capability, completed.stderr)

    def test_windowsapps_alias_is_rejected_before_probe_execution(self) -> None:
        alias, _ = self.make_python_files(self.root / "Microsoft" / "WindowsApps")
        sentinel = self.root / "probe-executed.txt"
        environment = os.environ.copy()
        environment.update(
            {
                "INSTALLER_SOURCE": str(INSTALLER_SOURCE),
                "PYTHON_PATH": str(alias),
                "PROBE_SENTINEL": str(sentinel),
            }
        )
        completed = subprocess.run(
            [self.pwsh, "-NoProfile", "-NonInteractive", "-Command", PYTHON_PREFLIGHT_HARNESS],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            env=environment,
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("WindowsApps", completed.stderr)
        self.assertFalse(sentinel.exists())

    def test_rejects_redirected_pythonw_sibling(self) -> None:
        runtime = self.root / "Redirected"
        python, pythonw = self.make_python_files(runtime)
        pythonw.unlink()
        target = self.root / "other-pythonw.exe"
        target.write_bytes(b"fixture")
        try:
            pythonw.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")
        completed = self.invoke_validator(python, self.valid_probe(python))
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("real sibling pythonw.exe", completed.stderr)

    def invoke_selector(
        self,
        *,
        requested: str = "",
        manager: str | None = None,
        active: str | None = None,
        candidates: list[str] | None = None,
        runtimes: list[dict[str, object]] | None = None,
        child_only: bool = False,
        broken_manager_hint: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "INSTALLER_SOURCE": str(INSTALLER_SOURCE),
                "REQUESTED_PYTHON": requested,
                "PYTHON_HINTS": json.dumps(
                    {"ManagerPython": manager, "ActivePython": active}
                ),
                "PYTHON_CANDIDATES": json.dumps(candidates or []),
                "PYTHON_RUNTIMES": json.dumps(runtimes or []),
                "CHILD_ONLY": str(child_only).lower(),
                "BROKEN_MANAGER_HINT": str(broken_manager_hint).lower(),
            }
        )
        return subprocess.run(
            [self.pwsh, "-NoProfile", "-NonInteractive", "-Command", PYTHON_SELECTION_HARNESS],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            env=environment,
        )

    @staticmethod
    def runtime(path: str, version: str, *, compatible: bool = True) -> dict[str, object]:
        return {
            "Path": path,
            "WindowlessPath": str(Path(path).with_name("pythonw.exe")),
            "Version": version,
            "Compatible": compatible,
        }

    def test_explicit_path_wins_and_child_mode_requires_it(self) -> None:
        explicit = r"C:\Explicit\python.exe"
        manager = r"C:\Sticky\python.exe"
        completed = self.invoke_selector(
            requested=explicit,
            manager=manager,
            candidates=[r"C:\Newer\python.exe"],
            runtimes=[self.runtime(explicit, "3.11.9")],
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout.strip().splitlines()[-1])["Path"], explicit)

        missing = self.invoke_selector(child_only=True)
        self.assertNotEqual(missing.returncode, 0, missing.stdout)
        self.assertIn("explicit -PythonPath", missing.stderr)

        child = self.invoke_selector(
            requested=explicit,
            manager=manager,
            active=r"C:\Other\python.exe",
            runtimes=[self.runtime(explicit, "3.11.9")],
            child_only=True,
        )
        self.assertEqual(child.returncode, 0, child.stderr)

        recovery = self.invoke_selector(
            requested=explicit,
            active=None,
            runtimes=[self.runtime(explicit, "3.11.9")],
            broken_manager_hint=True,
        )
        self.assertEqual(recovery.returncode, 0, recovery.stderr)

    def test_sticky_tasks_win_and_mismatched_task_directories_fail(self) -> None:
        sticky = r"C:\Python313\python.exe"
        newer = r"C:\Python315\python.exe"
        runtimes = [self.runtime(sticky, "3.13.11"), self.runtime(newer, "3.15.1")]
        completed = self.invoke_selector(
            manager=sticky,
            active=sticky,
            candidates=[newer],
            runtimes=runtimes,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout.strip().splitlines()[-1])["Path"], sticky)

        mismatch = self.invoke_selector(
            manager=r"C:\Python311\python.exe",
            active=sticky,
            runtimes=runtimes,
        )
        self.assertNotEqual(mismatch.returncode, 0, mismatch.stdout)
        self.assertIn("do not share", mismatch.stderr)

        incompatible = self.invoke_selector(
            manager=sticky,
            candidates=[newer],
            runtimes=[self.runtime(sticky, "3.13.11", compatible=False), self.runtime(newer, "3.15.1")],
        )
        self.assertNotEqual(incompatible.returncode, 0, incompatible.stdout)
        self.assertIn("existing scheduled runtime", incompatible.stderr)

    def test_active_one_shot_blocks_cross_directory_explicit_switch(self) -> None:
        active = r"C:\Python313\python.exe"
        requested = r"C:\Python315\python.exe"
        completed = self.invoke_selector(
            requested=requested,
            active=active,
            runtimes=[self.runtime(requested, "3.15.1")],
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("cross-directory", completed.stderr)

        same_directory = self.invoke_selector(
            requested=r"C:\Python313\python.exe",
            active=active,
            runtimes=[self.runtime(active, "3.13.11")],
        )
        self.assertEqual(same_directory.returncode, 0, same_directory.stderr)

    def test_fresh_install_selects_newest_valid_candidate(self) -> None:
        py311 = r"C:\Python311\python.exe"
        py315 = r"C:\Python315\python.exe"
        bad = r"C:\PythonBad\python.exe"
        completed = self.invoke_selector(
            candidates=[py311, py315, py311, bad],
            runtimes=[
                self.runtime(py311, "3.11.9"),
                self.runtime(py315, "3.15.1"),
                self.runtime(bad, "3.99.1", compatible=False),
            ],
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout.strip().splitlines()[-1])["Path"], py315)

    def test_discovery_is_read_only_deduplicated_launcher_and_path_enumeration(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        start = source.index("function Get-InstalledPythonCandidates")
        end = source.index("function ConvertFrom-PythonTaskCommand", start)
        body = source[start:end]
        self.assertIn("[Collections.Generic.HashSet[string]]", body)
        self.assertIn("-0p", body)
        self.assertIn("Get-Command python.exe -All", body)
        self.assertNotRegex(body, r"-[0-9]+\.[0-9]+")

    def test_probe_checks_windows_capabilities_and_build_gil_flag(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        start = source.index("function Get-PythonRuntimeProbe")
        end = source.index("function ConvertTo-CompatiblePythonRuntime", start)
        body = source[start:end]
        self.assertIn("-I -c $probeCode", body)
        self.assertIn("$lines = @(& $Path -I -c $probeCode", body)
        self.assertIn('get_config_var("Py_GIL_DISABLED")', body)
        for capability in ("tkinter.ttk", "WINFUNCTYPE", "msvcrt", "CREATE_NO_WINDOW"):
            self.assertIn(capability, body)

    def test_selected_runtime_is_rechecked_against_active_snapshot_before_mutation(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        snapshot = source.index("$activeOneShotSnapshot = Get-ActiveOneShotSnapshot")
        comparison = source.index("Assert-SelectedPythonMatchesActiveOneShot", snapshot)
        policy_snapshot = source.index("$policySnapshot = Get-FileByteSnapshot", comparison)
        lock = source.index("controller.lock for UI quiescence", policy_snapshot)
        unchanged = source.index("Assert-ActiveOneShotUnchanged", lock)
        locked_comparison = source.index("Assert-SelectedPythonMatchesActiveOneShot", unchanged)
        policy_mutation = source.index("$policyMutationAttempted = $true", locked_comparison)
        self.assertLess(snapshot, comparison)
        self.assertLess(comparison, policy_snapshot)
        self.assertLess(unchanged, locked_comparison)
        self.assertLess(locked_comparison, policy_mutation)


class InstallerAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pwsh = shutil.which("pwsh")
        if not self.pwsh:
            self.skipTest("PowerShell 7 is unavailable")
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.installers = self.root / "installers"
        self.runners = self.root / "runners"
        self.manifests = self.root / "manifests"
        self.state = self.root / "state"
        for directory in (self.installers, self.runners, self.manifests, self.state):
            directory.mkdir()
        self.installer = _write_content_addressed(
            self.installers, "install-", ".ps1", b"immutable installer fixture\n"
        )
        self.guard = _write_content_addressed(
            self.runners, "codex_reset_guard-", ".py", b"immutable guard fixture\n"
        )
        self.codex = self.root / "codex.exe"
        self.codex.write_bytes(b"codex fixture\n")
        self.codex_hash = hashlib.sha256(self.codex.read_bytes()).hexdigest()
        self.policy = {
            "enabled": True,
            "blocked": None,
            "runtimeInstaller": str(self.installer),
            "runtimeGuard": str(self.guard),
            "approvedCli": {
                "codexExe": str(self.codex),
                "codexVersion": "codex-cli 0.144.1",
                "codexSha256": self.codex_hash,
            },
            "currentJob": None,
        }
        self.observed = {
            "Path": str(self.codex),
            "Version": "0.144.1",
            "Sha256": self.codex_hash,
        }

    def tearDown(self) -> None:
        if hasattr(self, "temporary"):
            self.temporary.cleanup()

    def invoke(
        self,
        policy: dict[str, object],
        *,
        executing_installer: Path | None = None,
        guard_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        policy_path = self.state / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        selected_installer = executing_installer or self.installer
        selected_guard = guard_path or self.guard
        environment = os.environ.copy()
        environment.update(
            {
                "INSTALLER_SOURCE": str(INSTALLER_SOURCE),
                "POLICY_PATH": str(policy_path),
                "MANIFESTS_DIRECTORY": str(self.manifests),
                "INSTALL_ROOT": str(self.root),
                "EXECUTING_INSTALLER": str(selected_installer),
                "GUARD_PATH": str(selected_guard),
                "GUARD_SHA256": hashlib.sha256(selected_guard.read_bytes()).hexdigest(),
                "OBSERVED_CODEX": json.dumps(self.observed),
            }
        )
        return subprocess.run(
            [self.pwsh, "-NoProfile", "-NonInteractive", "-Command", HARNESS],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            env=environment,
        )

    def assert_rejected(self, policy: dict[str, object], message: str) -> None:
        completed = self.invoke(policy)
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn(message, completed.stderr)

    def test_accepts_exact_unblocked_controller_pins(self) -> None:
        completed = self.invoke(self.policy)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_rejects_blocked_policy(self) -> None:
        policy = copy.deepcopy(self.policy)
        policy["blocked"] = {"code": "CLI_VALIDATION_FAILED", "atUtc": "2026-07-12T00:00:00Z"}
        self.assert_rejected(policy, "policy is blocked")

    def test_rejects_missing_approved_cli_pin(self) -> None:
        policy = copy.deepcopy(self.policy)
        policy["approvedCli"] = None
        self.assert_rejected(policy, "no approved Codex CLI pin")

    def test_rejects_each_approved_cli_mismatch(self) -> None:
        other_codex = self.root / "other-codex.exe"
        other_codex.write_bytes(b"other codex fixture\n")
        mutations = {
            "path": ("codexExe", str(other_codex)),
            "version": ("codexVersion", "codex-cli 0.145.0"),
            "hash": ("codexSha256", "0" * 64),
        }
        for label, (field, value) in mutations.items():
            with self.subTest(label=label):
                policy = copy.deepcopy(self.policy)
                policy["approvedCli"][field] = value  # type: ignore[index]
                self.assert_rejected(policy, "does not match the path, version, and hash")

    def test_rejects_installer_or_guard_not_pinned_by_policy(self) -> None:
        other_installer = _write_content_addressed(
            self.installers, "install-", ".ps1", b"other installer fixture\n"
        )
        other_guard = _write_content_addressed(
            self.runners, "codex_reset_guard-", ".py", b"other guard fixture\n"
        )
        for field, value, message in (
            ("runtimeInstaller", str(other_installer), "executing installer does not match"),
            ("runtimeGuard", str(other_guard), "selected guard does not match"),
        ):
            with self.subTest(field=field):
                policy = copy.deepcopy(self.policy)
                policy[field] = value
                self.assert_rejected(policy, message)

    def test_rejects_pinned_runtime_without_content_addressed_identity(self) -> None:
        plain_installer = self.installers / "install.ps1"
        plain_installer.write_bytes(b"plain installer fixture\n")
        plain_guard = self.runners / "codex_reset_guard.py"
        plain_guard.write_bytes(b"plain guard fixture\n")

        policy = copy.deepcopy(self.policy)
        policy["runtimeInstaller"] = str(plain_installer)
        completed = self.invoke(policy, executing_installer=plain_installer)
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("not an immutable content-addressed installer", completed.stderr)

        policy = copy.deepcopy(self.policy)
        policy["runtimeGuard"] = str(plain_guard)
        completed = self.invoke(policy, guard_path=plain_guard)
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("not an immutable content-addressed guard", completed.stderr)


class InstallerSourceContractTests(unittest.TestCase):
    def test_manager_sync_uses_exact_thirty_minute_interval_and_logon_trigger(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        self.assertIn("$ManagerSyncIntervalMinutes = 30", source)
        self.assertIn(
            "-RepetitionInterval (New-TimeSpan -Minutes $ManagerSyncIntervalMinutes)",
            source,
        )
        self.assertIn('$expectedInterval = "PT$($ManagerSyncIntervalMinutes)M"', source)
        self.assertIn("$intervalNode.InnerText.Trim() -ne $expectedInterval", source)
        self.assertNotIn("PT15M", source)
        self.assertIn(
            "$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser",
            source,
        )
        self.assertIn("-Trigger @($logonTrigger, $periodicTrigger)", source)

    def test_usage_limit_reset_shortcut_is_verified_before_exact_prior_removals(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        manager_name = "$ManagerShortcutName = 'Codex Usage Limit Reset Manager.lnk'"
        current_name = "$CurrentManagerShortcutName = 'Codex Reset Credit Manager.lnk'"
        legacy_name = "$LegacyManagerShortcutName = 'Codex 초기화권 자동 사용.lnk'"
        self.assertIn(manager_name, source)
        self.assertIn(current_name, source)
        self.assertIn(legacy_name, source)
        create = source.index("Set-AndAssertManagerShortcut `", source.index("$shortcutPath ="))
        verified = source.index("$shortcutVerified = $true", create)
        verify_end = source.index("if ($PSCmdlet.ShouldProcess('Codex Usage Limit Reset Manager'", create)
        remove_current = source.index(
            "Remove-Item -LiteralPath $currentShortcutPath -Force", verify_end
        )
        remove_legacy = source.index(
            "Remove-Item -LiteralPath $legacyShortcutPath -Force", remove_current
        )
        self.assertLess(create, verify_end)
        self.assertLess(create, verified)
        self.assertLess(verified, remove_current)
        self.assertLess(remove_current, remove_legacy)
        self.assertIn("if ($shortcutVerified -and", source[verify_end:remove_current])
        self.assertIn("if ($shortcutVerified -and", source[remove_current:remove_legacy])
        self.assertIn("$readback.Description -ne $shortcutDescription", source)
        self.assertIn(
            "$shortcutDescription = 'Manage automatic use of Codex usage limit resets'",
            source,
        )
        self.assertLess(verify_end, remove_current)
        self.assertNotIn("Remove-Item -Path $currentShortcutPath", source)
        self.assertNotIn("Remove-Item -Path $legacyShortcutPath", source)
        self.assertNotIn("*Codex*", source)

    def test_installer_user_facing_text_is_english_except_parser_and_legacy_allowlist(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        hangul_lines = [
            line.strip()
            for line in source.splitlines()
            if any("\uac00" <= character <= "\ud7a3" for character in line)
        ]
        self.assertEqual(
            hangul_lines,
            [
                "$LegacyManagerShortcutName = 'Codex 초기화권 자동 사용.lnk'",
                "if ($statusText -match '(?i)Local\\s+CMOS\\s+Clock|로컬\\s*CMOS|unsynchroni[sz]ed|동기화되지\\s*않') {",
                "$sourceLine = $nonEmptyLines | Where-Object { $_ -match '(?i)^\\s*(Source|원본)\\s*:' } | Select-Object -First 1",
                "if ([string]::IsNullOrWhiteSpace($source) -or $source -match '(?i)Local\\s+CMOS\\s+Clock|로컬\\s*CMOS') {",
            ],
        )

    def test_manager_sync_uses_and_asserts_windowless_python(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        self.assertIn(
            "$managerAction = New-ScheduledTaskAction -Execute $python.WindowlessPath",
            source,
        )
        assertion = source.index("Assert-ManagerTask `", source.index("$managerAction ="))
        self.assertIn(
            "-ExpectedPython $python.WindowlessPath",
            source[assertion : assertion + 400],
        )

    def test_one_shot_uses_and_asserts_windowless_python(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        action_text = (
            "$action = New-ScheduledTaskAction -Execute $python.WindowlessPath "
            "-Argument $taskArguments"
        )
        action = source.index(action_text)
        assertion = source.index("Assert-RegisteredTask `", action)
        self.assertIn(
            "-ExpectedPython $python.WindowlessPath",
            source[assertion : assertion + 500],
        )

    def test_manager_wake_false_accepts_scheduler_default_omission(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        self.assertIn('$wakeNode = $xml.SelectSingleNode', source)
        self.assertIn('$null -ne $wakeNode -and', source)
        self.assertNotIn(
            "Get-RequiredXmlText $xml \"$settings/*[local-name()='WakeToRun']\"",
            source,
        )

    def test_update_stops_only_verified_installed_manager_ui_and_restarts_on_failure(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        self.assertIn("function Get-InstalledManagerUiProcesses", source)
        self.assertIn("codex_reset_manager-(?<digest>[0-9a-f]{64})", source)
        self.assertIn("Get-FileHash -LiteralPath $manager -Algorithm SHA256", source)
        stop = source.index("Stop-Process -Id $ui.ProcessId")
        bootstrap = source.index("$managerStatus = Invoke-Manager", stop)
        launch = source.index("-FilePath $python.WindowlessPath", bootstrap)
        cleanup = source.index("Stop-Process -Id $runningUi.ProcessId", launch)
        restore = source.index("-FilePath $previous.Pythonw", launch)
        self.assertLess(stop, bootstrap)
        self.assertLess(bootstrap, launch)
        self.assertLess(cleanup, restore)
        self.assertGreater(restore, launch)

    def test_update_snapshots_before_mutation_and_rolls_back_in_safe_order(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        normal = source.index("if (-not $ManagerChildOnly) {", source.index("$nonterminal ="))
        snapshots = (
            source.index("$policySnapshot = Get-FileByteSnapshot", normal),
            source.index("$managerTaskSnapshot = Get-ManagerTaskSnapshot", normal),
            source.index("$shortcutSnapshot = Get-FileByteSnapshot", normal),
            source.index("$currentShortcutSnapshot = Get-FileByteSnapshot", normal),
            source.index("$legacyShortcutSnapshot = Get-FileByteSnapshot", normal),
            source.index("$priorManagerUis = @(", normal),
        )
        stop = source.index("Stop-Process -Id $ui.ProcessId", snapshots[-1])
        bootstrap = source.index("$managerStatus = Invoke-Manager", stop)
        for snapshot in snapshots:
            self.assertLess(snapshot, stop)
            self.assertLess(snapshot, bootstrap)

        catch = source.index("$installationError = $_", bootstrap)
        stop_replacement = source.index("Stop-Process -Id $replacementUiProcess.Id", catch)
        restore_policy = source.index("Restore-FileByteSnapshot -Path $policyPath", catch)
        restore_task = source.index("Restore-ManagerTaskSnapshot -Snapshot $managerTaskSnapshot", catch)
        restore_shortcut = source.index("Restore-FileByteSnapshot -Path $shortcutPath", catch)
        restore_current = source.index("Restore-FileByteSnapshot -Path $currentShortcutPath", catch)
        restore_legacy = source.index("Restore-FileByteSnapshot -Path $legacyShortcutPath", catch)
        relaunch = source.index("-FilePath $previous.Pythonw", catch)
        self.assertLess(stop_replacement, restore_policy)
        self.assertLess(restore_policy, restore_task)
        self.assertLess(restore_task, restore_shortcut)
        self.assertLess(restore_shortcut, restore_current)
        self.assertLess(restore_current, restore_legacy)
        self.assertLess(restore_legacy, relaunch)

    def test_active_one_shot_and_controller_locks_guard_the_commit(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        snapshot = source.index("$activeOneShotSnapshot = Get-ActiveOneShotSnapshot")
        suspend = source.index("Suspend-ManagerSyncForInstall", snapshot)
        quiesce_lock = source.index("$controllerLockStream = Enter-InstallerByteRangeLock", suspend)
        pre_stop_revalidate = source.index("Assert-ActiveOneShotUnchanged", quiesce_lock)
        prior_stop = source.index("Stop-Process -Id $ui.ProcessId", pre_stop_revalidate)
        prior_gone = source.index("Get-Process -Id $priorId", prior_stop)
        post_stop_revalidate = source.index("Assert-ActiveOneShotUnchanged", prior_gone)
        quiesce_release = source.index(
            "Exit-InstallerByteRangeLock -Stream $controllerLockStream", post_stop_revalidate
        )
        status = source.index("$managerStatus = Invoke-Manager", quiesce_release)
        policy_pin = source.index("Assert-ManagerPolicyRuntimePins", status)
        controller_lock = source.index("$controllerLockStream = Enter-InstallerByteRangeLock", policy_pin)
        margin = source.index("Assert-ActiveOneShotTriggerMargin", controller_lock)
        dispatch_lock = source.index("$dispatchLockStream = Enter-InstallerByteRangeLock", margin)
        task_register = source.index("Register-ScheduledTask `", dispatch_lock)
        ui_handshake = source.index("Assert-ReplacementManagerUi `", task_register)
        commit_assert = source.index("Assert-ActiveOneShotUnchanged", ui_handshake)
        dispatch_release = source.index("Exit-InstallerByteRangeLock -Stream $dispatchLockStream", commit_assert)
        controller_release = source.index("Exit-InstallerByteRangeLock -Stream $controllerLockStream", dispatch_release)
        self.assertLess(snapshot, suspend)
        self.assertLess(suspend, quiesce_lock)
        self.assertLess(quiesce_lock, pre_stop_revalidate)
        self.assertLess(pre_stop_revalidate, prior_stop)
        self.assertLess(prior_stop, prior_gone)
        self.assertLess(prior_gone, post_stop_revalidate)
        self.assertLess(post_stop_revalidate, quiesce_release)
        self.assertLess(quiesce_release, status)
        self.assertLess(status, policy_pin)
        self.assertLess(policy_pin, controller_lock)
        self.assertLess(controller_lock, margin)
        self.assertLess(margin, dispatch_lock)
        self.assertLess(dispatch_lock, task_register)
        self.assertLess(task_register, ui_handshake)
        self.assertLess(ui_handshake, commit_assert)
        self.assertLess(commit_assert, dispatch_release)
        self.assertLess(dispatch_release, controller_release)

    def test_running_manager_sync_is_never_terminated_and_busy_controller_preserves_prior_ui(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        suspend = source.index("function Suspend-ManagerSyncForInstall")
        suspend_end = source.index("function Assert-ManagerPolicyRuntimePins", suspend)
        suspend_body = source[suspend:suspend_end]
        self.assertIn("Disable-ScheduledTask", suspend_body)
        self.assertIn("did not finish naturally within 30 seconds", suspend_body)
        self.assertNotIn("Stop-ScheduledTask", suspend_body)

        transaction = source.index("Suspend-ManagerSyncForInstall -Snapshot")
        lock = source.index("controller.lock for UI quiescence", transaction)
        prior_stop = source.index("Stop-Process -Id $ui.ProcessId", lock)
        self.assertLess(lock, prior_stop)
        catch = source.index("$installationError = $_", prior_stop)
        preserve_original = source.index("$runningUi.ProcessId -in $priorManagerUiProcessIds", catch)
        relaunch_check = source.index("$originalAlive = @(", preserve_original)
        self.assertLess(preserve_original, relaunch_check)

    def test_early_failure_skips_untouched_policy_and_shortcut_restores(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        transaction = source.index("$policyMutationAttempted = $false")
        suspend = source.index("Suspend-ManagerSyncForInstall -Snapshot", transaction)
        policy_attempt = source.index("$policyMutationAttempted = $true", suspend)
        status = source.index("$managerStatus = Invoke-Manager", policy_attempt)
        manager_attempt = source.index("$managerShortcutMutationAttempted = $true", status)
        manager_write = source.index("Set-AndAssertManagerShortcut `", manager_attempt)
        current_attempt = source.index("$currentShortcutMutationAttempted = $true", manager_write)
        current_remove = source.index(
            "Remove-Item -LiteralPath $currentShortcutPath", current_attempt
        )
        legacy_attempt = source.index("$legacyShortcutMutationAttempted = $true", current_remove)
        legacy_remove = source.index("Remove-Item -LiteralPath $legacyShortcutPath", legacy_attempt)
        self.assertLess(suspend, policy_attempt)
        self.assertLess(policy_attempt, status)
        self.assertLess(manager_attempt, manager_write)
        self.assertLess(current_attempt, current_remove)
        self.assertLess(current_remove, legacy_attempt)
        self.assertLess(legacy_attempt, legacy_remove)

        catch = source.index("$installationError = $_", legacy_remove)
        policy_guard = source.index("if ($policyMutationAttempted)", catch)
        policy_restore = source.index("Restore-FileByteSnapshot -Path $policyPath", policy_guard)
        manager_guard = source.index("if ($managerShortcutMutationAttempted)", policy_restore)
        manager_restore = source.index("Restore-FileByteSnapshot -Path $shortcutPath", manager_guard)
        current_guard = source.index("if ($currentShortcutMutationAttempted)", manager_restore)
        current_restore = source.index(
            "Restore-FileByteSnapshot -Path $currentShortcutPath", current_guard
        )
        legacy_guard = source.index("if ($legacyShortcutMutationAttempted)", current_restore)
        legacy_restore = source.index("Restore-FileByteSnapshot -Path $legacyShortcutPath", legacy_guard)
        self.assertLess(policy_guard, policy_restore)
        self.assertLess(manager_guard, manager_restore)
        self.assertLess(current_guard, current_restore)
        self.assertLess(legacy_guard, legacy_restore)

    def test_compatibility_paths_and_schema_facing_names_remain_stable(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        self.assertIn("$TaskFolder = '\\CodexResetCredit\\'", source)
        self.assertIn("Join-Path $env:LOCALAPPDATA 'CodexResetCredit'", source)
        self.assertIn('("reset-credit-{0}.json"', source)
        self.assertIn("'target.creditIdSha256'", source)
        self.assertIn("$CodexPathEnvironmentVariable = 'CODEX_RESET_GUARD_CODEX_PATH'", source)

    def test_policy_rollback_requires_held_or_reacquired_controller_lock(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        catch = source.index("$installationError = $_", source.index("$policyMutationAttempted = $false"))
        reacquire_condition = source.index(
            "if ($policyMutationAttempted -and $null -eq $controllerLockStream)", catch
        )
        reacquire = source.index("controller.lock for policy rollback", reacquire_condition)
        active_check = source.index("Assert-ActiveOneShotUnchanged", reacquire)
        policy_guard = source.index("if ($policyMutationAttempted)", active_check)
        ready_guard = source.index("if ($rollbackControllerReady)", policy_guard)
        policy_restore = source.index("Restore-FileByteSnapshot -Path $policyPath", ready_guard)
        self.assertLess(reacquire_condition, reacquire)
        self.assertLess(reacquire, active_check)
        self.assertLess(active_check, policy_guard)
        self.assertLess(policy_guard, ready_guard)
        self.assertLess(ready_guard, policy_restore)

    def test_rollback_never_overwrites_changed_one_shot_and_unlocks_before_prior_ui(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        catch = source.index("$installationError = $_", source.index("$activeOneShotSnapshot ="))
        active_check = source.index("Assert-ActiveOneShotUnchanged", catch)
        policy_restore = source.index("Restore-FileByteSnapshot -Path $policyPath", active_check)
        task_restore = source.index("Restore-ManagerTaskSnapshot", policy_restore)
        dispatch_release = source.index("Exit-InstallerByteRangeLock -Stream $dispatchLockStream", task_restore)
        controller_release = source.index("Exit-InstallerByteRangeLock -Stream $controllerLockStream", dispatch_release)
        prior_relaunch = source.index("-FilePath $previous.Pythonw", controller_release)
        self.assertLess(active_check, policy_restore)
        self.assertLess(policy_restore, task_restore)
        self.assertLess(task_restore, dispatch_release)
        self.assertLess(dispatch_release, controller_release)
        self.assertLess(controller_release, prior_relaunch)
        self.assertNotIn("Restore-ActiveOneShot", source)

    def test_replacement_ui_validation_polls_ready_lock_and_exact_identity(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        helper = source.index("function Assert-ReplacementManagerUi")
        end = source.index("Assert-WindowsPowerShell7", helper)
        body = source[helper:end]
        self.assertIn("manager-ui-ready.json", body)
        self.assertIn("Read-ManagerUiReadyMarker", body)
        self.assertIn("Test-ByteRangeLockOwned", body)
        self.assertIn("DateTimeStyles]::AssumeUniversal", body)
        self.assertIn("DateTimeStyles]::AdjustToUniversal", body)
        self.assertIn("$all.Count -ne 1", body)
        self.assertIn("$secondary.ExitCode -ne 0", body)
        self.assertIn("$PrimaryProcess.HasExited", body)
        self.assertIn("$lastReadyReason", body)
        marker_reader = source[
            source.index("function Read-ManagerUiReadyMarker") : helper
        ]
        self.assertIn("[Text.Json.JsonDocument]::Parse($raw)", marker_reader)
        self.assertNotIn("ConvertFrom-Json", marker_reader)
        self.assertIn("manager-ui-show-request.json", body)
        second_exit = body.index("$secondary.ExitCode -ne 0")
        consume_poll = body.index("$showRequestConsumed = $false", second_exit)
        marker_absent = body.index("-not (Test-Path -LiteralPath $showRequestPath", consume_poll)
        timeout_failure = body.index("did not consume the second-launch show request", marker_absent)
        self.assertLess(second_exit, consume_poll)
        self.assertLess(consume_poll, marker_absent)
        self.assertLess(marker_absent, timeout_failure)

    def test_existing_enabled_state_is_preserved_and_multiple_prior_uis_are_rejected(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        self.assertIn("$preinstallEnabled = [bool] $preinstallPolicy.enabled", source)
        self.assertIn("[bool] $finalPolicy.enabled -ne $preinstallEnabled", source)
        self.assertIn("if ($priorManagerUis.Count -gt 1)", source)

    def test_policy_and_shortcut_rollback_uses_atomic_byte_snapshots(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        self.assertIn("[IO.File]::ReadAllBytes($Path)", source)
        self.assertIn("[IO.File]::Move($temporary, $Path, $true)", source)
        self.assertIn("Remove-Item -LiteralPath $Path -Force", source)

    def test_manager_child_flow_does_not_enter_normal_install_transaction(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        transaction = source.index("$policySnapshot = Get-FileByteSnapshot")
        enclosing = source.rfind("if (-not $ManagerChildOnly) {", 0, transaction)
        self.assertNotEqual(enclosing, -1)

    def test_completion_message_reflects_preserved_enabled_state(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        self.assertIn("if ([bool] $finalPolicy.enabled)", source)
        self.assertIn("Existing automatic operation remains enabled", source)
        self.assertIn("Automatic operation remains paused", source)

    def test_child_admission_precedes_time_or_filesystem_mutation(self) -> None:
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        admission = source.index("-ObservedCodex $codex")
        time_check = source.index("$timeHealth = $null", admission)
        directory_write = source.index("'Create private runtime directories'", admission)
        self.assertLess(admission, time_check)
        self.assertLess(admission, directory_write)


class InstallerRollbackHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pwsh = shutil.which("pwsh")
        if not self.pwsh:
            self.skipTest("PowerShell 7 is unavailable")
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        if hasattr(self, "temporary"):
            self.temporary.cleanup()

    def invoke_file_rollback(self, path: Path) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "INSTALLER_SOURCE": str(INSTALLER_SOURCE),
                "SNAPSHOT_PATH": str(path),
            }
        )
        return subprocess.run(
            [self.pwsh, "-NoProfile", "-NonInteractive", "-Command", FILE_ROLLBACK_HARNESS],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            env=environment,
        )

    def test_existing_file_is_restored_byte_for_byte_atomically(self) -> None:
        path = self.root / "policy.json"
        original = b'{"enabled":true}\r\n\x00binary-tail'
        path.write_bytes(original)
        completed = self.invoke_file_rollback(path)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.root.glob("*.rollback-*.tmp")), [])

    def test_file_created_after_absent_snapshot_is_removed(self) -> None:
        path = self.root / "Codex Usage Limit Reset Manager.lnk"
        completed = self.invoke_file_rollback(path)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(path.exists())

    def invoke_task_rollback(self, prior_exists: bool) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "INSTALLER_SOURCE": str(INSTALLER_SOURCE),
                "PRIOR_TASK_EXISTS": str(prior_exists).lower(),
                "PRIOR_TASK_XML": "<Task><RegistrationInfo>prior</RegistrationInfo></Task>",
            }
        )
        return subprocess.run(
            [self.pwsh, "-NoProfile", "-NonInteractive", "-Command", TASK_ROLLBACK_HARNESS],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            env=environment,
        )

    def test_existing_manager_task_xml_is_restored(self) -> None:
        completed = self.invoke_task_rollback(True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        state = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertTrue(state["Exists"])
        self.assertEqual(state["Xml"], "<Task><RegistrationInfo>prior</RegistrationInfo></Task>")

    def test_new_manager_task_is_removed_when_none_existed(self) -> None:
        completed = self.invoke_task_rollback(False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        state = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertFalse(state["Exists"])
        self.assertIsNone(state["Xml"])


class InstallerActiveOneShotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pwsh = shutil.which("pwsh")
        if not self.pwsh:
            self.skipTest("PowerShell 7 is unavailable")
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manifest = self.root / "active.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "state": "ARMED",
                    "jobId": "11111111-1111-4111-8111-111111111111",
                    "armed": True,
                    "task": {"name": r"\CodexResetCredit\Consume-test"},
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        if hasattr(self, "temporary"):
            self.temporary.cleanup()

    def invoke(
        self,
        *,
        minutes_to_trigger: int = 60,
        state: str = "Ready",
        mutate_manifest: bool = False,
        mutate_task: bool = False,
        empty: bool = False,
        create_after_empty: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        self.setUp_manifest_again()
        start = datetime.now(UTC) + timedelta(minutes=minutes_to_trigger)
        end = start + timedelta(minutes=10)
        task_xml = f"""<Task>
  <Triggers><TimeTrigger>
    <StartBoundary>{start.strftime('%Y-%m-%dT%H:%M:%SZ')}</StartBoundary>
    <EndBoundary>{end.strftime('%Y-%m-%dT%H:%M:%SZ')}</EndBoundary>
  </TimeTrigger></Triggers>
  <Actions><Exec>
    <Command>C:\\Python313\\pythonw.exe</Command>
    <Arguments>"guard.py" run --manifest "active.json" --live</Arguments>
    <WorkingDirectory>C:\\Runtime</WorkingDirectory>
  </Exec></Actions>
</Task>"""
        environment = os.environ.copy()
        environment.update(
            {
                "INSTALLER_SOURCE": str(INSTALLER_SOURCE),
                "ACTIVE_MANIFEST": str(self.manifest),
                "ACTIVE_MANIFEST_DIRECTORY": str(self.root),
                "ACTIVE_TASK_XML": task_xml,
                "ACTIVE_TASK_STATE": state,
                "MUTATE_ACTIVE_MANIFEST": str(mutate_manifest).lower(),
                "MUTATE_ACTIVE_TASK": str(mutate_task).lower(),
                "ACTIVE_EMPTY": str(empty).lower(),
                "CREATE_ACTIVE_AFTER_EMPTY": str(create_after_empty).lower(),
                "ACTIVE_MANIFEST_JSON": self.manifest.read_text(encoding="utf-8"),
            }
        )
        return subprocess.run(
            [self.pwsh, "-NoProfile", "-NonInteractive", "-Command", ACTIVE_ONE_SHOT_HARNESS],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            env=environment,
        )

    def test_zero_or_one_safe_active_job_is_accepted(self) -> None:
        for empty in (True, False):
            with self.subTest(empty=empty):
                completed = self.invoke(empty=empty)
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_running_or_near_trigger_job_is_rejected_before_install(self) -> None:
        running = self.invoke(state="Running")
        self.assertNotEqual(running.returncode, 0, running.stdout)
        self.assertIn("not safe now", running.stderr)
        near = self.invoke(minutes_to_trigger=5)
        self.assertNotEqual(near.returncode, 0, near.stdout)
        self.assertIn("not more than 10 minutes", near.stderr)

    def test_manifest_or_task_change_is_detected_without_restoring_it(self) -> None:
        changed_manifest = self.invoke(mutate_manifest=True)
        self.assertNotEqual(changed_manifest.returncode, 0, changed_manifest.stdout)
        self.assertIn("active manifest changed", changed_manifest.stderr)
        # Recreate the original after the deliberate mutation.
        self.setUp_manifest_again()
        changed_task = self.invoke(mutate_task=True)
        self.assertNotEqual(changed_task.returncode, 0, changed_task.stdout)
        self.assertIn("task XML, action, or schedule changed", changed_task.stderr)

    def test_zero_active_snapshot_detects_a_newly_created_child(self) -> None:
        completed = self.invoke(empty=True, create_after_empty=True)
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("new nonterminal manifest appeared", completed.stderr)

    def setUp_manifest_again(self) -> None:
        self.manifest.write_text(
            json.dumps(
                {
                    "state": "ARMED",
                    "jobId": "11111111-1111-4111-8111-111111111111",
                    "armed": True,
                    "task": {"name": r"\CodexResetCredit\Consume-test"},
                }
            ),
            encoding="utf-8",
        )

    def test_dotnet_installer_lock_contends_with_python_file_lock(self) -> None:
        lock_path = self.root / "controller.lock"
        environment = os.environ.copy()
        environment.update(
            {"INSTALLER_SOURCE": str(INSTALLER_SOURCE), "LOCK_PATH": str(lock_path)}
        )
        process = subprocess.Popen(
            [self.pwsh, "-NoProfile", "-NonInteractive", "-Command", LOCK_HOLDER_HARNESS],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.assertEqual(process.stdout.readline().strip(), "ready")
        with self.assertRaises(manager.ManagerError) as caught:
            with manager.FileLock(lock_path, busy_code="CONTROLLER_BUSY"):
                pass
        self.assertEqual(caught.exception.code, "CONTROLLER_BUSY")
        process.kill()
        process.communicate(timeout=10)

    def invoke_ready_marker(self, raw_json: str) -> subprocess.CompletedProcess[str]:
        path = self.root / "manager-ui-ready.json"
        path.write_text(raw_json, encoding="utf-8")
        environment = os.environ.copy()
        environment.update(
            {"INSTALLER_SOURCE": str(INSTALLER_SOURCE), "READY_MARKER_PATH": str(path)}
        )
        return subprocess.run(
            [self.pwsh, "-NoProfile", "-NonInteractive", "-Command", READY_MARKER_HARNESS],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            env=environment,
        )

    def test_ready_iso_timestamp_remains_raw_string_on_powershell_75(self) -> None:
        raw = json.dumps(
            {
                "schemaVersion": 1,
                "pid": 4242,
                "readyAtUtc": "2026-07-11T23:59:58Z",
                "managerSha256": "a" * 64,
                "trayReady": True,
            }
        )
        completed = self.invoke_ready_marker(raw)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = completed.stdout.strip().splitlines()
        self.assertEqual(lines[-2:], ["2026-07-11T23:59:58Z", "System.String"])

    def test_ready_marker_duplicate_property_is_rejected(self) -> None:
        raw = (
            '{"schemaVersion":1,"pid":4242,"pid":4243,'
            '"readyAtUtc":"2026-07-11T23:59:58Z",'
            f'"managerSha256":"{"a" * 64}","trayReady":true}}'
        )
        completed = self.invoke_ready_marker(raw)
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("duplicate", completed.stderr)


class InstallerManagerTaskReadbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pwsh = shutil.which("pwsh")
        if not self.pwsh:
            self.skipTest("PowerShell 7 is unavailable")
        self.working = str(PROJECT_ROOT)
        self.pythonw = str(PROJECT_ROOT / "pythonw.exe")
        self.arguments = '"manager.py" sync --scheduled'

    def invoke(self, interval: str, *, include_logon: bool = True) -> subprocess.CompletedProcess[str]:
        logon = "<LogonTrigger />" if include_logon else ""
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Task>
  <Triggers>
    {logon}
    <TimeTrigger><Repetition><Interval>{html.escape(interval)}</Interval></Repetition></TimeTrigger>
  </Triggers>
  <Settings>
    <WakeToRun>false</WakeToRun>
    <StartWhenAvailable>true</StartWhenAvailable>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
  </Settings>
  <Actions><Exec>
    <Command>{html.escape(self.pythonw)}</Command>
    <Arguments>{html.escape(self.arguments)}</Arguments>
    <WorkingDirectory>{html.escape(self.working)}</WorkingDirectory>
  </Exec></Actions>
</Task>"""
        environment = os.environ.copy()
        environment.update(
            {
                "INSTALLER_SOURCE": str(INSTALLER_SOURCE),
                "MANAGER_TASK_XML": xml,
                "EXPECTED_PYTHON": self.pythonw,
                "EXPECTED_ARGUMENTS": self.arguments,
                "EXPECTED_WORKING": self.working,
            }
        )
        return subprocess.run(
            [self.pwsh, "-NoProfile", "-NonInteractive", "-Command", MANAGER_TASK_HARNESS],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            env=environment,
        )

    def test_accepts_exact_pt30m_with_logon_trigger(self) -> None:
        completed = self.invoke("PT30M")
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_rejects_old_pt15m_interval(self) -> None:
        completed = self.invoke("PT15M")
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("not exactly 30 minutes", completed.stderr)

    def test_rejects_missing_logon_trigger(self) -> None:
        completed = self.invoke("PT30M", include_logon=False)
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("exactly one logon trigger", completed.stderr)


if __name__ == "__main__":
    unittest.main()
