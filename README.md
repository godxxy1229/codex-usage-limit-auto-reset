# Codex Usage Limit Reset Manager

Codex Usage Limit Reset Manager is a small open-source Codex reset scheduler for Windows and systemd Linux. It automatically uses one selected OpenAI Codex usage limit reset about five minutes before it expires. This toy project combines a few Python scripts, the local Codex CLI app-server, and the operating system's task scheduler. Windows also has a small Tkinter manager UI.

ChatGPT displays these as **Usage limit resets**. Older posts may call them **Codex reset credits**; the local Codex app-server uses rate-limit reset credit terminology internally.

Search phrases such as **Codex auto redeem reset** and **Codex auto reset reserve** refer here only to scheduling and using an existing reset; this tool does not create additional resets, increase plan usage limits, read `auth.json`, or call the backend API directly.

Korean documentation is available in [README.ko.md](README.ko.md).

![Codex Usage Limit Reset Manager window showing automatic use enabled and a scheduled reset](docs/images/manager-ui-example-01.png)

*Actual Codex Usage Limit Reset Manager window.*

## Quick start — Windows

Requirements:

- Windows with PowerShell 7 (`pwsh.exe`)
- A final-release, GIL-enabled base installation of CPython 3.11 or later, with sibling `python.exe` and `pythonw.exe`
- A global npm installation of Codex CLI `0.144.1` or later
- A signed-in Codex CLI account

CPython 3.10, prereleases, free-threaded builds, PyPy, virtual environments, and WindowsApps aliases are not supported. Future final CPython 3.x releases are accepted only when the same built-in capability checks pass; the installer never downloads Python.

1. Double-click `setup.cmd`.
2. In the manager window, select **Start Automatic Use**.
3. Check for **Automatic use: On** and a scheduled-use time.

New installations start paused. UAC is requested only when Windows Time needs synchronization with `time.windows.com`; a healthy clock needs no prompt.

The installer pins scheduled tasks to the selected Python executable's absolute path. Keep that Python installation in place; removing or moving it stops the manager and one-shot tasks until the tool is reinstalled with a compatible runtime.

## Quick start — Linux

Linux support is intentionally small and headless. It targets native, systemd-based Linux with Ubuntu 24.04 LTS as the reference platform; Rocky Linux 9.5 x86-64 has also been validated. WSL, non-systemd distributions, standalone Codex installations, Linux GUI/tray integration, root services, and user lingering are not supported. The installer refuses a user account that already has systemd lingering enabled so jobs cannot continue after logout.

Requirements:

- A logged-in systemd user session with synchronized system time
- A final-release, GIL-enabled base installation of CPython 3.11 or later
- A global **npm** installation of Codex CLI `0.144.1` or later on x86-64 or ARM64
- A signed-in Codex CLI account

Install as the normal user; do not use `sudo`:

```sh
chmod +x setup-linux.sh
./setup-linux.sh
~/.local/bin/codex-reset-manager status --json
~/.local/bin/codex-reset-manager enable
```

New Linux installations start paused. Use the full wrapper path immediately after installation because `~/.local/bin` may not enter the current shell's `PATH` until the next login. The installer performs a read-only npm signature/provenance check, stores content-addressed runtimes under `~/.local/share/codex-usage-limit-auto-reset`, installs the command in `~/.local/bin`, and registers only `systemd --user` units. It pins the resolved `CODEX_HOME`, the verified npm launcher, and a deterministic runtime `PATH` into both the unit and wrapper; a reinstall keeps those pins unless `--codex-home /absolute/path` or `--npm-path /absolute/path/to/npm` is supplied. Rerun `setup-linux.sh` to update the installed runtimes. Keep the selected Python and Node.js/npm installations in place.

## How it works

`ManagerSync` runs at Windows sign-in or about one minute after the Linux user manager starts, then every 30 minutes. It checks the account, clock, Codex CLI, and complete list of resets, maintains at most one reservation, and never uses a reset.

One exact-ID, one-shot task starts shortly beforehand and uses the reset at about T−5 minutes. It can use only the selected reset and never falls back. Because synchronization runs every 30 minutes, automatic discovery is guaranteed only when a new reset appears with about **46 minutes** remaining. If the controller discovers it too close to expiry, it does not create a rushed live task.

When the global Codex CLI changes, `ManagerSync` automatically revalidates it read-only: version, platform trust, required app-server contracts, account, and full reset list. Windows verifies Authenticode; Linux verifies npm registry signatures, GitHub provenance, and the native executable hash. Only validated CLI updates are approved for future reservations.

## Important notes

- **X** hides the manager in the notification area. **Exit UI** closes only the window and tray icon. Neither pauses automatic use; the Start Menu shortcut reopens the manager.
- **Pause Automatic Use** (or `pause`) stops automation, requests cancellation of the active reservation, and prevents the next one.
- Tasks run only while the current user is logged on. Windows one-shot tasks retain `WakeToRun`; Linux timers never wake the machine. A persistent Linux timer may catch up after resume only while the user manager is available, and the guard still refuses work after its cutoff.
- Linux has no manager window or tray icon. Use `codex-reset-manager status --json`, `enable`, `pause`, `sync --scheduled`, and `doctor`.
- Do not enable automatic use for the same Codex account on multiple PCs. Independent installations may compete for the same reset.

## Safety

- Account reads and reset use go only through the local app-server exposed by Codex CLI.
- Policy, logs, UI, and notifications do not persist or display raw internal credit IDs, email addresses, tokens, or idempotency keys.
- Incomplete or ambiguous data, account changes, clock problems, incompatible CLIs, and changed contracts fail closed.
- Each reset is pinned to its preselected exact internal ID. An indeterminate result remains a barrier until that reset expires and disappears; the controller does not guess or move on to a later reset.

<details>
<summary>Advanced / development</summary>

Preview or install from PowerShell:

```powershell
# Preview only; makes no changes
pwsh -NoProfile -File .\install.ps1 -WhatIf -Confirm:$false

# Install or update
pwsh -NoProfile -File .\install.ps1 -Confirm:$false

# Allow UAC-assisted clock repair when required
pwsh -NoProfile -File .\install.ps1 -ConfigureWindowsTime -Confirm:$false
```

Manager commands:

```powershell
python .\codex_reset_manager.py ui
python .\codex_reset_manager.py enable
python .\codex_reset_manager.py pause
python .\codex_reset_manager.py sync --scheduled
python .\codex_reset_manager.py status --json
python .\codex_reset_manager.py doctor
```

The installed Linux wrapper exposes the same headless commands:

```sh
codex-reset-manager enable
codex-reset-manager pause
codex-reset-manager sync --scheduled
codex-reset-manager status --json
codex-reset-manager doctor
```

Run the fake-app-server tests (no real consumption):

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -v
```

</details>
