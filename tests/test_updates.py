import contextlib
import fcntl
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import manage, updater


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote"
        self.remote.mkdir()
        updater.git(self.remote, "init", "-b", "main")
        updater.git(self.remote, "config", "user.name", "Test")
        updater.git(self.remote, "config", "user.email", "test@example.invalid")
        (self.remote / "sm_manager").mkdir()
        (self.remote / "sm_manager/__main__.py").write_text("")
        (self.remote / "tests").mkdir()
        (self.remote / "tests/test_app.py").write_text(
            "import os, unittest\nfrom pathlib import Path\n"
            "class AppTest(unittest.TestCase):\n"
            "    def test_app(self):\n"
            "        self.assertFalse(Path('broken').exists())\n"
            "        self.assertTrue(os.environ['CODEX_HOME'].startswith(os.environ['HOME']))\n")
        (self.remote / ".gitignore").write_text("__pycache__/\n")
        self.commit("initial")
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"port": 8790}))
        self.directory = self.root / "deployment"
        updater.provision(self.directory, str(self.remote), self.config)
        self.deployment = updater.Deployment(self.directory)
        self.previous = self.deployment.check_checkout()
        self.calls = []
        self.active = True
        self.service_patch = patch.object(self.deployment, "service", side_effect=self.service)
        self.service_patch.start()
        self.addCleanup(self.service_patch.stop)
        self.health_patch = patch.object(self.deployment, "healthy", return_value=True)
        self.health = self.health_patch.start()
        self.addCleanup(self.health_patch.stop)

    def service(self, command, check=True):
        self.calls.append(command)
        if command == "stop":
            self.active = False
        elif command == "start":
            # Model ExecStartPre re-entry while the timer still owns its lock.
            self.assertFalse(updater.Deployment(self.directory).refresh(prepare=True))
            self.active = True
        return subprocess.CompletedProcess([], 0 if command != "is-active" or self.active else 3)

    def commit(self, name):
        updater.git(self.remote, "add", ".")
        updater.git(self.remote, "commit", "-m", name)
        return updater.git(self.remote, "rev-parse", "HEAD").stdout.strip()

    def new_main(self, broken=False):
        (self.remote / ("broken" if broken else "version")).write_text("new\n")
        return self.commit("new main")

    def test_update_checks_out_remote_main_and_restarts_once(self):
        target = self.new_main()
        self.assertTrue(self.deployment.refresh())
        self.assertEqual(self.deployment.check_checkout(), target)
        self.assertEqual(self.calls, ["is-active", "stop", "start"])
        self.health.assert_called_once()
        self.calls.clear()
        self.assertFalse(self.deployment.refresh())
        self.assertEqual(self.calls, [])

    def test_prepare_fetches_before_boot_without_systemctl(self):
        target = self.new_main()
        self.assertTrue(self.deployment.refresh(prepare=True))
        self.assertEqual(self.deployment.check_checkout(), target)
        self.assertEqual(self.calls, [])

    def test_failed_tests_leave_running_version_untouched(self):
        self.new_main(broken=True)
        with self.assertRaisesRegex(RuntimeError, "FAILED"):
            self.deployment.refresh()
        self.assertEqual(self.deployment.check_checkout(), self.previous)
        self.assertEqual(self.calls, [])

    def test_offline_update_retains_runtime_and_prepare_allows_boot(self):
        self.remote.rename(self.root / "offline")
        with self.assertRaises(RuntimeError):
            self.deployment.refresh()
        self.assertEqual(self.deployment.check_checkout(), self.previous)
        with patch.object(sys, "argv", ["updater.py", "prepare", "--directory", str(self.directory)]):
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                updater.main()
        self.assertIn("starting the installed version", stderr.getvalue())
        self.assertEqual(self.calls, [])

    def test_unhealthy_update_restores_previous_revision(self):
        self.new_main()
        self.health.return_value = False
        with self.assertRaisesRegex(RuntimeError, "did not become healthy"):
            self.deployment.refresh()
        self.assertEqual(self.deployment.check_checkout(), self.previous)
        self.assertEqual(self.calls, ["is-active", "stop", "start", "stop", "start"])
        self.assertTrue(self.active)

    def test_manually_stopped_app_stays_stopped(self):
        self.new_main()
        self.active = False
        self.deployment.refresh()
        self.assertEqual(self.calls, ["is-active"])
        self.health.assert_not_called()

    def test_dirty_runtime_is_not_overwritten(self):
        self.new_main()
        note = self.deployment.checkout / "local-note"
        note.write_text("keep this")
        with self.assertRaisesRegex(RuntimeError, "local changes"):
            self.deployment.refresh()
        self.assertEqual(note.read_text(), "keep this")
        self.assertEqual(self.calls, [])

    def test_other_branches_and_changed_origins_are_rejected(self):
        updater.git(self.deployment.checkout, "checkout", "-b", "feature")
        with self.assertRaisesRegex(RuntimeError, "must stay on main"):
            self.deployment.refresh()
        updater.git(self.deployment.checkout, "checkout", "main")
        updater.git(self.deployment.checkout, "remote", "set-url", "origin", str(self.root / "other"))
        with self.assertRaisesRegex(RuntimeError, "origin changed"):
            self.deployment.refresh()
        self.assertEqual(self.calls, [])

    def test_remote_rewind_is_rejected(self):
        self.new_main()
        self.deployment.refresh(prepare=True)
        current = self.deployment.check_checkout()
        updater.git(self.remote, "reset", "--hard", self.previous)
        with self.assertRaisesRegex(RuntimeError, "not a fast-forward"):
            self.deployment.refresh()
        self.assertEqual(self.deployment.check_checkout(), current)
        self.assertEqual(self.calls, [])

    def test_concurrent_update_is_rejected_and_prestart_does_not_deadlock(self):
        with (self.directory / "update.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaisesRegex(RuntimeError, "already in progress"):
                self.deployment.refresh()
            self.assertFalse(self.deployment.refresh(prepare=True))
        self.assertEqual(self.calls, [])

    def test_installed_helper_does_not_depend_on_source_checkout(self):
        installed = self.directory / "updater.py"
        result = subprocess.run([sys.executable, str(installed), "prepare"], cwd=self.root,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(self.previous[:12], result.stdout)

    def test_unrelated_deployment_directory_is_preserved(self):
        other = self.root / "unrelated"
        other.mkdir()
        with self.assertRaisesRegex(RuntimeError, "unrelated deployment"):
            updater.provision(other, str(self.remote), self.config)
        self.assertEqual(list(other.iterdir()), [])


class ManageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.unit = self.root / manage.SERVICE
        self.config = {"roots": [], "port": 8790, "data_dir": str(self.root / "data"),
                       "public_url": "https://private.example.invalid",
                       "https_mapping": {"service": "existing-proxy.service"}}
        for name, value in (("UNIT", self.unit), ("config_path", lambda: self.root / "config.json"),
                            ("load_config", lambda: self.config.copy())):
            patcher = patch.object(manage, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_install_preserves_private_access_and_uses_installed_helper(self):
        def command(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, "git@example.invalid:app.git\n", "")
        with patch.object(manage, "run", side_effect=command) as commands:
            with patch.object(updater, "provision", return_value=self.root / "runtime") as provision:
                with patch.object(manage, "serve_state", side_effect=AssertionError("No route changes")):
                    manage.install(False, auto_update=True)
        written = self.unit.read_text()
        self.assertIn(f'WorkingDirectory={self.root}/runtime', written)
        self.assertIn("updater.py\" prepare", written)
        self.assertIn("--config", written)
        self.assertIn("OnUnitInactiveSec=5min", self.unit.with_name(manage.UPDATE_TIMER).read_text())
        self.assertIn("updater.py\" update", self.unit.with_name(manage.UPDATE_SERVICE).read_text())
        saved = json.loads((self.root / "config.json").read_text())
        self.assertEqual(saved["public_url"], self.config["public_url"])
        self.assertEqual(saved["https_mapping"], self.config["https_mapping"])
        self.assertTrue(any(call.args[0][:2] == ["loginctl", "enable-linger"] for call in commands.call_args_list))
        provision.assert_called_once()
        if shutil.which("systemd-analyze"):
            result = subprocess.run(["systemd-analyze", "--user", "verify", str(self.unit),
                                     str(self.unit.with_name(manage.UPDATE_SERVICE)),
                                     str(self.unit.with_name(manage.UPDATE_TIMER))],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_install_refuses_to_overwrite_unrelated_timer(self):
        timer = self.unit.with_name(manage.UPDATE_TIMER)
        timer.write_text("unrelated")
        with patch.object(manage, "run") as commands:
            with self.assertRaisesRegex(RuntimeError, "unrelated unit"):
                manage.install(False, auto_update=True)
        commands.assert_not_called()
        self.assertEqual(timer.read_text(), "unrelated")

    def test_uninstall_removes_owned_timer_and_retains_deployment(self):
        deployment = self.root / "deployment"
        deployment.mkdir()
        (deployment / "keep").write_text("runtime")
        self.config["deployment_dir"] = str(deployment)
        for name in (manage.SERVICE, manage.UPDATE_SERVICE, manage.UPDATE_TIMER):
            self.unit.with_name(name).write_text(manage.MARKER)
        with patch.object(manage, "run") as commands:
            manage.uninstall()
        self.assertEqual((deployment / "keep").read_text(), "runtime")
        self.assertFalse(self.unit.with_name(manage.UPDATE_TIMER).exists())
        self.assertNotIn("deployment_dir", json.loads((self.root / "config.json").read_text()))
        commands.assert_any_call(["systemctl", "--user", "disable", "--now", manage.UPDATE_TIMER])


if __name__ == "__main__":
    unittest.main()
