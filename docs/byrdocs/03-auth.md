# 三、下载为什么要登录（以及凭据去哪了）

> 文档索引：[docs/README.md](https://github.com/Green1423/ncqqpy/blob/main/docs/README.md) · 上级：[byrdocs 插件](README.md) · 上一章：[配置](02-config.md) · 下一章：[白名单（谁能用）](04-allowlist.md)


byrdocs 的文件下载要求**校园网 IP** 或**登录令牌**。插件按官方 CLI 的流程自动登录：

```text
POST https://byrdocs.org/api/auth/login        → {tokenURL, loginURL}
POST https://byrdocs.org/api/auth/bupt_login   → {token}     ← 账号密码在这里
GET  https://byrdocs.org/files/<md5>.<ext>     Authorization: Bearer <token>
```

> ⚠️ **密码会发给 byrdocs.org**：该端点由站点运营方代为登录北邮统一认证（CAS）。
> 这是官方 CLI 与网页登录框的既有流程，不是插件新开的旁路 —— 但确实意味着账号密码
> 会离开本机、落到第三方服务器。不接受就别配 `username/password`：
> * 改配 `token`（浏览器登录后从开发者工具里复制，或 `byrdocs login` 后用
>   `~/.config/byrdocs/token`）；
> * 或者让机器人跑在校园网里 —— 出口 IP 在校园网时**免登录**下载。
>   `/byrdocs status` 里的「校园网」一行就是判定结果。

令牌会缓存在内存里（不落盘），过期或下载被拒时自动用账号密码换一张重试一次；
登录失败有冷却，不会把统一认证打到风控。

---
