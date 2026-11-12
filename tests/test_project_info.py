import http.client
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from sm_manager.discovery import Discovery
from sm_manager.project_info import MAX_ICON_BYTES, candidates, inspect_project, read_local, repository_url
from sm_manager.server import handler_for

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M0 0h24v24H0z"/></svg>'


class ProjectInfoTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, path, data):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data if isinstance(data, bytes) else data.encode())
        return target

    def test_manifest_and_html_icon_discovery_in_frontend(self):
        self.write('package.json', json.dumps({'description': 'A local app', 'version': '1.2.3'}))
        self.write('frontend/index.html', '<link rel="shortcut icon" href="/assets/brand.svg?v=1">')
        self.write('frontend/public/assets/brand.svg', SVG)
        self.write('favicon.ico', b'\0\0\1\0fallback')
        info, icon = inspect_project(self.root)
        self.assertEqual(info['description'], 'A local app')
        self.assertEqual(info['version'], '1.2.3')
        self.assertEqual(info['languages'], ['Node.js'])
        self.assertEqual(icon['data'], SVG)
        self.assertEqual(icon['mime'], 'image/svg+xml')

    def test_python_metadata_and_local_favicon_work_without_running_app(self):
        self.write('pyproject.toml', '[project]\ndescription="Local monitor"\nversion="0.1.0"')
        self.write('monitor/static/favicon.svg', SVG)
        info, icon = inspect_project(self.root)
        self.assertEqual(info['languages'], ['Python'])
        self.assertEqual(info['description'], 'Local monitor')
        self.assertIsNotNone(icon)

    def test_missing_invalid_and_oversized_metadata_are_optional(self):
        self.write('package.json', '[' * 1500)
        self.write('pyproject.toml', 'not valid toml')
        self.write('favicon.svg', b'<html>not an image</html>')
        self.write('favicon.png', b'\x89PNG\r\n\x1a\n' + b'x' * MAX_ICON_BYTES)
        info, icon = inspect_project(self.root)
        self.assertEqual(info['description'], '')
        self.assertEqual(info['languages'], [])
        self.assertIsNone(icon)

    def test_html_description_and_large_local_brand_image(self):
        self.write('index.html', '<meta name="description" content="An &amp; app"><link rel="icon" href="/src/assets/mark.png">')
        data = b'\x89PNG\r\n\x1a\n' + b'x' * 600000
        self.write('src/assets/mark.png', data)
        info, icon = inspect_project(self.root)
        self.assertEqual(info['description'], 'An & app')
        self.assertEqual(icon['data'], data)

    def test_symlinks_traversal_hidden_and_special_files_are_not_read(self):
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside)
            (external / 'private.svg').write_bytes(SVG)
            (self.root / 'favicon.svg').symlink_to(external / 'private.svg')
            (self.root / 'assets').symlink_to(external, target_is_directory=True)
            self.write('index.html', '<link rel="icon" href="/assets/private.svg">')
            self.assertIsNone(inspect_project(self.root)[1])
            self.assertIsNone(read_local(self.root, self.root / 'assets/private.svg'))
            self.assertIsNone(read_local(self.root, self.root / '../private.svg'))
            self.assertIsNone(read_local(self.root, external / 'private.svg'))
        self.write('.secret.svg', SVG)
        self.assertIsNone(read_local(self.root, self.root / '.secret.svg'))
        os.mkfifo(self.root / 'pipe.svg')
        self.assertIsNone(read_local(self.root, self.root / 'pipe.svg'))

    def test_external_icons_and_svg_entities_are_not_loaded(self):
        self.write('index.html', '<link rel="icon" href="https://example.test/icon.svg"><link rel="icon" href="//example.test/icon.svg"><link rel="icon" href="file:///etc/passwd">')
        self.write('favicon.svg', '<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg>&x;</svg>')
        self.assertIsNone(inspect_project(self.root)[1])

    def test_unknown_svg_encoding_does_not_interrupt_discovery(self):
        self.write('favicon.svg', '<?xml version="1.0" encoding="unknown-encoding"?><svg/>')
        info, icon = inspect_project(self.root)
        self.assertIsNone(icon)
        self.assertEqual(info['path'], str(self.root))

    def test_scans_skip_dependencies_and_obey_directory_budget(self):
        self.write('node_modules/dependency/favicon.svg', SVG)
        self.write('dist/favicon.svg', SVG)
        self.write('frontend/public/favicon.svg', SVG)
        with patch('sm_manager.project_info.MAX_DIRECTORIES', 1):
            self.assertEqual(candidates(self.root), [])
        self.assertEqual(candidates(self.root), [self.root / 'frontend/public/favicon.svg'])

    def test_repository_links_do_not_expose_credentials_or_executable_urls(self):
        for remote in ('git@github.com:example/app.git', 'ssh://git@github.com/example/app.git',
                       'https://user:secret@github.com/example/app.git?token=secret#secret'):
            self.assertEqual(repository_url(remote), 'https://github.com/example/app')
        for remote in ('/private/repository', 'file:///private/repository', 'javascript:alert(1)',
                       'https://example.test:bad/app', 'https://example.test/\napp'):
            self.assertEqual(repository_url(remote), '')

    def test_discovery_prefers_main_refreshes_icons_and_removes_stale_metadata(self):
        repository = self.root / 'project'
        repository.mkdir()
        def git(*args):
            return subprocess.run(['git', '-C', str(repository), *args], check=True, capture_output=True)
        git('init', '-b', 'main')
        self.write('project/package.json', '{"description":"Primary checkout"}')
        self.write('project/favicon.svg', SVG)
        git('add', '.')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'fixture')
        git('remote', 'add', 'origin', 'https://user:token@example.test/app.git')
        git('worktree', 'add', '-b', 'feature', str(self.root / 'feature'))
        self.write('feature/package.json', '{"description":"Other branch"}')
        discovery = Discovery({'roots': [str(repository)], 'codex_home': str(self.root / 'missing'), 'scan_depth': 2})
        discovery.refresh()
        self.assertEqual(len(discovery.project_info), 1)
        pid, info = next(iter(discovery.project_info.items()))
        self.assertEqual(info['description'], 'Primary checkout')
        self.assertEqual(info['primary_path'], str(repository))
        self.assertEqual(info['repository_url'], 'https://example.test/app')
        first_url = info['icon_url']
        self.write('project/favicon.svg', SVG.replace(b'24', b'32'))
        discovery.refresh()
        self.assertNotEqual(discovery.project_info[pid]['icon_url'], first_url)
        (repository / 'favicon.svg').unlink()
        discovery.refresh()
        self.assertEqual(discovery.project_info[pid]['icon_url'], '')
        self.assertEqual(discovery.project_icons, {})
        with patch('sm_manager.discovery.run', return_value=''):
            discovery.refresh()
        self.assertEqual(discovery.project_info, {})


class ProjectIconAccessTests(unittest.TestCase):
    def test_icon_route_requires_discovered_id_version_and_same_origin(self):
        collector = Mock()
        collector.get_snapshot.return_value = {'stale': False, 'timestamp': time.time(), 'projects': [{'id': 'known'}]}
        collector.get_project_icon.side_effect = lambda pid, version: {'data': SVG, 'mime': 'image/svg+xml'} if (pid, version) == ('known', 'current') else None
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(collector, {'allowed_hosts': ['localhost'], 'port': 0}))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(path, headers=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
            connection.request('GET', path, headers={'Host': 'localhost', **(headers or {})})
            response = connection.getresponse()
            result = response.status, dict(response.getheaders()), response.read()
            connection.close()
            return result
        try:
            status, headers, body = request('/api/project-icon/known/current')
            self.assertEqual((status, body), (200, SVG))
            self.assertEqual(headers['Content-Type'], 'image/svg+xml')
            self.assertIn('private', headers['Cache-Control'])
            self.assertIn("sandbox; default-src 'none'", headers['Content-Security-Policy'])
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
            for path in ('/api/project-icon/unknown/current', '/api/project-icon/known/old',
                         '/api/project-icon/known/current/extra', '/api/project-icon/../../etc/passwd',
                         '/api/project-icon/known', '/api/project-icon/%2e%2e/current'):
                self.assertEqual(request(path)[0], 404)
            collector.get_project_icon.reset_mock()
            for headers in ({'Host': 'evil.test'}, {'Origin': 'https://evil.test'}, {'Sec-Fetch-Site': 'cross-site'}):
                self.assertEqual(request('/api/project-icon/known/current', headers)[0], 403)
            collector.get_project_icon.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
