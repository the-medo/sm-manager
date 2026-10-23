# sm-manager

A quiet, local dashboard for the projects running on your machine. See which app,
branch, worktree, or Docker container is using memory and CPU without asking an
agent to inspect the host every time.

**Linux · Python 3.11+ · no runtime dependencies · no frontend build**

## What it does

- Groups running processes and containers by Git repository and worktree.
- Shows machine CPU, memory, swap, listening ports, and larger host processes.
- Keeps a rolling 24 hours of machine, project, and worktree history in SQLite.
- Compares all projects on one CPU or memory chart, with a dashed combined-total
  line and a clickable legend. Individual project, worktree, and whole-machine
  views remain available.
- Discovers Codex worktrees and shows local thread recency and runtime overhead.
- Links to apps that already have a private Tailscale Serve route.
- Copies a cleanup brief with exact resource identities for an agent to review.

The dashboard is read only. It doesn't stop processes, remove worktrees, delete
volumes, or run repository scripts. Cleanup briefs are a handoff to your agent.

## Run

```bash
git clone https://github.com/the-medo/sm-manager.git
cd sm-manager
python3 -m sm_manager serve
```

Open **http://127.0.0.1:8790**. The first sample usually arrives within a few
seconds. CPU needs two samples. History starts when the collector starts; it
isn't reconstructed or backfilled. The collector runs even when the browser is
closed.

Optional installation as a command: `pip install .`, then `sm-manager serve`.
Running directly from the checkout needs no package installation.

## Keep it running

On a Linux host with systemd user services:

```bash
python3 scripts/manage.py install
python3 scripts/manage.py status
python3 scripts/manage.py logs
python3 scripts/manage.py restart
python3 scripts/manage.py stop
python3 scripts/manage.py start
```

The service is enabled at login and restarts after a failure. For automatic
startup before login, enable user lingering with `loginctl enable-linger "$USER"`
if that matches how you manage your machine. Availability depends on the host and
its user service manager being up.

For a stable **private** browser URL on another computer in your tailnet:

```bash
python3 scripts/manage.py install --tailscale
```

The installer discovers the host's Tailscale DNS name and prints
`https://<host-dns>:8790`. The app still listens only on `127.0.0.1`. Tailscale
handles private network access; there is no separate app login. Any tailnet user
allowed by your ACLs to reach this host/port can read the dashboard. The helper
refuses to replace another app's Serve route and never enables Funnel.

To remove this monitor's service and its unchanged, owned Serve route:

```bash
python3 scripts/manage.py uninstall
```

Configuration and collected history are retained. Other applications and their
containers are unaffected.

## Configuration

Create `~/.config/sm-manager/config.json` (or under `$XDG_CONFIG_HOME`):

```json
{
  "port": 8790,
  "roots": ["~/dev", "~/.codex/worktrees"],
  "interval": 10,
  "discovery_interval": 60,
  "scan_depth": 4,
  "docker_socket": "/var/run/docker.sock",
  "allowed_hosts": ["localhost", "127.0.0.1"],
  "container_overrides": {
    "my-unlabelled-db": "~/dev/my-project"
  }
}
```

Restart the service after changes. `--config /path/to/config.json` before a CLI
subcommand selects another file. `codex_home` defaults to `$CODEX_HOME` or
`~/.codex`. `data_dir` defaults to `~/.local/share/sm-manager` (honoring
`$XDG_DATA_HOME`). Runtime data and machine configuration never belong in Git.

Discovery searches the configured roots, existing Codex thread/worktree paths,
and directories used by running processes and containers. It skips dependency
and build directories, then asks Git for linked worktrees. Nested repositories
are treated as separate projects when discovered through their own root/path.

### Docker ownership

The collector uses the local Docker Engine Unix socket. The user running the
monitor must already have access. It uses only GET requests; access to the Docker
socket itself still carries the privileges of your Docker installation.

Ownership is resolved in this order:

1. An exact container name or full ID in `container_overrides`.
2. `sm-manager.worktree`, Docker Compose working-directory labels, and custom
   `.dev.path` / `.worktree-path` labels.
3. A `.worktree-path-hash` label matching the first 16 characters of the
   worktree path's SHA-256 hash.
4. Bind mounts belonging to exactly one worktree.

Conflicting or absent ownership remains visible as **unassigned**. Container
names alone are not used to guess ownership. For new containers, prefer an
explicit label:

```bash
docker run --label "sm-manager.worktree=$PWD" ...
```

Docker outages and unavailable container statistics produce a visible warning;
host and project-process monitoring continue. Remote Docker engines and Windows
or macOS collectors are not supported in this first version.

## Reading the numbers

- **CPU:** percentage of the whole host across all logical cores. One saturated
  core on an eight-core machine is about 12.5%. Process samples use `/proc` tick
  deltas with PID start times to avoid PID-reuse spikes.
- **Host memory:** `MemTotal - MemAvailable`. Project memory is summed process
  RSS plus container working set. Shared pages may be counted more than once,
  so attribution is an estimate and won't exactly reconcile to host usage.
- **Containers:** memory excludes inactive file cache using Docker's documented
  accounting. Container processes are excluded from host-process attribution by
  their cgroup identity. See [Docker's memory accounting](https://docs.docker.com/reference/cli/docker/container/stats/).
- **Running worktree:** at least one detected process or running container. A
  database-only checkout still counts as running; “idle” means neither was found.
- **Codex:** local metadata reports thread update times, not whether an agent is
  actively working. It does not estimate token costs or read conversation text.
  The local database is an optional, version-dependent integration and failures
  don't prevent resource collection.
- **History:** samples every 10 seconds by default; chart points average into at
  most about 180 time buckets (the API also includes peaks). Samples older than
  24 hours are pruned each minute. Gaps indicate the monitor was not collecting.
  SQLite reuses freed pages; the database file need not shrink after pruning.

Some process working directories may be unreadable due to OS permissions. Those
processes appear outside projects. CPU/RSS collection does not require root.

## Agent interface and future cleanup

```bash
python3 -m sm_manager status
python3 -m sm_manager status --json
python3 -m sm_manager cleanup-plan <worktree-id>
```

Same-origin, read-only HTTP endpoints:

| Endpoint | Result |
| --- | --- |
| `GET /api/health` | Collector readiness and sample freshness |
| `GET /api/snapshot` | Projects, worktrees, processes, containers, Codex, host |
| `GET /api/history?hours=24&entity=host` | Time-bucketed CPU and memory |
| `GET /api/cleanup-plan/<worktree-id>` | Resource identities and a cleanup brief |

History entities are `host`, `projects`, `project:<id>`, or `worktree:<id>`.
IDs are stable hashes of the local Git common directory or worktree path.

An agent can use a brief to revalidate ownership, read the target repository's
instructions, and use its documented lifecycle command. PID start ticks and full
container IDs are included so a stale brief is never treated as a kill list.
The suggested cleanup preserves volumes, databases, source files, and worktrees.

## Privacy and access

No telemetry, external fonts, CDNs, or remote data services. The UI, API, and
SQLite history stay on your machine. No command-line arguments, environment
variables, Codex prompts, authentication files, or container environment are
stored or exposed. Metadata includes local paths, branch names, container image
names, process names, and resource identities; keep dashboard access private.

The server binds to loopback, validates the Host header, rejects cross-site
requests, and sends no CORS permissions. The static server exposes only its four
bundled assets. It has no mutation or shell-execution endpoint.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q sm_manager scripts
```

The backend is Python's standard library. The UI is plain HTML, CSS, and
JavaScript; edit and reload. See `AGENTS.md` for operating and extending it.
