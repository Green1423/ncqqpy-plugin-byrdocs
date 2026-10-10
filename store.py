"""byrdocs 插件的本地状态：搜索结果会话 + 下载文件登记。

拆成「会话索引」（`state.json`）与「文件本身」（`downloads/` 目录）两部分：

* 搜索结果只是**短编号 → 资源**的映射（编号是给用户看的，不是 byrdocs 的 ID），
  全量重写一个几十 KB 的 JSON 没有负担；
* 文件可能上百 MB，绝不可能塞进 JSON —— 只登记 `{hash: {path, size, mtime}}`，
  磁盘配额与淘汰按这份登记表算（见 `prune_downloads`）。

编号（`n`）在**一次搜索**内是稳定的：`search` 会一次取回整份结果（byrdocs 搜索
API 单次最多 100 条）并按 1..N 编号，翻页只是切窗口，所以「第 2 页的 6 号」
和「第 1 页的 1 号」指向的资源永远不会错位。下一次 `search` 换关键词时，
该会话的编号重新从 1 开始。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

STATE_VERSION = 1

#: 单次搜索最多保留多少条结果（受 byrdocs 搜索 API 的 limit 上限 100 约束）。
MAX_RESULTS = 100

#: 每个聊天最多保留多少条历史搜索（防止长期运行下 state.json 无限膨胀）。
MAX_SEARCHES_PER_CHAT = 5


def _now() -> float:
    return time.time()


def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


class ResultStore:
    """搜索会话 + 下载文件的持久化。

    用法::

        store = ResultStore("plugins/byrdocs/data/state.json")
        sid = store.begin_search(("group", 123456), keyword="高数", rtype="book",
                                 page_size=5, items=items, total=12, capped=False)
        item = store.resolve(("group", 123456), "3")
    """

    def __init__(self, state_path: str, logger=None) -> None:
        self.state_path = state_path
        self.download_dir = os.path.join(os.path.dirname(state_path), "downloads")
        self.logger = logger
        self._data: dict = {
            "version": STATE_VERSION,
            "updatedAt": None,
            "chats": {},        # {"group:123": {"searches": {...}, "last": sid}}
            "downloads": {},    # {hash: {"path", "size", "mtime", "name", "title"}}
        }
        self.load()

    # ------------------------------------------------------------------ 磁盘

    def load(self) -> None:
        raw = _load_json(self.state_path)
        if not raw:
            return
        chats = raw.get("chats")
        if isinstance(chats, dict):
            self._data["chats"] = chats
        downloads = raw.get("downloads")
        if isinstance(downloads, dict):
            # 只保留仍然存在的文件：外部清理过 downloads/ 时登记表不该继续骗人
            self._data["downloads"] = {
                k: v for k, v in downloads.items()
                if isinstance(v, dict) and os.path.exists(str(v.get("path") or ""))
            }
        if raw.get("updatedAt"):
            self._data["updatedAt"] = raw["updatedAt"]

    def save(self) -> None:
        d = os.path.dirname(self.state_path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._data["updatedAt"] = _now()
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, self.state_path)

    @property
    def download_dir_ready(self) -> str:
        os.makedirs(self.download_dir, exist_ok=True)
        return self.download_dir

    # ------------------------------------------------------------------ 搜索会话

    def _chat(self, chat: tuple, create: bool = False) -> Optional[dict]:
        key = f"{chat[0]}:{chat[1]}"
        chats = self._data["chats"]
        node = chats.get(key)
        if not isinstance(node, dict):
            if not create:
                return None
            node = {"searches": {}, "last": None, "updatedAt": _now()}
            chats[key] = node
        if not isinstance(node.get("searches"), dict):
            node["searches"] = {}
        return node

    def begin_search(self, chat: tuple, *, keyword: str, rtype: str, page_size: int,
                     items: list[dict], total: int, capped: bool) -> str:
        """开一次新搜索：编号 1..N 重新分配，返回搜索 ID。"""
        node = self._chat(chat, create=True)
        sid = f"s{int(_now() * 1000)}"
        results: dict[str, dict] = {}
        for i, item in enumerate(items[:MAX_RESULTS], start=1):
            rec = dict(item)
            rec["n"] = i
            # 保留 `_page`（它属于第几页）属于会话级信息，单独存，避免和条目混在一起
            results[str(i)] = rec
        node["searches"][sid] = {
            "keyword": keyword,
            "type": rtype,
            "pageSize": max(1, int(page_size)),
            "page": 1,
            "total": int(total),
            "count": len(results),
            "capped": bool(capped),
            "at": _now(),
            "results": results,
        }
        node["last"] = sid
        node["updatedAt"] = _now()
        self._trim_searches(node, keep=sid)
        self.save()
        return sid

    def _trim_searches(self, node: dict, keep: str) -> None:
        """只保留最近 `MAX_SEARCHES_PER_CHAT` 次搜索（含当前这次）。"""
        searches = node.get("searches") or {}
        if len(searches) <= MAX_SEARCHES_PER_CHAT:
            return
        ordered = sorted(searches.items(),
                         key=lambda kv: float((kv[1] or {}).get("at") or 0))
        for sid, _ in ordered[:-MAX_SEARCHES_PER_CHAT]:
            if sid != keep:
                searches.pop(sid, None)

    def current_search(self, chat: tuple) -> Optional[dict]:
        """该聊天最近一次搜索（`next` / `get` 默认看的会话）。"""
        node = self._chat(chat)
        if not node:
            return None
        sid = node.get("last")
        search = (node.get("searches") or {}).get(sid)
        return search if isinstance(search, dict) else None

    def current_id(self, chat: tuple) -> Optional[str]:
        node = self._chat(chat)
        return (node or {}).get("last")

    def set_page(self, chat: tuple, page: int) -> Optional[dict]:
        """更新当前搜索的页码（`next` 用），返回更新后的搜索。"""
        node = self._chat(chat)
        if not node:
            return None
        search = (node.get("searches") or {}).get(node.get("last"))
        if not isinstance(search, dict):
            return None
        search["page"] = max(1, int(page))
        node["updatedAt"] = _now()
        self.save()
        return search

    def resolve(self, chat: tuple, token: str) -> Optional[dict]:
        """把用户输入的编号解析成资源条目。

        只认**当前搜索**里的编号 —— 旧搜索的编号已经作废（否则「上次的 12 号」
        和「这次的 12 号」会指向两个不同资源，用户拿到的文件与预期不符）。
        32 位十六进制串按 byrdocs 的文件 MD5 直接放行（`get <md5>`）。
        """
        token = (token or "").strip()
        if len(token) == 32 and all(c in "0123456789abcdefABCDEF" for c in token):
            return {"id": token.lower(), "t": "", "url": "", "data": {},
                    "n": 0, "byHash": True}
        if not token.isdigit():
            return None
        search = self.current_search(chat)
        if not search:
            return None
        rec = (search.get("results") or {}).get(str(int(token)))
        return dict(rec) if isinstance(rec, dict) else None

    # ------------------------------------------------------------------ 下载登记

    def known_download(self, file_hash: str) -> Optional[dict]:
        rec = self._data["downloads"].get((file_hash or "").lower())
        if not isinstance(rec, dict):
            return None
        path = str(rec.get("path") or "")
        if path and os.path.exists(path):
            rec = dict(rec)
            rec["size"] = os.path.getsize(path)
            return rec
        self._data["downloads"].pop(file_hash, None)
        return None

    def register_download(self, file_hash: str, *, path: str, name: str,
                          title: str = "", url: str = "") -> dict:
        rec = {
            "path": path,
            "name": name,
            "title": title or name,
            "url": url,
            "size": os.path.getsize(path) if os.path.exists(path) else 0,
            "mtime": _now(),
        }
        self._data["downloads"][(file_hash or "").lower()] = rec
        self.save()
        return rec

    def touch_download(self, file_hash: str) -> None:
        """记录一次使用（淘汰时先删最久没用过的）。"""
        rec = self._data["downloads"].get((file_hash or "").lower())
        if isinstance(rec, dict):
            rec["mtime"] = _now()
            self.save()

    def downloads(self) -> dict:
        return dict(self._data["downloads"])

    def stats(self) -> dict:
        recs = self._data["downloads"]
        total = 0
        for rec in recs.values():
            try:
                total += int(rec.get("size") or 0)
            except (TypeError, ValueError):
                pass
        chats = self._data["chats"]
        return {
            "files": len(recs),
            "bytes": total,
            "chats": len(chats),
            "updatedAt": self._data.get("updatedAt"),
        }

    def prune_downloads(self, max_bytes: int) -> dict:
        """把 `downloads/` 的总占用压到 `max_bytes` 以内（LRU：最久未用的先删）。

        另外顺手清掉下载目录里**没有登记**的孤儿文件（上次写到一半被杀、
        或手工放进来的），否则配额永远算不准。
        """
        result = {"removed": 0, "freed": 0, "orphans": 0}
        recs = self._data["downloads"]
        known = {str(rec.get("path") or "") for rec in recs.values()}
        try:
            entries = os.listdir(self.download_dir)
        except OSError:
            entries = []
        for entry in entries:
            path = os.path.join(self.download_dir, entry)
            if not os.path.isfile(path) or path in known:
                continue
            try:
                size = os.path.getsize(path)
                os.remove(path)
            except OSError:
                continue
            result["orphans"] += 1
            result["freed"] += size

        max_bytes = max(0, int(max_bytes))
        ordered = sorted(recs.items(), key=lambda kv: float((kv[1] or {}).get("mtime") or 0))
        total = sum(int((rec or {}).get("size") or 0) for _, rec in ordered)
        for file_hash, rec in ordered:
            if total <= max_bytes:
                break
            path = str((rec or {}).get("path") or "")
            try:
                size = os.path.getsize(path)
            except OSError:
                size = int((rec or {}).get("size") or 0)
            try:
                os.remove(path)
            except OSError:
                pass
            recs.pop(file_hash, None)
            total -= size
            result["removed"] += 1
            result["freed"] += size
        if result["removed"] or result["orphans"]:
            self.save()
        return result

    def purge(self, chat: Optional[tuple] = None) -> int:
        """清搜索会话（`chat=None` 清全部），返回清掉的会话数。"""
        chats = self._data["chats"]
        if chat is None:
            n = len(chats)
            chats.clear()
        else:
            key = f"{chat[0]}:{chat[1]}"
            n = 1 if chats.pop(key, None) is not None else 0
        if n:
            self.save()
        return n
