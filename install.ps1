#requires -Version 7.0

<#
.SYNOPSIS
Installs the Codex Reset Credit Manager or creates one controller-owned job.

.DESCRIPTION
Normal mode installs immutable guard, manager, and installer files, creates the
ManagerSync safety-validation and reconciliation task and Start Menu shortcut, and opens the
manager once.  It never enables continuous operation and never creates a second
job while adopting an existing v1 job.

-ManagerChildOnly is an internal controller mode.  It performs the original
fail-closed enrollment flow for exactly one uniquely earliest credit, emits the
created job as JSON, and creates no manager task, shortcut, or UI process.

Windows Time configuration is intentionally opt-in because it changes a machine
service. Use -ConfigureWindowsTime to permit a narrowly scoped UAC repair only
when verification fails.  -InteractiveSetup explains the problem and asks
before showing the same UAC prompt.

-WhatIf discovers and validates local prerequisites, but does not copy files,
invoke Python, configure time, register a task, create a shortcut, or open UI.
#>

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter()]
    [string] $SourceRunner,

    [Parameter()]
    [string] $SourceManager,

    [Parameter()]
    [string] $SourceInstaller,

    [Parameter()]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.json$')]
    [string] $ManifestName = ("reset-credit-{0}.json" -f [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmssZ')),

    [Parameter()]
    [string] $TaskName,

    [Parameter()]
    [string] $PythonPath,

    [Parameter()]
    [string] $CodexPath,

    [Parameter()]
    [ValidateRange(10, 60)]
    [int] $MinimumLeadTimeMinutes = 10,

    [Parameter()]
    [switch] $ConfigureWindowsTime,

    [Parameter()]
    [switch] $ReplaceExistingTask,

    [Parameter()]
    [switch] $ManagerChildOnly,

    [Parameter()]
    [switch] $InteractiveSetup
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$TaskFolder = '\CodexResetCredit\'
$ManagerTaskName = 'ManagerSync'
$ManagerSyncIntervalMinutes = 30
$ManagerShortcutName = 'Codex Reset Credit Manager.lnk'
# This literal is intentionally retained solely to remove the exact shortcut
# created by older Korean-language releases. It must never be used as a glob.
$LegacyManagerShortcutName = 'Codex 초기화권 자동 사용.lnk'
$CodexPathEnvironmentVariable = 'CODEX_RESET_GUARD_CODEX_PATH'
$RuntimeInstallerEnvironmentVariable = 'CODEX_RESET_MANAGER_RUNTIME_INSTALLER'
$RuntimeGuardEnvironmentVariable = 'CODEX_RESET_MANAGER_RUNTIME_GUARD'
$MinimumCodexVersion = [version] '0.144.1'
$TerminalManifestStates = @(
    'SUCCEEDED', 'NO_ACTION', 'FAILED', 'INDETERMINATE', 'DISARMED',
    'CLEANED', 'SUPERSEDED_CLI', 'CANCELLED'
)

function Assert-WindowsPowerShell7 {
    if (-not $IsWindows) {
        throw 'This installer only supports Windows.'
    }
    if ($PSVersionTable.PSVersion.Major -lt 7) {
        throw 'PowerShell 7 or newer is required.'
    }
    if (-not [Environment]::UserInteractive) {
        throw 'Run this installer from the interactive user session that will own the task.'
    }
    if ((Get-Process -Id $PID).SessionId -eq 0) {
        throw 'Session 0 is not supported; run from the intended interactive user session.'
    }
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw 'LOCALAPPDATA is unavailable for the current interactive user.'
    }
}

function Resolve-CanonicalFile {
    param(
        [Parameter(Mandatory)]
        [string] $Path,

        [Parameter(Mandatory)]
        [string] $Description
    )

    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction Stop
    if ($resolved.Provider.Name -ne 'FileSystem' -or -not (Test-Path -LiteralPath $resolved.Path -PathType Leaf)) {
        throw "$Description is not a filesystem file: $Path"
    }
    return [IO.Path]::GetFullPath($resolved.Path)
}

function Get-Python313 {
    param([string] $RequestedPath)

    $candidates = [Collections.Generic.List[string]]::new()
    if (-not [string]::IsNullOrWhiteSpace($RequestedPath)) {
        $candidates.Add((Resolve-CanonicalFile -Path $RequestedPath -Description 'Python executable'))
    }
    else {
        $launcher = Get-Command py.exe -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $launcher) {
            $discovered = & $launcher.Source -3.13 -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace(($discovered | Select-Object -First 1))) {
                $candidate = ($discovered | Select-Object -First 1).Trim()
                if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                    $candidates.Add([IO.Path]::GetFullPath($candidate))
                }
            }
        }

        foreach ($command in @(Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
            if (-not [string]::IsNullOrWhiteSpace($command.Source) -and (Test-Path -LiteralPath $command.Source -PathType Leaf)) {
                $fullPath = [IO.Path]::GetFullPath($command.Source)
                if (-not $candidates.Contains($fullPath)) {
                    $candidates.Add($fullPath)
                }
            }
        }
    }

    foreach ($candidate in $candidates) {
        try {
            $versionText = & $candidate -c 'import platform; print(platform.python_version())' 2>$null
            if ($LASTEXITCODE -ne 0) {
                continue
            }
            $version = [version] (($versionText | Select-Object -First 1).Trim())
            if ($version.Major -eq 3 -and $version.Minor -eq 13) {
                $pythonw = Join-Path ([IO.Path]::GetDirectoryName($candidate)) 'pythonw.exe'
                if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
                    continue
                }
                return [pscustomobject]@{
                    Path    = $candidate
                    WindowlessPath = [IO.Path]::GetFullPath($pythonw)
                    Version = $version.ToString()
                }
            }
        }
        catch {
            continue
        }
    }

    throw 'A working CPython 3.13 executable was not found. Supply -PythonPath explicitly.'
}

function Get-NpmNativeCodex {
    param([string] $RequestedPath)

    $packageRoot = $null
    $packageVersion = $null

    if ([string]::IsNullOrWhiteSpace($RequestedPath)) {
        $npm = Get-Command npm -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $npm) {
            throw 'npm was not found; the native Codex executable must come from the global @openai/codex package.'
        }

        $npmRootOutput = & $npm.Source root --global 2>$null
        if ($LASTEXITCODE -ne 0 -or $null -eq $npmRootOutput) {
            throw 'Unable to resolve the global npm package root.'
        }
        $npmRoot = ($npmRootOutput | Select-Object -First 1).Trim()
        $packageRoot = Join-Path $npmRoot '@openai\codex'
        $packageJsonPath = Join-Path $packageRoot 'package.json'
        if (-not (Test-Path -LiteralPath $packageJsonPath -PathType Leaf)) {
            throw "The global @openai/codex package was not found beneath $npmRoot."
        }

        $packageJson = Get-Content -Raw -LiteralPath $packageJsonPath | ConvertFrom-Json -Depth 20
        $packageVersion = [string] $packageJson.version
        if ([string]::IsNullOrWhiteSpace($packageVersion)) {
            throw 'The global @openai/codex package has no version.'
        }

        $architecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLowerInvariant()
        if ($architecture -notin @('x64', 'arm64')) {
            throw "Unsupported Windows architecture for Codex: $architecture"
        }

        $nativeRoot = Join-Path $packageRoot "node_modules\@openai\codex-win32-$architecture"
        $nativeCandidates = @(
            Get-ChildItem -LiteralPath $nativeRoot -Recurse -File -Filter 'codex.exe' -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -match '[\\/]vendor[\\/].*[\\/]bin[\\/]codex\.exe$' }
        )
        if ($nativeCandidates.Count -ne 1) {
            throw "Expected exactly one npm-native codex.exe for $architecture; found $($nativeCandidates.Count)."
        }
        $resolvedCodex = [IO.Path]::GetFullPath($nativeCandidates[0].FullName)
    }
    else {
        $resolvedCodex = Resolve-CanonicalFile -Path $RequestedPath -Description 'Codex executable'
        if ($resolvedCodex -notmatch '(?i)[\\/]node_modules[\\/]@openai[\\/]codex[\\/]node_modules[\\/]@openai[\\/]codex-win32-(x64|arm64)[\\/]vendor[\\/].*[\\/]bin[\\/]codex\.exe$') {
            throw '-CodexPath must identify the native executable inside the global npm @openai/codex platform package.'
        }

        $marker = "$([IO.Path]::DirectorySeparatorChar)node_modules$([IO.Path]::DirectorySeparatorChar)@openai$([IO.Path]::DirectorySeparatorChar)codex$([IO.Path]::DirectorySeparatorChar)"
        $markerIndex = $resolvedCodex.IndexOf($marker, [StringComparison]::OrdinalIgnoreCase)
        if ($markerIndex -lt 0) {
            throw 'Unable to locate the parent @openai/codex package for -CodexPath.'
        }
        $packageRoot = $resolvedCodex.Substring(0, $markerIndex + $marker.Length - 1)
        $packageJsonPath = Join-Path $packageRoot 'package.json'
        $packageJson = Get-Content -Raw -LiteralPath $packageJsonPath | ConvertFrom-Json -Depth 20
        $packageVersion = [string] $packageJson.version
    }

    $signature = Get-AuthenticodeSignature -LiteralPath $resolvedCodex
    if ($signature.Status -ne [Management.Automation.SignatureStatus]::Valid -or
        $null -eq $signature.SignerCertificate -or
        $signature.SignerCertificate.Subject -notmatch '(?i)\bO\s*=\s*"?OpenAI\b|\bCN\s*=\s*"?OpenAI\b') {
        throw 'The npm-native Codex executable does not have a valid OpenAI Authenticode signature.'
    }

    $versionOutput = & $resolvedCodex --version 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'The npm-native Codex executable failed its version check.'
    }
    $versionLine = (($versionOutput | Select-Object -First 1) -as [string]).Trim()
    if ($versionLine -notmatch '^codex-cli\s+(?<version>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$') {
        throw "Unexpected Codex version output: $versionLine"
    }
    $binaryVersionText = $Matches.version
    $numericVersionText = ($binaryVersionText -split '[-+]', 2)[0]
    $numericVersion = [version] $numericVersionText
    if ($numericVersion -lt $MinimumCodexVersion) {
        throw "Codex $binaryVersionText is too old; $MinimumCodexVersion or newer is required."
    }
    if (-not [string]::IsNullOrWhiteSpace($packageVersion) -and $packageVersion -ne $binaryVersionText) {
        throw "npm package version $packageVersion does not match native binary version $binaryVersionText."
    }

    return [pscustomobject]@{
        Path       = $resolvedCodex
        Version    = $binaryVersionText
        Sha256     = (Get-FileHash -LiteralPath $resolvedCodex -Algorithm SHA256).Hash.ToLowerInvariant()
        PackageRoot = [IO.Path]::GetFullPath($packageRoot)
    }
}

function Invoke-ElevatedWindowsTimeConfiguration {
    $pwsh = Join-Path $PSHOME 'pwsh.exe'
    if (-not (Test-Path -LiteralPath $pwsh -PathType Leaf)) {
        throw "Unable to locate pwsh.exe beneath $PSHOME."
    }

    $elevatedScript = @'
$ErrorActionPreference = 'Stop'
try {
    Set-Service -Name W32Time -StartupType Automatic
    $service = Get-Service -Name W32Time
    if ($service.Status -ne 'Running') {
        Start-Service -Name W32Time
    }

    & "$env:SystemRoot\System32\w32tm.exe" /config '/manualpeerlist:time.windows.com,0x9' /syncfromflags:manual /update
    if ($LASTEXITCODE -ne 0) { throw "w32tm configuration failed with exit code $LASTEXITCODE." }

    Restart-Service -Name W32Time -Force
    & "$env:SystemRoot\System32\w32tm.exe" /resync /force
    if ($LASTEXITCODE -ne 0) { throw "w32tm resync failed with exit code $LASTEXITCODE." }
    exit 0
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
'@

    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($elevatedScript))
    try {
        $process = Start-Process -FilePath $pwsh `
            -ArgumentList @('-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded) `
            -Verb RunAs -WindowStyle Hidden -Wait -PassThru
    }
    catch {
        throw "Windows Time configuration was not authorized or could not start: $($_.Exception.Message)"
    }
    if ($process.ExitCode -ne 0) {
        throw "Elevated Windows Time configuration failed with exit code $($process.ExitCode)."
    }
}

function Get-WindowsTimeHealth {
    $service = Get-Service -Name W32Time -ErrorAction Stop
    if ($service.Status -ne 'Running') {
        throw 'Windows Time (W32Time) is not running.'
    }

    $w32tm = Join-Path $env:SystemRoot 'System32\w32tm.exe'
    [Text.Encoding]::RegisterProvider([Text.CodePagesEncodingProvider]::Instance)
    $nativeEncoding = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage)
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $w32tm
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.StandardOutputEncoding = $nativeEncoding
    $startInfo.StandardErrorEncoding = $nativeEncoding
    $startInfo.ArgumentList.Add('/query')
    $startInfo.ArgumentList.Add('/status')
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw 'w32tm did not start.'
        }
        $standardOutput = $process.StandardOutput.ReadToEndAsync()
        $standardError = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $statusText = $standardOutput.GetAwaiter().GetResult()
        $errorText = $standardError.GetAwaiter().GetResult()
        $exitCode = $process.ExitCode
    }
    finally {
        $process.Dispose()
    }
    if ($exitCode -ne 0) {
        $errorSuffix = if ([string]::IsNullOrWhiteSpace($errorText)) { '' } else { ": $($errorText.Trim())" }
        throw "Unable to query Windows Time status (exit code $exitCode)$errorSuffix"
    }

    $nonEmptyLines = @($statusText -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($nonEmptyLines.Count -eq 0 -or $nonEmptyLines[0] -notmatch ':\s*(?<leap>[0-3])(?:\D|$)') {
        throw 'Unable to parse the Windows Time leap indicator; refusing live enrollment.'
    }
    $leapIndicator = [int] $Matches.leap
    if ($leapIndicator -eq 3) {
        throw 'Windows Time reports an unsynchronized leap indicator.'
    }
    if ($statusText -match '(?i)Local\s+CMOS\s+Clock|로컬\s*CMOS|unsynchroni[sz]ed|동기화되지\s*않') {
        throw 'Windows Time is using Local CMOS Clock or reports an unsynchronized state.'
    }

    $sourceLine = $nonEmptyLines | Where-Object { $_ -match '(?i)^\s*(Source|원본)\s*:' } | Select-Object -First 1
    if ($null -eq $sourceLine) {
        throw 'Unable to identify the active Windows Time source.'
    }
    $source = ($sourceLine -replace '^\s*[^:]+:\s*', '').Trim()
    if ([string]::IsNullOrWhiteSpace($source) -or $source -match '(?i)Local\s+CMOS\s+Clock|로컬\s*CMOS') {
        throw 'Windows Time has no acceptable synchronized source.'
    }

    return [pscustomobject]@{
        Source = $source
        LeapIndicator = $leapIndicator
    }
}

function Set-PrivateInstallAcl {
    param(
        [Parameter(Mandatory)]
        [string] $InstallRoot,

        [Parameter(Mandatory)]
        [string] $UserName
    )

    $userSid = ([Security.Principal.NTAccount] $UserName).Translate(
        [Security.Principal.SecurityIdentifier]
    )
    $systemSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $allowed = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    $null = $allowed.Add($userSid.Value)
    $null = $allowed.Add($systemSid.Value)

    # icacls updates the DACL only and therefore works without
    # SeSecurityPrivilege on repeat installs. Remove inherited rules first,
    # then remove every unexpected explicit trustee before granting the two
    # exact allow rules.
    $icacls = Join-Path $env:SystemRoot 'System32\icacls.exe'
    & $icacls $InstallRoot '/inheritance:r' | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to disable ACL inheritance on $InstallRoot."
    }
    $seenRules = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    foreach ($entry in @((Get-Acl -LiteralPath $InstallRoot).Access)) {
        $entrySid = $entry.IdentityReference.Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
        if ($allowed.Contains($entrySid)) {
            continue
        }
        $removeKind = if (
            $entry.AccessControlType -eq [Security.AccessControl.AccessControlType]::Deny
        ) { '/remove:d' } else { '/remove:g' }
        $ruleKey = "$removeKind|$entrySid"
        if ($seenRules.Add($ruleKey)) {
            & $icacls $InstallRoot $removeKind $entry.IdentityReference.Value | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to remove an unexpected ACL rule from $InstallRoot."
            }
        }
    }
    & $icacls $InstallRoot '/grant:r' "${UserName}:(OI)(CI)F" 'SYSTEM:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to grant the private runtime ACL on $InstallRoot."
    }

    $descriptor = Get-Acl -LiteralPath $InstallRoot
    $readback = @($descriptor.Access)
    if (-not $descriptor.AreAccessRulesProtected) {
        throw 'Runtime ACL inheritance is unexpectedly enabled.'
    }
    if ($readback.Count -ne 2) {
        throw 'Runtime ACL contains an unexpected number of access rules.'
    }
    foreach ($entry in $readback) {
        $entrySid = $entry.IdentityReference.Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
        if (
            -not $allowed.Contains($entrySid) -or
            $entry.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            ($entry.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -ne
                [Security.AccessControl.FileSystemRights]::FullControl -or
            $entry.InheritanceFlags -ne (
                [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
                [Security.AccessControl.InheritanceFlags]::ObjectInherit
            ) -or
            $entry.IsInherited
        ) {
            throw 'Runtime ACL readback is not restricted to current user and SYSTEM full control.'
        }
    }
}

function Invoke-Guard {
    param(
        [Parameter(Mandatory)]
        [string] $Python,

        [Parameter(Mandatory)]
        [string] $Runner,

        [Parameter(Mandatory)]
        [string] $NativeCodex,

        [Parameter(Mandatory)]
        [string[]] $Arguments
    )

    $oldValue = [Environment]::GetEnvironmentVariable($CodexPathEnvironmentVariable, 'Process')
    try {
        [Environment]::SetEnvironmentVariable($CodexPathEnvironmentVariable, $NativeCodex, 'Process')
        & $Python $Runner @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Guard command '$($Arguments[0])' failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        [Environment]::SetEnvironmentVariable($CodexPathEnvironmentVariable, $oldValue, 'Process')
    }
}

function Get-JsonPathValue {
    param(
        [Parameter(Mandatory)]
        [object] $Object,

        [Parameter(Mandatory)]
        [string[]] $CandidatePaths,

        [Parameter(Mandatory)]
        [string] $Description
    )

    foreach ($candidatePath in $CandidatePaths) {
        $current = $Object
        $found = $true
        foreach ($segment in $candidatePath.Split('.')) {
            if ($null -eq $current) {
                $found = $false
                break
            }
            $property = $current.PSObject.Properties[$segment]
            if ($null -eq $property) {
                $found = $false
                break
            }
            $current = $property.Value
        }
        if ($found -and $null -ne $current) {
            return $current
        }
    }

    throw "Manifest is missing $Description."
}

function ConvertFrom-ManifestUtc {
    param(
        [Parameter(Mandatory)]
        [object] $Value,

        [Parameter(Mandatory)]
        [string] $Description
    )

    if ($Value -is [DateTimeOffset]) {
        if ($Value.Offset -ne [TimeSpan]::Zero) {
            throw "$Description is not UTC: $Value"
        }
        return $Value.ToUniversalTime()
    }
    if ($Value -is [DateTime]) {
        if ($Value.Kind -ne [DateTimeKind]::Utc) {
            throw "$Description lost its explicit UTC kind: $Value"
        }
        return [DateTimeOffset]::new($Value)
    }

    $text = [string] $Value
    if ($text -notmatch '(?:Z|\+00:00)$') {
        throw "$Description must contain an explicit UTC offset: $text"
    }
    $parsed = [DateTimeOffset]::Parse(
        $text,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::AdjustToUniversal
    )
    if ($parsed.Offset -ne [TimeSpan]::Zero) {
        throw "$Description is not UTC: $text"
    }
    return $parsed
}

function Quote-WindowsArgument {
    param([Parameter(Mandatory)][string] $Value)
    if ($Value.Contains('"')) {
        throw 'A scheduled-task path contains an unsupported quotation mark.'
    }
    return '"' + $Value + '"'
}

function Ensure-TaskFolder {
    param([Parameter(Mandatory)][string] $Path)

    $folderName = $Path.Trim('\')
    if ($folderName.Contains('\') -or [string]::IsNullOrWhiteSpace($folderName)) {
        throw "Only a single-level task folder is supported: $Path"
    }
    $comPath = "\$folderName"
    $service = New-Object -ComObject 'Schedule.Service'
    try {
        $service.Connect()
        try {
            $null = $service.GetFolder($comPath)
        }
        catch {
            $root = $service.GetFolder('\')
            $null = $root.CreateFolder($folderName, $null)
        }
    }
    finally {
        if ($null -ne $service) {
            [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($service)
        }
    }
}

function Resolve-AccountSid {
    param([Parameter(Mandatory)][string] $Account)

    if ($Account -match '^S-1-\d+(?:-\d+)+$') {
        return $Account
    }
    $ntAccount = [Security.Principal.NTAccount]::new($Account)
    return $ntAccount.Translate([Security.Principal.SecurityIdentifier]).Value
}

function Get-RequiredXmlText {
    param(
        [Parameter(Mandatory)]
        [xml] $Document,

        [Parameter(Mandatory)]
        [string] $XPath,

        [Parameter(Mandatory)]
        [string] $Description
    )

    $node = $Document.SelectSingleNode($XPath)
    if ($null -eq $node -or [string]::IsNullOrWhiteSpace($node.InnerText)) {
        throw "Registered task is missing $Description."
    }
    return $node.InnerText.Trim()
}

function Assert-DateTimeNear {
    param(
        [Parameter(Mandatory)]
        [DateTimeOffset] $Actual,

        [Parameter(Mandatory)]
        [DateTimeOffset] $Expected,

        [Parameter(Mandatory)]
        [string] $Description
    )

    if ([Math]::Abs(($Actual.UtcDateTime - $Expected.UtcDateTime).TotalSeconds) -gt 1) {
        throw "$Description differs from the manifest."
    }
}

function Assert-RegisteredTask {
    param(
        [Parameter(Mandatory)]
        [string] $Name,

        [Parameter(Mandatory)]
        [string] $Folder,

        [Parameter(Mandatory)]
        [string] $ExpectedUser,

        [Parameter(Mandatory)]
        [string] $ExpectedPython,

        [Parameter(Mandatory)]
        [string] $ExpectedArguments,

        [Parameter(Mandatory)]
        [string] $ExpectedWorkingDirectory,

        [Parameter(Mandatory)]
        [DateTimeOffset] $ExpectedStart,

        [Parameter(Mandatory)]
        [DateTimeOffset] $ExpectedEnd
    )

    $task = Get-ScheduledTask -TaskName $Name -TaskPath $Folder -ErrorAction Stop
    if ($task.State -eq 'Disabled') {
        throw 'The newly registered task is unexpectedly disabled.'
    }
    if ($task.Principal.RunLevel.ToString() -ne 'Limited') {
        throw 'The registered task is not configured for least privilege.'
    }
    if ($task.Principal.LogonType.ToString() -ne 'Interactive') {
        throw 'The registered task is not restricted to an interactive logon token.'
    }
    if ((Resolve-AccountSid -Account $task.Principal.UserId) -ne (Resolve-AccountSid -Account $ExpectedUser)) {
        throw 'The registered task principal differs from the current interactive user.'
    }

    [xml] $xml = Export-ScheduledTask -TaskName $Name -TaskPath $Folder -ErrorAction Stop
    $taskRoot = "/*[local-name()='Task']"
    $settingsRoot = "$taskRoot/*[local-name()='Settings']"
    $actionRoot = "$taskRoot/*[local-name()='Actions']/*[local-name()='Exec']"
    $triggerRoot = "$taskRoot/*[local-name()='Triggers']/*[local-name()='TimeTrigger']"

    if ((Get-RequiredXmlText -Document $xml -XPath "$settingsRoot/*[local-name()='WakeToRun']" -Description 'WakeToRun') -ne 'true') {
        throw 'WakeToRun was not preserved in the registered task.'
    }
    if ((Get-RequiredXmlText -Document $xml -XPath "$settingsRoot/*[local-name()='StartWhenAvailable']" -Description 'StartWhenAvailable') -ne 'true') {
        throw 'StartWhenAvailable was not preserved in the registered task.'
    }
    if ((Get-RequiredXmlText -Document $xml -XPath "$settingsRoot/*[local-name()='MultipleInstancesPolicy']" -Description 'MultipleInstancesPolicy') -ne 'IgnoreNew') {
        throw 'The registered task does not use IgnoreNew.'
    }
    if ((Get-RequiredXmlText -Document $xml -XPath "$settingsRoot/*[local-name()='AllowStartOnDemand']" -Description 'AllowStartOnDemand') -ne 'false') {
        throw 'The registered task permits demand starts.'
    }
    if ((Get-RequiredXmlText -Document $xml -XPath "$settingsRoot/*[local-name()='DisallowStartIfOnBatteries']" -Description 'DisallowStartIfOnBatteries') -ne 'false') {
        throw 'The registered task cannot start while running on battery power.'
    }
    if ((Get-RequiredXmlText -Document $xml -XPath "$settingsRoot/*[local-name()='StopIfGoingOnBatteries']" -Description 'StopIfGoingOnBatteries') -ne 'false') {
        throw 'The registered task would stop after switching to battery power.'
    }

    $command = Get-RequiredXmlText -Document $xml -XPath "$actionRoot/*[local-name()='Command']" -Description 'action command'
    $arguments = Get-RequiredXmlText -Document $xml -XPath "$actionRoot/*[local-name()='Arguments']" -Description 'action arguments'
    $workingDirectory = Get-RequiredXmlText -Document $xml -XPath "$actionRoot/*[local-name()='WorkingDirectory']" -Description 'working directory'
    if ([IO.Path]::GetFullPath($command) -ne [IO.Path]::GetFullPath($ExpectedPython)) {
        throw 'The registered task Python executable differs from the installed runtime.'
    }
    if ($arguments -ne $ExpectedArguments) {
        throw 'The registered task arguments differ from the requested guarded live command.'
    }
    if ([IO.Path]::GetFullPath($workingDirectory) -ne [IO.Path]::GetFullPath($ExpectedWorkingDirectory)) {
        throw 'The registered task working directory differs from the runtime directory.'
    }

    $startText = Get-RequiredXmlText -Document $xml -XPath "$triggerRoot/*[local-name()='StartBoundary']" -Description 'start boundary'
    $endText = Get-RequiredXmlText -Document $xml -XPath "$triggerRoot/*[local-name()='EndBoundary']" -Description 'end boundary'
    Assert-DateTimeNear -Actual ([DateTimeOffset]::Parse($startText)) -Expected $ExpectedStart -Description 'Task start boundary'
    Assert-DateTimeNear -Actual ([DateTimeOffset]::Parse($endText)) -Expected $ExpectedEnd -Description 'Task end boundary'
}

function Get-ManifestInventory {
    param([Parameter(Mandatory)][string] $Directory)

    $result = [Collections.Generic.List[object]]::new()
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
        return @()
    }
    foreach ($file in @(Get-ChildItem -LiteralPath $Directory -File -Filter '*.json' -ErrorAction Stop)) {
        try {
            $manifest = Get-Content -Raw -LiteralPath $file.FullName | ConvertFrom-Json -Depth 100
        }
        catch {
            throw "Cannot safely classify manifest $($file.FullName): $($_.Exception.Message)"
        }
        $stateProperty = $manifest.PSObject.Properties['state']
        $jobProperty = $manifest.PSObject.Properties['jobId']
        if ($null -eq $stateProperty -or [string]::IsNullOrWhiteSpace([string] $stateProperty.Value) -or
            $null -eq $jobProperty -or [string]::IsNullOrWhiteSpace([string] $jobProperty.Value)) {
            throw "Cannot safely classify manifest without state and jobId: $($file.FullName)"
        }
        $state = ([string] $stateProperty.Value).ToUpperInvariant()
        $result.Add([pscustomobject]@{
            Path = [IO.Path]::GetFullPath($file.FullName)
            JobId = [string] $jobProperty.Value
            State = $state
            Terminal = $state -in $TerminalManifestStates
        })
    }
    return @($result)
}

function Assert-ManagerChildAdmission {
    param(
        [Parameter(Mandatory)][string] $PolicyPath,
        [Parameter(Mandatory)][string] $ManifestsDirectory,
        [Parameter(Mandatory)][string] $InstallRoot,
        [Parameter(Mandatory)][string] $ExecutingInstaller,
        [Parameter(Mandatory)][string] $GuardPath,
        [Parameter(Mandatory)][string] $GuardSha256,
        [Parameter(Mandatory)][object] $ObservedCodex
    )

    if (-not (Test-Path -LiteralPath $PolicyPath -PathType Leaf)) {
        throw '-ManagerChildOnly requires an installed controller policy.'
    }
    try {
        $policy = Get-Content -Raw -LiteralPath $PolicyPath | ConvertFrom-Json -Depth 100
    }
    catch {
        throw "Controller policy cannot be read safely: $($_.Exception.Message)"
    }
    $enabledProperty = $policy.PSObject.Properties['enabled']
    if ($null -eq $enabledProperty -or $enabledProperty.Value -isnot [bool] -or -not [bool] $enabledProperty.Value) {
        throw '-ManagerChildOnly is allowed only while automatic operation is enabled.'
    }

    $blockedProperty = $policy.PSObject.Properties['blocked']
    if ($null -eq $blockedProperty) {
        throw 'Controller policy has no blocked-state field; refusing child enrollment.'
    }
    if ($null -ne $blockedProperty.Value) {
        throw 'Controller policy is blocked; refusing child enrollment until the controller clears it.'
    }

    # The controller may invoke only the exact content-addressed installer and
    # guard paths pinned in policy.  This prevents a copied/development
    # installer or a substituted guard from minting a live one-shot job.
    $runtimeInstallerProperty = $policy.PSObject.Properties['runtimeInstaller']
    $runtimeGuardProperty = $policy.PSObject.Properties['runtimeGuard']
    if ($null -eq $runtimeInstallerProperty -or
        [string]::IsNullOrWhiteSpace([string] $runtimeInstallerProperty.Value) -or
        $null -eq $runtimeGuardProperty -or
        [string]::IsNullOrWhiteSpace([string] $runtimeGuardProperty.Value)) {
        throw 'Controller policy does not pin both immutable installer and guard runtimes.'
    }

    $policyInstaller = Resolve-CanonicalFile -Path ([string] $runtimeInstallerProperty.Value) -Description 'Policy installer runtime'
    $policyGuard = Resolve-CanonicalFile -Path ([string] $runtimeGuardProperty.Value) -Description 'Policy guard runtime'
    $actualInstaller = Resolve-CanonicalFile -Path $ExecutingInstaller -Description 'Executing installer runtime'
    $actualGuard = Resolve-CanonicalFile -Path $GuardPath -Description 'Selected guard runtime'
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals($policyInstaller, $actualInstaller)) {
        throw 'The executing installer does not match the immutable runtime pinned in controller policy.'
    }
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals($policyGuard, $actualGuard)) {
        throw 'The selected guard does not match the immutable runtime pinned in controller policy.'
    }

    $expectedInstallersDirectory = [IO.Path]::GetFullPath((Join-Path $InstallRoot 'installers'))
    $expectedRunnersDirectory = [IO.Path]::GetFullPath((Join-Path $InstallRoot 'runners'))
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals([IO.Path]::GetDirectoryName($actualInstaller), $expectedInstallersDirectory) -or
        [IO.Path]::GetFileName($actualInstaller) -notmatch '^install-(?<digest>[0-9a-f]{64})\.ps1$') {
        throw 'The executing installer is not an immutable content-addressed installer runtime.'
    }
    $installerNameHash = $Matches.digest.ToLowerInvariant()
    $installerContentHash = (Get-FileHash -LiteralPath $actualInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($installerNameHash -ne $installerContentHash) {
        throw 'The executing installer content no longer matches its immutable filename.'
    }
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals([IO.Path]::GetDirectoryName($actualGuard), $expectedRunnersDirectory) -or
        [IO.Path]::GetFileName($actualGuard) -notmatch '^codex_reset_guard-(?<digest>[0-9a-f]{64})\.py$') {
        throw 'The selected guard is not an immutable content-addressed guard runtime.'
    }
    $guardNameHash = $Matches.digest.ToLowerInvariant()
    if ($GuardSha256 -notmatch '^[0-9a-f]{64}$' -or
        $guardNameHash -ne $GuardSha256.ToLowerInvariant()) {
        throw 'The selected guard content no longer matches its immutable filename.'
    }

    # A child job is authorized only for the exact CLI triplet that the
    # controller approved after its read-only compatibility validation.
    $approvedProperty = $policy.PSObject.Properties['approvedCli']
    if ($null -eq $approvedProperty -or $null -eq $approvedProperty.Value) {
        throw 'Controller policy has no approved Codex CLI pin.'
    }
    $approved = $approvedProperty.Value
    $approvedPathProperty = $approved.PSObject.Properties['codexExe']
    $approvedVersionProperty = $approved.PSObject.Properties['codexVersion']
    $approvedHashProperty = $approved.PSObject.Properties['codexSha256']
    if ($null -eq $approvedPathProperty -or
        [string]::IsNullOrWhiteSpace([string] $approvedPathProperty.Value) -or
        $null -eq $approvedVersionProperty -or
        [string]::IsNullOrWhiteSpace([string] $approvedVersionProperty.Value) -or
        $null -eq $approvedHashProperty -or
        ([string] $approvedHashProperty.Value) -notmatch '^[0-9a-fA-F]{64}$') {
        throw 'Controller policy has an invalid approved Codex CLI pin.'
    }
    $approvedPath = Resolve-CanonicalFile -Path ([string] $approvedPathProperty.Value) -Description 'Approved Codex executable'
    $approvedVersion = [string] $approvedVersionProperty.Value
    $approvedHash = ([string] $approvedHashProperty.Value).ToLowerInvariant()
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals($approvedPath, [string] $ObservedCodex.Path) -or
        $approvedVersion -notin @([string] $ObservedCodex.Version, "codex-cli $($ObservedCodex.Version)") -or
        $approvedHash -ne [string] $ObservedCodex.Sha256) {
        throw 'The requested Codex CLI does not match the path, version, and hash approved in controller policy.'
    }

    $inventory = @(Get-ManifestInventory -Directory $ManifestsDirectory)
    $nonterminal = @($inventory | Where-Object { -not $_.Terminal })
    if ($nonterminal.Count -ne 0) {
        $summary = ($nonterminal | ForEach-Object { "$($_.JobId):$($_.State)" }) -join ', '
        throw "Another nonterminal manifest already exists; refusing a second job: $summary"
    }

    # A controller may retain its last terminal job for audit.  It must not
    # claim a current nonterminal job while asking the installer for another.
    $currentProperty = $policy.PSObject.Properties['currentJob']
    if ($null -ne $currentProperty -and $null -ne $currentProperty.Value) {
        $current = $currentProperty.Value
        $currentState = $null
        if ($current -is [string]) {
            $matching = @($inventory | Where-Object { $_.JobId -eq [string] $current })
            if ($matching.Count -eq 1) { $currentState = $matching[0].State }
        }
        else {
            $stateProperty = $current.PSObject.Properties['state']
            if ($null -ne $stateProperty) { $currentState = ([string] $stateProperty.Value).ToUpperInvariant() }
        }
        if (-not [string]::IsNullOrWhiteSpace($currentState) -and $currentState -notin $TerminalManifestStates) {
            throw "Controller policy still marks its current job as nonterminal: $currentState"
        }
    }
}

function Invoke-Manager {
    param(
        [Parameter(Mandatory)][string] $Python,
        [Parameter(Mandatory)][string] $Manager,
        [Parameter(Mandatory)][string] $RuntimeInstaller,
        [Parameter(Mandatory)][string] $RuntimeGuard,
        [Parameter(Mandatory)][string[]] $Arguments
    )

    $oldInstaller = [Environment]::GetEnvironmentVariable($RuntimeInstallerEnvironmentVariable, 'Process')
    $oldGuard = [Environment]::GetEnvironmentVariable($RuntimeGuardEnvironmentVariable, 'Process')
    try {
        [Environment]::SetEnvironmentVariable($RuntimeInstallerEnvironmentVariable, $RuntimeInstaller, 'Process')
        [Environment]::SetEnvironmentVariable($RuntimeGuardEnvironmentVariable, $RuntimeGuard, 'Process')
        $output = @(& $Python $Manager @Arguments)
        if ($LASTEXITCODE -ne 0) {
            throw "Manager command '$($Arguments[0])' failed with exit code $LASTEXITCODE."
        }
        return $output
    }
    finally {
        [Environment]::SetEnvironmentVariable($RuntimeInstallerEnvironmentVariable, $oldInstaller, 'Process')
        [Environment]::SetEnvironmentVariable($RuntimeGuardEnvironmentVariable, $oldGuard, 'Process')
    }
}

function Get-FileByteSnapshot {
    param([Parameter(Mandatory)][string] $Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return [pscustomobject]@{ Exists = $false; Bytes = $null }
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Cannot snapshot a non-file path: $Path"
    }
    return [pscustomobject]@{
        Exists = $true
        Bytes = [IO.File]::ReadAllBytes($Path)
    }
}

function Restore-FileByteSnapshot {
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][object] $Snapshot
    )

    $existsProperty = $Snapshot.PSObject.Properties['Exists']
    $bytesProperty = $Snapshot.PSObject.Properties['Bytes']
    if ($null -eq $existsProperty -or $existsProperty.Value -isnot [bool]) {
        throw "Invalid file rollback snapshot for $Path."
    }
    if (-not [bool] $existsProperty.Value) {
        if (Test-Path -LiteralPath $Path) {
            if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
                throw "Refusing to remove a non-file during rollback: $Path"
            }
            Remove-Item -LiteralPath $Path -Force
        }
        return
    }
    if ($null -eq $bytesProperty -or $bytesProperty.Value -isnot [byte[]]) {
        throw "File rollback snapshot has no byte content for $Path."
    }

    $directory = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Path))
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        $null = New-Item -ItemType Directory -Path $directory -Force
    }
    $temporary = Join-Path $directory ('.{0}.rollback-{1}.tmp' -f [IO.Path]::GetFileName($Path), [guid]::NewGuid().ToString('N'))
    try {
        [IO.File]::WriteAllBytes($temporary, [byte[]] $bytesProperty.Value)
        if (Test-Path -LiteralPath $Path) {
            if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
                throw "Refusing to replace a non-file during rollback: $Path"
            }
            # File.Move with overwrite uses a same-volume replacement on the
            # local Windows installation and Start Menu volumes.
            [IO.File]::Move($temporary, $Path, $true)
        }
        else {
            [IO.File]::Move($temporary, $Path)
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-ManagerTaskSnapshot {
    $task = Get-ScheduledTask -TaskName $ManagerTaskName -TaskPath $TaskFolder -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        return [pscustomobject]@{ Exists = $false; Xml = $null }
    }
    if (@($task).Count -ne 1) {
        throw 'ManagerSync snapshot resolved more than one scheduled task.'
    }
    $xml = [string] (Export-ScheduledTask -TaskName $ManagerTaskName -TaskPath $TaskFolder -ErrorAction Stop)
    if ([string]::IsNullOrWhiteSpace($xml)) {
        throw 'ManagerSync exported an empty task definition.'
    }
    return [pscustomobject]@{ Exists = $true; Xml = $xml }
}

function Restore-ManagerTaskSnapshot {
    param([Parameter(Mandatory)][object] $Snapshot)

    $existsProperty = $Snapshot.PSObject.Properties['Exists']
    $xmlProperty = $Snapshot.PSObject.Properties['Xml']
    if ($null -eq $existsProperty -or $existsProperty.Value -isnot [bool]) {
        throw 'Invalid ManagerSync rollback snapshot.'
    }
    if ([bool] $existsProperty.Value) {
        if ($null -eq $xmlProperty -or [string]::IsNullOrWhiteSpace([string] $xmlProperty.Value)) {
            throw 'ManagerSync rollback snapshot has no XML definition.'
        }
        Ensure-TaskFolder -Path $TaskFolder
        $null = Register-ScheduledTask `
            -TaskName $ManagerTaskName `
            -TaskPath $TaskFolder `
            -Xml ([string] $xmlProperty.Value) `
            -Force
        $restoredXml = [string] (Export-ScheduledTask -TaskName $ManagerTaskName -TaskPath $TaskFolder -ErrorAction Stop)
        if ([string]::IsNullOrWhiteSpace($restoredXml)) {
            throw 'ManagerSync rollback registration could not be read back.'
        }
        return
    }

    $current = Get-ScheduledTask -TaskName $ManagerTaskName -TaskPath $TaskFolder -ErrorAction SilentlyContinue
    if ($null -ne $current) {
        Unregister-ScheduledTask -TaskName $ManagerTaskName -TaskPath $TaskFolder -Confirm:$false -ErrorAction Stop
    }
}

function Get-Sha256Hex {
    param([Parameter(Mandatory)][byte[]] $Bytes)

    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return [Convert]::ToHexString($algorithm.ComputeHash($Bytes)).ToLowerInvariant()
    }
    finally { $algorithm.Dispose() }
}

function Get-OneShotTaskContract {
    param([Parameter(Mandatory)][string] $XmlText)

    try { [xml] $xml = $XmlText }
    catch { throw "Active one-shot task XML is invalid: $($_.Exception.Message)" }
    $root = "/*[local-name()='Task']"
    $exec = "$root/*[local-name()='Actions']/*[local-name()='Exec']"
    $trigger = "$root/*[local-name()='Triggers']/*[local-name()='TimeTrigger']"
    return [pscustomobject]@{
        Command = Get-RequiredXmlText $xml "$exec/*[local-name()='Command']" 'active one-shot command'
        Arguments = Get-RequiredXmlText $xml "$exec/*[local-name()='Arguments']" 'active one-shot arguments'
        WorkingDirectory = Get-RequiredXmlText $xml "$exec/*[local-name()='WorkingDirectory']" 'active one-shot working directory'
        StartBoundary = Get-RequiredXmlText $xml "$trigger/*[local-name()='StartBoundary']" 'active one-shot start boundary'
        EndBoundary = Get-RequiredXmlText $xml "$trigger/*[local-name()='EndBoundary']" 'active one-shot end boundary'
    }
}

function Get-ActiveOneShotSnapshot {
    param(
        [Parameter(Mandatory)][AllowEmptyCollection()][object[]] $NonterminalInventory,
        [Parameter(Mandatory)][string] $ManifestDirectory,
        [int] $MinimumMinutesBeforeTrigger = 10
    )

    if ($NonterminalInventory.Count -eq 0) {
        return [pscustomobject]@{
            Exists = $false
            ManifestDirectory = [IO.Path]::GetFullPath($ManifestDirectory)
        }
    }
    if ($NonterminalInventory.Count -ne 1) {
        throw 'Exactly zero or one nonterminal manifest is required for manager installation.'
    }
    $manifestPath = [string] $NonterminalInventory[0].Path
    $manifestFile = Get-FileByteSnapshot -Path $manifestPath
    if (-not [bool] $manifestFile.Exists) { throw 'The active manifest disappeared during snapshot.' }
    try {
        $manifestText = [Text.UTF8Encoding]::new($false, $true).GetString([byte[]] $manifestFile.Bytes)
        $manifest = $manifestText | ConvertFrom-Json -Depth 100
    }
    catch { throw "The active manifest is not valid UTF-8 JSON: $($_.Exception.Message)" }
    if ([string] $manifest.state -cne 'ARMED' -or $manifest.armed -isnot [bool] -or -not [bool] $manifest.armed) {
        throw 'The sole nonterminal manifest must be armed before manager installation.'
    }
    $fullTaskName = [string] $manifest.task.name
    $match = [regex]::Match($fullTaskName, '^\\CodexResetCredit\\(?<leaf>Consume-[^\\]+)$', [Text.RegularExpressions.RegexOptions]::IgnoreCase)
    if (-not $match.Success) { throw 'The active manifest does not identify an exact Consume task.' }
    $leafName = $match.Groups['leaf'].Value
    $task = Get-ScheduledTask -TaskName $leafName -TaskPath $TaskFolder -ErrorAction Stop
    if (@($task).Count -ne 1) { throw 'The active manifest did not resolve exactly one Consume task.' }
    $taskState = $task.State.ToString()
    if ($taskState -in @('Running', 'Disabled')) {
        throw "The active Consume task is $taskState; manager installation is not safe now."
    }
    if ($task.TaskName -ne $leafName -or $task.TaskPath -ne $TaskFolder) {
        throw 'The active Consume task identity differs from its manifest.'
    }
    $taskXml = [string] (Export-ScheduledTask -TaskName $leafName -TaskPath $TaskFolder -ErrorAction Stop)
    if ([string]::IsNullOrWhiteSpace($taskXml)) { throw 'The active Consume task exported empty XML.' }
    $contract = Get-OneShotTaskContract -XmlText $taskXml
    try { $triggerAt = [DateTimeOffset]::Parse($contract.StartBoundary).ToUniversalTime() }
    catch { throw 'The active Consume task start boundary is invalid.' }
    if ($triggerAt -le [DateTimeOffset]::UtcNow.AddMinutes($MinimumMinutesBeforeTrigger)) {
        throw "The active Consume task is not more than $MinimumMinutesBeforeTrigger minutes from its trigger."
    }
    $manifestBytes = [byte[]] $manifestFile.Bytes
    return [pscustomobject]@{
        Exists = $true
        ManifestDirectory = [IO.Path]::GetFullPath($ManifestDirectory)
        ManifestPath = $manifestPath
        ManifestFile = $manifestFile
        ManifestSha256 = Get-Sha256Hex -Bytes $manifestBytes
        ManifestBase64 = [Convert]::ToBase64String($manifestBytes)
        TaskName = $fullTaskName
        TaskLeafName = $leafName
        TaskPath = $TaskFolder
        TaskState = $taskState
        TaskXml = $taskXml
        TaskXmlSha256 = Get-Sha256Hex -Bytes ([Text.Encoding]::UTF8.GetBytes($taskXml))
        Command = $contract.Command
        Arguments = $contract.Arguments
        WorkingDirectory = $contract.WorkingDirectory
        StartBoundary = $contract.StartBoundary
        EndBoundary = $contract.EndBoundary
    }
}

function Assert-ActiveOneShotUnchanged {
    param([Parameter(Mandatory)][object] $Snapshot)

    $currentNonterminal = @(
        Get-ManifestInventory -Directory $Snapshot.ManifestDirectory |
            Where-Object { -not $_.Terminal }
    )
    if (-not [bool] $Snapshot.Exists) {
        if ($currentNonterminal.Count -ne 0) {
            throw 'A new nonterminal manifest appeared during manager installation.'
        }
        return
    }
    if ($currentNonterminal.Count -ne 1 -or
        -not [StringComparer]::OrdinalIgnoreCase.Equals(
            [string] $currentNonterminal[0].Path,
            [string] $Snapshot.ManifestPath
        )) {
        throw 'The active manifest inventory changed during manager installation.'
    }
    if (-not (Test-Path -LiteralPath $Snapshot.ManifestPath -PathType Leaf)) {
        throw 'The active manifest disappeared during manager installation.'
    }
    $manifestBytes = [IO.File]::ReadAllBytes([string] $Snapshot.ManifestPath)
    if ((Get-Sha256Hex -Bytes $manifestBytes) -ne $Snapshot.ManifestSha256 -or
        [Convert]::ToBase64String($manifestBytes) -cne $Snapshot.ManifestBase64) {
        throw 'The active manifest changed during manager installation.'
    }
    $task = Get-ScheduledTask -TaskName $Snapshot.TaskLeafName -TaskPath $Snapshot.TaskPath -ErrorAction Stop
    if (@($task).Count -ne 1 -or $task.TaskName -ne $Snapshot.TaskLeafName -or
        $task.TaskPath -ne $Snapshot.TaskPath -or $task.State.ToString() -ne $Snapshot.TaskState) {
        throw 'The active Consume task identity or state changed during manager installation.'
    }
    $taskXml = [string] (Export-ScheduledTask -TaskName $Snapshot.TaskLeafName -TaskPath $Snapshot.TaskPath -ErrorAction Stop)
    $contract = Get-OneShotTaskContract -XmlText $taskXml
    if ((Get-Sha256Hex -Bytes ([Text.Encoding]::UTF8.GetBytes($taskXml))) -ne $Snapshot.TaskXmlSha256 -or
        $contract.Command -cne $Snapshot.Command -or
        $contract.Arguments -cne $Snapshot.Arguments -or
        $contract.WorkingDirectory -cne $Snapshot.WorkingDirectory -or
        $contract.StartBoundary -cne $Snapshot.StartBoundary -or
        $contract.EndBoundary -cne $Snapshot.EndBoundary) {
        throw 'The active Consume task XML, action, or schedule changed during manager installation.'
    }
}

function Assert-ActiveOneShotTriggerMargin {
    param(
        [Parameter(Mandatory)][object] $Snapshot,
        [int] $MinimumMinutesBeforeTrigger = 10
    )

    if (-not [bool] $Snapshot.Exists) { return }
    try { $triggerAt = [DateTimeOffset]::Parse([string] $Snapshot.StartBoundary).ToUniversalTime() }
    catch { throw 'The active Consume task snapshot has an invalid start boundary.' }
    if ($triggerAt -le [DateTimeOffset]::UtcNow.AddMinutes($MinimumMinutesBeforeTrigger)) {
        throw "The active Consume task is no longer more than $MinimumMinutesBeforeTrigger minutes from its trigger."
    }
}

function Assert-ManagerTask {
    param(
        [Parameter(Mandatory)][string] $ExpectedUser,
        [Parameter(Mandatory)][string] $ExpectedPython,
        [Parameter(Mandatory)][string] $ExpectedArguments,
        [Parameter(Mandatory)][string] $ExpectedWorkingDirectory
    )

    $task = Get-ScheduledTask -TaskName $ManagerTaskName -TaskPath $TaskFolder -ErrorAction Stop
    if ($task.State -eq 'Disabled') { throw 'ManagerSync is unexpectedly disabled.' }
    if ($task.Principal.RunLevel.ToString() -ne 'Limited') { throw 'ManagerSync is not least privilege.' }
    if ($task.Principal.LogonType.ToString() -ne 'Interactive') { throw 'ManagerSync is not interactive-logon only.' }
    if ((Resolve-AccountSid $task.Principal.UserId) -ne (Resolve-AccountSid $ExpectedUser)) {
        throw 'ManagerSync principal differs from the installing user.'
    }

    [xml] $xml = Export-ScheduledTask -TaskName $ManagerTaskName -TaskPath $TaskFolder -ErrorAction Stop
    $root = "/*[local-name()='Task']"
    $settings = "$root/*[local-name()='Settings']"
    $exec = "$root/*[local-name()='Actions']/*[local-name()='Exec']"
    # Task Scheduler commonly omits false-valued optional settings from the
    # exported XML. Missing WakeToRun therefore means the required default
    # false; an explicit value is accepted only when it is exactly false.
    $wakeNode = $xml.SelectSingleNode("$settings/*[local-name()='WakeToRun']")
    if ($null -ne $wakeNode -and $wakeNode.InnerText.Trim() -ne 'false') {
        throw 'ManagerSync must not wake the computer.'
    }
    if ((Get-RequiredXmlText $xml "$settings/*[local-name()='StartWhenAvailable']" 'ManagerSync StartWhenAvailable') -ne 'true') {
        throw 'ManagerSync does not use StartWhenAvailable.'
    }
    if ((Get-RequiredXmlText $xml "$settings/*[local-name()='MultipleInstancesPolicy']" 'ManagerSync MultipleInstancesPolicy') -ne 'IgnoreNew') {
        throw 'ManagerSync does not use IgnoreNew.'
    }
    $logonTriggers = @($xml.SelectNodes("$root/*[local-name()='Triggers']/*[local-name()='LogonTrigger']"))
    $timeTriggers = @($xml.SelectNodes("$root/*[local-name()='Triggers']/*[local-name()='TimeTrigger']"))
    if ($logonTriggers.Count -ne 1 -or $timeTriggers.Count -ne 1) {
        throw 'ManagerSync must have exactly one logon trigger and one repetition trigger.'
    }
    $intervalNode = $timeTriggers[0].SelectSingleNode("./*[local-name()='Repetition']/*[local-name()='Interval']")
    $expectedInterval = "PT$($ManagerSyncIntervalMinutes)M"
    if ($null -eq $intervalNode -or $intervalNode.InnerText.Trim() -ne $expectedInterval) {
        throw "ManagerSync repetition interval is not exactly $ManagerSyncIntervalMinutes minutes."
    }
    $command = Get-RequiredXmlText $xml "$exec/*[local-name()='Command']" 'ManagerSync action command'
    $arguments = Get-RequiredXmlText $xml "$exec/*[local-name()='Arguments']" 'ManagerSync action arguments'
    $working = Get-RequiredXmlText $xml "$exec/*[local-name()='WorkingDirectory']" 'ManagerSync working directory'
    if ([IO.Path]::GetFullPath($command) -ne [IO.Path]::GetFullPath($ExpectedPython) -or
        $arguments -ne $ExpectedArguments -or
        [IO.Path]::GetFullPath($working) -ne [IO.Path]::GetFullPath($ExpectedWorkingDirectory)) {
        throw 'ManagerSync action readback differs from the immutable manager runtime.'
    }
}

function Set-AndAssertManagerShortcut {
    param(
        [Parameter(Mandatory)][string] $ShortcutPath,
        [Parameter(Mandatory)][string] $Pythonw,
        [Parameter(Mandatory)][string] $Manager,
        [Parameter(Mandatory)][string] $WorkingDirectory
    )

    $shortcutDescription = 'Manage automatic Codex reset credit use'
    $shell = New-Object -ComObject WScript.Shell
    try {
        $shortcut = $shell.CreateShortcut($ShortcutPath)
        $shortcut.TargetPath = $Pythonw
        $shortcut.Arguments = "$(Quote-WindowsArgument $Manager) ui"
        $shortcut.WorkingDirectory = $WorkingDirectory
        $shortcut.Description = $shortcutDescription
        $shortcut.Save()

        $readback = $shell.CreateShortcut($ShortcutPath)
        if ([IO.Path]::GetFullPath($readback.TargetPath) -ne [IO.Path]::GetFullPath($Pythonw) -or
            $readback.Arguments -ne "$(Quote-WindowsArgument $Manager) ui" -or
            [IO.Path]::GetFullPath($readback.WorkingDirectory) -ne [IO.Path]::GetFullPath($WorkingDirectory) -or
            $readback.Description -ne $shortcutDescription) {
            throw 'Start Menu shortcut readback differs from the immutable manager runtime.'
        }
    }
    finally {
        if ($null -ne $shell) { [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) }
    }
}

function Get-InstalledManagerUiProcesses {
    param([Parameter(Mandatory)][string] $InstallRoot)

    $runnersRoot = [IO.Path]::GetFullPath((Join-Path $InstallRoot 'runners'))
    $pattern = '^\s*"(?<pythonw>[^"]*[\\/]pythonw\.exe)"\s+"(?<manager>[^"]+)"\s+ui\s*$'
    $result = [Collections.Generic.List[object]]::new()
    foreach ($process in @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction Stop)) {
        if ([string]::IsNullOrWhiteSpace([string] $process.CommandLine)) { continue }
        $match = [regex]::Match([string] $process.CommandLine, $pattern, [Text.RegularExpressions.RegexOptions]::IgnoreCase)
        if (-not $match.Success) { continue }
        $manager = Resolve-CanonicalFile -Path $match.Groups['manager'].Value -Description 'Running manager UI'
        if (-not [StringComparer]::OrdinalIgnoreCase.Equals([IO.Path]::GetDirectoryName($manager), $runnersRoot)) {
            continue
        }
        $nameMatch = [regex]::Match(
            [IO.Path]::GetFileName($manager),
            '^codex_reset_manager-(?<digest>[0-9a-f]{64})\.py$',
            [Text.RegularExpressions.RegexOptions]::IgnoreCase
        )
        if (-not $nameMatch.Success) {
            throw 'A running installed manager UI is not content-addressed.'
        }
        $contentHash = (Get-FileHash -LiteralPath $manager -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($contentHash -ne $nameMatch.Groups['digest'].Value.ToLowerInvariant()) {
            throw 'A running installed manager UI no longer matches its immutable filename.'
        }
        $pythonw = Resolve-CanonicalFile -Path $match.Groups['pythonw'].Value -Description 'Running manager Python runtime'
        if ([IO.Path]::GetFileName($pythonw) -ine 'pythonw.exe') {
            throw 'A running installed manager UI does not use pythonw.exe.'
        }
        $result.Add([pscustomobject]@{
            ProcessId = [int] $process.ProcessId
            Pythonw = $pythonw
            Manager = $manager
        })
    }
    return @($result | Sort-Object ProcessId)
}

function Suspend-ManagerSyncForInstall {
    param([Parameter(Mandatory)][object] $Snapshot)

    if (-not [bool] $Snapshot.Exists) { return }
    Disable-ScheduledTask -TaskName $ManagerTaskName -TaskPath $TaskFolder -ErrorAction Stop | Out-Null
    for ($attempt = 0; $attempt -lt 300; $attempt++) {
        $task = Get-ScheduledTask -TaskName $ManagerTaskName -TaskPath $TaskFolder -ErrorAction Stop
        if ($task.State.ToString() -ne 'Running') { return }
        Start-Sleep -Milliseconds 100
    }
    throw 'ManagerSync did not finish naturally within 30 seconds; installation was aborted without terminating it.'
}

function Assert-ManagerPolicyRuntimePins {
    param(
        [Parameter(Mandatory)][string] $PolicyPath,
        [Parameter(Mandatory)][string] $ExpectedInstaller,
        [Parameter(Mandatory)][string] $ExpectedGuard
    )

    try { $policy = Get-Content -Raw -LiteralPath $PolicyPath | ConvertFrom-Json -Depth 100 }
    catch { throw "Installed manager policy cannot be read back: $($_.Exception.Message)" }
    $installer = Resolve-CanonicalFile -Path ([string] $policy.runtimeInstaller) -Description 'Policy installer runtime'
    $guard = Resolve-CanonicalFile -Path ([string] $policy.runtimeGuard) -Description 'Policy guard runtime'
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals($installer, [IO.Path]::GetFullPath($ExpectedInstaller)) -or
        -not [StringComparer]::OrdinalIgnoreCase.Equals($guard, [IO.Path]::GetFullPath($ExpectedGuard))) {
        throw 'Manager policy did not pin the newly installed immutable runtimes.'
    }
}

function Enter-InstallerByteRangeLock {
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $Description
    )

    $directory = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Path))
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        $null = New-Item -ItemType Directory -Path $directory -Force
    }
    $stream = [IO.File]::Open(
        $Path,
        [IO.FileMode]::OpenOrCreate,
        [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::ReadWrite
    )
    try {
        if ($stream.Length -eq 0) {
            $stream.WriteByte(48)
            $stream.Flush($true)
        }
        $stream.Lock(0, 1)
        return $stream
    }
    catch {
        $stream.Dispose()
        throw "Unable to acquire ${Description}: $($_.Exception.Message)"
    }
}

function Exit-InstallerByteRangeLock {
    param([AllowNull()][object] $Stream)

    if ($null -eq $Stream) { return }
    try { $Stream.Unlock(0, 1) }
    finally { $Stream.Dispose() }
}

function Test-ByteRangeLockOwned {
    param([Parameter(Mandatory)][string] $Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $stream = [IO.File]::Open(
        $Path,
        [IO.FileMode]::Open,
        [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::ReadWrite
    )
    try {
        try { $stream.Lock(0, 1) }
        catch [IO.IOException] { return $true }
        $stream.Unlock(0, 1)
        return $false
    }
    finally { $stream.Dispose() }
}

function Read-ManagerUiReadyMarker {
    param([Parameter(Mandatory)][string] $Path)

    try {
        $raw = [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8)
        $document = [Text.Json.JsonDocument]::Parse($raw)
    }
    catch { throw 'The manager UI ready marker is not valid JSON.' }
    try {
        $root = $document.RootElement
        if ($root.ValueKind -ne [Text.Json.JsonValueKind]::Object) {
            throw 'The manager UI ready marker root is not an object.'
        }
        $properties = @($root.EnumerateObject())
        $names = @($properties | ForEach-Object { $_.Name })
        $uniqueNames = @($names | Sort-Object -Unique)
        $expectedNames = @('managerSha256', 'pid', 'readyAtUtc', 'schemaVersion', 'trayReady')
        if ($names.Count -ne $uniqueNames.Count -or
            ($uniqueNames -join '|') -cne ($expectedNames -join '|')) {
            throw 'The manager UI ready marker has missing, duplicate, or unknown properties.'
        }
        $schemaElement = $root.GetProperty('schemaVersion')
        $pidElement = $root.GetProperty('pid')
        $readyElement = $root.GetProperty('readyAtUtc')
        $shaElement = $root.GetProperty('managerSha256')
        $trayElement = $root.GetProperty('trayReady')
        if ($schemaElement.ValueKind -ne [Text.Json.JsonValueKind]::Number -or
            $pidElement.ValueKind -ne [Text.Json.JsonValueKind]::Number -or
            $readyElement.ValueKind -ne [Text.Json.JsonValueKind]::String -or
            $shaElement.ValueKind -ne [Text.Json.JsonValueKind]::String -or
            $trayElement.ValueKind -notin @([Text.Json.JsonValueKind]::True, [Text.Json.JsonValueKind]::False)) {
            throw 'The manager UI ready marker contains an invalid JSON type.'
        }
        try {
            $schemaVersion = $schemaElement.GetInt32()
            $pidValue = $pidElement.GetInt64()
        }
        catch { throw 'The manager UI ready marker contains an invalid integer.' }
        $readyText = $readyElement.GetString()
        $managerSha256 = $shaElement.GetString()
        $trayReady = $trayElement.GetBoolean()
        if ($schemaVersion -ne 1 -or $pidValue -le 0 -or -not $trayReady -or
            $managerSha256 -notmatch '^[0-9a-f]{64}$' -or
            $readyText -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$') {
            throw 'The manager UI ready marker schema values are invalid.'
        }
        return [pscustomobject]@{
            Pid = $pidValue
            ReadyAtUtc = $readyText
            ManagerSha256 = $managerSha256
            TrayReady = $trayReady
        }
    }
    finally { $document.Dispose() }
}

function Assert-ReplacementManagerUi {
    param(
        [Parameter(Mandatory)][string] $InstallRoot,
        [Parameter(Mandatory)][string] $ExpectedManager,
        [Parameter(Mandatory)][string] $ExpectedPythonw,
        [Parameter(Mandatory)][string] $ExpectedManagerSha256,
        [Parameter(Mandatory)][object] $PrimaryProcess,
        [Parameter(Mandatory)][AllowEmptyCollection()][int[]] $PriorProcessIds
    )

    $expectedManagerPath = [IO.Path]::GetFullPath($ExpectedManager)
    $expectedPythonPath = [IO.Path]::GetFullPath($ExpectedPythonw)
    $primaryUi = $null
    $stateDirectory = Join-Path $InstallRoot 'state'
    $readyPath = Join-Path $stateDirectory 'manager-ui-ready.json'
    $uiLockPath = Join-Path $stateDirectory 'manager-ui.lock'
    $showRequestPath = Join-Path $stateDirectory 'manager-ui-show-request.json'
    $lastReadyReason = 'process-not-ready'
    for ($attempt = 0; $attempt -lt 200; $attempt++) {
        if ($PrimaryProcess.HasExited) { throw 'The replacement manager UI exited before becoming ready.' }
        $all = @(Get-InstalledManagerUiProcesses -InstallRoot $InstallRoot)
        $priorStillRunning = @($all | Where-Object { $_.ProcessId -in $PriorProcessIds })
        if ($priorStillRunning.Count -ne 0) { throw 'A prior manager UI remained running during replacement validation.' }
        $matching = @($all | Where-Object {
            $_.ProcessId -eq $PrimaryProcess.Id -and
            [StringComparer]::OrdinalIgnoreCase.Equals($_.Manager, $expectedManagerPath) -and
            [StringComparer]::OrdinalIgnoreCase.Equals($_.Pythonw, $expectedPythonPath)
        })
        if ($all.Count -ne 1 -or $matching.Count -ne 1) {
            $lastReadyReason = 'process-identity-not-ready'
            Start-Sleep -Milliseconds 100
            continue
        }
        if (-not (Test-ByteRangeLockOwned -Path $uiLockPath)) {
            $lastReadyReason = 'ui-lock-not-owned'
            Start-Sleep -Milliseconds 100
            continue
        }
        if (-not (Test-Path -LiteralPath $readyPath -PathType Leaf)) {
            $lastReadyReason = 'ready-marker-missing'
            Start-Sleep -Milliseconds 100
            continue
        }
        try { $ready = Read-ManagerUiReadyMarker -Path $readyPath }
        catch {
            $lastReadyReason = 'ready-marker-invalid'
            Start-Sleep -Milliseconds 100
            continue
        }
        if ([long] $ready.Pid -ne [long] $PrimaryProcess.Id -or
            [string] $ready.ManagerSha256 -cne $ExpectedManagerSha256) {
            $lastReadyReason = 'ready-identity-mismatch'
            Start-Sleep -Milliseconds 100
            continue
        }
        $readyAt = [DateTimeOffset]::MinValue
        $styles = [Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::AdjustToUniversal
        if (-not [DateTimeOffset]::TryParseExact(
            [string] $ready.ReadyAtUtc,
            'yyyy-MM-ddTHH:mm:ssZ',
            [Globalization.CultureInfo]::InvariantCulture,
            $styles,
            [ref] $readyAt
        )) {
            $lastReadyReason = 'ready-timestamp-invalid'
            Start-Sleep -Milliseconds 100
            continue
        }
        $readyAge = ([DateTimeOffset]::UtcNow - $readyAt).TotalSeconds
        if ($readyAge -lt -5 -or $readyAge -gt 30) {
            $lastReadyReason = 'ready-marker-stale'
            Start-Sleep -Milliseconds 100
            continue
        }
        $primaryUi = $matching[0]
        break
    }
    if ($null -eq $primaryUi) {
        throw "Exactly one verified replacement manager UI, owned lock, and fresh tray-ready marker did not appear ($lastReadyReason)."
    }

    $secondary = Start-Process `
        -FilePath $ExpectedPythonw `
        -ArgumentList @((Quote-WindowsArgument $ExpectedManager), 'ui') `
        -WorkingDirectory $InstallRoot `
        -WindowStyle Hidden `
        -PassThru
    try {
        Wait-Process -Id $secondary.Id -Timeout 10 -ErrorAction SilentlyContinue
        $secondary.Refresh()
        if (-not $secondary.HasExited) { throw 'A second manager UI launch did not exit after requesting show.' }
        if ($secondary.ExitCode -ne 0) { throw "The second manager UI launch exited with code $($secondary.ExitCode)." }
    }
    finally {
        if (-not $secondary.HasExited) { Stop-Process -Id $secondary.Id -Force -ErrorAction SilentlyContinue }
        $secondary.Dispose()
    }
    $showRequestConsumed = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $PrimaryProcess.Refresh()
        if ($PrimaryProcess.HasExited) {
            throw 'The original replacement manager UI exited before consuming the show request.'
        }
        if (-not (Test-Path -LiteralPath $showRequestPath -PathType Leaf)) {
            $showRequestConsumed = $true
            break
        }
        Start-Sleep -Milliseconds 100
    }
    if (-not $showRequestConsumed) {
        throw 'The replacement manager UI did not consume the second-launch show request within three seconds.'
    }
    $PrimaryProcess.Refresh()
    if ($PrimaryProcess.HasExited) { throw 'The original replacement manager UI exited during the single-instance handshake.' }
    $after = @(Get-InstalledManagerUiProcesses -InstallRoot $InstallRoot)
    if ($after.Count -ne 1 -or $after[0].ProcessId -ne $PrimaryProcess.Id -or
        -not [StringComparer]::OrdinalIgnoreCase.Equals($after[0].Manager, $expectedManagerPath)) {
        throw 'The single-instance handshake did not preserve exactly one replacement manager UI.'
    }
}

Assert-WindowsPowerShell7

if ($ManagerChildOnly -and [string]::IsNullOrWhiteSpace($CodexPath)) {
    throw '-ManagerChildOnly requires the controller to supply an explicit -CodexPath.'
}

$installRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'CodexResetCredit'))
$runnersDirectory = Join-Path $installRoot 'runners'
$installersDirectory = Join-Path $installRoot 'installers'
$manifestsDirectory = Join-Path $installRoot 'manifests'
$logsDirectory = Join-Path $installRoot 'logs'
$stateDirectory = Join-Path $installRoot 'state'
$policyPath = Join-Path $stateDirectory 'policy.json'

if ([string]::IsNullOrWhiteSpace($SourceRunner)) {
    $developmentRunner = Join-Path $PSScriptRoot 'codex_reset_guard.py'
    $environmentRunner = [Environment]::GetEnvironmentVariable($RuntimeGuardEnvironmentVariable, 'Process')
    if (Test-Path -LiteralPath $developmentRunner -PathType Leaf) {
        $SourceRunner = $developmentRunner
    }
    elseif (-not [string]::IsNullOrWhiteSpace($environmentRunner)) {
        $SourceRunner = $environmentRunner
    }
    elseif (Test-Path -LiteralPath $policyPath -PathType Leaf) {
        $installedPolicy = Get-Content -Raw -LiteralPath $policyPath | ConvertFrom-Json -Depth 100
        $runtimeGuardProperty = $installedPolicy.PSObject.Properties['runtimeGuard']
        if ($null -ne $runtimeGuardProperty) { $SourceRunner = [string] $runtimeGuardProperty.Value }
    }
}
if ([string]::IsNullOrWhiteSpace($SourceRunner)) {
    throw 'Unable to resolve the immutable guard runner. The controller must pass CODEX_RESET_MANAGER_RUNTIME_GUARD.'
}
$sourceRunnerPath = Resolve-CanonicalFile -Path $SourceRunner -Description 'Guard runner'
if ([IO.Path]::GetExtension($sourceRunnerPath) -ne '.py') {
    throw 'The guard runner must be a .py file.'
}
$sourceRunnerHash = (Get-FileHash -LiteralPath $sourceRunnerPath -Algorithm SHA256).Hash.ToLowerInvariant()

$python = Get-Python313 -RequestedPath $PythonPath
$codex = Get-NpmNativeCodex -RequestedPath $CodexPath
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$runtimeRunner = Join-Path $runnersDirectory "codex_reset_guard-$sourceRunnerHash.py"
$manifestPath = Join-Path $manifestsDirectory $ManifestName

$sourceManagerPath = $null
$sourceManagerHash = $null
$runtimeManager = $null
$sourceInstallerPath = $null
$sourceInstallerHash = $null
$runtimeInstaller = $null
if (-not $ManagerChildOnly) {
    if ([string]::IsNullOrWhiteSpace($SourceManager)) { $SourceManager = Join-Path $PSScriptRoot 'codex_reset_manager.py' }
    if ([string]::IsNullOrWhiteSpace($SourceInstaller)) { $SourceInstaller = $PSCommandPath }
    $sourceManagerPath = Resolve-CanonicalFile -Path $SourceManager -Description 'Manager runner'
    $sourceInstallerPath = Resolve-CanonicalFile -Path $SourceInstaller -Description 'Installer source'
    if ([IO.Path]::GetExtension($sourceManagerPath) -ne '.py') { throw 'The manager runner must be a .py file.' }
    if ([IO.Path]::GetExtension($sourceInstallerPath) -ne '.ps1') { throw 'The installer source must be a .ps1 file.' }
    $sourceManagerHash = (Get-FileHash -LiteralPath $sourceManagerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $sourceInstallerHash = (Get-FileHash -LiteralPath $sourceInstallerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $runtimeManager = Join-Path $runnersDirectory "codex_reset_manager-$sourceManagerHash.py"
    $runtimeInstaller = Join-Path $installersDirectory "install-$sourceInstallerHash.ps1"
}

Write-Host "Python: $($python.Path) ($($python.Version))"
Write-Host "Codex:  $($codex.Path) ($($codex.Version), sha256 $($codex.Sha256.Substring(0, 12))...)"
Write-Host "Runtime: $installRoot"
if ($ManagerChildOnly) { Write-Host "Manifest: $manifestPath" }

if ($WhatIfPreference) {
    $modeDescription = if ($ManagerChildOnly) { 'Create one controller-owned guarded reset-credit job' } else { 'Install manager runtime and disabled policy bootstrap' }
    $null = $PSCmdlet.ShouldProcess($installRoot, $modeDescription)
    if ($ConfigureWindowsTime) {
        $null = $PSCmdlet.ShouldProcess('Windows Time service', 'Repair only if verification fails')
    }
    if ($ManagerChildOnly) {
        $null = $PSCmdlet.ShouldProcess("$TaskFolder<Task derived from enrolled credit hash>", 'Register and verify one-shot task, then arm manifest')
    }
    else {
        $null = $PSCmdlet.ShouldProcess("$TaskFolder$ManagerTaskName", "Register and verify logon plus $ManagerSyncIntervalMinutes-minute controller task")
        $null = $PSCmdlet.ShouldProcess('Current-user Start Menu', 'Create and verify the English manager shortcut, then remove the exact legacy shortcut')
    }
    Write-Host 'WhatIf complete. No Python command, file write, time change, task registration, shortcut, or GUI launch was performed.'
    return
}

if ($ManagerChildOnly) {
    Assert-ManagerChildAdmission `
        -PolicyPath $policyPath `
        -ManifestsDirectory $manifestsDirectory `
        -InstallRoot $installRoot `
        -ExecutingInstaller $PSCommandPath `
        -GuardPath $sourceRunnerPath `
        -GuardSha256 $sourceRunnerHash `
        -ObservedCodex $codex
}

$timeHealth = $null
$timeFailure = $null
try {
    $timeHealth = Get-WindowsTimeHealth
    if ($timeHealth.Source -notmatch '(?i)^time\.windows\.com(?:,0x9)?\s*$') {
        throw "Windows Time source is '$($timeHealth.Source)', not time.windows.com,0x9."
    }
}
catch {
    $timeFailure = $_.Exception.Message
}

if ($null -ne $timeFailure) {
    $repairTime = [bool] $ConfigureWindowsTime
    if (-not $repairTime -and $InteractiveSetup) {
        Write-Warning "Windows Time must be repaired before automatic use: $timeFailure"
        $answer = Read-Host 'Configure time.windows.com now? Windows will show one UAC prompt. [Y/N]'
        $repairTime = $answer -match '^(?i)y(?:es)?$'
    }
    if (-not $repairTime) {
        throw "Windows Time verification failed: $timeFailure Rerun with -ConfigureWindowsTime, or double-click setup.cmd and approve repair."
    }
    if ($PSCmdlet.ShouldProcess('Windows Time service', 'Configure time.windows.com,0x9 and force resynchronization with elevation')) {
        Invoke-ElevatedWindowsTimeConfiguration
    }
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            $timeHealth = Get-WindowsTimeHealth
            if ($timeHealth.Source -notmatch '(?i)^time\.windows\.com(?:,0x9)?\s*$') {
                throw "Windows Time source remains '$($timeHealth.Source)'."
            }
            $timeFailure = $null
            break
        }
        catch {
            $timeFailure = $_.Exception.Message
            if ($attempt -lt 5) { Start-Sleep -Seconds 2 }
        }
    }
    if ($null -ne $timeFailure) { throw "Windows Time repair could not be verified: $timeFailure" }
}
Write-Host "Windows Time source verified: $($timeHealth.Source)"

if ($PSCmdlet.ShouldProcess($installRoot, 'Create private runtime directories')) {
    $null = New-Item -ItemType Directory -Path $installRoot -Force
    $null = New-Item -ItemType Directory -Path $runnersDirectory -Force
    $null = New-Item -ItemType Directory -Path $installersDirectory -Force
    $null = New-Item -ItemType Directory -Path $manifestsDirectory -Force
    $null = New-Item -ItemType Directory -Path $logsDirectory -Force
    $null = New-Item -ItemType Directory -Path $stateDirectory -Force
    foreach ($privateDirectory in @(
        $installRoot,
        $runnersDirectory,
        $installersDirectory,
        $manifestsDirectory,
        $logsDirectory,
        $stateDirectory
    )) {
        Set-PrivateInstallAcl -InstallRoot $privateDirectory -UserName $currentUser
    }
}

if (Test-Path -LiteralPath $runtimeRunner -PathType Leaf) {
    $installedHash = (Get-FileHash -LiteralPath $runtimeRunner -Algorithm SHA256).Hash
    if ($sourceRunnerHash -ne $installedHash.ToLowerInvariant()) {
        throw 'The immutable runner path exists with unexpected content; refusing replacement.'
    }
}
elseif ($PSCmdlet.ShouldProcess($runtimeRunner, 'Copy immutable guarded Python runtime')) {
    Copy-Item -LiteralPath $sourceRunnerPath -Destination $runtimeRunner
}
if ($sourceRunnerHash -ne
    (Get-FileHash -LiteralPath $runtimeRunner -Algorithm SHA256).Hash.ToLowerInvariant()) {
    throw 'Installed runner hash does not match the source runner.'
}

if (-not $ManagerChildOnly) {
    foreach ($immutableFile in @(
        [pscustomobject]@{ Source = $sourceManagerPath; Destination = $runtimeManager; Hash = $sourceManagerHash; Description = 'manager' },
        [pscustomobject]@{ Source = $sourceInstallerPath; Destination = $runtimeInstaller; Hash = $sourceInstallerHash; Description = 'installer' }
    )) {
        if (Test-Path -LiteralPath $immutableFile.Destination -PathType Leaf) {
            $existingHash = (Get-FileHash -LiteralPath $immutableFile.Destination -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($existingHash -ne $immutableFile.Hash) {
                throw "Immutable $($immutableFile.Description) path exists with unexpected content."
            }
        }
        elseif ($PSCmdlet.ShouldProcess($immutableFile.Destination, "Copy immutable $($immutableFile.Description) runtime")) {
            Copy-Item -LiteralPath $immutableFile.Source -Destination $immutableFile.Destination
        }
        if ((Get-FileHash -LiteralPath $immutableFile.Destination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $immutableFile.Hash) {
            throw "Installed $($immutableFile.Description) hash differs from its source."
        }
    }
}

if (-not $ManagerChildOnly) {
    $nonterminal = @(Get-ManifestInventory -Directory $manifestsDirectory | Where-Object { -not $_.Terminal })
    if ($nonterminal.Count -gt 1) {
        throw 'More than one nonterminal manifest exists. Resolve the conflict before installing ManagerSync.'
    }
    $activeOneShotSnapshot = Get-ActiveOneShotSnapshot `
        -NonterminalInventory $nonterminal `
        -ManifestDirectory $manifestsDirectory

    $programsDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::Programs)
    if ([string]::IsNullOrWhiteSpace($programsDirectory)) { throw 'Current-user Start Menu directory is unavailable.' }
    $shortcutPath = Join-Path $programsDirectory $ManagerShortcutName
    $legacyShortcutPath = Join-Path $programsDirectory $LegacyManagerShortcutName

    # Snapshot every mutable installation surface before stopping a UI or
    # asking the manager to bootstrap policy. Immutable content-addressed
    # runtime copies are inert and intentionally need no rollback.
    $policySnapshot = Get-FileByteSnapshot -Path $policyPath
    $preinstallEnabled = $null
    if ([bool] $policySnapshot.Exists) {
        try {
            $preinstallPolicyText = [Text.UTF8Encoding]::new($false, $true).GetString([byte[]] $policySnapshot.Bytes)
            $preinstallPolicy = $preinstallPolicyText | ConvertFrom-Json -Depth 100
        }
        catch { throw "The existing manager policy cannot be snapshotted safely: $($_.Exception.Message)" }
        if ($preinstallPolicy.enabled -isnot [bool]) { throw 'The existing manager policy has no valid enabled state.' }
        $preinstallEnabled = [bool] $preinstallPolicy.enabled
    }
    $managerTaskSnapshot = Get-ManagerTaskSnapshot
    $shortcutSnapshot = Get-FileByteSnapshot -Path $shortcutPath
    $legacyShortcutSnapshot = Get-FileByteSnapshot -Path $legacyShortcutPath
    $priorManagerUis = @(Get-InstalledManagerUiProcesses -InstallRoot $installRoot)
    if ($priorManagerUis.Count -gt 1) {
        throw 'More than one verified manager UI is running; refusing an ambiguous upgrade.'
    }
    [int[]] $priorManagerUiProcessIds = @($priorManagerUis | ForEach-Object { [int] $_.ProcessId })
    $replacementUiProcess = $null
    $controllerLockStream = $null
    $dispatchLockStream = $null
    $priorUiWasStopped = $false
    $managerTaskDefinitionReplaced = $false
    $policyMutationAttempted = $false
    $englishShortcutMutationAttempted = $false
    $legacyShortcutMutationAttempted = $false
    try {
        # Prevent a scheduled controller from starting again while this
        # transaction replaces its runtime and definition.
        Suspend-ManagerSyncForInstall -Snapshot $managerTaskSnapshot

        # Acquire controller.lock before touching the prior UI. This proves no
        # UI or scheduled worker is inside a controller mutation and prevents
        # a new worker from starting while the old UI is being stopped.
        $controllerLockStream = Enter-InstallerByteRangeLock `
            -Path (Join-Path $stateDirectory 'controller.lock') `
            -Description 'controller.lock for UI quiescence'
        Assert-ActiveOneShotUnchanged -Snapshot $activeOneShotSnapshot

        # Stop only verified, content-addressed manager UI processes beneath
        # this user's install root. This prevents a stale UI from restoring
        # old runtime pins after an update and guarantees one visible manager.
        foreach ($ui in $priorManagerUis) {
            Stop-Process -Id $ui.ProcessId -Force -ErrorAction Stop
            Wait-Process -Id $ui.ProcessId -Timeout 5 -ErrorAction SilentlyContinue
        }
        foreach ($priorId in $priorManagerUiProcessIds) {
            if ($null -ne (Get-Process -Id $priorId -ErrorAction SilentlyContinue)) {
                throw "Prior manager UI process $priorId did not exit."
            }
        }
        if (@(Get-InstalledManagerUiProcesses -InstallRoot $installRoot).Count -ne 0) {
            throw 'A verified prior manager UI remained after shutdown.'
        }
        $priorUiWasStopped = $priorManagerUis.Count -gt 0
        Assert-ActiveOneShotUnchanged -Snapshot $activeOneShotSnapshot
        Exit-InstallerByteRangeLock -Stream $controllerLockStream
        $controllerLockStream = $null

        Write-Host 'Bootstrapping the manager policy and adopting any existing job...'
        $policyMutationAttempted = $true
        $managerStatus = Invoke-Manager `
            -Python $python.Path `
            -Manager $runtimeManager `
            -RuntimeInstaller $runtimeInstaller `
            -RuntimeGuard $runtimeRunner `
            -Arguments @('status', '--json')
        if ($null -ne $managerStatus) { $managerStatus | Write-Output }
        Assert-ManagerPolicyRuntimePins `
            -PolicyPath $policyPath `
            -ExpectedInstaller $runtimeInstaller `
            -ExpectedGuard $runtimeRunner

        # Match the manager's public lock order. The status bootstrap above is
        # deliberately complete before these locks are retained.
        $controllerLockStream = Enter-InstallerByteRangeLock `
            -Path (Join-Path $stateDirectory 'controller.lock') `
            -Description 'controller.lock'
        Assert-ActiveOneShotUnchanged -Snapshot $activeOneShotSnapshot
        Assert-ActiveOneShotTriggerMargin -Snapshot $activeOneShotSnapshot
        $dispatchLockStream = Enter-InstallerByteRangeLock `
            -Path (Join-Path $stateDirectory 'dispatch.lock') `
            -Description 'dispatch.lock'

        $managerArguments = "$(Quote-WindowsArgument $runtimeManager) sync --scheduled"
        $managerAction = New-ScheduledTaskAction -Execute $python.WindowlessPath -Argument $managerArguments -WorkingDirectory $installRoot
        $logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
        $periodicTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $ManagerSyncIntervalMinutes)
        $managerPrincipal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
        $managerSettings = New-ScheduledTaskSettingsSet `
            -StartWhenAvailable `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
            -RestartCount 0
        $managerDefinition = New-ScheduledTask `
            -Action $managerAction `
            -Trigger @($logonTrigger, $periodicTrigger) `
            -Principal $managerPrincipal `
            -Settings $managerSettings
        if ($PSCmdlet.ShouldProcess("$TaskFolder$ManagerTaskName", 'Register ManagerSync controller task')) {
            Ensure-TaskFolder -Path $TaskFolder
            $managerTaskDefinitionReplaced = $true
            $null = Register-ScheduledTask `
                -TaskName $ManagerTaskName `
                -TaskPath $TaskFolder `
                -InputObject $managerDefinition `
                -Force
        }
        Assert-ManagerTask `
            -ExpectedUser $currentUser `
            -ExpectedPython $python.WindowlessPath `
            -ExpectedArguments $managerArguments `
            -ExpectedWorkingDirectory $installRoot

        $shortcutVerified = $false
        if ($PSCmdlet.ShouldProcess($shortcutPath, 'Create and verify manager shortcut')) {
            $englishShortcutMutationAttempted = $true
            Set-AndAssertManagerShortcut `
                -ShortcutPath $shortcutPath `
                -Pythonw $python.WindowlessPath `
                -Manager $runtimeManager `
                -WorkingDirectory $installRoot
            $shortcutVerified = $true
        }

        if ($PSCmdlet.ShouldProcess('Codex Reset Credit Manager', 'Open the manager window once')) {
            $replacementUiProcess = Start-Process `
                -FilePath $python.WindowlessPath `
                -ArgumentList @((Quote-WindowsArgument $runtimeManager), 'ui') `
                -WorkingDirectory $installRoot `
                -WindowStyle Hidden `
                -PassThru
        }
        if ($null -eq $replacementUiProcess) { throw 'The replacement manager UI was not launched.' }
        Assert-ReplacementManagerUi `
            -InstallRoot $installRoot `
            -ExpectedManager $runtimeManager `
            -ExpectedPythonw $python.WindowlessPath `
            -ExpectedManagerSha256 $sourceManagerHash `
            -PrimaryProcess $replacementUiProcess `
            -PriorProcessIds $priorManagerUiProcessIds
        Assert-ManagerPolicyRuntimePins `
            -PolicyPath $policyPath `
            -ExpectedInstaller $runtimeInstaller `
            -ExpectedGuard $runtimeRunner
        $finalPolicy = Get-Content -Raw -LiteralPath $policyPath | ConvertFrom-Json -Depth 100
        if ($null -ne $preinstallEnabled -and [bool] $finalPolicy.enabled -ne $preinstallEnabled) {
            throw 'Manager installation changed the existing automatic-use enabled state.'
        }
        if ([bool] $finalPolicy.enabled) {
            Write-Host 'Manager installed. Existing automatic operation remains enabled.'
        }
        else {
            Write-Host 'Manager installed. Automatic operation remains paused until the user selects Start Automatic Use.'
        }
        # Remove only the exact shortcut written by the former Korean release,
        # and only after the replacement has been created and read back. Never
        # use a wildcard here: unrelated user shortcuts must be preserved.
        Assert-ActiveOneShotUnchanged -Snapshot $activeOneShotSnapshot
        if ($shortcutVerified -and
            (Test-Path -LiteralPath $legacyShortcutPath -PathType Leaf) -and
            $PSCmdlet.ShouldProcess($legacyShortcutPath, 'Remove exact legacy manager shortcut after replacement verification')) {
            $legacyShortcutMutationAttempted = $true
            Remove-Item -LiteralPath $legacyShortcutPath -Force
        }
        # The installer never mutates the active one-shot. Commit only if its
        # exact manifest bytes and exported task definition are still intact.
        Assert-ActiveOneShotUnchanged -Snapshot $activeOneShotSnapshot
        Exit-InstallerByteRangeLock -Stream $dispatchLockStream
        $dispatchLockStream = $null
        Exit-InstallerByteRangeLock -Stream $controllerLockStream
        $controllerLockStream = $null
        Write-Host "ManagerSync: $TaskFolder$ManagerTaskName"
        Write-Host "Shortcut: $shortcutPath"
        return
    }
    catch {
        $installationError = $_
        $rollbackErrors = [Collections.Generic.List[string]]::new()
        $uiCleanupComplete = $false

        # Stop only replacement UIs, or a prior UI that was already safely
        # quiesced under controller.lock. A busy controller can fail before the
        # original UI is touched; that original process must remain alive.
        try {
            if ($null -ne $replacementUiProcess -and -not $replacementUiProcess.HasExited) {
                Stop-Process -Id $replacementUiProcess.Id -Force -ErrorAction Stop
            }
            foreach ($runningUi in @(Get-InstalledManagerUiProcesses -InstallRoot $installRoot)) {
                if (-not $priorUiWasStopped -and $runningUi.ProcessId -in $priorManagerUiProcessIds) {
                    continue
                }
                Stop-Process -Id $runningUi.ProcessId -Force -ErrorAction Stop
                Wait-Process -Id $runningUi.ProcessId -Timeout 5 -ErrorAction SilentlyContinue
            }
            $remainingUis = @(Get-InstalledManagerUiProcesses -InstallRoot $installRoot)
            $unexpectedRemaining = @($remainingUis | Where-Object {
                $priorUiWasStopped -or $_.ProcessId -notin $priorManagerUiProcessIds
            })
            if ($unexpectedRemaining.Count -ne 0) {
                throw 'A verified replacement manager UI remained running after cleanup.'
            }
            $uiCleanupComplete = $true
        }
        catch { $rollbackErrors.Add("UI cleanup: $($_.Exception.Message)") }

        if ($priorUiWasStopped -or $null -ne $replacementUiProcess) {
            Remove-Item `
                -LiteralPath (Join-Path $stateDirectory 'manager-ui-ready.json') `
                -Force `
                -ErrorAction SilentlyContinue
        }

        $rollbackControllerReady = $true
        if ($policyMutationAttempted -and $null -eq $controllerLockStream) {
            try {
                $controllerLockStream = Enter-InstallerByteRangeLock `
                    -Path (Join-Path $stateDirectory 'controller.lock') `
                    -Description 'controller.lock for policy rollback'
            }
            catch {
                $rollbackControllerReady = $false
                $rollbackErrors.Add("controller.lock reacquire: $($_.Exception.Message)")
            }
        }

        # This transaction never writes the active one-shot. If another
        # process or its natural trigger changed it, report that fact and do
        # not overwrite the newer state with a stale snapshot.
        $laterMutationAttempted = (
            $policyMutationAttempted -or
            $managerTaskDefinitionReplaced -or
            $englishShortcutMutationAttempted -or
            $legacyShortcutMutationAttempted -or
            $null -ne $replacementUiProcess
        )
        if ($laterMutationAttempted -and $rollbackControllerReady) {
            try { Assert-ActiveOneShotUnchanged -Snapshot $activeOneShotSnapshot }
            catch { $rollbackErrors.Add("active one-shot changed externally: $($_.Exception.Message)") }
        }

        if ($policyMutationAttempted) {
            if ($rollbackControllerReady) {
                try { Restore-FileByteSnapshot -Path $policyPath -Snapshot $policySnapshot }
                catch { $rollbackErrors.Add("policy: $($_.Exception.Message)") }
            }
            else {
                $rollbackErrors.Add('policy: skipped because controller.lock could not be reacquired')
            }
        }

        try {
            $currentManagerTask = Get-ScheduledTask `
                -TaskName $ManagerTaskName `
                -TaskPath $TaskFolder `
                -ErrorAction SilentlyContinue
            if (-not $managerTaskDefinitionReplaced -and [bool] $managerTaskSnapshot.Exists -and
                $null -ne $currentManagerTask -and $currentManagerTask.State.ToString() -eq 'Running') {
                # Only Disabled was changed before this bounded natural-wait
                # timeout. Re-enable the original definition without replacing
                # or terminating its still-running process.
                Enable-ScheduledTask -TaskName $ManagerTaskName -TaskPath $TaskFolder -ErrorAction Stop | Out-Null
            }
            else {
                Restore-ManagerTaskSnapshot -Snapshot $managerTaskSnapshot
            }
        }
        catch { $rollbackErrors.Add("ManagerSync: $($_.Exception.Message)") }

        if ($englishShortcutMutationAttempted) {
            try { Restore-FileByteSnapshot -Path $shortcutPath -Snapshot $shortcutSnapshot }
            catch { $rollbackErrors.Add("English shortcut: $($_.Exception.Message)") }
        }

        if ($legacyShortcutMutationAttempted) {
            try { Restore-FileByteSnapshot -Path $legacyShortcutPath -Snapshot $legacyShortcutSnapshot }
            catch { $rollbackErrors.Add("legacy shortcut: $($_.Exception.Message)") }
        }

        try {
            Exit-InstallerByteRangeLock -Stream $dispatchLockStream
            $dispatchLockStream = $null
        }
        catch { $rollbackErrors.Add("dispatch.lock release: $($_.Exception.Message)") }
        try {
            Exit-InstallerByteRangeLock -Stream $controllerLockStream
            $controllerLockStream = $null
        }
        catch { $rollbackErrors.Add("controller.lock release: $($_.Exception.Message)") }

        # Restore one prior verified UI only if its original process is gone.
        # A controller-lock failure leaves that original untouched and must not
        # create a duplicate.
        try {
            if ($priorManagerUis.Count -gt 0) {
                $previous = $priorManagerUis[0]
                $originalAlive = @(
                    Get-InstalledManagerUiProcesses -InstallRoot $installRoot |
                        Where-Object { $_.ProcessId -eq $previous.ProcessId }
                ).Count -eq 1
                if (-not $originalAlive) {
                    if (-not $uiCleanupComplete) {
                        throw 'Prior UI was not relaunched because replacement UI cleanup was incomplete.'
                    }
                    if ((Test-Path -LiteralPath $previous.Pythonw -PathType Leaf) -and
                        (Test-Path -LiteralPath $previous.Manager -PathType Leaf)) {
                        $null = Start-Process `
                            -FilePath $previous.Pythonw `
                            -ArgumentList @((Quote-WindowsArgument $previous.Manager), 'ui') `
                            -WorkingDirectory $installRoot `
                            -WindowStyle Hidden
                    }
                    else {
                        throw 'The previously verified manager UI runtime is no longer available.'
                    }
                }
            }
        }
        catch { $rollbackErrors.Add("prior UI: $($_.Exception.Message)") }

        if ($rollbackErrors.Count -gt 0) {
            $rollbackSummary = $rollbackErrors -join '; '
            throw [InvalidOperationException]::new(
                "Manager installation failed and rollback was incomplete: $rollbackSummary",
                $installationError.Exception
            )
        }
        throw $installationError
    }
}

if (Test-Path -LiteralPath $manifestPath) {
    throw "Manifest already exists; choose a new -ManifestName: $manifestPath"
}

$registered = $false
$armed = $false
$armAttempted = $false
$resolvedTaskName = $null
try {
    Write-Host 'Running read-only Codex app-server contract probe...'
    Invoke-Guard -Python $python.Path -Runner $runtimeRunner -NativeCodex $codex.Path -Arguments @('probe')

    if ($PSCmdlet.ShouldProcess($manifestPath, 'Enroll uniquely earliest reset credit into an unarmed manifest')) {
        Invoke-Guard -Python $python.Path -Runner $runtimeRunner -NativeCodex $codex.Path -Arguments @(
            'enroll', '--earliest', '--manifest', $manifestPath
        )
    }
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw 'The guard did not create the requested manifest.'
    }

    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json -Depth 100
    $manifestArmedValue = Get-JsonPathValue -Object $manifest -CandidatePaths @('armed', 'state.armed', 'lifecycle.armed') -Description 'an armed flag'
    if ($manifestArmedValue -isnot [bool]) {
        throw 'Manifest armed flag must be a JSON boolean.'
    }
    $manifestArmed = [bool] $manifestArmedValue
    if ($manifestArmed) {
        throw 'Enrollment unexpectedly produced an armed manifest.'
    }

    $creditHash = [string] (Get-JsonPathValue -Object $manifest -CandidatePaths @(
        'target.creditIdSha256', 'target.credit_id_sha256', 'target.idSha256', 'creditIdSha256', 'credit_id_sha256'
    ) -Description 'the target credit ID SHA-256')
    if ($creditHash -notmatch '^[0-9a-fA-F]{64}$') {
        throw 'Manifest target credit hash is not a SHA-256 value.'
    }
    $jobIdText = [string] (Get-JsonPathValue -Object $manifest -CandidatePaths @(
        'jobId', 'job_id'
    ) -Description 'the manifest job ID')
    $jobId = [Guid]::Empty
    if (-not [Guid]::TryParse($jobIdText, [ref] $jobId)) {
        throw 'Manifest job ID is not a UUID.'
    }
    $jobSuffix = $jobId.ToString('N').Substring(0, 8).ToLowerInvariant()

    $manifestCodexPath = [string] (Get-JsonPathValue -Object $manifest -CandidatePaths @(
        'runtime.codexExe', 'cli.path', 'cli.nativePath', 'cli.native_path', 'binary.path', 'codex.path', 'codexPath', 'codex_path'
    ) -Description 'the pinned Codex executable path')
    if ([IO.Path]::GetFullPath($manifestCodexPath) -ne $codex.Path) {
        throw 'Manifest pinned a Codex executable other than the verified npm-native executable.'
    }
    $manifestCodexVersion = [string] (Get-JsonPathValue -Object $manifest -CandidatePaths @(
        'runtime.codexVersion', 'cli.version', 'binary.version', 'codex.version', 'codexVersion', 'codex_version'
    ) -Description 'the pinned Codex version')
    if ($manifestCodexVersion -notin @($codex.Version, "codex-cli $($codex.Version)")) {
        throw 'Manifest pinned a Codex version other than the verified npm-native executable.'
    }
    $manifestCodexHash = [string] (Get-JsonPathValue -Object $manifest -CandidatePaths @(
        'runtime.codexSha256', 'cli.sha256', 'binary.sha256', 'codex.sha256', 'codexSha256', 'codex_sha256'
    ) -Description 'the pinned Codex SHA-256')
    if ($manifestCodexHash -notmatch '^[0-9a-fA-F]{64}$' -or
        $manifestCodexHash.ToLowerInvariant() -ne $codex.Sha256) {
        throw 'Manifest pinned a Codex hash other than the verified npm-native executable.'
    }

    $triggerAt = ConvertFrom-ManifestUtc -Value (Get-JsonPathValue -Object $manifest -CandidatePaths @(
        'schedule.triggerAtUtc', 'schedule.trigger_at_utc', 'triggerAtUtc', 'trigger_at_utc'
    ) -Description 'the task trigger UTC time') -Description 'Task trigger'
    $cutoffAt = ConvertFrom-ManifestUtc -Value (Get-JsonPathValue -Object $manifest -CandidatePaths @(
        'schedule.cutoffAtUtc', 'schedule.cutoff_at_utc', 'schedule.deadlineAtUtc', 'cutoffAtUtc', 'cutoff_at_utc'
    ) -Description 'the task cutoff UTC time') -Description 'Task cutoff'

    if ($cutoffAt -le $triggerAt) {
        throw 'Manifest cutoff must be later than its trigger.'
    }
    $minimumTrigger = [DateTimeOffset]::UtcNow.AddMinutes($MinimumLeadTimeMinutes)
    if ($triggerAt -lt $minimumTrigger) {
        throw "Trigger is less than $MinimumLeadTimeMinutes minutes away. No other credit will be selected automatically."
    }

    if ([string]::IsNullOrWhiteSpace($TaskName)) {
        $resolvedTaskName = "Consume-$($creditHash.Substring(0, 12).ToLowerInvariant())-$jobSuffix"
    }
    else {
        if ($TaskName -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$') {
            throw 'TaskName may contain only letters, digits, dot, underscore, and hyphen (maximum 64 characters).'
        }
        $resolvedTaskName = $TaskName
    }

    $existingTask = Get-ScheduledTask -TaskName $resolvedTaskName -TaskPath $TaskFolder -ErrorAction SilentlyContinue
    if ($null -ne $existingTask -and -not $ReplaceExistingTask) {
        throw "Scheduled task $TaskFolder$resolvedTaskName already exists. Review it, then use -ReplaceExistingTask explicitly."
    }

    $runnerArgument = Quote-WindowsArgument -Value $runtimeRunner
    $manifestArgument = Quote-WindowsArgument -Value $manifestPath
    $taskArguments = "$runnerArgument run --manifest $manifestArgument --live"
    $action = New-ScheduledTaskAction -Execute $python.WindowlessPath -Argument $taskArguments -WorkingDirectory $installRoot
    $trigger = New-ScheduledTaskTrigger -Once -At $triggerAt.UtcDateTime
    $trigger.StartBoundary = $triggerAt.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", [Globalization.CultureInfo]::InvariantCulture)
    $trigger.EndBoundary = $cutoffAt.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", [Globalization.CultureInfo]::InvariantCulture)
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -WakeToRun `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -DisallowDemandStart `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
        -RestartCount 0
    $definition = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings

    if ($PSCmdlet.ShouldProcess("$TaskFolder$resolvedTaskName", 'Register unarmed one-shot guarded live task')) {
        Ensure-TaskFolder -Path $TaskFolder
        if ($ReplaceExistingTask) {
            $null = Register-ScheduledTask -TaskName $resolvedTaskName -TaskPath $TaskFolder -InputObject $definition -Force
        }
        else {
            $null = Register-ScheduledTask -TaskName $resolvedTaskName -TaskPath $TaskFolder -InputObject $definition
        }
        $registered = $true
    }

    Assert-RegisteredTask `
        -Name $resolvedTaskName `
        -Folder $TaskFolder `
        -ExpectedUser $currentUser `
        -ExpectedPython $python.WindowlessPath `
        -ExpectedArguments $taskArguments `
        -ExpectedWorkingDirectory $installRoot `
        -ExpectedStart $triggerAt `
        -ExpectedEnd $cutoffAt

    if ($PSCmdlet.ShouldProcess($manifestPath, "Arm manifest for verified task $TaskFolder$resolvedTaskName")) {
        $armAttempted = $true
        Invoke-Guard -Python $python.Path -Runner $runtimeRunner -NativeCodex $codex.Path -Arguments @(
            'arm', '--manifest', $manifestPath, '--task-name', "$TaskFolder$resolvedTaskName"
        )
    }

    $armedManifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json -Depth 100
    $armedValue = Get-JsonPathValue -Object $armedManifest -CandidatePaths @('armed', 'state.armed', 'lifecycle.armed') -Description 'an armed flag'
    if ($armedValue -isnot [bool]) {
        throw 'Manifest armed flag must remain a JSON boolean after arming.'
    }
    $armed = [bool] $armedValue
    if (-not $armed) {
        throw 'The guard returned without arming the manifest.'
    }

    Invoke-Guard -Python $python.Path -Runner $runtimeRunner -NativeCodex $codex.Path -Arguments @(
        'status', '--manifest', $manifestPath
    )

    Write-Host "Installed and armed one-shot task: $TaskFolder$resolvedTaskName"
    Write-Host "Trigger (UTC): $($triggerAt.ToString('O'))"
    Write-Host "Cutoff (UTC): $($cutoffAt.ToString('O'))"
    [pscustomobject]@{
        manifestPath = [IO.Path]::GetFullPath($manifestPath)
        taskName = "$TaskFolder$resolvedTaskName"
        jobId = $jobId.ToString()
    } | ConvertTo-Json -Compress | Write-Output
}
catch {
    if ($registered -and -not [string]::IsNullOrWhiteSpace($resolvedTaskName)) {
        try {
            $null = Disable-ScheduledTask -TaskName $resolvedTaskName -TaskPath $TaskFolder -ErrorAction Stop
            Write-Warning "Disabled unverified/unarmed task $TaskFolder$resolvedTaskName."
        }
        catch {
            Write-Warning "Could not disable task $TaskFolder$resolvedTaskName after failure: $($_.Exception.Message)"
        }
    }
    if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
        try {
            Invoke-Guard -Python $python.Path -Runner $runtimeRunner -NativeCodex $codex.Path -Arguments @(
                'disarm', '--manifest', $manifestPath
            )
        }
        catch {
            Write-Warning "Could not disarm the manifest after installation failure: $($_.Exception.Message)"
        }
    }
    throw
}
