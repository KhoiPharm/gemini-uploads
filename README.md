# Gemini GitHub Upload Sync

A Windows utility that silently keeps one or more local Gemini-upload folders synchronized with GitHub repositories.

## Normal behavior

```text
GitHub repository/branch
        ↓
Windows Scheduled Task (silent, every 2 minutes)
        ↓
Disposable local Git mirror
        ↓
Git-tracked files only
        ↓
Gemini upload folder
```

After one-time setup, there is **no terminal window during normal syncing**.

The scheduled task is stored by Windows, so it survives shutdown and restart. When the laptop is turned back on and you sign in, syncing resumes automatically. If the internet, GitHub authentication, or a Google Drive output folder is temporarily unavailable, a later scheduled run retries automatically.

## What is copied

The upload folder contains files tracked by Git at the selected GitHub branch/commit.

It does **not** copy:

- `.git`
- untracked files
- local databases or runtime files that are not committed
- caches created only inside the disposable mirror

The utility never changes your normal development checkout. Its own Git mirror is disposable and may safely be reset or cleaned.

## Requirements

- Windows 11
- Python 3.11+
- Git for Windows
- Git access already available for the source repository (Git Credential Manager, SSH, etc.)

No third-party Python packages are required.

## First repository

1. Clone or download this `gemini-uploads` repository.
2. Double-click **`SetupGeminiRepo.bat`**.
3. Paste the source repository clone URL.
4. Enter the branch Gemini should see.
5. Enter the local/Google Drive folder Gemini will upload from.

Setup performs the first synchronization immediately and registers a Windows Scheduled Task for that repository.

After setup completes, close the window. You do not need to leave anything open.

## Adding another repository

Double-click **`SetupGeminiRepo.bat`** again and enter the second repository, branch, and a different output folder.

Each configured repository gets:

- its own local profile
- its own disposable Git mirror
- its own output folder
- its own silent Windows Scheduled Task

Adding another repository does not replace the first one.

## Background behavior

Each repository is checked every **2 minutes** while you are signed into Windows.

The Scheduled Task runs `pythonw.exe`, and background Git commands are created without a console window. Normal background checks therefore do not flash Command Prompt or PowerShell windows.

The task persists across reboot. A laptop cannot synchronize while powered off, but after Windows starts and you sign in, the scheduled checks continue automatically.

This user-session design is intentional: private GitHub credentials and Google Drive for Desktop normally belong to your signed-in Windows account.

## Manual refresh

You normally do not need this. If you deliberately want to force an immediate refresh of every configured repository, double-click:

`UpdateGeminiRepo.bat`

That manual utility shows a terminal because you explicitly launched it and may want to see errors/results.

## Local configuration and logs

Repository URLs, branches, output paths, state, logs, mirrors, and the installed runtime copy live under your Windows local application-data directory, typically:

```text
%LOCALAPPDATA%\GeminiRepoSync\
```

They are not committed to this public utility repository.

Background logs are kept under:

```text
%LOCALAPPDATA%\GeminiRepoSync\logs\
```

Logs rotate automatically when they become large.

## Privacy and source of truth

GitHub remains authoritative. The output folder is a generated copy for Gemini web.

Local edits made directly inside a Gemini output folder are not preserved. The next changed GitHub commit replaces that folder with a fresh Git-tracked snapshot.

## Large files

Files over 100 MB are reported in the background log because they may exceed Gemini's per-file upload limit.
