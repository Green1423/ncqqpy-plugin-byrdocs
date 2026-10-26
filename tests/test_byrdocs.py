#!/usr/bin/env python
"""byrdocs 插件自检脚本。

默认**离线**运行：网络（搜索 / 登录 / 下载）全部用桩件替换，覆盖命令链路、
编号与翻页、白名单闸门、缓存与登录重试。加 `--online` 再跑一遍真实网络：

    venv-arm/bin/python -B tests/test_byrdocs.py
    BYRDOCS_PASSWORD=xxx venv-arm/bin/python -B tests/test_byrdocs.py --online

从仓库根执行（脚本用 `__file__` 定位仓库根，从哪跑都一样）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP_DIR = ROOT / "tests" / ".test-tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)   # 新 clone 里没有它（.gitignore 排除）

# ---- 插件包位置：两种仓库布局都要能跑 --------------------------------------
# 框架仓库布局 = <repo>/plugins/byrdocs/（直接可用）；
# 独立插件仓库布局 = 仓库根就是包目录，而且 clone 出来的目录名未必等于插件名。
# 后者搭一个「包农场」<tmp>/pkgfarm/plugins/byrdocs -> 插件包目录，让
# `import plugins.byrdocs.*` 与 `plugin_dir=<农场>/plugins` 两种口径都成立。
PLUGIN_NAME = "byrdocs"
PLUGIN_DIR = ROOT / "plugins" / PLUGIN_NAME
PLUGIN_ROOT_DIR = ROOT / "plugins"
if not (PLUGIN_DIR / "__init__.py").exists() and (ROOT / "__init__.py").exists():
    import tempfile
    PLUGIN_ROOT_DIR = (Path(tempfile.mkdtemp(prefix=f"pkgfarm-{PLUGIN_NAME}-"))
                       / "plugins")
    PLUGIN_ROOT_DIR.mkdir(parents=True, exist_ok=True)
    if not (PLUGIN_ROOT_DIR / PLUGIN_NAME).exists():
        (PLUGIN_ROOT_DIR / PLUGIN_NAME).symlink_to(ROOT, target_is_directory=True)
    sys.path.insert(0, str(PLUGIN_ROOT_DIR.parent))
    PLUGIN_DIR = PLUGIN_ROOT_DIR / PLUGIN_NAME
PLUGIN_PKG = f"plugins.{PLUGIN_NAME}"

from napcat.config import ConfigManager
from napcat.client import GroupBinding
from napcat.message import MessageChain
from napcat.models import MessageEvent
from napcat.client import NapCatClient                                 # noqa: E402
from napcat.command import CommandManager                              # noqa: E402
from napcat.events import EventBus, TimerManager                       # noqa: E402
from napcat.file_manager import FileManager                            # noqa: E402
from napcat.permission import PermissionManager                        # noqa: E402
from napcat.plugin import PluginManager                                # noqa: E402

SUPERUSER = 10000001
ADMIN = 10001
MEMBER = 10002
STRANGER = 10999
GROUP_OK = 1000000001
GROUP_OTHER = 100000002
BOUND_USER = 20001
TMP = ROOT / "tests" / ".test-tmp" / "byrdocs"

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    if cond:
        PASS.append(name)
        print(f"  \033[32mPASS\033[0m {name}")
    else:
        FAIL.append(name)
        print(f"  \033[31mFAIL\033[0m {name}" + (f"  <- {detail}" if detail else ""))
    return bool(cond)


def pmod(name: str):
    """取框架实际加载的插件子模块（相对导入只有框架的模块名下才成立）。"""
    module = sys.modules.get(f"napcat_plugin_byrdocs.{name}")
    if module is None:
        raise RuntimeError(f"插件子模块 {name} 未加载 —— 先 mgr.load_all()")
    return module


def flat(result) -> str:
    """把命令返回值（str / MessageChain / 消息段列表）拍成纯文本。"""
    if isinstance(result, (list, tuple)):
        if result and all(hasattr(x, "build") for x in result):
            return "".join(flat(x) for x in result)
        return "".join(s.get("data", {}).get("text", "")
                       for s in result if isinstance(s, dict))
    built = result.build() if hasattr(result, "build") else result
    return flat(built) if isinstance(built, list) else str(built or "")


# ---------------------------------------------------------------- 夹具

RAW_ITEMS = [
    {"type": "book", "id": "a" * 32,
     "url": "https://byrdocs.org/files/" + "a" * 32 + ".pdf?filename=%E9%AB%98%E7%AD%89%E6%95%B0%E5%AD%A6.pdf&f=1",
     "data": {"title": "高等数学（上）", "authors": ["北邮高数组"], "publisher": "北邮出版社",
              "publish_year": "2017", "filetype": "pdf", "filesize": 7058882}},
    {"type": "book", "id": "b" * 32,
     "url": "https://byrdocs.org/files/" + "b" * 32 + ".pdf",
     "data": {"title": "高等数学（下）", "authors": ["北邮高数组"], "filetype": "pdf",
              "filesize": 66443829}},
    {"type": "doc", "id": "c" * 32,
     "url": "https://byrdocs.org/files/" + "c" * 32 + ".zip",
     "data": {"title": "高数题库", "filetype": "zip", "filesize": 1024,
              "course": [{"name": "高等数学"}], "content": ["题库"]}},
    {"type": "test", "id": "d" * 32,
     "url": "https://byrdocs.org/files/" + "d" * 32 + ".pdf",
     "data": {"title": "2024 秋季高等数学期末试卷", "filetype": "pdf", "filesize": 2048,
              "course": [{"name": "高等数学"}], "content": ["原题", "答案"],
              "time": {"start": "2024", "stage": "期末"}}},
]


class FakeApi:
    """记录发送内容，不联网；`_call` 覆盖 `upload_private_file` 等裸 action。"""

    def __init__(self):
        self.sent: list[tuple] = []
        self.temp_sessions: list[tuple] = []
        self.actions: list[tuple] = []
        self.uploads: list[dict] = []
        self.fail_upload = False

    async def send_group_msg(self, group_id, message, auto_escape=False):
        self.sent.append(("group", str(group_id), message))
        return {"message_id": len(self.sent)}

    async def send_private_msg(self, user_id, message, auto_escape=False, group_id=None):
        self.sent.append(("private", str(user_id), message))
        if group_id:
            self.temp_sessions.append((str(user_id), str(group_id)))
        return {"message_id": len(self.sent)}

    async def upload_group_file(self, group_id, file, name="", folder="",
                                upload_file=False):
        if self.fail_upload:
            raise RuntimeError("模拟上传失败")
        self.uploads.append({"kind": "group", "group_id": str(group_id),
                             "file": file, "name": name})
        return {"file_id": "f1"}

    async def upload_private_file(self, user_id, file, name, upload_file=True):
        if self.fail_upload:
            raise RuntimeError("模拟上传失败")
        self.uploads.append({"kind": "private", "user_id": str(user_id),
                             "file": file, "name": name})
        return {"file_id": "f2"}

    async def _call(self, action, params=None):
        self.actions.append((action, dict(params or {})))
        if action == "upload_private_file":
            if self.fail_upload:
                raise RuntimeError("模拟上传失败")
            self.uploads.append({"kind": "private", **dict(params or {})})
            return {"file_id": "f2"}
        return {}

    async def delete_msg(self, message_id):
        return None

    def texts(self):
        out = []
        for kind, target, msg in self.sent:
            segs = msg.build() if hasattr(msg, "build") else msg
            out.append((kind, target, "".join(
                s.get("data", {}).get("text", "") for s in segs if s.get("type") == "text")))
        return out

    def last_text(self) -> str:
        texts = self.texts()
        return texts[-1][2] if texts else ""


class StubClient:
    """byrdocs 客户端桩件：不联网，可编排搜索结果 / 下载 / 登录行为。"""

    def __init__(self, *, total=None, downloads=None, token="TOKEN",
                 auth_required=False, auth_fail=False, size=1024):
        self.calls: list[tuple] = []
        self.total = total
        self.downloads = downloads or {}
        self._token = token
        self.auth_required = auth_required
        self.auth_fail = auth_fail
        self.size = size
        self.login_count = 0
        self.last_error = ""
        self.bytes = b"%PDF-1.6 STUB BYTES " + b"x" * 32

    # ---- 与真实客户端同形的接口 ----

    @property
    def has_credentials(self) -> bool:
        return True

    @property
    def token(self) -> str:
        return self._token

    def token_age(self) -> float:
        return 12.0 if self._token else -1.0

    def set_static_token(self, token):
        self._token = token

    @staticmethod
    def _mask(username: str) -> str:
        return (username or "")[:4] + "***"

    async def close(self):
        return None

    async def fetch_json(self, url):
        return {"ip": "10.0.0.1", "bupt": False}

    async def ensure_token(self, force: bool = False):
        self.calls.append(("login", force))
        if self.auth_fail:
            raise pmod("byrdocs_api").AuthFailed("用户名或密码错误")
        return self._token

    async def search(self, keyword="", rtype="all", limit=20, timeout=None):
        api_mod = pmod("byrdocs_api")
        SearchItem, SearchResult = api_mod.SearchItem, api_mod.SearchResult
        MAX_API_LIMIT = api_mod.MAX_API_LIMIT
        self.calls.append(("search", keyword, rtype, limit))
        items = [SearchItem.parse(raw) for raw in RAW_ITEMS]
        items = [i for i in items if i is not None]
        if rtype != "all":
            items = [i for i in items if i.type == rtype]
        total = self.total if self.total is not None else len(items)
        return SearchResult(total=total, items=items, capped=total > MAX_API_LIMIT)

    async def download(self, url, dest, *, file_hash="", max_bytes=0, use_token=True):
        api_mod = pmod("byrdocs_api")
        AuthRequired, DownloadError, TooLarge = (api_mod.AuthRequired,
                                                 api_mod.DownloadError, api_mod.TooLarge)
        self.calls.append(("download", file_hash))
        if self.auth_required or self.auth_fail:
            # auth_fail：登录失败，且匿名下载同样被拒（模拟真实站点：需要登录才能下）
            raise AuthRequired("需要登录")
        if max_bytes and self.size > max_bytes:
            raise TooLarge("文件太大")
        payload = self.downloads.get(file_hash, self.bytes)
        if payload is None:
            raise DownloadError("文件不存在（404）")
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(payload)
        return {"path": dest, "size": len(payload), "content_type": "application/pdf"}


class StubReply(NapCatClient):
    """只借用框架的 `_reply_command`，用来观察「命令返回值怎么发出去」。"""

    def __init__(self, api):
        self.api = api
        self.logger = logging.getLogger("test.byrdocs.reply")


def build_harness(cfg_path: Path, api, data_dir: Path, groups=()):
    cfg = ConfigManager(str(cfg_path))
    cfg.set("plugins.byrdocs.data_dir", str(data_dir))
    # 白名单里显式列群：框架的 `all_groups=True` 时 `bound_groups` 是空表
    # （空 = 监听所有群，语义与白名单相反），兜底路径在夹具里取不到东西。
    cfg.set("plugins.byrdocs.binding.groups", list(groups))
    cfg.set("plugins.byrdocs.binding.users", [])
    cfg.set("plugins.byrdocs.max_per_page", 2)      # 4 条结果 → 2 页，方便测翻页
    cfg.set("plugins.byrdocs.username", "2026000000")
    cfg.set("plugins.byrdocs.password", "pw")
    bus = EventBus()
    timer = TimerManager()
    timer.start()
    perm = PermissionManager(superusers=[SUPERUSER])
    perm.set_group_role(GROUP_OK, ADMIN, "admin")
    perm.set_group_role(GROUP_OK, MEMBER, "member")
    group_binding = GroupBinding(bound=set(), all_groups=True)
    cmds = CommandManager(prefixes=["/", "#"], perm_manager=perm,
                          groups_ref=group_binding, cfg_ref=cfg,
                          event_bus_ref=bus, timer_ref=timer)
    files = FileManager(api)
    mgr = PluginManager(plugin_dir=str(PLUGIN_ROOT_DIR), api=api, event_bus=bus,
                        timer=timer, cmd_manager=cmds, perm_manager=perm,
                        file_manager=files, cfg=cfg, groups=group_binding)
    cmds._plugins_ref = mgr
    return mgr, cmds


def ev(text: str, *, group: int = None, user: int = SUPERUSER,
       temp_group: int = None) -> MessageEvent:
    """构造一个消息事件（group / private / 群小窗）。"""
    if group is not None:
        return MessageEvent(message_type="group", user_id=user, group_id=group,
                            message=[{"type": "text", "data": {"text": text}}],
                            raw_message=text)
    return MessageEvent(
        message_type="private", sub_type="group" if temp_group else None,
        user_id=user, group_id=temp_group,
        message=[{"type": "text", "data": {"text": text}}], raw_message=text)


async def send(cmds, stub: StubReply, event: MessageEvent):
    """走一遍完整链路：框架分发 → 框架回复。返回命令返回值。"""
    result = await cmds.handle(event)
    await stub._reply_command(event, result)
    return result


# ---------------------------------------------------------------- 测试

async def main(online: bool) -> int:
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)
    data_dir = TMP / "data"

    api = FakeApi()
    mgr, cmds = build_harness(TMP / "config.json", api, data_dir, groups=[GROUP_OK])
    stub = StubReply(api)

    print("\n[1] 加载与配置补齐")
    await mgr.load_all()
    await mgr.enable_all()
    plugin = mgr.get("byrdocs")
    if not check("插件被发现并加载", plugin is not None, "PluginManager 未找到 byrdocs"):
        return 1
    check("name/version 正确", plugin.name == "byrdocs" and plugin.version == "1.0.0")
    seeded = plugin.config.all()
    for key in ("username", "password", "token", "data_dir", "cache_mb",
                "max_file_mb", "max_per_page", "binding", "timeout"):
        check(f"配置模板补齐 {key}", key in seeded, str(sorted(seeded)))
    check("data_dir 生效（不碰仓库里的真实数据）",
          str(data_dir) in str(plugin._store.state_path), str(plugin._store.state_path))
    subs = cmds.list_commands()["byrdocs"].subcommands
    check("命令注册齐全",
          all(n in subs for n in ("search", "next", "get", "add", "del",
                                  "targets", "status")),
          str(sorted(subs)))
    check("子命令权限口径正确（查询公开 / 管理仅超管私聊）",
          all((subs[n].permission, subs[n].scope) == ("", "all")
              for n in ("search", "next", "get"))
          and all((subs[n].permission, subs[n].scope) == ("superuser", "private")
                  for n in ("targets", "add", "del", "status")),
          str({n: (h.permission, h.scope) for n, h in subs.items()}))

    plugin._client = StubClient()

    print("\n[2] 搜索与编号")
    res = await send(cmds, stub, ev("/byrdocs search 高等数学", group=GROUP_OK))
    text = flat(res)
    check("搜索结果带编号 1./2.", " 1. " in text and " 2. " in text, text[:200])
    check("标题出现在结果里", "高等数学（上）" in text, text[:200])
    check("第二页提示 next", "/byrdocs next" in text, text[:200])
    check("分页只显示 max_per_page 条（2 条）",
          text.count(". 高等数学") == 2, text[:400])
    check("取文件提示带页内编号范围", "get <编号>" in text and "1–2" in text, text[-200:])
    check("search 调用带关键词与 limit=100",
          ("search", "高等数学", "all", 100) in plugin._client.calls,
          str(plugin._client.calls))

    res_next = await send(cmds, stub, ev("/byrdocs next", group=GROUP_OK))
    text_next = flat(res_next)
    check("翻页显示第 2/2 页", "第 2/2 页" in text_next, text_next[:120])
    check("第二页编号从 3 开始（跨页稳定）", " 3. " in text_next and " 4. " in text_next,
          text_next[:300])
    check("最后一页不再提示 next", "/byrdocs next" not in text_next, text_next[-200:])
    res_last = await send(cmds, stub, ev("/byrdocs next", group=GROUP_OK))
    check("已在最后一页时给出提示", "已经是最后一页" in flat(res_last), flat(res_last)[:120])

    print("\n[3] get：下载 / 缓存 / 文件发送")
    api.sent.clear()
    api.uploads.clear()
    res_get = await send(cmds, stub, ev("/byrdocs get 3", group=GROUP_OK))
    check("下载前先回执「正在下载」",
          any("正在下载" in t[2] for t in api.texts()), str(api.texts())[:200])
    check("群聊走 upload_group_file", api.uploads
          and api.uploads[-1]["kind"] == "group"
          and api.uploads[-1]["group_id"] == str(GROUP_OK),
          str(api.uploads))
    check("发送的文件名带后缀", api.uploads and api.uploads[-1]["name"].endswith(".zip"),
          str(api.uploads[-1]["name"]) if api.uploads else "no upload")
    check("发送的文件名取自 URL 的 filename / 标题",
          api.uploads and api.uploads[-1]["name"] in ("高数题库.zip",),
          str(api.uploads[-1]["name"]) if api.uploads else "no upload")
    check("get 不再重复回复（返回 None）", res_get is None, repr(res_get))
    check("文件真的写到下载目录",
          os.path.isdir(plugin._store.download_dir)
          and len(os.listdir(plugin._store.download_dir)) == 1,
          str(os.listdir(plugin._store.download_dir)))

    before = len([c for c in plugin._client.calls if c[0] == "download"])
    await send(cmds, stub, ev("/byrdocs get 3", group=GROUP_OK))
    after = len([c for c in plugin._client.calls if c[0] == "download"])
    check("同一个文件第二次 get 走缓存（不再下载）", after == before, f"{before} -> {after}")

    api.uploads.clear()
    res_temp = await send(cmds, stub, ev("/byrdocs get 4", temp_group=GROUP_OK,
                                        user=BOUND_USER))
    check("小窗沿用来源群的搜索会话（编号可直接用）", res_temp is None, repr(res_temp))
    check("群小窗走 upload_private_file 且带 user_id",
          api.uploads and api.uploads[-1]["kind"] == "private"
          and api.uploads[-1].get("user_id") == str(BOUND_USER),
          str(api.uploads))

    res_md5 = await send(cmds, stub, ev("/byrdocs get " + "e" * 32, group=GROUP_OK))
    check("get 支持直接给 32 位 MD5", api.uploads
          and api.uploads[-1]["file"].endswith("e" * 32 + ".pdf"),
          str(api.uploads[-1]) if api.uploads else "no upload")

    api.sent.clear()
    res_bad = await send(cmds, stub, ev("/byrdocs get 99", group=GROUP_OK))
    check("越界编号给出友好提示", "找不到编号" in flat(res_bad), flat(res_bad)[:160])
    res_bad2 = await send(cmds, stub, ev("/byrdocs next", group=GROUP_OTHER))
    check("没搜过就 next：提示先搜索", "先搜索" in flat(res_bad2), flat(res_bad2)[:160])

    res_type = await send(cmds, stub,
                          ev("/byrdocs search 高数 --type=doc", group=GROUP_OK))
    check("类型过滤只回 doc（--type=doc）", "📄 资料" in flat(res_type)
          and ("doc",) == (plugin._client.calls[-1][2],),
          f"{flat(res_type)[:120]} calls={plugin._client.calls[-1]}")

    res_multi = await send(cmds, stub,
                           ev("/byrdocs search 高等 数学 期末", group=GROUP_OK))
    check("多词关键词被拼成一个（不被当成 type/page）",
          plugin._client.calls[-1][1] == "高等 数学 期末", str(plugin._client.calls[-1]))

    res_page = await send(cmds, stub,
                          ev("/byrdocs search 高数 --page=2", group=GROUP_OK))
    check("--page=2 直接跳到第 2 页", "第 2/2 页" in flat(res_page), flat(res_page)[:120])

    res_none = await send(cmds, stub, ev("/byrdocs search", group=GROUP_OK))
    check("不带关键词也能浏览（返回结果而非报错）", "🔍" in flat(res_none), flat(res_none)[:120])

    res_kw = await send(cmds, stub, ev("/byrdocs search book", group=GROUP_OK))
    check("关键词写成类型名时提示改用 --type",
          "--type=book" in flat(res_kw), flat(res_kw)[:200])

    print("\n[4] 登录与失败降级")
    api.sent.clear()
    # 先恢复 1..4 的搜索会话：第 3 节末尾的 doc 搜索把它换成了单条结果
    await send(cmds, stub, ev("/byrdocs search 高等数学", group=GROUP_OK))

    plugin._client = StubClient(auth_required=True)
    res_auth = await send(cmds, stub, ev("/byrdocs get 1", group=GROUP_OK))
    check("需要登录时提示配置账号密码", "需要登录" in flat(res_auth)
          and "username/password" in flat(res_auth), flat(res_auth)[:200])
    check("需要登录时会强制重登一次（有账号密码）",
          any(c == ("login", True) for c in plugin._client.calls),
          str(plugin._client.calls))

    plugin._client = StubClient(auth_fail=True)
    res_fail = await send(cmds, stub, ev("/byrdocs get 1", group=GROUP_OK))
    check("登录失败时回显错误原因",
          "登录 byrdocs 失败" in flat(res_fail) and "用户名或密码错误" in flat(res_fail),
          flat(res_fail)[:200])

    plugin._client = StubClient(size=10 * 1024 * 1024)
    plugin.config.set("max_file_mb", 1)
    api.sent.clear()
    res_big = await send(cmds, stub, ev("/byrdocs get 2", group=GROUP_OK))
    check("文件超限时不下载并提示上限",
          "过大" in flat(res_big) and "max_file_mb" in flat(res_big), flat(res_big)[:200])
    check("超限时不会写入缓存",
          plugin._store.known_download("b" * 32) is None)
    plugin.config.set("max_file_mb", 300)
    plugin._client = StubClient()

    print("\n[5] 白名单闸门（check_access）")
    check("白名单内的群 → 放行",
          plugin.check_access(ev("x", group=GROUP_OK, user=MEMBER)) is True)
    check("白名单外的群 → 拒绝（不再无条件放行群聊）",
          plugin.check_access(ev("x", group=GROUP_OTHER, user=MEMBER)) is False)
    check("陌生人直接私聊 → 拒绝", plugin.check_access(ev("x", user=STRANGER)) is False)
    check("陌生人的群小窗（来源群在白名单里）→ 放行",
          plugin.check_access(ev("x", user=STRANGER, temp_group=GROUP_OK)) is True)
    check("白名单外来源群的群小窗 → 拒绝",
          plugin.check_access(ev("x", user=STRANGER, temp_group=GROUP_OTHER)) is False)
    check("superuser 私聊豁免", plugin.check_access(ev("x", user=SUPERUSER)) is True)
    silent = await send(cmds, stub, ev("/byrdocs search 高数", user=STRANGER))
    check("白名单外的私聊命令静默无响应（不暴露命令存在）", silent is None, repr(silent))
    silent_group = await send(cmds, stub, ev("/byrdocs search 高数", group=GROUP_OTHER,
                                             user=MEMBER))
    check("白名单外的群里命令静默无响应", silent_group is None, repr(silent_group))

    # 未显式配置 = 哪里都不可用（框架的 bound_groups 非空也**不**兜底）
    bare_cfg = TMP / "bare-cfg.json"
    bare = ConfigManager(str(bare_cfg))
    bare.set("plugins.byrdocs.data_dir", str(TMP / "bare"))
    bare.set("plugins.byrdocs.binding.groups", [])
    bare.set("plugins.byrdocs.binding.users", [])
    bare_bus, bare_timer = EventBus(), TimerManager(); bare_timer.start()
    bare_perm = PermissionManager(superusers=[SUPERUSER])
    # 框架绑定里**有**这个群：若插件偷看框架的 bound_groups，下面的断言就会翻车
    bare_groups = GroupBinding(bound={GROUP_OK}, all_groups=False)
    bare_cmds = CommandManager(prefixes=["/"], perm_manager=bare_perm,
                              groups_ref=bare_groups, cfg_ref=bare, event_bus_ref=bare_bus,
                              timer_ref=bare_timer)
    bare_mgr = PluginManager(plugin_dir=str(PLUGIN_ROOT_DIR), api=FakeApi(),
                             event_bus=bare_bus, timer=bare_timer, cmd_manager=bare_cmds,
                             perm_manager=bare_perm, file_manager=FileManager(FakeApi()),
                             cfg=bare, groups=bare_groups)
    bare_cmds._plugins_ref = bare_mgr
    await bare_mgr.load("byrdocs")
    await bare_mgr.enable("byrdocs")
    bare_plugin = bare_mgr.get("byrdocs")
    check("未配置 binding 时不绑定任何群（哪怕框架 bound_groups 非空）",
          bare_plugin._bound_groups() == set(), str(sorted(bare_plugin._bound_groups())))
    check("未配置 binding 时群里不可用",
          bare_plugin.check_access(ev("x", group=GROUP_OK, user=MEMBER)) is False)
    check("未配置 binding 时私聊不可用",
          bare_plugin.check_access(ev("x", user=BOUND_USER)) is False)
    check("未配置 binding 时超管仍豁免（否则改不回来）",
          bare_plugin.check_access(ev("x", user=SUPERUSER)) is True)
    await bare_mgr.disable("byrdocs")

    print("\n[6] 权限矩阵 + add / del / targets 管理")

    def sub_h(name):
        return cmds.list_commands()["byrdocs"].subcommands[name]

    check("search / next / get 公开且不限上下文",
          all((sub_h(n).permission, sub_h(n).scope) == ("", "all")
              for n in ("search", "next", "get")),
          str({n: (sub_h(n).permission, sub_h(n).scope)
               for n in ("search", "next", "get")}))
    check("targets / add / del / status 仅私聊 + 仅超级用户",
          all((sub_h(n).permission, sub_h(n).scope) == ("superuser", "private")
              for n in ("targets", "add", "del", "status")),
          str({n: (sub_h(n).permission, sub_h(n).scope)
               for n in ("targets", "add", "del", "status")}))

    # 公开命令：群聊里普通成员可用（群在白名单内）
    api.sent.clear()
    free = await send(cmds, stub, ev("/byrdocs search 高等数学", group=GROUP_OK, user=MEMBER))
    check("群里普通成员能用 search", isinstance(free, str) and "🔍" in flat(free),
          flat(free)[:80])

    # 管理命令：群里一律不可用（连超管都只在私聊用），且不泄露命令存在
    for line, who in ((f"/byrdocs add user {BOUND_USER}", ADMIN),
                      (f"/byrdocs add user {BOUND_USER}", SUPERUSER),
                      ("/byrdocs targets", ADMIN),
                      ("/byrdocs targets", SUPERUSER),
                      (f"/byrdocs del user {BOUND_USER}", SUPERUSER)):
        r = await cmds.handle(ev(line, group=GROUP_OK, user=who))
        hidden = r is None or (isinstance(r, tuple) and r and r[0] == "_not_found")
        check(f"群里不可用（{line} as {who}）", hidden, repr(r)[:80])
    check("群里执行管理命令不会改到白名单",
          BOUND_USER not in plugin._bound_users(), str(sorted(plugin._bound_users())))

    # 管理命令：私聊里非超管同样不可用
    r_stranger = await cmds.handle(ev("/byrdocs targets", user=STRANGER))
    check("私聊非超管不能用 targets",
          r_stranger is None or (isinstance(r_stranger, tuple) and r_stranger[0] == "_not_found"),
          repr(r_stranger)[:80])

    # 超级用户私聊：全部可用
    res_add = await send(cmds, stub, ev(f"/byrdocs add group {GROUP_OTHER}", user=SUPERUSER))
    check("superuser 私聊加群成功", "已加入白名单" in flat(res_add), flat(res_add)[:200])
    check("配置写回 config.json", GROUP_OTHER in plugin._bound_groups(),
          str(sorted(plugin._bound_groups())))
    res_add2 = await send(cmds, stub, ev(f"/byrdocs add group {GROUP_OTHER}", user=SUPERUSER))
    check("重复添加给出「已在」提示", "已经在白名单里" in flat(res_add2), flat(res_add2)[:120])
    res_nospec = await send(cmds, stub, ev("/byrdocs add", user=SUPERUSER))
    # 用法不足现在由**框架**统一回「⚠️ 用法不正确 + 该子命令帮助页」，
    # 插件不再手写用法行（见 plugin-dev §4.3.1 / 命令组帮助重定向规范）
    check("add 不带目标：框架回用法不正确 + 该子命令帮助页",
          "用法不正确" in flat(res_nospec) and "📖 /byrdocs add" in flat(res_nospec),
          flat(res_nospec)[:160])

    res_add_user = await send(cmds, stub, ev(f"/byrdocs add user {BOUND_USER}", user=SUPERUSER))
    check("superuser 私聊加用户成功", "已加入白名单" in flat(res_add_user),
          flat(res_add_user)[:160])
    check("用户白名单立即生效（私聊放行）",
          plugin.check_access(ev("x", user=BOUND_USER)) is True)

    res_targets = await send(cmds, stub, ev("/byrdocs targets", user=SUPERUSER))
    ttext = flat(res_targets)
    check("targets 列出群与用户", str(GROUP_OK) in ttext and str(GROUP_OTHER) in ttext
          and str(BOUND_USER) in ttext, ttext[:400])
    res_one = await send(cmds, stub, ev(f"/byrdocs targets group {GROUP_OTHER}",
                                       user=SUPERUSER))
    check("targets 查询单个目标", "在白名单里" in flat(res_one), flat(res_one)[:120])

    res_del = await send(cmds, stub, ev(f"/byrdocs del user {BOUND_USER}", user=SUPERUSER))
    check("superuser 私聊删用户成功", "已移出白名单" in flat(res_del), flat(res_del)[:160])
    check("删除后私聊立即被拦", plugin.check_access(ev("x", user=BOUND_USER)) is False)
    res_del2 = await send(cmds, stub, ev(f"/byrdocs del {GROUP_OTHER}", user=SUPERUSER))
    check("纯数字在私聊里按「群」理解", f"已移出白名单：群 {GROUP_OTHER}" in flat(res_del2),
          flat(res_del2)[:160])
    check("删群后该群不在绑定里", GROUP_OTHER not in plugin._bound_groups(),
          str(sorted(plugin._bound_groups())))

    print("\n[7] 帮助可见性与命令组本体")
    res_help_root = await send(cmds, stub, ev("/byrdocs", group=GROUP_OK, user=MEMBER))
    htext = flat(res_help_root)
    check("群里裸调命令组只列出公开子命令",
          all(f"/byrdocs {n}" in htext for n in ("search", "next", "get"))
          and not any(f"/byrdocs {n}" in htext
                      for n in ("targets", "add", "del", "status")), htext[:600])
    check("群里附 @机器人 提示", "@" in htext, htext[-200:])
    allowed = await cmds.is_visible(cmds.list_commands()["byrdocs"],
                                    ev("x", group=GROUP_OK))
    blocked = await cmds.is_visible(cmds.list_commands()["byrdocs"],
                                    ev("x", user=STRANGER))
    check("白名单内可见 / 白名单外不可见", allowed is True and blocked is False,
          f"{allowed} {blocked}")
    check("status 仅私聊（群里不出现）", sub_h("status").scope == "private",
          sub_h("status").scope)

    # /help 的分级：群里只列公开子命令，私聊超管能看到全部
    grp_help = flat(await cmds.handle(ev("/byrdocs", group=GROUP_OK, user=MEMBER)))
    check("群里帮助只列 search / next / get",
          all(f"/byrdocs {n}" in grp_help for n in ("search", "next", "get"))
          and not any(f"/byrdocs {n}" in grp_help
                      for n in ("targets", "add", "del", "status")),
          grp_help[:400])
    su_help = flat(await cmds.handle(ev("/byrdocs", user=SUPERUSER)))
    check("超管私聊帮助能看到全部子命令",
          all(f"/byrdocs {n}" in su_help
              for n in ("search", "next", "get", "targets", "add", "del", "status")),
          su_help[:400])
    # 普通用户私聊要看得到公开命令 → 临时放一个非超管进用户白名单
    plugin.config.set("binding", {"groups": sorted(plugin._bound_groups()),
                                  "users": sorted(plugin._bound_users()) + [MEMBER]})
    other_help = flat(await cmds.handle(ev("/byrdocs", user=MEMBER)))
    check("非超管私聊（白名单内）看不到管理命令",
          "/byrdocs search" in other_help
          and not any(f"/byrdocs {n}" in other_help
                      for n in ("targets", "add", "del", "status")),
          other_help[:300])

    print("\n[8] status 与缓存清理")
    res_status = await send(cmds, stub, ev("/byrdocs status", user=SUPERUSER))
    stext = flat(res_status)
    check("status 显示登录 / 白名单 / 缓存", "令牌" in stext and "白名单" in stext
          and "下载缓存" in stext, stext[:300])
    check("status 显示校园网判定", "校园网" in stext, stext[:400])
    plugin.config.set("cache_mb", 0)
    plugin.config.set("cache_mb", 1)
    freed = plugin._store.prune_downloads(1024 * 1024)
    check("缓存淘汰能跑通（不报错）", isinstance(freed, dict), str(freed))

    print("\n[9] 存储层单测（编号 / 翻页 / 求解）")
    ResultStore = pmod("store").ResultStore
    store = ResultStore(str(TMP / "unit-state.json"))
    chat = ("group", 42)
    store.begin_search(chat, keyword="k", rtype="all", page_size=2,
                       items=[{"id": str(i), "t": "book", "url": f"u{i}", "data": {}}
                              for i in range(1, 6)],
                       total=5, capped=False)
    check("编号从 1 连续分配", store.resolve(chat, "5")["id"] == "5",
          str(store.resolve(chat, "5")))
    check("越界编号解析为 None", store.resolve(chat, "6") is None)
    check("MD5 直接解析", store.resolve(chat, "A" * 32)["id"] == "a" * 32)
    check("非数字非 MD5 解析为 None", store.resolve(chat, "abc") is None)
    store.set_page(chat, 2)
    check("翻页后编号仍然稳定", store.resolve(chat, "1")["id"] == "1")
    store.begin_search(chat, keyword="k2", rtype="all", page_size=2,
                       items=[{"id": "x", "t": "book", "url": "u", "data": {}}],
                       total=1, capped=False)
    check("新搜索替换旧编号（旧编号作废）", store.resolve(chat, "5") is None
          and store.resolve(chat, "1")["id"] == "x")
    check("会话按聊天隔离", store.current_search(("group", 43)) is None)
    for i in range(8):
        store.begin_search(chat, keyword=f"k{i}", rtype="all", page_size=2,
                           items=[], total=0, capped=False)
    node = store._chat(chat)
    check("历史搜索数量有上限", len(node["searches"]) <= 5, str(len(node["searches"])))

    print("\n[10] 参数解析（schemas）")
    schemas = pmod("schemas")
    parse_target, parse_target_ctx = schemas.parse_target, schemas.parse_target_ctx
    check("group 前缀", parse_target("group 123456") == ("group", 123456))
    check("中文前缀", parse_target("群 123456") == ("group", 123456))
    check("user 前缀", parse_target("user 10000001") == ("user", 10000001))
    check("@ 形态", parse_target("[@10000001]") == ("user", 10000001))
    check("纯数字类型未知", parse_target("123456") == (None, 123456))
    check("上下文：群里按用户", parse_target_ctx("123456", is_group=True) == ("user", 123456))
    check("上下文：私聊按群", parse_target_ctx("123456", is_group=False) == ("group", 123456))
    check("非法输入", parse_target("hello") is None)

    print("\n[11] 停用清理")
    await mgr.disable("byrdocs")
    check("停用后命令被撤销",
          "byrdocs.search" not in cmds.list_commands().get("byrdocs", type(
              "H", (), {"subcommands": {}})()).subcommands)
    check("停用后白名单过滤器被摘掉", plugin._visibility_filter is None)

    if online:
        await online_checks()

    print(f"\n{'=' * 60}")
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for name in FAIL:
            print(f"  \033[31m✗\033[0m {name}")
        return 1
    print("\033[32m全部通过\033[0m")
    return 0


async def online_checks() -> None:
    """真实网络：搜索 API +（有密码时）登录 + 小文件下载。"""
    ByrdocsClient = pmod("byrdocs_api").ByrdocsClient

    print("\n[online 1] 搜索 API")
    client = ByrdocsClient(timeout=60)
    try:
        result = await client.search("高等数学", "book", limit=5)
        check("在线搜索返回结果", bool(result.items) and result.total >= 1,
              f"total={result.total} items={len(result.items)}")
        if result.items:
            item = result.items[0]
            check("结果含标题 / 大小 / 下载链接",
                  bool(item.title) and item.url.startswith("https://byrdocs.org/files/"),
                  f"{item.title} {item.url}")
    except Exception as e:  # noqa: BLE001
        check("在线搜索返回结果", False, repr(e))

    print("\n[online 2] 出口 IP / 校园网判定")
    try:
        info = await client.fetch_json("https://byrdocs.org/api/ip")
        check("/api/ip 可访问", isinstance(info, dict) and "ip" in info, str(info))
    except Exception as e:  # noqa: BLE001
        check("/api/ip 可访问", False, repr(e))

    password = os.environ.get("BYRDOCS_PASSWORD", "")
    username = os.environ.get("BYRDOCS_USERNAME", "")
    if not (username and password):
        print("  （跳过登录 / 下载：未设置 BYRDOCS_USERNAME / BYRDOCS_PASSWORD）")
        await client.close()
        return

    print("\n[online 3] 账号登录")
    client = ByrdocsClient(username=username, password=password, timeout=60)
    try:
        token = await client.ensure_token()
        check("登录拿到令牌", bool(token) and token.count(".") == 2, token[:20] + "…")
    except Exception as e:  # noqa: BLE001
        check("登录拿到令牌", False, repr(e))
        await client.close()
        return

    print("\n[online 4] 带令牌下载（取第一个结果的前 64KB）")
    try:
        result = await client.search("高等数学", "book", limit=1)
        item = result.items[0] if result.items else None
        check("拿到可下载条目", item is not None)
        if item is not None:
            dest = str(TMP / f"online-{item.id}.{item.ext}")
            info = await client.download(item.url, dest, file_hash=item.id, max_bytes=0)
            with open(dest, "rb") as fh:
                head = fh.read(8)
            check("下载得到真实文件（非 HTML）",
                  info["size"] > 1024 and not head.lstrip().startswith(b"<"),
                  f"{info['size']} bytes head={head!r}")
    except Exception as e:  # noqa: BLE001
        check("下载得到真实文件（非 HTML）", False, repr(e))
    finally:
        await client.close()


if __name__ == "__main__":
    online = "--online" in sys.argv
    sys.exit(asyncio.run(main(online)))
