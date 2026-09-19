# 女性脑影像队列复现

服务用于管理女性人生阶段（青春期 / 妊娠期 / 更年期）脑影像研究的访视、影像、同意与分析快照，支持对结论适用范围的复核。

运行 `python3 service.py --check` 可核对服务配置；执行 `python3 service.py --port 8000`（默认数据库 `cohort.db`，可用 `--db` 或环境变量 `COHORT_DB` 覆盖）后访问 `/health` 可确认服务身份。

## 设计原则

- **只增不改**：所有业务数据以追加方式写入。影像 / 质控 / 派生指标按访视版本化，重复上传按内容哈希幂等去重，内容变化的迟到上传生成新版本（旧版本保留并标记 `superseded_by`）。
- **冻结即不可变**：快照在冻结时刻把队列成员、每次访视引用的记录版本（`record_id + version + payload_hash`）、所依据的同意版本与分析代码版本钉入清单并计算内容哈希。数据中心迟到或重复上传都不会改变已冻结分析；`POST /snapshots/{id}/verify` 可随时重算校验。
- **撤回即停用**：参与者撤回后，新快照不再纳入、新导出自动剔除其数据行；撤回记录包含既有聚合结果的处理方式（`aggregate_handling`），旧快照清单作为历史记录保留并标记 `withdrawn_since_freeze`。
- **缺失与排除显式化**：质控 `missing` 必须给出 `missing_reason`，排除必须给出阶段与理由，快照清单附完整排除明细。
- **结论携带证据**：对外结论（claim）必须绑定冻结快照、适用范围与非空局限，并逐项确认快照自动识别的局限标记（自报判定、激素未全覆盖、判定方式不一致、重复测量、质控排除、小组样本量）。
- **导出脱敏**：成员标识按导出假名化（HMAC，跨导出不可关联），时间降精度为季度、年龄为 5 岁分带，按准标识符组合做 k-匿名（k=5）校验；影像采集参数不支持行级导出（影像+时间组合可重识别），仅提供聚合视图。被策略拒绝的导出也会留痕。

## API 概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/participants` | 登记参与者（`cohort_group`、`birth_year`） |
| GET | `/participants`、`/participants/{pid}` | 参与者列表 / 详情（状态、同意、撤回、访视） |
| POST | `/participants/{pid}/consents` | 记录知情同意版本（版本不可变） |
| POST | `/participants/{pid}/withdrawals` | 撤回（一次性；记录 `aggregate_handling`） |
| POST | `/participants/{pid}/stage-events` | 人生阶段事件（`stage`、`determination_method`、日期） |
| POST | `/participants/{pid}/visits` | 访视（`visit_index`、`visit_date`，支持重复测量） |
| POST | `/participants/{pid}/exclusions` | 排除记录（参与者级或访视级，必填理由） |
| GET | `/visits/{vid}` | 访视当前的影像 / 质控 / 派生指标最新版本 |
| POST | `/visits/{vid}/imaging` | 影像采集参数（版本化、幂等） |
| POST | `/visits/{vid}/qc` | 质控结论（`pass/fail/review/missing`） |
| POST | `/visits/{vid}/derived-metrics` | 派生指标（必填 `pipeline_version`，支持显式缺失） |
| POST | `/control-matches` | 对照匹配（`match_set_id`、匹配变量） |
| POST | `/snapshots` | 冻结分析快照（`purpose`、`code_version`、`cohort_spec`） |
| GET | `/snapshots/{sid}`、`/manifest`、`/imaging-summary` | 快照元信息 / 完整清单 / 影像聚合视图 |
| POST | `/snapshots/{sid}/verify` | 复核：重算哈希并核对钉住的记录版本 |
| POST | `/claims`、`GET /claims/{cid}` | 登记 / 查看对外结论（强制范围+局限+标记确认） |
| POST | `/exports`、`GET /exports/{eid}` | 脱敏导出（k-匿名、假名化、字段白名单） |
| GET | `/audit` | 审计日志（可 `?entity=` 过滤） |

## 枚举约定

- `cohort_group` / `stage`：`puberty`、`pregnancy`、`menopause`、`control`
- `determination_method`：`menstrual_record`、`gestational_week_record`、`hormone_assay`、`self_report`、`not_applicable`
- 质控 `missing_reason`：`not_collected`、`scan_failed`、`participant_unavailable`、`equipment_failure`、`withdrawn`、`other`
- `aggregate_handling`：`retain_existing_aggregates`、`exclude_from_future_aggregates`、`remove_where_feasible`

## 测试

`npm test`（等价于 `python3 -m unittest -v service_contract test_cohort`）运行基础契约测试与队列领域测试。
