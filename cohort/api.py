"""领域 HTTP 路由。

路由以 :class:`CohortRouter` 形式提供，可挂到任意 ``BaseHTTPRequestHandler``
上；与 :mod:`service` 解耦，便于在测试中直接驱动。
"""

import threading
from urllib.parse import urlsplit

from .store import CohortStore, DomainError


class CohortRouter:
    def __init__(self, store=None, db_path=":memory:"):
        self.store = store or CohortStore(db_path)
        # 单 SQLite 连接被工作线程共享：领域请求串行化，避免事务交错
        self._lock = threading.RLock()

    def close(self):
        self.store.close()

    def dispatch(self, method, path, body, headers):
        """返回 (status, response_dict)。找不到路由返回 None。"""
        with self._lock:
            return self._dispatch(method, path, body, headers)

    def _dispatch(self, method, path, body, headers):
        parsed = urlsplit(path)
        parts = [p for p in parsed.path.split("/") if p]
        try:
            if method == "GET" and parts == ["health"]:
                return None  # 由 service.Handler 处理，保持原契约
            if method == "POST" and parts == ["consent-versions"]:
                return 200, self.store.register_consent_version(
                    body["version_code"], body["title"], body["released_at"],
                    body["scope_text"], body.get("terms")) or {"status": "registered"}
            if method == "POST" and len(parts) == 2 and parts[0] == "batches":
                return 200, self.store.ingest_batch(
                    parts[1], body.get("source", "api"), body["payload"],
                    actor=headers.get("x-actor", ""))
            if method == "GET" and parts == ["batches"]:
                return 200, {"batches": self.store.list_batches()}
            if method == "GET" and len(parts) == 2 and parts[0] == "batches":
                return 200, self.store.get_batch(parts[1])
            if method == "POST" and len(parts) == 3 \
                    and parts[0] == "participants" and parts[2] == "withdraw":
                return 200, self.store.withdraw_participant(
                    parts[1], body["effective_at"],
                    body.get("version_code"),
                    actor=headers.get("x-actor", ""), note=body.get("note", ""))
            if method == "POST" and parts == ["snapshots"]:
                return 200, self.store.freeze_snapshot(
                    body["snapshot_id"], body["manuscript_ref"],
                    body["analysis_code_version"],
                    body.get("selection_criteria"),
                    body.get("cutoff_batch_id"),
                    body.get("created_by", ""))
            if method == "GET" and len(parts) == 2 and parts[0] == "snapshots":
                return 200, self.store.get_snapshot(parts[1])
            if method == "GET" and len(parts) == 3 \
                    and parts[0] == "snapshots" and parts[2] == "rebuild":
                return 200, self.store.rebuild_manuscript_cohort(parts[1])
            if method == "POST" and len(parts) == 3 \
                    and parts[0] == "snapshots" and parts[2] == "artifacts":
                return 200, self.store.register_aggregate_artifact(
                    body["artifact_id"], parts[1], body["kind"],
                    body.get("description", ""),
                    actor=headers.get("x-actor", ""))
            if method == "GET" and len(parts) == 2 and parts[0] == "artifacts":
                return 200, self.store.get_aggregate_artifact(parts[1])
            if method == "POST" and len(parts) == 3 \
                    and parts[0] == "artifacts" and parts[2] == "retention":
                return 200, self.store.decide_aggregate_retention(
                    parts[1], body["decision"], body["reason"],
                    body.get("decided_by", headers.get("x-actor", "")))
            if method == "POST" and parts == ["conclusions"]:
                return 200, self.store.publish_conclusion(
                    body["conclusion_id"], body["snapshot_id"],
                    body["headline"], body["claim_text"],
                    body["applicability"], body["limitation_codes"],
                    body.get("channel", ""), body.get("published_by", ""))
            if method == "GET" and len(parts) == 2 and parts[0] == "conclusions":
                return 200, self.store.get_conclusion(parts[1])
            if method == "POST" and parts == ["exports"]:
                return 200, self.store.request_export(
                    body["export_id"], body.get("requested_by",
                                                headers.get("x-actor", "")),
                    body["purpose"], body["granularity"], body["columns"],
                    snapshot_id=body.get("snapshot_id"),
                    filters=body.get("filters"), k_min=body.get("k_min"))
            if method == "GET" and len(parts) == 2 and parts[0] == "exports":
                return 200, self.store.get_export(parts[1])
            return None
        except DomainError as exc:
            return exc.status, {"error": exc.code, "message": exc.message,
                                "details": exc.details}
        except KeyError as exc:
            return 400, {"error": "MISSING_FIELD",
                         "message": f"缺少必填字段: {exc.args[0]}"}
        except (TypeError, ValueError) as exc:
            return 400, {"error": "BAD_REQUEST", "message": str(exc)}
