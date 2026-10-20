"""byrdocs —— BYR Docs（byrdocs.org）资料检索与下载插件。

插件入口在 `main.py`；这里只做导出与 `PLUGIN` 声明。
"""
from .main import ByrdocsPlugin

PLUGIN = ByrdocsPlugin
__all__ = ["ByrdocsPlugin"]
