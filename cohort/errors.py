"""API 错误类型：统一错误响应结构。"""


class ApiError(Exception):
    """携带 HTTP 状态码与结构化详情的领域错误。"""

    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def body(self):
        error = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"error": error}


def not_found(entity, entity_id):
    return ApiError(
        404,
        "not_found",
        f"资源不存在: {entity} {entity_id}",
        {"entity": entity, "id": entity_id},
    )
