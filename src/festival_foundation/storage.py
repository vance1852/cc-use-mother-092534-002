"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS traffic_incidents (
    incident_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    scene_key TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    state_version INTEGER NOT NULL CHECK(state_version >= 1),
    head_hash TEXT NOT NULL,
    control_kind TEXT,
    control_by TEXT,
    control_event_time TEXT,
    closed_by TEXT,
    closed_sequence INTEGER,
    closed_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS traffic_entries (
    incident_id TEXT NOT NULL REFERENCES traffic_incidents(incident_id),
    sequence INTEGER NOT NULL,
    message_id TEXT NOT NULL,
    entry_type TEXT NOT NULL,
    event_time TEXT NOT NULL,
    received_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    disposition TEXT NOT NULL,
    late INTEGER NOT NULL CHECK(late IN (0, 1)),
    note TEXT NOT NULL DEFAULT '',
    previous_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL,
    PRIMARY KEY (incident_id, sequence),
    UNIQUE(incident_id, message_id),
    UNIQUE(incident_id, entry_hash)
);
CREATE TABLE IF NOT EXISTS traffic_conditions (
    incident_id TEXT NOT NULL REFERENCES traffic_incidents(incident_id),
    condition_id TEXT NOT NULL,
    label TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_sequence INTEGER NOT NULL,
    confirmed_by TEXT,
    confirmed_sequence INTEGER,
    confirmed_at TEXT,
    PRIMARY KEY (incident_id, condition_id)
);
CREATE TABLE IF NOT EXISTS traffic_leases (
    lease_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES traffic_incidents(incident_id),
    holder_id TEXT NOT NULL REFERENCES actors(actor_id),
    granted_by TEXT NOT NULL REFERENCES actors(actor_id),
    granted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT,
    predecessor_id TEXT REFERENCES traffic_leases(lease_id),
    open_conditions_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS traffic_resources (
    resource_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    status TEXT NOT NULL,
    current_incident_id TEXT REFERENCES traffic_incidents(incident_id),
    holder_id TEXT REFERENCES actors(actor_id),
    occupied_at TEXT,
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS traffic_resource_movements (
    movement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id TEXT NOT NULL REFERENCES traffic_resources(resource_id),
    incident_id TEXT REFERENCES traffic_incidents(incident_id),
    action TEXT NOT NULL,
    plan_id TEXT,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    entry_sequence INTEGER,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS traffic_plans (
    plan_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES traffic_incidents(incident_id),
    state_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    feasible INTEGER NOT NULL CHECK(feasible IN (0, 1)),
    reason_summary TEXT NOT NULL,
    requested_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_by TEXT,
    confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS traffic_plan_items (
    plan_id TEXT NOT NULL REFERENCES traffic_plans(plan_id),
    position INTEGER NOT NULL,
    resource_id TEXT,
    kind TEXT NOT NULL,
    site_id TEXT,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    satisfied INTEGER NOT NULL CHECK(satisfied IN (0, 1)),
    PRIMARY KEY (plan_id, position)
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
