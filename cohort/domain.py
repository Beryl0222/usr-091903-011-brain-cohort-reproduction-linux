"""领域摄入与读取：同意、撤回、人生阶段事件、访视与版本化记录。

摄入原则：
- 重复上传（同一访视、同一内容哈希）幂等去重，返回已有记录；
- 内容变化的迟到上传生成新版本，旧版本保留并标记 superseded_by，
  已冻结快照引用的是具体版本，因此不受影响；
- 缺失值必须显式给出原因，排除必须显式给出理由。
"""

import json

from .common import (
    as_date,
    as_enum,
    as_str_list,
    new_id,
    now_iso,
    payload_hash,
    require,
    today_iso,
)
from .errors import ApiError, not_found

COHORT_GROUPS = ("puberty", "pregnancy", "menopause", "control")
STAGES = COHORT_GROUPS
DETERMINATION_METHODS = (
    "menstrual_record",       # 月经记录
    "gestational_week_record",  # 孕周记录
    "hormone_assay",          # 激素检测
    "self_report",            # 仅自报
    "not_applicable",         # 对照组等不适用情形
)
QC_CONCLUSIONS = ("pass", "fail", "review", "missing")
MISSING_REASONS = (
    "not_collected",
    "scan_failed",
    "participant_unavailable",
    "equipment_failure",
    "withdrawn",
    "other",
)
EXCLUSION_STAGES = ("pre_analysis", "qc", "outlier", "withdrawal", "other")
AGGREGATE_HANDLING = (
    "retain_existing_aggregates",       # 既有聚合结果保留
    "exclude_from_future_aggregates",   # 今后聚合一律排除
    "remove_where_feasible",            # 可行范围内移除
)

VERSIONED_TABLES = ("imaging_records", "qc_records", "derived_metrics")


# ---------------------------------------------------------------- 参与者

def create_participant(store, body, query=None):
    pid = body.get("participant_id") or new_id("p")
    group = as_enum(require(body, "cohort_group"), COHORT_GROUPS, "cohort_group")
    birth_year = body.get("birth_year")
    this_year = int(today_iso()[:4])
    if not isinstance(birth_year, int) or isinstance(birth_year, bool) or not 1900 <= birth_year <= this_year:
        raise ApiError(422, "invalid_value", "birth_year 必须是合理的四位年份", {"field": "birth_year"})
    enrolled_at = as_date(body["enrolled_at"], "enrolled_at") if body.get("enrolled_at") else today_iso()

    existing = store.query_one("SELECT * FROM participants WHERE participant_id=?", (pid,))
    if existing:
        same = (
            existing["cohort_group"] == group
            and existing["birth_year"] == birth_year
            and existing["enrolled_at"] == enrolled_at
        )
        if same:
            view = participant_view(store, pid)
            view["deduplicated"] = True
            return 200, view
        raise ApiError(
            409,
            "participant_conflict",
            f"参与者 {pid} 已存在且关键信息不一致",
            {"participant_id": pid},
        )

    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO participants(participant_id, cohort_group, birth_year, enrolled_at, created_at)"
            " VALUES (?,?,?,?,?)",
            (pid, group, birth_year, enrolled_at, now_iso()),
        )
        store.audit(conn, body.get("created_by"), "participant_created", "participant", pid,
                    {"cohort_group": group})
    return 201, participant_view(store, pid)


def must_participant(store, pid):
    row = store.query_one("SELECT * FROM participants WHERE participant_id=?", (pid,))
    if not row:
        raise not_found("participant", pid)
    return row


def get_participant(store, body=None, query=None, pid=None):
    return 200, participant_view(store, pid)


def participant_view(store, pid, **_):
    row = must_participant(store, pid)
    consents = store.query(
        "SELECT * FROM consents WHERE participant_id=? ORDER BY recorded_at, id", (pid,))
    withdrawal = store.query_one("SELECT * FROM withdrawals WHERE participant_id=?", (pid,))
    visits = store.query(
        "SELECT visit_id, visit_index, visit_date FROM visits WHERE participant_id=? ORDER BY visit_index",
        (pid,),
    )
    return {
        "participant_id": row["participant_id"],
        "cohort_group": row["cohort_group"],
        "birth_year": row["birth_year"],
        "enrolled_at": row["enrolled_at"],
        "status": "withdrawn" if withdrawal else "active",
        "consents": [
            {"version": c["version"], "scope": json.loads(c["scope_json"]), "signed_at": c["signed_at"]}
            for c in consents
        ],
        "withdrawal": _withdrawal_view(withdrawal) if withdrawal else None,
        "visits": [dict(v) for v in visits],
    }


def list_participants(store, body=None, query=None):
    rows = store.query(
        "SELECT p.participant_id, p.cohort_group, p.created_at,"
        "       EXISTS(SELECT 1 FROM withdrawals w WHERE w.participant_id=p.participant_id) AS withdrawn"
        " FROM participants p ORDER BY p.participant_id"
    )
    return 200, {
        "participants": [
            {
                "participant_id": r["participant_id"],
                "cohort_group": r["cohort_group"],
                "status": "withdrawn" if r["withdrawn"] else "active",
            }
            for r in rows
        ]
    }


# ---------------------------------------------------------------- 知情同意与撤回

def add_consent(store, body, query=None, pid=None):
    must_participant(store, pid)
    version = require(body, "version")
    scope = as_str_list(require(body, "scope"), "scope")
    signed_at = as_date(require(body, "signed_at"), "signed_at")

    existing = store.query_one(
        "SELECT * FROM consents WHERE participant_id=? AND version=?", (pid, version))
    if existing:
        same = json.loads(existing["scope_json"]) == scope and existing["signed_at"] == signed_at
        if same:
            return 200, {**_consent_view(existing), "deduplicated": True}
        # 同意书版本是不可变的法律文件，内容不同必须换新版本号
        raise ApiError(
            409,
            "consent_version_conflict",
            f"同意版本 {version} 已存在且内容不一致；知情同意版本不可变，请使用新版本号",
            {"participant_id": pid, "version": version},
        )

    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO consents(participant_id, version, scope_json, signed_at, recorded_at)"
            " VALUES (?,?,?,?,?)",
            (pid, version, json.dumps(scope, ensure_ascii=False), signed_at, now_iso()),
        )
        store.audit(conn, body.get("created_by"), "consent_recorded", "consent", pid,
                    {"version": version, "scope": scope})
    row = store.query_one("SELECT * FROM consents WHERE participant_id=? AND version=?", (pid, version))
    return 201, _consent_view(row)


def _consent_view(row):
    return {
        "participant_id": row["participant_id"],
        "version": row["version"],
        "scope": json.loads(row["scope_json"]),
        "signed_at": row["signed_at"],
        "recorded_at": row["recorded_at"],
    }


def add_withdrawal(store, body, query=None, pid=None):
    must_participant(store, pid)
    withdrawn_at = as_date(require(body, "withdrawn_at"), "withdrawn_at")
    scope = as_str_list(body.get("scope") or ["all"], "scope")
    handling = as_enum(
        require(body, "aggregate_handling"), AGGREGATE_HANDLING, "aggregate_handling")
    note = body.get("note")

    if store.query_one("SELECT 1 FROM withdrawals WHERE participant_id=?", (pid,)):
        raise ApiError(
            409,
            "withdrawal_exists",
            f"参与者 {pid} 已存在撤回记录；撤回是一次性法律事件",
            {"participant_id": pid},
        )

    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO withdrawals(participant_id, withdrawn_at, scope_json, aggregate_handling, note, recorded_at)"
            " VALUES (?,?,?,?,?,?)",
            (pid, withdrawn_at, json.dumps(scope, ensure_ascii=False), handling, note, now_iso()),
        )
        store.audit(conn, body.get("created_by"), "withdrawal_recorded", "withdrawal", pid,
                    {"scope": scope, "aggregate_handling": handling, "withdrawn_at": withdrawn_at})
    return 201, _withdrawal_view(store.query_one("SELECT * FROM withdrawals WHERE participant_id=?", (pid,)))


def _withdrawal_view(row):
    return {
        "participant_id": row["participant_id"],
        "withdrawn_at": row["withdrawn_at"],
        "scope": json.loads(row["scope_json"]),
        "aggregate_handling": row["aggregate_handling"],
        "note": row["note"],
        "recorded_at": row["recorded_at"],
    }


# ---------------------------------------------------------------- 人生阶段事件

def add_stage_event(store, body, query=None, pid=None):
    must_participant(store, pid)
    stage = as_enum(require(body, "stage"), STAGES, "stage")
    method = as_enum(require(body, "determination_method"), DETERMINATION_METHODS, "determination_method")
    event_date = as_date(require(body, "event_date"), "event_date")
    detail = body.get("detail") or {}
    if not isinstance(detail, dict):
        raise ApiError(422, "invalid_value", "detail 必须是对象", {"field": "detail"})

    existing = store.query_one(
        "SELECT * FROM stage_events WHERE participant_id=? AND stage=? AND event_date=? AND determination_method=?",
        (pid, stage, event_date, method),
    )
    if existing:
        return 200, {**_stage_event_view(existing), "deduplicated": True}

    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO stage_events(participant_id, stage, determination_method, event_date, detail_json, recorded_at)"
            " VALUES (?,?,?,?,?,?)",
            (pid, stage, method, event_date, json.dumps(detail, ensure_ascii=False), now_iso()),
        )
        store.audit(conn, body.get("created_by"), "stage_event_recorded", "stage_event", pid,
                    {"stage": stage, "determination_method": method, "event_date": event_date})
    row = store.query_one(
        "SELECT * FROM stage_events WHERE participant_id=? AND stage=? AND event_date=? AND determination_method=?",
        (pid, stage, event_date, method),
    )
    return 201, _stage_event_view(row)


def _stage_event_view(row):
    return {
        "participant_id": row["participant_id"],
        "stage": row["stage"],
        "determination_method": row["determination_method"],
        "event_date": row["event_date"],
        "detail": json.loads(row["detail_json"]),
        "recorded_at": row["recorded_at"],
    }


# ---------------------------------------------------------------- 访视

def create_visit(store, body, query=None, pid=None):
    must_participant(store, pid)
    visit_index = body.get("visit_index")
    if not isinstance(visit_index, int) or isinstance(visit_index, bool) or visit_index < 1:
        raise ApiError(422, "invalid_value", "visit_index 必须是 >=1 的整数", {"field": "visit_index"})
    visit_date = as_date(require(body, "visit_date"), "visit_date")
    vid = body.get("visit_id") or new_id("v")

    by_index = store.query_one(
        "SELECT * FROM visits WHERE participant_id=? AND visit_index=?", (pid, visit_index))
    if by_index:
        if by_index["visit_date"] == visit_date and by_index["visit_id"] == (body.get("visit_id") or by_index["visit_id"]):
            return 200, {**visit_view(store, by_index["visit_id"]), "deduplicated": True}
        raise ApiError(
            409,
            "visit_conflict",
            f"参与者 {pid} 的第 {visit_index} 次访视已存在且日期不一致",
            {"participant_id": pid, "visit_index": visit_index},
        )
    if store.query_one("SELECT 1 FROM visits WHERE visit_id=?", (vid,)):
        raise ApiError(409, "visit_conflict", f"visit_id {vid} 已被占用", {"visit_id": vid})

    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO visits(visit_id, participant_id, visit_index, visit_date, created_at)"
            " VALUES (?,?,?,?,?)",
            (vid, pid, visit_index, visit_date, now_iso()),
        )
        store.audit(conn, body.get("created_by"), "visit_created", "visit", vid,
                    {"participant_id": pid, "visit_index": visit_index, "visit_date": visit_date})
    return 201, visit_view(store, vid)


def must_visit(store, vid):
    row = store.query_one("SELECT * FROM visits WHERE visit_id=?", (vid,))
    if not row:
        raise not_found("visit", vid)
    return row


def get_visit(store, body=None, query=None, vid=None):
    return 200, visit_view(store, vid)


def visit_view(store, vid):
    visit = must_visit(store, vid)
    view = {
        "visit_id": visit["visit_id"],
        "participant_id": visit["participant_id"],
        "visit_index": visit["visit_index"],
        "visit_date": visit["visit_date"],
        "created_at": visit["created_at"],
    }
    for table, key in (("imaging_records", "imaging"), ("qc_records", "qc"), ("derived_metrics", "metrics")):
        latest = store.query_one(
            f"SELECT * FROM {table} WHERE visit_id=? AND superseded_by IS NULL ORDER BY version DESC LIMIT 1",
            (vid,),
        )
        view[key] = record_view(table, latest) if latest else None
    return view


# ---------------------------------------------------------------- 版本化记录（影像 / 质控 / 派生指标）

def record_view(table, row, deduplicated=False):
    view = {
        "record_id": row["record_id"],
        "visit_id": row["visit_id"],
        "version": row["version"],
        "source": row["source"],
        "recorded_at": row["recorded_at"],
        "superseded_by": row["superseded_by"],
    }
    if table == "imaging_records":
        view["params"] = json.loads(row["params_json"])
    elif table == "qc_records":
        view.update({
            "conclusion": row["conclusion"],
            "reasons": json.loads(row["reasons_json"]),
            "missing_reason": row["missing_reason"],
        })
    else:
        view.update({
            "metrics": json.loads(row["metrics_json"]),
            "missing": json.loads(row["missing_json"]),
            "pipeline_version": row["pipeline_version"],
        })
    if deduplicated:
        view["deduplicated"] = True
    return view


def _upsert_versioned(store, table, prefix, vid, payload, source, actor):
    """幂等 + 版本化写入：内容相同去重，内容不同生成新版本。"""
    digest = payload_hash(payload)
    existing = store.query_one(
        f"SELECT * FROM {table} WHERE visit_id=? AND payload_hash=?", (vid, digest))
    if existing:
        return 200, record_view(table, existing, deduplicated=True)

    latest = store.query_one(
        f"SELECT * FROM {table} WHERE visit_id=? AND superseded_by IS NULL ORDER BY version DESC LIMIT 1",
        (vid,),
    )
    version = latest["version"] + 1 if latest else 1
    rid = new_id(prefix)
    recorded_at = now_iso()

    if table == "imaging_records":
        sql = ("INSERT INTO imaging_records(record_id, visit_id, version, params_json, payload_hash, source, recorded_at)"
               " VALUES (?,?,?,?,?,?,?)")
        args = (rid, vid, version, json.dumps(payload["params"], ensure_ascii=False, sort_keys=True),
                digest, source, recorded_at)
    elif table == "qc_records":
        sql = ("INSERT INTO qc_records(record_id, visit_id, version, conclusion, reasons_json, missing_reason,"
               " payload_hash, source, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)")
        args = (rid, vid, version, payload["conclusion"], json.dumps(payload["reasons"], ensure_ascii=False),
                payload["missing_reason"], digest, source, recorded_at)
    else:
        sql = ("INSERT INTO derived_metrics(record_id, visit_id, version, metrics_json, missing_json,"
               " pipeline_version, payload_hash, source, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)")
        args = (rid, vid, version, json.dumps(payload["metrics"], ensure_ascii=False, sort_keys=True),
                json.dumps(payload["missing"], ensure_ascii=False, sort_keys=True),
                payload["pipeline_version"], digest, source, recorded_at)

    with store.transaction() as conn:
        conn.execute(sql, args)
        if latest:
            conn.execute(f"UPDATE {table} SET superseded_by=? WHERE record_id=?", (rid, latest["record_id"]))
        store.audit(conn, actor, "record_ingested", table, rid, {
            "visit_id": vid,
            "version": version,
            "supersedes": latest["record_id"] if latest else None,
        })
    return 201, record_view(table, store.query_one(f"SELECT * FROM {table} WHERE record_id=?", (rid,)))


def upsert_imaging(store, body, query=None, vid=None):
    must_visit(store, vid)
    params = require(body, "params")
    if not isinstance(params, dict) or not params:
        raise ApiError(422, "invalid_value", "params 必须是非空对象", {"field": "params"})
    if not params.get("scanner_model"):
        raise ApiError(422, "missing_field", "影像参数必须包含 scanner_model", {"field": "params.scanner_model"})
    return _upsert_versioned(store, "imaging_records", "img", vid,
                             {"params": params}, body.get("source"), body.get("created_by"))


def upsert_qc(store, body, query=None, vid=None):
    must_visit(store, vid)
    conclusion = as_enum(require(body, "conclusion"), QC_CONCLUSIONS, "conclusion")
    reasons = as_str_list(body.get("reasons") or [], "reasons", allow_empty=True)
    missing_reason = body.get("missing_reason")
    if conclusion == "fail" and not reasons:
        raise ApiError(422, "missing_field", "质控结论为 fail 时必须给出 reasons", {"field": "reasons"})
    if conclusion == "missing":
        if missing_reason not in MISSING_REASONS:
            raise ApiError(
                422, "missing_field", "质控结论为 missing 时必须给出合法的 missing_reason",
                {"field": "missing_reason", "allowed": list(MISSING_REASONS)})
    if conclusion == "pass":
        reasons, missing_reason = [], None
    payload = {"conclusion": conclusion, "reasons": reasons, "missing_reason": missing_reason}
    return _upsert_versioned(store, "qc_records", "qc", vid, payload,
                             body.get("source"), body.get("created_by"))


def upsert_metrics(store, body, query=None, vid=None):
    must_visit(store, vid)
    metrics = require(body, "metrics")
    if not isinstance(metrics, dict) or not metrics:
        raise ApiError(422, "invalid_value", "metrics 必须是非空对象", {"field": "metrics"})
    for name, value in metrics.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ApiError(422, "invalid_value", f"指标 {name} 必须是数值", {"field": f"metrics.{name}"})
    missing = body.get("missing") or {}
    if not isinstance(missing, dict):
        raise ApiError(422, "invalid_value", "missing 必须是对象", {"field": "missing"})
    for name, reason in missing.items():
        if reason not in MISSING_REASONS:
            raise ApiError(422, "invalid_value", f"指标 {name} 的缺失原因非法",
                           {"field": f"missing.{name}", "allowed": list(MISSING_REASONS)})
    pipeline_version = require(body, "pipeline_version")
    payload = {"metrics": metrics, "missing": missing, "pipeline_version": pipeline_version}
    return _upsert_versioned(store, "derived_metrics", "dm", vid, payload,
                             body.get("source"), body.get("created_by"))


# ---------------------------------------------------------------- 排除与对照匹配

def add_exclusion(store, body, query=None, pid=None):
    must_participant(store, pid)
    stage = as_enum(require(body, "stage"), EXCLUSION_STAGES, "stage")
    reason = require(body, "reason")
    vid = body.get("visit_id")
    if vid is not None:
        visit = must_visit(store, vid)
        if visit["participant_id"] != pid:
            raise ApiError(422, "invalid_value", "visit_id 不属于该参与者", {"visit_id": vid})

    digest = payload_hash({"participant_id": pid, "visit_id": vid, "stage": stage, "reason": reason})
    existing = store.query_one("SELECT * FROM exclusions WHERE payload_hash=?", (digest,))
    if existing:
        return 200, {**_exclusion_view(existing), "deduplicated": True}

    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO exclusions(participant_id, visit_id, stage, reason, payload_hash, recorded_at)"
            " VALUES (?,?,?,?,?,?)",
            (pid, vid, stage, reason, digest, now_iso()),
        )
        store.audit(conn, body.get("created_by"), "exclusion_recorded", "exclusion", pid,
                    {"visit_id": vid, "stage": stage, "reason": reason})
    return 201, _exclusion_view(store.query_one("SELECT * FROM exclusions WHERE payload_hash=?", (digest,)))


def _exclusion_view(row):
    return {
        "participant_id": row["participant_id"],
        "visit_id": row["visit_id"],
        "stage": row["stage"],
        "reason": row["reason"],
        "recorded_at": row["recorded_at"],
    }


def add_control_match(store, body, query=None):
    match_set_id = require(body, "match_set_id")
    pid = require(body, "participant_id")
    control_id = require(body, "control_id")
    matched_on = as_str_list(require(body, "matched_on"), "matched_on")
    must_participant(store, pid)
    must_participant(store, control_id)
    if pid == control_id:
        raise ApiError(422, "invalid_value", "对照不能与本人匹配", {"participant_id": pid})

    existing = store.query_one(
        "SELECT * FROM control_matches WHERE match_set_id=? AND participant_id=? AND control_id=?",
        (match_set_id, pid, control_id),
    )
    if existing:
        return 200, {**_match_view(existing), "deduplicated": True}

    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO control_matches(match_set_id, participant_id, control_id, matched_on_json, recorded_at)"
            " VALUES (?,?,?,?,?)",
            (match_set_id, pid, control_id, json.dumps(matched_on, ensure_ascii=False), now_iso()),
        )
        store.audit(conn, body.get("created_by"), "control_match_recorded", "control_match", match_set_id,
                    {"participant_id": pid, "control_id": control_id, "matched_on": matched_on})
    row = store.query_one(
        "SELECT * FROM control_matches WHERE match_set_id=? AND participant_id=? AND control_id=?",
        (match_set_id, pid, control_id),
    )
    return 201, _match_view(row)


def _match_view(row):
    return {
        "match_set_id": row["match_set_id"],
        "participant_id": row["participant_id"],
        "control_id": row["control_id"],
        "matched_on": json.loads(row["matched_on_json"]),
        "recorded_at": row["recorded_at"],
    }


# ---------------------------------------------------------------- 审计

def list_audit(store, body=None, query=None):
    query = query or {}
    entity = (query.get("entity") or [None])[0]
    try:
        limit = min(int((query.get("limit") or ["100"])[0]), 500)
    except ValueError:
        limit = 100
    if entity:
        rows = store.query("SELECT * FROM audit WHERE entity=? ORDER BY id DESC LIMIT ?", (entity, limit))
    else:
        rows = store.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
    return 200, {
        "entries": [
            {
                "id": r["id"],
                "ts": r["ts"],
                "actor": r["actor"],
                "action": r["action"],
                "entity": r["entity"],
                "entity_id": r["entity_id"],
                "detail": json.loads(r["detail_json"]),
            }
            for r in rows
        ]
    }
