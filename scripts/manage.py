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

SERVICE = "sm-manager.service"
UNIT = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user" / SERVICE


def run(args, check=True, capture=False):
    return subprocess.run(args, check=check, text=True, capture_output=capture, timeout=30)


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


def install(private):
    if UNIT.exists() and "# Managed by sm-manager/scripts/manage.py" not in UNIT.read_text():
        raise RuntimeError(f"Refusing to overwrite an unrelated unit: {UNIT}")
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
    unit = f'''# Managed by sm-manager/scripts/manage.py
[Unit]
Description=SM Manager local project resource dashboard

[Service]
Type=simple
WorkingDirectory={str(ROOT).replace('%', '%%')}
ExecStart={unit_quote(sys.executable)} -m sm_manager serve
Restart=on-failure
RestartSec=5
UMask=0077
Environment={unit_quote('PATH=' + service_path)}
NoNewPrivileges=true
TimeoutStopSec=10

[Install]
WantedBy=default.target
'''
    UNIT.write_text(unit)
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", SERVICE])
    run(["systemctl", "--user", "restart", SERVICE])
    print(f"Dashboard: {config.get('public_url', f'http://127.0.0.1:{port}')}")
    print(f"Local configuration: {config_path()}")
    print("Service enabled. User services start at login; lingering allows startup before login.")


def uninstall():
    if not UNIT.exists() or "# Managed by sm-manager/scripts/manage.py" not in UNIT.read_text():
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
    run(["systemctl", "--user", "disable", "--now", SERVICE])
    UNIT.unlink()
    run(["systemctl", "--user", "daemon-reload"])
    config.pop("serve_mapping", None)
    config.pop("public_url", None)
    save(config)
    print("SM Manager service removed. Configuration and history retained.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["install", "start", "stop", "restart", "status", "logs", "uninstall"])
    parser.add_argument("--tailscale", action="store_true", help="Install a private Tailscale Serve route")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "install":
            install(args.tailscale)
        elif args.command == "uninstall":
            uninstall()
        elif args.command == "logs":
            os.execvp("journalctl", ["journalctl", "--user", "-u", SERVICE, "-n", "70", "--no-pager"])
        elif args.command == "status":
            config = load_config()
            print(config.get("public_url", f"http://127.0.0.1:{config['port']}"), flush=True)
            result = run(["systemctl", "--user", "status", SERVICE, "--no-pager"], check=False)
            sys.exit(result.returncode)
        else:
            run(["systemctl", "--user", args.command, SERVICE])
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
