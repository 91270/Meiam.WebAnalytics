#!/usr/bin/python3
# coding: utf-8

from __future__ import annotations

import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.nginx_config import configure_all


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"enable", "disable"}:
        print("usage: configure_nginx.py {enable|disable}")
        raise SystemExit(2)
    print(json.dumps(configure_all(sys.argv[1] == "enable"), ensure_ascii=False))
