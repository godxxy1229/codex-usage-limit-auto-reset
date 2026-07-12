# Codex Reset Credit Manager

Codex Reset Credit Manager is a small community-built Windows tool for OpenAI Codex reset credits. It automatically redeems (consumes) one selected credit about five minutes before expiry. This toy project combines a few Python and PowerShell scripts, a small Tkinter UI, Windows Task Scheduler, and the local Codex CLI app-server.

It does not reset or increase Codex quota, create quota-reset tokens, read `auth.json`, or call the backend API directly.

Korean documentation is available in [README.ko.md](README.ko.md).

![Codex Reset Credit Manager window showing automatic use enabled and a scheduled reset credit](docs/images/manager-ui-example.svg)

*Manager UI shown with synthetic example data.*

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

`ManagerSync` runs at sign-in and every 30 minutes. It checks the account, clock, Codex CLI, and complete credit list, maintains at most one reservation, and never consumes a credit.

One exact-ID, one-shot task starts shortly beforehand and handles consumption at about T−5 minutes. It can use only the selected credit and never falls back. Because synchronization runs every 30 minutes, automatic discovery is guaranteed only when a new credit appears with about **46 minutes** remaining. If the controller discovers it too close to expiry, it does not create a rushed live task.

When the global Codex CLI changes, `ManagerSync` automatically revalidates it read-only: version, signature, required app-server contracts, account, and full credit list. Only validated CLI updates are approved for future reservations.

## Important notes

- **X** hides the manager in the notification area. **Exit UI** closes only the window and tray icon. Neither pauses automatic use; the Start Menu shortcut reopens the manager.
- **Pause Automatic Use** (or `pause`) stops automation, requests cancellation of the active reservation, and prevents the next one.
- Tasks run only while the current user is logged on. `ManagerSync` does not wake the PC; the exact one-shot task keeps `WakeToRun` for a sleeping PC in that session.
- Do not enable automatic use for the same Codex account on multiple PCs. Independent installations may compete for the same credit.

## Safety

- Account reads and credit consumption use only the local app-server exposed by Codex CLI.
- Policy, logs, UI, and notifications do not persist or display raw credit IDs, email addresses, tokens, or idempotency keys.
- Incomplete or ambiguous data, account changes, clock problems, incompatible CLIs, and changed contracts fail closed.
- Consumption requires the preselected exact credit. An indeterminate result remains a barrier until that credit expires and disappears; the controller does not guess or move on to a later credit.

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
