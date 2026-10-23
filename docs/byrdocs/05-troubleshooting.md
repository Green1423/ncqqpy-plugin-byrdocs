# 五、故障排查

> 文档索引：[docs/README.md](https://github.com/Green1423/ncqqpy/blob/main/docs/README.md) · 上级：[byrdocs 插件](README.md) · 上一章：[白名单（谁能用）](04-allowlist.md) · 下一章：[代码结构](06-code-structure.md)


| 现象 | 原因 / 处理 |
|------|------------|
| 群里发命令没反应 | 群不在 `binding.groups`（默认没配 = 不可用）；或没 @机器人；或框架未监听该群 |
| 私聊发命令没反应 | QQ 号不在 `binding.users`（小窗则看来源群） |
| 刚装好，哪儿都没反应 | 正常：白名单默认全关。超级用户私聊 `/byrdocs add group <群号>` 逐个加 |
| `🔐 该文件需要登录才能下载` | 没配账号密码，且出口 IP 不在校园网 → 配 `username/password` 或 `token` |
| `🔐 登录 byrdocs 失败：…` | 看错误原文（密码错 / 统一认证风控 / 服务端繁忙）；60s 内不会重试 |
| `⚠️ 文件过大` | 调大 `max_file_mb`（默认 300MB），或去 byrdocs.org 自行下载 |
| `ℹ️ 结果较多，只编号了前 100 条` | 搜索接口单次上限 100 条，换个更精确的关键词 |
| `🔍 还没有搜索记录` | 直接发了 `next` / `get`：先 `/byrdocs search <关键词>` |
| 想看状态 | 超级用户私聊 `/byrdocs status`（登录、出口 IP、白名单、缓存） |

日志前缀统一是 `[byrdocs]`。自检脚本
(../../../tests/test_byrdocs.py) 与
(../../../tests/diagnostics/probe_byrdocs_upload.py)：

```bash
cd /ncqqpy
venv-arm/bin/python -B tests/test_byrdocs.py              # 96 项离线用例
BYRDOCS_PASSWORD=… venv-arm/bin/python -B tests/test_byrdocs.py --online   # 追加真实联网
venv-arm/bin/python -B tests/diagnostics/probe_byrdocs_upload.py           # 真实发一个文件（会发私聊）
```

---
