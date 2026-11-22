"""投研大盘数据采集模块 - 采集器包。"""

from .macro import collect_macro
from .etf import collect_etf

__all__ = ["collect_macro", "collect_etf"]
