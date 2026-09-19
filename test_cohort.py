"""队列后端领域行为测试：摄入、冻结、撤回、结论与导出策略。"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from cohort.api import App
from service import make_handler


class ApiClient:
    def __init__(self):
        self.app = App(":memory:")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _request(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            payload = json.loads(error.read().decode("utf-8"))
            error.close()
            return error.code, payload

    def post(self, path, body=None):
        return self._request("POST", path, body if body is not None else {})

    def get(self, path):
        return self._request("GET", path)


class CohortTestCase(unittest.TestCase):
    def setUp(self):
        self.client = ApiClient()
        self.store = self.client.app.store

    def tearDown(self):
        self.client.close()

    # ------------------------------------------------------------ 构造工具

    def add_participant(self, pid, group="puberty", method="menstrual_record",
                        birth_year=2012, visit_date="2026-03-10", qc_conclusion="pass",
                        hormone=False, scope=("all",), metrics=True, visits=1):
        status, _ = self.client.post("/participants", {
            "participant_id": pid, "cohort_group": group, "birth_year": birth_year})
        self.assertEqual(status, 201)
        status, _ = self.client.post(f"/participants/{pid}/consents", {
            "version": "v1", "scope": list(scope), "signed_at": "2026-01-05"})
        self.assertEqual(status, 201)
        status, _ = self.client.post(f"/participants/{pid}/stage-events", {
            "stage": group, "determination_method": method, "event_date": "2026-02-01"})
        self.assertEqual(status, 201)
        if hormone:
            self.client.post(f"/participants/{pid}/stage-events", {
                "stage": group, "determination_method": "hormone_assay", "event_date": "2026-02-02"})
        vids = []
        for index in range(1, visits + 1):
            status, visit = self.client.post(f"/participants/{pid}/visits", {
                "visit_index": index, "visit_date": visit_date})
            self.assertEqual(status, 201)
            vid = visit["visit_id"]
            self.client.post(f"/visits/{vid}/imaging", {
                "params": {"scanner_model": "Prisma3T", "tr_ms": 2300, "voxel_mm": 1.0},
                "source": "datacenter-1"})
            qc_body = {"conclusion": qc_conclusion}
            if qc_conclusion == "fail":
                qc_body["reasons"] = ["motion"]
            if qc_conclusion == "missing":
                qc_body["missing_reason"] = "scan_failed"
            self.client.post(f"/visits/{vid}/qc", qc_body)
            if metrics:
                self.client.post(f"/visits/{vid}/derived-metrics", {
                    "metrics": {"gmv": 0.62, "ct": 2.71}, "pipeline_version": "gm@1.2.0"})
            vids.append(vid)
        return vids

    def freeze(self, groups=("puberty",), purpose="analysis_gm",
               code_version="analysis@abc123", **spec_overrides):
        spec = {"groups": list(groups)}
        spec.update(spec_overrides)
        status, snap = self.client.post("/snapshots", {
            "name": "稿件1冻结", "purpose": purpose, "code_version": code_version,
            "created_by": "statistician", "cohort_spec": spec})
        self.assertEqual(status, 201, snap)
        return snap

    def flag_names(self, snapshot):
        return {item["flag"] for item in snapshot["limitation_flags"]}


# ---------------------------------------------------------------- 摄入与版本化

class IngestionTest(CohortTestCase):
    def test_duplicate_upload_is_deduplicated(self):
        (vid,) = self.add_participant("p1")
        # add_participant 已上传过同一内容，重复上传应幂等去重
        payload = {"params": {"scanner_model": "Prisma3T", "tr_ms": 2300, "voxel_mm": 1.0}}
        status, dup = self.client.post(f"/visits/{vid}/imaging", payload)
        self.assertEqual(status, 200)
        self.assertTrue(dup["deduplicated"])
        self.assertEqual(dup["version"], 1)

        status, visit = self.client.get(f"/visits/{vid}")
        self.assertEqual(visit["imaging"]["version"], 1)

    def test_changed_upload_creates_new_version(self):
        (vid,) = self.add_participant("p1")
        status, visit = self.client.get(f"/visits/{vid}")
        first = visit["imaging"]
        self.assertEqual(first["version"], 1)

        status, second = self.client.post(f"/visits/{vid}/imaging", {
            "params": {"scanner_model": "Prisma3T", "tr_ms": 2500, "voxel_mm": 1.0}})
        self.assertEqual(status, 201)
        self.assertEqual(second["version"], 2)

        old = self.store.query_one(
            "SELECT * FROM imaging_records WHERE record_id=?", (first["record_id"],))
        self.assertEqual(old["superseded_by"], second["record_id"])

        status, visit = self.client.get(f"/visits/{vid}")
        self.assertEqual(visit["imaging"]["version"], 2)
        self.assertEqual(visit["imaging"]["params"]["tr_ms"], 2500)

    def test_qc_missing_requires_explicit_reason(self):
        (vid,) = self.add_participant("p1")
        status, err = self.client.post(f"/visits/{vid}/qc", {"conclusion": "missing"})
        self.assertEqual(status, 422)
        self.assertEqual(err["error"]["code"], "missing_field")
        status, err = self.client.post(f"/visits/{vid}/qc", {"conclusion": "fail"})
        self.assertEqual(status, 422)

    def test_metrics_require_numbers_and_pipeline_version(self):
        (vid,) = self.add_participant("p1")
        status, err = self.client.post(f"/visits/{vid}/derived-metrics", {
            "metrics": {"gmv": "high"}, "pipeline_version": "gm@1.2.0"})
        self.assertEqual(status, 422)
        status, err = self.client.post(f"/visits/{vid}/derived-metrics", {
            "metrics": {"gmv": 0.6}})
        self.assertEqual(status, 422)
        # 显式缺失原因合法
        status, rec = self.client.post(f"/visits/{vid}/derived-metrics", {
            "metrics": {"gmv": 0.6}, "missing": {"ct": "scan_failed"},
            "pipeline_version": "gm@1.2.0"})
        self.assertEqual(status, 201)
        self.assertEqual(rec["missing"], {"ct": "scan_failed"})

    def test_consent_version_is_immutable(self):
        self.add_participant("p1")
        status, dup = self.client.post("/participants/p1/consents", {
            "version": "v1", "scope": ["all"], "signed_at": "2026-01-05"})
        self.assertEqual(status, 200)
        self.assertTrue(dup["deduplicated"])
        status, err = self.client.post("/participants/p1/consents", {
            "version": "v1", "scope": ["analysis_gm"], "signed_at": "2026-01-05"})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "consent_version_conflict")

    def test_participant_conflict_on_different_payload(self):
        self.add_participant("p1")
        status, err = self.client.post("/participants", {
            "participant_id": "p1", "cohort_group": "menopause", "birth_year": 1968})
        self.assertEqual(status, 409)

    def test_control_match_recorded(self):
        self.add_participant("case1")
        self.add_participant("ctrl1", group="control", method="not_applicable", birth_year=2011)
        status, match = self.client.post("/control-matches", {
            "match_set_id": "ms1", "participant_id": "case1", "control_id": "ctrl1",
            "matched_on": ["age_band", "scanner_model"]})
        self.assertEqual(status, 201)
        status, dup = self.client.post("/control-matches", {
            "match_set_id": "ms1", "participant_id": "case1", "control_id": "ctrl1",
            "matched_on": ["age_band", "scanner_model"]})
        self.assertEqual(status, 200)


# ---------------------------------------------------------------- 快照冻结

class SnapshotTest(CohortTestCase):
    def test_frozen_snapshot_immune_to_late_and_duplicate_uploads(self):
        vids = {f"p{i}": self.add_participant(f"p{i}") for i in range(5)}
        snap = self.freeze()
        sid = snap["snapshot_id"]
        status, manifest_before = self.client.get(f"/snapshots/{sid}/manifest")

        # 迟到上传：同一访视的新版影像参数
        late_vid = vids["p0"][0]
        status, late = self.client.post(f"/visits/{late_vid}/imaging", {
            "params": {"scanner_model": "Prisma3T", "tr_ms": 2500, "voxel_mm": 1.0}})
        self.assertEqual(status, 201)
        self.assertEqual(late["version"], 2)
        # 重复上传：旧内容再来一次，幂等去重
        status, dup = self.client.post(f"/visits/{late_vid}/imaging", {
            "params": {"scanner_model": "Prisma3T", "tr_ms": 2300, "voxel_mm": 1.0}})
        self.assertEqual(status, 200)
        self.assertTrue(dup["deduplicated"])

        status, manifest_after = self.client.get(f"/snapshots/{sid}/manifest")
        self.assertEqual(manifest_before, manifest_after)
        status, snap_after = self.client.get(f"/snapshots/{sid}")
        self.assertEqual(snap_after["content_hash"], snap["content_hash"])

    def test_verify_valid_and_reports_superseded(self):
        vids = {f"p{i}": self.add_participant(f"p{i}") for i in range(5)}
        snap = self.freeze()
        sid = snap["snapshot_id"]
        self.client.post(f"/visits/{vids['p2'][0]}/imaging", {
            "params": {"scanner_model": "Prisma3T", "tr_ms": 2500, "voxel_mm": 1.0}})

        status, result = self.client.post(f"/snapshots/{sid}/verify")
        self.assertEqual(status, 200)
        self.assertTrue(result["valid"])
        self.assertTrue(result["content_hash_match"])
        self.assertEqual(result["records_missing"], 0)
        self.assertEqual(result["superseded_since_freeze"], 1)

    def test_manifest_pins_population_code_and_consent(self):
        self.add_participant("p0")
        for i in range(1, 5):
            self.add_participant(f"p{i}")
        snap = self.freeze(code_version="analysis@deadbeef")
        status, manifest = self.client.get(f"/snapshots/{snap['snapshot_id']}/manifest")
        self.assertEqual(manifest["snapshot"]["code_version"], "analysis@deadbeef")
        self.assertEqual(len(manifest["members"]), 5)
        member = manifest["members"][0]
        self.assertEqual(member["consent_version"], "v1")
        self.assertEqual(member["visits"][0]["imaging"]["version"], 1)
        self.assertEqual(member["visits"][0]["metrics"]["pipeline_version"], "gm@1.2.0")

    def test_consent_scope_gates_membership(self):
        self.add_participant("covered", scope=("all",))
        self.add_participant("narrow", scope=("other_purpose",))
        snap = self.freeze()
        self.assertEqual(snap["composition"]["total_participants"], 1)
        self.assertEqual(snap["composition"]["excluded"]["no_consent"], 1)

    def test_qc_fail_visit_excluded_with_reason(self):
        self.add_participant("ok1")
        self.add_participant("bad1", qc_conclusion="fail")
        snap = self.freeze()
        self.assertEqual(snap["composition"]["total_participants"], 1)
        self.assertEqual(snap["composition"]["excluded"]["qc_not_passed"], 1)
        self.assertIn("qc_exclusions_present", self.flag_names(snap))
        status, manifest = self.client.get(f"/snapshots/{snap['snapshot_id']}/manifest")
        qc_excluded = [d for d in manifest["excluded_detail"] if d["cause"] == "qc_not_passed"]
        self.assertEqual(qc_excluded[0]["reasons"], ["motion"])

    def test_repeated_measures_flagged(self):
        self.add_participant("p0", visits=2)
        for i in range(1, 5):
            self.add_participant(f"p{i}")
        snap = self.freeze()
        self.assertEqual(snap["composition"]["total_visits"], 6)
        self.assertIn("repeated_measures", self.flag_names(snap))

    def test_self_report_and_hormone_flags(self):
        self.add_participant("p0", method="self_report")
        for i in range(1, 5):
            self.add_participant(f"p{i}", hormone=True)
        snap = self.freeze()
        flags = self.flag_names(snap)
        self.assertIn("self_report_staging", flags)
        self.assertIn("incomplete_hormone_data", flags)  # p0 无激素检测
        self.assertIn("heterogeneous_staging_methods", flags)
        self.assertIn("small_group", flags)

    def test_imaging_summary_is_aggregate_only(self):
        for i in range(3):
            self.add_participant(f"p{i}")
        snap = self.freeze()
        status, summary = self.client.get(f"/snapshots/{snap['snapshot_id']}/imaging-summary")
        self.assertEqual(status, 200)
        self.assertEqual(summary["scanner_models"], {"Prisma3T": 3})
        self.assertEqual(summary["parameters"]["tr_ms"]["min"], 2300)
        blob = json.dumps(summary)
        self.assertNotIn("visit_date", blob)
        self.assertNotIn("participant", blob)


# ---------------------------------------------------------------- 撤回

class WithdrawalTest(CohortTestCase):
    def test_withdrawal_stops_new_uses_and_is_recorded(self):
        for i in range(6):
            self.add_participant(f"p{i}")
        snap = self.freeze()
        self.assertEqual(snap["composition"]["total_participants"], 6)

        status, withdrawal = self.client.post("/participants/p0/withdrawals", {
            "withdrawn_at": "2026-06-01",
            "aggregate_handling": "exclude_from_future_aggregates",
            "note": "本人要求撤回"})
        self.assertEqual(status, 201)
        self.assertEqual(withdrawal["aggregate_handling"], "exclude_from_future_aggregates")

        # 新快照不再纳入撤回者
        snap2 = self.freeze()
        self.assertEqual(snap2["composition"]["total_participants"], 5)
        self.assertEqual(snap2["composition"]["excluded"]["withdrawn"], 1)

        # 旧快照的清单保持原样（历史记录），但标记出冻结后撤回
        status, snap_view = self.client.get(f"/snapshots/{snap['snapshot_id']}")
        self.assertEqual(snap_view["composition"]["total_participants"], 6)
        self.assertEqual(snap_view["withdrawn_since_freeze"], 1)

        # 新导出属于新使用：撤回者的行被剔除
        status, export = self.client.post("/exports", {
            "snapshot_id": snap["snapshot_id"], "requester": "stat-1", "purpose": "analysis_gm"})
        self.assertEqual(status, 201)
        self.assertEqual(export["dropped_withdrawn"], 1)
        self.assertEqual(export["row_count"], 5)

        # 参与者视图与审计留痕
        status, participant = self.client.get("/participants/p0")
        self.assertEqual(participant["status"], "withdrawn")
        status, audit = self.client.get("/audit?entity=withdrawal")
        self.assertTrue(any(e["action"] == "withdrawal_recorded" for e in audit["entries"]))

    def test_withdrawal_is_one_time(self):
        self.add_participant("p0")
        self.client.post("/participants/p0/withdrawals", {
            "withdrawn_at": "2026-06-01", "aggregate_handling": "retain_existing_aggregates"})
        status, err = self.client.post("/participants/p0/withdrawals", {
            "withdrawn_at": "2026-06-02", "aggregate_handling": "remove_where_feasible"})
        self.assertEqual(status, 409)


# ---------------------------------------------------------------- 结论发布

class ClaimTest(CohortTestCase):
    def _freeze_with_flags(self):
        for i in range(5):
            self.add_participant(f"p{i}", method="self_report")
        return self.freeze()

    def test_claim_must_acknowledge_limitation_flags(self):
        snap = self._freeze_with_flags()
        status, err = self.client.post("/claims", {
            "snapshot_id": snap["snapshot_id"],
            "statement": "未见加速萎缩",
            "scope": {"population": "青春期女性", "applies_to": "横断面灰质体积组间比较"},
            "limitations": ["样本量较小"],
            "created_by": "comms-1"})
        self.assertEqual(status, 422)
        self.assertEqual(err["error"]["code"], "unacknowledged_limitations")
        missing = {item["flag"] for item in err["error"]["details"]["missing"]}
        self.assertIn("self_report_staging", missing)
        self.assertIn("incomplete_hormone_data", missing)

    def test_claim_requires_scope_and_limitations(self):
        snap = self._freeze_with_flags()
        flags = [item["flag"] for item in snap["limitation_flags"]]
        status, err = self.client.post("/claims", {
            "snapshot_id": snap["snapshot_id"], "statement": "未见加速萎缩",
            "scope": {"population": "青春期女性"},
            "limitations": ["局限"], "acknowledged_flags": flags, "created_by": "comms-1"})
        self.assertEqual(status, 422)
        status, err = self.client.post("/claims", {
            "snapshot_id": snap["snapshot_id"], "statement": "未见加速萎缩",
            "scope": {"population": "青春期女性", "applies_to": "横断面比较"},
            "limitations": [], "acknowledged_flags": flags, "created_by": "comms-1"})
        self.assertEqual(status, 422)

    def test_claim_carries_evidence_profile(self):
        snap = self._freeze_with_flags()
        flags = [item["flag"] for item in snap["limitation_flags"]]
        status, claim = self.client.post("/claims", {
            "snapshot_id": snap["snapshot_id"],
            "statement": "未见加速萎缩",
            "scope": {"population": "青春期女性", "applies_to": "横断面灰质体积组间比较"},
            "limitations": ["阶段判定含自报", "激素检测未全覆盖", "样本量较小"],
            "acknowledged_flags": flags,
            "created_by": "comms-1"})
        self.assertEqual(status, 201, claim)

        status, fetched = self.client.get(f"/claims/{claim['claim_id']}")
        self.assertEqual(fetched["statement"], "未见加速萎缩")
        self.assertEqual(fetched["evidence"]["code_version"], "analysis@abc123")
        self.assertEqual(fetched["evidence"]["composition"]["total_participants"], 5)
        self.assertEqual(fetched["evidence"]["content_hash"], snap["content_hash"])


# ---------------------------------------------------------------- 导出脱敏

class ExportTest(CohortTestCase):
    def _freeze_five(self):
        for i in range(5):
            self.add_participant(f"p{i}")
        return self.freeze()

    def test_forbidden_fields_rejected(self):
        snap = self._freeze_five()
        status, err = self.client.post("/exports", {
            "snapshot_id": snap["snapshot_id"], "requester": "r1", "purpose": "analysis_gm",
            "fields": ["group", "visit_date"]})
        self.assertEqual(status, 422)
        self.assertEqual(err["error"]["code"], "forbidden_fields")
        self.assertIn("visit_date", err["error"]["details"]["fields"])

    def test_imaging_plus_time_combination_rejected(self):
        snap = self._freeze_five()
        status, err = self.client.post("/exports", {
            "snapshot_id": snap["snapshot_id"], "requester": "r1", "purpose": "analysis_gm",
            "fields": ["imaging:tr_ms", "visit_quarter"]})
        self.assertEqual(status, 422)
        self.assertEqual(err["error"]["code"], "reidentifiable_combination")

        status, err = self.client.post("/exports", {
            "snapshot_id": snap["snapshot_id"], "requester": "r1", "purpose": "analysis_gm",
            "fields": ["imaging:tr_ms"]})
        self.assertEqual(status, 422)
        self.assertEqual(err["error"]["code"], "imaging_not_exportable")

    def test_k_anonymity_enforced(self):
        for i in range(4):
            self.add_participant(f"p{i}")
        snap = self.freeze()
        status, err = self.client.post("/exports", {
            "snapshot_id": snap["snapshot_id"], "requester": "r1", "purpose": "analysis_gm"})
        self.assertEqual(status, 422)
        self.assertEqual(err["error"]["code"], "k_anonymity_violation")
        self.assertEqual(err["error"]["details"]["violations"][0]["count"], 4)

        self.add_participant("p4")
        snap2 = self.freeze()
        status, export = self.client.post("/exports", {
            "snapshot_id": snap2["snapshot_id"], "requester": "r1", "purpose": "analysis_gm"})
        self.assertEqual(status, 201)
        self.assertEqual(export["row_count"], 5)

    def test_export_rows_are_deidentified(self):
        snap = self._freeze_five()
        status, export = self.client.post("/exports", {
            "snapshot_id": snap["snapshot_id"], "requester": "r1", "purpose": "analysis_gm"})
        self.assertEqual(status, 201, export)
        self.assertEqual(export["row_count"], 5)
        blob = json.dumps(export)
        self.assertNotIn("visit_date", blob)
        self.assertNotIn("2026-03-10", blob)
        for row in export["rows"]:
            self.assertTrue(row["member_id"].startswith("m_"))
            self.assertNotIn(row["member_id"], {f"p{i}" for i in range(5)})
            self.assertEqual(row["visit_quarter"], "2026Q1")
            self.assertEqual(row["age_band"], "10-14")
            self.assertIn("metric.gmv", row)

        # 不同导出之间假名不可关联
        status, export2 = self.client.post("/exports", {
            "snapshot_id": snap["snapshot_id"], "requester": "r2", "purpose": "analysis_gm"})
        ids1 = {row["member_id"] for row in export["rows"]}
        ids2 = {row["member_id"] for row in export2["rows"]}
        self.assertTrue(ids1.isdisjoint(ids2))

    def test_unknown_metric_rejected(self):
        snap = self._freeze_five()
        status, err = self.client.post("/exports", {
            "snapshot_id": snap["snapshot_id"], "requester": "r1", "purpose": "analysis_gm",
            "fields": ["group", "metric:nope"]})
        self.assertEqual(status, 422)
        self.assertEqual(err["error"]["code"], "unknown_metric")

    def test_blocked_export_is_audited(self):
        for i in range(4):
            self.add_participant(f"p{i}")
        snap = self.freeze()
        self.client.post("/exports", {
            "snapshot_id": snap["snapshot_id"], "requester": "r1", "purpose": "analysis_gm"})
        status, audit = self.client.get("/audit?entity=snapshot")
        blocked = [e for e in audit["entries"] if e["action"] == "export_blocked"]
        self.assertTrue(blocked)
        self.assertEqual(blocked[0]["detail"]["reason"], "k_anonymity_violation")


if __name__ == "__main__":
    unittest.main()
