# Gemini GitHub Upload Sync

A small Windows utility that keeps a clean local folder synchronized to one GitHub branch so the folder can be uploaded to Gemini web.

## What it does

```text
GitHub repository/branch
        ↓ fetch
Disposable local Git mirror
        ↓ copy Git-tracked files only
Gemini upload folder
```

The upload folder contains the repository files from the selected GitHub commit, but **does not contain `.git`**, untracked files, caches, local databases, or other files that are not committed to GitHub.

The utility never changes the source GitHub repository and never touches your normal development checkout. Its own mirror clone is disposable and may safely be reset/cleaned.

## Requirements

- Windows 11
- Python 3.11+
- Git for Windows
- Git credentials already configured for the source repository (Git Credential Manager, SSH, etc.)

No Python packages are required.

## First-time setup

1. Clone or download this `gemini-uploads` repository.
2. Double-click **`SetupGeminiRepo.bat`**.
3. Paste the source repository clone URL, for example `https://github.com/OWNER/REPOSITORY.git`.
4. Enter the branch Gemini should see.
5. Enter a local output folder, for example `C:\GeminiUploads\PharmacyRepo`.

The configuration is stored under your Windows local application-data folder, not committed to this repository.

After setup, the first synchronization runs automatically.

## Normal use

Double-click **`UpdateGeminiRepo.bat`** whenever you want to refresh the upload folder from GitHub.

For continuous checking while you work, double-click **`WatchGeminiRepo.bat`**. It checks GitHub every two minutes and only rebuilds the upload folder when the branch commit changes. Press `Ctrl+C` to stop it.

## Important behavior

- GitHub is authoritative. Local edits made directly inside the Gemini upload folder are not preserved after the next GitHub update.
- Only files tracked by Git are copied.
- The `.git` directory is never copied.
- Deleting a file from GitHub removes it from the upload folder on the next changed commit.
- A replacement upload folder is built before the old one is swapped out, reducing the chance of leaving a half-updated folder.
- Files over 100 MB are reported as warnings because they may exceed Gemini's upload limit.

## Recovery

If the disposable mirror becomes damaged, run:

```bat
python sync_github_for_gemini.py --reclone --force
```

This deletes/recreates only the utility's disposable mirror. It does not touch your normal repository checkout.

To rebuild the upload folder even if GitHub is still on the same commit:

```bat
python sync_github_for_gemini.py --force
```

## Why this is intentionally simpler

This utility does not generate handoff summaries, architecture summaries, patches, or curated source bundles. If the GitHub repository already contains the documentation and handoff material an agent needs, duplicating that information creates unnecessary complexity and another possible source of stale context.
