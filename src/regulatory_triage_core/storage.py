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
CREATE TABLE IF NOT EXISTS rule_sets (
    rule_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL CHECK(version >= 1),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(rule_id, version)
);
CREATE TABLE IF NOT EXISTS active_rule_versions (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    activated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_snapshots (
    fact_key TEXT PRIMARY KEY,
    fact_version INTEGER NOT NULL CHECK(fact_version >= 1),
    site_id TEXT NOT NULL,
    fact_type TEXT NOT NULL,
    status TEXT NOT NULL,
    severity TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    plan_date TEXT NOT NULL,
    district_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    rules_hash TEXT NOT NULL,
    facts_hash TEXT NOT NULL,
    inputs_hash TEXT NOT NULL,
    input_summary_json TEXT NOT NULL,
    plan_version INTEGER NOT NULL CHECK(plan_version >= 1),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(plan_date, district_id, plan_version)
);
CREATE TABLE IF NOT EXISTS plan_items (
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    site_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    action TEXT NOT NULL,
    score REAL NOT NULL,
    risk_level TEXT NOT NULL,
    slot_id TEXT,
    window_override INTEGER NOT NULL CHECK(window_override IN (0, 1)),
    decision_json TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    PRIMARY KEY(plan_id, site_id)
);
CREATE TABLE IF NOT EXISTS plan_facts (
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    fact_key TEXT NOT NULL,
    site_id TEXT NOT NULL,
    fact_type TEXT NOT NULL,
    weight REAL NOT NULL,
    PRIMARY KEY(plan_id, fact_key)
);
CREATE TABLE IF NOT EXISTS dispatches (
    dispatch_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    plan_date TEXT NOT NULL,
    district_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    slot_id TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    claimed_by TEXT,
    claimed_at TEXT,
    released_by TEXT,
    released_at TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, site_id)
);
CREATE TABLE IF NOT EXISTS dispatch_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    expected_plan_version INTEGER,
    reason TEXT,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS emergencies (
    emergency_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    trigger_reference TEXT NOT NULL,
    trigger_detail_json TEXT NOT NULL,
    trigger_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    plan_id TEXT,
    declared_by TEXT NOT NULL,
    declared_at TEXT NOT NULL
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._write_lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。

        进程内用写锁串行化写事务，配合 BEGIN IMMEDIATE 与条件更新，
        保证同进程多个调度线程不会交叉写库或重复派单。
        """

        with self._write_lock:
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
