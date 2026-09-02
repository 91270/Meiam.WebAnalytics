#!/usr/bin/python3
# coding: utf-8
"""Nginx/Apache combined 访问日志解析器。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional


_QUOTED = r'(?P<{name}>(?:\\.|[^"\\])*)'
_COMBINED_RE = re.compile(
    r"^(?P<remote_addr>\S+)\s+\S+\s+\S+\s+"
    r"\[(?P<time_local>[^\]]+)\]\s+\""
    + _QUOTED.format(name="request")
    + r"\"\s+(?P<status>\d{3})\s+(?P<body_bytes>\d+|-)"
    + r"(?:\s+\""
    + _QUOTED.format(name="referer")
    + r"\"\s+\""
    + _QUOTED.format(name="user_agent")
    + r"\")?"
)

_TIME_RE = re.compile(
    r"^(?P<day>\d{1,2})/(?P<month>[A-Za-z]{3})/(?P<year>\d{4}):"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2}) "
    r"(?P<offset>[+-]\d{4})$"
)
_MONTHS = {
    name: index
    for index, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        1,
    )
}


@dataclass(frozen=True)
class AccessEvent:
    timestamp: int
    remote_addr: str
    method: str
    uri: str
    protocol: str
    status: int
    body_bytes: int
    referer: str
    user_agent: str
    host: str = ""
    cookie: str = ""


def _unescape(value: str) -> str:
    return value.replace(r"\x22", '"').replace(r"\x5C", "\\").replace(r"\"", '"')


def _parse_time(value: str) -> int:
    """解析日志固定英文月份，不依赖服务器的系统语言环境。"""
    match = _TIME_RE.match(value)
    if match is None:
        raise ValueError("invalid access log time")
    month = _MONTHS.get(match.group("month").title())
    if month is None:
        raise ValueError("invalid access log month")
    offset = match.group("offset")
    direction = 1 if offset[0] == "+" else -1
    offset_minutes = direction * (int(offset[1:3]) * 60 + int(offset[3:5]))
    moment = datetime(
        int(match.group("year")), month, int(match.group("day")),
        int(match.group("hour")), int(match.group("minute")),
        int(match.group("second")),
        tzinfo=timezone(timedelta(minutes=offset_minutes)),
    )
    return int(moment.timestamp())


def parse_access_line(line: str) -> Optional[AccessEvent]:
    """解析 common/combined 兼容格式；无法识别时返回 None。"""
    match = _COMBINED_RE.match(line.strip())
    if not match:
        return None

    try:
        request = _unescape(match.group("request"))
        request_parts = request.split(" ", 2)
        if len(request_parts) == 3:
            method, uri, protocol = request_parts
        elif len(request_parts) == 2:
            method, uri = request_parts
            protocol = ""
        else:
            method, uri, protocol = "", request, ""

        raw_bytes = match.group("body_bytes")
        return AccessEvent(
            timestamp=_parse_time(match.group("time_local")),
            remote_addr=match.group("remote_addr"),
            method=method[:16],
            uri=uri[:8192],
            protocol=protocol[:32],
            status=int(match.group("status")),
            body_bytes=0 if raw_bytes == "-" else int(raw_bytes),
            referer=_unescape(match.group("referer") or "")[:8192],
            user_agent=_unescape(match.group("user_agent") or "")[:4096],
        )
    except (ValueError, OverflowError):
        return None
