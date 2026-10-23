import copy
import http.client
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from sm_manager.collector import Collector, cleanup_plan
from sm_manager.config import load_config
from sm_manager.discovery import Discovery, codex_metadata, identity, within
from sm_manager.history import History, RETENTION
from sm_manager.metrics import Docker, docker_memory, parse_stat, process_cpu
from sm_manager.server import handler_for


def worktree(path, project="project", branch="main"):
    return {"id": identity(path), "path": path, "name": Path(path).name,
            "project_id": project, "project_name": project, "branch": branch,
            "is_main": branch == "main", "codex_managed": False}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.discovery = Discovery({"container_overrides": {}})
        self.a = worktree("/workspace/app")
        self.b = worktree("/workspace/app2", branch="feature")
        self.nested = worktree("/workspace/app/packages/site", project="site")
        self.discovery.worktrees = {w["id"]: w for w in [self.a, self.b, self.nested]}

    def test_path_boundaries_and_longest_prefix(self):
        self.assertFalse(within("/workspace/application", "/workspace/app"))
        self.assertEqual(self.discovery.match("/workspace/app/packages/site/src")["id"], self.nested["id"])
        self.assertIsNone(self.discovery.match("/workspace/app-other"))

    def test_container_labels_hashes_and_ambiguous_mounts(self):
        container = {"Id": "abc", "Names": ["/db"], "Labels": {"com.docker.compose.project.working_dir": "/workspace/app/server"}}
        self.assertEqual(self.discovery.container_owner(container)[0], self.a)
        container["Labels"] = {"com.example.worktree-path-hash": identity(self.b["path"])}
        self.assertEqual(self.discovery.container_owner(container)[0], self.b)
        container["Labels"] = {}
        container["Mounts"] = [{"Type": "bind", "Source": self.a["path"]}, {"Type": "bind", "Source": self.b["path"]}]
        self.assertEqual(self.discovery.container_owner(container), (None, "shared mounts"))
        self.discovery.config["container_overrides"] = {"db": self.a["path"]}
        self.assertEqual(self.discovery.container_owner(container)[0], self.a)

    def test_conflicting_labels_are_not_guessed(self):
        container = {"Id": "abc", "Names": ["/db"], "Labels": {"sm-manager.worktree": self.a["path"], "com.docker.compose.project.working_dir": self.b["path"]}}
        self.assertEqual(self.discovery.container_owner(container), (None, "conflicting worktree labels"))

    def test_git_worktrees_share_project_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "project"
            repository.mkdir()
            def git(*args):
                return subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True, text=True)
            git("init", "-b", "main")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "fixture")
            git("worktree", "add", "-b", "feature", str(root / "feature"))
            config = {"roots": [str(repository)], "codex_home": str(root / "missing"), "scan_depth": 2, "container_overrides": {}}
            discovery = Discovery(config)
            discovery.refresh()
            self.assertEqual(len(discovery.worktrees), 2)
            self.assertEqual(len({w["project_id"] for w in discovery.worktrees.values()}), 1)
            self.assertEqual({w["branch"] for w in discovery.worktrees.values()}, {"main", "feature"})

    def test_codex_reader_selects_only_metadata_and_tolerates_missing_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            db = sqlite3.connect(Path(directory) / "state_5.sqlite")
            db.execute("CREATE TABLE threads(cwd TEXT,updated_at INTEGER,archived INTEGER,first_user_message TEXT)")
            db.execute("INSERT INTO threads VALUES('/workspace/app',1234,0,'never expose this')")
            db.commit()
            result = codex_metadata(directory)
            self.assertTrue(result["available"])
            self.assertNotIn("never expose", json.dumps(result))
            db.execute("DROP TABLE threads")
            db.commit()
            self.assertFalse(codex_metadata(directory)["available"])
            db.close()


class MetricsTests(unittest.TestCase):
    def test_proc_stat_handles_parentheses_and_spaces(self):
        fields = ["S"] + ["0"] * 21
        fields[1], fields[11], fields[12], fields[19], fields[21] = "7", "100", "20", "55", "12"
        result = parse_stat("42 (worker (pool)) " + " ".join(fields))
        self.assertEqual(result["name"], "worker (pool)")
        self.assertEqual(result["ticks"], 120)
        self.assertEqual(result["memory_bytes"], 12 * os.sysconf("SC_PAGE_SIZE"))

    def test_cpu_normalization_and_pid_reuse(self):
        previous = {(42, 100): 200}
        self.assertEqual(process_cpu(previous, 42, 100, 300, 1, 100, 8), 12.5)
        self.assertEqual(process_cpu(previous, 42, 999, 300, 1, 100, 8), 0)
        self.assertEqual(process_cpu(previous, 42, 100, 50, 1, 100, 8), 0)

    def test_docker_cache_accounting(self):
        self.assertEqual(docker_memory({"memory_stats": {"usage": 1000, "stats": {"inactive_file": 300}}}), 700)
        self.assertEqual(docker_memory({"memory_stats": {"usage": 1000, "stats": {"total_inactive_file": 200}}}), 800)
        self.assertEqual(docker_memory({"memory_stats": {"usage": 1000, "stats": {"inactive_file": 2000}}}), 1000)

    def test_docker_failure_is_not_reported_as_zero_usage(self):
        docker = Docker("/nonexistent/socket")
        self.assertEqual(docker.sample(), [])
        self.assertFalse(docker.available)
        self.assertIsNone(docker.stats({"Id": "example"})["memory_bytes"])

    def test_docker_cpu_is_whole_machine_share(self):
        docker = Docker("/unused")
        docker.previous["example"] = (100, 1000)
        with patch.object(docker, "get", return_value={"cpu_stats": {"cpu_usage": {"total_usage": 200}, "system_cpu_usage": 1800, "online_cpus": 8}}):
            self.assertEqual(docker.stats({"Id": "example"})["cpu_percent"], 12.5)


class HistoryTests(unittest.TestCase):
    def test_retention_aggregation_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            history = History(directory)
            now = int(time.time())
            history.db.execute("INSERT INTO samples VALUES(?,?,?,?)", (now - RETENTION - 100, "host", 99, 999))
            snapshot = {"timestamp": now - 10, "system": {"cpu_percent": 5, "memory_used": 100}, "totals": {"cpu_percent": 2, "memory_bytes": 20}, "projects": []}
            history.record(snapshot)
            snapshot["timestamp"] = now
            history.record(snapshot)
            self.assertEqual(history.db.execute("SELECT COUNT(*) FROM samples WHERE timestamp<?", (now - RETENTION,)).fetchone()[0], 0)
            self.assertTrue(history.query("host", 24)["points"])
            self.assertFalse(history.query("unknown", 24)["points"])
            history.close()
            history = History(directory)
            self.assertEqual(history.query("host", 24)["points"][-1]["memory_bytes"], 100)
            history.close()


class AttributionTests(unittest.TestCase):
    def test_containers_not_double_counted_ancestry_and_codex_separated(self):
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(Path(directory) / "absent.json")
            config["data_dir"] = directory
            collector = Collector(config)
            w = worktree("/workspace/app")
            collector.discovery.worktrees = {w["id"]: w}
            collector.discovery.codex = {"threads": [], "available": False, "status": "missing"}
            collector.last_discovery = time.monotonic()
            def proc(pid, ppid, cwd, memory, container=False, codex=False):
                return {"pid": pid, "ppid": ppid, "cwd": cwd, "memory_bytes": memory, "cpu_percent": 1, "container": container, "codex": codex, "ports": [], "name": "worker", "start_ticks": 100}
            processes = [proc(1, 0, w["path"], 100), proc(2, 1, "/tmp", 50), proc(3, 0, w["path"], 1000, container=True), proc(4, 0, w["path"], 80, codex=True)]
            container = {"id": "abc", "name": "db", "memory_bytes": 200, "cpu_percent": 2, "ports": [], "stats_available": True,
                         "raw": {"Id": "abc", "Names": ["/db"], "Labels": {"sm-manager.worktree": w["path"]}}}
            collector.docker.available = True
            with patch.object(collector.processes, "sample", return_value=(processes, {"memory_used": 2000, "cpu_percent": 8})), patch.object(collector.docker, "sample", return_value=[container]):
                result = collector.sample()
            self.assertEqual(result["totals"]["memory_bytes"], 350)
            self.assertEqual(result["codex"]["memory_bytes"], 80)
            actual = result["projects"][0]["worktrees"][0]
            self.assertEqual(len(actual["processes"]), 2)
            before = copy.deepcopy(result)
            plan = cleanup_plan(result, w["id"])
            self.assertTrue(plan["read_only"])
            self.assertEqual(plan["resources"]["processes"][0]["start_ticks"], 100)
            self.assertIn("Preserve databases", plan["prompt"])
            self.assertEqual(result, before)
            self.assertIsNone(cleanup_plan(result, "missing"))
            collector.history.close()


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class FakeCollector:
            def get_snapshot(self):
                return {"stale": False, "timestamp": time.time(), "projects": []}
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(FakeCollector(), {"allowed_hosts": ["localhost", "127.0.0.1"], "port": 0}))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, path, method="GET", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request(method, path, headers={"Host": "localhost", **(headers or {})})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_health_static_and_no_mutation_endpoint(self):
        status, headers, body = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.request("/")[0], 200)
        self.assertEqual(self.request("/api/snapshot", method="POST")[0], 501)

    def test_host_origin_and_static_path_boundaries(self):
        self.assertEqual(self.request("/api/snapshot", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("/api/snapshot", headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("/api/snapshot", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request("/../../etc/passwd")[0], 404)
        self.assertEqual(self.request("/config.json")[0], 404)


if __name__ == "__main__":
    unittest.main()
