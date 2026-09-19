"""对外结论（claim）登记：结论必须绑定快照、适用范围与局限。

传播团队发布任何结论（如"未见加速萎缩"）时，系统强制：
- 结论必须引用一个已冻结快照（证据来源可复核）；
- 必须给出适用范围（人群、结论适用的比较/场景）；
- 必须给出非空的局限说明；
- 必须逐项确认快照自动识别的局限标记（如阶段判定含自报、
  激素检测未全覆盖），未确认的标记会阻止登记并留痕。
"""

import json

from .common import as_str_list, new_id, now_iso, require
from .errors import ApiError
from .snapshots import FLAG_DESCRIPTIONS, must_snapshot, snapshot_view

REQUIRED_SCOPE_KEYS = ("population", "applies_to")


def register_claim(store, body, query=None):
    snapshot = must_snapshot(store, require(body, "snapshot_id"))
    statement = require(body, "statement")
    if len(statement) > 500:
        raise ApiError(422, "invalid_value", "statement 过长（<=500 字）", {"field": "statement"})
    created_by = require(body, "created_by")

    scope = require(body, "scope")
    if not isinstance(scope, dict):
        raise ApiError(422, "invalid_value", "scope 必须是对象", {"field": "scope"})
    missing_scope = [key for key in REQUIRED_SCOPE_KEYS if not scope.get(key)]
    if missing_scope:
        raise ApiError(
            422, "missing_field", "适用范围缺少必填项",
            {"missing": missing_scope, "required": list(REQUIRED_SCOPE_KEYS)})

    limitations = as_str_list(require(body, "limitations"), "limitations")
    acknowledged = body.get("acknowledged_flags") or []
    if not isinstance(acknowledged, list):
        raise ApiError(422, "invalid_value", "acknowledged_flags 必须是数组",
                       {"field": "acknowledged_flags"})

    flags = json.loads(snapshot["limitation_flags_json"])
    missing_flags = [flag for flag in flags if flag not in acknowledged]
    if missing_flags:
        store.audit_standalone(created_by, "claim_blocked", "snapshot", snapshot["snapshot_id"], {
            "reason": "unacknowledged_limitations", "missing": missing_flags})
        raise ApiError(
            422,
            "unacknowledged_limitations",
            "快照存在未被确认的局限标记，发布结论前必须逐项确认并在局限中说明",
            {"missing": [
                {"flag": flag, "description": FLAG_DESCRIPTIONS.get(flag, flag)}
                for flag in missing_flags
            ]},
        )

    cid = new_id("clm")
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO claims(claim_id, snapshot_id, statement, scope_json, limitations_json,"
            " acknowledged_flags_json, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                cid, snapshot["snapshot_id"], statement,
                json.dumps(scope, ensure_ascii=False, sort_keys=True),
                json.dumps(limitations, ensure_ascii=False),
                json.dumps(acknowledged, ensure_ascii=False),
                created_by, now_iso(),
            ),
        )
        store.audit(conn, created_by, "claim_registered", "claim", cid,
                    {"snapshot_id": snapshot["snapshot_id"], "statement": statement})
    return 201, claim_view(store, store.query_one("SELECT * FROM claims WHERE claim_id=?", (cid,)))


def get_claim(store, body=None, query=None, cid=None):
    row = store.query_one("SELECT * FROM claims WHERE claim_id=?", (cid,))
    if not row:
        from .errors import not_found
        raise not_found("claim", cid)
    return 200, claim_view(store, row)


def claim_view(store, row):
    snapshot = store.query_one("SELECT * FROM snapshots WHERE snapshot_id=?", (row["snapshot_id"],))
    return {
        "claim_id": row["claim_id"],
        "snapshot_id": row["snapshot_id"],
        "statement": row["statement"],
        "scope": json.loads(row["scope_json"]),
        "limitations": json.loads(row["limitations_json"]),
        "acknowledged_flags": json.loads(row["acknowledged_flags_json"]),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        # 证据档案：让审阅者能核对该结论实际基于的人群与代码版本
        "evidence": {
            "content_hash": snapshot["content_hash"],
            "code_version": snapshot["code_version"],
            "purpose": snapshot["purpose"],
            "composition": json.loads(snapshot["composition_json"]),
            "limitation_flags": snapshot_view(store, snapshot["snapshot_id"])["limitation_flags"],
            "withdrawn_since_freeze": snapshot_view(store, snapshot["snapshot_id"])["withdrawn_since_freeze"],
        },
    }
