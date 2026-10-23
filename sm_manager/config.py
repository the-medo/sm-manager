import json
import os
from pathlib import Path


def config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "sm-manager/config.json"


def load_config(path=None):
    path = Path(path) if path else config_path()
    custom = json.loads(path.read_text()) if path.exists() else {}
    config = {
        "port": 8790,
        "roots": [str(Path.home() / "dev"), str(Path.home() / ".codex/worktrees")],
        "codex_home": os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
        "data_dir": str(Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "sm-manager"),
        "interval": 10,
        "discovery_interval": 60,
        "scan_depth": 4,
        "allowed_hosts": ["localhost", "127.0.0.1"],
        "docker_socket": "/var/run/docker.sock",
        "container_overrides": {},
    }
    config.update(custom)
    config["roots"] = [str(Path(p).expanduser().resolve()) for p in config["roots"]]
    config["data_dir"] = str(Path(config["data_dir"]).expanduser())
    config["codex_home"] = str(Path(config["codex_home"]).expanduser())
    config["interval"] = max(2, int(config["interval"]))
    config["discovery_interval"] = max(30, int(config["discovery_interval"]))
    if not 1024 <= int(config["port"]) <= 65535:
        raise ValueError("Choose a port between 1024 and 65535")
    return config
