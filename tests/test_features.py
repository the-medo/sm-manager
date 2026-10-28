import copy
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from sm_manager.collector import cleanup_candidates
from sm_manager.health import MachineHealth, memory_pressure
from sm_manager.history import History
from sm_manager.server import handler_for
from sm_manager.storage import DiskUsage, directory_usage, docker_usage


class HealthTests(unittest.TestCase):
    def test_pressure_and_missing_support(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pressure"
            self.assertIsNone(memory_pressure(path))
            path.write_text("some avg10=1.00 avg60=2.00 avg300=3.00 total=40\nfull avg10=0 avg60=0 avg300=0 total=0\n")
            self.assertEqual(memory_pressure(path)["some"]["avg60"], 2)
            path.write_text("invalid")
            self.assertIsNone(memory_pressure(path))

    def test_os_change_survives_monitor_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            release = {"NAME": "Fixture OS", "VERSION_ID": "1", "BUILD_ID": "abc"}
            with patch("sm_manager.health.platform.freedesktop_os_release", return_value=release):
                self.assertIsNone(MachineHealth(directory).os_version()["previous"])
            with patch("sm_manager.health.platform.freedesktop_os_release", return_value={**release, "VERSION_ID": "2"}):
                result = MachineHealth(directory).os_version()
                self.assertEqual(result["previous"]["version"], "1")
                self.assertIsNotNone(result["changed_at"])
                self.assertEqual(MachineHealth(directory).os_version(), result)

    def test_unverified_sleep_and_private_route_are_not_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {"data_dir": directory, "serve_mapping": {"host": "fixture.test:8790", "target": "http://127.0.0.1:8790"}}
            docker = Mock(available=True, status="Connected")
            def run(args):
                if args[0] == "systemctl":
                    return "masked\nloaded\nmasked\nmasked"
                return json.dumps({"BackendState": "Running"}) if args[1] == "status" else "{}"
            with patch("sm_manager.health.run", side_effect=run), patch("sm_manager.health.memory_pressure", return_value=None):
                result = MachineHealth(directory).sample({}, docker, config)
            checks = {check["id"]: check for check in result["checks"]}
            self.assertEqual(checks["sleep"]["status"], "warning")
            self.assertEqual(checks["tailscale"]["value"], "Route missing")
            self.assertEqual(checks["pressure"]["status"], "unknown")
            self.assertEqual(checks["docker"]["status"], "ok")


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.worktree = {"id": "w", "path": "/fixture/app", "branch": "feature", "running": True,
                         "memory_bytes": 200 * 2**20, "cpu_percent": 0.1, "last_codex_activity": None,
                         "processes": [{"pid": -1, "start_ticks": 10}], "containers": []}
        self.snapshot = {"timestamp": self.now, "projects": [{"name": "app", "worktrees": [self.worktree]}]}
        self.activity = {"w": {"first": self.now - 1200, "last": self.now, "samples": 121, "cpu_average": 0.1, "cpu_peak": 0.3}}

    def test_continuously_quiet_apps_are_only_review_candidates(self):
        before = copy.deepcopy(self.snapshot)
        result = cleanup_candidates(self.snapshot, self.activity, 10)
        self.assertEqual(result[0]["worktree_id"], "w")
        self.assertEqual(result[0]["memory_bytes"], self.worktree["memory_bytes"])
        self.assertEqual(self.snapshot, before)

    def test_gaps_short_observation_stale_data_and_cpu_spikes_excluded(self):
        for update in ({"samples": 10}, {"first": self.now - 100}, {"last": self.now - 100}, {"cpu_peak": 2}, {"cpu_average": 0.7}):
            with self.subTest(update=update):
                activity = {"w": {**self.activity["w"], **update}}
                self.assertEqual(cleanup_candidates(self.snapshot, activity, 10), [])

    def test_monitor_recent_codex_and_unknown_container_stats_excluded(self):
        for update in ({"processes": [{"pid": os.getpid()}]}, {"last_codex_activity": self.now - 100},
                       {"containers": [{"stats_available": False}]}, {"running": False}, {"memory_bytes": 0}):
            with self.subTest(update=update):
                snapshot = copy.deepcopy(self.snapshot)
                snapshot["projects"][0]["worktrees"][0].update(update)
                self.assertEqual(cleanup_candidates(snapshot, self.activity, 10), [])


class ActivityTests(unittest.TestCase):
    def test_quarter_hour_and_activity_windows_do_not_include_old_or_future_data(self):
        with tempfile.TemporaryDirectory() as directory:
            history = History(directory)
            now = 10000
            for timestamp in (now - 4000, now - 1000, now - 100, now + 10):
                history.db.execute("INSERT INTO samples VALUES(?,?,?,?)", (timestamp, "worktree:w", 1, 100))
            history.db.commit()
            data = history.query("worktree:w", 0.25, now=now)
            self.assertEqual(data["hours"], 0.25)
            self.assertEqual(len(data["points"]), 1)
            self.assertEqual(data["bucket_seconds"], 10)
            activity = history.worktree_activity(now=now)["w"]
            self.assertEqual(activity["samples"], 2)
            self.assertEqual(activity["first"], now - 1000)
            history.close()


class StorageTests(unittest.TestCase):
    def test_links_nested_worktrees_and_categories_without_modifying_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            outside = Path(directory) / "outside"
            root.mkdir(); outside.mkdir()
            for name in ("node_modules", "build", ".git", "nested"):
                (root / name).mkdir()
                (root / name / "data").write_bytes(b"x" * 8192)
            (outside / "private").write_bytes(b"x" * 65536)
            (root / "external").symlink_to(outside, target_is_directory=True)
            (root / "source").write_bytes(b"y" * 8192)
            (root / "hardlink").hardlink_to(root / "source")
            before = (root / "source").read_bytes()
            result = directory_usage(root, [root / "nested"])
            self.assertFalse(result["partial"])
            for category in ("dependencies", "build_cache", "git"):
                self.assertGreaterEqual(result["categories"][category], 8192)
            self.assertLess(result["bytes"], 65536)
            self.assertEqual(result["bytes"], sum(result["categories"].values()))
            self.assertEqual((root / "source").read_bytes(), before)
            alias = Path(directory) / "alias"
            alias.symlink_to(root, target_is_directory=True)
            self.assertTrue(directory_usage(alias)["partial"])
            self.assertEqual(directory_usage(alias)["bytes"], 0)

    def test_limits_and_missing_directories_are_explicitly_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "file").write_bytes(b"a" * 4096)
            self.assertTrue(directory_usage(directory, max_entries=0)["partial"])
            self.assertTrue(directory_usage(directory, deadline=0)["partial"])
            self.assertTrue(directory_usage(Path(directory) / "missing")["partial"])

    def test_docker_shared_storage_stopped_containers_and_unassigned_ownership(self):
        worktrees = [{"id": "a", "project_id": "a", "path": "/fixture/a"}, {"id": "b", "project_id": "b", "path": "/fixture/b"}]
        projects = [{"id": worktree["id"], "worktrees": [worktree]} for worktree in worktrees]
        containers = [{"Id": name, "Names": ["/" + name], "State": "exited", "ImageID": "image", "SizeRw": 100,
                       "Labels": {"sm-manager.worktree": worktree["path"]}, "Mounts": [{"Type": "volume", "Name": "shared"}]} for name, worktree in zip(("one", "two"), worktrees)]
        containers.append({"Id": "unknown", "Names": ["/a"], "Labels": {}, "Mounts": [], "SizeRw": 99999})
        docker = Mock()
        docker.get.side_effect = [containers, {"Containers": [{"Id": "one", "SizeRw": 100}], "Images": [{"Id": "image", "Size": 1000, "SharedSize": 0}], "Volumes": [{"Name": "shared", "UsageData": {"Size": 2000}}]}]
        result = docker_usage(docker, {"container_overrides": {}}, projects, "a")
        self.assertEqual(result["container_count"], 1)
        self.assertEqual(result["writable_bytes"], 100)
        self.assertEqual(result["image_bytes"], 1000)
        self.assertEqual(result["volume_bytes"], 2000)
        self.assertEqual(result["shared_images"], 1)
        self.assertEqual(result["shared_volumes"], 1)
        self.assertFalse(result["partial"])
        docker.get.assert_any_call("/containers/json?all=1")
        docker.get.assert_any_call("/system/df", timeout=45)

    def test_unowned_projects_skip_expensive_docker_size_traversal(self):
        docker = Mock()
        docker.get.return_value = [{"Id": "unassigned", "Labels": {}, "Mounts": []}]
        result = docker_usage(docker, {"container_overrides": {}}, [], "a")
        self.assertTrue(result["available"])
        self.assertEqual(result["container_count"], 0)
        docker.get.assert_called_once_with("/containers/json?all=1")

    def test_unknown_sizes_and_docker_timeouts_are_explicit(self):
        projects = [{"id": "a", "worktrees": [{"id": "w", "project_id": "a", "path": "/fixture/a"}]}]
        containers = [{"Id": "owned", "Labels": {"sm-manager.worktree": "/fixture/a"}, "Mounts": []}]
        docker = Mock()
        docker.get.side_effect = [containers, {}]
        result = docker_usage(docker, {"container_overrides": {}}, projects, "a")
        self.assertTrue(result["partial"])
        docker.get.side_effect = [containers, TimeoutError()]
        result = docker_usage(docker, {"container_overrides": {}}, projects, "a")
        self.assertFalse(result["available"])
        self.assertIn("timed out", result["status"])

    def test_scan_only_accepts_discovered_project_and_limits_concurrency(self):
        with tempfile.TemporaryDirectory() as directory:
            usage = DiskUsage({"data_dir": directory})
            projects = [{"id": "a", "worktrees": []}, {"id": "b", "worktrees": []}]
            with self.assertRaises(KeyError):
                usage.start(projects, "/etc")
            with patch("sm_manager.storage.threading.Thread"):
                self.assertTrue(usage.start(projects, "a"))
                self.assertTrue(usage.start(projects, "a"))
                self.assertFalse(usage.start(projects, "b"))
            self.assertEqual(usage.status({"a"})["scanning"], "a")

    def test_cached_scan_results_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            root.mkdir()
            (root / "file").write_bytes(b"x" * 8192)
            config = {"data_dir": directory, "docker_socket": "/unused"}
            projects = [{"id": "a", "worktrees": [{"id": "w", "branch": "main", "path": str(root)}]}]
            usage = DiskUsage(config)
            with patch("sm_manager.storage.docker_usage", return_value={"available": False}):
                usage._scan(projects, "a")
            self.assertGreater(usage.status({"a"})["projects"]["a"]["bytes"], 0)
            self.assertEqual(DiskUsage(config).status({"a"}), usage.status({"a"}))
            self.assertEqual(usage.status({"other"})["projects"], {})


class FeatureAccessTests(unittest.TestCase):
    def test_storage_and_month_history_keep_access_boundaries(self):
        collector = Mock()
        collector.get_snapshot.return_value = {"stale": False, "timestamp": time.time(), "projects": [{"id": "known", "worktrees": []}]}
        collector.disk_usage.status.return_value = {"projects": {}, "scanning": None}
        collector.disk_usage.start.return_value = True
        collector.history.query.return_value = {"hours": 0.25, "points": []}
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(collector, {"allowed_hosts": ["localhost"], "port": 0}))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(path, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request("GET", path, headers={"Host": "localhost", **(headers or {})})
            response = connection.getresponse()
            result = response.status, response.read()
            connection.close()
            return result
        try:
            self.assertEqual(request("/api/disk-usage?scan=1&project=/etc")[0], 404)
            self.assertEqual(request("/api/disk-usage?scan=1&project=known", {"Origin": "https://evil.test"})[0], 403)
            collector.disk_usage.start.assert_not_called()
            self.assertEqual(request("/api/disk-usage?scan=1&project=known")[0], 202)
            collector.disk_usage.start.assert_called_once()
            collector.disk_usage.start.return_value = False
            self.assertEqual(request("/api/disk-usage?scan=1&project=known")[0], 409)
            self.assertEqual(request("/api/disk-usage")[0], 200)
            self.assertEqual(request("/api/history")[0], 200)
            collector.history.query.assert_called_with("host", 24)
            for hours in (0.25, 24, 25, 168, 720):
                with self.subTest(hours=hours):
                    self.assertEqual(request(f"/api/history?hours={hours}&entity=worktree:w")[0], 200)
                    collector.history.query.assert_called_with("worktree:w", hours)
            for hours in ("0", "0.24", "720.01", "721", "nan", "inf", "-inf", "bad"):
                status, body = request("/api/history?hours=" + hours)
                self.assertEqual(status, 400)
                self.assertIn("0.25..720", json.loads(body)["error"])
            self.assertEqual(request("/api/history?hours=720&entity=" + "x" * 101)[0], 400)
            collector.history.query.reset_mock()
            for headers in ({"Host": "evil.test"}, {"Origin": "https://evil.test"}, {"Sec-Fetch-Site": "cross-site"}):
                self.assertEqual(request("/api/history?hours=720", headers)[0], 403)
            collector.history.query.assert_not_called()
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__":
    unittest.main()
