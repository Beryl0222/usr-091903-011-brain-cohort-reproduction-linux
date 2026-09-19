"""规则引擎单元测试：证据分级、限制项推导、结论门控、导出隐私。"""

import unittest

from cohort import rules


class EvidenceGradeTest(unittest.TestCase):
    def test_objective_record_with_hormone_is_grade_a(self):
        self.assertEqual(
            rules.evidence_grade("menstrual_record", True), rules.GRADE_A)
        self.assertEqual(
            rules.evidence_grade("gestational_age_record", True),
            rules.GRADE_A)

    def test_objective_record_without_hormone_is_grade_b(self):
        self.assertEqual(
            rules.evidence_grade("clinical_assessment", False), rules.GRADE_B)

    def test_self_report_grades(self):
        self.assertEqual(
            rules.evidence_grade("self_report", True), rules.GRADE_B)
        self.assertEqual(
            rules.evidence_grade("self_report", False), rules.GRADE_C)
        self.assertEqual(
            rules.evidence_grade("unassessed", False), rules.GRADE_C)


class RequiredLimitationsTest(unittest.TestCase):
    def test_all_three_limitations_required_for_mixed_cohort(self):
        members = [
            {"assessment_method": "menstrual_record",
             "hormone_assay_done": True},
            {"assessment_method": "self_report",
             "hormone_assay_done": False},
        ]
        self.assertEqual(
            rules.required_limitations(members),
            {"SELF_REPORT_STAGE", "HORMONES_NOT_COMPREHENSIVE",
             "MIXED_ASSESSMENT_METHODS"})

    def test_partial_hormone_coverage_still_flags(self):
        members = [
            {"assessment_method": "menstrual_record",
             "hormone_assay_done": True},
            {"assessment_method": "menstrual_record",
             "hormone_assay_done": False},
        ]
        required = rules.required_limitations(members)
        self.assertIn("HORMONES_NOT_COMPREHENSIVE", required)
        self.assertNotIn("SELF_REPORT_STAGE", required)
        self.assertNotIn("MIXED_ASSESSMENT_METHODS", required)

    def test_full_objective_and_hormone_coverage_has_no_limits(self):
        members = [
            {"assessment_method": "gestational_age_record",
             "hormone_assay_done": True},
        ]
        self.assertEqual(rules.required_limitations(members), set())


class EligibilityTest(unittest.TestCase):
    def ok(self, **kw):
        defaults = dict(consent_active=True, qc_decision="pass",
                        assessment_method="menstrual_record",
                        present_metric_count=1)
        defaults.update(kw)
        return rules.evaluate_visit_eligibility(**defaults)

    def test_included(self):
        self.assertEqual(self.ok(), (True, ""))

    def test_exclusion_reasons(self):
        self.assertEqual(self.ok(consent_active=False),
                         (False, "consent_not_active"))
        self.assertEqual(self.ok(qc_decision="fail"), (False, "qc_fail"))
        self.assertEqual(self.ok(qc_decision="provisional"),
                         (False, "qc_provisional"))
        self.assertEqual(self.ok(qc_decision=None), (False, "qc_missing"))
        self.assertEqual(self.ok(assessment_method="unassessed"),
                         (False, "stage_unassessed"))
        self.assertEqual(self.ok(present_metric_count=0),
                         (False, "no_derived_metrics"))


class RepeatedMeasurementTest(unittest.TestCase):
    def test_repeat_index_per_participant(self):
        members = [
            {"participant_id": "A", "visit_date": "2024-05-01"},
            {"participant_id": "B", "visit_date": "2024-02-01"},
            {"participant_id": "A", "visit_date": "2024-02-01"},
        ]
        rules.annotate_repeated_measurements(members)
        a = sorted(
            [m for m in members if m["participant_id"] == "A"],
            key=lambda m: m["visit_date"])
        self.assertEqual([m["repeat_index"] for m in a], [1, 2])
        self.assertEqual([m["repeated_measurement"] for m in a], [0, 1])
        b = [m for m in members if m["participant_id"] == "B"][0]
        self.assertEqual(b["repeat_index"], 1)
        self.assertEqual(b["repeated_measurement"], 0)


class ConclusionGateTest(unittest.TestCase):
    def setUp(self):
        self.members = [
            {"participant_id": "P1", "cohort_group": "puberty",
             "assessment_method": "menstrual_record",
             "hormone_assay_done": True},
            {"participant_id": "P2", "cohort_group": "menopause",
             "assessment_method": "self_report",
             "hormone_assay_done": False},
        ]
        self.applicability = {
            "snapshot_id": "S1",
            "groups": ["puberty", "menopause"],
            "stage_scope": "mid_puberty/perimenopause",
            "participant_count": 2,
        }

    def test_complete_limitations_pass(self):
        verdict = rules.evaluate_conclusion_publication(
            applicability=self.applicability,
            limitation_codes=["SELF_REPORT_STAGE",
                              "HORMONES_NOT_COMPREHENSIVE",
                              "MIXED_ASSESSMENT_METHODS"],
            snapshot_id="S1",
            included_groups=["puberty", "menopause"],
            snapshot_members=self.members)
        self.assertTrue(verdict["allowed"], verdict["violations"])

    def test_missing_self_report_limitation_blocks(self):
        verdict = rules.evaluate_conclusion_publication(
            applicability=self.applicability,
            limitation_codes=["HORMONES_NOT_COMPREHENSIVE",
                              "MIXED_ASSESSMENT_METHODS"],
            snapshot_id="S1",
            included_groups=["puberty", "menopause"],
            snapshot_members=self.members)
        self.assertFalse(verdict["allowed"])
        self.assertIn("LIMITATION_MISSING:SELF_REPORT_STAGE",
                      verdict["violations"])

    def test_scope_exceeding_data_blocks(self):
        applicability = dict(self.applicability, groups=["pregnancy"])
        verdict = rules.evaluate_conclusion_publication(
            applicability=applicability, limitation_codes=[],
            snapshot_id="S1",
            included_groups=["puberty", "menopause"],
            snapshot_members=self.members)
        self.assertFalse(verdict["allowed"])
        self.assertIn("APPLICABILITY_GROUP_EXCEEDS_DATA", verdict["violations"])

    def test_wrong_count_blocks(self):
        applicability = dict(self.applicability, participant_count=999)
        verdict = rules.evaluate_conclusion_publication(
            applicability=applicability, limitation_codes=[],
            snapshot_id="S1",
            included_groups=["puberty", "menopause"],
            snapshot_members=self.members)
        self.assertIn("APPLICABILITY_COUNT_MISMATCH", verdict["violations"])

    def test_unknown_limitation_code_blocks(self):
        verdict = rules.evaluate_conclusion_publication(
            applicability=self.applicability,
            limitation_codes=["MADE_UP_LIMIT"],
            snapshot_id="S1",
            included_groups=["puberty", "menopause"],
            snapshot_members=self.members)
        self.assertIn("LIMITATION_UNKNOWN:MADE_UP_LIMIT",
                      verdict["violations"])


class ExportRulesTest(unittest.TestCase):
    def test_direct_identifier_rejected(self):
        result = rules.evaluate_export(
            columns=["participant_id", "gm_volume"],
            rows=[{"participant_id": "P1", "gm_volume": 1.0}], k_min=1)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any("DIRECT_IDENTIFIER" in r for r in result["reasons"]))

    def test_image_plus_precise_time_rejected(self):
        result = rules.evaluate_export(
            columns=["image_uri", "visit_date"],
            rows=[{"image_uri": "x", "visit_date": "2024-03-01"}] * 10,
            k_min=2)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(
            any("IMAGE_TIME_REIDENTIFICATION" in r
                for r in result["reasons"]))

    def test_image_with_coarse_time_allowed(self):
        result = rules.evaluate_export(
            columns=["image_uri", "visit_month"],
            rows=[{"image_uri": "x", "visit_month": "2024-03"}] * 10,
            k_min=2)
        self.assertEqual(result["status"], "released", result["reasons"])

    def test_k_anonymity_failure_reports_smallest_class(self):
        rows = [
            {"birth_year": 2010, "cohort_group": "puberty"},
            {"birth_year": 2011, "cohort_group": "puberty"},
            {"birth_year": 2010, "cohort_group": "puberty"},
        ]
        result = rules.evaluate_export(
            columns=["birth_year", "cohort_group"], rows=rows, k_min=2)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(
            any(r.startswith("K_ANONYMITY") for r in result["reasons"]))

    def test_aggregate_small_groups_suppressed(self):
        result = rules.evaluate_export(
            columns=["cohort_group"], rows=[], k_min=5,
            group_counts={("puberty",): 20, ("menopause",): 2})
        self.assertEqual(result["status"], "suppressed_cells")
        self.assertEqual(len(result["released_rows"]), 1)
        self.assertEqual(result["suppressed_cells"][0]["count"], 2)

    def test_aggregate_all_small_rejected(self):
        result = rules.evaluate_export(
            columns=["cohort_group"], rows=[], k_min=5,
            group_counts={("puberty",): 1})
        self.assertEqual(result["status"], "rejected")


class HashingTest(unittest.TestCase):
    def test_batch_hash_stable_without_salt(self):
        self.assertEqual(rules.stable_hash({"a": 1}),
                         rules.stable_hash({"a": 1}))

    def test_pseudonym_unlinkable_across_exports(self):
        p1 = rules.pseudonym("P1", salt="salt-a")
        p2 = rules.pseudonym("P1", salt="salt-b")
        self.assertNotEqual(p1, p2)
        self.assertEqual(rules.pseudonym("P1", salt="salt-a"), p1)
        self.assertNotIn("P1", p1)


if __name__ == "__main__":
    unittest.main()
