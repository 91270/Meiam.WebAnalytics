#!/usr/bin/python3
# coding: utf-8
"""WebAnalytics Nginx syslog 数据报协议。"""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import datetime
from typing import Iterable, Optional, Tuple

from .parsers import AccessEvent


_MESSAGE_RE = re.compile(rb"(?:^|\s)wa_(\d+)_access:\s*(\{.*\})\s*$", re.S)


def _address(value: object) -> Optional[str]:
    candidate = str(value or "").strip().strip("[]")
    if not candidate or candidate == "-":
        return None
    if candidate.count(":") == 1 and "." in candidate:
        candidate = candidate.rsplit(":", 1)[0]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _trusted(address: str, cidrs: Iterable[str]) -> bool:
    try:
        value = ipaddress.ip_address(address)
        return any(value in ipaddress.ip_network(str(cidr), strict=False) for cidr in cidrs)
    except ValueError:
        return False


def resolve_client_ip(data: dict, trusted_proxy_cidrs: Iterable[str]) -> str:
    peer = _address(data.get("remote_addr") or data.get("ip")) or ""
    if not peer or not _trusted(peer, trusted_proxy_cidrs):
        return peer

    for key in (
        "cf_connecting_ip",
        "true_client_ip",
        "ali_cdn_real_ip",
        "cdn_real_ip",
        "x_real_ip",
        "client_ip",
    ):
        candidate = _address(data.get(key))
        if candidate:
            return candidate

    forwarded = str(data.get("x_forwarded_for") or data.get("forwarded_for") or "")
    valid_chain = [
        candidate for candidate in (_address(item) for item in forwarded.split(","))
        if candidate
    ]
    for candidate in reversed(valid_chain):
        if not _trusted(candidate, trusted_proxy_cidrs):
            return candidate
    return valid_chain[0] if valid_chain else peer


def parse_datagram(
    payload: bytes, trusted_proxy_cidrs: Iterable[str] = ()
) -> Optional[Tuple[int, AccessEvent]]:
    match = _MESSAGE_RE.search(payload)
    if not match:
        return None
    try:
        panel_site_id = int(match.group(1))
        data = json.loads(match.group(2).decode("utf-8", "replace"))
        moment = datetime.fromisoformat(str(data["time"]).replace("Z", "+00:00"))
        return panel_site_id, AccessEvent(
            timestamp=int(moment.timestamp()),
            remote_addr=resolve_client_ip(data, trusted_proxy_cidrs)[:256],
            method=str(data.get("method", ""))[:16],
            uri=str(data.get("uri", ""))[:8192],
            protocol=str(data.get("protocol", ""))[:32],
            status=int(data.get("status", 0)),
            body_bytes=max(0, int(data.get("bytes", 0) or 0)),
            referer=str(data.get("referer", ""))[:8192],
            user_agent=str(data.get("ua", "") or data.get("user_agent", ""))[:4096],
            host=str(data.get("host", ""))[:512],
            cookie=str(data.get("cookie", ""))[:4096],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
