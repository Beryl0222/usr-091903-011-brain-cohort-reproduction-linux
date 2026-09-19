"""存储层：批次幂等、冻结快照、撤回版本化、结论与导出门控。

所有写操作都走显式事务；冻结表的不可变性另外由 schema 触发器兜底。
时间统一使用 UTC ISO-8601 字符串，便于排序与复现。
"""

import json
import secrets
import sqlite3
from datetime import datetime, timezone

from . import rules
from .schema import connect

ACCEPTED_BATCH_STATES = ("accepted", "accepted_idempotent")


class _ConflictAbort(Exception):
    """行级去重发现同键不同内容：中止当前批次事务。"""


class DomainError(Exception):
    """可映射为 HTTP 4xx 的领域错误。"""

    def __init__(self, code, message, status=400, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or []


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# 参与者级导出允许从冻结快照中取出的字段（其余列一律拒）
EXPORTABLE_MEMBER_FIELDS = frozenset(
    {
        "pseudonym",
        "cohort_group",
        "life_stage",
        "assessment_method",
        "hormone_assay_done",
        "visit_month",
        "visit_year",
    }
)


class CohortStore:
    def __init__(self, db_path=":memory:"):
        self.conn = connect(db_path)

    def close(self):
        self.conn.close()

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------
    def _audit(self, action, entity, entity_id="", detail=None, actor=""):
        self.conn.execute(
            "INSERT INTO audit_log(ts, actor, action, entity, entity_id, detail_json)"
            " VALUES (?,?,?,?,?,?)",
            (_now(), actor, action, entity, entity_id,
             rules.canonical_json(detail or {})),
        )

    # ------------------------------------------------------------------
    # 主数据
    # ------------------------------------------------------------------
    def register_consent_version(self, version_code, title, released_at,
                                 scope_text, terms=None, commit=True):
        self.conn.execute(
            "INSERT OR IGNORE INTO consent_versions"
            "(version_code,title,released_at,scope_text,terms_json)"
            " VALUES (?,?,?,?,?)",
            (version_code, title, released_at, scope_text,
             rules.canonical_json(terms or {})),
        )
        if commit:
            self.conn.commit()

    def add_participant(self, participant_id, cohort_group,
                        birth_year=None, batch_id=None):
        self.conn.execute(
            "INSERT OR IGNORE INTO participants"
            "(participant_id,cohort_group,birth_year,created_batch,created_at)"
            " VALUES (?,?,?,?,?)",
            (participant_id, cohort_group, birth_year, batch_id, _now()),
        )

    # ------------------------------------------------------------------
    # 上传批次（迟到 / 重复）
    # ------------------------------------------------------------------
    def ingest_batch(self, batch_id, source, payload, actor=""):
        """接收一个上传批次。

        返回批次摘要字典。三种幂等语义：

        - 同 ``batch_id`` 同内容         → accepted_idempotent，不重复写行；
        - 不同 ``batch_id`` 但内容指纹相同 → duplicate，指向首个批次；
        - 同 ``batch_id`` 不同内容，或行键冲突 → rejected_conflict，整批不落库。

        迟到数据（在某个快照冻结之后到达）照常进入活动表，但任何已冻结
        快照都不会因此改变——成员清单在冻结时已物化。
        """
        if not isinstance(payload, dict):
            raise DomainError("PAYLOAD_NOT_OBJECT", "上传内容必须是 JSON 对象")
        content_hash = rules.stable_hash(payload)

        existing_id = self.conn.execute(
            "SELECT * FROM upload_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if existing_id:
            if existing_id["content_hash"] == content_hash:
                return self._batch_summary(existing_id)
            raise DomainError(
                "BATCH_CONFLICT",
                f"批次 {batch_id} 已以不同内容上传过，禁止覆盖",
                status=409,
            )

        twin = self.conn.execute(
            "SELECT batch_id FROM upload_batches WHERE content_hash=?",
            (content_hash,),
        ).fetchone()
        if twin:
            self.conn.execute(
                "INSERT INTO upload_batches(batch_id,source,received_at,"
                "content_hash,status,duplicate_of,note) VALUES (?,?,?,?,?,?,?)",
                (batch_id, source, _now(), content_hash, "duplicate",
                 twin["batch_id"], "内容与既有批次完全相同，按重复上传处理"),
            )
            self._audit("upload_duplicate", "batch", batch_id,
                        {"duplicate_of": twin["batch_id"]}, actor)
            self.conn.commit()
            return self._batch_summary(
                self.conn.execute(
                    "SELECT * FROM upload_batches WHERE batch_id=?", (batch_id,)
                ).fetchone()
            )

        new_rows = 0
        duplicate_rows = 0
        conflicts = []

        # 先登记 pending 批次：数据行外键 recorded_batch 必须指向已存在的批次。
        # 若随后行级冲突，事务回滚，本记录一并回滚，再在事务外登记拒收。
        self.conn.execute(
            "INSERT INTO upload_batches(batch_id,source,received_at,"
            "content_hash,status,note) VALUES (?,?,?,?,'pending','')",
            (batch_id, source, _now(), content_hash))

        try:
            with self.conn:
                for cv in payload.get("consent_versions", []):
                    self.register_consent_version(
                        cv["version_code"], cv["title"], cv["released_at"],
                        cv["scope_text"], cv.get("terms"), commit=False)

                for p in payload.get("participants", []):
                    kind = self._dedup_insert(
                        "participants",
                        ("participant_id",),
                        ("participant_id", "cohort_group", "birth_year"),
                        {
                            "participant_id": p["participant_id"],
                            "cohort_group": p["cohort_group"],
                            "birth_year": p.get("birth_year"),
                        },
                        conflicts,
                        extra=("created_batch", "created_at"),
                        extra_values=(batch_id, _now()),
                    )
                    new_rows += kind == "new"
                    duplicate_rows += kind == "duplicate"

                for e in payload.get("consent_events", []):
                    kind = self._dedup_insert(
                        "consent_events",
                        ("participant_id", "version_code", "action",
                         "effective_at"),
                        ("participant_id", "version_code", "action",
                         "effective_at", "note"),
                        {
                            "participant_id": e["participant_id"],
                            "version_code": e["version_code"],
                            "action": e["action"],
                            "effective_at": e["effective_at"],
                            "note": e.get("note", ""),
                        },
                        conflicts,
                        extra=("recorded_batch",),
                        extra_values=(batch_id,),
                    )
                    new_rows += kind == "new"
                    duplicate_rows += kind == "duplicate"

                for v in payload.get("visits", []):
                    kind = self._dedup_insert(
                        "life_stage_visits",
                        ("participant_id", "visit_date"),
                        ("participant_id", "visit_date", "life_stage",
                         "stage_detail", "assessment_method",
                         "evidence_fields_json", "hormone_assay_done",
                         "hormone_assay_count"),
                        {
                            "participant_id": v["participant_id"],
                            "visit_date": v["visit_date"],
                            "life_stage": v["life_stage"],
                            "stage_detail": v.get("stage_detail", ""),
                            "assessment_method": v["assessment_method"],
                            "evidence_fields_json": rules.canonical_json(
                                v.get("evidence_fields", {})),
                            "hormone_assay_done":
                                1 if v.get("hormone_assay_done") else 0,
                            "hormone_assay_count":
                                int(v.get("hormone_assay_count", 0)),
                        },
                        conflicts,
                        extra=("recorded_batch",),
                        extra_values=(batch_id,),
                    )
                    new_rows += kind == "new"
                    duplicate_rows += kind == "duplicate"

                for c in payload.get("controls", []):
                    kind = self._dedup_insert(
                        "matched_controls",
                        ("visit_participant_id", "visit_date",
                         "control_participant_id"),
                        ("visit_participant_id", "visit_date",
                         "control_participant_id", "matched_on_json"),
                        {
                            "visit_participant_id": c["visit_participant_id"],
                            "visit_date": c["visit_date"],
                            "control_participant_id": c["control_participant_id"],
                            "matched_on_json": rules.canonical_json(
                                c.get("matched_on", {})),
                        },
                        conflicts,
                        extra=("recorded_batch",),
                        extra_values=(batch_id,),
                    )
                    new_rows += kind == "new"
                    duplicate_rows += kind == "duplicate"

                for m in payload.get("mri", []):
                    kind = self._dedup_insert(
                        "mri_acquisitions",
                        ("participant_id", "visit_date"),
                        ("participant_id", "visit_date", "scanner_id",
                         "field_tesla", "protocol_fingerprint",
                         "series_uid_hash", "parameters_json"),
                        {
                            "participant_id": m["participant_id"],
                            "visit_date": m["visit_date"],
                            "scanner_id": m["scanner_id"],
                            "field_tesla": m.get("field_tesla"),
                            "protocol_fingerprint":
                                m.get("protocol_fingerprint", ""),
                            "series_uid_hash":
                                rules.stable_hash("series_uid", m["series_uid"]),
                            "parameters_json": rules.canonical_json(
                                m.get("parameters", {})),
                        },
                        conflicts,
                        extra=("recorded_batch",),
                        extra_values=(batch_id,),
                    )
                    new_rows += kind == "new"
                    duplicate_rows += kind == "duplicate"

                for q in payload.get("qc", []):
                    kind = self._dedup_insert(
                        "qc_reviews",
                        ("participant_id", "visit_date"),
                        ("participant_id", "visit_date", "decision",
                         "rationale", "reviewer"),
                        {
                            "participant_id": q["participant_id"],
                            "visit_date": q["visit_date"],
                            "decision": q["decision"],
                            "rationale": q.get("rationale", ""),
                            "reviewer": q.get("reviewer", ""),
                        },
                        conflicts,
                        extra=("recorded_batch",),
                        extra_values=(batch_id,),
                    )
                    new_rows += kind == "new"
                    duplicate_rows += kind == "duplicate"

                for d in payload.get("metrics", []):
                    is_missing = 1 if d.get("is_missing") else 0
                    kind = self._dedup_insert(
                        "derived_metrics",
                        ("participant_id", "visit_date", "metric_code",
                         "metric_version"),
                        ("participant_id", "visit_date", "metric_code", "value",
                         "is_missing", "missing_reason", "metric_version"),
                        {
                            "participant_id": d["participant_id"],
                            "visit_date": d["visit_date"],
                            "metric_code": d["metric_code"],
                            "value": None if is_missing else d.get("value"),
                            "is_missing": is_missing,
                            "missing_reason": d.get("missing_reason", ""),
                            "metric_version": d["metric_version"],
                        },
                        conflicts,
                        extra=("recorded_batch",),
                        extra_values=(batch_id,),
                    )
                    new_rows += kind == "new"
                    duplicate_rows += kind == "duplicate"

                if conflicts:
                    raise _ConflictAbort()

                self.conn.execute(
                    "UPDATE upload_batches SET status=?,"
                    "new_row_count=?,duplicate_rows=?,conflicts_json=?,note=?"
                    " WHERE batch_id=?",
                    ("accepted_idempotent" if new_rows == 0 and duplicate_rows
                     else "accepted",
                     new_rows, duplicate_rows,
                     rules.canonical_json(conflicts),
                     "全部为既有重复行" if new_rows == 0 and duplicate_rows
                     else f"新增 {new_rows} 行，重复 {duplicate_rows} 行",
                     batch_id),
                )
                self._audit("upload_accept", "batch", batch_id,
                            {"new_rows": new_rows,
                             "duplicate_rows": duplicate_rows}, actor)
        except _ConflictAbort:
            # with 已回滚数据行；批次拒收记录单独落库
            self.conn.execute(
                "INSERT INTO upload_batches(batch_id,source,received_at,"
                "content_hash,status,duplicate_rows,conflicts_json,note)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (batch_id, source, _now(), content_hash, "rejected_conflict",
                 0, rules.canonical_json(conflicts),
                 "行键存在但内容不一致，整批拒收，未改动任何既有数据"),
            )
            self._audit("upload_reject", "batch", batch_id,
                        {"conflicts": conflicts}, actor)
            self.conn.commit()
            raise DomainError(
                "BATCH_ROW_CONFLICT",
                "批次含与既有数据冲突的行，整批拒收",
                status=409,
                details=conflicts,
            )

        self.conn.commit()
        return self.get_batch(batch_id)

    def _dedup_insert(self, table, key_cols, all_cols, row, conflicts,
                      extra=(), extra_values=()):
        """通用行级去重。返回 'new' / 'duplicate'；冲突时记录并抛 _ConflictAbort。

        内容比对只覆盖业务列（``all_cols``）；``extra`` 中的批次/时间戳等
        溯源字段不参与比较，否则同内容重复上传会被误判为冲突。
        """
        cols = list(all_cols) + list(extra)
        values = [row[c] for c in all_cols] + list(extra_values)
        business_values = [row[c] for c in all_cols]
        where = " AND ".join(f"{c}=?" for c in key_cols)
        existing = self.conn.execute(
            f"SELECT {', '.join(all_cols)} FROM {table} WHERE {where}",
            [row[c] for c in key_cols],
        ).fetchone()
        if existing is not None:
            if tuple(existing) == tuple(business_values):
                return "duplicate"
            conflicts.append(
                {"table": table, "key": {c: row[c] for c in key_cols},
                 "existing": dict(existing), "incoming": dict(zip(cols, values))}
            )
            raise _ConflictAbort()
        placeholders = ", ".join("?" for _ in cols)
        try:
            self.conn.execute(
                f"INSERT INTO {table}({', '.join(cols)}) VALUES ({placeholders})",
                values,
            )
        except sqlite3.IntegrityError as exc:
            conflicts.append({"table": table,
                              "key": {c: row[c] for c in key_cols},
                              "error": str(exc)})
            raise _ConflictAbort()
        return "new"

    def get_batch(self, batch_id):
        row = self.conn.execute(
            "SELECT * FROM upload_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise DomainError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在", 404)
        return self._batch_summary(row)

    def list_batches(self):
        rows = self.conn.execute(
            "SELECT * FROM upload_batches ORDER BY received_at"
        ).fetchall()
        return [self._batch_summary(r) for r in rows]

    @staticmethod
    def _batch_summary(row):
        return {
            "batch_id": row["batch_id"],
            "source": row["source"],
            "received_at": row["received_at"],
            "status": row["status"],
            "duplicate_of": row["duplicate_of"],
            "new_row_count": row["new_row_count"],
            "duplicate_rows": row["duplicate_rows"],
            "conflicts": json.loads(row["conflicts_json"]),
            "note": row["note"],
        }

    # ------------------------------------------------------------------
    # 同意状态与撤回
    # ------------------------------------------------------------------
    def _version_states(self, participant_id, as_of, visible_batches=None):
        """截至 as_of 各同意版本的最新状态：{version: 'given'|'withdrawn'}。

        ``visible_batches`` 给定时只统计这些批次记录的同意事件，
        使按数据截止批次冻结的快照可以排除"迟到上传的同意历史"。
        """
        sql = ("SELECT version_code, action FROM consent_events"
               " WHERE participant_id=? AND effective_at<=?")
        params = [participant_id, as_of]
        if visible_batches is not None:
            if not visible_batches:
                return {}
            placeholders = ",".join("?" for _ in visible_batches)
            sql += f" AND recorded_batch IN ({placeholders})"
            params.extend(sorted(visible_batches))
        sql += " ORDER BY effective_at, id"
        rows = self.conn.execute(sql, params).fetchall()
        states = {}
        for r in rows:
            states[r["version_code"]] = r["action"]
        return states

    def consent_for_use(self, participant_id, visit_date, as_of,
                        visible_batches=None):
        """判断访视数据在 as_of 时点能否用于新分析。

        返回 (是否可用, 排除理由, 授权同意版本)。撤回是"对新使用"的 prospective
        停止：访视之后发生的撤回会让该参与者的所有访视无法进入 *新* 快照。
        """
        at_visit = {v for v, a in self._version_states(
            participant_id, visit_date, visible_batches).items()
            if a == "given"}
        if not at_visit:
            return False, "consent_not_active", ""
        now_states = self._version_states(participant_id, as_of)
        still_active = {v for v in at_visit if now_states.get(v) == "given"}
        if not still_active:
            withdrawn_after_visit = self.conn.execute(
                "SELECT 1 FROM consent_events"
                " WHERE participant_id=? AND action='withdrawn'"
                " AND effective_at>? AND effective_at<=? LIMIT 1",
                (participant_id, visit_date, as_of),
            ).fetchone()
            reason = ("consent_withdrawn" if withdrawn_after_visit
                      else "consent_not_active")
            return False, reason, ""
        # 授权版本取最晚给予、且仍有效的一个
        chosen = max(
            still_active,
            key=lambda v: self.conn.execute(
                "SELECT MAX(effective_at) FROM consent_events"
                " WHERE participant_id=? AND version_code=? AND action='given'",
                (participant_id, v),
            ).fetchone()[0],
        )
        return True, "", chosen

    def withdraw_participant(self, participant_id, effective_at,
                             version_code=None, actor="", note=""):
        """登记撤回；不指定版本则撤回该时点仍有效的全部版本。

        既有冻结分析不受影响（触发器保证），但系统记录受影响快照，
        需随后对其中既有聚合结果登记处置（retain/restrict_access/remove）。
        """
        participant = self.conn.execute(
            "SELECT 1 FROM participants WHERE participant_id=?", (participant_id,)
        ).fetchone()
        if not participant:
            raise DomainError("PARTICIPANT_NOT_FOUND",
                              f"参与者 {participant_id} 不存在", 404)

        if version_code:
            targets = [version_code]
        else:
            states = self._version_states(participant_id, effective_at)
            targets = [v for v, a in states.items() if a == "given"]
        if not targets:
            raise DomainError("NO_ACTIVE_CONSENT",
                              "该参与者在撤回时点没有有效同意可撤回", 409)

        affected = self.snapshots_for_participant(participant_id)
        with self.conn:
            for v in targets:
                self.conn.execute(
                    "INSERT INTO consent_events(participant_id,version_code,"
                    "action,effective_at,note) VALUES (?,?,?,?,?)",
                    (participant_id, v, "withdrawn", effective_at,
                     note or "参与者撤回知情同意"),
                )
            self._audit("consent_withdraw", "participant", participant_id,
                        {"effective_at": effective_at, "versions": targets,
                         "affected_snapshots": [a["snapshot_id"] for a in affected]},
                        actor)
        return {"participant_id": participant_id,
                "withdrawn_versions": targets,
                "affected_snapshots": [a["snapshot_id"] for a in affected]}

    def snapshots_for_participant(self, participant_id):
        rows = self.conn.execute(
            "SELECT snapshot_id FROM snapshot_members"
            " WHERE participant_id=? AND included=1"
            " GROUP BY snapshot_id ORDER BY snapshot_id",
            (participant_id,),
        ).fetchall()
        return [{"snapshot_id": r["snapshot_id"]} for r in rows]

    # ------------------------------------------------------------------
    # 冻结快照
    # ------------------------------------------------------------------
    def latest_batch_id(self):
        row = self.conn.execute(
            "SELECT batch_id FROM upload_batches WHERE status IN (?,?)"
            " ORDER BY received_at DESC, rowid DESC LIMIT 1",
            ACCEPTED_BATCH_STATES,
        ).fetchone()
        return row["batch_id"] if row else None

    def freeze_snapshot(self, snapshot_id, manuscript_ref, analysis_code_version,
                        selection_criteria=None, cutoff_batch_id=None,
                        created_by=""):
        if self.conn.execute(
            "SELECT 1 FROM frozen_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone():
            raise DomainError("SNAPSHOT_EXISTS",
                             f"快照 {snapshot_id} 已存在且不可变", 409)
        cutoff = cutoff_batch_id or self.latest_batch_id()
        if not cutoff:
            raise DomainError("NO_DATA", "尚无已接收批次，无法冻结快照", 409)
        cutoff_row = self.conn.execute(
            "SELECT 1 FROM upload_batches WHERE batch_id=? AND status IN (?,?)",
            (cutoff, *ACCEPTED_BATCH_STATES),
        ).fetchone()
        if not cutoff_row:
            raise DomainError("CUTOFF_BATCH_INVALID",
                             f"截止批次 {cutoff} 不存在或未被接收", 409)

        # 数据可见集：截止批次及之前（按入库行序）处于接收状态的批次。
        # 显式以旧批次为截止冻结时，之后迟到的数据不得进入本快照。
        cutoff_rowid = self.conn.execute(
            "SELECT rowid FROM upload_batches WHERE batch_id=?", (cutoff,)
        ).fetchone()[0]
        visible = [r["batch_id"] for r in self.conn.execute(
            "SELECT batch_id FROM upload_batches"
            " WHERE status IN (?,?) AND rowid<=?"
            " ORDER BY rowid",
            (*ACCEPTED_BATCH_STATES, cutoff_rowid)).fetchall()]
        placeholders = ",".join("?" for _ in visible)

        as_of = _now()
        visits = self.conn.execute(
            f"SELECT v.*, p.cohort_group FROM life_stage_visits v"
            f" JOIN participants p ON p.participant_id=v.participant_id"
            f" WHERE v.recorded_batch IN ({placeholders})"
            f" AND p.created_batch IN ({placeholders})"
            " ORDER BY v.participant_id, v.visit_date",
            [*visible, *visible],
        ).fetchall()

        members = []
        for v in visits:
            pid, vdate = v["participant_id"], v["visit_date"]
            qc = self.conn.execute(
                f"SELECT decision FROM qc_reviews"
                f" WHERE participant_id=? AND visit_date=?"
                f" AND recorded_batch IN ({placeholders})",
                (pid, vdate, *visible),
            ).fetchone()
            metric_rows = self.conn.execute(
                f"SELECT metric_code,is_missing FROM derived_metrics"
                f" WHERE participant_id=? AND visit_date=?"
                f" AND recorded_batch IN ({placeholders})",
                (pid, vdate, *visible),
            ).fetchall()
            present = [r["metric_code"] for r in metric_rows
                       if not r["is_missing"]]
            missing = [r["metric_code"] for r in metric_rows
                       if r["is_missing"]]

            usable, consent_reason, consent_version = self.consent_for_use(
                pid, vdate, as_of, visible_batches=set(visible))
            if usable:
                included, reason = rules.evaluate_visit_eligibility(
                    consent_active=True,
                    qc_decision=qc["decision"] if qc else None,
                    assessment_method=v["assessment_method"],
                    present_metric_count=len(present),
                )
            else:
                included, reason = False, consent_reason

            members.append({
                "participant_id": pid,
                "visit_date": vdate,
                "included": 1 if included else 0,
                "exclusion_reason": reason,
                "evidence_grade": rules.evidence_grade(
                    v["assessment_method"], v["hormone_assay_done"]),
                "life_stage": v["life_stage"],
                "cohort_group": v["cohort_group"],
                "assessment_method": v["assessment_method"],
                "hormone_assay_done": v["hormone_assay_done"],
                "present_metric_codes": present,
                "missing_metric_codes": missing,
                "consent_version_code": consent_version,
            })

        included_members = rules.annotate_repeated_measurements(
            [m for m in members if m["included"]])
        included_keys = {
            (m["participant_id"], m["visit_date"]) for m in included_members}
        for m in members:
            if (m["participant_id"], m["visit_date"]) not in included_keys:
                m["repeated_measurement"] = 0
                m["repeat_index"] = 0

        summary = self._summarize_members(members)

        with self.conn:
            self.conn.execute(
                "INSERT INTO frozen_snapshots(snapshot_id,manuscript_ref,"
                "analysis_code_version,selection_criteria_json,"
                "data_cutoff_batch,frozen_at,created_by,cohort_summary_json)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (snapshot_id, manuscript_ref, analysis_code_version,
                 rules.canonical_json(selection_criteria or {}), cutoff, as_of,
                 created_by, rules.canonical_json(summary)),
            )
            for m in members:
                self.conn.execute(
                    "INSERT INTO snapshot_members(snapshot_id,participant_id,"
                    "visit_date,included,exclusion_reason,evidence_grade,"
                    "repeated_measurement,repeat_index,present_metric_codes,"
                    "missing_metric_codes,consent_version_code,cohort_group,"
                    "assessment_method,hormone_assay_done,life_stage)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (snapshot_id, m["participant_id"], m["visit_date"],
                     m["included"], m["exclusion_reason"], m["evidence_grade"],
                     m["repeated_measurement"], m["repeat_index"],
                     rules.canonical_json(m["present_metric_codes"]),
                     rules.canonical_json(m["missing_metric_codes"]),
                     m["consent_version_code"], m["cohort_group"],
                     m["assessment_method"], m["hormone_assay_done"],
                     m["life_stage"]),
                )
            self._audit("freeze", "snapshot", snapshot_id,
                        {"cutoff_batch": cutoff,
                         "included_visits": summary["included_visits"],
                         "excluded_visits": summary["excluded_visits"]},
                        created_by)
        return self.get_snapshot(snapshot_id)

    @staticmethod
    def _summarize_members(members):
        included = [m for m in members if m["included"]]
        excluded = [m for m in members if not m["included"]]
        groups = {}
        methods = {}
        hormone_covered = 0
        missing_metrics = {}
        repeated = 0
        participants = set()
        for m in included:
            g = groups.setdefault(m["cohort_group"],
                                  {"participants": set(), "visits": 0})
            g["participants"].add(m["participant_id"])
            g["visits"] += 1
            participants.add(m["participant_id"])
            methods[m["assessment_method"]] = methods.get(
                m["assessment_method"], 0) + 1
            hormone_covered += 1 if m["hormone_assay_done"] else 0
            if m["repeated_measurement"]:
                repeated += 1
            for code in m["missing_metric_codes"]:
                missing_metrics[code] = missing_metrics.get(code, 0) + 1
        exclusion_reasons = {}
        for m in excluded:
            exclusion_reasons[m["exclusion_reason"]] = \
                exclusion_reasons.get(m["exclusion_reason"], 0) + 1
        return {
            "total_participants": len(participants),
            "included_visits": len(included),
            "excluded_visits": len(excluded),
            "repeated_measurements": repeated,
            "groups": {g: {"participants": len(v["participants"]),
                           "visits": v["visits"]}
                       for g, v in sorted(groups.items())},
            "assessment_methods": dict(sorted(methods.items())),
            "hormone_assay_coverage": {
                "covered_visits": hormone_covered,
                "total_visits": len(included),
            },
            "missing_metrics": dict(sorted(missing_metrics.items())),
            "exclusion_reasons": dict(sorted(exclusion_reasons.items())),
        }

    def get_snapshot(self, snapshot_id):
        row = self.conn.execute(
            "SELECT * FROM frozen_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise DomainError("SNAPSHOT_NOT_FOUND",
                              f"快照 {snapshot_id} 不存在", 404)
        return {
            "snapshot_id": row["snapshot_id"],
            "manuscript_ref": row["manuscript_ref"],
            "analysis_code_version": row["analysis_code_version"],
            "selection_criteria": json.loads(row["selection_criteria_json"]),
            "data_cutoff_batch": row["data_cutoff_batch"],
            "frozen_at": row["frozen_at"],
            "created_by": row["created_by"],
            "cohort_summary": json.loads(row["cohort_summary_json"]),
        }

    def _member_rows(self, snapshot_id):
        return self.conn.execute(
            "SELECT * FROM snapshot_members WHERE snapshot_id=?"
            " ORDER BY participant_id,visit_date",
            (snapshot_id,),
        ).fetchall()

    @staticmethod
    def _member_dict(row):
        return {
            "participant_id": row["participant_id"],
            "visit_date": row["visit_date"],
            "included": bool(row["included"]),
            "exclusion_reason": row["exclusion_reason"],
            "evidence_grade": row["evidence_grade"],
            "repeated_measurement": bool(row["repeated_measurement"]),
            "repeat_index": row["repeat_index"],
            "present_metric_codes": json.loads(row["present_metric_codes"]),
            "missing_metric_codes": json.loads(row["missing_metric_codes"]),
            "consent_version_code": row["consent_version_code"],
            "cohort_group": row["cohort_group"],
            "life_stage": row["life_stage"],
            "assessment_method": row["assessment_method"],
            "hormone_assay_done": bool(row["hormone_assay_done"]),
        }

    def rebuild_manuscript_cohort(self, snapshot_id):
        """返回统计人员重建某篇稿件人群所需的完整、自包含信息。"""
        snapshot = self.get_snapshot(snapshot_id)
        members = [self._member_dict(r) for r in self._member_rows(snapshot_id)]
        return {
            "snapshot": snapshot,
            "included": [m for m in members if m["included"]],
            "excluded": [m for m in members if not m["included"]],
            "rebuild_recipe": {
                "code_version": snapshot["analysis_code_version"],
                "data_cutoff_batch": snapshot["data_cutoff_batch"],
                "selection_criteria": snapshot["selection_criteria"],
                "exclusion_reason_legend": {
                    "consent_not_active": "访视时无有效知情同意",
                    "consent_withdrawn": "同意已被撤回，不得用于新分析",
                    "qc_fail": "影像质控不通过",
                    "qc_provisional": "影像质控仅为临时结论",
                    "qc_missing": "缺少质控结论",
                    "stage_unassessed": "人生阶段未评估",
                    "no_derived_metrics": "无任何可用派生指标",
                },
            },
        }

    # ------------------------------------------------------------------
    # 撤回后既有聚合结果的处置
    # ------------------------------------------------------------------
    def register_aggregate_artifact(self, artifact_id, snapshot_id, kind,
                                    description="", actor=""):
        self.get_snapshot(snapshot_id)
        with self.conn:
            self.conn.execute(
                "INSERT INTO aggregate_artifacts(artifact_id,snapshot_id,kind,"
                "description,retention_decision,reason,decided_by,decided_at)"
                " VALUES (?,?,?,?, 'retain', '', '', '')",
                (artifact_id, snapshot_id, kind, description),
            )
            self._audit("artifact_register", "artifact", artifact_id,
                        {"snapshot_id": snapshot_id, "kind": kind}, actor)
        return self.get_aggregate_artifact(artifact_id)

    def decide_aggregate_retention(self, artifact_id, decision, reason,
                                   decided_by):
        if decision not in ("retain", "restrict_access", "remove"):
            raise DomainError("INVALID_RETENTION_DECISION",
                             f"未知处置 {decision}")
        row = self.conn.execute(
            "SELECT 1 FROM aggregate_artifacts WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()
        if not row:
            raise DomainError("ARTIFACT_NOT_FOUND",
                              f"聚合产物 {artifact_id} 不存在", 404)
        with self.conn:
            self.conn.execute(
                "UPDATE aggregate_artifacts SET retention_decision=?,"
                "reason=?,decided_by=?,decided_at=? WHERE artifact_id=?",
                (decision, reason, decided_by, _now(), artifact_id),
            )
            self._audit("artifact_retention", "artifact", artifact_id,
                        {"decision": decision, "reason": reason}, decided_by)
        return self.get_aggregate_artifact(artifact_id)

    def get_aggregate_artifact(self, artifact_id):
        r = self.conn.execute(
            "SELECT * FROM aggregate_artifacts WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()
        if not r:
            raise DomainError("ARTIFACT_NOT_FOUND",
                              f"聚合产物 {artifact_id} 不存在", 404)
        return dict(r)

    # ------------------------------------------------------------------
    # 对外结论发布
    # ------------------------------------------------------------------
    def publish_conclusion(self, conclusion_id, snapshot_id, headline,
                           claim_text, applicability, limitation_codes,
                           channel="", published_by=""):
        self.get_snapshot(snapshot_id)
        if self.conn.execute(
            "SELECT 1 FROM conclusions WHERE conclusion_id=?", (conclusion_id,)
        ).fetchone():
            raise DomainError("CONCLUSION_EXISTS",
                             f"结论 {conclusion_id} 已存在", 409)

        member_rows = [self._member_dict(r)
                       for r in self._member_rows(snapshot_id)
                       if r["included"]]
        included_groups = sorted({m["cohort_group"] for m in member_rows})
        verdict = rules.evaluate_conclusion_publication(
            applicability=applicability,
            limitation_codes=limitation_codes,
            snapshot_id=snapshot_id,
            included_groups=included_groups,
            snapshot_members=member_rows,
        )
        if not verdict["allowed"]:
            raise DomainError(
                "CONCLUSION_GATE_FAILED",
                "结论未通过适用范围/限制项门控，禁止发布",
                status=422,
                details=verdict["violations"],
            )

        limitations = [
            {"code": code, "text": rules.LIMITATION_CATALOG[code]}
            for code in sorted(limitation_codes)
        ]
        with self.conn:
            self.conn.execute(
                "INSERT INTO conclusions(conclusion_id,snapshot_id,headline,"
                "claim_text,applicability_json,limitations_json,channel,"
                "published_at,published_by) VALUES (?,?,?,?,?,?,?,?,?)",
                (conclusion_id, snapshot_id, headline, claim_text,
                 rules.canonical_json(applicability),
                 rules.canonical_json(limitations), channel,
                 _now(), published_by),
            )
            self._audit("conclusion_publish", "conclusion", conclusion_id,
                        {"snapshot_id": snapshot_id, "channel": channel,
                         "limitation_codes": sorted(limitation_codes)},
                        published_by)
        return self.get_conclusion(conclusion_id)

    def get_conclusion(self, conclusion_id):
        r = self.conn.execute(
            "SELECT * FROM conclusions WHERE conclusion_id=?", (conclusion_id,)
        ).fetchone()
        if not r:
            raise DomainError("CONCLUSION_NOT_FOUND",
                              f"结论 {conclusion_id} 不存在", 404)
        return {
            "conclusion_id": r["conclusion_id"],
            "snapshot_id": r["snapshot_id"],
            "headline": r["headline"],
            "claim_text": r["claim_text"],
            "applicability": json.loads(r["applicability_json"]),
            "limitations": json.loads(r["limitations_json"]),
            "channel": r["channel"],
            "status": r["status"],
            "published_at": r["published_at"],
            "published_by": r["published_by"],
        }

    # ------------------------------------------------------------------
    # 导出申请与隐私评估
    # ------------------------------------------------------------------
    def request_export(self, export_id, requested_by, purpose, granularity,
                       columns, snapshot_id=None, filters=None, k_min=None,
                       salt=None):
        filters = filters or {}
        k_min = int(k_min or rules.DEFAULT_K)
        if self.conn.execute(
            "SELECT 1 FROM exports WHERE export_id=?", (export_id,)
        ).fetchone():
            raise DomainError("EXPORT_EXISTS", f"导出 {export_id} 已存在", 409)

        # 参与者级导出必须基于冻结快照；聚合级可不绑定快照时无数据可取，故同样要求
        if not snapshot_id:
            raise DomainError("SNAPSHOT_REQUIRED",
                             "导出必须基于某个冻结快照")
        self.get_snapshot(snapshot_id)

        rows = [self._member_dict(r)
                for r in self._member_rows(snapshot_id) if r["included"]]
        groups_filter = set(filters.get("cohort_groups", []))
        if groups_filter:
            rows = [r for r in rows if r["cohort_group"] in groups_filter]
        for r in rows:
            r["visit_year"] = r["visit_date"][:4]
            r["visit_month"] = r["visit_date"][:7]

        unsupported = sorted({c for c in columns
                              if c.lower() not in
                              {f.lower() for f in EXPORTABLE_MEMBER_FIELDS}
                              | rules.DIRECT_IDENTIFIER_COLUMNS
                              | rules.IMAGE_COLUMNS
                              | rules.PRECISE_TIME_COLUMNS
                              | rules.QUASI_IDENTIFIER_COLUMNS})
        reasons_pre = [f"UNSUPPORTED_COLUMN:{c}" for c in unsupported]
        lower_cols = {c.lower() for c in columns}
        if granularity == "participant_level" \
                and lower_cols & rules.PRECISE_TIME_COLUMNS:
            reasons_pre.append(
                "PRECISE_TIME_AT_PARTICIPANT_LEVEL:"
                + ",".join(sorted(lower_cols & rules.PRECISE_TIME_COLUMNS))
                + "（参与者级只能使用 visit_month/visit_year 粗粒度时间）")

        if granularity == "participant_level":
            # 评估时只看申请列，行内保留真实编号仅供随后生成假名
            eval_rows = [{c: row.get(c) for c in columns} for row in rows]
            verdict = rules.evaluate_export(
                columns=columns, rows=eval_rows, k_min=k_min)
        elif granularity == "aggregate":
            dims = [c for c in columns
                    if c.lower() in EXPORTABLE_MEMBER_FIELDS
                    and c.lower() != "pseudonym"]
            counts = {}
            for row in rows:
                key = tuple(row.get(d) for d in dims) if dims else ("all",)
                counts[key] = counts.get(key, 0) + 1
            verdict = rules.evaluate_export(
                columns=columns, rows=[], k_min=k_min, group_counts=counts)
        else:
            raise DomainError("INVALID_GRANULARITY",
                             f"未知导出粒度 {granularity}")

        all_reasons = reasons_pre + verdict["reasons"]
        hard_reject = bool(reasons_pre) or verdict["status"] == "rejected"
        status = "rejected" if hard_reject else verdict["status"]

        pseudonym_salt = ""
        released_rows = []
        if status != "rejected" and granularity == "participant_level":
            pseudonym_salt = salt or secrets.token_hex(16)
            qi_cols = [c for c in columns
                       if c.lower() in rules.QUASI_IDENTIFIER_COLUMNS]
            small_keys = {tuple(cell["quasi_key"])
                          for cell in verdict["suppressed_cells"]}
            safe_cols = [c for c in columns
                         if c.lower() not in
                         (rules.DIRECT_IDENTIFIER_COLUMNS
                          | rules.IMAGE_COLUMNS
                          | rules.PRECISE_TIME_COLUMNS)]
            want_pseudonym = "pseudonym" in {c.lower() for c in columns}
            for row in rows:
                if tuple(row.get(c) for c in qi_cols) in small_keys:
                    continue
                out = {c: row.get(c) for c in safe_cols}
                if want_pseudonym:
                    out["pseudonym"] = rules.pseudonym(
                        row["participant_id"], pseudonym_salt)
                released_rows.append(out)
        elif status != "rejected":
            released_rows = verdict["released_rows"]

        with self.conn:
            self.conn.execute(
                "INSERT INTO exports(export_id,snapshot_id,requested_by,purpose,"
                "granularity,columns_json,filters_json,k_min,status,"
                "decision_reasons_json,suppressed_cells_json,pseudonym_salt,"
                "row_count,requested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (export_id, snapshot_id, requested_by, purpose, granularity,
                 rules.canonical_json(columns), rules.canonical_json(filters),
                 k_min, status, rules.canonical_json(all_reasons),
                 rules.canonical_json(verdict["suppressed_cells"]),
                 pseudonym_salt if status != "rejected" else "",
                 len(released_rows), _now()),
            )
            self._audit("export_request", "export", export_id,
                        {"snapshot_id": snapshot_id, "status": status,
                         "reasons": all_reasons}, requested_by)

        return {"export_id": export_id, "snapshot_id": snapshot_id,
                "status": status, "reasons": all_reasons,
                "suppressed_cells": verdict["suppressed_cells"],
                "granularity": granularity, "k_min": k_min,
                "row_count": len(released_rows), "rows": released_rows}

    def get_export(self, export_id):
        r = self.conn.execute(
            "SELECT * FROM exports WHERE export_id=?", (export_id,)
        ).fetchone()
        if not r:
            raise DomainError("EXPORT_NOT_FOUND",
                              f"导出 {export_id} 不存在", 404)
        # 盐值不随查询返回：它仅用于服务端复算假名，本身接近密钥
        return {
            "export_id": r["export_id"],
            "snapshot_id": r["snapshot_id"],
            "requested_by": r["requested_by"],
            "purpose": r["purpose"],
            "granularity": r["granularity"],
            "columns": json.loads(r["columns_json"]),
            "filters": json.loads(r["filters_json"]),
            "k_min": r["k_min"],
            "status": r["status"],
            "decision_reasons": json.loads(r["decision_reasons_json"]),
            "suppressed_cells": json.loads(r["suppressed_cells_json"]),
            "row_count": r["row_count"],
            "requested_at": r["requested_at"],
        }
