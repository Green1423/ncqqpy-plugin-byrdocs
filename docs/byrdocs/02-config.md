# 二、配置

> 文档索引：[docs/README.md](https://github.com/Green1423/ncqqpy/blob/main/docs/README.md) · 上级：[byrdocs 插件](README.md) · 上一章：[命令一览](01-commands.md) · 下一章：[下载为什么要登录（以及凭据去哪了）](03-auth.md)


`plugins.byrdocs`（插件首次加载会自动补齐缺失的键，**不会覆盖已有值**）：

```json
{
  "byrdocs": {
    "enabled": true,
    "data_dir": "./plugins/byrdocs/data",
    "username": "2026000000",
    "password": "……",
    "token": "",
    "timeout": 60,
    "login_cooldown": 60,
    "max_file_mb": 300,
    "cache_mb": 2048,
    "max_per_page": 5,
    "binding": { "groups": [], "users": [10000001] }
  }
}
```

> `binding.groups` 为空就是这个示例的意思：**群聊里谁都不能用**；只有
> `binding.users` 里的 QQ 能私聊调命令（这份配置里是超级用户自己，
> 也就是「刚装好、还没挑群」的状态）。要开放某个群就
> `/byrdocs add group <群号>`（超级用户私聊执行）。

| 键 | 默认 | 说明 |
|----|------|------|
| `data_dir` | `./plugins/byrdocs/data` | 状态与下载目录（相对路径以仓库根为基准） |
| `username` / `password` | 空 | 北邮账号（学号/工号）。留空 = 不自动登录 |
| `token` | 空 | 已有登录令牌（JWT）。配了就不走账号密码 |
| `timeout` | 60 | HTTP 超时秒数（登录单独放宽到 ≥90s） |
| `login_cooldown` | 60 | 登录失败后的冷却秒数（避免密码错时反复打统一认证） |
| `max_file_mb` | 300 | 单个文件下载上限；`0` = 不限 |
| `cache_mb` | 2048 | 下载缓存上限；`0` = 不淘汰 |
| `max_per_page` | 5 | 每页显示条数（1..20） |
| `binding.groups` | `[]` | 允许在**群聊**里使用的群号（白名单；空 = 群里不可用） |
| `binding.users` | `[]` | 允许**直接私聊**使用的 QQ 号（白名单；空 = 私聊不可用） |

凭据优先级：环境变量 `BYRDOCS_TOKEN` / `BYRDOCS_USERNAME` / `BYRDOCS_PASSWORD`
> `config.json`。用环境变量可以避免把密码写进配置文件。

---
