import copy
import logging
import os
import threading
import time
from pathlib import Path

from .discovery import Discovery
from .history import History
from .health import MachineHealth
from .storage import DiskUsage
from .metrics import Docker, Processes, private_urls

LOG = logging.getLogger(__name__)


def resources(items):
    return {"memory_bytes": sum(x.get("memory_bytes") or 0 for x in items),
            "cpu_percent": round(sum(x.get("cpu_percent") or 0 for x in items), 3)}


class Collector:
    def __init__(self, config):
        self.config = config
        self.discovery = Discovery(config)
        self.processes = Processes()
        self.docker = Docker(config["docker_socket"])
        self.history = History(config["data_dir"])
        self.machine_health = MachineHealth(config["data_dir"])
        self.disk_usage = DiskUsage(config)
        self.health = None
        self.last_health = 0
        self.activity = {}
        self.last_activity = 0
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.snapshot = None
        self.error = None
        self.last_discovery = 0
        self.urls = {}

    def sample(self):
        started = time.monotonic()
        processes, system = self.processes.sample()
        containers = self.docker.sample()
        if started - self.last_discovery >= self.config["discovery_interval"]:
            extra = [p["cwd"] for p in processes if p["cwd"] and not p["container"]]
            for container in containers:
                raw = container["raw"]
                extra.extend(m.get("Source", "") for m in raw.get("Mounts", []) if m.get("Type") == "bind")
                extra.extend(v for k, v in (raw.get("Labels") or {}).items() if k.endswith(("working_dir", ".dev.path", ".worktree-path")))
            self.discovery.refresh(extra)
            self.urls = private_urls()
            self.last_discovery = started
        worktrees = {wid: {**info, "processes": [], "containers": [], "recent_threads": 0, "last_codex_activity": None} for wid, info in self.discovery.worktrees.items()}
        by_pid = {p["pid"]: p for p in processes}
        assigned, other, codex_processes = {}, [], []
        for process in processes:
            if process["container"]:
                continue
            if process["codex"]:
                codex_processes.append(process)
                continue
            owner = self.discovery.match(process["cwd"])
            if owner:
                assigned[process["pid"]] = owner["id"]
        for process in processes:
            if process["container"] or process["codex"]:
                continue
            wid = assigned.get(process["pid"])
            if not wid:
                parent, visited = process["ppid"], set()
                while parent in by_pid and parent not in visited:
                    visited.add(parent)
                    if parent in assigned:
                        wid = assigned[parent]
                        break
                    if by_pid[parent]["codex"] or by_pid[parent]["container"]:
                        break
                    parent = by_pid[parent]["ppid"]
            if wid:
                process = {**process, "ownership": "working directory" if process["pid"] in assigned else "parent process"}
                worktrees[wid]["processes"].append(process)
            else:
                other.append(process)
        unassigned = []
        for container in containers:
            owner, reason = self.discovery.container_owner(container.pop("raw"))
            container["ownership"] = reason
            container["worktree_id"] = owner["id"] if owner else None
            if owner:
                worktrees[owner["id"]]["containers"].append(container)
            else:
                unassigned.append(container)
        now = time.time()
        threads = self.discovery.codex.get("threads", [])
        for thread in threads:
            owner = self.discovery.match(thread["cwd"])
            if owner:
                worktree = worktrees[owner["id"]]
                worktree["last_codex_activity"] = max(worktree["last_codex_activity"] or 0, thread["updated_at"])
                if thread["updated_at"] >= now - 86400:
                    worktree["recent_threads"] += 1
        projects = {}
        for worktree in worktrees.values():
            items = worktree["processes"] + worktree["containers"]
            worktree.update(resources(items))
            worktree["running"] = bool(items)
            worktree["ports"] = sorted({port for item in items for port in item["ports"]})
            worktree["urls"] = [{"port": p, "url": self.urls[p]} for p in worktree["ports"] if p in self.urls]
            worktree["processes"].sort(key=lambda p: p["memory_bytes"], reverse=True)
            project = projects.setdefault(worktree["project_id"], {"id": worktree["project_id"], "name": worktree["project_name"], "worktrees": []})
            project["worktrees"].append(worktree)
        for project in projects.values():
            project.update(resources(project["worktrees"]))
            project["running_worktrees"] = sum(w["running"] for w in project["worktrees"])
            project["container_count"] = sum(len(w["containers"]) for w in project["worktrees"])
            project["process_count"] = sum(len(w["processes"]) for w in project["worktrees"])
            project["recent_threads"] = sum(w["recent_threads"] for w in project["worktrees"])
            project["worktrees"].sort(key=lambda w: (not w["running"], -w["memory_bytes"], w["branch"]))
        project_list = sorted(projects.values(), key=lambda p: (-p["memory_bytes"], p["name"]))
        total = resources(project_list)
        total.update(project_count=len(project_list), running_projects=sum(p["running_worktrees"] > 0 for p in project_list),
                     worktree_count=len(worktrees), running_worktrees=sum(w["running"] for w in worktrees.values()),
                     container_count=len(containers), process_count=sum(p["process_count"] for p in project_list))
        warnings = []
        if not self.docker.available:
            warnings.append(self.docker.status + ". Container totals are incomplete.")
        missing_stats = sum(not c["stats_available"] for c in containers)
        if missing_stats:
            warnings.append(f"Statistics unavailable for {missing_stats} container(s); totals are partial.")
        result = {"timestamp": now, "interval": self.config["interval"], "system": system, "projects": project_list, "totals": total,
                  "unassigned_containers": unassigned, "other_processes": sorted(other, key=lambda p: p["memory_bytes"], reverse=True)[:12],
                  "codex": {"available": self.discovery.codex.get("available", False), "status": self.discovery.codex.get("status"),
                            "thread_count": len(threads), "recent_threads": sum(t["updated_at"] >= now - 86400 for t in threads),
                            "managed_worktrees": sum(w["codex_managed"] for w in worktrees.values()),
                            "processes": codex_processes, **resources(codex_processes)},
                  "docker": {"available": self.docker.available, "status": self.docker.status},
                  "warnings": warnings, "collection_ms": round((time.monotonic() - started) * 1000)}
        self.history.record(result)
        if started - self.last_health >= 60 or self.health is None:
            try:
                self.health = self.machine_health.sample(system, self.docker, self.config)
            except Exception:
                LOG.exception("Machine health check failed")
            self.last_health = started
        result["machine_health"] = self.health
        if started - self.last_activity >= 60 or not self.activity:
            self.activity = self.history.worktree_activity(now)
            self.last_activity = started
        result["cleanup_candidates"] = cleanup_candidates(result, self.activity, self.config["interval"])
        result["collection_ms"] = round((time.monotonic() - started) * 1000)
        with self.lock:
            self.snapshot = result
            self.error = None
        return result

    def get_snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.snapshot)
            if result:
                result["stale"] = time.time() - result["timestamp"] > max(30, 3 * self.config["interval"])
                if self.error:
                    result["warnings"].append(self.error)
            return result

    def run(self):
        while not self.stop_event.is_set():
            start = time.monotonic()
            try:
                self.sample()
            except Exception:
                LOG.exception("Collection failed; retrying on next interval")
                with self.lock:
                    self.error = "A collection failed; showing the last successful sample. Check service logs."
            self.stop_event.wait(max(0.5, self.config["interval"] - (time.monotonic() - start)))


def cleanup_plan(snapshot, worktree_id):
    worktree = next((w for p in snapshot["projects"] for w in p["worktrees"] if w["id"] == worktree_id), None)
    if not worktree:
        return None
    resources_to_review = {"processes": [{k: p[k] for k in ("pid", "ppid", "name", "start_ticks", "cwd", "ownership")} for p in worktree["processes"]],
                           "containers": [{k: c[k] for k in ("id", "name", "ownership")} for c in worktree["containers"]]}
    launchers = [str(p.relative_to(worktree["path"])) for p in (Path(worktree["path"]) / "scripts/worktree-dev").glob("*-worktree-dev") if p.is_file()]
    prompt = (
        f"Review and clean up the running app in worktree {worktree['path']} (branch {worktree['branch']}).\n"
        f"SM Manager worktree ID: {worktree_id}. Read the current local /api/snapshot and /api/cleanup-plan/{worktree_id} before acting.\n"
        "Follow that repository's AGENTS.md and lifecycle documentation. Verify current PID start times, working directories, and container ownership; this snapshot can become stale. "
        "Stop only this worktree's app resources using its documented launcher when possible. "
        "Preserve databases, volumes, source files, worktrees, and unrelated services. Report what was stopped and any ambiguous resources.\n"
    )
    return {"read_only": True, "generated_at": snapshot["timestamp"], "worktree_id": worktree_id,
            "path": worktree["path"], "branch": worktree["branch"], "launchers_to_review": launchers,
            "resources": resources_to_review, "prompt": prompt}


def cleanup_candidates(snapshot, activity, interval):
    """A review shortlist, never a claim that an app is safe to stop."""
    now = snapshot["timestamp"]
    candidates = []
    for project in snapshot["projects"]:
        for worktree in project["worktrees"]:
            stats = activity.get(worktree["id"])
            if not worktree["running"] or worktree["memory_bytes"] < 64 * 2**20 or not stats:
                continue
            if any(process["pid"] == os.getpid() for process in worktree["processes"]):
                continue
            span = stats["last"] - stats["first"]
            expected = span / interval + 1
            if span < 900 or now - stats["last"] > max(90, interval * 3) or stats["samples"] < expected * 0.8:
                continue
            if stats["cpu_average"] > 0.5 or stats["cpu_peak"] > 1 or worktree["cpu_percent"] > 1:
                continue
            if (worktree["last_codex_activity"] or 0) > now - 1800:
                continue
            if any(not container.get("stats_available", False) for container in worktree["containers"]):
                continue
            candidates.append({"worktree_id": worktree["id"], "project_name": project["name"],
                               "branch": worktree["branch"], "path": worktree["path"],
                               "memory_bytes": worktree["memory_bytes"], "cpu_percent": worktree["cpu_percent"],
                               "observed_seconds": span, "cpu_average": stats["cpu_average"], "cpu_peak": stats["cpu_peak"]})
    return sorted(candidates, key=lambda candidate: -candidate["memory_bytes"])
