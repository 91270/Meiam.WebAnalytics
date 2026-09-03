#!/usr/bin/python3
# coding: utf-8
"""统计口径与访问分类。"""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath
from typing import Dict, Iterable, Optional
from urllib.parse import urlsplit

from .parsers import AccessEvent


_BOT_RULES = (
    ("Baiduspider", re.compile(r"baiduspider", re.I)),
    ("Googlebot", re.compile(r"googlebot", re.I)),
    ("Bingbot", re.compile(r"bingbot|msnbot", re.I)),
    ("Sogou", re.compile(r"sogou.*spider", re.I)),
    ("360Spider", re.compile(r"360spider|haosouspider", re.I)),
    ("Shenma", re.compile(r"yisouspider|sm\.cn", re.I)),
    ("YandexBot", re.compile(r"yandexbot", re.I)),
    ("Bytespider", re.compile(r"bytespider", re.I)),
    ("AhrefsBot", re.compile(r"ahrefsbot", re.I)),
    ("SemrushBot", re.compile(r"semrushbot", re.I)),
    ("MJ12bot", re.compile(r"mj12bot", re.I)),
    ("DotBot", re.compile(r"dotbot", re.I)),
    ("PetalBot", re.compile(r"petalbot", re.I)),
    ("GPTBot", re.compile(r"gptbot|chatgpt-user", re.I)),
    ("ClaudeBot", re.compile(r"claudebot|anthropic-ai", re.I)),
    ("CommonCrawl", re.compile(r"ccbot|commoncrawl", re.I)),
    ("ArchiveBot", re.compile(r"ia_archiver|archive\.org_bot", re.I)),
)


def stable_hash(value: str, salt: str) -> str:
    return hashlib.sha256((salt + "\0" + value).encode("utf-8", "ignore")).hexdigest()


def visitor_hash(event: AccessEvent, salt: str) -> str:
    return stable_hash(event.remote_addr + "\0" + event.user_agent, salt)


def ip_hash(event: AccessEvent, salt: str) -> str:
    return stable_hash(event.remote_addr, salt)


def request_path(uri: str) -> str:
    try:
        return urlsplit(uri).path or "/"
    except ValueError:
        return uri.split("?", 1)[0] or "/"


def is_page_view(event: AccessEvent, static_extensions: Iterable[str]) -> bool:
    if event.method.upper() not in {"GET", "HEAD"}:
        return False
    if not 200 <= int(event.status) < 400:
        return False
    if spider_name(event.user_agent) is not None:
        return False
    path = request_path(event.uri)
    suffix = PurePosixPath(path.lower()).suffix.lstrip(".")
    return not suffix or suffix not in set(static_extensions)


def is_excluded(event: AccessEvent, excluded_paths: Iterable[str]) -> bool:
    path = request_path(event.uri)
    return any(path.startswith(prefix) for prefix in excluded_paths if prefix)


def spider_name(user_agent: str) -> Optional[str]:
    for name, pattern in _BOT_RULES:
        if pattern.search(user_agent):
            return name
    if re.search(r"bot|spider|crawler|slurp", user_agent, re.I):
        return "OtherBot"
    return None


def client_info(user_agent: str) -> Dict[str, str]:
    """轻量 UA 分类；无需外部规则库即可覆盖主流客户端。"""
    ua = user_agent or ""
    if re.search(r"edg/", ua, re.I):
        browser = "Edge"
    elif re.search(r"opr/|opera", ua, re.I):
        browser = "Opera"
    elif re.search(r"chrome/|crios/", ua, re.I):
        browser = "Chrome"
    elif re.search(r"firefox/|fxios/", ua, re.I):
        browser = "Firefox"
    elif re.search(r"safari/", ua, re.I):
        browser = "Safari"
    else:
        browser = "Other"
    if re.search(r"windows", ua, re.I):
        system = "Windows"
    elif re.search(r"android", ua, re.I):
        system = "Android"
    elif re.search(r"iphone|ipad|ios", ua, re.I):
        system = "iOS"
    elif re.search(r"mac os|macintosh", ua, re.I):
        system = "macOS"
    elif re.search(r"linux", ua, re.I):
        system = "Linux"
    else:
        system = "Other"
    if re.search(r"bot|spider|crawler|slurp", ua, re.I):
        device = "Bot"
    elif re.search(r"ipad|tablet", ua, re.I):
        device = "Tablet"
    elif re.search(r"mobile|iphone|android", ua, re.I):
        device = "Mobile"
    else:
        device = "Desktop"
    return {"browser": browser, "system": system, "device": device}
