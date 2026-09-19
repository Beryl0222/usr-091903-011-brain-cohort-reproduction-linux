"""HTTP 路由：把 JSON 请求映射到领域函数。"""

import re

from . import claims, domain, exports, snapshots
from .store import Store

ROUTES = [
    ("POST", r"^/participants$", domain.create_participant),
    ("GET", r"^/participants$", domain.list_participants),
    ("GET", r"^/participants/(?P<pid>[^/]+)$", domain.get_participant),
    ("POST", r"^/participants/(?P<pid>[^/]+)/consents$", domain.add_consent),
    ("POST", r"^/participants/(?P<pid>[^/]+)/withdrawals$", domain.add_withdrawal),
    ("POST", r"^/participants/(?P<pid>[^/]+)/stage-events$", domain.add_stage_event),
    ("POST", r"^/participants/(?P<pid>[^/]+)/visits$", domain.create_visit),
    ("POST", r"^/participants/(?P<pid>[^/]+)/exclusions$", domain.add_exclusion),
    ("GET", r"^/visits/(?P<vid>[^/]+)$", domain.get_visit),
    ("POST", r"^/visits/(?P<vid>[^/]+)/imaging$", domain.upsert_imaging),
    ("POST", r"^/visits/(?P<vid>[^/]+)/qc$", domain.upsert_qc),
    ("POST", r"^/visits/(?P<vid>[^/]+)/derived-metrics$", domain.upsert_metrics),
    ("POST", r"^/control-matches$", domain.add_control_match),
    ("POST", r"^/snapshots$", snapshots.freeze_snapshot),
    ("GET", r"^/snapshots$", snapshots.list_snapshots),
    ("GET", r"^/snapshots/(?P<sid>[^/]+)$", snapshots.get_snapshot),
    ("GET", r"^/snapshots/(?P<sid>[^/]+)/manifest$", snapshots.get_manifest),
    ("POST", r"^/snapshots/(?P<sid>[^/]+)/verify$", snapshots.verify_snapshot),
    ("GET", r"^/snapshots/(?P<sid>[^/]+)/imaging-summary$", snapshots.imaging_summary),
    ("POST", r"^/claims$", claims.register_claim),
    ("GET", r"^/claims/(?P<cid>[^/]+)$", claims.get_claim),
    ("POST", r"^/exports$", exports.create_export),
    ("GET", r"^/exports/(?P<eid>[^/]+)$", exports.get_export),
    ("GET", r"^/audit$", domain.list_audit),
]

_COMPILED = [(method, re.compile(pattern), fn) for method, pattern, fn in ROUTES]


def path_known(path):
    """路径是否匹配任何已知路由（用于在触碰数据存储前快速 404）。"""
    return any(rx.match(path) for _, rx, _ in _COMPILED)


class App:
    def __init__(self, db_path=":memory:"):
        self.store = Store(db_path)

    def handle(self, method, path, body, query):
        for route_method, rx, fn in _COMPILED:
            if route_method != method:
                continue
            match = rx.match(path)
            if match:
                return fn(self.store, body or {}, query or {}, **match.groupdict())
        return 404, {"error": {"code": "not_found", "message": "未知路由"}}
