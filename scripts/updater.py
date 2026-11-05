#!/usr/bin/env python3
"""Installed, standalone updater for SM Manager's private runtime checkout."""

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SERVICE = "sm-manager.service"
OWNER = "sm-manager deployment v1"
ERRORS = (RuntimeError, OSError, ValueError, subprocess.SubprocessError)


def run(args, *, cwd=None, check=True, env=None, timeout=45):
    result = subprocess.run(args, cwd=cwd, check=False, capture_output=True,
                            text=True, timeout=timeout, env=env)
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def git(directory, *args, check=True):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
           "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oConnectTimeout=10"}
    return run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(directory), *args],
               check=check, env=env)


def validate(checkout):
    # A candidate's tests never use the real user's config, history, or Codex DB.
    with tempfile.TemporaryDirectory(prefix="sm-manager-test-") as directory:
        env = {**os.environ, "HOME": directory, "XDG_CONFIG_HOME": directory + "/config",
               "XDG_DATA_HOME": directory + "/data", "XDG_CACHE_HOME": directory + "/cache",
               "CODEX_HOME": directory + "/codex", "PYTHONPATH": str(checkout)}
        if not (checkout / "tests").is_dir() or not (checkout / "sm_manager/__main__.py").is_file():
            raise RuntimeError("The candidate is missing SM Manager or its tests")
        run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            cwd=checkout, env=env, timeout=120)


def provision(directory, remote, config_path):
    """Create only our own clone; never modify the development checkout."""
    directory = Path(directory)
    settings = {"owner": OWNER, "remote": remote, "config_path": str(config_path)}
    marker = directory / "deployment.json"
    if directory.exists():
        if not marker.is_file() or json.loads(marker.read_text()) != settings:
            raise RuntimeError(f"Refusing to reuse an unrelated deployment: {directory}")
    else:
        directory.mkdir(parents=True, mode=0o700)
        marker.write_text(json.dumps(settings, indent=2) + "\n")
    checkout = directory / "checkout"
    if not checkout.exists():
        with tempfile.TemporaryDirectory(prefix="install-", dir=directory) as temporary:
            candidate = Path(temporary) / "checkout"
            git(directory, "clone", "--no-local", "--single-branch", "--branch", "main",
                "--", remote, str(candidate))
            validate(candidate)
            candidate.rename(checkout)
    deployment = Deployment(directory)
    deployment.check_checkout()
    installed = directory / "updater.py"
    if Path(__file__).resolve() != installed.resolve():
        temporary = directory / "updater.py.tmp"
        shutil.copyfile(__file__, temporary)
        temporary.replace(installed)
    return checkout


class Deployment:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.settings = json.loads((self.directory / "deployment.json").read_text())
        if self.settings.get("owner") != OWNER:
            raise RuntimeError("Not an SM Manager-owned deployment")
        self.checkout = self.directory / "checkout"

    def check_checkout(self):
        if git(self.checkout, "symbolic-ref", "--short", "HEAD").stdout.strip() != "main":
            raise RuntimeError("Runtime checkout must stay on main; refusing to change branches")
        if git(self.checkout, "remote", "get-url", "origin").stdout.strip() != self.settings["remote"]:
            raise RuntimeError("Runtime origin changed; refusing to update")
        if git(self.checkout, "status", "--porcelain", "--untracked-files=all").stdout.strip():
            raise RuntimeError("Runtime checkout has local changes; leaving them untouched")
        return git(self.checkout, "rev-parse", "HEAD").stdout.strip()

    def service(self, command, check=True):
        return run(["systemctl", "--user", command, SERVICE], check=check)

    def healthy(self, timeout=45):
        config = json.loads(Path(self.settings["config_path"]).read_text())
        url = f"http://127.0.0.1:{int(config['port'])}/api/health"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with opener.open(url, timeout=2) as response:
                    if json.load(response).get("status") == "ok":
                        return True
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(1)
        return False

    def refresh(self, prepare=False):
        # A restart invokes ExecStartPre while the timer holds this lock. That
        # pre-start call must use the prepared checkout without waiting on itself.
        with (self.directory / "update.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if prepare:
                    print("An update is already preparing the runtime checkout.", flush=True)
                    return False
                raise RuntimeError("An update is already in progress; try again shortly")
            return self._refresh(prepare)

    def _refresh(self, prepare):
        previous = self.check_checkout()
        git(self.checkout, "fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main")
        target = git(self.checkout, "rev-parse", "refs/remotes/origin/main").stdout.strip()
        if previous == target:
            print(f"Already running main at {target[:12]}.", flush=True)
            return False
        if git(self.checkout, "merge-base", "--is-ancestor", previous, target, check=False).returncode:
            raise RuntimeError("main is not a fast-forward; leaving the installed version untouched")
        print(f"Validating main {previous[:12]} -> {target[:12]}...", flush=True)
        with tempfile.TemporaryDirectory(prefix="candidate-", dir=self.directory) as temporary:
            candidate = Path(temporary) / "checkout"
            git(self.directory, "clone", "--shared", "--no-checkout", "--", str(self.checkout), str(candidate))
            git(candidate, "checkout", "--detach", target)
            validate(candidate)
        if self.check_checkout() != previous:
            raise RuntimeError("Runtime checkout changed during validation; update cancelled")
        active = not prepare and self.service("is-active", check=False).returncode == 0
        if active:
            self.service("stop")
        try:
            git(self.checkout, "merge", "--ff-only", "--no-edit", target)
            if active:
                self.service("start")
                if not self.healthy():
                    raise RuntimeError("Updated service did not become healthy")
        except ERRORS:
            # This is exclusively our clean runtime clone, never a user's tree.
            # Stop any failing new process before restoring the previously tested code.
            if active:
                self.service("stop")
            git(self.checkout, "reset", "--hard", previous)
            if active:
                self.service("start")
            print(f"Restored previous revision {previous[:12]}.", flush=True)
            raise
        print(f"Installed main at {target[:12]}.", flush=True)
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "update"])
    parser.add_argument("--directory", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        Deployment(args.directory).refresh(prepare=args.command == "prepare")
    except ERRORS as error:
        if args.command == "prepare":
            # Network/auth/test failures must not prevent offline boot.
            print(f"Update skipped; starting the installed version: {error}", file=sys.stderr, flush=True)
        else:
            parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
