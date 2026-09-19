"""通用工具：时间规范化、内容哈希与请求校验。"""

import datetime
import hashlib
import json
import uuid

from .errors import ApiError


def now_iso():
    """服务器当前时间（UTC，秒级）。所有 recorded_at 只能来自这里，
    客户端不能伪造系统时间，这是冻结分析点-in-time 正确性的基础。"""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def today_iso():
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def canonical(obj):
    """确定性 JSON 序列化，用于内容哈希。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(obj):
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def require(body, field):
    value = body.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ApiError(422, "missing_field", f"缺少必填字段: {field}", {"field": field})
    return value


def as_enum(value, allowed, field):
    if value not in allowed:
        raise ApiError(
            422,
            "invalid_value",
            f"字段 {field} 取值非法: {value}",
            {"field": field, "allowed": list(allowed)},
        )
    return value


def as_date(value, field):
    """临床日期（访视日期、签署日期等），规范为 YYYY-MM-DD。"""
    if not isinstance(value, str):
        raise ApiError(422, "invalid_date", f"字段 {field} 必须是 YYYY-MM-DD 日期", {"field": field})
    try:
        return datetime.date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        raise ApiError(
            422,
            "invalid_date",
            f"字段 {field} 必须是 YYYY-MM-DD 日期",
            {"field": field, "value": value},
        )


def as_datetime(value, field):
    """系统时间点（如快照 as_of），规范为 UTC 秒级 ISO 字符串，
    与 now_iso() 输出同格式，可按字典序比较。"""
    if not isinstance(value, str):
        raise ApiError(422, "invalid_datetime", f"字段 {field} 必须是 ISO 日期时间", {"field": field})
    text = value.strip()
    try:
        if len(text) == 10:
            day = datetime.date.fromisoformat(text)
            parsed = datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.timezone.utc)
        else:
            parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        raise ApiError(
            422,
            "invalid_datetime",
            f"字段 {field} 必须是 ISO 日期时间",
            {"field": field, "value": value},
        )
    return parsed.astimezone(datetime.timezone.utc).isoformat(timespec="seconds")


def as_str_list(value, field, allow_empty=False):
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ApiError(422, "invalid_value", f"字段 {field} 必须是非空字符串数组", {"field": field})
    if not value and not allow_empty:
        raise ApiError(422, "invalid_value", f"字段 {field} 不能为空数组", {"field": field})
    return [item.strip() for item in value]


def scope_covers(scope, purpose):
    """同意/撤回范围是否覆盖某个用途。"""
    return "all" in scope or purpose in scope
