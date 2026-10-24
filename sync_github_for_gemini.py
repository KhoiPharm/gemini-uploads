#!/usr/bin/env python3
"""Silently mirror one or more GitHub branches into clean Gemini upload folders.

Each configured repository gets its own disposable local Git mirror, output folder,
and Windows Scheduled Task. Background runs are windowless and repeat every two
minutes while the configured Windows user is signed in.

Standard library only. Requires Git on PATH.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

APP_DIR_NAME = "GeminiRepoSync"
PROFILES_NAME = "profiles.json"
LEGACY_CONFIG_NAME = "config.json"
STATE_DIR_NAME = "state"
LOCK_DIR_NAME = "locks"
LOG_DIR_NAME = "logs"
MIRROR_DIR_NAME = "mirrors"
RUNTIME_DIR_NAME = "runtime"
TASK_PREFIX = "GeminiRepoSync-"
SYNC_INTERVAL_MINUTES = 2
MAX_UPLOAD_FILE_BYTES = 100 * 1024 * 1024
MAX_LOG_BYTES = 2 * 1024 * 1024
BACKGROUND_GIT_TIMEOUT_SECONDS = 90
MAX_SCHTASKS_ACTION_CHARS = 262
GEMINI_BUNDLE_VERSION = 2
GEMINI_BUNDLE_TARGET_BYTES = 256 * 1024
GEMINI_START_VERSION = 1
DRIVE_EXPORT_VERSION = 1
DRIVE_START_FOLDER_NAME = "Gemini Start"
DRIVE_BUNDLES_FOLDER_NAME = "Gemini Bundles"
START_CONTEXT_MAX_BYTES = 192 * 1024
START_CONTEXT_BASENAMES = {
    "readme.md",
    "agents.md",
    "architecture.md",
    "program_map.md",
    "current_handover.md",
}
DEFAULT_GEMINI_EXCLUDED_TOP_LEVEL = {"logs"}
BUNDLE_CATEGORY_PREFIXES = {
    "SOURCE": "01_SOURCE",
    "TESTS": "02_TESTS",
    "DOCS": "03_DOCS",
    "CONFIG_MIGRATIONS_TOOLS": "04_CONFIG_MIGRATIONS_TOOLS",
}

BACKGROUND_MODE = False


def local_app_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_DIR_NAME
    return Path.home() / ".gemini_repo_sync"


def default_profiles_path() -> Path:
    return local_app_dir() / PROFILES_NAME


def expand_path(value: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value.strip().strip('"')))
    return Path(expanded).resolve()


def safe_slug(value: str) -> str:
    cleaned = value.strip().rstrip("/\\")
    name = cleaned.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name).strip("-")
    return safe or "repository"


def profile_id_for(source: str, branch: str) -> str:
    digest = hashlib.sha1(f"{source.strip()}\n{branch.strip()}".encode("utf-8")).hexdigest()[:8]
    return f"{safe_slug(source)[:40]}-{digest}"


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Could not read JSON file '{path}': {exc}") from exc


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_profiles(path: Path) -> dict[str, dict[str, Any]]:
    data = load_json(path)
    if isinstance(data.get("profiles"), dict):
        return dict(data["profiles"])

    if data.get("source_repo") and data.get("output_dir"):
        branch = str(data.get("branch") or "main")
        pid = profile_id_for(str(data["source_repo"]), branch)
        return {
            pid: {
                "source_repo": str(data["source_repo"]),
                "branch": branch,
                "output_dir": str(data["output_dir"]),
                "mirror_dir": str(data.get("mirror_dir") or ""),
            }
        }

    if not path.exists() and path == default_profiles_path():
        legacy = local_app_dir() / LEGACY_CONFIG_NAME
        legacy_data = load_json(legacy)
        if legacy_data.get("source_repo") and legacy_data.get("output_dir"):
            branch = str(legacy_data.get("branch") or "main")
            pid = profile_id_for(str(legacy_data["source_repo"]), branch)
            return {
                pid: {
                    "source_repo": str(legacy_data["source_repo"]),
                    "branch": branch,
                    "output_dir": str(legacy_data["output_dir"]),
                    "mirror_dir": str(legacy_data.get("mirror_dir") or ""),
                }
            }
    return {}


def save_profiles(path: Path, profiles: dict[str, dict[str, Any]]) -> None:
    write_json_atomic(path, {"version": 2, "profiles": profiles})


def normalized_remote(value: str) -> str:
    return value.rstrip("/\\").removesuffix(".git").lower()


def redact_remote_credentials(message: str) -> str:
    """Remove URL user-info before Git diagnostics are printed or logged."""
    return re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1[credentials]@", message)


def validate_output_dir(output: Path, mirror: Path) -> None:
    output = output.resolve()
    mirror = mirror.resolve()
    if output == Path(output.anchor):
        raise RuntimeError("Refusing to use a drive/filesystem root as the output folder.")
    if output == Path.home().resolve():
        raise RuntimeError("Refusing to use your home directory as the output folder.")
    try:
        mirror.relative_to(output)
    except ValueError:
        pass
    else:
        raise RuntimeError("Mirror directory cannot be inside the Gemini output folder.")
    try:
        output.relative_to(mirror)
    except ValueError:
        pass
    else:
        raise RuntimeError("Gemini output folder cannot be inside the Git mirror directory.")


def _creation_flags() -> int:
    if BACKGROUND_MODE and sys.platform == "win32":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def run_git(args: list[str], cwd: Path | None = None, *, text: bool = True) -> str | bytes:
    cmd = ["git", *args]
    env = os.environ.copy()
    if BACKGROUND_MODE:
        env["GIT_TERMINAL_PROMPT"] = "0"
        # Git Credential Manager honors this and will fail instead of waiting
        # for an authentication UI that is hidden behind pythonw.exe.
        env["GCM_INTERACTIVE"] = "never"
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
            encoding="utf-8" if text else None,
            errors="replace" if text else None,
            check=False,
            env=env,
            creationflags=_creation_flags(),
            timeout=BACKGROUND_GIT_TIMEOUT_SECONDS if BACKGROUND_MODE else None,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Git was not found. Install Git for Windows and try again.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Git did not finish within {BACKGROUND_GIT_TIMEOUT_SECONDS} seconds. "
            "The background sync will retry on its next scheduled run."
        ) from exc
    if proc.returncode != 0:
        stderr = proc.stderr.strip() if text else proc.stderr.decode("utf-8", "replace").strip()
        stderr = redact_remote_credentials(stderr)
        raise RuntimeError(f"Git command failed ({cmd[1] if len(cmd) > 1 else 'git'}):\n{stderr}")
    return proc.stdout.strip() if text else proc.stdout


@contextmanager
def sync_lock(lock_path: Path) -> Iterable[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        age = time.time() - lock_path.stat().st_mtime
        if age > 1800:
            lock_path.unlink(missing_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        else:
            raise RuntimeError("This repository is already being synchronized by another process.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def ensure_mirror(source: str, branch: str, mirror: Path, *, reclone: bool = False) -> None:
    if reclone and mirror.exists():
        shutil.rmtree(mirror)

    if not mirror.exists():
        mirror.parent.mkdir(parents=True, exist_ok=True)
        print(f"[sync] Cloning {redact_remote_credentials(source)} ({branch}) ...")
        run_git(["clone", "--no-tags", "--single-branch", "--branch", branch, source, str(mirror)])
        return

    if not (mirror / ".git").exists():
        raise RuntimeError(
            f"Mirror path exists but is not a Git clone: {mirror}\n"
            "Remove it or run again with --reclone."
        )

    current_remote = str(run_git(["remote", "get-url", "origin"], mirror))
    if normalized_remote(current_remote) != normalized_remote(source):
        raise RuntimeError(
            "The disposable mirror points to a different repository.\n"
            f"Configured: {redact_remote_credentials(source)}\n"
            f"Mirror origin: {redact_remote_credentials(current_remote)}\n"
            "Run with --reclone to rebuild only the disposable mirror."
        )


def update_mirror(branch: str, mirror: Path) -> str:
    print(f"[sync] Checking GitHub branch '{branch}' ...")
    run_git(["fetch", "--prune", "--no-tags", "origin", branch], mirror)
    run_git(["checkout", "-B", branch, "FETCH_HEAD"], mirror)
    run_git(["reset", "--hard", "FETCH_HEAD"], mirror)
    run_git(["clean", "-fdx"], mirror)
    return str(run_git(["rev-parse", "HEAD"], mirror))


def tracked_files(mirror: Path) -> list[str]:
    raw = run_git(["ls-files", "-z"], mirror, text=False)
    assert isinstance(raw, bytes)
    return [item.decode("utf-8", "strict") for item in raw.split(b"\0") if item]


def cleanup_stale_backups(output: Path) -> None:
    """Best-effort cleanup of prior successful output backups.

    Cleanup is intentionally safe to retry. If a folder is still locked by
    Google Drive, Explorer, antivirus, or indexing, leave it for a later run.
    Never remove recovery backups while the primary output folder is missing.
    """
    if not output.exists():
        return
    parent = output.parent
    for stale_backup in parent.glob(f".{output.name}.previous-*"):
        try:
            shutil.rmtree(stale_backup)
        except OSError:
            pass


def materialize_tracked_files(mirror: Path, output: Path) -> tuple[int, list[str]]:
    files = tracked_files(mirror)
    parent = output.parent
    parent.mkdir(parents=True, exist_ok=True)

    # Recover a known-good output left under its backup name if a previous
    # process was interrupted between the two directory renames.
    if not output.exists():
        backups = list(parent.glob(f".{output.name}.previous-*"))
        backups.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
        for previous in backups:
            try:
                previous.rename(output)
                print(f"[INFO] Restored prior output before refresh: {output}")
                break
            except OSError:
                continue

    staging = parent / f".{output.name}.staging-{uuid.uuid4().hex[:8]}"
    # A unique backup means a locked folder left by a prior run cannot prevent
    # a later run from staging a fresh snapshot.
    backup = parent / f".{output.name}.previous-{uuid.uuid4().hex[:8]}"
    staging.mkdir(parents=True, exist_ok=False)
    oversized: list[str] = []

    try:
        for rel in files:
            src = mirror / Path(rel)
            dst = staging / Path(rel)
            if not src.exists() and not src.is_symlink():
                raise RuntimeError(f"Tracked file disappeared from mirror during sync: {rel}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst, follow_symlinks=False)
            try:
                if dst.stat().st_size > MAX_UPLOAD_FILE_BYTES:
                    oversized.append(rel)
            except OSError:
                pass

        moved_old = False
        if output.exists():
            output.rename(backup)
            moved_old = True
        try:
            staging.rename(output)
        except Exception:
            if moved_old and backup.exists() and not output.exists():
                backup.rename(output)
            raise

        if backup.exists():
            try:
                shutil.rmtree(backup)
            except OSError as exc:
                print(f"[WARNING] New output is ready; old folder cleanup will be retried later: {exc}")
        cleanup_stale_backups(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    return len(files), oversized


def gemini_bundle_output_dir(profile: dict[str, Any], exact_output: Path) -> Path:
    configured = str(profile.get("gemini_output_dir") or "").strip()
    if configured:
        return expand_path(configured)
    return exact_output.with_name(exact_output.name + "-Gemini")


def gemini_start_output_dir(profile: dict[str, Any], exact_output: Path) -> Path:
    configured = str(profile.get("gemini_start_output_dir") or "").strip()
    if configured:
        return expand_path(configured)
    return exact_output.with_name(exact_output.name + "-Gemini-Start")


def drive_workspace_dir(profile: dict[str, Any]) -> Path | None:
    configured = str(profile.get("drive_workspace_dir") or "").strip()
    return expand_path(configured) if configured else None


def drive_output_dirs(workspace: Path) -> tuple[Path, Path]:
    return workspace / DRIVE_START_FOLDER_NAME, workspace / DRIVE_BUNDLES_FOLDER_NAME


def validate_output_layout(*outputs: Path) -> None:
    resolved = [path.resolve() for path in outputs]
    for index, left in enumerate(resolved):
        for right in resolved[index + 1:]:
            if left == right:
                raise RuntimeError("Generated output folders must be different from each other.")
            for child, parent in ((left, right), (right, left)):
                try:
                    child.relative_to(parent)
                except ValueError:
                    continue
                raise RuntimeError(
                    f"Generated output folders cannot be nested: '{child}' is inside '{parent}'."
                )


def gemini_excluded_top_level(profile: dict[str, Any]) -> set[str]:
    configured = profile.get("gemini_excluded_dirs")
    if configured is None:
        return set(DEFAULT_GEMINI_EXCLUDED_TOP_LEVEL)
    if not isinstance(configured, list):
        raise RuntimeError("gemini_excluded_dirs must be a JSON list when configured.")
    return {str(item).strip().lower() for item in configured if str(item).strip()}


def classify_bundle_path(rel: str) -> str:
    normalized = rel.replace("\\", "/")
    lower = normalized.lower()
    parts = lower.split("/")
    first = parts[0] if parts else ""
    name = parts[-1] if parts else lower
    suffix = Path(name).suffix.lower()

    if first == "tests" or name.startswith("test_") or "/test_" in lower:
        return "TESTS"
    if first == "docs" or suffix == ".md":
        return "DOCS"
    if first in {".github", ".verification", "migrations", "tools", "scripts", "ops"}:
        return "CONFIG_MIGRATIONS_TOOLS"
    if suffix in {
        ".json", ".jsonl", ".sql", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf",
        ".csv", ".tsv", ".bat", ".cmd", ".ps1", ".sh", ".xml",
    }:
        return "CONFIG_MIGRATIONS_TOOLS"
    return "SOURCE"


def decode_bundle_text(path: Path) -> str | None:
    if path.is_symlink():
        return None
    data = path.read_bytes()
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            try:
                return data.decode("utf-16")
            except UnicodeDecodeError:
                return None
        return None


def _bundle_section(rel: str, text: str) -> str:
    content = text.rstrip("\r\n")
    return (
        "\n" + "=" * 80 + "\n"
        f"FILE: {rel}\n"
        + "=" * 80 + "\n\n"
        + content + "\n"
    )


def _promote_generated_directory(staging: Path, output: Path) -> None:
    backup = output.parent / f".{output.name}.previous-{uuid.uuid4().hex[:8]}"
    moved_old = False
    if output.exists():
        output.rename(backup)
        moved_old = True
    try:
        staging.rename(output)
    except Exception:
        if moved_old and backup.exists() and not output.exists():
            backup.rename(output)
        raise

    if backup.exists():
        try:
            shutil.rmtree(backup)
        except OSError as exc:
            print(f"[WARNING] New Gemini bundles are ready; old bundle cleanup will retry later: {exc}")
    cleanup_stale_backups(output)


def sync_generated_directory_in_place(source: Path, destination: Path) -> int:
    """Mirror generated files into a Drive folder without replacing the folder itself."""
    if not source.is_dir():
        raise RuntimeError(f"Generated source folder is missing: {source}")
    destination.mkdir(parents=True, exist_ok=True)

    source_files = {
        path.relative_to(source)
        for path in source.rglob("*")
        if path.is_file()
    }
    temp_paths: list[Path] = []
    copied = 0
    try:
        for rel in sorted(source_files, key=lambda item: str(item).lower()):
            src = source / rel
            dst = destination / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            temp = dst.with_name(dst.name + f".GeminiRepoSync-{uuid.uuid4().hex[:8]}.tmp")
            temp_paths.append(temp)
            shutil.copy2(src, temp)
            os.replace(temp, dst)
            copied += 1

        for existing in sorted(
            (path for path in destination.rglob("*") if path.is_file()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            rel = existing.relative_to(destination)
            if rel in source_files:
                continue
            existing.unlink()

        for directory in sorted(
            (path for path in destination.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
    finally:
        for temp in temp_paths:
            temp.unlink(missing_ok=True)

    return copied


def materialize_gemini_bundles(
    mirror: Path,
    output: Path,
    head_sha: str,
    *,
    excluded_top_level: set[str] | None = None,
    target_bytes: int = GEMINI_BUNDLE_TARGET_BYTES,
) -> dict[str, Any]:
    """Create Gemini-friendly plain-text bundles from Git-tracked repository content."""
    selected_exclusions = DEFAULT_GEMINI_EXCLUDED_TOP_LEVEL if excluded_top_level is None else excluded_top_level
    excluded = {item.lower() for item in selected_exclusions}
    categories: dict[str, list[tuple[str, str]]] = {key: [] for key in BUNDLE_CATEGORY_PREFIXES}
    skipped: list[tuple[str, str]] = []

    for rel in sorted(tracked_files(mirror), key=str.lower):
        normalized = rel.replace("\\", "/")
        first = normalized.split("/", 1)[0].lower()
        if first in excluded:
            skipped.append((rel, f"excluded directory: {first}"))
            continue

        src = mirror / Path(rel)
        try:
            text = decode_bundle_text(src)
        except OSError as exc:
            skipped.append((rel, f"unreadable: {type(exc).__name__}"))
            continue
        if text is None:
            reason = "symlink" if src.is_symlink() else "binary or non-text encoding"
            skipped.append((rel, reason))
            continue
        categories[classify_bundle_path(rel)].append((rel, text))

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid.uuid4().hex[:8]}"
    staging.mkdir(parents=True, exist_ok=False)
    bundle_records: list[tuple[str, str, list[str]]] = []
    included_mapping: list[tuple[str, str, str]] = []

    try:
        for category, prefix in BUNDLE_CATEGORY_PREFIXES.items():
            entries = categories[category]
            if not entries:
                continue
            chunk_number = 1
            current_sections: list[str] = []
            current_paths: list[str] = []
            header = (
                "GENERATED GEMINI CODE BUNDLE\n"
                f"Git commit: {head_sha}\n"
                f"Category: {category}\n"
                "Each FILE section below contains the exact text from one Git-tracked file.\n"
                "Section headers are generated metadata and are not part of the original file.\n"
            )
            current_bytes = len(header.encode("utf-8"))

            def flush_chunk() -> None:
                nonlocal chunk_number, current_sections, current_paths, current_bytes
                if not current_sections:
                    return
                name = f"{prefix}_{chunk_number:03d}.txt"
                (staging / name).write_text(header + "".join(current_sections), encoding="utf-8", newline="\n")
                bundle_records.append((name, category, list(current_paths)))
                for path in current_paths:
                    included_mapping.append((path, category, name))
                chunk_number += 1
                current_sections = []
                current_paths = []
                current_bytes = len(header.encode("utf-8"))

            for rel, text in entries:
                section = _bundle_section(rel, text)
                section_bytes = len(section.encode("utf-8"))
                if current_sections and current_bytes + section_bytes > target_bytes:
                    flush_chunk()
                current_sections.append(section)
                current_paths.append(rel)
                current_bytes += section_bytes
            flush_chunk()

        index_lines = [
            "GEMINI REPOSITORY BUNDLE INDEX",
            f"Git commit: {head_sha}",
            f"Bundle format version: {GEMINI_BUNDLE_VERSION}",
            "",
            "DO NOT ZIP OR UPLOAD THIS ENTIRE FOLDER AT ONCE.",
            "First upload the sibling '-Gemini-Start' folder.",
            "Then upload only the specific bundle file(s) Gemini requests from this folder.",
            "The sibling folder without '-Gemini' is the exact Git-tracked mirror.",
            "These bundles contain source text only; no summaries or rewritten code are generated.",
            "",
            f"Included text files: {len(included_mapping)}",
            f"Skipped files: {len(skipped)}",
            f"Bundle files: {len(bundle_records)}",
            f"Excluded top-level directories: {', '.join(sorted(excluded)) or '(none)'}",
            "",
            "BUNDLES",
        ]
        for name, category, paths in bundle_records:
            index_lines.append(f"{name}\t{category}\t{len(paths)} files")
        index_lines.extend(["", "INCLUDED FILES", "bundle\tcategory\tpath"])
        for rel, category, name in sorted(included_mapping, key=lambda item: item[0].lower()):
            index_lines.append(f"{name}\t{category}\t{rel}")
        index_lines.extend(["", "SKIPPED FILES", "reason\tpath"])
        if skipped:
            for rel, reason in sorted(skipped, key=lambda item: item[0].lower()):
                index_lines.append(f"{reason}\t{rel}")
        else:
            index_lines.append("(none)")
        (staging / "00_REPOSITORY_INDEX.txt").write_text("\n".join(index_lines) + "\n", encoding="utf-8", newline="\n")

        _promote_generated_directory(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    return {
        "included_files": len(included_mapping),
        "skipped_files": len(skipped),
        "bundle_files": len(bundle_records),
        "output_dir": str(output),
    }


def materialize_gemini_start(
    mirror: Path,
    bundle_output: Path,
    start_output: Path,
    head_sha: str,
) -> dict[str, Any]:
    """Create a tiny first-upload folder that tells Gemini which bundles to request."""
    bundle_index = bundle_output / "00_REPOSITORY_INDEX.txt"
    if not bundle_index.is_file():
        raise RuntimeError(f"Gemini bundle index is missing: {bundle_index}")

    start_output.parent.mkdir(parents=True, exist_ok=True)
    staging = start_output.parent / f".{start_output.name}.staging-{uuid.uuid4().hex[:8]}"
    staging.mkdir(parents=True, exist_ok=False)
    context_sections: list[str] = []
    context_paths: list[str] = []
    context_bytes = 0

    try:
        guide = (
            "GEMINI REPOSITORY STARTER\n"
            f"Git commit: {head_sha}\n"
            f"Starter format version: {GEMINI_START_VERSION}\n\n"
            "PURPOSE\n"
            "This folder is intentionally small. Read it first to understand the repository map.\n\n"
            "IMPORTANT UPLOAD RULE\n"
            "Do NOT ask the user to upload the whole repository ZIP or the whole '-Gemini' folder.\n"
            "Those can exceed Gemini's extraction/context stream even when the files are valid.\n\n"
            "WORKFLOW\n"
            "1. Read 01_REPOSITORY_INDEX.txt and 02_CORE_CONTEXT.txt.\n"
            "2. Identify the exact repository files needed for the current task.\n"
            "3. Use the index to find which bundle contains each required file.\n"
            "4. Ask the user to upload only those bundle file(s) from the sibling '-Gemini' folder.\n"
            "5. Prefer the smallest set of bundles necessary for the task.\n\n"
            "The bundle files preserve original repository paths and exact source text.\n"
        )
        (staging / "00_START_HERE.txt").write_text(guide, encoding="utf-8", newline="\n")
        shutil.copy2(bundle_index, staging / "01_REPOSITORY_INDEX.txt")

        for rel in sorted(tracked_files(mirror), key=str.lower):
            if Path(rel).name.lower() not in START_CONTEXT_BASENAMES:
                continue
            src = mirror / Path(rel)
            try:
                text = decode_bundle_text(src)
            except OSError:
                continue
            if text is None:
                continue
            section = _bundle_section(rel, text)
            size = len(section.encode("utf-8"))
            if size > START_CONTEXT_MAX_BYTES or context_bytes + size > START_CONTEXT_MAX_BYTES:
                continue
            context_sections.append(section)
            context_paths.append(rel)
            context_bytes += size

        core_header = (
            "GEMINI STARTER CORE CONTEXT\n"
            f"Git commit: {head_sha}\n"
            "This file contains selected repository orientation documents only.\n"
            "Use 01_REPOSITORY_INDEX.txt to request implementation bundles for actual code review.\n"
        )
        (staging / "02_CORE_CONTEXT.txt").write_text(
            core_header + "".join(context_sections), encoding="utf-8", newline="\n"
        )
        _promote_generated_directory(staging, start_output)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    return {
        "output_dir": str(start_output),
        "context_files": len(context_paths),
        "context_bytes": context_bytes,
    }


def profile_paths(profile_id: str, profile: dict[str, Any]) -> tuple[str, str, Path, Path, Path, Path]:
    source = str(profile.get("source_repo") or "").strip()
    branch = str(profile.get("branch") or "main").strip()
    output_raw = str(profile.get("output_dir") or "").strip()
    mirror_raw = str(profile.get("mirror_dir") or "").strip()
    if not source or not output_raw:
        raise RuntimeError(f"Profile '{profile_id}' is incomplete.")
    output = expand_path(output_raw)
    mirror = expand_path(mirror_raw) if mirror_raw else local_app_dir() / MIRROR_DIR_NAME / profile_id
    validate_output_dir(output, mirror)
    state_path = local_app_dir() / STATE_DIR_NAME / f"{profile_id}.json"
    lock_path = local_app_dir() / LOCK_DIR_NAME / f"{profile_id}.lock"
    return source, branch, output, mirror, state_path, lock_path


def sync_profile(profile_id: str, profile: dict[str, Any], *, force: bool = False, reclone: bool = False) -> int:
    source, branch, output, mirror, state_path, lock_path = profile_paths(profile_id, profile)
    gemini_output = gemini_bundle_output_dir(profile, output)
    gemini_start = gemini_start_output_dir(profile, output)
    drive_workspace = drive_workspace_dir(profile)
    drive_start: Path | None = None
    drive_bundles: Path | None = None

    outputs = [output, gemini_output, gemini_start]
    if drive_workspace is not None:
        drive_start, drive_bundles = drive_output_dirs(drive_workspace)
        outputs.extend([drive_start, drive_bundles])

    validate_output_layout(*outputs)
    validate_output_dir(gemini_output, mirror)
    validate_output_dir(gemini_start, mirror)
    if drive_start is not None and drive_bundles is not None:
        validate_output_dir(drive_start, mirror)
        validate_output_dir(drive_bundles, mirror)

    with sync_lock(lock_path):
        cleanup_stale_backups(output)
        cleanup_stale_backups(gemini_output)
        cleanup_stale_backups(gemini_start)
        ensure_mirror(source, branch, mirror, reclone=reclone)
        head = update_mirror(branch, mirror)
        state = load_json(state_path)

        same_head = state.get("head_sha") == head
        exact_current = output.exists() and same_head
        bundle_current = (
            gemini_output.exists()
            and same_head
            and state.get("bundle_version") == GEMINI_BUNDLE_VERSION
        )
        start_current = (
            gemini_start.exists()
            and same_head
            and state.get("start_version") == GEMINI_START_VERSION
            and bundle_current
        )
        drive_current = drive_workspace is None or (
            drive_start is not None
            and drive_bundles is not None
            and drive_start.exists()
            and drive_bundles.exists()
            and state.get("drive_head_sha") == head
            and state.get("drive_export_version") == DRIVE_EXPORT_VERSION
        )

        if not force and exact_current and bundle_current and start_current and drive_current:
            print(f"[up to date] {profile_id}: {branch} @ {head[:12]}")
            print(f"[Gemini starter] {gemini_start}")
            print(f"[Gemini bundles] {gemini_output}")
            if drive_workspace is not None:
                print(f"[Drive workspace] {drive_workspace}")
            return 0

        if force or not exact_current:
            count, oversized = materialize_tracked_files(mirror, output)
        else:
            count = len(tracked_files(mirror))
            oversized = []

        if force or not bundle_current:
            bundle_stats = materialize_gemini_bundles(
                mirror,
                gemini_output,
                head,
                excluded_top_level=gemini_excluded_top_level(profile),
            )
        else:
            bundle_stats = {
                "included_files": state.get("gemini_included_files", 0),
                "skipped_files": state.get("gemini_skipped_files", 0),
                "bundle_files": state.get("gemini_bundle_files", 0),
                "output_dir": str(gemini_output),
            }

        if force or not start_current:
            start_stats = materialize_gemini_start(mirror, gemini_output, gemini_start, head)
        else:
            start_stats = {
                "output_dir": str(gemini_start),
                "context_files": state.get("gemini_start_context_files", 0),
                "context_bytes": state.get("gemini_start_context_bytes", 0),
            }

        drive_stats: dict[str, Any] | None = None
        if drive_workspace is not None and drive_start is not None and drive_bundles is not None:
            if force or not drive_current:
                start_file_count = sync_generated_directory_in_place(gemini_start, drive_start)
                bundle_file_count = sync_generated_directory_in_place(gemini_output, drive_bundles)
                drive_stats = {
                    "workspace": str(drive_workspace),
                    "start_files": start_file_count,
                    "bundle_files": bundle_file_count,
                }
            else:
                drive_stats = {
                    "workspace": str(drive_workspace),
                    "start_files": state.get("drive_start_files", 0),
                    "bundle_files": state.get("drive_bundle_files", 0),
                }

        state_data: dict[str, Any] = {
            "profile_id": profile_id,
            "source_repo": source,
            "branch": branch,
            "head_sha": head,
            "output_dir": str(output),
            "gemini_output_dir": str(gemini_output),
            "gemini_start_output_dir": str(gemini_start),
            "bundle_version": GEMINI_BUNDLE_VERSION,
            "start_version": GEMINI_START_VERSION,
            "gemini_included_files": bundle_stats["included_files"],
            "gemini_skipped_files": bundle_stats["skipped_files"],
            "gemini_bundle_files": bundle_stats["bundle_files"],
            "gemini_start_context_files": start_stats["context_files"],
            "gemini_start_context_bytes": start_stats["context_bytes"],
            "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if drive_stats is not None:
            state_data.update(
                {
                    "drive_workspace_dir": drive_stats["workspace"],
                    "drive_head_sha": head,
                    "drive_export_version": DRIVE_EXPORT_VERSION,
                    "drive_start_files": drive_stats["start_files"],
                    "drive_bundle_files": drive_stats["bundle_files"],
                }
            )
        write_json_atomic(state_path, state_data)

        print(f"[SUCCESS] {profile_id} refreshed from GitHub.")
        print(f"[branch] {branch}")
        print(f"[commit] {head}")
        print(f"[files]  {count} Git-tracked files in exact mirror")
        print(f"[folder] {output}")
        print(
            f"[Gemini] {bundle_stats['included_files']} text files -> "
            f"{bundle_stats['bundle_files']} bundles; {bundle_stats['skipped_files']} skipped"
        )
        print(f"[Gemini starter] {gemini_start}")
        print(f"[Gemini bundles] {gemini_output}")
        if drive_stats is not None:
            print(f"[Drive workspace] {drive_stats['workspace']}")
            print(f"[Drive] {DRIVE_START_FOLDER_NAME}: {drive_stats['start_files']} files")
            print(f"[Drive] {DRIVE_BUNDLES_FOLDER_NAME}: {drive_stats['bundle_files']} files")
        if oversized:
            print("[WARNING] Files over 100 MB may exceed Gemini's per-file upload limit:")
            for path in oversized:
                print(f"  - {path}")
        return 0

def find_pythonw() -> Path:
    exe = Path(sys.executable).resolve()
    sibling = exe.with_name("pythonw.exe")
    if sibling.exists():
        return sibling
    found = shutil.which("pythonw.exe") or shutil.which("pythonw")
    if found:
        return Path(found).resolve()
    raise RuntimeError(
        "Could not find pythonw.exe, which is needed for silent background sync. "
        "Reinstall Python for Windows with the standard launcher/runtime enabled."
    )


def installed_runtime_script() -> Path:
    return local_app_dir() / RUNTIME_DIR_NAME / "sync_github_for_gemini.py"


def install_runtime_copy() -> Path:
    target = installed_runtime_script()
    target.parent.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve()
    if source != target:
        shutil.copy2(source, target)
    return target


def task_name_for(profile_id: str) -> str:
    return f"{TASK_PREFIX}{profile_id}"[:230]


def vbs_quote(value: str) -> str:
    return value.replace('"', '""')


def write_hidden_launcher(profile_id: str, profiles_path: Path, pythonw: Path, runtime_script: Path) -> Path:
    launcher = local_app_dir() / RUNTIME_DIR_NAME / f"launch-{profile_id}.vbs"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    command = (
        f'"{pythonw}" "{runtime_script}" --profile "{profile_id}" '
        f'--config "{profiles_path}" --background'
    )
    content = (
        'Option Explicit\n'
        'Dim shell\n'
        'Set shell = CreateObject("WScript.Shell")\n'
        f'shell.Run "{vbs_quote(command)}", 0, False\n'
    )
    # WSH must read non-ASCII usernames and folder names correctly.
    launcher.write_text(content, encoding="utf-16")
    return launcher


def build_task_action(launcher: Path) -> str:
    return f'wscript.exe //B //Nologo "{launcher}"'


def validate_task_action(action: str) -> None:
    """Fail with a clear message before schtasks rejects an overlong /TR value."""
    if len(action) > MAX_SCHTASKS_ACTION_CHARS:
        raise RuntimeError(
            "Windows Task Scheduler cannot store the background launcher because its path is too long. "
            f"Task action length is {len(action)} characters; limit is {MAX_SCHTASKS_ACTION_CHARS}. "
            "Use a shorter Windows user/local-app-data path or configure a shorter runtime path."
        )


def build_task_registration_args(task_name: str, action: str) -> list[str]:
    """Return schtasks arguments separately so Windows task setup is testable."""
    return [
        "schtasks.exe", "/Create", "/TN", task_name, "/TR", action,
        "/SC", "MINUTE", "/MO", str(SYNC_INTERVAL_MINUTES),
        "/IT", "/RL", "LIMITED", "/F",
    ]


def register_background_task(profile_id: str, profiles_path: Path) -> None:
    if sys.platform != "win32":
        print("[INFO] Windows Task Scheduler registration skipped on non-Windows system.")
        return

    runtime_script = install_runtime_copy()
    pythonw = find_pythonw()
    launcher = write_hidden_launcher(profile_id, profiles_path, pythonw, runtime_script)
    task_name = task_name_for(profile_id)
    action = build_task_action(launcher)
    validate_task_action(action)
    cmd = build_task_registration_args(task_name, action)
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"Could not register Windows background task:\n{proc.stderr.strip() or proc.stdout.strip()}")
    print(f"[SUCCESS] Silent background sync registered: {task_name}")
    print(f"          It runs every {SYNC_INTERVAL_MINUTES} minutes while you are signed in.")


def remove_background_task(profile_id: str) -> None:
    if sys.platform != "win32":
        return
    subprocess.run(
        ["schtasks.exe", "/Delete", "/TN", task_name_for(profile_id), "/F"],
        capture_output=True,
        text=True,
        check=False,
    )


def rotate_log(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            old = path.with_suffix(".old.log")
            old.unlink(missing_ok=True)
            path.replace(old)
    except OSError:
        pass


def enable_background_logging(profile_id: str) -> None:
    log_path = local_app_dir() / LOG_DIR_NAME / f"{profile_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    rotate_log(log_path)
    stream = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = stream
    sys.stderr = stream
    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] background sync start")


def interactive_setup(profiles_path: Path) -> str:
    print("\nGemini GitHub Sync - add or update a repository")
    print("This setup window is only for configuration. Normal syncing will be silent.\n")
    source = input("GitHub repository clone URL (HTTPS or SSH): ").strip()
    if not source:
        raise RuntimeError("Repository URL cannot be empty.")
    branch = input("Branch to mirror [main]: ").strip() or "main"
    output = input(r"Gemini upload folder (example C:\GeminiUploads\PharmacyRepo): ").strip()
    if not output:
        raise RuntimeError("Output folder cannot be empty.")

    pid = profile_id_for(source, branch)
    profiles = load_profiles(profiles_path)
    mirror = str(local_app_dir() / MIRROR_DIR_NAME / pid)
    exact_output = expand_path(output)
    gemini_output = exact_output.with_name(exact_output.name + "-Gemini")
    gemini_start = exact_output.with_name(exact_output.name + "-Gemini-Start")
    profiles[pid] = {
        "source_repo": source,
        "branch": branch,
        "output_dir": output,
        "gemini_output_dir": str(gemini_output),
        "gemini_start_output_dir": str(gemini_start),
        "mirror_dir": mirror,
    }
    save_profiles(profiles_path, profiles)

    print(f"\n[setup] Repository profile: {pid}")
    print("[setup] Performing the first sync now; Git may ask you to sign in once.")
    sync_profile(pid, profiles[pid], force=True)
    register_background_task(pid, profiles_path)
    print("\n[DONE] You can close this window.")
    print("From now on Windows will refresh this repository silently in the background.")
    print(f"Upload this folder FIRST: {gemini_start}")
    print(f"When Gemini asks for code, attach the requested files from: {gemini_output}")
    print("Run SetupGeminiRepo.bat again only if you want to add another repository.")
    return pid


def select_profile_for_configuration(
    profiles: dict[str, dict[str, Any]],
    requested_profile: str | None = None,
) -> str:
    if requested_profile:
        if requested_profile not in profiles:
            raise RuntimeError(f"Unknown profile '{requested_profile}'. Run with --list to see configured repositories.")
        return requested_profile
    if len(profiles) == 1:
        return next(iter(profiles))
    if not profiles:
        raise RuntimeError("No repositories are configured. Run SetupGeminiRepo.bat first.")

    ordered = sorted(profiles)
    print("\nMore than one repository is configured:")
    for index, pid in enumerate(ordered, start=1):
        profile = profiles[pid]
        print(
            f"  {index}. {safe_slug(str(profile.get('source_repo') or pid))} "
            f"[{profile.get('branch', 'main')}]"
        )
    choice = input("Choose the repository number for this Drive workspace: ").strip()
    try:
        selected = int(choice)
    except ValueError as exc:
        raise RuntimeError("Repository selection must be a number.") from exc
    if selected < 1 or selected > len(ordered):
        raise RuntimeError("Repository selection is out of range.")
    return ordered[selected - 1]


def configure_drive_workspace(
    profiles_path: Path,
    workspace_value: str,
    requested_profile: str | None = None,
) -> str:
    workspace = expand_path(workspace_value)
    if not workspace.exists() or not workspace.is_dir():
        raise RuntimeError(
            "The selected Google Drive workspace is not available as a local folder. "
            "Open Google Drive for Desktop, wait for it to appear in File Explorer, then try again."
        )

    profiles = load_profiles(profiles_path)
    pid = select_profile_for_configuration(profiles, requested_profile)
    profile = profiles[pid]
    profile["drive_workspace_dir"] = str(workspace)
    profiles[pid] = profile
    save_profiles(profiles_path, profiles)

    # Refresh the installed scheduled-task runtime so background sync immediately
    # understands the new optional Drive destination. The task definition itself
    # does not change.
    install_runtime_copy()

    print(f"\n[Drive setup] Repository profile: {pid}")
    print(f"[Drive setup] Workspace: {workspace}")
    print("[Drive setup] Existing local mirror/starter/bundles will be preserved.")
    sync_profile(pid, profile, force=True)

    drive_start, drive_bundles = drive_output_dirs(workspace)
    print("\n[DONE] Google Drive is now an additional synchronized destination.")
    print(f"[Drive] Starter: {drive_start}")
    print(f"[Drive] Bundles: {drive_bundles}")
    print("Your original local folders continue to update as before.")
    return pid


def select_profiles(args: argparse.Namespace, profiles: dict[str, dict[str, Any]]) -> list[str]:
    if args.all:
        return sorted(profiles)
    if args.profile:
        if args.profile not in profiles:
            raise RuntimeError(f"Unknown profile '{args.profile}'. Run with --list to see configured repositories.")
        return [args.profile]
    if len(profiles) == 1:
        return [next(iter(profiles))]
    if not profiles:
        raise RuntimeError("No repositories are configured. Run SetupGeminiRepo.bat first.")
    raise RuntimeError("More than one repository is configured. Use --profile <id> or --all.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Keep GitHub repositories mirrored into clean Gemini upload folders.")
    p.add_argument("--setup", action="store_true", help="Interactively add/update a repository and install its background task.")
    p.add_argument("--config", help="Override profiles JSON path.")
    p.add_argument("--profile", help="Synchronize one configured repository profile.")
    p.add_argument("--all", action="store_true", help="Synchronize all configured repositories.")
    p.add_argument("--list", action="store_true", help="List configured repository profiles.")
    p.add_argument("--force", action="store_true", help="Rebuild output even if GitHub commit is unchanged.")
    p.add_argument("--reclone", action="store_true", help="Recreate only the selected disposable mirror clone.")
    p.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--refresh-runtime", action="store_true", help="Update the installed silent background runtime from this copy.")
    p.add_argument("--configure-drive", metavar="FOLDER", help="Add a Google Drive for Desktop workspace as a second output destination.")
    p.add_argument("--install-task", metavar="PROFILE", help="Install/update the Windows background task for a profile.")
    p.add_argument("--remove-task", metavar="PROFILE", help="Remove the Windows background task for a profile.")
    return p


def main() -> int:
    global BACKGROUND_MODE
    args = build_parser().parse_args()
    BACKGROUND_MODE = bool(args.background)
    profiles_path = expand_path(args.config) if args.config else default_profiles_path()

    if sys.platform == "win32" and not BACKGROUND_MODE:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if BACKGROUND_MODE and args.profile:
        enable_background_logging(args.profile)

    try:
        if args.setup:
            interactive_setup(profiles_path)
            return 0

        profiles = load_profiles(profiles_path)

        if args.configure_drive:
            configure_drive_workspace(profiles_path, args.configure_drive, args.profile)
            return 0

        if args.refresh_runtime:
            runtime = install_runtime_copy()
            print(f"[SUCCESS] Background runtime updated: {runtime}")

        if args.list:
            if not profiles:
                print("No repositories configured.")
                return 0
            for pid, profile in sorted(profiles.items()):
                exact = expand_path(str(profile.get("output_dir") or ""))
                gemini = gemini_bundle_output_dir(profile, exact)
                starter = gemini_start_output_dir(profile, exact)
                drive = drive_workspace_dir(profile)
                print(
                    f"{pid}\n  {redact_remote_credentials(str(profile.get('source_repo') or ''))} "
                    f"[{profile.get('branch', 'main')}]\n  exact -> {exact}\n"
                    f"  starter -> {starter}\n  bundles -> {gemini}"
                    + (f"\n  Drive -> {drive}" if drive is not None else "")
                )
            return 0

        if args.install_task:
            if args.install_task not in profiles:
                raise RuntimeError(f"Unknown profile '{args.install_task}'.")
            register_background_task(args.install_task, profiles_path)
            return 0

        if args.remove_task:
            remove_background_task(args.remove_task)
            print(f"Background task removed for {args.remove_task}.")
            return 0

        selected = select_profiles(args, profiles)
        failures = 0
        for pid in selected:
            try:
                sync_profile(pid, profiles[pid], force=args.force, reclone=args.reclone)
            except Exception as exc:
                failures += 1
                print(f"[ERROR] {pid}: {exc}", file=sys.stderr)
        return 1 if failures else 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"\n[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
