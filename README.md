# Codex Usage Limit Reset Manager

Codex Usage Limit Reset Manager is a small community-built Windows tool. It automatically uses one selected OpenAI Codex usage limit reset about five minutes before it expires. This toy project combines a few Python and PowerShell scripts, a small Tkinter UI, Windows Task Scheduler, and the local Codex CLI app-server.

ChatGPT displays these as **Usage limit resets**. The local Codex app-server internally represents them as rate-limit reset credits.

It does not create additional resets, increase plan usage limits, read `auth.json`, or call the backend API directly.

Korean documentation is available in [README.ko.md](README.ko.md).

![Codex Usage Limit Reset Manager window showing automatic use enabled and a scheduled reset](docs/images/manager-ui-example-01.png)

*Actual Codex Usage Limit Reset Manager window.*

## Quick start

Requirements:

- Windows with PowerShell 7 (`pwsh.exe`)
- CPython 3.13 with `python.exe` and `pythonw.exe` in the same directory
- A global npm installation of Codex CLI `0.144.1` or later
- A signed-in Codex CLI account

1. Double-click `setup.cmd`.
2. In the manager window, select **Start Automatic Use**.
3. Check for **Automatic use: On** and a scheduled-use time.

New installations start paused. UAC is requested only when Windows Time needs synchronization with `time.windows.com`; a healthy clock needs no prompt.

## How it works

`ManagerSync` runs at sign-in and every 30 minutes. It checks the account, clock, Codex CLI, and complete list of resets, maintains at most one reservation, and never uses a reset.

One exact-ID, one-shot task starts shortly beforehand and uses the reset at about T−5 minutes. It can use only the selected reset and never falls back. Because synchronization runs every 30 minutes, automatic discovery is guaranteed only when a new reset appears with about **46 minutes** remaining. If the controller discovers it too close to expiry, it does not create a rushed live task.

When the global Codex CLI changes, `ManagerSync` automatically revalidates it read-only: version, signature, required app-server contracts, account, and full reset list. Only validated CLI updates are approved for future reservations.

## Important notes

- **X** hides the manager in the notification area. **Exit UI** closes only the window and tray icon. Neither pauses automatic use; the Start Menu shortcut reopens the manager.
- **Pause Automatic Use** (or `pause`) stops automation, requests cancellation of the active reservation, and prevents the next one.
- Tasks run only while the current user is logged on. `ManagerSync` does not wake the PC; the exact one-shot task keeps `WakeToRun` for a sleeping PC in that session.
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

Run the fake-app-server tests (no real consumption):

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -v
```

</details>
