"""Small, read-only host checks; slow checks are cached by the collector."""

import json
import platform
import shutil
import time
from pathlib import Path

from .discovery import run

SLEEP_SERVICES = ("systemd-suspend.service", "systemd-hibernate.service",
                  "systemd-hybrid-sleep.service", "systemd-suspend-then-hibernate.service")


def memory_pressure(path=Path("/proc/pressure/memory")):
    try:
        rows = {}
        for line in path.read_text().splitlines():
            name, *fields = line.split()
            values = dict(field.split("=", 1) for field in fields)
            rows[name] = {key: float(values[key]) for key in ("avg10", "avg60", "avg300")}
        return rows
    except (OSError, ValueError, KeyError):
        return None


class MachineHealth:
    def __init__(self, data_dir):
        self.state_path = Path(data_dir) / "health-state.json"
        try:
            self.state = json.loads(self.state_path.read_text())
            if not isinstance(self.state, dict):
                self.state = {}
        except (OSError, ValueError):
            self.state = {}

    def os_version(self):
        try:
            release = platform.freedesktop_os_release()
        except OSError:
            release = {}
        current = {"name": release.get("NAME", "Linux"), "version": release.get("VERSION_ID", ""),
                   "build": release.get("BUILD_ID", "")}
        old = self.state.get("current")
        if old != current:
            self.state = {"current": current, "previous": old, "changed_at": time.time() if old else None}
            try:
                temporary = self.state_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(self.state))
                temporary.replace(self.state_path)
            except OSError:
                pass  # Reporting health must not depend on writable state storage.
        return {**current, "previous": self.state.get("previous"), "changed_at": self.state.get("changed_at")}

    def sample(self, system, docker, config):
        checks = []

        def add(key, label, status, value, detail):
            checks.append({"id": key, "label": label, "status": status, "value": value, "detail": detail})

        add("docker", "Docker", "ok" if docker.available else "warning",
            "Connected" if docker.available else "Unavailable", docker.status)
        try:
            tailscale = json.loads(run(["tailscale", "status", "--json"]) or "{}")
            backend = tailscale.get("BackendState")
            connected = backend == "Running"
            route_ok = True
            route = config.get("serve_mapping")
            if connected and route:
                serve = json.loads(run(["tailscale", "serve", "status", "--json"]) or "{}")
                route_ok = serve.get("Web", {}).get(route["host"], {}).get("Handlers", {}).get("/", {}).get("Proxy") == route["target"]
            add("tailscale", "Tailscale", "ok" if connected and route_ok else "warning" if backend else "unknown",
                "Connected" if connected and route_ok else "Route missing" if connected else backend or "Unknown",
                "Private access route verified" if connected and route and route_ok else "Tailnet connected" if connected and route_ok else "Check the local Tailscale service and private route")
        except (ValueError, AttributeError, KeyError, TypeError):
            add("tailscale", "Tailscale", "unknown", "Unknown", "Status could not be read")
        states = run(["systemctl", "show", "--property=LoadState", "--value", *SLEEP_SERVICES]).split()
        protected = len(states) == len(SLEEP_SERVICES) and all(value == "masked" for value in states)
        add("sleep", "Sleep protection", "ok" if protected else "warning" if states else "unknown",
            "Protected" if protected else "Not verified",
            "All suspend and hibernation services are masked" if protected else "Always-on protection is not confirmed; check sleep settings")
        disks = []
        seen = set()
        for label, path in (("Home", Path.home()), ("Monitor data", Path(config["data_dir"]))):
            try:
                device = path.stat().st_dev
                if device in seen:
                    continue
                seen.add(device)
                usage = shutil.disk_usage(path)
                disks.append({"label": label, "total": usage.total, "used": usage.used, "free": usage.free})
            except OSError:
                pass
        for index, disk in enumerate(disks):
            fraction = disk["free"] / max(1, disk["total"])
            add(f"disk-{index}", f"{disk['label']} disk", "warning" if fraction < 0.1 else "ok",
                f"{disk['free'] / 2**30:.1f} GiB free", f"{fraction * 100:.1f}% available of {disk['total'] / 2**30:.1f} GiB")
        if not disks:
            add("disk", "Disk space", "unknown", "Unknown", "Filesystem usage is unavailable")
        pressure = memory_pressure()
        some = pressure.get("some", {}).get("avg60") if pressure else None
        add("pressure", "Memory pressure", "unknown" if some is None else "warning" if some >= 5 else "ok",
            "Unknown" if some is None else f"{some:.2f}% stalled",
            "Tasks waiting for memory, averaged over one minute")
        release = self.os_version()
        changed_recently = release["changed_at"] and time.time() - release["changed_at"] < 86400
        add("os", "Operating system", "warning" if changed_recently else "ok",
            f"{release['name']} {release['version']}".strip(),
            f"Build {release['build']}" + (" · changed since the previous observed version; check machine settings" if release["changed_at"] else " · baseline recorded"))
        return {"checked_at": time.time(), "checks": checks, "disks": disks,
                "pressure": pressure, "os": release, "uptime_seconds": system.get("uptime_seconds")}
