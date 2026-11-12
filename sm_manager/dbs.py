"""Read the published database catalog without contacting its endpoints."""

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


STALE_AFTER_SECONDS = 3 * 60


def launch_url(value):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 or c in "\\#" for c in value):
        return None
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.netloc.lower() not in {"db.the-medo.com", "db.the-medo.com:443"}
                or url.path != "/cloudbeaver/open"):
            return None
        query = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=1)
    except ValueError:
        return None
    if len(query) == 1:
        key, connection_id = query[0]
        if key == "id" and connection_id.strip() and connection_id.isprintable():
            return value
    return None


def text_fields(record, fields):
    if not isinstance(record, dict) or any(
        not isinstance(record.get(key), str) or not record[key].strip() for key in fields
    ):
        raise ValueError("Invalid catalog fields")
    return {key: record[key] for key in fields}


def connection(record, stale):
    entry = text_fields(record, ("id", "name", "engine", "database", "source", "status"))
    if entry["status"] not in {"available", "configured", "stopped", "unreachable"}:
        raise ValueError("Unknown database status")
    if not {"host", "port", "owner", "cloudbeaver_url"} <= record.keys():
        raise ValueError("Incomplete connection")
    host, port = record["host"], record["port"]
    if host is not None and not isinstance(host, str):
        raise ValueError("Invalid database host")
    if port is not None and (type(port) is not int or not 0 < port <= 65535):
        raise ValueError("Invalid database port")
    owner = record["owner"]
    if owner is None and record.get("group") is not None:
        entry.update(text_fields(record, ("group",)))
        if "group_checked_at" in record:
            entry.update(text_fields(record, ("group_checked_at",)))
            if datetime.fromisoformat(entry["group_checked_at"]).utcoffset() != timedelta(0):
                raise ValueError("Expected UTC group check time")
    if owner is not None:
        owner = text_fields(owner, ("group", "repository", "primary_path", "worktree", "worktree_path"))
        if not all(os.path.isabs(owner[key]) for key in ("primary_path", "worktree_path")):
            raise ValueError("Invalid owner paths")
        if "branch" not in record["owner"]:
            raise ValueError("Missing owner branch")
        branch = record["owner"]["branch"]
        if branch is not None and not isinstance(branch, str):
            raise ValueError("Invalid branch")
        owner["branch"] = branch
    url = launch_url(record["cloudbeaver_url"]) if entry["status"] == "available" and not stale else None
    return {**entry, "host": host, "port": port, "owner": owner, "cloudbeaver_url": url}


def read_db_catalog(filename):
    catalog = json.loads(Path(filename).read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or type(catalog.get("schema_version")) is not int or catalog["schema_version"] != 1:
        raise ValueError("Expected DB catalog version 1")
    if not isinstance(catalog.get("connections"), list) or not isinstance(catalog.get("checked_at"), str):
        raise ValueError("Expected connections and a UTC check time")
    checked = datetime.fromisoformat(catalog["checked_at"])
    if checked.utcoffset() != timedelta(0):
        raise ValueError("Expected UTC check time")
    age = time.time() - checked.timestamp()
    if age < 0:
        raise ValueError("Future check time")
    stale = age >= STALE_AFTER_SECONDS
    return {
        "schema_version": 1, "checked_at": checked.isoformat(), "stale": stale,
        "fresh_for_seconds": max(0, STALE_AFTER_SECONDS - age),
        "connections": [connection(record, stale) for record in catalog["connections"]],
    }
