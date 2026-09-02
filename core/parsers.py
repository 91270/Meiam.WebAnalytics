#!/usr/bin/python3
# coding: utf-8
"""Nginx/Apache combined 访问日志解析器。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
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


def parse_access_line(line: str) -> Optional[AccessEvent]:
    """解析 common/combined 兼容格式；无法识别时返回 None。"""
    match = _COMBINED_RE.match(line.strip())
    if not match:
        return None

    try:
        moment = datetime.strptime(match.group("time_local"), "%d/%b/%Y:%H:%M:%S %z")
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
            timestamp=int(moment.timestamp()),
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
