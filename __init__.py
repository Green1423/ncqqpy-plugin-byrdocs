"""byrdocs —— BYR Docs（byrdocs.org）资料检索与下载插件。

插件入口：`PluginBase` 子类在 `main.py`，框架按类发现并实例化。
"""
from .main import ByrdocsPlugin

__all__ = ["ByrdocsPlugin"]
