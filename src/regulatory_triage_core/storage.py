"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
import threading
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
CREATE TABLE IF NOT EXISTS rule_versions (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_weights_json TEXT NOT NULL,
    risk_multipliers_json TEXT NOT NULL,
    no_visit_days INTEGER NOT NULL CHECK(no_visit_days >= 0),
    remote_capacity INTEGER NOT NULL CHECK(remote_capacity >= 0),
    content_hash TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    plan_date TEXT NOT NULL,
    rules_version INTEGER NOT NULL,
    rules_content_hash TEXT NOT NULL,
    facts_snapshot_hash TEXT NOT NULL,
    onsite_capacity INTEGER NOT NULL CHECK(onsite_capacity >= 0),
    remote_capacity INTEGER NOT NULL CHECK(remote_capacity >= 0),
    district_capacity_json TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    status TEXT NOT NULL CHECK(status IN ('active', 'superseded', 'closed')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plans_date_status ON plans(plan_date, status);
CREATE TABLE IF NOT EXISTS plan_sites (
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    site_id TEXT NOT NULL,
    district_id TEXT NOT NULL,
    rank_order INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('onsite', 'remote', 'assist_online', 'deferred')),
    score REAL NOT NULL,
    risk_level TEXT NOT NULL,
    urgent INTEGER NOT NULL CHECK(urgent IN (0, 1)),
    exempt INTEGER NOT NULL CHECK(exempt IN (0, 1)),
    window_broken INTEGER NOT NULL CHECK(window_broken IN (0, 1)),
    facts_json TEXT NOT NULL,
    contributions_json TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    assigned_slot TEXT,
    claim_status TEXT NOT NULL CHECK(claim_status IN ('open', 'locked')),
    claimed_by TEXT,
    claimed_at TEXT,
    PRIMARY KEY(plan_id, site_id)
);
CREATE TABLE IF NOT EXISTS slot_actions (
    action_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    site_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('lock', 'release', 'reassign')),
    expected_revision INTEGER NOT NULL,
    from_officer_id TEXT,
    to_officer_id TEXT,
    reason TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slot_actions_plan ON slot_actions(plan_id, site_id, created_at);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        # 单连接被多线程 HTTP 服务共享：用可重入锁把所有事务与只读访问串行化，
        # 再叠加 BEGIN IMMEDIATE 的条件 UPDATE，保证并发领取不会重复派单。
        self.lock = threading.RLock()
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交；同一进程内全程持锁。"""

        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    @contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        """为事务外的只读访问提供同样的互斥保护。"""

        with self.lock:
            yield self.connection

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
