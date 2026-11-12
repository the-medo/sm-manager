import http.client
import json
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

from sm_manager.config import load_config
from sm_manager.dbs import STALE_AFTER_SECONDS, launch_url, read_db_catalog
from sm_manager.server import handler_for


LAUNCHER = "https://db.the-medo.com/cloudbeaver/open"
URL = LAUNCHER + "?id=postgres-jdbc-example"


class DbCatalogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "connections.json"
        self.now = datetime.now(timezone.utc)
        self.owner = {
            "group": "group", "repository": "repo", "worktree": "feature", "branch": "main",
            "primary_path": "/repo", "worktree_path": "/trees/feature",
        }
        self.db = {
            "name": "container", "database": "app", "engine": "PostgreSQL", "source": "docker",
            "id": "catalog-id", "host": "localhost", "port": 5432, "status": "available",
            "owner": self.owner, "cloudbeaver_url": URL,
        }

    def publish(self, rows, **fields):
        data = {"schema_version": 1, "checked_at": self.now.isoformat(), "connections": rows, **fields}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.path)

    def test_config_accepts_custom_catalog_and_expands_default(self):
        self.assertEqual(load_config(self.path)["db_catalog_path"], str(Path.home() / "db-projects/connections.json"))
        self.path.write_text('{"db_catalog_path":"~/alternate/dbs.json"}')
        self.assertEqual(load_config(self.path)["db_catalog_path"], str(Path.home() / "alternate/dbs.json"))

    def test_offline_statuses_and_unassigned(self):
        rows = [dict(self.db, owner=None, secret="unpublished")]
        rows += [dict(self.db, id=status, status=status) for status in ("configured", "stopped", "unreachable")]
        self.publish(rows)
        catalog = read_db_catalog(self.path)
        self.assertFalse(catalog["stale"])
        unassigned, *offline = catalog["connections"]
        self.assertIsNone(unassigned["owner"])
        self.assertEqual(unassigned["cloudbeaver_url"], URL)
        self.assertNotIn("secret", unassigned)
        self.assertEqual([db["status"] for db in offline], ["configured", "stopped", "unreachable"])
        self.assertTrue(all(db["cloudbeaver_url"] is None and db["owner"] == self.owner for db in offline))

    def test_stale_catalog_preserves_rows_without_launch_urls(self):
        old = (self.now - timedelta(seconds=STALE_AFTER_SECONDS)).isoformat()
        self.publish([self.db], checked_at=old)
        catalog = read_db_catalog(self.path)
        self.assertTrue(catalog["stale"])
        self.assertEqual(catalog["fresh_for_seconds"], 0)
        self.assertEqual(catalog["checked_at"], old)
        self.assertEqual(catalog["connections"], [dict(self.db, cloudbeaver_url=None)])
        self.publish([self.db])
        fresh = read_db_catalog(self.path)
        self.assertGreater(fresh["fresh_for_seconds"], 0)
        self.assertEqual(fresh["connections"], [self.db])

    def test_external_group_preserves_name_and_launch_rules_without_checkout_ownership(self):
        external = dict(self.db, group="Demo DEV", name="dev-example", owner=None, group_checked_at=self.now.isoformat())
        self.publish([external, dict(external, id="offline", status="configured")])
        online, offline = read_db_catalog(self.path)["connections"]
        self.assertEqual(online, external)
        self.assertIsNone(offline["cloudbeaver_url"])

    def test_only_cloudbeaver_https_open_urls_with_one_id(self):
        for url in (URL, LAUNCHER + "?id=connection%3Aexample", URL.replace(".com/", ".com:443/")):
            self.assertEqual(launch_url(url), url)
        for url in (
            None, 23, "", "javascript:alert(1)", URL[6:], URL.replace("https:", "http:"),
            URL.replace(".com/", ".com.evil.test/"), URL.replace(".com/", ".com:444/"),
            URL.replace(".com/", ".com@evil.test/"), URL.replace("db.the", "user@db.the"),
            URL.replace("db.the", "evil.test\\@db.the"), URL.replace("db.the", "db.\nthe"),
            URL.replace("/open", "/other"), LAUNCHER, LAUNCHER + "?id=", LAUNCHER + "?id=%20",
            LAUNCHER + "?id=%00", LAUNCHER + "?other=x", URL + "&id=second", URL + "#other",
        ):
            with self.subTest(url=url):
                self.assertIsNone(launch_url(url))
        self.publish([dict(self.db, cloudbeaver_url="https://other.test/")])
        self.assertIsNone(read_db_catalog(self.path)["connections"][0]["cloudbeaver_url"])

    def test_api_distinguishes_missing_invalid_and_empty_and_checks_origin(self):
        config = {"port": 0, "allowed_hosts": ["localhost"], "db_catalog_path": str(self.path)}
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(Mock(), config))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def request(**headers):
            with closing(http.client.HTTPConnection("127.0.0.1", server.server_port)) as client:
                client.request("GET", "/api/dbs", headers={"Host": "localhost", **headers})
                response = client.getresponse()
                return response.status, json.loads(response.read())

        status, missing = request()
        self.assertEqual(status, 503)
        self.path.write_text("{")
        status, malformed = request()
        self.assertEqual(status, 503)
        self.assertNotEqual(malformed["error"], missing["error"])
        for fields in [
            {"schema_version": True}, {"connections": {}}, {"checked_at": "yesterday"},
            {"checked_at": "2026-01-01"}, {"checked_at": (self.now + timedelta(days=1)).isoformat()},
            {"connections": [dict(self.db, owner={})]}, {"connections": [None]},
        ]:
            with self.subTest(fields=fields):
                self.publish([], **fields)
                self.assertEqual(request(), (503, malformed))
        self.publish([])
        self.assertEqual(request(Host="other.test")[0], 403)
        self.assertEqual(request(Origin="https://other.test")[0], 403)
        self.assertEqual(request(**{"Sec-Fetch-Site": "cross-site"})[0], 403)
        status, empty = request()
        self.assertEqual(status, 200)
        self.assertFalse(empty["stale"])
        self.assertEqual(empty["connections"], [])
