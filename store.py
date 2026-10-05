"""sqlite storage for servers, runs and call logs, so a refresh or a restart keeps the board."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    command TEXT NOT NULL,
    builtin INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    tools TEXT NOT NULL DEFAULT '[]',   -- the tools it advertised last, as JSON
    info TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    tools TEXT NOT NULL,                -- allowed "server/tool" names, as JSON
    max_calls INTEGER,                  -- NULL = no limit
    timeout REAL NOT NULL DEFAULT 60,
    state TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    started_at REAL,
    ended_at REAL
);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    at REAL NOT NULL,
    tool TEXT NOT NULL,
    arguments TEXT NOT NULL,            -- JSON
    allowed INTEGER NOT NULL,
    status TEXT NOT NULL,
    result TEXT,                        -- the MCP result, as JSON
    error TEXT NOT NULL DEFAULT '',
    ms REAL
);
CREATE INDEX IF NOT EXISTS calls_by_run ON calls(run_id);
"""

# Columns added after the first release, for databases made before them.
ADDED_COLUMNS = {
    "runs": {"max_calls": "INTEGER", "timeout": "REAL NOT NULL DEFAULT 60"},
}


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, isolation_level=None)  # autocommit: every save lands at once
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        for table, columns in ADDED_COLUMNS.items():
            have = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ writes

    def save_server(self, s) -> None:
        self.db.execute(
            "INSERT INTO servers (id, name, command, builtin, status, error, tools, info) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "command=excluded.command, status=excluded.status, error=excluded.error, "
            "tools=excluded.tools, info=excluded.info",
            (s.id, s.name, s.command, int(s.builtin), s.status, s.error, json.dumps(s.tools), json.dumps(s.info)))

    def delete_server(self, server_id: int) -> None:
        self.db.execute("DELETE FROM servers WHERE id = ?", (server_id,))

    def save_run(self, r) -> None:
        self.db.execute(
            "INSERT INTO runs (id, name, tools, max_calls, timeout, state, outcome, reason, created_at, "
            "started_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET state=excluded.state, "
            "outcome=excluded.outcome, reason=excluded.reason, started_at=excluded.started_at, "
            "ended_at=excluded.ended_at",
            (r.id, r.name, json.dumps(r.tools), r.max_calls, r.timeout, r.state, r.outcome, r.reason, r.created_at, r.started_at, r.ended_at))

    def save_call(self, c) -> None:
        self.db.execute(
            "INSERT INTO calls (id, run_id, at, tool, arguments, allowed, status, result, error, ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET allowed=excluded.allowed, "
            "status=excluded.status, result=excluded.result, error=excluded.error, ms=excluded.ms",
            (c.id, c.run_id, c.at, c.tool, json.dumps(c.arguments), int(c.allowed), c.status,
             None if c.result is None else json.dumps(c.result), c.error, c.ms))

    # ------------------------------------------------------------------ reads

    def servers(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM servers ORDER BY id").fetchall()
        return [{**dict(r), "builtin": bool(r["builtin"]), "tools": json.loads(r["tools"]),
                 "info": json.loads(r["info"])} for r in rows]

    def runs(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM runs ORDER BY id").fetchall()
        return [{**dict(r), "tools": json.loads(r["tools"])} for r in rows]

    def calls(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM calls ORDER BY id").fetchall()
        return [{**dict(r), "allowed": bool(r["allowed"]), "arguments": json.loads(r["arguments"]),
                 "result": None if r["result"] is None else json.loads(r["result"])} for r in rows]
