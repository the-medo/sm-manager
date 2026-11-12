import http.client
import json
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

from sm_manager.server import handler_for
from sm_manager.zed import read_catalog


class ZedTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.catalog_path = self.root / "published.json"
        self.catalog = {"schema_version": 1, "ssh_base_url": "zed://ssh/base/", "groups": []}

    def publish(self, repositories):
        self.catalog["groups"] = [{"name": "family", "repositories": repositories}]
        self.catalog_path.write_text(json.dumps(self.catalog))

    def tree(self, name, path, **fields):
        return {"name": name, "path": str(path), "branch": "shared", "is_main": False,
                "head": None, "zed_url": "zed://ssh/alias/published/" + name, **fields}

    def test_duplicate_branches_keep_published_urls_and_idle_checkouts(self):
        a = self.tree("one", self.root / "repo1")
        b = self.tree("two", self.root / "repo2")
        idle = self.tree("idle", self.root / "repo1-linked")
        self.publish([{"name": "repo1", "primary_path": a["path"], "worktrees": [idle, a]},
                      {"name": "repo2", "primary_path": b["path"], "worktrees": [b]}])
        data = read_catalog(self.catalog_path, [a["path"], b["path"]])
        repos = data["groups"][0]["repositories"]
        trees = [w for repo in repos for w in repo["worktrees"]]
        self.assertEqual([w["name"] for w in trees], ["one", "idle", "two"])
        self.assertEqual([w["is_main"] for w in trees], [True, False, True])
        self.assertEqual({w["path"]: w["zed_url"] for w in trees},
                         {w["path"]: w["zed_url"] for w in [a, b, idle]})
        self.assertEqual(data["monitor_only"], [])

    def test_fallback_only_for_existing_uncatalogued_absolute_directories(self):
        known = self.root / "main"
        extra = self.root / "linked trees" / "café #1?%+"
        known.mkdir()
        extra.mkdir(parents=True)
        file = self.root / "not-a-directory"
        file.touch()
        self.publish([{"name": "repo", "worktrees": [self.tree("main", known)]}])
        paths = [str(known), str(extra), str(extra), str(self.root / "missing"), str(file), "."]
        data = read_catalog(self.catalog_path, paths)
        self.assertEqual(data["monitor_only"], [{
            "name": extra.name, "path": str(extra),
            "zed_url": f"zed://ssh/base{self.root}/linked%20trees/caf%C3%A9%20%231%3F%25%2B",
        }])

    def test_detached_head_missing_revision_and_invalid_urls(self):
        first = self.tree("detached", self.root / "a", branch=None, head="0123456789abcdef")
        second = self.tree("unknown", self.root / "b", branch=None, zed_url="javascript:void(0)")
        self.catalog.pop("ssh_base_url")
        self.publish([{"name": "repo", "worktrees": [first, second]}])
        data = read_catalog(self.catalog_path, [str(self.root)])
        detached, unknown = data["groups"][0]["repositories"][0]["worktrees"]
        self.assertEqual(detached["revision"], "0123456 (detached)")
        self.assertEqual(detached["zed_url"], first["zed_url"])
        self.assertEqual(unknown["revision"], "Revision unavailable")
        self.assertIsNone(unknown["zed_url"])
        self.assertEqual(data["monitor_only"], [])

    def test_api_recovers_from_missing_invalid_and_replaced_files(self):
        config = {"allowed_hosts": ["localhost"], "port": 0, "zed_catalog_path": str(self.catalog_path)}
        collector = Mock()
        collector.get_snapshot.return_value = None  # Catalog is available during warm-up.
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(collector, config))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def request(host="localhost"):
            with closing(http.client.HTTPConnection("127.0.0.1", server.server_port)) as conn:
                conn.request("GET", "/api/zed", headers={"Host": host})
                response = conn.getresponse()
                return response.status, json.loads(response.read())

        self.assertEqual(request()[0], 503)
        for invalid in ["{", "null", '{"schema_version":2}', '{"schema_version":1,"groups":[null]}']:
            self.catalog_path.write_text(invalid)
            self.assertEqual(request()[0], 503)
        self.publish([])
        self.assertEqual(request("other.example")[0], 403)
        status, data = request()
        self.assertEqual(status, 200)
        self.assertEqual(data["groups"][0]["repositories"], [])
        self.publish([{"name": "new-repo", "worktrees": []}])
        collector.get_snapshot.return_value = {"projects": [{"id": "p", "worktrees": []}]}
        collector.disk_usage.status.return_value = {"projects": {"p": {"worktrees": [{"path": str(self.root)}]}}}
        status, data = request()
        self.assertEqual(status, 200)
        self.assertEqual(data["groups"][0]["repositories"][0]["name"], "new-repo")
        self.assertEqual(data["monitor_only"][0]["path"], str(self.root))
