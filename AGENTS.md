# SM Manager

## Scope and architecture

This is a local, read-only Linux resource monitor. Keep it lightweight: Python
standard library backend, SQLite 30-day history, buildless browser UI.

- `sm_manager/discovery.py`: Git/Codex discovery and container ownership.
- `sm_manager/metrics.py`: `/proc`, Docker GET requests, and private app URLs.
- `sm_manager/collector.py`: resource attribution and cleanup briefs.
- `sm_manager/history.py`: bounded persistent samples.
- `sm_manager/server.py`: loopback API and explicit static asset allowlist.
- `scripts/manage.py`: owned systemd service and optional Tailscale Serve route.
- `scripts/updater.py`: installed main-branch updater and isolated runtime clone.

## Run and verify

Use `python3 scripts/manage.py status` to inspect the existing instance. After
changes, run appropriate tests (`python3 -m unittest discover -s tests -v`) and
restart with `python3 scripts/manage.py restart`. Leave the app running.
Read the saved access profile at `~/.config/sm-manager/config.json` for the exact
private browser URL. Do not hardcode a host, username, or machine path in source.
Verify `/api/health`, `/api/snapshot`, and the browser through that URL.

With `install --auto-update`, the service runs remote `main` from the independent
`<data_dir>/deployment/checkout`, not a development worktree. It checks for updates
before startup and every five minutes. Do not edit or remove that runtime clone.
After merging this project's branch, run `python3 scripts/manage.py update` and
verify the service before cleaning up the merged development branch/worktree.
If that script is unavailable, use `systemctl --user start sm-manager-update.service`.
Changes to the updater or generated units require rerunning
`python3 scripts/manage.py install --auto-update` from the updated source.
Update tests must use temporary Git repositories and mock all service controls.

Never bind the app to all interfaces, enable Funnel, reset Serve, overwrite
another application's route, or publish collected machine data. Runtime
configuration, screenshots of real projects, and databases stay out of Git.

## Extending cleanup

The current interface generates a plan only. A user request to clean up another
project should first read a fresh snapshot and that worktree's plan, follow the
target repository's own instructions, and verify PID start times, directories,
and container labels. Prefer its documented lifecycle tooling. Preserve volumes
and source/worktrees unless the user specifically authorizes their deletion.
Never infer destructive ownership from a container name or a stale PID alone.

Keep Docker process usage out of host-process totals. Test attribution,
PID reuse, history retention, and access boundaries when changing them. No tests
should stop live host services or touch the user's real Codex database.
