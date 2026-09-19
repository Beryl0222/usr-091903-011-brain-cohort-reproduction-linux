"""HTTP 层测试：通过真实 ThreadingHTTPServer 验证完整链路与错误码。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cohort.api import CohortRouter
from service import Handler
from tests.fixtures import CONSENT_V1, base_payload


class HttpScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.router = CohortRouter()
        Handler.router = cls.router
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.router.close()

    def post(self, path, payload):
        req = Request(
            f"{self.base}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-Actor": "tester"},
            method="POST")
        return json.load(urlopen(req, timeout=3))

    def get(self, path):
        return json.load(urlopen(f"{self.base}{path}", timeout=3))

    def post_expect_error(self, path, payload, status):
        req = Request(
            f"{self.base}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(HTTPError) as ctx:
            urlopen(req, timeout=3)
        self.assertEqual(ctx.exception.code, status)
        return json.load(ctx.exception)

    def test_full_open_review_workflow(self):
        # 1) 首批数据上传
        batch = self.post("/batches/B1",
                          {"source": "dc", "payload": base_payload()})
        self.assertEqual(batch["status"], "accepted")

        # 2) 重复上传：幂等
        again = self.post("/batches/B1",
                          {"source": "dc", "payload": base_payload()})
        self.assertEqual(again["new_row_count"], batch["new_row_count"])

        # 3) 冻结快照
        snap = self.post("/snapshots", {
            "snapshot_id": "S1", "manuscript_ref": "MS-2026-01",
            "analysis_code_version": "git:abc123"})
        self.assertIn("cohort_summary", snap)

        # 4) 统计人员重建稿件人群
        rebuild = self.get("/snapshots/S1/rebuild")
        self.assertEqual(
            rebuild["rebuild_recipe"]["code_version"], "git:abc123")
        self.assertTrue(rebuild["included"])
        self.assertTrue(rebuild["excluded"])

        # 5) 结论缺限制项 → 422
        applicability = {
            "snapshot_id": "S1",
            "groups": ["puberty", "pregnancy", "menopause"],
            "stage_scope": "三个人生阶段",
            "participant_count": 4,
        }
        err = self.post_expect_error("/conclusions", {
            "conclusion_id": "C-bad", "snapshot_id": "S1",
            "headline": "未见加速萎缩", "claim_text": "...",
            "applicability": applicability, "limitation_codes": []}, 422)
        self.assertEqual(err["error"], "CONCLUSION_GATE_FAILED")

        # 6) 结论带齐限制项 → 发布，响应同时含适用范围与限制文案
        conclusion = self.post("/conclusions", {
            "conclusion_id": "C-ok", "snapshot_id": "S1",
            "headline": "未见加速萎缩",
            "claim_text": "三组女性灰质未观察到加速萎缩。",
            "applicability": applicability,
            "limitation_codes": ["SELF_REPORT_STAGE",
                                 "HORMONES_NOT_COMPREHENSIVE",
                                 "MIXED_ASSESSMENT_METHODS"],
            "channel": "press"})
        self.assertEqual(len(conclusion["limitations"]), 3)

        # 7) 撤回后新快照排除该参与者
        self.post("/participants/P5/withdraw",
                  {"effective_at": "2024-06-01"})
        self.post("/snapshots", {
            "snapshot_id": "S2", "manuscript_ref": "MS-2026-02",
            "analysis_code_version": "git:def456"})
        rebuild2 = self.get("/snapshots/S2/rebuild")
        self.assertNotIn("P5",
                         {m["participant_id"] for m in rebuild2["included"]})

        # 8) 导出：影像 + 精确日期 → 拒绝
        blocked = self.post("/exports", {
            "export_id": "E-bad", "requested_by": "ext",
            "purpose": "复核", "granularity": "participant_level",
            "columns": ["series_uid_hash", "visit_date"],
            "snapshot_id": "S1", "k_min": 2})
        self.assertEqual(blocked["status"], "rejected")

        # 9) 导出：假名 + 粗粒度时间 → 放行，无真实编号与精确日期
        safe = self.post("/exports", {
            "export_id": "E-ok", "requested_by": "ext",
            "purpose": "传播配图数据", "granularity": "participant_level",
            "columns": ["pseudonym", "visit_year", "hormone_assay_done"],
            "snapshot_id": "S1", "k_min": 2})
        self.assertNotEqual(safe["status"], "rejected")
        for row in safe["rows"]:
            self.assertNotIn("participant_id", row)
            self.assertNotIn("visit_date", row)

    def test_bad_json_returns_400(self):
        req = Request(f"{self.base}/batches/X", data=b"{not json",
                      headers={"Content-Type": "application/json"},
                      method="POST")
        with self.assertRaises(HTTPError) as ctx:
            urlopen(req, timeout=3)
        self.assertEqual(ctx.exception.code, 400)

    def test_missing_route_404(self):
        with self.assertRaises(HTTPError) as ctx:
            urlopen(f"{self.base}/does-not-exist", timeout=3)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
