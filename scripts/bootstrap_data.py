#!/usr/bin/python3
# coding: utf-8
"""安装/升级阶段为尚无统计的站点预热已有日志数据。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.collector import run_once
from core.settings import load_config


if __name__ == "__main__":
    config = dict(load_config())
    config["collect_from_end"] = False
    config["only_sites_without_statistics"] = True
    config["force_tail_backfill"] = True
    config["run_budget_seconds"] = min(
        30, max(5, int(config.get("run_budget_seconds", 30)))
    )
    print(json.dumps(run_once(config), ensure_ascii=False, sort_keys=True))
