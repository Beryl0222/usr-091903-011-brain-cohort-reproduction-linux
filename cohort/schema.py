"""SQLite 数据库模式。

设计原则：

- 所有领域行都可溯源到上传批次（``*_batch_id``），迟到/重复上传不覆盖既有行。
- 冻结快照及其成员表只增不改（由触发器强制），保证"已冻结分析不可变"。
- 同意是带版本的事件流（给予/撤回），不删除参与者本身。
- 派生指标允许为空，缺失用 ``is_missing`` + ``missing_reason`` 显式表达，
  与"数值为零"严格区分。
"""

SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- 知情同意模板版本：撤回时按版本停止新的使用
CREATE TABLE IF NOT EXISTS consent_versions (
    version_code   TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    released_at    TEXT NOT NULL,
    scope_text     TEXT NOT NULL,
    terms_json     TEXT NOT NULL DEFAULT '{}'
);

-- 上传批次：迟到/重复上传的幂等与审计入口
CREATE TABLE IF NOT EXISTS upload_batches (
    batch_id        TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN
                        ('pending', 'accepted', 'accepted_idempotent',
                         'duplicate', 'rejected_conflict')),
    duplicate_of    TEXT REFERENCES upload_batches(batch_id),
    new_row_count   INTEGER NOT NULL DEFAULT 0,
    duplicate_rows  INTEGER NOT NULL DEFAULT 0,
    conflicts_json  TEXT NOT NULL DEFAULT '[]',
    note            TEXT NOT NULL DEFAULT ''
);
-- 非唯一：不同 batch_id 可以是同内容重复上传（status='duplicate'）
CREATE INDEX IF NOT EXISTS idx_batch_content_hash
    ON upload_batches(content_hash);

CREATE TABLE IF NOT EXISTS participants (
    participant_id  TEXT PRIMARY KEY,
    cohort_group    TEXT NOT NULL CHECK (cohort_group IN
                        ('puberty', 'pregnancy', 'menopause')),
    birth_year      INTEGER,
    sex_at_birth    TEXT NOT NULL DEFAULT 'F',
    created_batch   TEXT REFERENCES upload_batches(batch_id),
    created_at      TEXT NOT NULL
);

-- 同意事件流：同一参与者同一版本可先给后撤，effective_at 决定生效顺序
CREATE TABLE IF NOT EXISTS consent_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id  TEXT NOT NULL REFERENCES participants(participant_id),
    version_code    TEXT NOT NULL REFERENCES consent_versions(version_code),
    action          TEXT NOT NULL CHECK (action IN ('given', 'withdrawn')),
    effective_at    TEXT NOT NULL,
    recorded_batch  TEXT REFERENCES upload_batches(batch_id),
    note            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_consent_participant
    ON consent_events(participant_id, effective_at);

-- 人生阶段访视：阶段判定方式逐访视记录（记录/自报/激素检测）
CREATE TABLE IF NOT EXISTS life_stage_visits (
    participant_id      TEXT NOT NULL REFERENCES participants(participant_id),
    visit_date          TEXT NOT NULL,
    life_stage          TEXT NOT NULL,
    stage_detail        TEXT NOT NULL DEFAULT '',
    assessment_method   TEXT NOT NULL CHECK (assessment_method IN
                            ('menstrual_record', 'gestational_age_record',
                             'clinical_assessment', 'self_report',
                             'unassessed')),
    evidence_fields_json TEXT NOT NULL DEFAULT '{}',
    hormone_assay_done  INTEGER NOT NULL DEFAULT 0 CHECK (hormone_assay_done IN (0, 1)),
    hormone_assay_count INTEGER NOT NULL DEFAULT 0,
    recorded_batch      TEXT REFERENCES upload_batches(batch_id),
    PRIMARY KEY (participant_id, visit_date)
);

-- 匹配对照：一个病例访视可匹配多个对照
CREATE TABLE IF NOT EXISTS matched_controls (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    visit_participant_id    TEXT NOT NULL,
    visit_date              TEXT NOT NULL,
    control_participant_id  TEXT NOT NULL REFERENCES participants(participant_id),
    matched_on_json         TEXT NOT NULL DEFAULT '{}',
    recorded_batch          TEXT REFERENCES upload_batches(batch_id),
    UNIQUE (visit_participant_id, visit_date, control_participant_id),
    FOREIGN KEY (visit_participant_id, visit_date)
        REFERENCES life_stage_visits(participant_id, visit_date)
);

-- MRI 采集参数：只存序列唯一号的哈希，不落可重识别明文
CREATE TABLE IF NOT EXISTS mri_acquisitions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id      TEXT NOT NULL,
    visit_date          TEXT NOT NULL,
    scanner_id          TEXT NOT NULL,
    field_tesla         REAL,
    protocol_fingerprint TEXT NOT NULL DEFAULT '',
    series_uid_hash     TEXT NOT NULL,
    parameters_json     TEXT NOT NULL DEFAULT '{}',
    recorded_batch      TEXT REFERENCES upload_batches(batch_id),
    UNIQUE (participant_id, visit_date),
    UNIQUE (series_uid_hash),
    FOREIGN KEY (participant_id, visit_date)
        REFERENCES life_stage_visits(participant_id, visit_date)
);

-- 质控结论
CREATE TABLE IF NOT EXISTS qc_reviews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id  TEXT NOT NULL,
    visit_date      TEXT NOT NULL,
    decision        TEXT NOT NULL CHECK (decision IN
                        ('pass', 'fail', 'provisional')),
    rationale       TEXT NOT NULL DEFAULT '',
    reviewer        TEXT NOT NULL DEFAULT '',
    recorded_batch  TEXT REFERENCES upload_batches(batch_id),
    UNIQUE (participant_id, visit_date),
    FOREIGN KEY (participant_id, visit_date)
        REFERENCES life_stage_visits(participant_id, visit_date)
);

-- 派生指标：value 可空；is_missing=1 时必须给出 missing_reason
CREATE TABLE IF NOT EXISTS derived_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id  TEXT NOT NULL,
    visit_date      TEXT NOT NULL,
    metric_code     TEXT NOT NULL,
    value           REAL,
    is_missing      INTEGER NOT NULL DEFAULT 0 CHECK (is_missing IN (0, 1)),
    missing_reason  TEXT NOT NULL DEFAULT '',
    metric_version  TEXT NOT NULL,
    recorded_batch  TEXT REFERENCES upload_batches(batch_id),
    UNIQUE (participant_id, visit_date, metric_code, metric_version),
    CHECK (is_missing = 0 OR missing_reason <> ''),
    CHECK (is_missing = 1 OR value IS NOT NULL),
    FOREIGN KEY (participant_id, visit_date)
        REFERENCES life_stage_visits(participant_id, visit_date)
);

-- 冻结分析快照：只增不改
CREATE TABLE IF NOT EXISTS frozen_snapshots (
    snapshot_id         TEXT PRIMARY KEY,
    manuscript_ref      TEXT NOT NULL,
    analysis_code_version TEXT NOT NULL,
    selection_criteria_json TEXT NOT NULL,
    data_cutoff_batch   TEXT NOT NULL REFERENCES upload_batches(batch_id),
    frozen_at           TEXT NOT NULL,
    created_by          TEXT NOT NULL DEFAULT '',
    cohort_summary_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_members (
    snapshot_id             TEXT NOT NULL REFERENCES frozen_snapshots(snapshot_id),
    participant_id          TEXT NOT NULL,
    visit_date              TEXT NOT NULL,
    included                INTEGER NOT NULL CHECK (included IN (0, 1)),
    exclusion_reason        TEXT NOT NULL DEFAULT '',
    evidence_grade          TEXT NOT NULL DEFAULT '',
    repeated_measurement    INTEGER NOT NULL DEFAULT 0,
    repeat_index           INTEGER NOT NULL DEFAULT 0,
    present_metric_codes    TEXT NOT NULL DEFAULT '[]',
    missing_metric_codes    TEXT NOT NULL DEFAULT '[]',
    consent_version_code    TEXT NOT NULL DEFAULT '',
    cohort_group            TEXT NOT NULL DEFAULT '',
    life_stage              TEXT NOT NULL DEFAULT '',
    assessment_method       TEXT NOT NULL DEFAULT '',
    hormone_assay_done      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_id, participant_id, visit_date)
);

-- 既有聚合结果在参与者撤回后的处置记录
CREATE TABLE IF NOT EXISTS aggregate_artifacts (
    artifact_id     TEXT PRIMARY KEY,
    snapshot_id     TEXT NOT NULL REFERENCES frozen_snapshots(snapshot_id),
    kind            TEXT NOT NULL CHECK (kind IN
                        ('aggregate_table', 'figure', 'model_result', 'report')),
    description     TEXT NOT NULL DEFAULT '',
    retention_decision TEXT NOT NULL CHECK (retention_decision IN
                        ('retain', 'restrict_access', 'remove')),
    reason          TEXT NOT NULL DEFAULT '',
    decided_by      TEXT NOT NULL DEFAULT '',
    decided_at      TEXT NOT NULL
);

-- 对外结论：必须带适用范围与限制项结构化编码
CREATE TABLE IF NOT EXISTS conclusions (
    conclusion_id       TEXT PRIMARY KEY,
    snapshot_id         TEXT NOT NULL REFERENCES frozen_snapshots(snapshot_id),
    headline            TEXT NOT NULL,
    claim_text          TEXT NOT NULL,
    applicability_json  TEXT NOT NULL,
    limitations_json    TEXT NOT NULL,
    channel             TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'published',
    published_at        TEXT NOT NULL,
    published_by        TEXT NOT NULL DEFAULT ''
);

-- 导出申请与隐私评估
CREATE TABLE IF NOT EXISTS exports (
    export_id       TEXT PRIMARY KEY,
    snapshot_id     TEXT REFERENCES frozen_snapshots(snapshot_id),
    requested_by    TEXT NOT NULL,
    purpose         TEXT NOT NULL,
    granularity     TEXT NOT NULL CHECK (granularity IN
                        ('participant_level', 'aggregate')),
    columns_json    TEXT NOT NULL,
    filters_json    TEXT NOT NULL DEFAULT '{}',
    k_min           INTEGER NOT NULL DEFAULT 5,
    status          TEXT NOT NULL CHECK (status IN
                        ('released', 'rejected', 'suppressed_cells')),
    decision_reasons_json TEXT NOT NULL DEFAULT '[]',
    suppressed_cells_json TEXT NOT NULL DEFAULT '[]',
    pseudonym_salt  TEXT NOT NULL DEFAULT '',
    row_count       INTEGER NOT NULL DEFAULT 0,
    requested_at    TEXT NOT NULL
);

-- 通用不可变审计日志
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL,
    entity      TEXT NOT NULL,
    entity_id   TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}'
);

-- 冻结后不可修改/删除：数据库层兜底，任何迟到数据都不能改写历史分析
CREATE TRIGGER IF NOT EXISTS trg_frozen_snapshot_no_update
BEFORE UPDATE ON frozen_snapshots
BEGIN SELECT RAISE(ABORT, 'frozen_snapshots 不可变：已冻结分析不能被修改'); END;

CREATE TRIGGER IF NOT EXISTS trg_frozen_snapshot_no_delete
BEFORE DELETE ON frozen_snapshots
BEGIN SELECT RAISE(ABORT, 'frozen_snapshots 不可变：已冻结分析不能被删除'); END;

CREATE TRIGGER IF NOT EXISTS trg_snapshot_members_no_update
BEFORE UPDATE ON snapshot_members
BEGIN SELECT RAISE(ABORT, 'snapshot_members 不可变：冻结纳入清单不能被修改'); END;

CREATE TRIGGER IF NOT EXISTS trg_snapshot_members_no_delete
BEFORE DELETE ON snapshot_members
BEGIN SELECT RAISE(ABORT, 'snapshot_members 不可变：冻结纳入清单不能被删除'); END;
"""


def connect(path=":memory:"):
    """打开（必要时初始化）数据库连接。"""
    import sqlite3

    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA_SQL)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn
