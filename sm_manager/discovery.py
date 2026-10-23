"""Discover ownership from Git and minimal, read-only Codex metadata."""

import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path

SKIP = {"node_modules", "vendor", "dist", "build", "target", "__pycache__", "nodejs", ".venv", "venv", "coverage"}


def identity(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


def within(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def run(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=4, check=False)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def codex_metadata(home):
    result = {"available": False, "threads": [], "worktrees": [], "status": "No Codex metadata found"}
    databases = sorted(Path(home).glob("state_*.sqlite"), key=lambda p: int(p.stem.split("_")[-1]) if p.stem.split("_")[-1].isdigit() else 0, reverse=True)
    if not databases:
        return result
    try:
        with sqlite3.connect(databases[0].resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as db:
            db.execute("PRAGMA query_only=ON")
            # Intentionally do not select titles, prompts, rollout paths, or token counters.
            result["threads"] = [{"cwd": row[0], "updated_at": row[1], "archived": bool(row[2])} for row in db.execute("SELECT cwd, updated_at, archived FROM threads")]
            try:
                rows = db.execute("SELECT payload FROM thread_attachments WHERE attachment_type='worktree'")
                for (raw,) in rows:
                    try:
                        data = json.loads(raw)
                        data = data.get("worktree", data)
                        path = data.get("workspaceRoot") or data.get("root")
                        if isinstance(path, str):
                            result["worktrees"].append(path)
                    except (ValueError, AttributeError):
                        continue
            except sqlite3.OperationalError:
                pass
        result.update(available=True, status="Local metadata · read only")
    except (OSError, sqlite3.Error):
        result["status"] = "Codex metadata unavailable; monitoring continues"
    return result


class Discovery:
    def __init__(self, config):
        self.config = config
        self.worktrees = {}
        self.codex = {}

    def refresh(self, extra_paths=()):
        self.codex = codex_metadata(self.config["codex_home"])
        candidates = set()
        roots = set(self.config["roots"])
        roots.update(self.codex["worktrees"])
        roots.update(t["cwd"] for t in self.codex["threads"] if isinstance(t["cwd"], str))
        for root in roots:
            base = Path(root).expanduser()
            if not base.is_dir() or base == Path.home() or base == Path("/"):
                continue
            for directory, dirs, files in os.walk(base):
                depth = len(Path(directory).relative_to(base).parts)
                if ".git" in dirs or ".git" in files:
                    candidates.add(str(Path(directory).resolve()))
                    dirs[:] = []
                else:
                    dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP] if depth < self.config["scan_depth"] else []
        # Processes and container paths may be outside the configured roots.
        for raw in extra_paths:
            if not raw or not raw.startswith("/"):
                continue
            path = Path(raw)
            for parent in (path, *path.parents):
                if (parent / ".git").exists():
                    candidates.add(str(parent.resolve()))
                    break
        expanded = set(candidates)
        common_dirs = set()
        for candidate in sorted(candidates):
            common = run(["git", "-C", candidate, "rev-parse", "--path-format=absolute", "--git-common-dir"])
            if not common or common in common_dirs:
                continue
            common_dirs.add(common)
            for line in run(["git", "-C", candidate, "worktree", "list", "--porcelain"]).splitlines():
                if line.startswith("worktree ") and Path(line[9:]).is_dir():
                    expanded.add(str(Path(line[9:]).resolve()))
        found = {}
        for path in sorted(expanded):
            output = run(["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel"])
            lines = output.splitlines()
            if len(lines) != 2:
                continue
            common, top = lines
            main = str(Path(common).parent) if Path(common).name == ".git" else str(Path(common))
            project_name = Path(main).name
            if project_name in {"app", "main", "monorepo"}:
                project_name = Path(main).parent.name
            branch = run(["git", "-C", top, "symbolic-ref", "--quiet", "--short", "HEAD"]) or "detached"
            branch = branch.removeprefix("refs/heads/")
            wid = identity(top)
            found[wid] = {
                "id": wid, "path": top, "name": Path(top).name,
                "project_id": identity(common), "project_name": project_name,
                "branch": branch, "is_main": top == main,
                "codex_managed": within(top, str(Path(self.config["codex_home"]) / "worktrees")),
            }
        self.worktrees = found

    def match(self, path):
        if not path:
            return None
        matches = [w for w in self.worktrees.values() if within(path, w["path"])]
        return max(matches, key=lambda w: len(w["path"])) if matches else None

    def container_owner(self, container):
        labels = container.get("Labels") or {}
        name = (container.get("Names") or [""])[0].lstrip("/")
        override = self.config["container_overrides"].get(name) or self.config["container_overrides"].get(container["Id"])
        if override:
            owner = self.match(str(Path(override).expanduser().resolve()))
            return (owner, "configured mapping") if owner else (None, "configured path not found")
        paths = []
        for key, value in labels.items():
            if key in {"com.docker.compose.project.working_dir", "sm-manager.worktree"} or key.endswith((".dev.path", ".worktree-path")):
                if isinstance(value, str) and value.startswith("/"):
                    paths.append(value)
        owners = {w["id"]: w for p in paths if (w := self.match(p))}
        if len(owners) == 1:
            return next(iter(owners.values())), "worktree label"
        if len(owners) > 1:
            return None, "conflicting worktree labels"
        for key, value in labels.items():
            if key.endswith(".worktree-path-hash"):
                owners = [w for w in self.worktrees.values() if identity(w["path"]) == value]
                if len(owners) == 1:
                    return owners[0], "worktree path hash"
        owners = {w["id"]: w for m in container.get("Mounts", []) if m.get("Type") == "bind" and (w := self.match(m.get("Source")))}
        if len(owners) == 1:
            return next(iter(owners.values())), "bind mount"
        return None, "shared mounts" if owners else "no ownership metadata"
