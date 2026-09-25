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
CREATE TABLE IF NOT EXISTS relay_incidents (
    incident_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    road_code TEXT NOT NULL,
    title TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 2 CHECK(priority BETWEEN 1 AND 3),
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    head_revision_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS relay_open_incident_uq
    ON relay_incidents(site_id, road_code) WHERE status = 'open';
CREATE TABLE IF NOT EXISTS relay_revisions (
    revision_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES relay_incidents(incident_id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    message_id TEXT NOT NULL,
    event_time TEXT NOT NULL,
    received_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    ordering TEXT NOT NULL CHECK(ordering IN ('in_order', 'late')),
    applied INTEGER NOT NULL CHECK(applied IN (0, 1)),
    not_applied_reason TEXT,
    prev_hash TEXT NOT NULL,
    revision_hash TEXT NOT NULL,
    audit_sequence INTEGER,
    UNIQUE(incident_id, seq),
    UNIQUE(incident_id, message_id)
);
CREATE INDEX IF NOT EXISTS relay_revisions_incident_seq
    ON relay_revisions(incident_id, seq);
CREATE TABLE IF NOT EXISTS relay_conditions (
    incident_id TEXT NOT NULL REFERENCES relay_incidents(incident_id),
    condition_key TEXT NOT NULL,
    label TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'satisfied')),
    satisfied_revision_id TEXT,
    satisfied_at TEXT,
    reopened_at TEXT,
    PRIMARY KEY(incident_id, condition_key)
);
CREATE TABLE IF NOT EXISTS relay_reviews (
    review_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES relay_incidents(incident_id),
    revision_id TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    result TEXT NOT NULL CHECK(result IN ('pass', 'fail')),
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relay_leases (
    lease_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES relay_incidents(incident_id),
    holder_id TEXT NOT NULL REFERENCES actors(actor_id),
    status TEXT NOT NULL CHECK(status IN ('active', 'transferred', 'expired', 'completed')),
    claimed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    transferred_from_lease_id TEXT,
    note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relay_proposals (
    proposal_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES relay_incidents(incident_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    demands_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('proposed', 'confirmed', 'infeasible')),
    base_version INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relay_proposal_items (
    proposal_id TEXT NOT NULL REFERENCES relay_proposals(proposal_id),
    item_index INTEGER NOT NULL,
    resource_key TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('free', 'borrow', 'shortage')),
    source_incident_id TEXT,
    reason TEXT NOT NULL,
    PRIMARY KEY(proposal_id, item_index)
);
CREATE TABLE IF NOT EXISTS relay_allocations (
    allocation_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    incident_id TEXT NOT NULL REFERENCES relay_incidents(incident_id),
    status TEXT NOT NULL CHECK(status IN ('reserved', 'occupied', 'released', 'preempted')),
    proposal_id TEXT NOT NULL REFERENCES relay_proposals(proposal_id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    released_at TEXT,
    release_revision_id TEXT,
    release_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS relay_active_allocation_uq
    ON relay_allocations(site_id, resource_key)
    WHERE status = 'reserved' OR status = 'occupied';
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
