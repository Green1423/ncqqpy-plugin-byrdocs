# tests/ —— byrdocs 插件验收套件

这些套件需要框架可导入（它们 `import napcat`）。两种跑法：

```bash
# ① 独立 clone 本插件仓库：把框架仓库挂到 PYTHONPATH
PYTHONPATH=/path/to/ncqqpy python -B tests/test_byrdocs.py

# ② 框架仓库布局（本插件已 clone 到框架的 plugins/byrdocs/ 下）
cd /path/to/ncqqpy && python -B tests/test_byrdocs.py
```

| 套件 | 覆盖 |
|------|------|
| `test_byrdocs.py` | 插件验收：搜索编号、翻页、取文件、白名单 |

## 说明

- 临时夹具落在 `tests/.test-tmp/`（不进仓库，可随时删）；
- 需要 Chromium 的套件（出图类）在没有浏览器的机器上会跳过相关断言；
- 依赖别的插件作夹具的断言，在本插件单独 checkout 时会打印 `⏭️ 跳过`。
