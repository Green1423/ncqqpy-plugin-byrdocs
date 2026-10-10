"""byrdocs.org 客户端：搜索、下载、账号登录。

三个上游（都写在 https://byrdocs.org/llms.txt 与 https://search.byrdocs.org/llms.txt）：

| 用途 | 端点 | 认证 |
|------|------|------|
| 搜索/检索元数据 | `POST https://search.byrdocs.org/api/search` | 无 |
| 下载文件 | `GET  https://byrdocs.org/files/<md5>.<ext>` | 校园网 IP / 登录 token |
| 账号登录 | `POST https://byrdocs.org/api/auth/bupt_login` | 无（凭据即北邮统一认证） |

登录流程照抄官方 CLI（`byrdocs login`）：

    POST /api/auth/login                  → {tokenURL, loginURL}
    POST /api/auth/bupt_login             → {token}       # 服务端代跑北邮 CAS（含验证码识别）
    GET  /files/<md5>.pdf  Authorization: Bearer <token>  → 文件字节

⚠️ **凭据会发给 byrdocs.org**：该端点由站点运营方代为登录北邮统一认证（CAS），
也就是说账号密码会离开本机、落到第三方服务器上。这是官方 CLI 与网页登录框的
既有流程，不是本插件引入的旁路；不接受这一点就别配账号密码，改用
`token`（手动粘贴登录令牌）或让机器人跑在校园网里（校园网 IP 免登录下载）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp

SITE = "https://byrdocs.org"
SEARCH_API = "https://search.byrdocs.org/api/search"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: 搜索 API 单次返回上限（服务端硬限制）。
MAX_API_LIMIT = 100

CHUNK = 1 << 16          # 64 KiB
_MD5_RE = re.compile(r"^[0-9a-f]{32}$", re.I)


class ByrdocsError(RuntimeError):
    """byrdocs 访问失败的基类。"""


class SearchError(ByrdocsError):
    """搜索接口失败。"""


class AuthRequired(ByrdocsError):
    """需要登录（校园网外 + 无有效令牌）。"""


class AuthFailed(ByrdocsError):
    """登录被拒（账号密码错误 / 触发风控 / 服务端 CAS 失败）。"""


class DownloadError(ByrdocsError):
    """下载失败（网络、配额、文件不存在等）。"""


class TooLarge(ByrdocsError):
    """文件超过配置的大小上限。"""


# ------------------------------------------------------------------ 小工具

def is_md5(value: str) -> bool:
    return bool(_MD5_RE.match((value or "").strip()))


def guess_ext(item: dict) -> str:
    """推断文件后缀：优先数据里的 `filetype`，再看 URL 路径，最后兜底 pdf。"""
    data = item.get("data") or {}
    ftype = str(data.get("filetype") or "").strip().lower()
    if ftype and re.fullmatch(r"[a-z0-9]{1,8}", ftype) and ftype != "wiki":
        return ftype
    path = urlparse(str(item.get("url") or "")).path
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if ext and re.fullmatch(r"[a-z0-9]{1,8}", ext):
        return ext
    return "pdf"


def filename_of(item: dict) -> str:
    """下载时想用的文件名：URL 的 `filename` 参数 → 标题 → md5。"""
    query = parse_qs(urlparse(str(item.get("url") or "")).query)
    for key in ("filename", "name"):
        values = query.get(key) or []
        if values and values[0].strip():
            return _basename(unquote(values[0]))
    title = str((item.get("data") or {}).get("title") or "").strip()
    if title:
        return _basename(f"{title}.{guess_ext(item)}")
    return f"{item.get('id') or 'byrdocs'}.{guess_ext(item)}"


def _basename(path: str) -> str:
    """去掉目录部分与控制字符（`filename` 参数是外部输入，别拿它当路径用）。"""
    name = os.path.basename(str(path).replace("\\", "/")).strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"/\\|?*')
    name = name or "byrdocs"
    stem, ext = os.path.splitext(name)
    if len(stem) > 90:
        stem = stem[:90]
    return stem + ext


def format_size(num: Any) -> str:
    try:
        size = float(num)
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def merge_bearer(value: str) -> str:
    """归一化令牌：允许用户把 `Bearer xxx` 整串粘进配置。"""
    value = (value or "").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value


# ------------------------------------------------------------------ 数据模型

@dataclass
class SearchItem:
    """一条搜索结果（byrdocs 的 item 结构，`data` 随 type 变化）。"""

    type: str
    id: str
    url: str
    data: dict = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any) -> Optional["SearchItem"]:
        if not isinstance(raw, dict) or not raw.get("id"):
            return None
        data = raw.get("data")
        return cls(
            type=str(raw.get("type") or ""),
            id=str(raw.get("id")),
            url=str(raw.get("url") or ""),
            data=data if isinstance(data, dict) else {},
        )

    @classmethod
    def parse_many(cls, raw: Any) -> list["SearchItem"]:
        if not isinstance(raw, list):
            return []
        out: list[SearchItem] = []
        for entry in raw:
            item = cls.parse(entry)
            if item is not None:
                out.append(item)
        return out

    @property
    def title(self) -> str:
        return str(self.data.get("title") or "").strip() or self.id

    @property
    def ext(self) -> str:
        return guess_ext({"data": self.data, "url": self.url})

    @property
    def size(self) -> int:
        try:
            return int(self.data.get("filesize") or 0)
        except (TypeError, ValueError):
            return 0

    def plain(self) -> dict:
        """落库用的普通字典（不含 `n`，编号由 store 分配）。"""
        return {"t": self.type, "id": self.id, "url": self.url, "data": self.data}


@dataclass
class SearchResult:
    """一次搜索的聚合结果。"""

    total: int
    items: list[SearchItem]
    capped: bool = False


# ------------------------------------------------------------------ 客户端

class ByrdocsClient:
    """byrdocs HTTP 客户端（搜索 / 下载 / 登录），可长期复用。

    会话（`aiohttp.ClientSession`）懒创建，`close()` 释放；插件停用时调用。
    """

    def __init__(self, *, token: str = "", username: str = "", password: str = "",
                 timeout: int = 60, logger=None,
                 login_cooldown: float = 60.0,
                 now: Callable[[], float] = time.time) -> None:
        self._static_token = merge_bearer(token)
        self._username = (username or "").strip()
        self._password = password or ""
        self.timeout = max(5, int(timeout or 60))
        self.logger = logger
        self.login_cooldown = max(0.0, float(login_cooldown))
        self._session: Optional[aiohttp.ClientSession] = None
        self._token = self._static_token
        self._token_at = 0.0
        self._login_lock = asyncio.Lock()
        self._next_login_at = 0.0        # 登录失败后的冷却终点
        self.last_error = ""
        self.last_login_at = 0.0
        self.login_count = 0
        self._now = now

    # ---- 生命周期 ----

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ---- 凭据 ----

    @property
    def has_credentials(self) -> bool:
        return bool(self._username and self._password)

    @property
    def token(self) -> str:
        return self._token

    @property
    def static_token(self) -> str:
        """配置里写死的令牌（含 `Bearer ` 前缀时已归一化）。"""
        return self._static_token

    def set_static_token(self, token: str) -> None:
        """换用配置里的静态令牌（登录成功后不再需要账号密码）。"""
        self._static_token = merge_bearer(token)
        self._token = self._static_token
        self._token_at = self._now() if self._token else 0.0

    def token_age(self) -> float:
        return self._now() - self._token_at if self._token_at else -1.0

    # ---- 搜索 ----

    async def search(self, keyword: str = "", rtype: str = "all",
                     limit: int = 20, timeout: Optional[int] = None) -> SearchResult:
        """调用搜索 API。

        `total` 是**过滤后**的结果总数（可能大于 `len(items)`），`capped` 表示
        服务端把结果截到了 `MAX_API_LIMIT`（100）条 —— 调用方据此提示用户细化关键词。
        """
        want = max(1, min(int(limit or 20), MAX_API_LIMIT))
        payload: dict = {"limit": want}
        keyword = (keyword or "").strip()
        if keyword:
            payload["keyword"] = keyword
        if rtype and rtype != "all":
            payload["type"] = rtype
        session = await self._ensure_session()
        try:
            async with session.post(
                SEARCH_API, json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout or self.timeout),
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise SearchError(f"搜索接口返回 HTTP {resp.status}: {text[:200]}")
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as e:
                    raise SearchError(f"搜索接口返回了非 JSON 内容: {e}") from e
        except asyncio.TimeoutError as e:
            raise SearchError("搜索接口超时") from e
        except aiohttp.ClientError as e:
            raise SearchError(f"搜索接口连接失败: {e}") from e

        if not isinstance(data, dict):
            raise SearchError("搜索接口返回结构异常")
        items = SearchItem.parse_many(data.get("results"))
        try:
            total = int(data.get("total") or len(items))
        except (TypeError, ValueError):
            total = len(items)
        capped = total > MAX_API_LIMIT
        if capped:
            total = MAX_API_LIMIT
        return SearchResult(total=total, items=items, capped=capped)

    # ---- 登录 ----

    async def ensure_token(self, *, force: bool = False) -> str:
        """拿到可用于下载的令牌。

        优先级：静态令牌（配置/环境变量）> 缓存令牌 > 账号密码登录。
        `force=True` 用于「403/401 后重登」：有账号密码时强制换新令牌。
        """
        if self._static_token and not force:
            return self._static_token
        if self._token and not force:
            return self._token
        if not self.has_credentials:
            raise AuthRequired("未配置 byrdocs 账号密码（或有效令牌）")
        async with self._login_lock:
            # 等锁期间可能已被别的调用刷新
            if self._token and not force and self._token != self._static_token:
                return self._token
            return await self._login()

    async def _login(self) -> str:
        now = self._now()
        if now < self._next_login_at:
            wait = self._next_login_at - now
            raise AuthFailed(f"登录失败冷却中，请 {wait:.0f}s 后重试")
        session = await self._ensure_session()
        try:
            async with session.post(
                f"{SITE}/api/auth/login",
                timeout=aiohttp.ClientTimeout(total=min(self.timeout, 30)),
            ) as resp:
                if resp.status != 200:
                    raise AuthFailed(f"登录会话创建失败（HTTP {resp.status}）")
                begin = await resp.json(content_type=None)
        except asyncio.TimeoutError as e:
            raise AuthFailed("登录会话创建超时") from e
        except aiohttp.ClientError as e:
            raise AuthFailed(f"无法连接 byrdocs：{e}") from e

        uuid = self._uuid_of(begin)
        if not uuid:
            raise AuthFailed("登录会话响应异常（拿不到 uuid）")

        try:
            async with session.post(
                f"{SITE}/api/auth/bupt_login",
                json={"username": self._username, "password": self._password,
                      "uuid": uuid},
                timeout=aiohttp.ClientTimeout(total=max(self.timeout, 90)),
            ) as resp:
                body = await resp.text()
                status = resp.status
        except asyncio.TimeoutError as e:
            raise AuthFailed("北邮统一认证登录超时（服务端可能繁忙）") from e
        except aiohttp.ClientError as e:
            raise AuthFailed(f"登录请求失败：{e}") from e

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}
        if status == 200 and isinstance(data, dict) and data.get("token"):
            self._token = str(data["token"])
            self._token_at = self._now()
            self.last_login_at = self._token_at
            self.login_count += 1
            self._next_login_at = 0.0
            self.last_error = ""
            if self.logger:
                self.logger.info("[byrdocs] 登录成功（账号 %s）", self._mask(self._username))
            return self._token

        message = ""
        if isinstance(data, dict):
            message = str(data.get("error") or data.get("message") or "")
        message = message or f"HTTP {status}"
        self._next_login_at = self._now() + self.login_cooldown
        self.last_error = message
        if self.logger:
            self.logger.warning("[byrdocs] 登录失败（账号 %s）：%s",
                                self._mask(self._username), message)
        raise AuthFailed(message)

    @staticmethod
    def _uuid_of(begin: Any) -> str:
        if not isinstance(begin, dict):
            return ""
        login_url = str(begin.get("loginURL") or "")
        match = re.search(r"/auth/([0-9a-fA-F-]{36})", login_url)
        return match.group(1) if match else ""

    @staticmethod
    def _mask(username: str) -> str:
        if len(username) <= 4:
            return username[:1] + "***"
        return username[:4] + "***"

    # ---- 下载 ----

    async def download(self, url: str, dest: str, *, file_hash: str = "",
                       max_bytes: int = 0, use_token: bool = True) -> dict:
        """下载到 `dest`（先写 `.part` 再改名，失败不会留下半个文件）。

        返回 `{path, size, content_type}`；`max_bytes` > 0 时超限抛 `TooLarge`。
        401/403 → `AuthRequired`（调用方决定要不要登录后重试）。
        """
        session = await self._ensure_session()
        headers = {"Accept": "*/*"}
        if use_token:
            try:
                token = await self.ensure_token()
            except AuthRequired:
                token = ""
            except AuthFailed as e:
                # 登录失败不该让「校园网内其实能下」的场景也挂掉：先不带令牌试一次
                if self.logger:
                    self.logger.warning("[byrdocs] 登录失败，退回匿名下载：%s", e)
                token = ""
            if token:
                headers["Authorization"] = f"Bearer {token}"

        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        part = dest + ".part"
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
        try:
            async with session.get(url, headers=headers, allow_redirects=False,
                                   timeout=timeout) as resp:
                if resp.status in (401, 403) or 300 <= resp.status < 400:
                    raise AuthRequired("该文件需要登录（或校园网 IP）才能下载")
                if resp.status == 404:
                    raise DownloadError("文件不存在（404）—— 记录可能已下架")
                if resp.status != 200:
                    raise DownloadError(f"下载失败：HTTP {resp.status}")

                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "text/html" in ctype:
                    raise DownloadError("服务端返回了网页而不是文件（可能需要登录）")
                length = 0
                try:
                    length = int(resp.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    length = 0
                if max_bytes and length and length > max_bytes:
                    raise TooLarge(f"文件 {format_size(length)} 超过上限 "
                                   f"{format_size(max_bytes)}")

                written = 0
                with open(part, "wb") as fh:
                    async for chunk in resp.content.iter_chunked(CHUNK):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if max_bytes and written > max_bytes:
                            raise TooLarge(f"文件超过上限 {format_size(max_bytes)}")
                        fh.write(chunk)
            if written == 0:
                raise DownloadError("下载到 0 字节（服务端异常）")
            os.replace(part, dest)
            return {"path": dest, "size": written, "content_type": ctype}
        finally:
            if os.path.exists(part):
                try:
                    os.remove(part)
                except OSError:
                    pass

    async def fetch_json(self, url: str) -> Any:
        """抓一个 JSON（诊断用，例如 `/api/ip` 看当前出口 IP 是否在校园网）。"""
        session = await self._ensure_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            return await resp.json(content_type=None)
