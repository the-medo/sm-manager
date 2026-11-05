#!/usr/bin/env python3
"""Own only the SM Manager service, config, and optional private Serve route."""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sm_manager.config import config_path, load_config  # noqa: E402
from scripts import updater  # noqa: E402

SERVICE = "sm-manager.service"
UNIT = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user" / SERVICE
UPDATE_SERVICE = "sm-manager-update.service"
UPDATE_TIMER = "sm-manager-update.timer"
MARKER = "# Managed by sm-manager/scripts/manage.py"


def run(args, check=True, capture=False, timeout=30):
    return subprocess.run(args, check=check, text=True, capture_output=capture, timeout=timeout)


def tailscale():
    executable = shutil.which("tailscale")
    if not executable:
        raise RuntimeError("tailscale is not on PATH; add its installation directory for this command")
    return executable


def serve_state():
    return json.loads(run([tailscale(), "serve", "status", "--json"], capture=True).stdout or "{}")


def save(config):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def unit_quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def check_units():
    for path in (UNIT, UNIT.with_name(UPDATE_SERVICE), UNIT.with_name(UPDATE_TIMER)):
        if path.exists() and MARKER not in path.read_text():
            raise RuntimeError(f"Refusing to overwrite an unrelated unit: {path}")


def install(private, auto_update=False):
    check_units()
    config = load_config()
    config["roots"] = list(dict.fromkeys([*config["roots"], str(ROOT)]))
    port = config["port"]
    active = run(["systemctl", "--user", "is-active", "--quiet", SERVICE], check=False).returncode == 0
    if not active:
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as error:
                raise RuntimeError(f"Port {port} is occupied. Set a free port in {config_path()}.") from error
    deployment = config.get("deployment_dir")
    runtime = ROOT
    if auto_update or deployment:
        deployment = Path(deployment or Path(config["data_dir"]) / "deployment").resolve()
        remote = run(["git", "-C", str(ROOT), "remote", "get-url", "origin"], capture=True).stdout.strip()
        runtime = updater.provision(deployment, remote, config_path().resolve())
        config["deployment_dir"] = str(deployment)
        # Enabled user units alone start only at login. Lingering starts the
        # user manager during boot, including on an unattended machine.
        run(["loginctl", "enable-linger", str(os.getuid())])
    if private:
        binary = tailscale()
        state = json.loads(run([binary, "status", "--json"], capture=True).stdout)
        hostname = state.get("Self", {}).get("DNSName", "").rstrip(".")
        if not hostname:
            raise RuntimeError("Tailscale has no DNS hostname; connect it before installing private access")
        host = f"{hostname}:{port}"
        target = f"http://127.0.0.1:{port}"
        existing = serve_state()
        expected = {"Handlers": {"/": {"Proxy": target}}}
        recorded = config.get("serve_mapping", {})
        occupied = str(port) in existing.get("TCP", {}) or host in existing.get("Web", {})
        if occupied and not (recorded == {"host": host, "target": target, "port": port} and existing.get("Web", {}).get(host) == expected):
            raise RuntimeError(f"Serve port {port} already belongs to another route. Choose a different port.")
        config["allowed_hosts"] = list(dict.fromkeys([*config["allowed_hosts"], hostname]))
        config["public_url"] = f"https://{host}"
        run([binary, "serve", "--bg", f"--https={port}", target])
        config["serve_mapping"] = {"host": host, "target": target, "port": port}
    UNIT.parent.mkdir(parents=True, exist_ok=True)
    save(config)
    service_path = ":".join(dict.fromkeys([
        *(str(Path(executable).parent) for name in ("git", "ss", "tailscale") if (executable := shutil.which(name))),
        "/usr/local/bin", "/usr/bin", "/bin",
    ]))
    prepare = (f"ExecStartPre={unit_quote(sys.executable)} {unit_quote(deployment / 'updater.py')} prepare\n"
               if deployment else "")
    unit = f'''{MARKER}
[Unit]
Description=SM Manager local project resource dashboard

[Service]
Type=simple
WorkingDirectory={str(runtime).replace('%', '%%')}
{prepare}ExecStart={unit_quote(sys.executable)} -m sm_manager --config {unit_quote(config_path().resolve())} serve
Restart=on-failure
RestartSec=5
UMask=0077
Environment={unit_quote('PATH=' + service_path)}
NoNewPrivileges=true
TimeoutStopSec=10
TimeoutStartSec=5min

[Install]
WantedBy=default.target
'''
    UNIT.write_text(unit)
    if deployment:
        UNIT.with_name(UPDATE_SERVICE).write_text(f'''{MARKER}
[Unit]
Description=Check main for SM Manager updates

[Service]
Type=oneshot
ExecStart={unit_quote(sys.executable)} {unit_quote(deployment / 'updater.py')} update
Environment={unit_quote('PATH=' + service_path)}
UMask=0077
NoNewPrivileges=true
TimeoutStartSec=6min
''')
        UNIT.with_name(UPDATE_TIMER).write_text(f'''{MARKER}
[Unit]
Description=Check main for SM Manager updates every five minutes

[Timer]
OnStartupSec=5min
OnUnitInactiveSec=5min
AccuracySec=15s
Unit={UPDATE_SERVICE}

[Install]
WantedBy=timers.target
''')
    units = [UNIT]
    if deployment:
        units += [UNIT.with_name(UPDATE_SERVICE), UNIT.with_name(UPDATE_TIMER)]
    run(["systemd-analyze", "--user", "verify", *(str(path) for path in units)])
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", SERVICE])
    run(["systemctl", "--user", "restart", SERVICE], timeout=360)
    if deployment:
        run(["systemctl", "--user", "enable", "--now", UPDATE_TIMER])
    print(f"Dashboard: {config.get('public_url', f'http://127.0.0.1:{port}')}")
    print(f"Local configuration: {config_path()}")
    if deployment:
        print(f"Boot startup and five-minute main updates enabled. Runtime: {runtime}")
    else:
        print("Service enabled. User services start at login; lingering allows startup before login.")


def uninstall():
    check_units()
    if not UNIT.exists():
        raise RuntimeError("No SM Manager-owned unit found; nothing changed")
    config = load_config()
    route = config.get("serve_mapping")
    if route:
        current = serve_state()
        expected = {"Handlers": {"/": {"Proxy": route["target"]}}}
        if current.get("Web", {}).get(route["host"]) == expected:
            run([tailscale(), "serve", f"--https={route['port']}", "off"])
        else:
            print("Serve mapping has changed; leaving it untouched.")
    if UNIT.with_name(UPDATE_TIMER).exists():
        run(["systemctl", "--user", "disable", "--now", UPDATE_TIMER])
    if UNIT.with_name(UPDATE_SERVICE).exists():
        run(["systemctl", "--user", "stop", UPDATE_SERVICE])
    run(["systemctl", "--user", "disable", "--now", SERVICE])
    UNIT.unlink()
    UNIT.with_name(UPDATE_TIMER).unlink(missing_ok=True)
    UNIT.with_name(UPDATE_SERVICE).unlink(missing_ok=True)
    run(["systemctl", "--user", "daemon-reload"])
    config.pop("serve_mapping", None)
    config.pop("public_url", None)
    config.pop("deployment_dir", None)
    save(config)
    print("SM Manager service and updater removed. Configuration, history, and runtime checkout retained.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["install", "start", "stop", "restart", "status", "logs", "update", "uninstall"])
    parser.add_argument("--tailscale", action="store_true", help="Install a private Tailscale Serve route")
    parser.add_argument("--auto-update", action="store_true", help="Run main from a separate clone; update at boot and every five minutes")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "install":
            install(args.tailscale, args.auto_update)
        elif args.command == "uninstall":
            uninstall()
        elif args.command == "logs":
            os.execvp("journalctl", ["journalctl", "--user", "-u", SERVICE, "-u", UPDATE_SERVICE, "-n", "70", "--no-pager"])
        elif args.command == "update":
            deployment = load_config().get("deployment_dir")
            if not deployment:
                raise RuntimeError("Automatic updates are not installed. Run install --auto-update first.")
            run([sys.executable, str(Path(deployment) / "updater.py"), "update"], timeout=360)
        elif args.command == "status":
            config = load_config()
            print(config.get("public_url", f"http://127.0.0.1:{config['port']}"), flush=True)
            result = run(["systemctl", "--user", "status", SERVICE, "--no-pager"], check=False)
            if deployment := config.get("deployment_dir"):
                revision = updater.git(Path(deployment) / "checkout", "rev-parse", "--short", "HEAD").stdout.strip()
                print(f"Runtime main: {revision} ({deployment}/checkout)", flush=True)
                run(["systemctl", "--user", "status", UPDATE_TIMER, "--no-pager"], check=False)
            sys.exit(result.returncode)
        else:
            run(["systemctl", "--user", args.command, SERVICE], timeout=360)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
