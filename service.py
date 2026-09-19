"""女性脑影像队列复现的运行入口。

健康检查保持原有稳定契约；领域接口委托给 :mod:`cohort.api`。
数据默认落在进程内 SQLite（适合联调），用 ``--db`` 指定文件即可持久化。
"""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cohort.api import CohortRouter
from cohort.store import DomainError

SERVICE_ID = "brain-cohort-reproduction"
SERVICE_NAME = "女性脑影像队列复现"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """健康检查 + 队列研究领域路由。"""

    router = None  # 由 main() 注入，测试可直接替换

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DomainError("INVALID_JSON", "请求体不是合法 JSON", 400)
        if not isinstance(data, dict):
            raise DomainError("BODY_NOT_OBJECT", "请求体必须是 JSON 对象", 400)
        return data

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            self._send_json(200, health_payload())
            return
        result = self.router.dispatch("GET", self.path, {}, self.headers) \
            if self.router else None
        if result is None:
            self.send_error(404)
        else:
            self._send_json(*result)

    def do_POST(self):
        if self.router is None:
            self.send_error(404)
            return
        try:
            body = self._read_body()
        except DomainError as exc:
            self._send_json(exc.status,
                            {"error": exc.code, "message": exc.message})
            return
        result = self.router.dispatch("POST", self.path, body, self.headers)
        if result is None:
            self.send_error(404)
        else:
            self._send_json(*result)

    def log_message(self, *_args):
        return


def build_router(db_path=None):
    return CohortRouter(db_path=db_path or os.environ.get("COHORT_DB", ":memory:"))


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=None,
                        help="SQLite 数据库路径，默认进程内内存库")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        # 顺带验证领域模块可装配、数据库模式可初始化
        router = build_router(args.db)
        try:
            assert router.store.conn.execute(
                "SELECT 1 FROM frozen_snapshots LIMIT 1").fetchone() is None
        finally:
            router.close()
        print("基础检查通过")
        return

    Handler.router = build_router(args.db)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
