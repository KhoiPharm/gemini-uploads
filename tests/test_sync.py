from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
        self.mirror = self.root / "mirror"
        self.output = self.root / "output"
        self.config = self.root / "config.json"

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

        self.config.write_text(
            json.dumps(
                {
                    "source_repo": str(self.remote),
                    "branch": "main",
                    "output_dir": str(self.output),
                    "mirror_dir": str(self.mirror),
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_sync(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--config", str(self.config), *extra],
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


if __name__ == "__main__":
    unittest.main()
