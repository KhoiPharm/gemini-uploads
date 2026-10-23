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
    with sync_lock(lock_path):
        # Retry local cleanup even when GitHub has not changed. This matters
        # when Drive/Explorer temporarily locked an old backup on a prior run.
        cleanup_stale_backups(output)
        ensure_mirror(source, branch, mirror, reclone=reclone)
        head = update_mirror(branch, mirror)
        state = load_json(state_path)

        if not force and output.exists() and state.get("head_sha") == head:
            print(f"[up to date] {profile_id}: {branch} @ {head[:12]}")
            return 0

        count, oversized = materialize_tracked_files(mirror, output)
        write_json_atomic(
            state_path,
            {
                "profile_id": profile_id,
                "source_repo": source,
                "branch": branch,
                "head_sha": head,
                "output_dir": str(output),
                "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

        print(f"[SUCCESS] {profile_id} refreshed from GitHub.")
        print(f"[branch] {branch}")
        print(f"[commit] {head}")
        print(f"[files]  {count} Git-tracked files")
        print(f"[folder] {output}")
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
    if len(action) > MAX_SCHTASKS_ACTION_CHARS:
        raise RuntimeError(
            "Windows Task Scheduler cannot store the background launcher because its path is too long. "
            f"Task action length is {len(action)} characters; limit is {MAX_SCHTASKS_ACTION_CHARS}. "
            "Move the Windows user profile/local-app-data path to a shorter location or configure a shorter runtime path."
        )
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
    profiles[pid] = {
        "source_repo": source,
        "branch": branch,
        "output_dir": output,
        "mirror_dir": mirror,
    }
    save_profiles(profiles_path, profiles)

    print(f"\n[setup] Repository profile: {pid}")
    print("[setup] Performing the first sync now; Git may ask you to sign in once.")
    sync_profile(pid, profiles[pid], force=True)
    register_background_task(pid, profiles_path)
    print("\n[DONE] You can close this window.")
    print("From now on Windows will refresh this repository silently in the background.")
    print("Run SetupGeminiRepo.bat again only if you want to add another repository.")
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

        if args.list:
            if not profiles:
                print("No repositories configured.")
                return 0
            for pid, profile in sorted(profiles.items()):
                print(
                    f"{pid}\n  {redact_remote_credentials(str(profile.get('source_repo') or ''))} "
                    f"[{profile.get('branch', 'main')}]\n  -> {profile.get('output_dir')}"
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
