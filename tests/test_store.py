"""存储层端到端测试：冻结不可变、迟到/重复批次、撤回处置、门控、导出隐私。"""

import sqlite3
import unittest

from cohort.rules import canonical_json
from cohort.store import CohortStore, DomainError
from tests.fixtures import CONSENT_V1, base_payload, seeded_store


class SnapshotFreezeTest(unittest.TestCase):
    def setUp(self):
        self.store = seeded_store()
        self.snapshot = self.store.freeze_snapshot(
            "S1", "MS-2026-01", "git:abc123",
            selection_criteria={"qc": "pass_only"})

    def tearDown(self):
        self.store.close()

    def test_inclusion_and_exclusion_reasons(self):
        rebuild = self.store.rebuild_manuscript_cohort("S1")
        keys_in = {(m["participant_id"], m["visit_date"])
                   for m in rebuild["included"]}
        # P1 两次（合格，重复测量）、P2、P3、P5（自报但有指标且QC通过）
        self.assertIn(("P1", "2024-03-01"), keys_in)
        self.assertIn(("P1", "2024-04-01"), keys_in)
        self.assertIn(("P5", "2024-03-15"), keys_in)
        # P4 质控失败、P6 未评估阶段、P7 无指标
        excluded = {(m["participant_id"], m["visit_date"]):
                    m["exclusion_reason"] for m in rebuild["excluded"]}
        self.assertEqual(excluded[("P4", "2024-03-12")], "qc_fail")
        self.assertEqual(excluded[("P6", "2024-03-18")], "stage_unassessed")
        self.assertEqual(excluded[("P7", "2024-03-20")], "no_derived_metrics")

    def test_repeated_measurements_marked(self):
        rebuild = self.store.rebuild_manuscript_cohort("S1")
        p1 = sorted(
            [m for m in rebuild["included"] if m["participant_id"] == "P1"],
            key=lambda m: m["visit_date"])
        self.assertEqual([m["repeat_index"] for m in p1], [1, 2])
        self.assertEqual([m["repeated_measurement"] for m in p1], [False, True])
        others = [m for m in rebuild["included"]
                  if m["participant_id"] != "P1"]
        self.assertTrue(all(not m["repeated_measurement"] for m in others))

    def test_missing_metrics_kept_distinct_from_zero(self):
        rebuild = self.store.rebuild_manuscript_cohort("S1")
        p2 = [m for m in rebuild["included"]
              if m["participant_id"] == "P2"][0]
        self.assertEqual(p2["present_metric_codes"], ["gm_volume"])
        self.assertEqual(p2["missing_metric_codes"], ["wm_volume"])
        self.assertEqual(
            self.snapshot["cohort_summary"]["missing_metrics"],
            {"wm_volume": 1})

    def test_evidence_grades_reflect_methods(self):
        rebuild = self.store.rebuild_manuscript_cohort("S1")
        grades = {(m["participant_id"], m["visit_date"]): m["evidence_grade"]
                  for m in rebuild["included"]}
        self.assertEqual(grades[("P1", "2024-03-01")], "A")
        self.assertEqual(grades[("P1", "2024-04-01")], "B")
        self.assertEqual(grades[("P5", "2024-03-15")], "C")

    def test_snapshot_records_code_version_and_cutoff(self):
        self.assertEqual(self.snapshot["analysis_code_version"], "git:abc123")
        self.assertEqual(self.snapshot["manuscript_ref"], "MS-2026-01")
        self.assertEqual(self.snapshot["data_cutoff_batch"], "B1")
        recipe = self.store.rebuild_manuscript_cohort("S1")["rebuild_recipe"]
        self.assertEqual(recipe["code_version"], "git:abc123")
        self.assertEqual(recipe["data_cutoff_batch"], "B1")

    def test_frozen_tables_are_immutable_in_sql(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.conn.execute(
                "UPDATE frozen_snapshots SET manuscript_ref='x' WHERE snapshot_id='S1'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.conn.execute(
                "DELETE FROM snapshot_members WHERE snapshot_id='S1'")


class LateAndDuplicateBatchTest(unittest.TestCase):
    def setUp(self):
        self.store = seeded_store("B1")
        self.store.freeze_snapshot("S1", "MS1", "git:abc123")

    def tearDown(self):
        self.store.close()

    def test_late_batch_does_not_change_frozen_snapshot(self):
        # 迟到批次：新增参与者 + 覆盖式"修改"既有指标的企图（同键新值→冲突拒收）
        late = base_payload()
        late["participants"].append(
            {"participant_id": "P8", "cohort_group": "puberty",
             "birth_year": 2010})
        late["consent_events"].append(
            {"participant_id": "P8", "version_code": CONSENT_V1,
             "action": "given", "effective_at": "2024-01-01"})
        late["visits"].append(
            {"participant_id": "P8", "visit_date": "2024-05-01",
             "life_stage": "mid_puberty",
             "assessment_method": "menstrual_record"})
        late["qc"].append(
            {"participant_id": "P8", "visit_date": "2024-05-01",
             "decision": "pass"})
        late["metrics"].append(
            {"participant_id": "P8", "visit_date": "2024-05-01",
             "metric_code": "gm_volume", "value": 700.0,
             "metric_version": "pipe-2.1"})
        result = self.store.ingest_batch("B2", "data-center", late)
        self.assertEqual(result["status"], "accepted")

        rebuild_before = self.store.rebuild_manuscript_cohort("S1")
        before = canonical_json(rebuild_before)
        # 反复读取：冻结快照内容恒定
        self.assertEqual(
            canonical_json(self.store.rebuild_manuscript_cohort("S1")), before)
        included = {(m["participant_id"], m["visit_date"])
                    for m in rebuild_before["included"]}
        self.assertNotIn(("P8", "2024-05-01"), included)

        # 新快照可以纳入迟到数据
        s2 = self.store.freeze_snapshot("S2", "MS2", "git:def456")
        self.assertEqual(s2["data_cutoff_batch"], "B2")
        included2 = {m["participant_id"]
                     for m in self.store.rebuild_manuscript_cohort("S2")["included"]}
        self.assertIn("P8", included2)

        # 显式以旧批次 B1 为截止：迟到数据不可见，重建结果与 S1 一致
        s1b = self.store.freeze_snapshot("S1b", "MS1-rebuild", "git:abc123",
                                         cutoff_batch_id="B1")
        included_old = {m["participant_id"]
                        for m in self.store.rebuild_manuscript_cohort("S1b")["included"]}
        self.assertNotIn("P8", included_old)
        self.assertEqual(
            included_old,
            {m["participant_id"]
             for m in self.store.rebuild_manuscript_cohort("S1")["included"]})

    def test_same_batch_id_same_content_is_idempotent(self):
        first = self.store.ingest_batch("B1", "data-center", base_payload())
        self.assertIn(first["status"], ("accepted", "accepted_idempotent"))
        # 重复 POST 返回同一条批次记录，不新增任何行、不改写状态
        second = self.store.ingest_batch("B1", "data-center", base_payload())
        self.assertEqual(second["status"], first["status"])
        self.assertEqual(second["new_row_count"], first["new_row_count"])

    def test_same_batch_id_different_content_conflicts(self):
        changed = base_payload()
        changed["participants"][0]["birth_year"] = 1999
        with self.assertRaises(DomainError) as ctx:
            self.store.ingest_batch("B1", "data-center", changed)
        self.assertEqual(ctx.exception.code, "BATCH_CONFLICT")
        self.assertEqual(ctx.exception.status, 409)

    def test_identical_content_new_batch_id_marked_duplicate(self):
        result = self.store.ingest_batch("B1-copy", "backup-link", base_payload())
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(result["duplicate_of"], "B1")

    def test_row_level_conflict_rejects_whole_batch(self):
        payload = base_payload()
        # 同主键、不同指标值：任何行冲突整批拒收
        payload["metrics"][0]["value"] = 999.9
        with self.assertRaises(DomainError) as ctx:
            self.store.ingest_batch("B3", "data-center", payload)
        self.assertEqual(ctx.exception.code, "BATCH_ROW_CONFLICT")
        batch = self.store.get_batch("B3")
        self.assertEqual(batch["status"], "rejected_conflict")
        self.assertTrue(batch["conflicts"])

    def test_conflicting_batch_does_not_partially_apply(self):
        payload = base_payload()
        payload["participants"].append(
            {"participant_id": "P9", "cohort_group": "puberty"})
        payload["metrics"][0]["value"] = -1.0
        with self.assertRaises(DomainError):
            self.store.ingest_batch("B4", "data-center", payload)
        self.assertIsNone(self.store.conn.execute(
            "SELECT 1 FROM participants WHERE participant_id='P9'").fetchone())


class WithdrawalTest(unittest.TestCase):
    def setUp(self):
        self.store = seeded_store()
        self.store.freeze_snapshot("S1", "MS1", "git:abc123")

    def tearDown(self):
        self.store.close()

    def test_withdrawal_stops_new_use_but_keeps_frozen_analysis(self):
        result = self.store.withdraw_participant(
            "P5", "2024-06-01", actor="ethics", note="参与者行使撤回权")
        self.assertIn("S1", result["affected_snapshots"])

        # 既有冻结快照：P5 仍在（聚合结果的合法留存）
        s1 = self.store.rebuild_manuscript_cohort("S1")
        self.assertIn("P5", {m["participant_id"] for m in s1["included"]})

        # 新快照：P5 被排除，理由为 consent_withdrawn
        s2 = self.store.freeze_snapshot("S2", "MS2", "git:def456")
        s2_data = self.store.rebuild_manuscript_cohort("S2")
        p5 = [m for m in s2_data["excluded"] if m["participant_id"] == "P5"]
        self.assertTrue(p5)
        self.assertTrue(
            all(m["exclusion_reason"] == "consent_withdrawn" for m in p5))
        self.assertNotIn("P5",
                         {m["participant_id"] for m in s2_data["included"]})

    def test_withdrawal_before_visit_excludes_as_not_active(self):
        # P8 仅在 B0 出现：2023-01 给予同意、随后 2023-06 撤回，访视时已无同意
        self.store.ingest_batch("B0", "data-center", {
            "participants": [
                {"participant_id": "P8", "cohort_group": "puberty"}],
            "consent_events": [
                {"participant_id": "P8", "version_code": CONSENT_V1,
                 "action": "given", "effective_at": "2023-01-01"},
            ],
            "visits": [
                {"participant_id": "P8", "visit_date": "2024-02-01",
                 "life_stage": "mid_puberty",
                 "assessment_method": "menstrual_record"}],
            "qc": [
                {"participant_id": "P8", "visit_date": "2024-02-01",
                 "decision": "pass"}],
            "metrics": [
                {"participant_id": "P8", "visit_date": "2024-02-01",
                 "metric_code": "gm_volume", "value": 700.0,
                 "metric_version": "pipe-2.1"}],
        })
        self.store.withdraw_participant("P8", "2023-06-01")
        self.store.freeze_snapshot("S2b", "MS2", "git:def456")
        p8 = [m for m in self.store.rebuild_manuscript_cohort("S2b")["excluded"]
              if m["participant_id"] == "P8"]
        self.assertEqual(p8[0]["exclusion_reason"], "consent_not_active")

    def test_aggregate_artifact_retention_recorded(self):
        self.store.register_aggregate_artifact(
            "FIG-1", "S1", "figure", "图2：各组 GM 体积变化")
        artifact = self.store.decide_aggregate_retention(
            "FIG-1", "restrict_access",
            "含 P5 个体水平信息，撤回后限制内部访问，不再对外分发",
            decided_by="ethics")
        self.assertEqual(artifact["retention_decision"], "restrict_access")
        self.assertTrue(artifact["reason"])
        # 既有聚合的处置可审计
        log = self.store.conn.execute(
            "SELECT action FROM audit_log WHERE entity='artifact'").fetchall()
        self.assertIn("artifact_retention", [r["action"] for r in log])

    def test_withdraw_without_active_consent_is_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            self.store.withdraw_participant("P5", "2020-01-01")
        self.assertEqual(ctx.exception.code, "NO_ACTIVE_CONSENT")


class ConclusionGateStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = seeded_store()
        self.store.freeze_snapshot("S1", "MS1", "git:abc123")
        self.applicability = {
            "snapshot_id": "S1",
            "groups": ["puberty", "pregnancy", "menopause"],
            "stage_scope": "青春期/妊娠20-24周/围绝经期，2024年3-4月访视",
            "participant_count": 4,
        }
        self.required = ["SELF_REPORT_STAGE", "HORMONES_NOT_COMPREHENSIVE",
                         "MIXED_ASSESSMENT_METHODS"]

    def tearDown(self):
        self.store.close()

    def test_publish_without_limitations_blocked(self):
        with self.assertRaises(DomainError) as ctx:
            self.store.publish_conclusion(
                "C1", "S1", "未见加速萎缩",
                "三组女性灰质体积均未观察到加速萎缩。",
                self.applicability, limitation_codes=[])
        self.assertEqual(ctx.exception.code, "CONCLUSION_GATE_FAILED")
        self.assertEqual(ctx.exception.status, 422)
        self.assertTrue(
            any("SELF_REPORT_STAGE" in d for d in ctx.exception.details))

    def test_publish_with_full_limitations_carries_text(self):
        conclusion = self.store.publish_conclusion(
            "C2", "S1", "未见加速萎缩",
            "三组女性灰质体积均未观察到加速萎缩。",
            self.applicability, limitation_codes=self.required,
            channel="press-release", published_by="comms")
        codes = {item["code"] for item in conclusion["limitations"]}
        self.assertEqual(codes, set(self.required))
        self.assertTrue(all(item["text"] for item in conclusion["limitations"]))
        self.assertEqual(
            conclusion["applicability"]["groups"],
            ["puberty", "pregnancy", "menopause"])

    def test_applicability_cannot_claim_unstudied_group(self):
        applicability = dict(self.applicability, groups=["pregnancy", "male"])
        with self.assertRaises(DomainError) as ctx:
            self.store.publish_conclusion(
                "C3", "S1", "未见加速萎缩", "...", applicability,
                limitation_codes=self.required)
        self.assertIn("APPLICABILITY_GROUP_EXCEEDS_DATA",
                      ctx.exception.details)


class ExportStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = seeded_store()
        self.store.freeze_snapshot("S1", "MS1", "git:abc123")

    def tearDown(self):
        self.store.close()

    def test_image_with_exact_date_is_rejected(self):
        result = self.store.request_export(
            "E1", "analyst", "复核影像时间", "participant_level",
            ["series_uid_hash", "visit_date"], snapshot_id="S1", k_min=2)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(
            any("IMAGE_TIME_REIDENTIFICATION" in r for r in result["reasons"]))

    def test_direct_identifier_rejected(self):
        result = self.store.request_export(
            "E2", "analyst", "索要编号", "participant_level",
            ["participant_id", "cohort_group"], snapshot_id="S1", k_min=2)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(
            any("DIRECT_IDENTIFIER" in r for r in result["reasons"]))

    def test_pseudonymized_export_has_no_real_id_or_exact_date(self):
        result = self.store.request_export(
            "E3", "analyst", "人群描述", "participant_level",
            ["pseudonym", "visit_year", "hormone_assay_done"],
            snapshot_id="S1", k_min=2)
        self.assertNotEqual(result["status"], "rejected", result["reasons"])
        for row in result["rows"]:
            self.assertNotIn("participant_id", row)
            self.assertNotIn("visit_date", row)
            self.assertNotIn("series_uid_hash", row)
            self.assertTrue(row["pseudonym"].startswith("P-"))
            self.assertNotIn("P1", row["pseudonym"])

    def test_participant_level_precise_date_alone_is_rejected(self):
        result = self.store.request_export(
            "E4", "analyst", "精确时间", "participant_level",
            ["cohort_group", "visit_date"], snapshot_id="S1", k_min=2)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(
            any("PRECISE_TIME_AT_PARTICIPANT_LEVEL" in r
                for r in result["reasons"]))

    def test_aggregate_export_suppresses_small_cells(self):
        result = self.store.request_export(
            "E5", "analyst", "分组人数", "aggregate",
            ["cohort_group"], snapshot_id="S1", k_min=3)
        # menopause 组纳入者仅 P5（P6 排除），应被抑制
        self.assertEqual(result["status"], "suppressed_cells")
        suppressed = [c["group_key"] for c in result["suppressed_cells"]]
        self.assertIn(["menopause"], suppressed)

    def test_unsupported_column_rejected(self):
        result = self.store.request_export(
            "E6", "analyst", "越权列", "participant_level",
            ["cohort_group", "ssn"], snapshot_id="S1", k_min=1)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(
            any("UNSUPPORTED_COLUMN:ssn" in r for r in result["reasons"]))

    def test_export_record_persisted_without_salt_on_reject(self):
        self.store.request_export(
            "E7", "analyst", "违规", "participant_level",
            ["participant_id"], snapshot_id="S1")
        record = self.store.get_export("E7")
        self.assertEqual(record["status"], "rejected")
        self.assertTrue(record["decision_reasons"])


if __name__ == "__main__":
    unittest.main()
