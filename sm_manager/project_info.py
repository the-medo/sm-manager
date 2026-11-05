"""Bounded, local project metadata reads. Never execute or fetch project assets."""

import hashlib
import json
import os
import re
import stat
import tomllib
from collections import deque
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

MAX_FILE_BYTES = 256 * 1024
MAX_ICON_BYTES = 1024 * 1024
MAX_DIRECTORIES = 160
MAX_ENTRIES = 6000
SKIP = {
    "node_modules", "vendor", "dist", "build", "target", "__pycache__", "venv",
    "coverage", "docs", "doc", "tests", "test", "test-results", "prototypes",
    "storybook-static", "generated", "bin", "obj",
}
ICON_NAMES = {"favicon.svg": 0, "favicon.ico": 1, "favicon.png": 2,
              "icon.svg": 3, "icon.png": 4, "apple-touch-icon.png": 5}


def read_local(root, path, limit=MAX_FILE_BYTES):
    """Only read bounded regular files within this checkout, without symlinks."""
    try:
        relative = path.relative_to(root)
        if not relative.parts or any(part.startswith(".") for part in relative.parts):
            return None
        # Open each component relative to its parent fd to avoid symlink races.
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in relative.parts[:-1]:
                child_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = child_fd
            fd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                return None
            data = handle.read(limit + 1)
        return data if len(data) <= limit else None
    except (OSError, ValueError, RuntimeError):
        return None


def candidates(root):
    queue = deque([(root, 0)])
    remaining = MAX_ENTRIES
    result = []
    for _ in range(MAX_DIRECTORIES):
        if not queue or remaining <= 0:
            break
        directory, depth = queue.popleft()
        children = []
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    remaining -= 1
                    if remaining < 0:
                        break
                    if entry.name.startswith(".") or entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if depth < 6 and entry.name not in SKIP:
                            children.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False) and (
                        entry.name in {"package.json", "pyproject.toml", "index.html", *ICON_NAMES}
                        or ("favicon" in entry.name.lower() and Path(entry.name).suffix.lower() in {".svg", ".png", ".ico"})
                    ):
                        result.append(Path(entry.path))
        except OSError:
            continue
        queue.extend((child, depth + 1) for child in sorted(children, key=lambda p: (p.name not in {"frontend", "web", "public", "src", "app", "static"}, p.name)))
    return sorted(result, key=lambda p: (len(p.relative_to(root).parts), str(p)))


class PageInfo(HTMLParser):
    def __init__(self):
        super().__init__()
        self.icons = []
        self.description = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and "icon" in (attrs.get("rel") or "").lower().split():
            self.icons.append(attrs.get("href") or "")
        if tag == "meta" and (attrs.get("name") or "").lower() == "description":
            self.description = attrs.get("content") or ""


def clean_text(value, limit=240):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def repository_url(remote):
    """Convert common Git remotes to browser links, discarding credentials."""
    remote = remote.strip()
    if re.search(r"\s", remote):
        return ""
    if "://" not in remote:
        match = re.fullmatch(r"(?:[^@/\s]+@)?([\w.-]+):([\w./-]+)", remote)
        if not match:
            return ""
        remote = f"https://{match[1]}/{match[2]}"
    try:
        parsed = urlsplit(remote)
        host = parsed.hostname or ""
        if parsed.scheme not in {"https", "http", "ssh", "git"} or not re.fullmatch(r"[a-zA-Z0-9.-]+", host):
            return ""
        path = parsed.path.rstrip("/").removesuffix(".git")
        if not path or not re.fullmatch(r"/[\w./~-]+", path):
            return ""
        port = f":{parsed.port}" if parsed.port and parsed.scheme in {"http", "https"} else ""
        scheme = "http" if parsed.scheme == "http" else "https"
        return f"{scheme}://{host}{port}{path}"
    except ValueError:
        return ""


def load_icon(root, path):
    data = read_local(root, path, MAX_ICON_BYTES)
    if not data:
        return None
    suffix = path.suffix.lower()
    mime = None
    if suffix == ".svg" and b"<!DOCTYPE" not in data.upper() and b"<!ENTITY" not in data.upper():
        try:
            if ElementTree.fromstring(data).tag in {"svg", "{http://www.w3.org/2000/svg}svg"}:
                mime = "image/svg+xml"
        except (ElementTree.ParseError, LookupError, ValueError):
            pass
    elif suffix == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif suffix == ".ico" and data.startswith(b"\x00\x00\x01\x00"):
        mime = "image/x-icon"
    if mime:
        return {"data": data, "mime": mime, "version": hashlib.sha256(data).hexdigest()[:16]}
    return None


def inspect_project(path):
    root = Path(path).resolve()
    files = candidates(root)
    info = {"path": str(root), "description": "", "version": "", "languages": []}
    for manifest in [p for p in files if p.name in {"package.json", "pyproject.toml"}][:16]:
        raw = read_local(root, manifest)
        if raw is None:
            continue
        try:
            if manifest.name == "package.json":
                data, language = json.loads(raw), "Node.js"
            else:
                data, language = tomllib.loads(raw.decode()).get("project", {}), "Python"
            if not isinstance(data, dict):
                continue
            if language not in info["languages"]:
                info["languages"].append(language)
            if not info["description"]:
                info["description"] = clean_text(data.get("description"))
            if not info["version"]:
                info["version"] = clean_text(data.get("version"), 40)
        except (ValueError, UnicodeError, AttributeError, RecursionError):
            continue
    icon_paths = []
    for page in [p for p in files if p.name == "index.html"][:16]:
        raw = read_local(root, page)
        if raw is None:
            continue
        parser = PageInfo()
        parser.feed(raw.decode("utf-8", errors="replace"))
        if not info["description"]:
            info["description"] = clean_text(parser.description)
        for href in parser.icons:
            try:
                url = urlsplit(href)
            except ValueError:
                continue
            if url.scheme or url.netloc or not url.path:
                continue
            relative = unquote(url.path).lstrip("/")
            for base in (page.parent, page.parent / "public", page.parent / "static",
                         page.parent / "src", page.parent / "src/assets", page.parent / "src/app/assets"):
                icon_paths.append(base / relative)
    icon_paths.extend(sorted((p for p in files if "favicon" in p.name.lower() or p.name in ICON_NAMES),
                             key=lambda p: (ICON_NAMES.get(p.name, 2), len(p.relative_to(root).parts), str(p))))
    for icon_path in list(dict.fromkeys(icon_paths))[:64]:
        if icon := load_icon(root, icon_path):
            return info, icon
    return info, None
