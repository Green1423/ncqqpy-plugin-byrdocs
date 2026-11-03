# 六、代码结构

> 文档索引：[docs/README.md](https://github.com/Green1423/ncqqpy/blob/main/docs/README.md) · 上级：[byrdocs 插件](README.md) · 上一章：[故障排查](05-troubleshooting.md)


| 文件 | 职责 |
|------|------|
| (../../../plugins/byrdocs/main.py) | 插件本体：命令注册、白名单闸门、渲染、取文件与发送 |
| (../../../plugins/byrdocs/byrdocs_api.py) | 上游 HTTP：搜索 API、账号登录、流式下载（含 `TooLarge`/`AuthRequired` 语义） |
| (../../../plugins/byrdocs/store.py) | 本地状态：搜索会话（编号 ↔ 资源）、下载登记与 LRU 淘汰 |
| (../../../plugins/byrdocs/schemas.py) | 命令参数 Schema 与目标（群/用户）解析 |
| (../../../plugins/byrdocs/data) | 运行时状态：`state.json` + `downloads/`（可整目录删除，只是缓存） |

上游接口文档：<https://byrdocs.org/llms.txt>、<https://search.byrdocs.org/llms.txt>。
