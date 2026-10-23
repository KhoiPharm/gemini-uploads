# Gemini GitHub Upload Sync

A Windows utility that silently keeps one or more local repository mirrors synchronized with GitHub and prepares Gemini-friendly upload packs.

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
        ├── Gemini starter
        │      C:\GeminiUploads\PharmacyRepo-Gemini-Start
        │
        └── Gemini code bundles
               C:\GeminiUploads\PharmacyRepo-Gemini
```

After one-time setup, there is no terminal window during normal syncing.

## Important: do not upload the whole repository to Gemini

Gemini may successfully accept the ZIP/folder and still truncate the extracted text before reaching later files.

The supported workflow for this utility is now:

1. Upload only the folder ending in `-Gemini-Start`.
2. Let Gemini read the repository index and small orientation context.
3. Gemini identifies which source files it needs for the current task.
4. The index tells Gemini which bundle contains those files.
5. Upload only those requested `.txt` bundle files from the sibling `-Gemini` folder.

Do not ZIP the full `-Gemini` folder and do not upload every bundle at once.

## Starter folder

For example:

```text
C:\GeminiUploads\PharmacyRepo-Gemini-Start
```

It contains only three small text files:

```text
00_START_HERE.txt
01_REPOSITORY_INDEX.txt
02_CORE_CONTEXT.txt
```

`00_START_HERE.txt` tells Gemini how to request code without loading the entire repository.

`01_REPOSITORY_INDEX.txt` maps every included repository path to the exact bundle filename containing it.

`02_CORE_CONTEXT.txt` includes a small set of orientation documents when present, such as README, AGENTS, architecture, program map, and current handover documents.

## Code bundle folder

For example:

```text
C:\GeminiUploads\PharmacyRepo-Gemini
```

It contains files similar to:

```text
00_REPOSITORY_INDEX.txt
01_SOURCE_001.txt
01_SOURCE_002.txt
...
02_TESTS_001.txt
...
03_DOCS_001.txt
...
04_CONFIG_MIGRATIONS_TOOLS_001.txt
...
```

Bundles are split at approximately **256 KB** instead of 1 MB so individual requests stay comfortably smaller.

Each bundle preserves the original path:

```text
================================================================================
FILE: src/pharmacy_app/application/patients/patient_service.py
================================================================================

<exact original source text>
```

No summaries, rewrites, or AI-generated source transformations are created.

## Exact mirror

The folder without a Gemini suffix remains the exact Git-tracked mirror:

```text
C:\GeminiUploads\PharmacyRepo
```

It contains Git-tracked files from the selected commit but not `.git` or untracked local files.

The utility never performs destructive Git operations against your normal development checkout. Reset/clean operations apply only to its disposable utility-owned clone.

## Gemini bundle filtering

The exact mirror retains all Git-tracked files.

The Gemini bundle output skips by default:

- top-level `logs/`
- binary files
- symbolic links
- text encodings that cannot be read safely

The repository index records every skipped path and reason.

It does not exclude source merely because filenames contain domain words such as `patient`, `pharmacy`, or `prescription`.

## Requirements

- Windows 11
- Python 3.11+
- Git for Windows
- Git access for the source repository through Git Credential Manager, SSH, or equivalent

No third-party Python packages are required.

## First setup

1. Clone or download this repository.
2. Double-click `SetupGeminiRepo.bat`.
3. Enter the source Git repository.
4. Enter the branch.
5. Enter the exact mirror destination, for example:

```text
C:\GeminiUploads\PharmacyRepo
```

Setup automatically creates:

```text
C:\GeminiUploads\PharmacyRepo
C:\GeminiUploads\PharmacyRepo-Gemini
C:\GeminiUploads\PharmacyRepo-Gemini-Start
```

Setup performs the first sync and registers the silent Windows Scheduled Task.

## Adding another repository

Run `SetupGeminiRepo.bat` again.

Each repository receives independent configuration, mirror, exact output, Gemini starter, Gemini bundles, state, lock, scheduled task and log.

## Updating this utility

After pulling/downloading a newer utility version, double-click:

```text
UpdateGeminiRepo.bat
```

It refreshes the installed background runtime and force-refreshes all configured outputs.

You do not need to re-enter repository settings for an ordinary upgrade.

## Background behavior

Each repository is checked every two minutes while the Windows user is signed in.

The scheduled process uses `pythonw.exe` and hidden child processes, so ordinary checks do not leave a terminal open or flash a console every two minutes.

The scheduled task persists across restart. Nothing can synchronize while the laptop is powered off, but syncing resumes after Windows starts and the user signs in.

## Local configuration and logs

Runtime configuration, mirrors, locks, state and logs live under:

```text
%LOCALAPPDATA%\GeminiRepoSync\
```

Background logs are under:

```text
%LOCALAPPDATA%\GeminiRepoSync\logs\
```

## Privacy and source of truth

GitHub remains authoritative.

Generated output folders are disposable views of the configured GitHub commit. Local edits made inside them may be replaced during a later synchronization.

Repository URLs, local paths and authentication state are not committed to this public utility repository.
