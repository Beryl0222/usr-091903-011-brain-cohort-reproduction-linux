"""导出与脱敏策略。

导出只允许从已冻结快照取数（内容即冻结时状态），并强制执行：
- 成员标识假名化：member_id = HMAC-SHA256(export 专属盐, participant_id)，
  同一受试者在不同导出中不可关联；
- 时间降精度：访视日期只导出季度（visit_quarter），年龄只导出 5 岁分带；
- 影像采集参数不支持行级导出（影像+时间组合可重识别），
  仅提供 /snapshots/{id}/imaging-summary 聚合视图；
- k-匿名：按 (分组, 年龄带, 季度, 阶段判定方式) 组合计数，
  任何单元格人数不足 K 即拒绝导出；
- 撤回参与者的行一律从新导出中剔除并计数留痕；
- 每次导出（含被策略拒绝的尝试）都写入审计。
"""

import hashlib
import hmac
import json
from collections import Counter

from .common import as_str_list, new_id, now_iso, payload_hash, require, scope_covers
from .errors import ApiError
from .snapshots import must_snapshot

K_ANONYMITY = 5

# 允许的行级字段（均为降精度或派生值）
ALLOWED_ROW_FIELDS = (
    "member_id",
    "group",
    "age_band",
    "visit_quarter",
    "determination_method",
    "qc_conclusion",
)
DEFAULT_FIELDS = ["member_id", "group", "age_band", "visit_quarter", "determination_method", "qc_conclusion", "metrics"]

# 时间类字段（用于"影像+时间"组合判定）
TIME_LIKE_FIELDS = ("visit_quarter", "visit_date", "acquisition_timestamp", "scan_time")

QUASI_IDENTIFIER_FIELDS = ("group", "age_band", "visit_quarter", "determination_method")


def create_export(store, body, query=None):
    snapshot = must_snapshot(store, require(body, "snapshot_id"))
    requester = require(body, "requester")
    purpose = require(body, "purpose")
    sid = snapshot["snapshot_id"]

    fields = body.get("fields")
    if fields is None:
        fields = list(DEFAULT_FIELDS)
    fields = as_str_list(fields, "fields")
    requested = list(dict.fromkeys(fields))

    # 影像参数与任何时间粒度同时请求时，直接按可重识别组合拒绝
    imaging_req = [f for f in requested if f == "imaging" or f.startswith("imaging:")]
    time_req = [f for f in requested if f in TIME_LIKE_FIELDS]
    if imaging_req and time_req:
        _blocked(store, requester, sid, "reidentifiable_combination",
                 {"imaging": imaging_req, "time": time_req})
        raise ApiError(
            422, "reidentifiable_combination",
            "影像采集参数与采集时间的组合可重识别个体，禁止同时导出；"
            "影像参数仅可通过 /snapshots/{id}/imaging-summary 获取聚合视图",
            {"imaging_fields": imaging_req, "time_fields": time_req})
    if imaging_req:
        _blocked(store, requester, sid, "imaging_not_exportable", {"imaging": imaging_req})
        raise ApiError(
            422, "imaging_not_exportable",
            "影像采集参数不支持行级导出，请使用快照的 imaging-summary 聚合视图",
            {"fields": imaging_req})

    unknown = [
        f for f in requested
        if f not in ALLOWED_ROW_FIELDS and f != "metrics" and not f.startswith("metric:")
    ]
    if unknown:
        _blocked(store, requester, sid, "forbidden_fields", {"fields": unknown})
        raise ApiError(
            422, "forbidden_fields",
            "存在不允许导出的字段（原始时间、原始标识、影像参数等一律禁止）",
            {"fields": unknown, "allowed": sorted(ALLOWED_ROW_FIELDS) + ["metrics", "metric:<name>"]})

    manifest = json.loads(snapshot["manifest_json"])

    # 撤回执行：新导出属于新使用，覆盖导出/快照用途的撤回一律剔除
    members, dropped_withdrawn = [], 0
    for member in manifest["members"]:
        withdrawal = store.query_one(
            "SELECT * FROM withdrawals WHERE participant_id=?", (member["participant_id"],))
        if withdrawal:
            scopes = json.loads(withdrawal["scope_json"])
            if scope_covers(scopes, purpose) or scope_covers(scopes, snapshot["purpose"]):
                dropped_withdrawn += 1
                continue
        members.append(member)

    # 指标列：metrics 表示全部指标的并集，metric:<name> 指定单列
    all_metric_names = set()
    for member in members:
        for visit in member["visits"]:
            pin = visit.get("metrics")
            if not pin:
                continue
            record = store.query_one(
                "SELECT metrics_json FROM derived_metrics WHERE record_id=?", (pin["record_id"],))
            if record:
                all_metric_names.update(json.loads(record["metrics_json"]).keys())
    explicit_metrics = [f[len("metric:"):] for f in requested if f.startswith("metric:")]
    unknown_metrics = [name for name in explicit_metrics if name not in all_metric_names]
    if unknown_metrics:
        raise ApiError(422, "unknown_metric", "快照中不存在请求的指标",
                       {"metrics": unknown_metrics, "available": sorted(all_metric_names)})
    metric_cols = sorted(all_metric_names if "metrics" in requested else set(explicit_metrics))

    export_id = new_id("exp")
    rows = []
    cell_keys = []
    for member in members:
        participant = store.query_one(
            "SELECT birth_year FROM participants WHERE participant_id=?", (member["participant_id"],))
        for visit in member["visits"]:
            visit_row = store.query_one("SELECT * FROM visits WHERE visit_id=?", (visit["visit_id"],))
            visit_date = visit_row["visit_date"]
            age_band = _age_band(int(visit_date[:4]) - participant["birth_year"])
            quarter = _quarter(visit_date)
            method = member["stage_event"]["determination_method"]
            cell_keys.append((member["group"], age_band, quarter, method))

            row = {}
            if "member_id" in requested:
                row["member_id"] = _pseudonym(export_id, member["participant_id"])
            if "group" in requested:
                row["group"] = member["group"]
            if "age_band" in requested:
                row["age_band"] = age_band
            if "visit_quarter" in requested:
                row["visit_quarter"] = quarter
            if "determination_method" in requested:
                row["determination_method"] = method
            if "qc_conclusion" in requested:
                row["qc_conclusion"] = visit["qc"]["conclusion"] if visit.get("qc") else None
            if metric_cols:
                metrics = {}
                pin = visit.get("metrics")
                if pin:
                    record = store.query_one(
                        "SELECT metrics_json FROM derived_metrics WHERE record_id=?", (pin["record_id"],))
                    if record:
                        metrics = json.loads(record["metrics_json"])
                for name in metric_cols:
                    row[f"metric.{name}"] = metrics.get(name)
            rows.append(row)

    # k-匿名：无论本次是否导出全部准标识符，都按完整组合校验（更严格）
    cells = Counter(cell_keys)
    violations = [
        {"cell": dict(zip(QUASI_IDENTIFIER_FIELDS, cell)), "count": count}
        for cell, count in sorted(cells.items())
        if count < K_ANONYMITY
    ]
    if violations:
        _blocked(store, requester, sid, "k_anonymity_violation", {"violations": violations})
        raise ApiError(
            422, "k_anonymity_violation",
            f"按准标识符组合存在人数不足 {K_ANONYMITY} 的单元格，导出被阻止",
            {"k": K_ANONYMITY, "violations": violations})

    content_hash = payload_hash(rows)
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO exports(export_id, snapshot_id, requester, purpose, fields_json, rows_json,"
            " row_count, dropped_withdrawn, content_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                export_id, sid, requester, purpose,
                json.dumps(requested, ensure_ascii=False),
                json.dumps(rows, ensure_ascii=False, sort_keys=True),
                len(rows), dropped_withdrawn, content_hash, now_iso(),
            ),
        )
        store.audit(conn, requester, "export_created", "export", export_id, {
            "snapshot_id": sid,
            "purpose": purpose,
            "row_count": len(rows),
            "dropped_withdrawn": dropped_withdrawn,
            "fields": requested,
        })
    return 201, export_view(store.query_one("SELECT * FROM exports WHERE export_id=?", (export_id,)))


def get_export(store, body=None, query=None, eid=None):
    row = store.query_one("SELECT * FROM exports WHERE export_id=?", (eid,))
    if not row:
        from .errors import not_found
        raise not_found("export", eid)
    return 200, export_view(row)


def export_view(row):
    return {
        "export_id": row["export_id"],
        "snapshot_id": row["snapshot_id"],
        "requester": row["requester"],
        "purpose": row["purpose"],
        "fields": json.loads(row["fields_json"]),
        "row_count": row["row_count"],
        "dropped_withdrawn": row["dropped_withdrawn"],
        "content_hash": row["content_hash"],
        "created_at": row["created_at"],
        "policy": {
            "k_anonymity": K_ANONYMITY,
            "member_id": "hmac-sha256(export-scoped salt, participant_id)",
            "time_resolution": "quarter",
            "age_resolution": "5-year band",
            "imaging_params": "aggregate-only via imaging-summary",
        },
        "rows": json.loads(row["rows_json"]),
    }


def _blocked(store, requester, snapshot_id, reason, detail):
    store.audit_standalone(requester, "export_blocked", "snapshot", snapshot_id,
                           {"reason": reason, **detail})


def _pseudonym(export_id, participant_id):
    digest = hmac.new(export_id.encode("utf-8"), participant_id.encode("utf-8"),
                      hashlib.sha256).hexdigest()
    return f"m_{digest[:16]}"


def _age_band(age):
    if age < 0:
        return "unknown"
    low = (age // 5) * 5
    return f"{low}-{low + 4}"


def _quarter(date_str):
    year, month = int(date_str[:4]), int(date_str[5:7])
    return f"{year}Q{(month - 1) // 3 + 1}"
