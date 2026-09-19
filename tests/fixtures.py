"""测试共用的队列夹具：覆盖三种判定方式、激素覆盖差异、质控/指标缺失与重复测量。"""

from cohort.store import CohortStore

CONSENT_V1 = "consent-v1-2023"


def base_payload():
    return {
        "consent_versions": [
            {"version_code": CONSENT_V1, "title": "2023 版知情同意",
             "released_at": "2023-01-01",
             "scope_text": "MRI 影像与人生阶段纵向研究"}
        ],
        "participants": [
            {"participant_id": "P1", "cohort_group": "puberty",
             "birth_year": 2010},
            {"participant_id": "P2", "cohort_group": "puberty",
             "birth_year": 2011},
            {"participant_id": "P3", "cohort_group": "pregnancy",
             "birth_year": 1992},
            {"participant_id": "P4", "cohort_group": "pregnancy",
             "birth_year": 1990},
            {"participant_id": "P5", "cohort_group": "menopause",
             "birth_year": 1972},
            {"participant_id": "P6", "cohort_group": "menopause",
             "birth_year": 1970},
            {"participant_id": "P7", "cohort_group": "puberty",
             "birth_year": 2012},
        ],
        "consent_events": [
            {"participant_id": pid, "version_code": CONSENT_V1,
             "action": "given", "effective_at": "2024-01-01"}
            for pid in ("P1", "P2", "P3", "P4", "P5", "P6", "P7")
        ],
        "visits": [
            {"participant_id": "P1", "visit_date": "2024-03-01",
             "life_stage": "mid_puberty",
             "assessment_method": "menstrual_record",
             "hormone_assay_done": True, "hormone_assay_count": 2},
            {"participant_id": "P1", "visit_date": "2024-04-01",
             "life_stage": "mid_puberty",
             "assessment_method": "menstrual_record",
             "hormone_assay_done": False, "hormone_assay_count": 0},
            {"participant_id": "P2", "visit_date": "2024-03-05",
             "life_stage": "early_puberty",
             "assessment_method": "menstrual_record",
             "hormone_assay_done": False},
            {"participant_id": "P3", "visit_date": "2024-03-10",
             "life_stage": "gestation_week_20", "stage_detail": "GW20",
             "assessment_method": "gestational_age_record",
             "hormone_assay_done": True, "hormone_assay_count": 1},
            {"participant_id": "P4", "visit_date": "2024-03-12",
             "life_stage": "gestation_week_24", "stage_detail": "GW24",
             "assessment_method": "gestational_age_record",
             "hormone_assay_done": True},
            {"participant_id": "P5", "visit_date": "2024-03-15",
             "life_stage": "perimenopause",
             "assessment_method": "self_report",
             "hormone_assay_done": False},
            {"participant_id": "P6", "visit_date": "2024-03-18",
             "life_stage": "unknown",
             "assessment_method": "unassessed",
             "hormone_assay_done": False},
            {"participant_id": "P7", "visit_date": "2024-03-20",
             "life_stage": "early_puberty",
             "assessment_method": "menstrual_record",
             "hormone_assay_done": False},
        ],
        "controls": [
            {"visit_participant_id": "P3", "visit_date": "2024-03-10",
             "control_participant_id": "P4",
             "matched_on": {"age_year": 1, "scanner": True}}
        ],
        "mri": [
            {"participant_id": pid, "visit_date": vdate, "scanner_id": "SC-A",
             "field_tesla": 3.0, "series_uid": f"1.2.3.{pid}.{vdate}"}
            for pid, vdate in [
                ("P1", "2024-03-01"), ("P1", "2024-04-01"),
                ("P2", "2024-03-05"), ("P3", "2024-03-10"),
                ("P4", "2024-03-12"), ("P5", "2024-03-15"),
            ]
        ],
        "qc": [
            {"participant_id": "P1", "visit_date": "2024-03-01",
             "decision": "pass", "reviewer": "r1"},
            {"participant_id": "P1", "visit_date": "2024-04-01",
             "decision": "pass", "reviewer": "r1"},
            {"participant_id": "P2", "visit_date": "2024-03-05",
             "decision": "pass", "reviewer": "r1"},
            {"participant_id": "P3", "visit_date": "2024-03-10",
             "decision": "pass", "reviewer": "r2"},
            {"participant_id": "P4", "visit_date": "2024-03-12",
             "decision": "fail", "rationale": "运动伪影"},
            {"participant_id": "P5", "visit_date": "2024-03-15",
             "decision": "pass", "reviewer": "r2"},
        ],
        "metrics": [
            {"participant_id": "P1", "visit_date": "2024-03-01",
             "metric_code": "gm_volume", "value": 720.5,
             "metric_version": "pipe-2.1"},
            {"participant_id": "P1", "visit_date": "2024-04-01",
             "metric_code": "gm_volume", "value": 719.8,
             "metric_version": "pipe-2.1"},
            {"participant_id": "P2", "visit_date": "2024-03-05",
             "metric_code": "gm_volume", "value": 731.0,
             "metric_version": "pipe-2.1"},
            {"participant_id": "P2", "visit_date": "2024-03-05",
             "metric_code": "wm_volume", "is_missing": True,
             "missing_reason": "acquisition_failed",
             "metric_version": "pipe-2.1"},
            {"participant_id": "P3", "visit_date": "2024-03-10",
             "metric_code": "gm_volume", "value": 690.2,
             "metric_version": "pipe-2.1"},
            {"participant_id": "P4", "visit_date": "2024-03-12",
             "metric_code": "gm_volume", "value": 688.0,
             "metric_version": "pipe-2.1"},
            {"participant_id": "P5", "visit_date": "2024-03-15",
             "metric_code": "gm_volume", "value": 655.4,
             "metric_version": "pipe-2.1"},
        ],
    }


def seeded_store(batch_id="B1"):
    store = CohortStore()
    store.ingest_batch(batch_id, "data-center", base_payload())
    return store
