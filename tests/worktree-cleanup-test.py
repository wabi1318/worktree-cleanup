import contextlib
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/worktree-cleanup.py"
SPEC = importlib.util.spec_from_file_location("worktree_cleanup", SCRIPT)
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)


class WorktreeCleanupTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repository = self.root / "repository"
        self.worktree = self.root / "作業 worktree"
        self.repository.mkdir()
        self.git("init", "--quiet", "-b", "main")
        (self.repository / "README.md").write_text("fixture\n")
        (self.repository / ".gitignore").write_text("node_modules/\n.env.local\n")
        self.git("add", "README.md", ".gitignore")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "fixture")
        self.git("worktree", "add", "--quiet", "-b", "feature/cleanup", str(self.worktree))
        self.entry = cleanup.list_worktrees(self.repository)[1]

    def git(self, *arguments):
        return subprocess.run(["git", "-C", str(self.repository), *arguments], check=True, capture_output=True).stdout

    def reason(self, paths=None):
        return cleanup.local_block_reason(self.repository, self.entry, paths or set())

    def process(self, apply=False, merged=True):
        github = unittest.mock.Mock()
        github.merged_pull_request.return_value = "https://github.com/example/repository/pull/1" if merged else None
        output = io.StringIO()
        with patch.object(cleanup, "GitHub", return_value=github), patch.object(cleanup, "open_process_paths", return_value=set()), contextlib.redirect_stdout(output):
            cleanup.process_repository(self.repository, [(self.root, "fixture")], apply, set())
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_dry_run_keeps_candidate_and_primary(self):
        self.assertEqual(self.process()[0]["status"], "candidate")
        self.assertTrue(self.worktree.exists())
        self.assertTrue(self.repository.exists())

    def test_apply_removes_worktree_but_keeps_branch_and_primary(self):
        self.assertEqual(self.process(apply=True)[0]["status"], "removed")
        self.assertFalse(self.worktree.exists())
        self.assertTrue(self.repository.exists())
        self.assertTrue(self.git("rev-parse", "--verify", "refs/heads/feature/cleanup"))
        self.assertEqual(len(cleanup.list_worktrees(self.repository)), 1)

    def test_unmerged_is_kept(self):
        self.assertEqual(self.process(apply=True, merged=False)[0]["reason"], "no-merged-pr-at-head")
        self.assertTrue(self.worktree.exists())

    def test_changes_untracked_and_staged_are_kept(self):
        (self.worktree / "README.md").write_text("changed\n")
        self.assertEqual(self.reason(), "uncommitted-or-untracked")
        (self.worktree / "README.md").write_text("fixture\n")
        (self.worktree / "untracked.txt").write_text("keep\n")
        self.assertEqual(self.reason(), "uncommitted-or-untracked")
        cleanup.git(self.worktree, "add", "untracked.txt")
        self.assertEqual(self.reason(), "uncommitted-or-untracked")

    def test_ignored_secret_is_kept_without_reading_contents(self):
        (self.worktree / ".env.local").write_text("fixture-secret\n")
        self.assertEqual(self.reason(), "ignored-files")
        self.assertEqual(self.process(apply=True)[0]["reason"], "ignored-files")
        self.assertTrue((self.worktree / ".env.local").exists())

    def test_dependency_directory_can_be_removed(self):
        (self.worktree / "node_modules").mkdir()
        (self.worktree / "node_modules/dependency.js").write_text("fixture\n")
        self.assertIsNone(self.reason())
        self.assertEqual(self.process(apply=True)[0]["status"], "removed")

    def test_open_file_and_cwd_protect_worktree(self):
        self.assertEqual(self.reason({self.worktree}), "in-use")
        self.assertEqual(self.reason({self.worktree / "README.md"}), "in-use")
        self.assertIsNone(self.reason({Path(str(self.worktree) + "-other/README.md")}))

    def test_locked_worktree_is_kept(self):
        self.git("worktree", "lock", str(self.worktree))
        self.assertEqual(self.process(apply=True)[0]["reason"], "locked-or-prunable")

    def test_head_change_during_inspection_is_kept(self):
        self.entry["head"] = "0" * 40
        self.assertEqual(self.reason(), "head-changed")

    def test_git_operation_is_kept(self):
        operation = Path(cleanup.git(self.worktree, "rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD").decode().strip())
        operation.write_text(self.entry["head"] + "\n")
        self.assertEqual(self.reason(), "git-operation-in-progress")

    def test_recheck_prevents_deletion_when_a_file_opens(self):
        github = unittest.mock.Mock()
        github.merged_pull_request.return_value = "https://github.com/example/repository/pull/1"
        with patch.object(cleanup, "GitHub", return_value=github), patch.object(cleanup, "open_process_paths", return_value={self.worktree}), contextlib.redirect_stdout(io.StringIO()):
            cleanup.process_repository(self.repository, [(self.root, "fixture")], True, set())
        self.assertTrue(self.worktree.exists())

    def test_discovery_deduplicates_worktrees(self):
        self.assertEqual(len(list(cleanup.discover_repositories([self.root]))), 1)

    def test_discovery_ignores_package_cache_without_hiding_worktrees(self):
        cache = self.root / ".cache/uv/package"
        cache.mkdir(parents=True)
        (cache / ".git").write_text("not a development repository\n")
        hidden_worktree = self.root / ".cache/worktrees/hidden"
        self.git("worktree", "add", "--quiet", "-b", "feature/hidden", str(hidden_worktree))
        with patch.object(cleanup.Path, "home", return_value=self.root), patch.object(cleanup, "report") as report:
            self.assertEqual(len(list(cleanup.discover_repositories([self.root]))), 1)
            report.assert_not_called()
        self.assertIn(hidden_worktree, [entry["path"] for entry in cleanup.list_worktrees(self.repository)])

    def test_auth_mismatch_blocks_before_reading_prs(self):
        with patch.object(cleanup.GitHub, "run", side_effect=[b"", b"another-account\n"]):
            with self.assertRaisesRegex(cleanup.Blocked, "account-mismatch"):
                cleanup.GitHub(self.repository, "fixture")

    def test_longest_account_scope_wins(self):
        self.assertEqual(cleanup.expected_account(self.repository, [(self.root, "personal"), (self.repository, "work")]), "work")

    def test_remote_url_credentials_and_non_github_are_rejected(self):
        for remote in ("https://token@github.com/owner/repository.git", "https://example.invalid/owner/repository.git"):
            with patch.object(cleanup, "git", return_value=remote.encode()):
                with self.assertRaisesRegex(cleanup.Blocked, "unsupported-origin"):
                    cleanup.github_repository(self.repository)

    def test_merged_head_must_match_and_reused_open_branch_is_kept(self):
        github = object.__new__(cleanup.GitHub)
        github.origin = "owner/repository"
        github.targets = [github.origin]
        pr = {"state": "closed", "merged_at": "2026-11-17T00:00:00Z", "html_url": "https://github.com/owner/repository/pull/1", "head": {"ref": "feature/cleanup", "sha": self.entry["head"], "repo": github.origin}}
        with patch.object(github, "run", return_value=json.dumps(pr).encode()):
            self.assertEqual(github.merged_pull_request(self.entry), pr["html_url"])
        with patch.object(github, "run", return_value=json.dumps({**pr, "head": {**pr["head"], "sha": "0" * 40}}).encode()):
            self.assertIsNone(github.merged_pull_request(self.entry))
        with patch.object(github, "run", return_value=(json.dumps(pr) + "\n" + json.dumps({**pr, "state": "open"})).encode()):
            self.assertIsNone(github.merged_pull_request(self.entry))

    def test_fork_pr_matches_origin_head_in_parent_repository(self):
        github = object.__new__(cleanup.GitHub)
        github.origin = "fork/repository"
        github.targets = [github.origin, "upstream/repository"]
        pr = {"state": "closed", "merged_at": "2026-11-17T00:00:00Z", "html_url": "https://github.com/upstream/repository/pull/1", "head": {"ref": "feature/cleanup", "sha": self.entry["head"], "repo": github.origin}}
        with patch.object(github, "run", side_effect=[b"", json.dumps(pr).encode()]):
            self.assertEqual(github.merged_pull_request(self.entry), pr["html_url"])

    def test_partial_worktree_output_is_rejected(self):
        with self.assertRaisesRegex(cleanup.Blocked, "invalid-worktree-list"):
            cleanup.parse_worktrees(b"worktree /tmp/incomplete\0")

    def test_external_command_errors_do_not_print_secret_values(self):
        error = subprocess.CalledProcessError(1, ["direnv"], stderr=b"fixture-secret")
        with patch.object(cleanup.subprocess, "run", side_effect=error):
            with self.assertRaisesRegex(cleanup.Blocked, "^direnv-failed$"):
                cleanup.command(["direnv"])


if __name__ == "__main__":
    unittest.main()
