from __future__ import annotations

import json
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import sync_github_for_gemini as sync

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "sync_github_for_gemini.py"


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)
    return proc.stdout.strip()


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.remote = self.root / "remote.git"
        self.mirror = self.root / "mirror clone"
        self.output = self.root / "Gemini upload output"
        self.gemini_output = self.root / "Gemini upload output-Gemini"
        self.gemini_start = self.root / "Gemini upload output-Gemini-Start"
        self.drive_workspace = self.root / "Google Drive" / "Pharmacy AI Workspace"
        self.config = self.root / "profiles config.json"

        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "main", str(self.work)], check=True, capture_output=True)
        git(self.work, "config", "user.email", "test@example.invalid")
        git(self.work, "config", "user.name", "Test User")
        (self.work / "src").mkdir()
        (self.work / "src" / "app.py").write_text("print('v1')\n", encoding="utf-8")
        (self.work / "README.md").write_text("repo docs\n", encoding="utf-8")
        git(self.work, "add", ".")
        git(self.work, "commit", "-m", "initial")
        git(self.work, "remote", "add", "origin", str(self.remote))
        git(self.work, "push", "-u", "origin", "main")

        self.profile = "test-repo"
        self.write_profiles(
            {
                self.profile: {
                    "source_repo": str(self.remote),
                    "branch": "main",
                    "output_dir": str(self.output),
                    "mirror_dir": str(self.mirror),
                }
            }
        )

    def write_profiles(self, profiles: dict) -> None:
        self.config.write_text(json.dumps({"version": 2, "profiles": profiles}), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_sync(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--config", str(self.config), "--profile", self.profile, *extra],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_first_sync_copies_only_tracked_files(self) -> None:
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.output / "src" / "app.py").read_text(encoding="utf-8"), "print('v1')\n")
        self.assertTrue((self.output / "README.md").exists())
        self.assertFalse((self.output / ".git").exists())

    def test_first_sync_creates_gemini_text_bundles(self) -> None:
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.gemini_output.is_dir())
        index = (self.gemini_output / "00_REPOSITORY_INDEX.txt").read_text(encoding="utf-8")
        self.assertIn("src/app.py", index)
        self.assertIn("README.md", index)
        bundle_files = sorted(self.gemini_output.glob("*.txt"))
        self.assertGreaterEqual(len(bundle_files), 3)
        combined = "\n".join(path.read_text(encoding="utf-8") for path in bundle_files)
        self.assertIn("FILE: src/app.py", combined)
        self.assertIn("print('v1')", combined)
        self.assertIn("FILE: README.md", combined)
        self.assertTrue(all(path.suffix == ".txt" for path in self.gemini_output.iterdir()))

    def test_first_sync_creates_small_starter_folder(self) -> None:
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.gemini_start.is_dir())
        names = sorted(path.name for path in self.gemini_start.iterdir())
        self.assertEqual(names, [
            "00_START_HERE.txt",
            "01_REPOSITORY_INDEX.txt",
            "02_CORE_CONTEXT.txt",
        ])
        guide = (self.gemini_start / "00_START_HERE.txt").read_text(encoding="utf-8")
        self.assertIn("Do NOT ask the user to upload the whole repository ZIP", guide)
        self.assertIn("upload only those bundle file(s)", guide)
        index = (self.gemini_start / "01_REPOSITORY_INDEX.txt").read_text(encoding="utf-8")
        self.assertIn("src/app.py", index)
        core = (self.gemini_start / "02_CORE_CONTEXT.txt").read_text(encoding="utf-8")
        self.assertIn("FILE: README.md", core)
        self.assertNotIn("FILE: src/app.py", core)

    def test_bundle_index_warns_against_full_folder_upload(self) -> None:
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        index = (self.gemini_output / "00_REPOSITORY_INDEX.txt").read_text(encoding="utf-8")
        self.assertIn("DO NOT ZIP OR UPLOAD THIS ENTIRE FOLDER AT ONCE", index)
        self.assertIn("-Gemini-Start", index)

    def test_gemini_bundles_exclude_logs_and_binary_but_exact_mirror_keeps_them(self) -> None:
        (self.work / "logs").mkdir()
        (self.work / "logs" / "runtime.json").write_text('{"large": "runtime evidence"}\n', encoding="utf-8")
        (self.work / "assets").mkdir()
        (self.work / "assets" / "image.bin").write_bytes(b"abc\x00def")
        (self.work / "src" / "helper.py").write_text("VALUE = 42\n", encoding="utf-8")
        git(self.work, "add", ".")
        git(self.work, "commit", "-m", "bundle filtering fixtures")
        git(self.work, "push", "origin", "main")

        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.output / "logs" / "runtime.json").exists())
        self.assertTrue((self.output / "assets" / "image.bin").exists())

        index = (self.gemini_output / "00_REPOSITORY_INDEX.txt").read_text(encoding="utf-8")
        self.assertIn("excluded directory: logs\tlogs/runtime.json", index)
        self.assertIn("binary or non-text encoding\tassets/image.bin", index)
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in self.gemini_output.glob("*.txt")
            if path.name != "00_REPOSITORY_INDEX.txt"
        )
        self.assertIn("FILE: src/helper.py", combined)
        self.assertIn("VALUE = 42", combined)
        self.assertNotIn("runtime evidence", combined)
        self.assertNotIn("abc\x00def", combined)

    def test_explicit_empty_exclusion_list_can_include_logs(self) -> None:
        (self.work / "logs").mkdir()
        (self.work / "logs" / "useful.txt").write_text("include me\n", encoding="utf-8")
        git(self.work, "add", ".")
        git(self.work, "commit", "-m", "tracked useful log")
        git(self.work, "push", "origin", "main")
        profiles = json.loads(self.config.read_text(encoding="utf-8"))["profiles"]
        profiles[self.profile]["gemini_excluded_dirs"] = []
        self.write_profiles(profiles)
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in self.gemini_output.glob("*.txt")
        )
        self.assertIn("FILE: logs/useful.txt", combined)
        self.assertIn("include me", combined)

    def test_nested_gemini_output_is_rejected(self) -> None:
        profiles = json.loads(self.config.read_text(encoding="utf-8"))["profiles"]
        profiles[self.profile]["gemini_output_dir"] = str(self.output / "nested-gemini")
        self.write_profiles(profiles)
        result = self.run_sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot be nested", result.stderr)

    def test_missing_gemini_bundle_folder_is_rebuilt_without_new_commit(self) -> None:
        first = self.run_sync()
        self.assertEqual(first.returncode, 0, first.stderr)
        shutil.rmtree(self.gemini_output)
        second = self.run_sync()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue((self.gemini_output / "00_REPOSITORY_INDEX.txt").exists())
        self.assertNotIn("[up to date]", second.stdout)

    def test_missing_starter_folder_is_rebuilt_without_new_commit(self) -> None:
        first = self.run_sync()
        self.assertEqual(first.returncode, 0, first.stderr)
        shutil.rmtree(self.gemini_start)
        second = self.run_sync()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue((self.gemini_start / "00_START_HERE.txt").exists())
        self.assertNotIn("[up to date]", second.stdout)

    def test_bundle_generation_splits_large_categories_and_preserves_file_paths(self) -> None:
        self.assertEqual(self.run_sync().returncode, 0)
        bundle_output = self.root / "small-bundles"
        head = git(self.mirror, "rev-parse", "HEAD")
        stats = sync.materialize_gemini_bundles(
            self.mirror,
            bundle_output,
            head,
            target_bytes=180,
        )
        self.assertGreater(stats["bundle_files"], 1)
        index = (bundle_output / "00_REPOSITORY_INDEX.txt").read_text(encoding="utf-8")
        self.assertIn("src/app.py", index)
        self.assertIn("README.md", index)
        self.assertTrue(all(path.suffix == ".txt" for path in bundle_output.iterdir()))

    def enable_drive_for_profile(self) -> None:
        self.drive_workspace.mkdir(parents=True, exist_ok=True)
        profiles = json.loads(self.config.read_text(encoding="utf-8"))["profiles"]
        profiles[self.profile]["drive_workspace_dir"] = str(self.drive_workspace)
        self.write_profiles(profiles)

    def test_drive_is_optional_and_local_outputs_still_exist(self) -> None:
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.output.is_dir())
        self.assertTrue(self.gemini_output.is_dir())
        self.assertTrue(self.gemini_start.is_dir())
        self.assertFalse((self.drive_workspace / sync.DRIVE_START_FOLDER_NAME).exists())
        self.assertFalse((self.drive_workspace / sync.DRIVE_BUNDLES_FOLDER_NAME).exists())

    def test_drive_workspace_receives_second_copy_without_replacing_local_outputs(self) -> None:
        self.enable_drive_for_profile()
        shared_notes = self.drive_workspace / "Shared Notes"
        shared_notes.mkdir()
        (shared_notes / "keep.txt").write_text("do not touch\n", encoding="utf-8")

        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)

        drive_start = self.drive_workspace / sync.DRIVE_START_FOLDER_NAME
        drive_bundles = self.drive_workspace / sync.DRIVE_BUNDLES_FOLDER_NAME
        self.assertTrue((drive_start / "00_START_HERE.txt").is_file())
        self.assertTrue((drive_bundles / "00_REPOSITORY_INDEX.txt").is_file())
        self.assertEqual(
            (drive_start / "00_START_HERE.txt").read_text(encoding="utf-8"),
            (self.gemini_start / "00_START_HERE.txt").read_text(encoding="utf-8"),
        )
        self.assertTrue((self.output / "src" / "app.py").is_file())
        self.assertTrue((self.gemini_output / "00_REPOSITORY_INDEX.txt").is_file())
        self.assertEqual((shared_notes / "keep.txt").read_text(encoding="utf-8"), "do not touch\n")

    def test_drive_missing_file_is_repaired_on_unchanged_commit(self) -> None:
        self.enable_drive_for_profile()
        first = self.run_sync()
        self.assertEqual(first.returncode, 0, first.stderr)
        drive_start = self.drive_workspace / sync.DRIVE_START_FOLDER_NAME
        missing = drive_start / "00_START_HERE.txt"
        missing.unlink()

        second = self.run_sync()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(missing.is_file())
        self.assertNotIn("[up to date]", second.stdout)

    def test_drive_stale_generated_file_is_removed_but_workspace_siblings_survive(self) -> None:
        self.enable_drive_for_profile()
        first = self.run_sync()
        self.assertEqual(first.returncode, 0, first.stderr)
        drive_bundles = self.drive_workspace / sync.DRIVE_BUNDLES_FOLDER_NAME
        stale = drive_bundles / "old-generated.txt"
        stale.write_text("stale\n", encoding="utf-8")
        notes = self.drive_workspace / "Shared Notes"
        notes.mkdir()
        note = notes / "human-note.txt"
        note.write_text("preserve\n", encoding="utf-8")

        second = self.run_sync("--force")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertFalse(stale.exists())
        self.assertEqual(note.read_text(encoding="utf-8"), "preserve\n")

    def test_configure_drive_workspace_saves_path_refreshes_runtime_and_syncs(self) -> None:
        self.drive_workspace.mkdir(parents=True)
        appdata = self.root / "appdata"
        runtime = appdata / "runtime" / "sync_github_for_gemini.py"
        with mock.patch.object(sync, "local_app_dir", return_value=appdata), mock.patch.object(
            sync, "install_runtime_copy", return_value=runtime
        ) as install_runtime:
            pid = sync.configure_drive_workspace(
                self.config,
                str(self.drive_workspace),
                self.profile,
            )
        self.assertEqual(pid, self.profile)
        install_runtime.assert_called_once()
        profiles = json.loads(self.config.read_text(encoding="utf-8"))["profiles"]
        self.assertEqual(
            Path(profiles[self.profile]["drive_workspace_dir"]),
            self.drive_workspace.resolve(),
        )
        self.assertTrue((self.drive_workspace / sync.DRIVE_START_FOLDER_NAME / "00_START_HERE.txt").is_file())
        self.assertTrue((self.drive_workspace / sync.DRIVE_BUNDLES_FOLDER_NAME / "00_REPOSITORY_INDEX.txt").is_file())
        self.assertTrue(self.gemini_start.is_dir())
        self.assertTrue(self.gemini_output.is_dir())

    def test_drive_workspace_cannot_be_nested_inside_local_generated_outputs(self) -> None:
        profiles = json.loads(self.config.read_text(encoding="utf-8"))["profiles"]
        profiles[self.profile]["drive_workspace_dir"] = str(self.gemini_output / "Drive Workspace")
        self.write_profiles(profiles)
        result = self.run_sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot be nested", result.stderr)

    def test_nested_unicode_and_spaces_are_exported(self) -> None:
        (self.work / "資料 folder").mkdir()
        (self.work / "資料 folder" / "薬局 notes.txt").write_text("日本語 café 🧪\n", encoding="utf-8")
        git(self.work, "add", ".")
        git(self.work, "commit", "-m", "unicode file")
        git(self.work, "push", "origin", "main")
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.output / "資料 folder" / "薬局 notes.txt").read_text(encoding="utf-8"), "日本語 café 🧪\n")

    def test_renamed_file_is_reflected(self) -> None:
        self.assertEqual(self.run_sync().returncode, 0)
        (self.work / "README.md").rename(self.work / "renamed README.md")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-m", "rename")
        git(self.work, "push", "origin", "main")
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.output / "README.md").exists())
        self.assertTrue((self.output / "renamed README.md").exists())

    def test_failed_copy_preserves_previous_output(self) -> None:
        self.output.mkdir()
        (self.output / "known-good.txt").write_text("keep me", encoding="utf-8")
        self.mirror.mkdir()
        (self.mirror / "README.md").write_text("new", encoding="utf-8")
        with mock.patch.object(sync, "tracked_files", return_value=["README.md"]), mock.patch.object(
            sync.shutil, "copy2", side_effect=OSError("simulated locked destination")
        ):
            with self.assertRaises(OSError):
                sync.materialize_tracked_files(self.mirror, self.output)
        self.assertEqual((self.output / "known-good.txt").read_text(encoding="utf-8"), "keep me")
        self.assertFalse((self.output / "README.md").exists())

    def test_failed_promotion_restores_previous_output(self) -> None:
        self.output.mkdir()
        (self.output / "known-good.txt").write_text("keep me", encoding="utf-8")
        self.mirror.mkdir()
        (self.mirror / "README.md").write_text("new", encoding="utf-8")
        original_rename = Path.rename

        def fail_staging_rename(path: Path, target: Path) -> Path:
            if ".staging-" in path.name:
                raise PermissionError("simulated Drive lock")
            return original_rename(path, target)

        with mock.patch.object(sync, "tracked_files", return_value=["README.md"]), mock.patch.object(
            Path, "rename", fail_staging_rename
        ):
            with self.assertRaises(PermissionError):
                sync.materialize_tracked_files(self.mirror, self.output)
        self.assertEqual((self.output / "known-good.txt").read_text(encoding="utf-8"), "keep me")

    def test_interrupted_swap_recovers_previous_output(self) -> None:
        self.mirror.mkdir()
        (self.mirror / "README.md").write_text("fresh", encoding="utf-8")
        backup = self.root / f".{self.output.name}.previous-interrupted"
        backup.mkdir()
        (backup / "known-good.txt").write_text("recover me", encoding="utf-8")
        with mock.patch.object(sync, "tracked_files", return_value=["README.md"]):
            count, _ = sync.materialize_tracked_files(self.mirror, self.output)
        self.assertEqual(count, 1)
        self.assertEqual((self.output / "README.md").read_text(encoding="utf-8"), "fresh")
        self.assertFalse((self.output / "known-good.txt").exists())
        self.assertFalse(backup.exists())

    def test_unchanged_sync_retries_stale_backup_cleanup(self) -> None:
        self.assertEqual(self.run_sync().returncode, 0)
        stale = self.output.parent / f".{self.output.name}.previous-leftover"
        stale.mkdir()
        (stale / "old.txt").write_text("old", encoding="utf-8")
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[up to date]", result.stdout)
        self.assertFalse(stale.exists())

    def test_stale_lock_is_recovered(self) -> None:
        lock = self.root / "locks" / "repo.lock"
        lock.parent.mkdir()
        lock.write_text("stale", encoding="utf-8")
        old = time.time() - 3600
        os.utime(lock, (old, old))
        with sync.sync_lock(lock):
            self.assertTrue(lock.exists())
        self.assertFalse(lock.exists())

    def test_recent_lock_blocks_simultaneous_run(self) -> None:
        lock = self.root / "locks" / "repo.lock"
        lock.parent.mkdir()
        lock.write_text("active", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "already being synchronized"):
            with sync.sync_lock(lock):
                pass

    def test_legacy_single_profile_config_migrates(self) -> None:
        legacy = self.root / "config.json"
        legacy.write_text(json.dumps({
            "source_repo": str(self.remote), "branch": "main", "output_dir": str(self.output),
        }), encoding="utf-8")
        with mock.patch.object(sync, "local_app_dir", return_value=self.root):
            profiles = sync.load_profiles(self.root / "profiles.json")
        self.assertEqual(len(profiles), 1)
        profile = next(iter(profiles.values()))
        self.assertEqual(profile["source_repo"], str(self.remote))
        self.assertEqual(profile["output_dir"], str(self.output))

    def test_background_git_disables_prompts_and_sets_no_window(self) -> None:
        fake = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="ok", stderr="")
        with mock.patch.object(sync, "BACKGROUND_MODE", True), mock.patch.object(
            sync.subprocess, "run", return_value=fake
        ) as run:
            self.assertEqual(sync.run_git(["status"]), "ok")
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(kwargs["env"]["GCM_INTERACTIVE"], "never")
        self.assertEqual(kwargs["creationflags"], int(getattr(subprocess, "CREATE_NO_WINDOW", 0)))
        self.assertEqual(kwargs["timeout"], sync.BACKGROUND_GIT_TIMEOUT_SECONDS)

    def test_background_git_timeout_has_retry_message(self) -> None:
        timeout = subprocess.TimeoutExpired(cmd="git fetch", timeout=90)
        with mock.patch.object(sync, "BACKGROUND_MODE", True), mock.patch.object(
            sync.subprocess, "run", side_effect=timeout
        ):
            with self.assertRaisesRegex(RuntimeError, "will retry on its next scheduled run"):
                sync.run_git(["fetch"])

    def test_git_diagnostics_redact_embedded_url_credentials(self) -> None:
        fake = subprocess.CompletedProcess(
            args=["git", "clone"], returncode=128, stdout="",
            stderr="fatal: Authentication failed for https://user:test-secret-not-real@github.com/org/repo.git",
        )
        with mock.patch.object(sync.subprocess, "run", return_value=fake):
            with self.assertRaises(RuntimeError) as caught:
                sync.run_git(["clone", "https://user:test-secret-not-real@github.com/org/repo.git"])
        self.assertNotIn("test-secret-not-real", str(caught.exception))
        self.assertIn("[credentials]@github.com", str(caught.exception))

    def test_clone_progress_redacts_embedded_url_credentials(self) -> None:
        console = io.StringIO()
        source = "https://user:test-secret-not-real@github.com/org/repo.git"
        with mock.patch.object(sync, "run_git", side_effect=RuntimeError("stop")), redirect_stdout(console):
            with self.assertRaises(RuntimeError):
                sync.ensure_mirror(source, "main", self.root / "fresh mirror")
        self.assertNotIn("test-secret-not-real", console.getvalue())
        self.assertIn("[credentials]@github.com", console.getvalue())

    def test_unavailable_remote_preserves_previous_output_and_reports_failure(self) -> None:
        self.assertEqual(self.run_sync().returncode, 0)
        (self.output / "local marker.txt").write_text("preserve", encoding="utf-8")
        # Keep the configured/origin URL unchanged so this exercises a real
        # fetch failure rather than only the mirror-URL mismatch guard.
        self.remote.rename(self.root / "remote-offline.git")
        result = self.run_sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Git command failed (fetch)", result.stderr)
        self.assertEqual((self.output / "src" / "app.py").read_text(encoding="utf-8"), "print('v1')\n")
        self.assertEqual((self.output / "local marker.txt").read_text(encoding="utf-8"), "preserve")

    def test_large_tracked_file_is_reported(self) -> None:
        profile = json.loads(self.config.read_text(encoding="utf-8"))["profiles"][self.profile]
        console = io.StringIO()
        with mock.patch.object(sync, "MAX_UPLOAD_FILE_BYTES", 1), mock.patch.object(
            sync, "local_app_dir", return_value=self.root / "appdata"
        ), redirect_stdout(console):
            result = sync.sync_profile(self.profile, profile, force=True)
        self.assertEqual(result, 0)
        self.assertIn("Files over 100 MB", console.getvalue())

    def test_windows_task_action_is_short_quoted_and_unicode_safe(self) -> None:
        app = self.root / "本地 应用資料"
        launcher = app / "runtime" / 'launch-profile.vbs'
        with mock.patch.object(sync, "local_app_dir", return_value=app):
            launcher = sync.write_hidden_launcher("profile", self.root / "配置 文件.json", self.root / "pythonw.exe", self.root / "runtime.py")
        content = launcher.read_text(encoding="utf-16")
        self.assertIn('""', content)
        self.assertIn("--background", content)
        action = sync.build_task_action(launcher)
        args = sync.build_task_registration_args(sync.task_name_for("profile"), action)
        self.assertEqual(args[args.index("/SC") + 1], "MINUTE")
        self.assertEqual(args[args.index("/MO") + 1], "2")
        self.assertLess(len(action), 260)
        self.assertIn('"', action)

    def test_windows_task_action_rejects_overlong_command(self) -> None:
        action = "x" * (sync.MAX_SCHTASKS_ACTION_CHARS + 1)
        with self.assertRaisesRegex(RuntimeError, "path is too long"):
            sync.validate_task_action(action)

    def test_update_add_modify_delete(self) -> None:
        self.assertEqual(self.run_sync().returncode, 0)
        (self.work / "src" / "app.py").write_text("print('v2')\n", encoding="utf-8")
        (self.work / "README.md").unlink()
        (self.work / "docs").mkdir()
        (self.work / "docs" / "handoff.md").write_text("continue here\n", encoding="utf-8")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-m", "second")
        git(self.work, "push", "origin", "main")

        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.output / "src" / "app.py").read_text(encoding="utf-8"), "print('v2')\n")
        self.assertFalse((self.output / "README.md").exists())
        self.assertEqual((self.output / "docs" / "handoff.md").read_text(encoding="utf-8"), "continue here\n")

    def test_untracked_mirror_file_is_never_exported(self) -> None:
        self.assertEqual(self.run_sync().returncode, 0)
        (self.mirror / "local-secret.txt").write_text("not tracked\n", encoding="utf-8")
        result = self.run_sync("--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.output / "local-secret.txt").exists())

    def test_unchanged_commit_skips_rebuild(self) -> None:
        self.assertEqual(self.run_sync().returncode, 0)
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[up to date]", result.stdout)

    def test_multiple_profiles_are_independent(self) -> None:
        second_mirror = self.root / "mirror2"
        second_output = self.root / "output2"
        profiles = json.loads(self.config.read_text(encoding="utf-8"))["profiles"]
        profiles["second"] = {
            "source_repo": str(self.remote),
            "branch": "main",
            "output_dir": str(second_output),
            "mirror_dir": str(second_mirror),
        }
        self.write_profiles(profiles)
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--config", str(self.config), "--all"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.output / "src" / "app.py").exists())
        self.assertTrue((second_output / "src" / "app.py").exists())
        self.assertTrue((self.gemini_output / "00_REPOSITORY_INDEX.txt").exists())
        self.assertTrue((self.gemini_start / "00_START_HERE.txt").exists())
        self.assertTrue((second_output.with_name(second_output.name + "-Gemini") / "00_REPOSITORY_INDEX.txt").exists())
        self.assertTrue((second_output.with_name(second_output.name + "-Gemini-Start") / "00_START_HERE.txt").exists())

    def test_profile_id_is_stable(self) -> None:
        probe = subprocess.run(
            [sys.executable, "-c", (
                "import importlib.util; "
                f"s=importlib.util.spec_from_file_location('m', r'{SCRIPT}'); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                "print(m.profile_id_for('https://github.com/A/B.git','main'))"
            )],
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(probe.stdout.strip(), "B-dec055c3")


if __name__ == "__main__":
    unittest.main()
