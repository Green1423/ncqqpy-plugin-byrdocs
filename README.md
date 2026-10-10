# byrdocs —— BYR Docs 资料检索

检索 byrdocs.org 资料库、按编号翻页取详情、下载文件到群，带服务范围白名单。

这是 [ncqqpy](https://github.com/<you>/ncqqpy) 的插件，**独立仓库**：
框架按 `plugin_dir`（默认 `./plugins`）扫描子目录加载插件，所以把它 clone 到
框架的 `plugins/` 下即可。

## 安装

```bash
cd /path/to/ncqqpy
git clone https://github.com/<you>/ncqqpy-plugin-byrdocs plugins/byrdocs
# 重新加载（QQ 内，超级用户 · 仅私聊）
#   /reload byrdocs
```

要求：框架 [ncqqpy](https://github.com/<you>/ncqqpy)（`napcat` 包需可导入）。

## 配置

插件被**构造**时（`on_load()` 之前）会把自己的**全部**配置项写进
`config.json` 的 `plugins.byrdocs` 段（只补缺键、不覆盖已有值、幂等），所以打开
文件就能看到所有开关和默认值。模板如下：

```json
{
  "data_dir": "./plugins/byrdocs/data",
  "username": "",
  "password": "",
  "token": "",
  "timeout": 60,
  "login_cooldown": 60,
  "max_file_mb": 300,
  "cache_mb": 2048,
  "max_per_page": 5,
  "binding": {
    "groups": [],
    "users": []
  }
}
```

环境变量（优先级高于文件）：

| 变量 | 说明 |
|------|------|
| `BYRDOCS_TOKEN / BYRDOCS_USERNAME / BYRDOCS_PASSWORD` | 资料库凭据（也可写在 `config.json → plugins.byrdocs.*`；环境变量优先） |


详细文档：[`docs/byrdocs.md`](docs/byrdocs.md)。
## 测试

```bash
# 独立仓库里跑：框架需可导入（PYTHONPATH 指向框架仓库，或 pip install -e）
PYTHONPATH=/path/to/ncqqpy python -B tests/test_byrdocs.py
```

套件：`tests/test_byrdocs.py`。测试会在 `tests/.test-tmp/` 下建临时目录，不碰真实数据。

## 注意

- `tests/diagnostics/probe_byrdocs_upload.py` 是只读探针。

## License

MIT，见 [LICENSE](LICENSE)。
