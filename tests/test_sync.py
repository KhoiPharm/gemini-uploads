from __future__ import annotations

import json
import io
import os
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

    def test_unavailable_remote_preserves_previous_output_and_reports_failure(self) -> None:
        self.assertEqual(self.run_sync().returncode, 0)
        (self.output / "local marker.txt").write_text("preserve", encoding="utf-8")
        git(self.mirror, "remote", "set-url", "origin", str(self.root / "missing remote.git"))
        result = self.run_sync()
        self.assertNotEqual(result.returncode, 0)
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
