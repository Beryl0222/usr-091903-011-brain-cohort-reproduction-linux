"""领域规则引擎（纯函数，无 IO，便于独立测试与复核）。

覆盖四类规则：

1. 阶段判定证据分级：区分客观记录、自报与激素检测，差异本身要可量化。
2. 快照准入：同意状态、质控结论、阶段可判定性、指标缺失，逐一给出排除理由。
3. 结论发布门控：适用范围不得超出快照人群；自报、未全面测激素等限制为强制项。
4. 导出隐私评估：禁止直接标识符、禁止"影像 + 精确时间"组合、准标识符 k-匿名。
"""

import hashlib
import json

# ---------------------------------------------------------------------------
# 常量与字典
# ---------------------------------------------------------------------------

OBJECTIVE_METHODS = frozenset(
    {"menstrual_record", "gestational_age_record", "clinical_assessment"}
)
SELF_REPORT_METHODS = frozenset({"self_report", "unassessed"})

# 证据等级：
# A = 客观阶段记录 + 激素检测
# B = 客观记录但无激素，或自报但有激素检测佐证
# C = 仅自报/未评估，且无激素检测
GRADE_A = "A"
GRADE_B = "B"
GRADE_C = "C"

# 结论必须携带的限制项编码 -> 标准文案（传播团队只能照此附带，不得删改）
LIMITATION_CATALOG = {
    "SELF_REPORT_STAGE": "部分参与者的人生阶段仅依据自报，未经月经/孕周记录或临床评估核实。",
    "HORMONES_NOT_COMPREHENSIVE": "激素检测并非每次访视都进行，阶段划分未得到全面的激素水平佐证。",
    "MIXED_ASSESSMENT_METHODS": "三组人群阶段判定方式不一致（客观记录与自报混用），组间可比性受限。",
}

# 导出列分类（列名大小写不敏感，统一小写比较）
DIRECT_IDENTIFIER_COLUMNS = frozenset(
    {"participant_id", "name", "id_number", "passport", "phone", "email", "contact"}
)
IMAGE_COLUMNS = frozenset(
    {"series_uid", "series_uid_hash", "dicom_uid", "image_uri", "image_path"}
)
PRECISE_TIME_COLUMNS = frozenset({"visit_date", "scan_timestamp", "scan_datetime"})
COARSE_TIME_COLUMNS = frozenset({"visit_month", "visit_year"})
QUASI_IDENTIFIER_COLUMNS = frozenset(
    {
        "birth_year",
        "cohort_group",
        "life_stage",
        "stage_detail",
        "scanner_id",
        "site",
        "visit_month",
        "visit_year",
        "field_tesla",
    }
)

DEFAULT_K = 5


# ---------------------------------------------------------------------------
# 规范化哈希
# ---------------------------------------------------------------------------

def canonical_json(obj):
    """生成稳定的 JSON 文本（键排序、无多余空白），用于批次指纹与假名。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(*parts, salt=""):
    """对任意多个部件生成可复现的 SHA-256 摘要。

    批次去重时不带盐（同内容必须得到同哈希）；
    对外假名使用带盐摘要，盐随导出记录保存且不随数据发放。
    """
    material = canonical_json({"salt": salt, "parts": [str(p) for p in parts]})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def pseudonym(participant_id, salt):
    """参与者对外假名：不可逆、同一次导出内稳定、跨导出不可链接。"""
    return "P-" + stable_hash(participant_id, salt=salt)[:16]


# ---------------------------------------------------------------------------
# 1. 阶段判定证据分级
# ---------------------------------------------------------------------------

def evidence_grade(assessment_method, hormone_assay_done):
    """返回单访视阶段判定的证据等级。"""
    hormone = bool(hormone_assay_done)
    if assessment_method in OBJECTIVE_METHODS:
        return GRADE_A if hormone else GRADE_B
    if assessment_method == "self_report":
        return GRADE_B if hormone else GRADE_C
    # unassessed 或未知值：按最弱证据处理
    return GRADE_C


def required_limitations(members):
    """根据快照实际纳入的访视推导"必须附带"的限制项编码集合。

    ``members`` 为可迭代的字典，每项至少含
    ``assessment_method``、``hormone_assay_done``。
    结论发布时这些编码一个都不能缺，缺即拒发。
    """
    methods = set()
    has_self_report = False
    total = 0
    with_hormone = 0
    for m in members:
        total += 1
        method = m.get("assessment_method", "unassessed")
        methods.add(method)
        if method in SELF_REPORT_METHODS:
            has_self_report = True
        if m.get("hormone_assay_done"):
            with_hormone += 1

    required = set()
    if has_self_report:
        required.add("SELF_REPORT_STAGE")
    if total > 0 and with_hormone < total:
        required.add("HORMONES_NOT_COMPREHENSIVE")
    if methods & OBJECTIVE_METHODS and methods & SELF_REPORT_METHODS:
        required.add("MIXED_ASSESSMENT_METHODS")
    return required


# ---------------------------------------------------------------------------
# 2. 快照准入与重复测量
# ---------------------------------------------------------------------------

def evaluate_visit_eligibility(*, consent_active, qc_decision, assessment_method,
                               present_metric_count):
    """纯规则：判断一次访视能否进入分析，返回 (是否纳入, 排除理由)。

    排除理由为稳定编码，写入冻结快照的 ``exclusion_reason``，
    统计人员重建人群时可直接按编码复现筛选。
    """
    if not consent_active:
        return False, "consent_not_active"
    # 阶段无法判定时，质控与指标均无讨论前提（该访视通常根本没有扫描）
    if assessment_method == "unassessed" or not assessment_method:
        return False, "stage_unassessed"
    if qc_decision == "fail":
        return False, "qc_fail"
    if qc_decision == "provisional":
        return False, "qc_provisional"
    # 没有任何派生指标的访视无法进入分析；连扫描都没有时质控自然也缺失，
    # 此时以"无指标"作为更本质的排除理由
    if present_metric_count <= 0:
        return False, "no_derived_metrics"
    if qc_decision is None or qc_decision == "":
        return False, "qc_missing"
    return True, ""


def annotate_repeated_measurements(included_members):
    """对纳入访视按参与者标注重复测量序号。

    入参为 ``[{participant_id, visit_date, ...}, ...]``，
    原地（并返回）补充 ``repeated_measurement`` 与 ``repeat_index``：
    同一参与者第 2 次及以后的纳入访视 ``repeated_measurement=1``，
    ``repeat_index`` 从 1 开始按访视日期排序。
    """
    by_participant = {}
    for m in included_members:
        by_participant.setdefault(m["participant_id"], []).append(m)
    for visits in by_participant.values():
        visits.sort(key=lambda m: m["visit_date"])
        for idx, m in enumerate(visits, start=1):
            m["repeat_index"] = idx
            m["repeated_measurement"] = 1 if idx > 1 else 0
    return included_members


# ---------------------------------------------------------------------------
# 3. 结论发布门控
# ---------------------------------------------------------------------------

def validate_applicability(applicability, *, snapshot_id, included_groups,
                           snapshot_members):
    """校验结论的适用范围声明。返回违规编码列表（空列表表示通过）。"""
    violations = []
    if not isinstance(applicability, dict):
        return ["APPLICABILITY_NOT_STRUCTURED"]

    if applicability.get("snapshot_id") != snapshot_id:
        violations.append("APPLICABILITY_SNAPSHOT_MISMATCH")

    groups = applicability.get("groups") or []
    if not groups:
        violations.append("APPLICABILITY_GROUP_EMPTY")
    extra = set(groups) - set(included_groups)
    if extra:
        violations.append("APPLICABILITY_GROUP_EXCEEDS_DATA")

    scope = applicability.get("stage_scope")
    if not scope:
        violations.append("APPLICABILITY_STAGE_SCOPE_MISSING")

    # 声称的样本量必须与冻结快照一致，防止传播稿使用过期数字
    claimed_n = applicability.get("participant_count")
    actual_n = len({m["participant_id"] for m in snapshot_members})
    if claimed_n is not None and claimed_n != actual_n:
        violations.append("APPLICABILITY_COUNT_MISMATCH")
    return violations


def evaluate_conclusion_publication(*, applicability, limitation_codes,
                                    snapshot_id, included_groups,
                                    snapshot_members):
    """结论发布门控。返回 ``{"allowed": bool, "violations": [...]}``。"""
    violations = validate_applicability(
        applicability,
        snapshot_id=snapshot_id,
        included_groups=included_groups,
        snapshot_members=snapshot_members,
    )
    required = required_limitations(snapshot_members)
    missing = required - set(limitation_codes or [])
    for code in sorted(missing):
        violations.append(f"LIMITATION_MISSING:{code}")
    unknown = set(limitation_codes or []) - set(LIMITATION_CATALOG)
    for code in sorted(unknown):
        violations.append(f"LIMITATION_UNKNOWN:{code}")
    return {"allowed": not violations, "violations": violations}


# ---------------------------------------------------------------------------
# 4. 导出隐私评估
# ---------------------------------------------------------------------------

def _norm_columns(columns):
    return {c.lower(): c for c in columns}


def evaluate_export(*, columns, rows, k_min=DEFAULT_K, group_counts=None):
    """评估一次导出是否可以放行。

    - ``columns``：申请导出的列名列表。
    - ``rows``：参与者级数据行（字典列表），用于准标识符 k-匿名核算；
      聚合级导出可为空，此时使用 ``group_counts``。
    - ``group_counts``：聚合级导出的分组人数，``{组键元组: 人数}``。

    返回::

        {"status": "released|suppressed_cells|rejected",
         "reasons": [...], "suppressed_cells": [...], "released_rows": [...]}

    规则：
      1. 直接标识符一律拒绝（参与者编号须经 :func:`pseudonym` 替换）。
      2. 影像标识列与精确时间列不得同时出现——这是"可重识别影像+时间"红线。
      3. 参与者级：准标识符组合的每个等价类人数须 ≥ k，否则拒绝并给出最小类。
      4. 聚合级：人数 < k 的分组被抑制；全部被抑制则拒绝。
    """
    normalized = _norm_columns(columns)
    lower = set(normalized)
    reasons = []

    direct = lower & DIRECT_IDENTIFIER_COLUMNS
    if direct:
        reasons.append("DIRECT_IDENTIFIER:" + ",".join(sorted(direct)))

    if (lower & IMAGE_COLUMNS) and (lower & PRECISE_TIME_COLUMNS):
        reasons.append(
            "IMAGE_TIME_REIDENTIFICATION:"
            + ",".join(sorted(lower & IMAGE_COLUMNS))
            + "+"
            + ",".join(sorted(lower & PRECISE_TIME_COLUMNS))
        )

    suppressed_cells = []

    if group_counts is None:
        # 参与者级：按申请列中的准标识符（含粗粒度时间）做 k-匿名
        qi_cols = [c for c in columns if c.lower() in QUASI_IDENTIFIER_COLUMNS]
        classes = {}
        for row in rows:
            key = tuple(row.get(c) for c in qi_cols)
            classes.setdefault(key, 0)
            classes[key] += 1
        small = [key for key, n in classes.items() if n < k_min]
        if small:
            smallest = min(classes.values())
            reasons.append(
                f"K_ANONYMITY:k={k_min},smallest_class={smallest},"
                f"quasi={qi_cols}"
            )
            suppressed_cells = [
                {"quasi_key": list(key), "count": classes[key]} for key in small
            ]
        released_rows = [
            row for row in rows
            if tuple(row.get(c) for c in qi_cols) not in set(small)
        ]
    else:
        # 聚合级：抑制小分组
        released_counts = {}
        for key, count in group_counts.items():
            if count < k_min:
                suppressed_cells.append({"group_key": list(key), "count": count})
            else:
                released_counts[key] = count
        released_rows = [
            {"group_key": list(key), "count": n}
            for key, n in sorted(released_counts.items())
        ]
        if not released_counts:
            reasons.append(f"K_ANONYMITY:all_groups_below_k={k_min}")

    if any(r.startswith(("DIRECT_IDENTIFIER", "IMAGE_TIME")) for r in reasons):
        status = "rejected"
        released_rows = []
    elif group_counts is not None:
        # 聚合级：有可放行分组则带抑制标记；全被抑制则整体拒绝
        status = "suppressed_cells" if released_rows else "rejected"
    elif suppressed_cells:
        status = "rejected"
    else:
        status = "released"
    return {
        "status": status,
        "reasons": reasons,
        "suppressed_cells": suppressed_cells,
        "released_rows": released_rows,
    }
