import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from .config import load_config
from .server import serve


def main():
    parser = argparse.ArgumentParser(description="SM Manager — local projects, worktrees, and resource usage")
    parser.add_argument("--config", help="Path to local JSON configuration")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="Run the local dashboard and collector")
    status = sub.add_parser("status", help="Read the running monitor")
    status.add_argument("--json", action="store_true")
    plan = sub.add_parser("cleanup-plan", help="Print a read-only resource inventory for a worktree")
    plan.add_argument("worktree_id")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command == "serve":
        os.umask(0o077)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        if not sys.platform.startswith("linux"):
            parser.error("The collector currently supports Linux (/proc).")
        serve(config)
        return
    endpoint = "/api/snapshot" if args.command == "status" else "/api/cleanup-plan/" + urllib.parse.quote(args.worktree_id, safe="")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{config['port']}{endpoint}", timeout=10) as response:
            data = json.load(response)
    except (OSError, urllib.error.URLError) as error:
        parser.exit(1, f"Monitor unavailable: {error}. Start it with python3 -m sm_manager serve\n")
    if args.command == "cleanup-plan" or args.json:
        print(json.dumps(data, indent=2))
        return
    print(f"{data['system']['hostname']} · CPU {data['system']['cpu_percent']:.1f}% · RAM {data['system']['memory_used'] / 2**30:.1f} GiB")
    if data["stale"]:
        print("WARNING: last sample is stale")
    for project in data["projects"]:
        for worktree in project["worktrees"]:
            if worktree["running"]:
                print(f"{project['name']:18} {worktree['branch'][:36]:36} {worktree['memory_bytes'] / 2**20:8.1f} MiB {worktree['cpu_percent']:5.1f}%  {worktree['id']}")


if __name__ == "__main__":
    main()
