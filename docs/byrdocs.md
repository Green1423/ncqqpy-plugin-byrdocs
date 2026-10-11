# byrdocs 插件使用说明

> 插件目录：[plugins/byrdocs/](../plugins/byrdocs/) · 配置段：[config.json](../config.json) → `plugins.byrdocs`
> 上游：[byrdocs.org](https://byrdocs.org)（BYR Docs，北邮资料分享平台）
> 自检：[tests/test_byrdocs.py](../tests/test_byrdocs.py)（`venv-arm/bin/python -B tests/test_byrdocs.py`）
>
> 文档索引：[docs/README.md](README.md)

在 QQ 里检索 BYR Docs 的资料（教材 / 资料 / 试题），按编号取回**文件**，并管理
「哪些群、哪些人能用」的白名单。

## 目录

1. [命令一览](#一命令一览)
    - [1.1 实际长什么样](#11-实际长什么样)
    - [1.2 几个设计点](#12-几个设计点)
2. [配置](#二配置)
3. [下载为什么要登录（以及凭据去哪了）](#三下载为什么要登录以及凭据去哪了)
4. [白名单（谁能用）](#四白名单谁能用)
5. [故障排查](#五故障排查)
6. [代码结构](#六代码结构)

---

## 一、命令一览

前缀取自 [config.json](../config.json) 的 `command_prefixes`（默认 `/` 和 `#`）。群里需要先 @机器人。

| 命令 | 说明 | 权限 |
|------|------|------|
| `/byrdocs search <关键词 ...> [--type=book] [--page=1]` | 搜索并给结果编号 | **所有人**（群聊 / 私聊 / 小窗；另受白名单约束） |
| `/byrdocs next [页数]` | 翻到下一页（沿用上次关键词） | **所有人** |
| `/byrdocs get <编号\|md5>` | 下载并以**文件形式**发送 | **所有人** |
| `/byrdocs targets [group 群号\|user QQ号]` | 查看白名单 | 仅私聊 · 仅超级用户 |
| `/byrdocs add <group 群号\|user QQ号>` | 加入白名单 | 仅私聊 · 仅超级用户 |
| `/byrdocs del <group 群号\|user QQ号>` | 移出白名单 | 仅私聊 · 仅超级用户 |
| `/byrdocs status` | 登录 / 出口 IP / 缓存状态 | 仅私聊 · 仅超级用户 |

权限声明写在**每条子命令自己身上**，由框架在路由前校验：`scope` 不符或权限不够都
按「命令不存在」处理（不泄露命令存在）。所以在群里，管理命令既执行不了、也不出现在
`/help` 里；在私聊里，非超级用户看到的也只有 `search` / `next` / `get`。
这两级（框架权限 + 下面的服务范围白名单）是**与关系**：公开命令也要先过白名单。

`/help byrdocs`、`/help byrdocs search` 有同一份帮助（由框架按 Schema 渲染）。

### 1.1 实际长什么样

```
> /byrdocs search 数据结构 期末 --type=test
🔍 「数据结构 期末」 · 📝 试题｜第 1/7 页｜共 14 条

 1. 2025-2026 第二学期 数据结构与算法 期末试卷
   📝 试题 · PDF · 4.8 MB · 原题
   2025 期末
 2. 2025-2026 第二学期 数据结构与算法 期末试卷
   📝 试题 · PDF · 原题
   2025 期末

➡️ 下一页：/byrdocs next
📥 取文件：/byrdocs get <编号>（本页 1–2）

> /byrdocs next
🔍 「数据结构 期末」 · 📝 试题｜第 2/7 页｜共 14 条
 3. …

> /byrdocs get 3
📥 正在下载「2025-2026 第二学期 数据结构与算法 期末试卷」…
（随后收到一个 PDF 文件）
```

### 1.2 几个设计点

* **编号跨页稳定**：byrdocs 搜索接口没有 offset，一次最多 100 条。插件一次取回
  全部结果并编号 `1..N`，`next` 只是切窗口 —— 所以第 2 页的 `6 号` 永远指向同一份
  资料，翻回去还能用。**换关键词重新搜索时编号从 1 重排**（旧编号作废，会明确提示
  「找不到编号」，不会张冠李戴）。
* **多词关键词**：`search 高等数学 期末` 会把词拼成一个关键词（中文检索天然带空格），
  类型与页码请用 `--type=` / `--page=`。想要一次给全也可以用引号：
  `/byrdocs search "高等数学 期末"`。
* **群小窗沿用群里的编号**：在群里搜索后点头像开小窗发 `get 3`，编号照样对得上
  （小窗与来源群共用一份搜索会话）；文件会发到小窗里。
* **取文件是「文件」不是链接**：群聊走 `upload_group_file`（群文件），
  私聊 / 小窗走 `upload_private_file`（在线文件）。两条路都失败才退回 file 消息段。
* **本地缓存**：同一个文件只下一次（按 MD5 认），重复 `get` 直接发缓存；
  总占用超过 `cache_mb` 时按「最久未用」淘汰，并顺手清理下载目录里的孤儿文件。

---

## 二、配置

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

## 三、下载为什么要登录（以及凭据去哪了）

byrdocs 的文件下载要求**校园网 IP** 或**登录令牌**。插件按官方 CLI 的流程自动登录：

```
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

## 四、白名单（谁能用）

**白名单模式，默认全关**：没有显式配置 = **群聊和私聊一个都不绑定**，任何地方都
用不了（表现是**完全静默** —— 连 `/help` 里都看不到本插件，与「命令不存在」同口径）。

| 上下文 | 放行条件 |
|--------|----------|
| 群聊 | 群号 ∈ `binding.groups` |
| 群聊临时会话（小窗） | **来源群** ∈ `binding.groups`（不看 QQ 号：群里能用，该群成员的小窗就能用） |
| 直接私聊 | QQ 号 ∈ `binding.users` |

* **不继承框架的 `bound_groups`**：框架那份回答的是「机器人监听哪些群」（空 = 全部），
  与「允许哪些群用 byrdocs」（空 = 一个都不允许）语义正好相反。拿它兜底等于
  「机器人进了多少群，这个插件就在多少群里可用」，与白名单模式自相矛盾。
* **超级用户豁免**：`config.json → superusers` 里的人不受白名单限制 —— 否则一旦
  配错（比如把自己锁在外面）就再也改不回来。**第一次启用只能靠超级用户私聊**
  往白名单里加东西（管理命令本来就只在私聊、只对超级用户开放，正好一致）。

增删（仅超级用户，且必须私聊）：

```
/byrdocs add group 1000000001      # 把这个群加进来（加上之后该群及其小窗都能用）
/byrdocs add user 10000001         # 允许这个 QQ 直接私聊使用
/byrdocs del group 1000000001      # 移出
/byrdocs targets                   # 查看当前白名单
```

群里只给纯数字时按「用户」理解、私聊里按「群」理解（更符合当时的语境），
想明确就写前缀。改动会**立即写回 `config.json`**，重启不丢。

> 小提醒：`del user <自己>` 会让你在私聊里立刻失去访问权（群管理员仍可改回来，
> 超级用户也豁免）。

---

## 五、故障排查

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
[`tests/test_byrdocs.py`](../tests/test_byrdocs.py) 与
[`tests/diagnostics/probe_byrdocs_upload.py`](../tests/diagnostics/probe_byrdocs_upload.py)：

```bash
cd /ncqqpy
venv-arm/bin/python -B tests/test_byrdocs.py              # 96 项离线用例
BYRDOCS_PASSWORD=… venv-arm/bin/python -B tests/test_byrdocs.py --online   # 追加真实联网
venv-arm/bin/python -B tests/diagnostics/probe_byrdocs_upload.py           # 真实发一个文件（会发私聊）
```

---

## 六、代码结构

| 文件 | 职责 |
|------|------|
| [main.py](../plugins/byrdocs/main.py) | 插件本体：命令注册、白名单闸门、渲染、取文件与发送 |
| [byrdocs_api.py](../plugins/byrdocs/byrdocs_api.py) | 上游 HTTP：搜索 API、账号登录、流式下载（含 `TooLarge`/`AuthRequired` 语义） |
| [store.py](../plugins/byrdocs/store.py) | 本地状态：搜索会话（编号 ↔ 资源）、下载登记与 LRU 淘汰 |
| [schemas.py](../plugins/byrdocs/schemas.py) | 命令参数 Schema 与目标（群/用户）解析 |
| [data/](../plugins/byrdocs/data/) | 运行时状态：`state.json` + `downloads/`（可整目录删除，只是缓存） |

上游接口文档：<https://byrdocs.org/llms.txt>、<https://search.byrdocs.org/llms.txt>。
