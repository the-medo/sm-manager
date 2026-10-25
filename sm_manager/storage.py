"""Bounded, on-demand filesystem and Docker storage accounting. No deletion."""

import copy
import http.client
import json
import os
import stat
import threading
import time
from pathlib import Path

from .discovery import Discovery
from .metrics import Docker

CATEGORIES = ("dependencies", "build_cache", "git", "other")
DEPENDENCIES = {"node_modules", "vendor", ".venv", "venv", ".yarn", ".pnpm-store"}
BUILD_CACHE = {"dist", "build", "target", ".next", ".nuxt", ".turbo", ".cache", "__pycache__", "coverage", ".gradle"}


class ScanLimit(Exception):
    pass


def directory_usage(path, excluded=(), seen=None, deadline=None, max_entries=500000):
    """Count allocated blocks without following links or crossing mounts.

    Directory FDs and O_NOFOLLOW also prevent a directory replaced by a symlink
    during a scan from escaping the discovered worktree.
    """
    root = os.path.abspath(path)
    excluded = {os.path.abspath(value) for value in excluded if os.path.abspath(value) != root}
    seen = seen if seen is not None else set()
    deadline = deadline if deadline is not None else time.monotonic() + 60
    result = {"path": root, "bytes": 0, "categories": dict.fromkeys(CATEGORIES, 0),
              "entries": 0, "partial": False, "notes": []}
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

    def partial(note):
        result["partial"] = True
        if note not in result["notes"]:
            result["notes"].append(note)

    def count(info, category):
        key = (info.st_dev, info.st_ino)
        if key not in seen:
            seen.add(key)
            size = info.st_blocks * 512
            result["bytes"] += size
            result["categories"][category] += size

    def walk(fd, relative, category, device, depth):
        if depth > 100:
            partial("Directory depth limit reached")
            return
        try:
            with os.scandir(fd) as entries:
                for entry in entries:
                    if result["entries"] >= max_entries or time.monotonic() >= deadline:
                        raise ScanLimit()
                    result["entries"] += 1
                    relative_child = os.path.join(relative, entry.name)
                    if os.path.join(root, relative_child) in excluded:
                        continue  # A nested discovered worktree is counted separately.
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if info.st_dev != device:
                            partial("Mounted filesystem skipped")
                            continue
                        bucket = category
                        if category == "other":
                            bucket = "dependencies" if entry.name in DEPENDENCIES else "build_cache" if entry.name in BUILD_CACHE else "git" if entry.name == ".git" else "other"
                        if stat.S_ISDIR(info.st_mode):
                            child = os.open(entry.name, flags, dir_fd=fd)
                            try:
                                actual = os.fstat(child)
                                if actual.st_dev != device:
                                    partial("Mounted filesystem skipped")
                                    continue
                                count(actual, bucket)
                                walk(child, relative_child, bucket, device, depth + 1)
                            finally:
                                os.close(child)
                        else:
                            count(info, bucket)
                    except OSError:
                        partial("Some entries were unreadable or changed during the scan")
        except OSError:
            partial("Some directories could not be read")

    try:
        fd = os.open(root, flags)
        try:
            info = os.fstat(fd)
            count(info, "other")
            walk(fd, "", "other", info.st_dev, 0)
        finally:
            os.close(fd)
    except ScanLimit:
        partial("Scan time or entry limit reached; totals are a lower bound")
    except OSError:
        partial("Worktree directory is unavailable")
    return result


def docker_usage(docker, config, projects, project_id):
    """Attribute stopped and running containers, with explicit shared sizes."""
    discovery = Discovery(config)
    discovery.worktrees = {worktree["id"]: worktree for project in projects for worktree in project["worktrees"]}
    try:
        # Listing ownership is cheap. Avoid Docker's expensive size traversal
        # entirely if the selected project has no associated containers.
        containers = docker.get("/containers/json?all=1")
        image_owners, volume_owners, owned = {}, {}, []
        for container in containers:
            owner, _ = discovery.container_owner(container)
            owner_id = owner["project_id"] if owner else None
            image_owners.setdefault(container.get("ImageID"), set()).add(owner_id)
            for mount in container.get("Mounts", []):
                if mount.get("Type") == "volume":
                    volume_owners.setdefault(mount.get("Name"), set()).add(owner_id)
            if owner_id == project_id:
                owned.append(container)
        data = docker.get("/system/df", timeout=45) if owned else {}
        sizes = {container["Id"]: container.get("SizeRw", -1) for container in data.get("Containers") or []}
        images = [image for image in data.get("Images") or [] if project_id in image_owners.get(image["Id"], set())]
        volumes = [volume for volume in data.get("Volumes") or [] if project_id in volume_owners.get(volume["Name"], set())]
        volume_sizes = [(volume.get("UsageData") or {}).get("Size", -1) for volume in volumes]
        layers = [sizes.get(container["Id"], -1) for container in owned]
        return {"available": True, "container_count": len(owned),
                "writable_bytes": sum(max(0, size) for size in layers),
                "image_bytes": sum(max(0, image.get("Size", 0)) for image in images),
                "volume_bytes": sum(max(0, size) for size in volume_sizes),
                "shared_images": sum(len(image_owners[image["Id"]]) > 1 or image.get("SharedSize", 0) > 0 for image in images),
                "shared_volumes": sum(len(volume_owners[volume["Name"]]) > 1 for volume in volumes),
                "partial": any(size < 0 for size in [*layers, *volume_sizes])}
    except TimeoutError:
        return {"available": False, "status": "Docker disk scan timed out; filesystem totals are available"}
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        return {"available": False, "status": "Docker disk accounting unavailable"}


class DiskUsage:
    def __init__(self, config):
        self.config = config
        self.path = Path(config["data_dir"]) / "disk-usage.json"
        self.lock = threading.Lock()
        self.scanning = None
        try:
            self.results = json.loads(self.path.read_text())
            if not isinstance(self.results, dict):
                self.results = {}
        except (OSError, ValueError):
            self.results = {}

    def status(self, project_ids):
        with self.lock:
            return {"scanning": self.scanning, "projects": copy.deepcopy({key: value for key, value in self.results.items() if key in project_ids})}

    def start(self, projects, project_id):
        project = next((project for project in projects if project["id"] == project_id), None)
        if not project:
            raise KeyError(project_id)
        with self.lock:
            if self.scanning is not None:
                return self.scanning == project_id
            self.scanning = project_id
        threading.Thread(target=self._scan, args=(copy.deepcopy(projects), project_id), daemon=True, name="disk-scan").start()
        return True

    def _scan(self, projects, project_id):
        result = {"project_id": project_id, "worktrees": [], "bytes": 0, "partial": False}
        try:
            project = next(project for project in projects if project["id"] == project_id)
            roots = [worktree["path"] for item in projects for worktree in item["worktrees"]]
            seen = set()
            remaining = 500000
            deadline = time.monotonic() + 120
            for worktree in sorted(project["worktrees"], key=lambda worktree: worktree["path"]):
                usage = directory_usage(worktree["path"], roots, seen, min(deadline, time.monotonic() + 30), max_entries=remaining)
                remaining = max(0, remaining - usage["entries"])
                result["worktrees"].append({**usage, "worktree_id": worktree["id"], "branch": worktree["branch"]})
                result["bytes"] += usage["bytes"]
                result["partial"] |= usage["partial"]
            result["docker"] = docker_usage(Docker(self.config["docker_socket"]), self.config, projects, project_id)
        except Exception:
            result.update(partial=True, error="Scan interrupted; totals may be incomplete")
        finally:
            result["scanned_at"] = time.time()
            with self.lock:
                self.results = {key: value for key, value in self.results.items() if key in {project["id"] for project in projects}}
                self.results[project_id] = result
                self.scanning = None
                try:
                    temporary = self.path.with_suffix(".tmp")
                    temporary.write_text(json.dumps(self.results))
                    temporary.replace(self.path)
                except OSError:
                    pass
