"use strict";

const { spawnSync } = require("node:child_process");

const runs = [
  // 原基础契约（健康检查、未知路由）必须继续通过
  ["-m", "unittest", "-v", "service_contract"],
  // 领域测试（规则引擎 / 存储端到端 / HTTP 全链路）
  ["-m", "unittest", "discover", "-v", "-s", "tests", "-t", "."],
];

for (const args of runs) {
  const result = spawnSync("python3", args, { stdio: "inherit" });
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  if (result.status !== 0) process.exit(result.status ?? 1);
}
