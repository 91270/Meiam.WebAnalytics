#!/usr/bin/python3
# coding: utf-8
"""离线 IP 归属地解析，优先使用服务器已有的 GeoLite2/GeoIP2 MMDB。"""

from __future__ import annotations

import ipaddress
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional


COMMON_DATABASES = (
    "/www/server/webanalytics/data/GeoLite2-City.mmdb",
    "/www/server/panel/data/GeoLite2-City.mmdb",
    "/usr/share/GeoIP/GeoLite2-City.mmdb",
    "/usr/share/GeoIP/GeoIP2-City.mmdb",
    "/usr/local/share/GeoIP/GeoLite2-City.mmdb",
)


def _database_path() -> Optional[Path]:
    configured = os.environ.get("WEBANALYTICS_GEOIP_DB", "").strip()
    for value in ([configured] if configured else []) + list(COMMON_DATABASES):
        path = Path(value)
        if path.is_file():
            return path
    return None


def _name(value: Any) -> str:
    names = getattr(value, "names", None) or {}
    return str(names.get("zh-CN") or names.get("en") or getattr(value, "name", "") or "")


@lru_cache(maxsize=1)
def _reader():
    path = _database_path()
    if path is None:
        return None
    try:
        import geoip2.database  # type: ignore
        return ("geoip2", geoip2.database.Reader(str(path)))
    except (ImportError, OSError, ValueError):
        try:
            import maxminddb  # type: ignore
            return ("maxminddb", maxminddb.open_database(str(path)))
        except (ImportError, OSError, ValueError):
            return None


@lru_cache(maxsize=8192)
def locate_ip(value: str) -> Dict[str, str]:
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return {"location": "无效地址", "country": "", "region": "", "city": ""}
    if address.is_loopback:
        return {"location": "本机", "country": "", "region": "", "city": ""}
    if address.is_private:
        return {"location": "内网地址", "country": "", "region": "", "city": ""}
    if address.is_multicast or address.is_unspecified or address.is_reserved:
        return {"location": "保留地址", "country": "", "region": "", "city": ""}
    reader = _reader()
    if reader is None:
        return {"location": "未知", "country": "", "region": "", "city": ""}
    try:
        mode, backend = reader
        if mode == "geoip2":
            record = backend.city(str(address))
            country = _name(record.country)
            region = _name(record.subdivisions.most_specific)
            city = _name(record.city)
        else:
            record = backend.get(str(address)) or {}
            def dictionary_name(item):
                names = (item or {}).get("names") or {}
                return str(names.get("zh-CN") or names.get("en") or "")
            country = dictionary_name(record.get("country"))
            subdivisions = record.get("subdivisions") or []
            region = dictionary_name(subdivisions[0] if subdivisions else {})
            city = dictionary_name(record.get("city"))
        parts = []
        for part in (country, region, city):
            if part and part not in parts:
                parts.append(part)
        return {"location": " · ".join(parts) or "未知", "country": country,
                "region": region, "city": city}
    except Exception:
        return {"location": "未知", "country": "", "region": "", "city": ""}


def database_status() -> Dict[str, str]:
    path = _database_path()
    return {"available": bool(path and _reader()), "path": str(path or "")}
