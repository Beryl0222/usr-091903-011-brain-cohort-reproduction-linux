# 女性脑影像队列复现后端

服务用于管理女性人生阶段（青春期 / 妊娠期 / 更年期）脑影像研究的
知情同意、人生阶段事件、匹配对照、MRI 采集参数、质控结论与派生指标，
并为开放复核提供不可变分析快照、结论发布门控与隐私安全导出。

仅依赖 Python 3 标准库（含 SQLite），无外部运行时依赖。

## 运行

```bash
python3 service.py --check            # 配置与模式自检
python3 service.py --port 8000        # 进程内内存库（联调）
python3 service.py --db cohort.db     # 持久化 SQLite
COHORT_DB=cohort.db python3 service.py
npm test                              # 54 个测试（基础契约 + 规则 + 存储 + HTTP）
```

`/health` 保持原有稳定契约不变。

## 领域模型如何回应需求

| 需求 | 落地方式 |
| --- | --- |
| 三组阶段判定方式不同（月经/孕周记录 vs 自报，激素检测不全） | 每次访视记录 `assessment_method`（客观记录 / 临床评估 / 自报 / 未评估）与 `hormone_assay_done/count`；冻结时派生证据等级 A/B/C，并把判定方式分布写入快照摘要 |
| 缺失值、排除理由、重复测量 | 指标 `is_missing + missing_reason` 与数值零严格区分；排除使用稳定编码（`qc_fail`、`consent_withdrawn` 等）；同一参与者多次纳入访视标注 `repeat_index` / `repeated_measurement` |
| 迟到或重复上传不能改变已冻结分析 | 批次按内容指纹与行键做三级幂等（同批同内容幂等、异批同内容记 duplicate、同键不同值整批 conflict 拒收，绝不部分写入）；冻结表 `frozen_snapshots/snapshot_members` 由数据库触发器禁止 UPDATE/DELETE。迟到数据可进活动表并进入**新**快照 |
| 撤回后按同意版本停止新使用、记录既有聚合处置 | 同意是带版本的事件流；撤回只 prospective 地排除新分析，冻结快照原样保留；撤回返回受影响快照，聚合产物登记 `retain / restrict_access / remove` 决定与理由 |
| 统计人员重建稿件实际人群与代码版本 | `POST /snapshots` 物化纳入/排除清单（自包含，含同意版本、指标缺失、重复测量），记录稿件号、`analysis_code_version`、数据截止批次；`GET /snapshots/<id>/rebuild` 返回重建配方与排除理由字典 |
| 传播"未见加速萎缩"必须带适用范围与限制 | 结论发布门控：适用范围不得超出快照人群、人数必须一致；按数据实际情况强制附带 `SELF_REPORT_STAGE`、`HORMONES_NOT_COMPREHENSIVE`、`MIXED_ASSESSMENT_METHODS`（含标准中文文案），缺一即 HTTP 422 |
| 导出不得暴露可重识别的影像与时间组合 | 直接标识符拒绝；影像标识列与精确日期同现即拒绝（`IMAGE_TIME_REIDENTIFICATION`）；参与者级只允许月/年粗粒度时间；准标识符 k-匿名（默认 k=5）；聚合级抑制小分组；参与者编号以带盐假名 `P-xxxx` 输出，盐不随数据返回，跨导出不可链接 |

## HTTP 接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /consent-versions` | 登记同意模板版本 |
| `POST /batches/<batch_id>` | 上传批次（body: `source`, `payload`） |
| `GET /batches` / `GET /batches/<id>` | 批次列表/详情（含重复、冲突明细） |
| `POST /participants/<id>/withdraw` | 撤回同意（可指定 `version_code`） |
| `POST /snapshots` | 冻结分析快照（`snapshot_id`, `manuscript_ref`, `analysis_code_version`, 可选 `cutoff_batch_id`） |
| `GET /snapshots/<id>` / `.../rebuild` | 快照摘要 / 稿件人群重建包 |
| `POST /snapshots/<id>/artifacts` | 登记既有聚合产物 |
| `POST /artifacts/<id>/retention` | 撤回后处置：retain / restrict_access / remove |
| `POST /conclusions` | 发布结论（门控，失败返回 422 + 违规编码） |
| `GET /conclusions/<id>` | 结论（含适用范围与限制文案） |
| `POST /exports` | 导出申请（即时隐私评估并返回假名化行） |
| `GET /exports/<id>` | 导出决定记录（不含假名盐） |

写操作可通过 `X-Actor` 头记录操作人；全部关键动作进入 `audit_log`。

### 批次 payload 结构

```json
{
  "consent_versions": [{"version_code": "v1", "title": "...", "released_at": "2024-01-01", "scope_text": "..."}],
  "participants": [{"participant_id": "P1", "cohort_group": "puberty", "birth_year": 2010}],
  "consent_events": [{"participant_id": "P1", "version_code": "v1", "action": "given", "effective_at": "2024-01-01"}],
  "visits": [{"participant_id": "P1", "visit_date": "2024-03-01", "life_stage": "mid_puberty",
              "assessment_method": "menstrual_record", "hormone_assay_done": true}],
  "controls": [{"visit_participant_id": "P3", "visit_date": "2024-03-10",
                "control_participant_id": "P4", "matched_on": {"age_year": 1}}],
  "mri": [{"participant_id": "P1", "visit_date": "2024-03-01", "scanner_id": "SC-A",
           "field_tesla": 3.0, "series_uid": "1.2.3.x"}],
  "qc": [{"participant_id": "P1", "visit_date": "2024-03-01", "decision": "pass"}],
  "metrics": [{"participant_id": "P1", "visit_date": "2024-03-01",
               "metric_code": "gm_volume", "value": 720.5, "metric_version": "pipe-2.1"},
              {"participant_id": "P2", "visit_date": "2024-03-05",
               "metric_code": "wm_volume", "is_missing": true,
               "missing_reason": "acquisition_failed", "metric_version": "pipe-2.1"}]
}
```

`series_uid` 等影像唯一号在入库前做 SHA-256 哈希，明文不落库。

## 代码结构

```
cohort/schema.py   SQLite 模式与不可变触发器
cohort/rules.py    纯函数规则（证据分级、准入、结论门控、导出 k-匿名、哈希/假名）
cohort/store.py    批次幂等、冻结快照、撤回版本化、结论/导出门控、审计
cohort/api.py      领域路由（与传输层解耦）
service.py         HTTP 入口（/health 原契约 + 领域路由）
tests/             规则单测、存储端到端、HTTP 全链路
```

## 已知边界（有意为之）

- 结论门控验证的是**结构化**适用范围与强制限制项是否齐备且与数据一致，
  不校验自由文本措辞；传播文案仍需人工/法务复核。
- k-匿名基于申请列中的准标识符组合；默认 k=5，可按数据政策在每次导出时调整。
- 撤回采用 prospective 语义：系统不重写历史聚合，而是强制对每个受影响
  快照的聚合产物做出并记录处置决定。
