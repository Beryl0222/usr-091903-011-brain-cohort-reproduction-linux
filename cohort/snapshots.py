"""分析快照：冻结、清单、复核与队列构成。

冻结语义：
- 快照在冻结时刻（as_of）解析队列，把每位成员、每次访视所引用的
  记录版本（record_id + version + payload_hash）以及所依据的同意版本
  全部钉入清单，并对清单计算内容哈希；
- 之后的迟到上传只会产生新版本记录，重复上传被幂等去重，
  两者都不会触碰已冻结的清单；
- verify 可随时重算哈希并逐条核对被钉住的记录，供统计人员复核
  某篇稿件实际纳入的人群与代码版本。
"""

import json

from .common import as_datetime, new_id, now_iso, payload_hash, require, scope_covers
from .domain import COHORT_GROUPS
from .errors import ApiError, not_found

FLAG_DESCRIPTIONS = {
    "self_report_staging": "部分受试者的人生阶段仅靠自报判定",
    "incomplete_hormone_data": "激素检测未覆盖全部受试者",
    "heterogeneous_staging_methods": "各组人生阶段判定方式不一致",
    "repeated_measures": "包含同一受试者的重复测量",
    "qc_exclusions_present": "存在因质控未通过而被排除的访视",
    "small_group": "至少一组样本量较小（<20）",
}

SMALL_GROUP_THRESHOLD = 20


def freeze_snapshot(store, body, query=None):
    name = require(body, "name")
    purpose = require(body, "purpose")
    code_version = require(body, "code_version")
    created_by = require(body, "created_by")
    spec = body.get("cohort_spec") or {}
    if not isinstance(spec, dict):
        raise ApiError(422, "invalid_value", "cohort_spec 必须是对象", {"field": "cohort_spec"})

    groups = spec.get("groups") or list(COHORT_GROUPS)
    for group in groups:
        if group not in COHORT_GROUPS:
            raise ApiError(422, "invalid_value", f"未知分组: {group}",
                           {"field": "cohort_spec.groups", "allowed": list(COHORT_GROUPS)})
    normalized_spec = {
        "groups": list(groups),
        "require_qc_pass": bool(spec.get("require_qc_pass", True)),
        "require_imaging": bool(spec.get("require_imaging", True)),
        "as_of": as_datetime(spec["as_of"], "cohort_spec.as_of") if spec.get("as_of") else now_iso(),
    }

    members, composition, flags, excluded_detail = build_cohort(
        store,
        purpose=purpose,
        groups=normalized_spec["groups"],
        require_qc_pass=normalized_spec["require_qc_pass"],
        require_imaging=normalized_spec["require_imaging"],
        as_of=normalized_spec["as_of"],
    )

    manifest = {
        "snapshot": {
            "name": name,
            "purpose": purpose,
            "code_version": code_version,
            "cohort_spec": normalized_spec,
        },
        "members": members,
        "excluded_detail": excluded_detail,
    }
    sid = new_id("snp")
    created_at = now_iso()
    content_hash = payload_hash(manifest)

    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO snapshots(snapshot_id, name, purpose, code_version, cohort_spec_json,"
            " manifest_json, composition_json, limitation_flags_json, content_hash, created_by, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                sid, name, purpose, code_version,
                json.dumps(normalized_spec, ensure_ascii=False, sort_keys=True),
                json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                json.dumps(composition, ensure_ascii=False, sort_keys=True),
                json.dumps(flags, ensure_ascii=False),
                content_hash, created_by, created_at,
            ),
        )
        store.audit(conn, created_by, "snapshot_frozen", "snapshot", sid, {
            "purpose": purpose,
            "code_version": code_version,
            "content_hash": content_hash,
            "participants": composition["total_participants"],
            "visits": composition["total_visits"],
        })
    return 201, snapshot_view(store, sid)


def build_cohort(store, purpose, groups, require_qc_pass, require_imaging, as_of):
    """按 as_of 的系统时间视角解析队列。所有筛选都用服务器生成的
    recorded_at / created_at，临床日期（visit_date 等）只作为数据。"""
    excluded = {
        "withdrawn": 0,
        "no_consent": 0,
        "no_stage_event": 0,
        "group_mismatch": 0,
        "participant_excluded": 0,
        "visit_excluded": 0,
        "qc_not_passed": 0,
        "no_imaging": 0,
        "no_visits": 0,
    }
    excluded_detail = []
    members = []

    participants = store.query(
        "SELECT * FROM participants WHERE created_at <= ? ORDER BY participant_id", (as_of,))
    for participant in participants:
        pid = participant["participant_id"]

        withdrawal = store.query_one(
            "SELECT * FROM withdrawals WHERE participant_id=? AND recorded_at <= ?", (pid, as_of))
        if withdrawal and scope_covers(json.loads(withdrawal["scope_json"]), purpose):
            excluded["withdrawn"] += 1
            excluded_detail.append({"participant_id": pid, "cause": "withdrawn",
                                    "aggregate_handling": withdrawal["aggregate_handling"]})
            continue

        consent = store.query_one(
            "SELECT * FROM consents WHERE participant_id=? AND recorded_at <= ?"
            " ORDER BY recorded_at DESC, id DESC LIMIT 1",
            (pid, as_of),
        )
        if not consent or not scope_covers(json.loads(consent["scope_json"]), purpose):
            excluded["no_consent"] += 1
            excluded_detail.append({"participant_id": pid, "cause": "no_consent"})
            continue

        stage = store.query_one(
            "SELECT * FROM stage_events WHERE participant_id=? AND recorded_at <= ?"
            " ORDER BY event_date DESC, recorded_at DESC, id DESC LIMIT 1",
            (pid, as_of),
        )
        if not stage:
            excluded["no_stage_event"] += 1
            excluded_detail.append({"participant_id": pid, "cause": "no_stage_event"})
            continue
        if stage["stage"] not in groups:
            excluded["group_mismatch"] += 1
            continue

        participant_exclusion = store.query_one(
            "SELECT * FROM exclusions WHERE participant_id=? AND visit_id IS NULL AND recorded_at <= ?"
            " ORDER BY recorded_at DESC, id DESC LIMIT 1",
            (pid, as_of),
        )
        if participant_exclusion:
            excluded["participant_excluded"] += 1
            excluded_detail.append({"participant_id": pid, "cause": "participant_excluded",
                                    "stage": participant_exclusion["stage"],
                                    "reason": participant_exclusion["reason"]})
            continue

        visits = []
        visit_rows = store.query(
            "SELECT * FROM visits WHERE participant_id=? AND created_at <= ? ORDER BY visit_index",
            (pid, as_of),
        )
        for visit in visit_rows:
            vid = visit["visit_id"]
            visit_exclusion = store.query_one(
                "SELECT * FROM exclusions WHERE visit_id=? AND recorded_at <= ?"
                " ORDER BY recorded_at DESC, id DESC LIMIT 1",
                (vid, as_of),
            )
            if visit_exclusion:
                excluded["visit_excluded"] += 1
                excluded_detail.append({"participant_id": pid, "visit_id": vid,
                                        "cause": "visit_excluded",
                                        "stage": visit_exclusion["stage"],
                                        "reason": visit_exclusion["reason"]})
                continue

            imaging = _latest_version(store, "imaging_records", vid, as_of)
            if require_imaging and not imaging:
                excluded["no_imaging"] += 1
                excluded_detail.append({"participant_id": pid, "visit_id": vid, "cause": "no_imaging"})
                continue

            qc = _latest_version(store, "qc_records", vid, as_of)
            if require_qc_pass and (not qc or qc["conclusion"] != "pass"):
                excluded["qc_not_passed"] += 1
                excluded_detail.append({
                    "participant_id": pid, "visit_id": vid, "cause": "qc_not_passed",
                    "conclusion": qc["conclusion"] if qc else None,
                    "reasons": json.loads(qc["reasons_json"]) if qc else [],
                    "missing_reason": qc["missing_reason"] if qc else None,
                })
                continue

            metrics = _latest_version(store, "derived_metrics", vid, as_of)
            visits.append({
                "visit_id": vid,
                "visit_index": visit["visit_index"],
                "visit_date": visit["visit_date"],
                "imaging": _pin(imaging),
                "qc": _pin(qc, extra={"conclusion": qc["conclusion"]}) if qc else None,
                "metrics": _pin(metrics, extra={"pipeline_version": metrics["pipeline_version"]}) if metrics else None,
            })

        if not visits:
            excluded["no_visits"] += 1
            continue

        hormone_assayed = store.query_one(
            "SELECT 1 FROM stage_events WHERE participant_id=? AND determination_method='hormone_assay'"
            " AND recorded_at <= ? LIMIT 1",
            (pid, as_of),
        )
        match = store.query_one(
            "SELECT * FROM control_matches WHERE participant_id=? AND recorded_at <= ?"
            " ORDER BY recorded_at DESC, id DESC LIMIT 1",
            (pid, as_of),
        )
        members.append({
            "participant_id": pid,
            "group": stage["stage"],
            "consent_version": consent["version"],
            "stage_event": {
                "stage": stage["stage"],
                "determination_method": stage["determination_method"],
                "event_date": stage["event_date"],
            },
            "hormone_assayed": bool(hormone_assayed),
            "matched_control": (
                {"control_id": match["control_id"], "match_set_id": match["match_set_id"],
                 "matched_on": json.loads(match["matched_on_json"])}
                if match else None
            ),
            "visits": visits,
        })

    composition = _composition(members, excluded)
    flags = _limitation_flags(members, excluded)
    return members, composition, flags, excluded_detail


def _latest_version(store, table, vid, as_of):
    return store.query_one(
        f"SELECT * FROM {table} WHERE visit_id=? AND recorded_at <= ?"
        " ORDER BY version DESC LIMIT 1",
        (vid, as_of),
    )


def _pin(row, extra=None):
    if not row:
        return None
    pinned = {"record_id": row["record_id"], "version": row["version"], "payload_hash": row["payload_hash"]}
    if extra:
        pinned.update(extra)
    return pinned


def _composition(members, excluded):
    groups = {}
    for member in members:
        group = groups.setdefault(member["group"], {
            "participants": 0,
            "visits": 0,
            "determination_methods": {},
            "self_report": 0,
            "hormone_assayed": 0,
        })
        group["participants"] += 1
        group["visits"] += len(member["visits"])
        method = member["stage_event"]["determination_method"]
        group["determination_methods"][method] = group["determination_methods"].get(method, 0) + 1
        if method == "self_report":
            group["self_report"] += 1
        if member["hormone_assayed"]:
            group["hormone_assayed"] += 1
    return {
        "groups": groups,
        "excluded": excluded,
        "total_participants": len(members),
        "total_visits": sum(len(m["visits"]) for m in members),
    }


def _limitation_flags(members, excluded):
    flags = []
    methods = {
        m["stage_event"]["determination_method"] for m in members
    } - {"not_applicable"}
    if "self_report" in methods:
        flags.append("self_report_staging")
    if any(not m["hormone_assayed"] for m in members):
        flags.append("incomplete_hormone_data")
    if len(methods) > 1:
        flags.append("heterogeneous_staging_methods")
    if any(len(m["visits"]) > 1 for m in members):
        flags.append("repeated_measures")
    if excluded.get("qc_not_passed", 0) > 0:
        flags.append("qc_exclusions_present")
    group_sizes = {}
    for member in members:
        group_sizes[member["group"]] = group_sizes.get(member["group"], 0) + 1
    if any(size < SMALL_GROUP_THRESHOLD for size in group_sizes.values()):
        flags.append("small_group")
    return flags


# ---------------------------------------------------------------- 读取与复核

def must_snapshot(store, sid):
    row = store.query_one("SELECT * FROM snapshots WHERE snapshot_id=?", (sid,))
    if not row:
        raise not_found("snapshot", sid)
    return row


def withdrawn_since_freeze(store, snapshot_row):
    """清单冻结后又有成员撤回（覆盖快照用途）的数量。"""
    manifest = json.loads(snapshot_row["manifest_json"])
    purpose = snapshot_row["purpose"]
    count = 0
    for member in manifest["members"]:
        withdrawal = store.query_one(
            "SELECT * FROM withdrawals WHERE participant_id=?", (member["participant_id"],))
        if withdrawal and scope_covers(json.loads(withdrawal["scope_json"]), purpose):
            count += 1
    return count


def snapshot_view(store, sid):
    row = must_snapshot(store, sid)
    flags = json.loads(row["limitation_flags_json"])
    return {
        "snapshot_id": row["snapshot_id"],
        "name": row["name"],
        "purpose": row["purpose"],
        "code_version": row["code_version"],
        "cohort_spec": json.loads(row["cohort_spec_json"]),
        "content_hash": row["content_hash"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "composition": json.loads(row["composition_json"]),
        "limitation_flags": [
            {"flag": flag, "description": FLAG_DESCRIPTIONS.get(flag, flag)} for flag in flags
        ],
        "withdrawn_since_freeze": withdrawn_since_freeze(store, row),
    }


def get_snapshot(store, body=None, query=None, sid=None):
    return 200, snapshot_view(store, sid)


def list_snapshots(store, body=None, query=None):
    rows = store.query("SELECT snapshot_id FROM snapshots ORDER BY created_at, snapshot_id")
    return 200, {"snapshots": [snapshot_view(store, r["snapshot_id"]) for r in rows]}


def get_manifest(store, body=None, query=None, sid=None):
    row = must_snapshot(store, sid)
    return 200, json.loads(row["manifest_json"])


def verify_snapshot(store, body=None, query=None, sid=None):
    """重算内容哈希，并逐条核对清单钉住的记录版本是否原样存在。
    冻结后产生的新版本只会体现在 superseded_since_freeze 上，不影响有效性。"""
    row = must_snapshot(store, sid)
    manifest = json.loads(row["manifest_json"])
    tables = {"imaging": "imaging_records", "qc": "qc_records", "metrics": "derived_metrics"}
    checked = missing = mismatched = superseded = 0
    for member in manifest["members"]:
        for visit in member["visits"]:
            for kind, table in tables.items():
                ref = visit.get(kind)
                if not ref:
                    continue
                checked += 1
                record = store.query_one(
                    f"SELECT record_id, payload_hash, superseded_by FROM {table} WHERE record_id=?",
                    (ref["record_id"],),
                )
                if not record:
                    missing += 1
                    continue
                if record["payload_hash"] != ref["payload_hash"]:
                    mismatched += 1
                if record["superseded_by"]:
                    superseded += 1
    hash_match = payload_hash(manifest) == row["content_hash"]
    return 200, {
        "snapshot_id": sid,
        "valid": hash_match and missing == 0 and mismatched == 0,
        "content_hash_match": hash_match,
        "records_checked": checked,
        "records_missing": missing,
        "hash_mismatches": mismatched,
        "superseded_since_freeze": superseded,
    }


def imaging_summary(store, body=None, query=None, sid=None):
    """影像参数的聚合视图：只有分布统计，不含任何行级记录、
    时间点或个体标识，可安全用于方法学描述。"""
    row = must_snapshot(store, sid)
    manifest = json.loads(row["manifest_json"])
    scanner_counts = {}
    numeric = {}
    visits_with_imaging = 0
    for member in manifest["members"]:
        for visit in member["visits"]:
            pin = visit.get("imaging")
            if not pin:
                continue
            record = store.query_one(
                "SELECT params_json FROM imaging_records WHERE record_id=?", (pin["record_id"],))
            if not record:
                continue
            visits_with_imaging += 1
            params = json.loads(record["params_json"])
            scanner = params.get("scanner_model", "unknown")
            scanner_counts[scanner] = scanner_counts.get(scanner, 0) + 1
            for key, value in params.items():
                if key == "scanner_model":
                    continue
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    numeric.setdefault(key, []).append(value)
    parameters = {
        key: {
            "min": min(values),
            "max": max(values),
            "mean": round(sum(values) / len(values), 4),
            "n": len(values),
        }
        for key, values in sorted(numeric.items())
        if values
    }
    return 200, {
        "snapshot_id": sid,
        "visits_with_imaging": visits_with_imaging,
        "scanner_models": scanner_counts,
        "parameters": parameters,
    }
