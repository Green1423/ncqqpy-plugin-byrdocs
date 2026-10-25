"""byrdocs.org 资料检索 / 下载插件。

功能
----
* **检索**：`/byrdocs search <关键词>` 走官方搜索 API（中文分词、ISBN / MD5 精确匹配），
  结果**编号**后返回元数据（书名 / 作者 / 类型 / 大小 / 出版社）
* **翻页**：`/byrdocs next`（byrdocs 搜索接口没有 offset，一次最多 100 条 ——
  本插件一次取回并编号 1..N，翻页只是切窗口，所以**编号跨页稳定**）
* **取文件**：`/byrdocs get <编号>` 下载后**以文件形式**发送（群聊发群文件、
  私聊发在线文件），带本地缓存（同一个文件只下一次）
* **账号登录**：下载需要校园网 IP 或登录令牌。配置账号密码后，
  插件按官方 CLI 的流程走 `POST byrdocs.org/api/auth/bupt_login`（站点代跑北邮
  统一认证），拿到 JWT 放进 `Authorization: Bearer`；令牌与凭据都落在
  `config.json`（见「配置」）
* **服务范围白名单**：`add` / `del` / `targets` 维护**本插件自己的**允许清单
  （群聊 + 私聊），与框架的 `bound_groups` / 权限体系互不干扰。
  **没显式配置就是谁都不能用**（群聊 / 私聊都一样，只有超级用户豁免）

配置（`config.json` → `plugins.byrdocs`）
----------------------------------------
    data_dir        状态与下载目录，默认 `plugins/byrdocs/data`
    username        北邮账号（学号 / 工号）；留空 = 不自动登录
    password        北邮密码；**也可用环境变量 `BYRDOCS_PASSWORD`（优先级更高）**
    token           已有登录令牌（JWT）；配了就不走账号密码
    timeout         HTTP 超时秒数，默认 60
    login_cooldown  登录失败冷却秒数（默认 60，避免密码错时反复打统一认证）
    max_file_mb     单个文件下载上限，默认 300；0 = 不限
    cache_mb        下载缓存上限，默认 2048；超了按「最久未用」淘汰
    max_per_page    `search` 每页条数，默认 5（1..20）
    binding.groups  允许在**群聊**里使用的群号（白名单，空 = 群里不可用）
    binding.users   允许**直接私聊**使用的 QQ 号（空 = 私人私聊不可用）
                    ⚠️ 两份都不配 = 哪里都不可用（不继承框架的 bound_groups）

凭据优先级：环境变量 `BYRDOCS_TOKEN` / `BYRDOCS_USERNAME` / `BYRDOCS_PASSWORD`
最优先，其次才是 `config.json`。

命令（前缀取自 config.json，默认 `/` 和 `#`）
-----------------------------------------------
用法里的占位符统一写作 `<名称>`（必填）与 `[名称]`（可选），尖括号/方括号本身不用输入。

    /byrdocs search <关键词 ...> [--type=book] [--page=1]   搜索并编号
    /byrdocs next [页数]                    往后翻页（沿用上次关键词）
    /byrdocs get <编号>                     下载并以文件形式发送
    /byrdocs targets                       查看白名单（群 / 用户）
    /byrdocs add <group 群号|user QQ号>     加入白名单
    /byrdocs del <group 群号|user QQ号>     移出白名单
    /byrdocs status                        查看登录 / 缓存 / 白名单状态

权限（声明在各子命令自己身上，框架在路由前校验；不满足一律回「命令不存在」）
--------------------------------------------------------------------------
    search / next / get              公开：群聊、私聊、群小窗都能用（仍受下面的白名单约束）
    targets / add / del / status     **仅私聊 + 仅超级用户**（群里连帮助里都不出现）

服务范围（白名单）
------------------
**白名单模式，默认全关**：`binding.groups` 之外的群（含该群的群聊小窗）、
`binding.users` 之外的私聊，一律静默不响应（连 `/help` 里都看不到本命名空间）。
**两张表都没配 = 一个地方都用不了** —— 不看框架的 `bound_groups`（那是「监听哪些
群」，与「允许哪些群用本插件」是两件事；拿它兜底等于"机器人进了多少群就能在多少群用"）。
超级用户豁免 —— 否则一配错就再也改不回来。判定只有一处 `check_access()`，
命令守卫与 `/help` 可见性共用它。

⚠️ 凭据流向：**密码会发给 byrdocs.org**，由站点代为登录北邮统一认证（官方 CLI
与网页登录框都是这个流程）。不接受就别配账号密码，改配 `token`，或让机器人跑在
校园网里（校园网 IP 免登录下载）。
"""
from __future__ import annotations

import functools
import inspect
import os
import time
from pathlib import Path
from typing import Optional

from napcat.command import GROUP_AT_HINT
from napcat.message import MessageChain
from napcat.plugin import PluginBase

from .byrdocs_api import (
    AuthFailed,
    AuthRequired,
    ByrdocsClient,
    ByrdocsError,
    DownloadError,
    SearchError,
    TooLarge,
    filename_of,
    format_size,
    guess_ext,
    is_md5,
)
from .schemas import (
    GetSchema,
    NextSchema,
    SearchSchema,
    TargetSchema,
    TargetsSchema,
    join_spec,
    parse_target_ctx,
)
from .store import MAX_RESULTS, ResultStore

DEFAULT_PAGE_SIZE = 5
DEFAULT_MAX_FILE_MB = 300
DEFAULT_CACHE_MB = 2048

TYPE_LABEL = {
    "book": "📕 教材",
    "doc": "📄 资料",
    "test": "📝 试题",
}
TYPE_ALIAS = {
    "book": "book", "书": "book", "教材": "book", "图书": "book",
    "doc": "doc", "资料": "doc", "文档": "doc", "课件": "doc",
    "test": "test", "试题": "test", "试卷": "test", "真题": "test",
    "all": "all", "全部": "all", "所有": "all",
}
CONTENT_LABEL = {"原题": "原题", "答案": "答案", "题库": "题库",
                 "知识点": "知识点", "课件": "课件", "思维导图": "思维导图"}


def _disp_width(text: str) -> int:
    """粗略显示宽度：CJK 全角算 2，其余算 1（只用于排版，不追求精确）。"""
    width = 0
    for ch in text:
        width += 2 if ord(ch) > 0x2E7F else 1
    return width


def _clip(text: str, limit: int) -> str:
    """按显示宽度截断（中文标题很长时不至于把一行撑爆）。"""
    text = " ".join(str(text).split())
    if _disp_width(text) <= limit:
        return text
    out = ""
    width = 0
    for ch in text:
        step = 2 if ord(ch) > 0x2E7F else 1
        if width + step > limit - 1:
            break
        out += ch
        width += step
    return out + "…"


class ByrdocsPlugin(PluginBase):
    name = "byrdocs"
    version = "1.0.0"
    description = "BYR Docs 资料检索与下载（搜索编号 / 翻页 / 取文件 / 白名单）"
    author = "ncqqpy"

    # ------------------------------------------------------------------ 生命周期

    async def on_load(self):
        self._data_dir = self._resolve_data_dir()
        os.makedirs(self._data_dir, exist_ok=True)
        self._store = ResultStore(os.path.join(self._data_dir, "state.json"),
                                  logger=self.logger)
        self._client: Optional[ByrdocsClient] = None
        self._command_names: list[str] = []
        self._visibility_filter = None
        self._scope_denied: dict[int, tuple] = {}
        # 配置项由 `config_template()` 经框架在加载时补进 config.json
        # （只补缺、不覆盖、嵌套段逐键补、幂等，见 PluginBase.config_template）
        self.logger.info("[byrdocs] 已加载（数据目录 %s）", self._data_dir)

    async def on_enable(self):
        self._client = ByrdocsClient(
            token=os.environ.get("BYRDOCS_TOKEN") or self.config.get("token", "") or "",
            username=(os.environ.get("BYRDOCS_USERNAME")
                      or self.config.get("username", "") or ""),
            password=(os.environ.get("BYRDOCS_PASSWORD")
                      or self.config.get("password", "") or ""),
            timeout=self._int_cfg("timeout", 60, minimum=5),
            login_cooldown=self._int_cfg("login_cooldown", 60, minimum=0),
            logger=self.logger,
        )
        self._register_commands()
        # 技能通道是框架新加的（`napcat.skills`），**框架代码要重启进程**才生效；
        # 热重载只换插件代码。这时不能崩 —— 命令是主功能，技能没有就只是没有。
        if hasattr(self, "register_skill"):
            try:
                self._register_skills()
            except Exception as e:  # noqa: BLE001 - 技能失败不该影响插件启用
                # 典型：进程里还是旧框架（`register_skill` 签名不同）——
                # 技能没挂上而已，命令照旧可用；重启进程后生效
                self.logger.warning("[byrdocs] 注册技能失败：%s", e)
        else:
            self.logger.warning(
                "[byrdocs] 当前进程里的框架没有 register_skill：技能暂不注册"
                "（命令不受影响），重启进程后生效")
        if not self._client.has_credentials and not self._client.token:
            self.logger.warning(
                "[byrdocs] 未配置账号密码或令牌：仅当机器人能直连校园网时才能下载文件"
                "（配置 plugins.byrdocs.username/password，或环境变量 BYRDOCS_PASSWORD）")
        if not self._bound_groups() and not self._bound_users():
            self.logger.warning(
                "[byrdocs] 服务范围白名单为空：群聊与私聊都不可用（未显式配置不绑定任何目标）。"
                "先用超级用户在私聊发 /byrdocs add group <群号> 或 add user <QQ号>")

    async def on_disable(self):
        await super().on_disable()
        for cname in list(self._command_names):
            try:
                self.unregister_command(cname)
            except Exception:  # noqa: BLE001 - 卸载阶段尽力而为
                pass
        self._command_names = []
        if self._visibility_filter is not None:
            try:
                self.cmds.remove_visibility_filter(self._visibility_filter)
            except Exception:  # noqa: BLE001
                pass
            self._visibility_filter = None
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._store.save()
        self.logger.info("[byrdocs] 已停用")

    async def on_unload(self):
        try:
            self._store.save()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 配置

    def config_template(self) -> dict:
        """完整配置模板（框架在加载时写进 `config.json` → `plugins.byrdocs`）。

        只补缺键、不覆盖已有值；嵌套段（`binding`）逐键补全。凭据留空占位，
        读取时环境变量（`BYRDOCS_TOKEN` / `BYRDOCS_USERNAME` / `BYRDOCS_PASSWORD`）
        优先。
        """
        return {
            "data_dir": "./plugins/byrdocs/data",
            "username": "",
            "password": "",
            "token": "",
            "timeout": 60,
            "login_cooldown": 60,
            "max_file_mb": DEFAULT_MAX_FILE_MB,
            "cache_mb": DEFAULT_CACHE_MB,
            "max_per_page": DEFAULT_PAGE_SIZE,
            "binding": {"groups": [], "users": []},
        }

    def _default_data_dir(self) -> str:
        return str(Path(__file__).resolve().parent / "data")

    def _resolve_data_dir(self) -> str:
        raw = str(self.config.get("data_dir", "") or "").strip()
        if not raw:
            return self._default_data_dir()
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(__file__).resolve().parent.parent.parent / path
        return str(path)

    def _int_cfg(self, key: str, default: int, *, minimum: int = 0) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default
        return max(minimum, value)

    def _page_size(self) -> int:
        return min(20, max(1, self._int_cfg("max_per_page", DEFAULT_PAGE_SIZE, minimum=1)))

    def _max_file_bytes(self) -> int:
        mb = self._int_cfg("max_file_mb", DEFAULT_MAX_FILE_MB, minimum=0)
        return mb * 1024 * 1024

    def _cache_bytes(self) -> int:
        mb = self._int_cfg("cache_mb", DEFAULT_CACHE_MB, minimum=0)
        return mb * 1024 * 1024

    # ------------------------------------------------------------------ 服务范围（白名单）

    @staticmethod
    def _to_ids(value) -> set[int]:
        """配置里的「号列表」→ 正整数集合（`bool` 会被 `int()` 收成 1，丢弃）。"""
        if not isinstance(value, (list, tuple, set)):
            return set()
        out: set[int] = set()
        for entry in value:
            if isinstance(entry, bool):
                continue
            try:
                num = int(entry)
            except (TypeError, ValueError):
                continue
            if num > 0:
                out.add(num)
        return out

    def _binding(self) -> dict:
        value = self.config.get("binding")
        return value if isinstance(value, dict) else {}

    def _bound_groups(self) -> set[int]:
        """允许使用本插件的群号 —— **全部来自本插件自己的配置**。

        ⚠️ 这里**故意不**回落到框架的 `bound_groups`。两者回答的是不同问题：
        框架那份是「机器人监听哪些群」（为空 = 监听所有群），本插件这份是
        「哪些群允许用 byrdocs」（为空 = **一个都不允许**）——语义正好相反，
        拿监听列表兜底等于「机器人进了多少群，这个插件就在多少群里可用」，
        与「白名单模式」自相矛盾。没显式配置就是没有：要用哪个群，就
        `/byrdocs add group <群号>` 显式加进来。
        """
        return self._to_ids(self._binding().get("groups"))

    def _bound_users(self) -> set[int]:
        return self._to_ids(self._binding().get("users"))

    def check_access(self, event) -> bool:
        """这条消息的调用者是否在本插件的服务范围内（与权限等级无关）。

        | 上下文 | 放行条件 |
        |--------|----------|
        | 群聊 | 群号 ∈ `binding.groups` |
        | 群小窗（群聊临时会话） | 来源群 ∈ `binding.groups`（**不看** QQ 号：群里能用，该群成员的小窗就能用） |
        | 直接私聊 | QQ 号 ∈ `binding.users` |

        **两张表都不配 = 哪里都不可用**（只有超级用户豁免）。没有任何继承或兜底：
        框架的 `bound_groups` 只是「监听哪些群」，与「允许哪些群用本插件」是两件事。
        """
        if event is None:
            return True                      # 无上下文（帮助渲染 / 直接调用）
        try:
            user_id = int(event.user_id or 0)
        except (TypeError, ValueError):
            return False
        if self.perm.is_superuser(user_id):
            return True                      # 豁免：防止白名单把自己锁在外面
        if getattr(event, "message_type", "") != "private":
            source = self._to_ids([getattr(event, "group_id", None)])
            return bool(source) and source <= self._bound_groups()
        if getattr(event, "is_temp_session", False):
            # 用 `_to_ids` 而不是 `int(...)`：字段缺失/写成非数字时不该抛异常
            # （注意 `set() <= 白名单` 恒为真，所以不能拿空集合去比）
            source = self._to_ids([getattr(event, "group_id", None)])
            return bool(source) and source <= self._bound_groups()
        return user_id in self._bound_users()

    _SCOPE_LOG_INTERVAL = 60.0

    def _log_scope_denied(self, event) -> None:
        if event is None:
            return
        try:
            user_id = int(event.user_id or 0)
        except (TypeError, ValueError):
            return
        now = time.time()
        last, count = self._scope_denied.get(user_id, (0.0, 0))
        if now - last < self._SCOPE_LOG_INTERVAL:
            self._scope_denied[user_id] = (last, count + 1)
            return
        if len(self._scope_denied) >= 256:
            self._scope_denied.clear()
        self._scope_denied[user_id] = (now, 0)
        source = (f"，来源群 {event.group_id}"
                  if getattr(event, "is_temp_session", False) else "")
        self.logger.info(
            "[byrdocs] 私聊命令被服务范围拒绝：用户 %s%s（近 %.0fs 内 %d 次；"
            "白名单 = plugins.byrdocs.binding）",
            user_id, source, self._SCOPE_LOG_INTERVAL, count)

    def _install_visibility_filter(self) -> None:
        """让 `/help` 与执行同口径：范围外的人看不到本命名空间。"""
        cmds = self.cmds
        if not hasattr(cmds, "add_visibility_filter"):
            return
        if self._visibility_filter is not None:
            cmds.remove_visibility_filter(self._visibility_filter)

        def _visible(handler, event) -> bool:
            if getattr(handler, "parent_name", "") != "byrdocs" \
                    and getattr(handler, "name", "") != "byrdocs":
                return True
            return self.check_access(event)

        self._visibility_filter = _visible
        cmds.add_visibility_filter(_visible)

    # ------------------------------------------------------------------ 命令注册

    # ------------------------------------------------------------------ 技能（给 AI 用）
    def _register_skills(self) -> None:
        """把「搜资料」这条**命令**挂给 ai_chat 的技能通道（见 `napcat.skills`）。

        技能 = 命令映射：参数 schema 与说明都从 `/byrdocs search` 自己取，
        触发时走 `CommandManager.handle()` —— 权限、作用域、白名单与用户手敲
        完全一致（下载/白名单这些仍有副作用的动作只留在命令里，不做成技能）。
        """
        self.register_skill("search", command="byrdocs.search")

    def _register_commands(self) -> None:
        self._command_names = []

        def reg(name: str, **kw):
            self._command_names.append(name)
            return self.register_command(name, **kw)

        # 白名单守卫：包在**每一条**命令（含子命令）外面。
        # 两个坑（bupt_news 实测踩过，照抄结论）：
        # 1. 包装器必须自己声明 `packet` / `params` —— 框架按**参数名**注入，而
        #    `functools.wraps` 会把 `__signature__` 指回原函数，于是包装器多写的
        #    参数形同不存在（`cmd_list(params)` 连 event 都拿不到 → 守卫静默失效）。
        # 2. `packet` / `params` 被包装器自己的形参接住，**不会**出现在 `**kwargs`
        #    里，转发时要按原函数签名手工放回。
        def _guarded(func):
            wanted = tuple(inspect.signature(func).parameters)

            @functools.wraps(func)
            async def wrapper(packet=None, params=None, **kwargs):
                event = getattr(packet, "event", None)
                if not self.check_access(event):
                    self._log_scope_denied(event)
                    return None
                forward = {k: v for k, v in kwargs.items() if k in wanted}
                if "packet" in wanted:
                    forward["packet"] = packet
                if "params" in wanted and params is not None:
                    forward["params"] = params
                if "event" in wanted and event is not None:
                    forward["event"] = event
                return await func(**forward)

            wrapper.__signature__ = inspect.Signature([
                inspect.Parameter("packet", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                  default=None),
                inspect.Parameter("params", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                  default=None)])
            return wrapper

        original_register = self.register_command

        def _register_with_guard(name, **kw):
            decorator = original_register(name, **kw)

            def _wrap(func):
                return decorator(_guarded(func))
            return _wrap

        self.register_command = _register_with_guard
        self._install_visibility_filter()

        @reg("byrdocs", permission="", scope="all", description="BYR Docs 资料检索与下载")
        async def cmd_root(packet):
            # ⚠️ 命令组本体**不会被执行**：框架在路由前就把裸调（`/byrdocs`）
            # 重定向到 `/help byrdocs`（本体描述 + 你有权使用的子命令 + 群里的
            # @机器人 提示）。所以这里不写逻辑 —— 写了也不会运行。
            return None

        # ---- 权限矩阵（改这里之前先看 docs/plugins/byrdocs/04-allowlist.md）----
        #   公开（群聊 + 私聊 + 小窗，仍受服务范围白名单约束）：search / next / get
        #   仅私聊 + 仅超级用户：targets / add / del / status
        # 声明在**每条子命令自己身上**：框架在路由到子命令前按其声明校验
        # （scope 不符或权限不够都回「命令不存在」，不泄露命令存在）。
        # 插件内部**不再**另写一套判定，避免与框架口径漂移。
        @reg("byrdocs.search", permission="", scope="all", schema=SearchSchema,
             description="搜索资料并编号（关键词 / 类型 / 页码）")
        async def cmd_search(params, event):
            return await self._cmd_search(params, event)

        @reg("byrdocs.next", permission="", scope="all", schema=NextSchema,
             description="翻到下一页（沿用上次搜索的关键词）")
        async def cmd_next(params, event):
            return await self._cmd_next(params, event)

        @reg("byrdocs.get", permission="", scope="all", schema=GetSchema,
             description="按编号下载资料并以文件形式发送")
        async def cmd_get(params, event):
            return await self._cmd_get(params, event)

        @reg("byrdocs.targets", permission="superuser", scope="private",
             schema=TargetsSchema, description="查看白名单（可用本插件的群与用户）")
        async def cmd_targets(params, event):
            return await self._cmd_targets(params, event)

        @reg("byrdocs.add", permission="superuser", scope="private",
             schema=TargetSchema, description="把群 / 用户加入白名单")
        async def cmd_add(params, event):
            return await self._cmd_add(params, event)

        @reg("byrdocs.del", permission="superuser", scope="private",
             schema=TargetSchema, description="把群 / 用户移出白名单")
        async def cmd_del(params, event):
            return await self._cmd_del(params, event)

        @reg("byrdocs.status", permission="superuser", scope="private",
             description="登录 / 缓存 / 白名单状态")
        async def cmd_status(packet):
            return await self._cmd_status()

    # ------------------------------------------------------------------ 会话与渲染

    @staticmethod
    def _chat_key(event) -> Optional[tuple]:
        """搜索结果按会话隔离：群聊按群、**群小窗沿用来源群**、好友私聊按用户。

        小窗为什么跟着群走：用户在群里看到编号后顺手点开小窗发 `/byrdocs get 3`，
        这是同一个语境；若按 QQ 号另起一份会话，编号就对不上（「找不到编号 3」）。
        搜索结果是公开信息（谁搜都一样），同一群的小窗共用一份不会串味。
        """
        if event is None:
            return None
        if getattr(event, "message_type", "") == "group" and event.group_id:
            return ("group", int(event.group_id))
        if getattr(event, "is_temp_session", False) and event.group_id:
            return ("group", int(event.group_id))
        try:
            return ("private", int(event.user_id or 0))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _scope_label(chat: tuple) -> str:
        return f"群 {chat[1]}" if chat and chat[0] == "group" else f"用户 {chat[1]}"

    async def _reply(self, event, text) -> None:
        """插件自己发消息（命令返回 None 时框架不会自动回复）。"""
        if isinstance(text, str):
            chain = MessageChain().text(text)
        elif isinstance(text, MessageChain):
            chain = text
        else:
            chain = text
        try:
            if getattr(event, "message_type", "") == "group":
                await self.api.send_group_msg(group_id=str(event.group_id), message=chain)
            else:
                await self.api.send_private_msg(
                    user_id=str(event.user_id), message=chain,
                    group_id=str(event.group_id) if getattr(event, "is_temp_session", False) else None)
        except Exception as e:  # noqa: BLE001 - 回复失败不该把命令处理打挂
            self.logger.warning("[byrdocs] 回复发送失败：%s", e)

    # ------------------------------------------------------------------ 搜索 / 翻页

    async def _cmd_search(self, params, event):
        keyword = " ".join(str(x) for x in (getattr(params, "keyword", None) or [])).strip()
        # 只写了一个类型名（`/byrdocs search book`）：用户多半想按类型浏览，顺手提一句
        note = ""
        if keyword and keyword.lower() in TYPE_ALIAS:
            note = f"（提示：按类型浏览请用 /byrdocs search --type={keyword.lower()}）"
        rtype = self._normalize_type(getattr(params, "type", "all"))
        page = self._as_int(getattr(params, "page", 1), 1)

        client = self._ensure_client()
        size = self._page_size()
        try:
            result = await client.search(keyword, rtype, limit=MAX_RESULTS)
        except SearchError as e:
            self.logger.warning("[byrdocs] 搜索失败：%s", e)
            return f"⚠️ 搜索失败：{e}"
        except ByrdocsError as e:
            return f"⚠️ 搜索失败：{e}"

        if not result.items:
            hint = "换个关键词试试" if keyword else "该类型下暂无结果"
            return (f"🔍 没有找到匹配「{keyword or TYPE_LABEL.get(rtype, rtype)}」的资料\n"
                    f"💡 {hint}；也可用 /byrdocs search <关键词> 检索")

        chat = self._chat_key(event)
        if chat is None:
            return "⚠️ 无法确定会话（拿不到群号 / QQ号），请从群聊或私聊里重试"
        self._store.begin_search(
            chat, keyword=keyword, rtype=rtype, page_size=size,
            items=[item.plain() for item in result.items],
            total=result.total, capped=result.capped)
        search = self._store.current_search(chat)
        return self._render_search(search, page=page, prefix=note)

    async def _cmd_next(self, params, event):
        chat = self._chat_key(event)
        search = self._store.current_search(chat) if chat else None
        if not search:
            return ("🔍 还没有搜索记录\n💡 先搜索：/byrdocs search <关键词>")
        step = self._as_int(getattr(params, "count", 1), 1)
        pages = self._page_count(search)
        current = self._as_int(search.get("page", 1), 1)
        target = min(current + max(1, step), pages)
        if target == current:
            return (f"📄 已经是最后一页（第 {current}/{pages} 页）\n"
                    f"💡 换关键词继续找：/byrdocs search <关键词>")
        self._store.set_page(chat, target)
        return self._render_search(self._store.current_search(chat), page=target)

    @staticmethod
    def _normalize_type(value) -> str:
        text = str(value or "all").strip().lower()
        return TYPE_ALIAS.get(text, "all")

    @staticmethod
    def _as_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _page_count(search: dict) -> int:
        count = int(search.get("count") or 0)
        size = max(1, int(search.get("pageSize") or DEFAULT_PAGE_SIZE))
        return max(1, -(-count // size))

    def _render_search(self, search: Optional[dict], *, page: int,
                       prefix: str = "") -> str:
        if not search:
            return "🔍 搜索记录已失效，请重新搜索：/byrdocs search <关键词>"
        results = search.get("results") or {}
        size = max(1, int(search.get("pageSize") or DEFAULT_PAGE_SIZE))
        pages = self._page_count(search)
        page = min(max(1, page), pages)
        start = (page - 1) * size
        chunk = [results.get(str(n)) for n in range(start + 1, start + size + 1)]
        chunk = [rec for rec in chunk if isinstance(rec, dict)]
        if not chunk:
            return "🔍 这一页没有内容了，请重新搜索：/byrdocs search <关键词>"

        keyword = str(search.get("keyword") or "").strip()
        rtype = str(search.get("type") or "all")
        type_hint = "" if rtype == "all" else f" · {TYPE_LABEL.get(rtype, rtype)}"
        lines = [f"🔍 「{_clip(keyword, 30)}」{type_hint}｜第 {page}/{pages} 页"
                 f"｜共 {int(search.get('count') or 0)} 条"]
        if prefix:
            lines.insert(0, prefix)
        lines.append("")
        for rec in chunk:
            lines.extend(self._format_item(rec))

        footer = [""]
        if page < pages:
            footer.append("➡️ 下一页：/byrdocs next")
        else:
            footer.append("✅ 已经是最后一页")
        first = int(chunk[0].get("n") or 1)
        last = int(chunk[-1].get("n") or first)
        footer.append(f"📥 取文件：/byrdocs get <编号>（本页 {first}–{last}）")
        if search.get("capped"):
            footer.append(f"ℹ️ 结果较多，只编号了前 {MAX_RESULTS} 条 —— 用更精确的关键词缩小范围")
        return "\n".join(lines + footer)

    def _format_item(self, rec: dict) -> list[str]:
        number = int(rec.get("n") or 0)
        rtype = str(rec.get("t") or "")
        data = rec.get("data") or {}
        title = _clip(str(data.get("title") or rec.get("id") or "（无标题）"), 44)
        label = TYPE_LABEL.get(rtype, f"📦 {rtype or '未知'}")
        ext = guess_ext(rec)
        size = format_size(data.get("filesize"))
        meta = " · ".join([part for part in (
            (ext or "").upper(), size,
            " / ".join(str(x) for x in (data.get("content") or [])[:3]),
        ) if part])
        line2 = f"   {label}" + (f" · {meta}" if meta else "")
        detail = self._item_detail(rec)
        out = [f"{number:>2}. {title}", line2]
        if detail:
            out.append(f"   {detail}")
        return out

    @staticmethod
    def _item_detail(rec: dict) -> str:
        rtype = str(rec.get("t") or "")
        data = rec.get("data") or {}
        parts: list[str] = []
        if rtype == "book":
            authors = data.get("authors") or []
            if authors:
                parts.append(" / ".join(str(a) for a in authors[:3]))
            publisher = str(data.get("publisher") or "").strip()
            year = str(data.get("publish_year") or "").strip()
            tail = " ".join(part for part in (publisher, year) if part)
            if tail:
                parts.append(tail)
        elif rtype == "test":
            courses = data.get("course") or []
            names = [str(c.get("name")) for c in courses
                     if isinstance(c, dict) and c.get("name")]
            if names:
                parts.append("课程：" + " / ".join(names[:2]))
            timing = data.get("time") or {}
            when = " ".join(str(x) for x in (timing.get("start"), timing.get("stage"))
                            if x) if isinstance(timing, dict) else ""
            if when:
                parts.append(when)
        elif rtype == "doc":
            courses = data.get("course") or []
            names = [str(c.get("name")) for c in courses
                     if isinstance(c, dict) and c.get("name")]
            if names:
                parts.append("课程：" + " / ".join(names[:2]))
        return _clip(" ｜ ".join(parts), 60)

    # ------------------------------------------------------------------ 取文件

    def _ensure_client(self) -> ByrdocsClient:
        if self._client is None:
            self._client = ByrdocsClient(
                token=os.environ.get("BYRDOCS_TOKEN") or self.config.get("token", "") or "",
                username=(os.environ.get("BYRDOCS_USERNAME")
                          or self.config.get("username", "") or ""),
                password=(os.environ.get("BYRDOCS_PASSWORD")
                          or self.config.get("password", "") or ""),
                timeout=self._int_cfg("timeout", 60, minimum=5),
                login_cooldown=self._int_cfg("login_cooldown", 60, minimum=0),
                logger=self.logger,
            )
        return self._client

    async def _cmd_get(self, params, event):
        chat = self._chat_key(event)
        token = str(getattr(params, "query", "") or "").strip()
        rec = self._store.resolve(chat, token) if chat else None
        if rec is None and is_md5(token):
            rec = {"id": token.lower(), "t": "", "url": "", "data": {}, "n": 0}
        if rec is None:
            return (f"⚠️ 找不到编号 {token} 对应的资料\n"
                    f"💡 编号来自最近一次搜索，先搜索再用编号取：/byrdocs search <关键词>"
                    f"（也可以直接给 32 位文件 MD5）")
        if not rec.get("url") and not rec.get("id"):
            return f"⚠️ 编号 {token} 的记录缺少下载地址，请重新搜索"

        item = {
            "id": rec.get("id") or "",
            "url": rec.get("url") or self._file_url(rec),
            "t": rec.get("t") or "",
            "data": rec.get("data") or {},
        }
        if not item["url"]:
            return f"⚠️ 编号 {token} 的记录缺少下载地址，请重新搜索"

        await self._reply(event, f"📥 正在下载「{_clip(str(item['data'].get('title') or item['id']), 30)}」…")
        try:
            path, name, size = await self._fetch_file(item)
        except TooLarge as e:
            return f"⚠️ 文件过大，未下载：{e}\n💡 可在 byrdocs.org 自行下载，或调大 plugins.byrdocs.max_file_mb"
        except AuthRequired:
            return ("🔐 该文件需要登录才能下载\n"
                    "💡 在 config.json 的 plugins.byrdocs 里配置 username/password"
                    "（北邮账号，站点代登录统一认证），或让机器人跑在校园网内")
        except AuthFailed as e:
            return f"🔐 登录 byrdocs 失败：{e}\n💡 检查 plugins.byrdocs.username/password"
        except (DownloadError, ByrdocsError) as e:
            self.logger.warning("[byrdocs] 下载失败 %s：%s", item["id"], e)
            return f"⚠️ 下载失败：{e}"

        ok = await self._send_file(event, path, name)
        if not ok:
            return f"⚠️ 文件已下载（{name}，{format_size(size)}）但发送失败，见日志"
        return None

    def _file_url(self, rec: dict) -> str:
        """没有原始 URL（例如 `get <md5>`）时按站点规则拼一个。"""
        file_hash = str(rec.get("id") or "")
        if is_md5(file_hash):
            return f"https://byrdocs.org/files/{file_hash}.{guess_ext(rec)}"
        return ""

    async def _fetch_file(self, item: dict) -> tuple[str, str, int]:
        """下载（带缓存）→ `(本地路径, 发送用文件名, 字节数)`。"""
        file_hash = str(item.get("id") or "").lower()
        name = filename_of(item)
        cached = self._store.known_download(file_hash) if is_md5(file_hash) else None
        if cached:
            self._store.touch_download(file_hash)
            return cached["path"], cached.get("name") or name, int(cached.get("size") or 0)

        client = self._ensure_client()
        dest = os.path.join(self._store.download_dir_ready,
                            f"{file_hash or 'byrdocs-' + str(int(time.time()))}.{guess_ext(item)}")
        try:
            info = await client.download(item["url"], dest, file_hash=file_hash,
                                         max_bytes=self._max_file_bytes())
        except AuthRequired:
            # 令牌可能过期：有账号密码就强制换一张再试一次
            if not client.has_credentials:
                raise
            self.logger.info("[byrdocs] 下载需要登录，尝试重新登录后重试")
            await client.ensure_token(force=True)
            info = await client.download(item["url"], dest, file_hash=file_hash,
                                         max_bytes=self._max_file_bytes())
        if is_md5(file_hash):
            self._store.register_download(
                file_hash, path=info["path"], name=name,
                title=str((item.get("data") or {}).get("title") or ""),
                url=item.get("url") or "")
        self._prune_cache()
        return info["path"], name, int(info.get("size") or 0)

    def _prune_cache(self) -> None:
        limit = self._cache_bytes()
        if limit <= 0:
            return
        try:
            result = self._store.prune_downloads(limit)
        except Exception as e:  # noqa: BLE001 - 清理失败不该影响取文件
            self.logger.warning("[byrdocs] 清理下载缓存失败：%s", e)
            return
        if result.get("removed") or result.get("orphans"):
            self.logger.info("[byrdocs] 缓存清理：删除 %d 个文件 + %d 个孤儿，释放 %s",
                             result["removed"], result["orphans"],
                             format_size(result["freed"]))

    async def _send_file(self, event, path: str, name: str) -> bool:
        """把本地文件**以文件形式**发到消息来的地方（群 / 好友 / 群小窗）。"""
        upload_name = self._safe_send_name(name, path)
        try:
            if getattr(event, "message_type", "") == "group":
                await self.api.upload_group_file(
                    group_id=str(event.group_id), file=path, name=upload_name)
                return True
            # NapCat 的 go-cqhttp 兼容 action（框架 ApiClient 未封装，直接 _call）。
            # 源码：NapCatQQ/packages/napcat-onebot/action/go-cqhttp/UploadPrivateFile.ts
            # —— `user_id` / `file` / `name` / `upload_file`（默认 true）；
            # 好友走 C2C、非好友走 TEMPC2CFROMGROUP（群小窗正是这条），无需我们分叉。
            await self.api._call("upload_private_file", {
                "user_id": str(event.user_id), "file": path, "name": upload_name,
                "upload_file": True})
            return True
        except Exception as e:  # noqa: BLE001 - 换一条路径再试
            self.logger.warning("[byrdocs] 上传文件失败（改用文件消息段）：%s", e)
        try:
            await self._reply(event, MessageChain().file(path, name=upload_name))
            return True
        except Exception as e:  # noqa: BLE001
            self.logger.error("[byrdocs] 发送文件失败：%s", e)
            return False

    @staticmethod
    def _safe_send_name(name: str, path: str) -> str:
        """发送用的文件名：QQ 客户端靠后缀决定怎么打开，所以必须带后缀。"""
        candidate = (name or "").strip() or os.path.basename(path)
        if not os.path.splitext(candidate)[1]:
            candidate = f"{candidate}{os.path.splitext(path)[1] or '.pdf'}"
        return candidate

    # ------------------------------------------------------------------ 白名单管理

    async def _cmd_targets(self, params, event) -> str:
        spec = join_spec(getattr(params, "spec", ""))
        if spec:
            parsed = parse_target_ctx(spec, is_group=getattr(event, "message_type", "") == "group")
            if parsed is None:
                return ("_schema_error",
                        "目标格式不对（应写成 group <群号> 或 user <QQ号>）")
            kind, target = parsed
            pool = self._bound_groups() if kind == "group" else self._bound_users()
            mark = "✅ 在白名单里" if target in pool else "❌ 不在白名单里"
            what = "群" if kind == "group" else "用户"
            return f"🔎 {what} {target}：{mark}"
        return self._render_targets(event)

    def _render_targets(self, event=None) -> str:
        groups = sorted(self._bound_groups())
        users = sorted(self._bound_users())
        lines = ["📋 BYR Docs 服务范围白名单", ""]
        lines.append(f"👥 可用群聊（{len(groups)}）")
        lines.append("   " + ("、".join(str(g) for g in groups) if groups else "（空 —— 群里不可用）"))
        lines.append(f"🙋 可用私聊（{len(users)}）")
        lines.append("   " + ("、".join(str(u) for u in users) if users else "（空 —— 私聊不可用）"))
        lines.append("")
        lines.append("✏️ 增删：/byrdocs add|del <group 群号|user QQ号>（仅本私聊可用）")
        lines.append("   纯数字按「群」理解，也可显式写 group / user 前缀")
        lines.append("   ⚠️ 不配置 = 哪里都不可用（框架的 bound_groups 不参与判定）")
        me = ""
        if event is not None:
            chat = self._chat_key(event)
            if chat is not None:
                me = f"\n📍 当前会话：{self._scope_label(chat)}"
        return "\n".join(lines) + me

    async def _cmd_add(self, params, event):
        # 资格由框架按 `permission="superuser"` + `scope="private"` 校验（见注册处）
        spec = join_spec(getattr(params, "spec", ""))
        if not spec:
            # 可变参数不参与框架的 required 校验（`[spec ...]` 在帮助里就是可选的），
            # 所以缺参数在这里拦。**不要手写用法行**（会与 Schema 漂移）：
            # 返回 `_schema_error` 让框架回「⚠️ 用法不正确 + /help byrdocs.add」。
            return ("_schema_error", "没给目标（应写成 group <群号> 或 user <QQ号>）")
        parsed = parse_target_ctx(spec,
                                  is_group=getattr(event, "message_type", "") == "group")
        if parsed is None:
            return ("_schema_error", "目标格式不对（应写成 group <群号> 或 user <QQ号>）")
        kind, target = parsed
        return self._mutate_binding(kind, target, add=True)

    async def _cmd_del(self, params, event):
        # 资格由框架校验（同上）
        spec = join_spec(getattr(params, "spec", ""))
        if not spec:
            return ("_schema_error", "没给目标（应写成 group <群号> 或 user <QQ号>）")
        parsed = parse_target_ctx(spec,
                                  is_group=getattr(event, "message_type", "") == "group")
        if parsed is None:
            return ("_schema_error", "目标格式不对（应写成 group <群号> 或 user <QQ号>）")
        kind, target = parsed
        return self._mutate_binding(kind, target, add=False)

    def _mutate_binding(self, kind: str, target: int, *, add: bool) -> str:
        key = "groups" if kind == "group" else "users"
        binding = dict(self._binding())
        current = self._to_ids(binding.get(key))
        what = "群" if kind == "group" else "用户"
        if add:
            if target in current:
                return f"ℹ️ {what} {target} 已经在白名单里了\n\n" + self._render_targets()
            current.add(target)
        else:
            if target not in current:
                return f"ℹ️ {what} {target} 本来就不在白名单里\n\n" + self._render_targets()
            current.discard(target)
        binding[key] = sorted(current)
        self.config.set("binding", binding)          # 自动写回 config.json
        head = f"✅ 已{'加入' if add else '移出'}白名单：{what} {target}"
        return head + "\n\n" + self._render_targets()

    # ------------------------------------------------------------------ 状态

    async def _cmd_status(self) -> str:
        client = self._ensure_client()
        await self.refresh_campus_state()
        stats = self._store.stats()
        try:
            import napcat as _napcat
            fw_version = _napcat.__version__
        except Exception:  # noqa: BLE001
            fw_version = "?"
        age = client.token_age()
        if getattr(client, "static_token", ""):
            token_state = "静态令牌（配置 / 环境变量）"
        elif client.token and age >= 0:
            token_state = f"已登录 {age / 60:.0f} 分钟前"
        else:
            token_state = "未登录"
        username = str(getattr(client, "_username", "") or "")
        mask = getattr(client, "_mask", None)
        lines = [
            "📚 BYR Docs 插件状态",
            "",
            "🔐 凭据",
            f"  账号：{mask(username) if (username and callable(mask)) else (username or '（未配置）')}",
            f"  令牌：{token_state}",
            f"  登录次数：{getattr(client, 'login_count', 0)}",
        ]
        if getattr(client, "last_error", ""):
            lines.append(f"  上次错误：{_clip(client.last_error, 60)}")
        lines += [
            "",
            "🌐 出口 / 登录检查",
            type(self)._campus_state,
            "",
            "📋 白名单",
            f"  群：{len(self._bound_groups())} 个",
            f"  用户：{len(self._bound_users())} 个",
            "  （未配置 = 都不可用；仅本插件自己的 binding，不看框架 bound_groups）",
            "",
            "💾 下载缓存",
            f"  文件：{stats['files']} 个 / {format_size(stats['bytes']) or '0 B'}"
            f"（上限 {self._int_cfg('cache_mb', DEFAULT_CACHE_MB, minimum=0)} MB）",
            f"  目录：{self._store.download_dir}",
            "",
            f"🧩 框架版本：{fw_version}",
        ]
        return "\n".join(lines)

    #: `status` 里那行「出口 / 登录检查」的缓存（避免每次看状态都发一次请求）。
    _campus_state = "  未检查"
    _campus_checked_at = 0.0
    _CAMPUS_TTL = 300.0

    async def refresh_campus_state(self, *, force: bool = False) -> None:
        """查 `/api/ip` 判断当前出口是不是校园网（决定能否免登录下载）。"""
        fresh = time.time() - type(self)._campus_checked_at < self._CAMPUS_TTL
        if fresh and not force:
            return
        client = self._ensure_client()
        try:
            data = await client.fetch_json("https://byrdocs.org/api/ip")
        except Exception as e:  # noqa: BLE001 - 纯诊断信息
            type(self)._campus_state = f"  检查失败：{_clip(str(e), 60)}"
            return
        if not isinstance(data, dict):
            type(self)._campus_state = "  检查失败：响应结构异常"
            return
        ip = str(data.get("ip") or "未知")
        bupt = bool(data.get("bupt"))
        type(self)._campus_state = (
            f"  出口 IP：{ip}\n"
            f"  校园网：{'✅ 是（可免登录下载）' if bupt else '❌ 否（需要登录令牌）'}")
        type(self)._campus_checked_at = time.time()


def _load_plugin():
    """供 `PluginManager` 之外的自检脚本使用（框架自己按类名发现插件）。"""
    return ByrdocsPlugin
