"""sts2mon —— 杀戮尖塔2 数据监控程序。

用法速览::

    python -m sts2mon doctor          # 检查数据源
    python -m sts2mon ingest          # 导入全部历史对局
    python -m sts2mon serve           # 打开仪表盘 http://127.0.0.1:8770
    python -m sts2mon watch           # 实时监控 + 仪表盘
    python -m sts2mon demo            # 用真实对局回放模拟实时
"""
from .analytics import Analytics
from .events import Event, EventBus, K
from .glossary import Glossary, default_glossary
from .live import LiveState, start_replay
from .model import Combat, MapNode, RunState, Store
from .tracker import Tracker

__version__ = "0.1.0"

__all__ = ["Analytics", "Combat", "Event", "EventBus", "Glossary", "K", "LiveState",
           "MapNode", "RunState", "Store", "Tracker", "default_glossary", "start_replay",
           "__version__"]
