import sqlite3
import threading
import time
from pathlib import Path

RETENTION = 24 * 60 * 60


class History:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(directory / "history.sqlite", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=3000")
        self.db.execute("""CREATE TABLE IF NOT EXISTS samples (
            timestamp INTEGER NOT NULL, entity TEXT NOT NULL,
            cpu REAL, memory INTEGER, PRIMARY KEY (timestamp, entity)
        ) WITHOUT ROWID""")
        self.db.execute("CREATE INDEX IF NOT EXISTS samples_entity_time ON samples(entity,timestamp)")
        self.lock = threading.Lock()
        self.last_prune = 0

    def record(self, snapshot):
        timestamp = int(snapshot["timestamp"])
        system = snapshot["system"]
        totals, codex = snapshot["totals"], snapshot["codex"]
        rows = [(timestamp, "host", system["cpu_percent"], system["memory_used"])]
        rows.append((timestamp, "projects", totals["cpu_percent"], totals["memory_bytes"]))
        rows.append((timestamp, "codex", codex["cpu_percent"], codex["memory_bytes"]))
        # RSS and container working sets are estimates and may overlap. Never
        # turn an over-attributed sample into negative system usage.
        rows.append((timestamp, "other",
                     max(0, system["cpu_percent"] - totals["cpu_percent"] - codex["cpu_percent"]),
                     max(0, system["memory_used"] - totals["memory_bytes"] - codex["memory_bytes"])))
        for project in snapshot["projects"]:
            rows.append((timestamp, "project:" + project["id"], project["cpu_percent"], project["memory_bytes"]))
            for worktree in project["worktrees"]:
                rows.append((timestamp, "worktree:" + worktree["id"], worktree["cpu_percent"], worktree["memory_bytes"]))
        with self.lock, self.db:
            self.db.executemany("INSERT OR REPLACE INTO samples VALUES (?,?,?,?)", rows)
            if timestamp - self.last_prune >= 60:
                self.db.execute("DELETE FROM samples WHERE timestamp < ?", (timestamp - RETENTION,))
                self.last_prune = timestamp

    def query(self, entity="host", hours=24, now=None):
        now = now or time.time()
        hours = max(0.25, min(24, hours))
        bucket = max(10, int(hours * 3600 / 180))
        with self.lock:
            rows = self.db.execute("""SELECT CAST(timestamp / ? AS INTEGER) * ?, AVG(cpu), AVG(memory), MAX(cpu), MAX(memory)
                FROM samples WHERE entity=? AND timestamp>=? AND timestamp<=?
                GROUP BY CAST(timestamp / ? AS INTEGER) ORDER BY MIN(timestamp)""",
                (bucket, bucket, entity, now - hours * 3600, now, bucket)).fetchall()
            first = self.db.execute("SELECT MIN(timestamp) FROM samples WHERE entity=? AND timestamp>=?", (entity, now - RETENTION)).fetchone()[0]
        return {"entity": entity, "hours": hours, "bucket_seconds": bucket, "started_at": first,
                "points": [{"timestamp": r[0], "cpu_percent": r[1], "memory_bytes": r[2], "cpu_peak": r[3], "memory_peak": r[4]} for r in rows]}

    def worktree_activity(self, now=None):
        now = now or time.time()
        with self.lock:
            rows = self.db.execute("""SELECT entity, MIN(timestamp), MAX(timestamp), COUNT(*), AVG(cpu), MAX(cpu)
                FROM samples WHERE entity LIKE 'worktree:%' AND timestamp>=? AND timestamp<=? GROUP BY entity""",
                (now - 1800, now)).fetchall()
        return {row[0][9:]: {"first": row[1], "last": row[2], "samples": row[3], "cpu_average": row[4], "cpu_peak": row[5]} for row in rows}

    def close(self):
        with self.lock:
            self.db.close()
