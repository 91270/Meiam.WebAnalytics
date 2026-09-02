#!/usr/bin/python3
# coding: utf-8

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.repository import Repository
from core.settings import load_config


if __name__ == "__main__":
    load_config()
    Repository().initialize()
    print("WebAnalytics database initialized")
