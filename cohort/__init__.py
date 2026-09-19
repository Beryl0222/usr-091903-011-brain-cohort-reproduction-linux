"""女性脑影像队列复现后端。

模块划分：

- ``cohort.schema``   数据库模式（SQLite，仅标准库）
- ``cohort.rules``    纯函数规则：阶段判定证据等级、快照准入、结论发布门控、导出隐私
- ``cohort.store``    存储层：批次幂等、冻结快照、撤回版本化、结论/导出门控
- ``cohort.api``      领域 HTTP 路由
"""

__all__ = ["rules", "schema", "store", "api"]
