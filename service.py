"""女性脑影像队列复现的运行入口。"""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from cohort.api import App, path_known
from cohort.errors import ApiError

SERVICE_ID = "brain-cohort-reproduction"
SERVICE_NAME = "女性脑影像队列复现"

_DEFAULT_APP = None


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def default_app():
    """懒加载默认应用，避免仅做健康检查时创建数据库文件。"""
    global _DEFAULT_APP
    if _DEFAULT_APP is None:
        _DEFAULT_APP = App(os.environ.get("COHORT_DB", "cohort.db"))
    return _DEFAULT_APP


class Handler(BaseHTTPRequestHandler):
    """健康检查 + 队列领域 API。"""

    app = None  # 由 make_handler 绑定；未绑定时使用 default_app()

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def _resolve_app(self):
        return self.__class__.app if self.__class__.app is not None else default_app()

    def _dispatch(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if self.command == "GET" and path == "/health":
            self._send(200, health_payload())
            return
        if not path_known(path):
            self._send(404, {"error": {"code": "not_found", "message": "未知路由"}})
            return

        body = None
        if self.command == "POST":
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._send(400, {"error": {"code": "invalid_json", "message": "请求体不是合法 JSON"}})
                    return
                if not isinstance(body, dict):
                    self._send(400, {"error": {"code": "invalid_json", "message": "请求体必须是 JSON 对象"}})
                    return
            else:
                body = {}

        try:
            status, payload = self._resolve_app().handle(
                self.command, path, body, parse_qs(parsed.query))
        except ApiError as error:
            status, payload = error.status, error.body()
        except Exception as error:  # 兜底，避免把堆栈暴露给调用方
            status, payload = 500, {"error": {"code": "internal_error", "message": str(error)}}
        self._send(status, payload)

    def _send(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def make_handler(app):
    """生成绑定了指定应用的 Handler 子类（测试用内存库，生产用文件库）。"""
    return type("BoundHandler", (Handler,), {"app": app})


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=os.environ.get("COHORT_DB", "cohort.db"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        print("基础检查通过")
        return
    app = App(args.db)
    ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(app)).serve_forever()


if __name__ == "__main__":
    main()
