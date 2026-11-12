"""Read the published Zed catalog and resolve links for monitor-only checkouts."""

import json
import os
import re
from pathlib import Path
from urllib.parse import quote_from_bytes


SSH_BASE = r"zed://ssh/[^/?#\s]+"


def entries(parent, key):
    items = parent.get(key)
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError(f"Expected a list of {key}")
    return items


def name_of(item):
    name = item.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("Missing catalog name")
    return name


def checkout(item, primary_path):
    path = item.get("path")
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError("Expected an absolute checkout path")
    branch, head = item.get("branch"), item.get("head")
    revision = "Revision unavailable"
    if isinstance(branch, str) and branch:
        revision = branch
    elif isinstance(head, str) and head:
        revision = f"{head[:7]} (detached)"
    url = item.get("zed_url")
    return {
        "name": name_of(item), "path": path, "revision": revision,
        "is_main": item.get("is_main") is True or path == primary_path,
        "zed_url": url if isinstance(url, str) and re.fullmatch(SSH_BASE + r"/\S+", url) else None,
    }


def read_catalog(filename, monitor_paths):
    catalog = json.loads(Path(filename).read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or type(catalog.get("schema_version")) is not int or catalog["schema_version"] != 1:
        raise ValueError("Expected Zed catalog version 1")
    groups = []
    catalog_paths = set()
    for group in entries(catalog, "groups"):
        repositories = []
        for repo in entries(group, "repositories"):
            trees = [checkout(item, repo.get("primary_path")) for item in entries(repo, "worktrees")]
            trees.sort(key=lambda tree: not tree["is_main"])
            catalog_paths.update(tree["path"] for tree in trees)
            repositories.append({"name": name_of(repo), "worktrees": trees})
        groups.append({"name": name_of(group), "repositories": repositories})

    monitor_only = []
    base = catalog.get("ssh_base_url")
    if isinstance(base, str) and re.fullmatch(SSH_BASE + "/?", base):
        for path in sorted(set(monitor_paths) - catalog_paths):
            if os.path.isabs(path) and os.path.isdir(path):
                url = base.rstrip("/") + quote_from_bytes(os.fsencode(path), safe="/")
                monitor_only.append({"name": Path(path).name, "path": path, "zed_url": url})
    return {"groups": groups, "monitor_only": monitor_only}
