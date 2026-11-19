#!/usr/bin/env python3
"""Clean up merged GitHub PR worktrees. Dry run by default.

Config: ~/.config/worktree-cleanup.json
Usage: python3 scripts/worktree-cleanup.py [--apply] [--install]
"""

import argparse
import codecs
import fcntl
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode


LABEL = "local.worktree-cleanup"
DAILY_RUN_HOUR = 10
DAILY_RUN_MINUTE = 15
COMMAND_TIMEOUT_SECONDS = 60
SCAN_EXCLUSIONS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".Trash",
    ".ssh", "ssh", ".aws", ".gnupg", ".npm", ".pnpm-store",
    ".next", ".turbo", "Library",
}
SHARED_BRANCHES = {"main", "master", "develop", "staging", "production", "trunk"}
CONFIG_PATH = Path.home() / ".config/worktree-cleanup.json"
PR_FIELDS = ".[] | {state, merged_at, html_url, head: {ref: .head.ref, sha: .head.sha, repo: .head.repo.full_name}} | @json"


class Blocked(Exception):
    pass


def command(arguments, cwd=None):
    try:
        return subprocess.run(
            arguments, cwd=cwd, check=True, capture_output=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"},
        ).stdout
    except (subprocess.SubprocessError, OSError) as error:
        # External command output may contain environment values; keep it out of logs.
        raise Blocked(f"{Path(arguments[0]).name}-failed") from error


def git(repository, *arguments):
    return command(["git", "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(repository), *arguments])


def normalize_path_for_comparison(path):
    return Path(unicodedata.normalize("NFD", str(path)).casefold())


def has_repository_git_filters(repository):
    output = git(repository, "config", "--null", "--show-scope", "--name-only", "--list")
    if not output.endswith(b"\0"):
        raise Blocked("invalid-git-configuration")
    fields = output[:-1].split(b"\0")
    if len(fields) % 2:
        raise Blocked("invalid-git-configuration")
    return any(
        scope in {b"local", b"worktree"} and key.lower().startswith(b"filter.")
        and key.lower().endswith((b".clean", b".process"))
        for scope, key in zip(fields[::2], fields[1::2])
    )


def report(status, **fields):
    print(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "status": status, **fields}), flush=True)


def parse_worktrees(output):
    if not output.endswith(b"\0\0"):
        raise Blocked("invalid-worktree-list")
    entries = []
    for block in output[:-2].split(b"\0\0"):
        fields = {}
        for field in block.split(b"\0"):
            name, _, value = field.partition(b" ")
            if name in fields:
                raise Blocked("invalid-worktree-list")
            fields[name] = value
        if b"worktree" not in fields:
            raise Blocked("invalid-worktree-list")
        entries.append({
            "path": Path(os.fsdecode(fields[b"worktree"])),
            "head": fields.get(b"HEAD", b"").decode("ascii"),
            "branch": os.fsdecode(fields.get(b"branch", b"")),
            "locked": b"locked" in fields,
            "prunable": b"prunable" in fields,
        })
    return entries


def list_worktrees(repository):
    return parse_worktrees(git(repository, "worktree", "list", "--porcelain", "-z"))


def discover_repositories(roots):
    seen = set()
    for root in roots:
        if not root.is_dir():
            report("discovery-error", path=str(root), reason="root-missing")
            continue
        def walk_error(error):
            report("discovery-error", path=str(error.filename), reason="unreadable-directory")
        for directory, directories, files in os.walk(root, onerror=walk_error, followlinks=False):
            if ".git" in directories or ".git" in files:
                try:
                    common = git(directory, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
                    if common not in seen:
                        seen.add(common)
                        yield Path(directory)
                except Blocked as error:
                    report("discovery-error", path=directory, reason=str(error))
            directories[:] = [
                name for name in directories
                if name not in SCAN_EXCLUSIONS and Path(directory) / name != Path.home() / ".cache/uv"
            ]


def open_process_paths():
    output = command(["lsof", "-nP", "-u", str(os.getuid()), "-F0pn"])
    paths = set()
    process_id = None
    for field in output.split(b"\0"):
        field = field.lstrip(b"\n")
        if field.startswith(b"p"):
            process_id = int(field[1:])
        elif field.startswith(b"n/") and process_id != os.getpid():
            # 日本語や改行が別の表記で出力されても、使用中のファイルを見落とさないようにする。
            raw_path = codecs.escape_decode(field[1:])[0]
            paths.add(Path(os.fsdecode(raw_path)).resolve())
    return paths


def local_block_reason(repository, worktree, process_paths):
    path = worktree["path"]
    if worktree["locked"] or worktree["prunable"]:
        return "locked-or-prunable"
    if not path.is_dir() or path.is_symlink():
        return "missing-or-symlink"
    comparison_path = normalize_path_for_comparison(path.resolve())
    branch = worktree["branch"]
    if not branch.startswith("refs/heads/") or branch.removeprefix("refs/heads/") in SHARED_BRANCHES:
        return "detached-or-shared-branch"
    if any(normalize_path_for_comparison(open_path).is_relative_to(comparison_path) for open_path in process_paths):
        return "in-use"
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", worktree["head"]):
        return "invalid-head"
    for operation in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer", "index.lock"):
        operation_path = os.fsdecode(git(path, "rev-parse", "--path-format=absolute", "--git-path", operation)).strip()
        if Path(operation_path).exists():
            return "git-operation-in-progress"
    # 変更の確認を省く設定があると、編集済みファイルも変更なしと判定される。
    indexed_files = git(path, "ls-files", "-v", "-z").split(b"\0")
    if any(entry.startswith(b"S ") or entry[:1].islower() for entry in indexed_files):
        return "assumed-or-skipped-files"
    if any(
        entry.startswith(b"160000 ") for entry in git(path, "ls-files", "--stage", "-z").split(b"\0")
    ):
        return "submodule"
    if has_repository_git_filters(path):
        return "repository-git-filters"
    if git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"):
        return "uncommitted-or-untracked"
    ignored = git(path, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z")
    if any("node_modules" not in Path(os.fsdecode(name)).parts for name in ignored.split(b"\0") if name):
        return "ignored-files"
    if git(path, "rev-parse", "HEAD").decode().strip() != worktree["head"]:
        return "head-changed"
    return None


def github_repository(repository):
    remote = git(repository, "remote", "get-url", "origin").decode().strip()
    match = re.fullmatch(r"(?:git@github\.com:|https://github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?", remote)
    if not match:
        raise Blocked("unsupported-origin")
    return match[1]


def expected_account(repository, accounts):
    matching = [scope for scope in accounts if repository.resolve().is_relative_to(scope[0])]
    if not matching:
        raise Blocked("account-not-configured")
    return max(matching, key=lambda scope: len(scope[0].parts))[1]


class GitHub:
    def __init__(self, repository, account):
        self.repository = repository
        self.run("auth", "status", "--active", "--hostname", "github.com")
        if self.run("api", "--hostname", "github.com", "user", "--jq", ".login").decode().strip() != account:
            raise Blocked("account-mismatch")
        self.origin = github_repository(repository)
        metadata = json.loads(self.run("api", "--hostname", "github.com", f"repos/{self.origin}", "--jq", "{full_name, parent: .parent.full_name}"))
        if metadata["full_name"].lower() != self.origin.lower():
            raise Blocked("origin-mismatch")
        self.targets = [self.origin]
        if metadata.get("parent"):
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", metadata["parent"]):
                raise Blocked("invalid-parent")
            self.targets.append(metadata["parent"])

    def run(self, *arguments):
        return command(["direnv", "exec", str(self.repository), "gh", *arguments], cwd=self.repository)

    def merged_pull_request(self, worktree):
        branch = worktree["branch"].removeprefix("refs/heads/")
        query = urlencode({"state": "all", "head": f"{self.origin.split('/')[0]}:{branch}", "per_page": 100})
        pull_requests = []
        for target in self.targets:
            output = self.run(
                "api", "--hostname", "github.com", "--method", "GET",
                f"repos/{target}/pulls?{query}", "--paginate", "--jq", PR_FIELDS,
            )
            pull_requests.extend(json.loads(line) for line in output.splitlines() if line)
        matching = [pr for pr in pull_requests if pr["head"]["repo"] and pr["head"]["repo"].lower() == self.origin.lower() and pr["head"]["ref"] == branch]
        if any(pr["state"] == "open" for pr in matching):
            return None
        return next((pr["html_url"] for pr in matching if pr["state"] == "closed" and pr["merged_at"] and pr["head"]["sha"] == worktree["head"]), None)


def process_repository(found_path, accounts, apply, process_paths):
    worktrees = list_worktrees(found_path)
    repository = worktrees[0]["path"]
    github = None
    for worktree in worktrees[1:]:
        path = str(worktree["path"])
        try:
            reason = local_block_reason(repository, worktree, process_paths)
            if reason:
                report("skipped", path=path, reason=reason)
                continue
            if github is None:
                github = GitHub(repository, expected_account(repository, accounts))
            pull_request = github.merged_pull_request(worktree)
            if not pull_request:
                report("skipped", path=path, reason="no-merged-pr-at-head")
                continue
            if not apply:
                report("candidate", path=path, head=worktree["head"], pr=pull_request)
                continue
            current = next((entry for entry in list_worktrees(repository)[1:] if entry["path"] == worktree["path"]), None)
            if current != worktree:
                report("skipped", path=path, reason="worktree-changed")
                continue
            reason = local_block_reason(repository, current, open_process_paths())
            if reason:
                report("skipped", path=path, reason=reason)
                continue
            git(repository, "worktree", "remove", "--", path)
            report("removed", path=path, head=worktree["head"], pr=pull_request)
        except (OSError, RuntimeError):
            report("blocked", path=path, reason="filesystem-unreadable")
        except (Blocked, ValueError, KeyError, TypeError) as error:
            report("blocked", path=path, reason=str(error) if isinstance(error, Blocked) else "invalid-response")


def install_launch_agent(config_path, apply):
    target = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    if target.exists() or target.is_symlink():
        raise Blocked("launch-agent-already-exists")
    path_directories = []
    for tool in ("git", "direnv", "gh", "lsof"):
        executable = shutil.which(tool)
        if executable is None:
            raise Blocked("missing-required-tool:" + tool)
        path_directories.append(str(Path(executable).absolute().parent))
    log_directory = Path.home() / "Library/Logs/worktree-cleanup"
    log_directory.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    arguments = [sys.executable, str(Path(__file__).resolve()), "--config", str(config_path)]
    if apply:
        arguments.append("--apply")
    agent = {
        "Label": LABEL, "ProgramArguments": arguments,
        "StartCalendarInterval": {"Hour": DAILY_RUN_HOUR, "Minute": DAILY_RUN_MINUTE},
        "RunAtLoad": False, "WorkingDirectory": str(Path.home()),
        "EnvironmentVariables": {"PATH": ":".join(dict.fromkeys([*path_directories, "/usr/bin", "/bin", "/usr/sbin", "/sbin"]))},
        "StandardOutPath": str(log_directory / "events.jsonl"),
        "StandardErrorPath": str(log_directory / "stderr.log"),
        "ProcessType": "Background",
    }
    with target.open("xb") as stream:
        plistlib.dump(agent, stream)
    report("installed", path=str(target), mode="apply" if apply else "dry-run")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--apply", action="store_true", help="remove eligible worktrees")
    parser.add_argument("--install", action="store_true", help="write a LaunchAgent plist without loading or overwriting it")
    args = parser.parse_args()
    config_path = args.config.expanduser().resolve()
    try:
        config = json.loads(config_path.read_text())
        roots = [Path(value).expanduser().resolve() for value in config["scan_roots"]]
        accounts = [(Path(value["path"]).expanduser().resolve(), value["login"]) for value in config["accounts"]]
        if not roots or not accounts or any(not re.fullmatch(r"[A-Za-z0-9-]+", login) for _, login in accounts):
            raise Blocked("invalid-config")
        if args.install:
            install_launch_agent(config_path, args.apply)
            return 0
        state = Path.home() / ".local/state/worktree-cleanup"
        state.mkdir(parents=True, exist_ok=True)
        with (state / "run.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                report("skipped", reason="already-running")
                return 0
            report("started", mode="apply" if args.apply else "dry-run")
            # 利用中のファイルを確認できない場合は、削除を進めない。
            process_paths = open_process_paths()
            repositories = list(discover_repositories(roots))
            for repository in repositories:
                try:
                    process_repository(repository, accounts, args.apply, process_paths)
                except (OSError, RuntimeError):
                    report("blocked", path=str(repository), reason="filesystem-unreadable")
                except (Blocked, ValueError, KeyError, TypeError):
                    report("blocked", path=str(repository), reason="repository-unreadable")
            report("finished", repositories=len(repositories))
        return 0
    except (Blocked, OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        report("error", reason=str(error) if isinstance(error, Blocked) else "config-or-runtime-error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
