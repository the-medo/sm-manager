"""Linux /proc and read-only local Docker Engine sampling. No external packages."""

import concurrent.futures
import http.client
import json
import os
import re
import socket
import time
from pathlib import Path

from .discovery import run


def parse_stat(raw):
    end = raw.rfind(")")
    fields = raw[end + 2:].split()
    return {"name": raw[raw.index("(") + 1:end], "ppid": int(fields[1]),
            "ticks": int(fields[11]) + int(fields[12]), "start_ticks": int(fields[19]),
            "memory_bytes": max(0, int(fields[21])) * os.sysconf("SC_PAGE_SIZE")}


def process_cpu(previous, pid, start_ticks, ticks, elapsed, hz, cores):
    old = previous.get((pid, start_ticks))
    return round(max(0, ticks - old) / hz / elapsed / cores * 100, 3) if old is not None and elapsed else 0.0


class Processes:
    def __init__(self):
        self.previous = {}
        self.previous_time = None
        self.previous_cpu = None
        self.cores = os.cpu_count() or 1
        self.hz = os.sysconf("SC_CLK_TCK")

    def sample(self):
        now = time.monotonic()
        elapsed = now - self.previous_time if self.previous_time else 0
        current, items = {}, []
        ports = {}
        for line in run(["ss", "-H", "-ltnp"]).splitlines():
            fields = line.split()
            if len(fields) >= 4:
                try:
                    port = int(fields[3].rsplit(":", 1)[-1])
                    for pid in re.findall(r"pid=(\d+)", line):
                        ports.setdefault(int(pid), set()).add(port)
                except ValueError:
                    pass
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                data = parse_stat((entry / "stat").read_text())
                pid = int(entry.name)
                key = (pid, data["start_ticks"])
                data["cpu_percent"] = process_cpu(self.previous, pid, data["start_ticks"], data["ticks"], elapsed, self.hz, self.cores)
                current[key] = data.pop("ticks")
                data.update(pid=pid, ports=sorted(ports.get(pid, [])), cwd=None, container=False, codex=False)
                try:
                    data["cwd"] = os.readlink(entry / "cwd").removesuffix(" (deleted)")
                except OSError:
                    pass
                try:
                    executable = os.readlink(entry / "exe").removesuffix(" (deleted)")
                    if data["name"] in {"MainThread", "python3", "python"}:
                        data["name"] = Path(executable).name
                    data["codex"] = "codex" in Path(executable).name.lower() or "codex" in data["name"].lower()
                except OSError:
                    data["codex"] = "codex" in data["name"].lower()
                try:
                    cgroup = (entry / "cgroup").read_text()
                    data["container"] = bool(re.search(r"(?:docker[-/]|cri-containerd[-/])[0-9a-f]{12,64}", cgroup))
                except OSError:
                    pass
                items.append(data)
            except (OSError, ValueError, IndexError):
                continue  # A process can exit between reads.
        self.previous, self.previous_time = current, now
        values = [int(v) for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
        total, idle = sum(values), values[3] + values[4]
        cpu = 0.0
        if self.previous_cpu:
            diff = total - self.previous_cpu[0]
            if diff > 0:
                cpu = max(0, min(100, 100 * (1 - (idle - self.previous_cpu[1]) / diff)))
        self.previous_cpu = total, idle
        memory = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            memory[key] = int(value.strip().split()[0]) * 1024
        system = {"cpu_percent": round(cpu, 2), "cores": self.cores,
                  "memory_total": memory["MemTotal"], "memory_used": memory["MemTotal"] - memory["MemAvailable"],
                  "swap_total": memory["SwapTotal"], "swap_used": memory["SwapTotal"] - memory["SwapFree"],
                  "uptime_seconds": int(float(Path("/proc/uptime").read_text().split()[0])),
                  "load": list(os.getloadavg()), "hostname": socket.gethostname(), "process_count": len(items)}
        return items, system


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=3)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def docker_memory(stats):
    memory = stats.get("memory_stats", {})
    cache = memory.get("stats", {})
    inactive = cache.get("inactive_file", cache.get("total_inactive_file", cache.get("cache", 0)))
    usage = memory.get("usage", 0)
    return max(0, usage - inactive) if inactive < usage else usage


class Docker:
    def __init__(self, path):
        self.path = path
        self.previous = {}
        self.status = "Waiting for Docker"
        self.available = False

    def get(self, path):
        connection = UnixConnection(self.path)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            if response.status != 200:
                raise OSError(f"Docker returned HTTP {response.status}")
            return json.loads(response.read())
        finally:
            connection.close()

    def stats(self, container):
        cid = container["Id"]
        try:
            stats = self.get(f"/containers/{cid}/stats?stream=false&one-shot=true")
            cpu = stats.get("cpu_stats", {})
            usage = cpu.get("cpu_usage", {}).get("total_usage", 0)
            system = cpu.get("system_cpu_usage", 0)
            previous = self.previous.get(cid)
            percent = 0.0
            if previous and system > previous[1]:
                # Share of the whole machine, consistent with host process CPU.
                percent = max(0, (usage - previous[0]) / (system - previous[1]) * 100)
            self.previous[cid] = (usage, system)
            return {"memory_bytes": docker_memory(stats), "cpu_percent": round(percent, 3),
                    "pids": stats.get("pids_stats", {}).get("current", 0), "stats_available": True}
        except (OSError, ValueError, http.client.HTTPException):
            return {"memory_bytes": None, "cpu_percent": None, "pids": None, "stats_available": False}

    def sample(self):
        try:
            containers = self.get("/containers/json")
            self.available = True
            self.status = "Connected · local Docker Engine"
        except (OSError, ValueError, http.client.HTTPException):
            self.available = False
            self.status = "Docker unavailable · check socket access"
            return []
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            metrics = list(pool.map(self.stats, containers))
        live = {c["Id"] for c in containers}
        self.previous = {key: value for key, value in self.previous.items() if key in live}
        # Strip commands, arbitrary labels, and network metadata before returning to callers.
        return [{"raw": {k: c[k] for k in ("Id", "Names", "Labels", "Mounts") if k in c},
                 "id": c["Id"], "name": (c.get("Names") or [c["Id"][:12]])[0].lstrip("/"),
                 "image": c.get("Image", ""), "state": c.get("State", "running"),
                 "service": (c.get("Labels") or {}).get("com.docker.compose.service", ""),
                 "ports": sorted({p["PublicPort"] for p in c.get("Ports", []) if p.get("PublicPort")}),
                 **m} for c, m in zip(containers, metrics)]


def private_urls():
    try:
        state = json.loads(run(["tailscale", "serve", "status", "--json"]) or "{}")
        result = {}
        for host, web in state.get("Web", {}).items():
            proxy = web.get("Handlers", {}).get("/", {}).get("Proxy", "")
            match = re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):(\d+)/?", proxy)
            if match and re.fullmatch(r"[a-zA-Z0-9.-]+:\d+", host):
                result[int(match[1])] = "https://" + host
        return result
    except (ValueError, AttributeError):
        return {}
