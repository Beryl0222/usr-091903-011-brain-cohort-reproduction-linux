"""SQLite 持久化：表结构、事务与审计。

设计要点：
- 所有业务表只增不改（版本化记录例外：旧版本仅会被打上 superseded_by 标记，
  内容本身不变），冻结快照因此永远不会被后续上传改写。
- recorded_at 一律由服务器生成，客户端无法伪造，保证 as_of 重建的正确性。
"""

import json
import sqlite3
import threading
from contextlib import contextmanager

from .common import now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
  participant_id TEXT PRIMARY KEY,
  cohort_group   TEXT NOT NULL,
  birth_year     INTEGER NOT NULL,
  enrolled_at    TEXT NOT NULL,
  created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consents (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  participant_id TEXT NOT NULL REFERENCES participants(participant_id),
  version        TEXT NOT NULL,
  scope_json     TEXT NOT NULL,
  signed_at      TEXT NOT NULL,
  recorded_at    TEXT NOT NULL,
  UNIQUE(participant_id, version)
);

CREATE TABLE IF NOT EXISTS withdrawals (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  participant_id     TEXT NOT NULL REFERENCES participants(participant_id),
  withdrawn_at       TEXT NOT NULL,
  scope_json         TEXT NOT NULL,
  aggregate_handling TEXT NOT NULL,
  note               TEXT,
  recorded_at        TEXT NOT NULL,
  UNIQUE(participant_id)
);

CREATE TABLE IF NOT EXISTS stage_events (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  participant_id       TEXT NOT NULL REFERENCES participants(participant_id),
  stage                TEXT NOT NULL,
  determination_method TEXT NOT NULL,
  event_date           TEXT NOT NULL,
  detail_json          TEXT NOT NULL DEFAULT '{}',
  recorded_at          TEXT NOT NULL,
  UNIQUE(participant_id, stage, event_date, determination_method)
);

CREATE TABLE IF NOT EXISTS visits (
  visit_id       TEXT PRIMARY KEY,
  participant_id TEXT NOT NULL REFERENCES participants(participant_id),
  visit_index    INTEGER NOT NULL,
  visit_date     TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  UNIQUE(participant_id, visit_index)
);

CREATE TABLE IF NOT EXISTS imaging_records (
  record_id     TEXT PRIMARY KEY,
  visit_id      TEXT NOT NULL REFERENCES visits(visit_id),
  version       INTEGER NOT NULL,
  params_json   TEXT NOT NULL,
  payload_hash  TEXT NOT NULL,
  source        TEXT,
  recorded_at   TEXT NOT NULL,
  superseded_by TEXT,
  UNIQUE(visit_id, payload_hash)
);

CREATE TABLE IF NOT EXISTS qc_records (
  record_id      TEXT PRIMARY KEY,
  visit_id       TEXT NOT NULL REFERENCES visits(visit_id),
  version        INTEGER NOT NULL,
  conclusion     TEXT NOT NULL,
  reasons_json   TEXT NOT NULL DEFAULT '[]',
  missing_reason TEXT,
  payload_hash   TEXT NOT NULL,
  source         TEXT,
  recorded_at    TEXT NOT NULL,
  superseded_by  TEXT,
  UNIQUE(visit_id, payload_hash)
);

CREATE TABLE IF NOT EXISTS derived_metrics (
  record_id        TEXT PRIMARY KEY,
  visit_id         TEXT NOT NULL REFERENCES visits(visit_id),
  version          INTEGER NOT NULL,
  metrics_json     TEXT NOT NULL,
  missing_json     TEXT NOT NULL DEFAULT '{}',
  pipeline_version TEXT NOT NULL,
  payload_hash     TEXT NOT NULL,
  source           TEXT,
  recorded_at      TEXT NOT NULL,
  superseded_by    TEXT,
  UNIQUE(visit_id, payload_hash)
);

CREATE TABLE IF NOT EXISTS exclusions (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  participant_id TEXT NOT NULL REFERENCES participants(participant_id),
  visit_id       TEXT REFERENCES visits(visit_id),
  stage          TEXT NOT NULL,
  reason         TEXT NOT NULL,
  payload_hash   TEXT NOT NULL UNIQUE,
  recorded_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS control_matches (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  match_set_id   TEXT NOT NULL,
  participant_id TEXT NOT NULL REFERENCES participants(participant_id),
  control_id     TEXT NOT NULL REFERENCES participants(participant_id),
  matched_on_json TEXT NOT NULL,
  recorded_at    TEXT NOT NULL,
  UNIQUE(match_set_id, participant_id, control_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id           TEXT PRIMARY KEY,
  name                  TEXT NOT NULL,
  purpose               TEXT NOT NULL,
  code_version          TEXT NOT NULL,
  cohort_spec_json      TEXT NOT NULL,
  manifest_json         TEXT NOT NULL,
  composition_json      TEXT NOT NULL,
  limitation_flags_json TEXT NOT NULL,
  content_hash          TEXT NOT NULL,
  created_by            TEXT NOT NULL,
  created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
  claim_id                TEXT PRIMARY KEY,
  snapshot_id             TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  statement               TEXT NOT NULL,
  scope_json              TEXT NOT NULL,
  limitations_json        TEXT NOT NULL,
  acknowledged_flags_json TEXT NOT NULL,
  created_by              TEXT NOT NULL,
  created_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exports (
  export_id        TEXT PRIMARY KEY,
  snapshot_id      TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  requester        TEXT NOT NULL,
  purpose          TEXT NOT NULL,
  fields_json      TEXT NOT NULL,
  rows_json        TEXT NOT NULL,
  row_count        INTEGER NOT NULL,
  dropped_withdrawn INTEGER NOT NULL,
  content_hash     TEXT NOT NULL,
  created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  actor       TEXT,
  action      TEXT NOT NULL,
  entity      TEXT NOT NULL,
  entity_id   TEXT,
  detail_json TEXT NOT NULL DEFAULT '{}'
);
"""


class Store:
    """单连接 + 可重入锁，适配 ThreadingHTTPServer 的多线程访问。"""

    def __init__(self, path=":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    @contextmanager
    def transaction(self):
        with self.lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def query(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def query_one(self, sql, args=()):
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def audit(self, conn, actor, action, entity, entity_id, detail=None):
        """在当前事务内追加审计条目，与业务写入同生共死。"""
        conn.execute(
            "INSERT INTO audit(ts, actor, action, entity, entity_id, detail_json) VALUES (?,?,?,?,?,?)",
            (now_iso(), actor, action, entity, entity_id, json.dumps(detail or {}, ensure_ascii=False)),
        )

    def audit_standalone(self, actor, action, entity, entity_id, detail=None):
        """独立事务审计，用于记录被拒绝的操作（如被策略拦截的导出）。"""
        with self.transaction() as conn:
            self.audit(conn, actor, action, entity, entity_id, detail)
