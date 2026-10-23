# Gemini GitHub Upload Sync

A Windows utility that silently keeps one or more local repository mirrors synchronized with GitHub and also creates Gemini-friendly plain-text code bundles.

## Normal behavior

```text
GitHub repository/branch
        ↓
Windows Scheduled Task (silent, every 2 minutes)
        ↓
Disposable local Git mirror
        ↓
        ├── Exact Git-tracked mirror
        │      C:\GeminiUploads\PharmacyRepo
        │
        └── Gemini-friendly text bundles
               C:\GeminiUploads\PharmacyRepo-Gemini
```

After one-time setup, there is **no terminal window during normal syncing**.

The scheduled task is stored by Windows, so it survives shutdown and restart. When the laptop is turned back on and you sign in, syncing resumes automatically. If the internet, GitHub authentication, or an output folder is temporarily unavailable, a later scheduled run retries automatically.

## Which folder should be uploaded to Gemini?

Upload the folder ending in:

```text
-Gemini
```

For example:

```text
C:\GeminiUploads\PharmacyRepo-Gemini
```

That folder contains only ordinary `.txt` files. Gemini therefore does not need to parse hundreds or thousands of mixed repository file types.

The sibling folder without `-Gemini` remains an exact Git-tracked copy and is useful for inspection or other tooling.

## Gemini bundle format

The generated Gemini folder contains files similar to:

```text
00_REPOSITORY_INDEX.txt
01_SOURCE_001.txt
01_SOURCE_002.txt
02_TESTS_001.txt
03_DOCS_001.txt
04_CONFIG_MIGRATIONS_TOOLS_001.txt
```

Large categories are split into additional numbered chunks automatically.

Each bundle preserves the original repository path:

```text
================================================================================
FILE: src/pharmacy_app/application/patients/patient_service.py
================================================================================

<exact original source text>
```

No summaries, rewrites, AI-generated handoffs, or transformed source code are created.

`00_REPOSITORY_INDEX.txt` records:

- Git commit SHA
- every included repository file
- which bundle contains each file
- every skipped file and the reason it was skipped

## What is included in the Gemini bundles?

Git-tracked plain-text files are bundled.

The default categories are:

- source
- tests
- documentation
- configuration, migrations, scripts and tools

The exact file extension is not the deciding factor. If a Git-tracked file is valid text, it can be included.

## What is skipped from the Gemini bundles?

By default:

- top-level `logs/`
- binary files
- non-text encodings that cannot be read safely
- symbolic links

The **exact repository mirror still contains those Git-tracked files**. The filtering only affects the `-Gemini` sibling folder.

The default `logs/` exclusion exists because large tracked runtime logs can overwhelm or destabilize Gemini's code-folder importer while adding little value to code review.

## Exact mirror behavior

The exact mirror contains files tracked by Git at the selected GitHub branch/commit.

It does **not** copy:

- `.git`
- untracked files
- caches or runtime files that are not committed

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
4. Enter the branch.
5. Enter the exact mirror destination, for example:

   ```text
   C:\GeminiUploads\PharmacyRepo
   ```

Setup creates both:

```text
C:\GeminiUploads\PharmacyRepo
C:\GeminiUploads\PharmacyRepo-Gemini
```

It then performs the first synchronization and registers a Windows Scheduled Task.

After setup completes, close the window. You do not need to leave anything open.

## Adding another repository

Double-click **`SetupGeminiRepo.bat`** again and enter the second repository, branch, and a different exact-mirror output folder.

Each configured repository gets its own:

- local profile
- disposable Git mirror
- exact output folder
- Gemini bundle folder
- state and lock
- silent Windows Scheduled Task
- background log

Adding another repository does not replace the first one.

## Updating this utility

After downloading a newer copy of `gemini-uploads`, double-click **`SetupGeminiRepo.bat`** once and enter the same repository URL, branch, and destination as an existing profile.

Setup refreshes the installed background runtime and scheduled task. Existing profiles and destinations remain configured.

Older profiles that do not contain an explicit Gemini bundle path automatically use:

```text
<exact-output-folder>-Gemini
```

## Background behavior

Each repository is checked every **2 minutes** while you are signed into Windows.

The Scheduled Task runs `pythonw.exe`, and background Git commands are created without a console window. Normal checks therefore do not flash Command Prompt or PowerShell windows.

The task persists across reboot. A laptop cannot synchronize while powered off, but after Windows starts and you sign in, scheduled checks continue automatically.

## Manual refresh

You normally do not need this. To immediately refresh every configured repository, double-click:

```text
UpdateGeminiRepo.bat
```

That manually invoked utility shows a terminal so results and errors are visible.

## Local configuration and logs

Repository URLs, branches, output paths, state, logs, mirrors, and the installed runtime copy live under:

```text
%LOCALAPPDATA%\GeminiRepoSync\
```

They are not committed to this public utility repository.

Background logs are stored under:

```text
%LOCALAPPDATA%\GeminiRepoSync\logs\
```

Logs rotate automatically when they become large.

## Privacy and source of truth

GitHub remains authoritative.

Local edits made directly inside either generated output folder are not preserved. A later synchronization may replace those folders with data generated from the configured GitHub commit.

The Gemini bundle generator does not silently remove source based on pharmacy-domain words such as `patient`, `prescription`, or `pharmacy`. Filtering is based on explicit excluded directories and whether a file is plain text.

## Large files

The exact mirror reports Git-tracked files over 100 MB as warnings.

Gemini bundles are split into approximately 1 MB text chunks by default so no single generated code bundle becomes unnecessarily large.
