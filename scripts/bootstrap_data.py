#!/usr/bin/python3
# coding: utf-8
"""安装/升级阶段完整导入尚无统计站点的现存日志。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.collector import run_history_backfill
from core.settings import load_config


if __name__ == "__main__":
    config = dict(load_config())
    print(json.dumps(run_history_backfill(config), ensure_ascii=False, sort_keys=True))
