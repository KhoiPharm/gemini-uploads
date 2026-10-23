#!/usr/bin/env python3
"""Keep a clean Gemini-upload folder synchronized to one GitHub branch.

The program uses a dedicated disposable Git clone, never the user's normal
working checkout. Only Git-tracked files from the selected commit are copied
into the upload folder; .git metadata and local/generated files are excluded.

Standard library only. Requires Git on PATH.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

APP_DIR_NAME = "GeminiRepoSync"
CONFIG_NAME = "config.json"
STATE_NAME = "state.json"
LOCK_NAME = "sync.lock"
DEFAULT_POLL_SECONDS = 120
MAX_UPLOAD_FILE_BYTES = 100 * 1024 * 1024


def local_app_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_DIR_NAME
    return Path.home() / ".gemini_repo_sync"


def default_config_path() -> Path:
    return local_app_dir() / CONFIG_NAME


def expand_path(value: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value.strip().strip('"')))
    return Path(expanded).resolve()


def run_git(args: list[str], cwd: Path | None = None, *, text: bool = True) -> str | bytes:
    cmd = ["git", *args]
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
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Git was not found. Install Git for Windows and try again.") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.strip() if text else proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"Git command failed: {' '.join(cmd)}\n{stderr}")
    return proc.stdout.strip() if text else proc.stdout


def repo_slug(source: str) -> str:
    cleaned = source.rstrip("/\\")
    name = cleaned.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return safe or "repository"


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


def normalized_remote(value: str) -> str:
    return value.rstrip("/\\").removesuffix(".git").lower()


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
            raise RuntimeError("Another Gemini repository sync is already running.")
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
        print(f"[sync] Cloning {source} ({branch}) ...")
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
            "The existing mirror points to a different repository.\n"
            f"Configured: {source}\nMirror origin: {current_remote}\n"
            "Run with --reclone to rebuild the disposable mirror safely."
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
    files: list[str] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        files.append(item.decode("utf-8", "strict"))
    return files


def materialize_tracked_files(mirror: Path, output: Path) -> tuple[int, list[str]]:
    files = tracked_files(mirror)
    parent = output.parent
    parent.mkdir(parents=True, exist_ok=True)

    staging = parent / f".{output.name}.staging-{uuid.uuid4().hex[:8]}"
    backup = parent / f".{output.name}.previous"
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

        if backup.exists():
            shutil.rmtree(backup)

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
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    return len(files), oversized


def resolve_settings(args: argparse.Namespace) -> tuple[str, str, Path, Path, Path]:
    config_path = expand_path(args.config) if args.config else default_config_path()
    cfg = load_json(config_path)

    source = args.source or cfg.get("source_repo")
    branch = args.branch or cfg.get("branch") or "main"
    output_raw = args.output or cfg.get("output_dir")
    mirror_raw = args.mirror or cfg.get("mirror_dir")

    if not source or not output_raw:
        raise RuntimeError(
            "Sync is not configured yet. Run SetupGeminiRepo.bat once, or run:\n"
            "  python sync_github_for_gemini.py --setup"
        )

    output = expand_path(str(output_raw))
    mirror = expand_path(str(mirror_raw)) if mirror_raw else local_app_dir() / "mirrors" / repo_slug(str(source))
    validate_output_dir(output, mirror)
    return str(source), str(branch), output, mirror, config_path


def interactive_setup(config_path: Path) -> None:
    print("\nGemini GitHub Sync - one-time setup")
    print("This stores configuration only on this computer, not in GitHub.\n")
    source = input("GitHub repository clone URL (HTTPS or SSH): ").strip()
    if not source:
        raise RuntimeError("Repository URL cannot be empty.")
    branch = input("Branch to mirror [main]: ").strip() or "main"
    output = input(r"Gemini upload folder (example C:\GeminiUploads\PharmacyRepo): ").strip()
    if not output:
        raise RuntimeError("Output folder cannot be empty.")
    mirror = str(local_app_dir() / "mirrors" / repo_slug(source))
    write_json_atomic(
        config_path,
        {
            "source_repo": source,
            "branch": branch,
            "output_dir": output,
            "mirror_dir": mirror,
        },
    )
    print(f"\n[SUCCESS] Configuration saved locally: {config_path}")
    print("Run UpdateGeminiRepo.bat to create/update the Gemini upload folder.")


def sync_once(args: argparse.Namespace) -> int:
    source, branch, output, mirror, config_path = resolve_settings(args)
    state_path = config_path.with_name(STATE_NAME)
    lock_path = config_path.with_name(LOCK_NAME)

    with sync_lock(lock_path):
        ensure_mirror(source, branch, mirror, reclone=args.reclone)
        head = update_mirror(branch, mirror)
        state = load_json(state_path)

        if not args.force and output.exists() and state.get("head_sha") == head:
            print(f"[up to date] {branch} @ {head[:12]}")
            print(f"[folder] {output}")
            return 0

        count, oversized = materialize_tracked_files(mirror, output)
        write_json_atomic(
            state_path,
            {
                "source_repo": source,
                "branch": branch,
                "head_sha": head,
                "output_dir": str(output),
                "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

        print("[SUCCESS] Gemini upload folder refreshed from GitHub.")
        print(f"[branch] {branch}")
        print(f"[commit] {head}")
        print(f"[files]  {count} Git-tracked files")
        print(f"[folder] {output}")
        if oversized:
            print("[WARNING] These files are over 100 MB and may exceed Gemini's per-file upload limit:")
            for path in oversized:
                print(f"  - {path}")
        return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Mirror one GitHub branch into a clean Gemini upload folder.")
    p.add_argument("--setup", action="store_true", help="Run interactive one-time setup.")
    p.add_argument("--config", help="Override local config JSON path.")
    p.add_argument("--source", help="Override source Git repository URL/path.")
    p.add_argument("--branch", help="Override branch name.")
    p.add_argument("--output", help="Override Gemini upload folder.")
    p.add_argument("--mirror", help="Override disposable local Git clone path.")
    p.add_argument("--force", action="store_true", help="Rebuild output even if commit SHA is unchanged.")
    p.add_argument("--reclone", action="store_true", help="Delete/recreate only the disposable mirror clone.")
    p.add_argument(
        "--watch",
        nargs="?",
        const=DEFAULT_POLL_SECONDS,
        type=int,
        metavar="SECONDS",
        help=f"Keep polling GitHub (default {DEFAULT_POLL_SECONDS}s).",
    )
    return p


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = build_parser().parse_args()
    config_path = expand_path(args.config) if args.config else default_config_path()

    try:
        if args.setup:
            interactive_setup(config_path)
            return 0

        if args.watch is None:
            return sync_once(args)

        interval = max(30, args.watch)
        print(f"[watch] Checking GitHub every {interval} seconds. Press Ctrl+C to stop.")
        while True:
            try:
                sync_once(args)
            except Exception as exc:
                print(f"[WARNING] Sync failed: {exc}", file=sys.stderr)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[stopped] GitHub watch stopped.")
        return 0
    except Exception as exc:
        print(f"\n[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
