# coding=utf-8
"""
Ravenis Core - 热点新闻聚合与分析工具

使用方式:
  python -m trendradar        # 模块执行
  trendradar                  # 安装后执行
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trendradar.context import AppContext

__version__ = "6.9.0"
__all__ = ["AppContext", "__version__"]


def __getattr__(name: str) -> Any:
    """Keep library submodule imports lightweight while preserving the public API."""
    if name == "AppContext":
        from trendradar.context import AppContext

        return AppContext
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
