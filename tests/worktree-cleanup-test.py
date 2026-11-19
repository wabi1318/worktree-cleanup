import contextlib
import importlib.util
import io
import json
import os
import plistlib
import subprocess
import tempfile
import unicodedata
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
        (self.repository / ".gitignore").write_text("node_modules/\n.env\n")
        self.git("add", "README.md", ".gitignore")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture", "commit", "--quiet", "-m", "fixture")
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
        (self.worktree / ".env").write_text("fixture-secret\n")
        self.assertEqual(self.reason(), "ignored-files")
        self.assertEqual(self.process(apply=True)[0]["reason"], "ignored-files")
        self.assertTrue((self.worktree / ".env").exists())

    def test_dependency_directory_can_be_removed(self):
        (self.worktree / "node_modules").mkdir()
        (self.worktree / "node_modules/dependency.js").write_text("fixture\n")
        self.assertIsNone(self.reason())
        self.assertEqual(self.process(apply=True)[0]["status"], "removed")

    def test_open_file_and_cwd_protect_worktree(self):
        self.assertEqual(self.reason({self.worktree}), "in-use")
        self.assertEqual(self.reason({self.worktree / "README.md"}), "in-use")
        self.assertIsNone(self.reason({Path(str(self.worktree) + "-other/README.md")}))

    def test_symlinked_parent_does_not_hide_an_open_worktree(self):
        storage = self.root / "storage"
        storage.mkdir()
        self.git("worktree", "move", str(self.worktree), str(storage / "worktree"))
        moved_storage = self.root / "moved-storage"
        storage.rename(moved_storage)
        storage.symlink_to(moved_storage, target_is_directory=True)
        self.entry = cleanup.list_worktrees(self.repository)[1]
        self.assertEqual(self.reason({moved_storage / "worktree/README.md"}), "in-use")

    def test_process_path_alias_does_not_hide_an_open_file(self):
        alias = self.root / "alias"
        alias.symlink_to(self.worktree, target_is_directory=True)
        output = f"p0\0n{alias / 'README.md'}\0".encode()
        with patch.object(cleanup, "command", return_value=output):
            process_paths = cleanup.open_process_paths()
        self.assertEqual(self.reason(process_paths), "in-use")

    def test_process_paths_decode_escaped_filenames(self):
        names = (
            ("日本語の作業", rb"\xe6\x97\xa5\xe6\x9c\xac\xe8\xaa\x9e\xe3\x81\xae\xe4\xbd\x9c\xe6\xa5\xad"),
            (r"path\x41", rb"path\\x41"),
            ("white\tspace\nname", rb"white\tspace\nname"),
            ("literal^A", rb"literal^A"),
        )
        for name, encoded_name in names:
            with self.subTest(name=name):
                directory = self.root / name
                directory.mkdir()
                output = b"p0\0n" + os.fsencode(self.root) + b"/" + encoded_name + b"\0"
                with patch.object(cleanup, "command", return_value=output):
                    process_paths = cleanup.open_process_paths()
                self.assertIn(directory, process_paths)

    def test_unescaped_backslash_process_path_is_preserved(self):
        for name in (r"file\x41", r"file\x00"):
            with self.subTest(name=name):
                directory = self.root / name
                directory.mkdir()
                output = b"p0\0n" + os.fsencode(directory) + b"\0"
                with patch.object(cleanup, "command", return_value=output):
                    process_paths = cleanup.open_process_paths()
                self.assertIn(directory, process_paths)

    def test_worktree_with_ambiguous_lsof_path_is_kept(self):
        worktree = self.root / "control\x01name"
        self.git("worktree", "move", str(self.worktree), str(worktree))
        self.entry = cleanup.list_worktrees(self.repository)[1]
        self.assertEqual(self.reason(), "ambiguous-worktree-path")
        self.assertTrue(worktree.exists())

    def test_case_variant_process_path_protects_worktree(self):
        process_path = Path(str(self.worktree / "README.md").swapcase())
        self.assertEqual(self.reason({process_path}), "in-use")

    def test_unicode_variant_process_path_protects_worktree(self):
        self.worktree = self.root / "が"
        self.git("worktree", "move", str(self.entry["path"]), str(self.worktree))
        self.entry = cleanup.list_worktrees(self.repository)[1]
        process_path = Path(unicodedata.normalize("NFD", str(self.worktree / "README.md")))
        self.assertEqual(self.reason({process_path}), "in-use")

    def test_repository_fsmonitor_does_not_run(self):
        marker = self.root / "monitor-called"
        monitor = self.root / "monitor"
        monitor.write_text("#!" + cleanup.sys.executable + "\nfrom pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('called')\n")
        monitor.chmod(0o700)
        self.git("config", "core.fsmonitor", str(monitor))
        cleanup.git(self.worktree, "status", "--porcelain")
        self.assertFalse(marker.exists())
        self.assertEqual(self.git("config", "--get", "core.fsmonitor").decode().strip(), str(monitor))

    def test_repository_filter_keeps_worktree_without_running(self):
        attributes = self.worktree / ".gitattributes"
        attributes.write_text("README.md filter=probe\n")
        cleanup.git(self.worktree, "add", ".gitattributes")
        marker = self.root / "filter-called"
        program = self.root / "clean-filter"
        program.write_text("#!" + cleanup.sys.executable + "\nimport sys\nfrom pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('called')\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n")
        program.chmod(0o700)
        self.git("config", "filter.probe.clean", str(program))
        (self.worktree / "README.md").write_text("changed\n")
        self.assertEqual(self.reason(), "repository-git-filters")
        self.assertFalse(marker.exists())

    def test_global_filter_definition_does_not_block_worktree(self):
        config = self.root / "global.gitconfig"
        config.write_text('[filter "fixture"]\n\tclean = cat\n')
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1"}):
            self.assertIsNone(self.reason())

    def test_submodule_is_kept_before_status_inspection(self):
        cleanup.git(self.worktree, "update-index", "--add", "--cacheinfo", "160000," + self.entry["head"] + ",external")
        original = cleanup.git
        def inspect(repository, *arguments):
            if arguments[0] == "status":
                raise AssertionError("Submodule status must not run")
            return original(repository, *arguments)
        with patch.object(cleanup, "git", side_effect=inspect):
            self.assertEqual(self.reason(), "submodule")

    def test_files_with_skipped_change_detection_are_kept(self):
        for flag, reset_flag in (("--assume-unchanged", "--no-assume-unchanged"), ("--skip-worktree", "--no-skip-worktree")):
            with self.subTest(flag=flag):
                cleanup.git(self.worktree, "update-index", flag, "README.md")
                (self.worktree / "README.md").write_text("modified fixture\n")
                try:
                    self.assertEqual(self.reason(), "assumed-or-skipped-files")
                finally:
                    (self.worktree / "README.md").write_text("fixture\n")
                    cleanup.git(self.worktree, "update-index", reset_flag, "README.md")

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

    @unittest.skipIf(os.geteuid() == 0, "requires filesystem permission checks")
    def test_unreadable_worktree_does_not_stop_other_worktrees(self):
        storage = self.root / "a-unreadable"
        storage.mkdir()
        self.git("worktree", "move", str(self.worktree), str(storage / "worktree"))
        available = self.root / "z-available"
        self.git("worktree", "add", "--quiet", "-b", "feature/available", str(available))
        previous_mode = storage.stat().st_mode & 0o777
        storage.chmod(0)
        try:
            rows = self.process()
        finally:
            storage.chmod(previous_mode)
        candidates = [row["path"] for row in rows if row["status"] == "candidate"]
        self.assertEqual(candidates, [str(available)])

    def test_worktree_inspection_error_does_not_stop_other_worktrees(self):
        self.git("worktree", "add", "--quiet", "-b", "feature/another", str(self.root / "another-worktree"))
        with patch.object(cleanup, "local_block_reason", side_effect=[PermissionError("inspection denied"), None]):
            rows = self.process()
        self.assertEqual([row["status"] for row in rows], ["blocked", "candidate"])
        self.assertEqual(rows[0]["reason"], "filesystem-unreadable")

    def test_process_path_resolution_failure_stops_before_repository_scan(self):
        config = self.root / "config.json"
        config.write_text(json.dumps({"scan_roots": [str(self.root)], "accounts": [{"path": str(self.root), "login": "fixture"}]}))
        output = io.StringIO()
        with patch.object(cleanup.sys, "argv", [str(SCRIPT), "--config", str(config)]), patch.object(cleanup.Path, "home", return_value=self.root), patch.object(cleanup, "open_process_paths", side_effect=RuntimeError("path resolution failed")), patch.object(cleanup, "discover_repositories") as discover, contextlib.redirect_stdout(output):
            result = cleanup.main()
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1])["status"], "error")
        discover.assert_not_called()

    def test_discovery_deduplicates_worktrees(self):
        self.assertEqual(len(list(cleanup.discover_repositories([self.root]))), 1)

    def test_removal_after_discovery_does_not_report_false_walk_errors(self):
        config = self.root / "config.json"
        config.write_text(json.dumps({"scan_roots": [str(self.root)], "accounts": [{"path": str(self.root), "login": "fixture"}]}))
        original_walk = os.walk
        def ordered_walk(*arguments, **options):
            for directory, directories, files in original_walk(*arguments, **options):
                directories.sort()
                yield directory, directories, files
        def remove_fixture(*arguments):
            self.git("worktree", "remove", str(self.worktree))
        output = io.StringIO()
        with patch.object(cleanup.sys, "argv", [str(SCRIPT), "--config", str(config), "--apply"]), patch.object(cleanup.Path, "home", return_value=self.root), patch.object(cleanup, "open_process_paths", return_value=set()), patch.object(cleanup.os, "walk", side_effect=ordered_walk), patch.object(cleanup, "process_repository", side_effect=remove_fixture), contextlib.redirect_stdout(output):
            result = cleanup.main()
        self.assertEqual(result, 0)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertNotIn("discovery-error", [row["status"] for row in rows])

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
        for remote in ("https://利用者@github.com/owner/repository.git", "https://example.invalid/owner/repository.git"):
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

    def test_installer_rejects_missing_commands(self):
        with patch.object(cleanup.Path, "home", return_value=self.root), patch.object(cleanup.shutil, "which", return_value=None), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(cleanup.Blocked, "missing-required-tool"):
                cleanup.install_launch_agent(self.root / "config.json", False)
        self.assertFalse((self.root / "Library/LaunchAgents/local.worktree-cleanup.plist").exists())

    def test_installer_records_absolute_command_paths(self):
        with patch.object(cleanup.Path, "home", return_value=self.root), patch.object(cleanup.shutil, "which", side_effect=lambda name: f"commands/{name}"), contextlib.redirect_stdout(io.StringIO()):
            cleanup.install_launch_agent(self.root / "config.json", False)
        agent = plistlib.loads((self.root / "Library/LaunchAgents/local.worktree-cleanup.plist").read_bytes())
        self.assertTrue(all(Path(value).is_absolute() for value in agent["EnvironmentVariables"]["PATH"].split(":")))
        self.assertEqual(agent["StartCalendarInterval"], {"Hour": 10, "Minute": 15})
        self.assertFalse(agent["RunAtLoad"])
        self.assertNotIn("--apply", agent["ProgramArguments"])


if __name__ == "__main__":
    unittest.main()
