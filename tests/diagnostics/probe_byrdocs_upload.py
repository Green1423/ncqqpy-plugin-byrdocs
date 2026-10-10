#!/usr/bin/env python
"""诊断探针：byrdocs 插件「下载 → 以文件形式私聊发送」的真实链路。

覆盖三件单测覆盖不到的事：
1. 真实账号登录 byrdocs 并带令牌下载（`POST /api/auth/bupt_login`）；
2. NapCat 的 `upload_private_file` action 名与参数是否真的可用
   （框架 `ApiClient` 没有封装这个 action，插件直接走 `_call`）；
3. 发送后的响应结构。

用法（先停 bot，避免两个连接抢 WS 响应；探针只发一条私聊）：

    venv-arm/bin/python -B tests/diagnostics/probe_byrdocs_upload.py <QQ号>

**必须显式给出目标 QQ**（不设默认值：默认值会让别人一跑就把消息发给一个陌生人）。
**会真的发一条私聊消息**（一个真实文件）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from napcat import NapCatClient                                     # noqa: E402
from plugins.byrdocs.byrdocs_api import (                           # noqa: E402
    ByrdocsClient, filename_of, format_size, guess_ext,
)

SMALL = {
    "id": "cbdf08cbbecffb096c220bc15f477d53",
    "t": "doc",
    "data": {"title": "网安C++高级语言程序设计代码", "filetype": "zip", "filesize": 54124},
    "url": "",
}


async def main() -> int:
    if len(sys.argv) < 2 or not str(sys.argv[1]).strip().isdigit():
        print("用法: probe_byrdocs_upload.py <QQ号>（会真的发一条私聊）", file=sys.stderr)
        return 2
    target = str(sys.argv[1]).strip()
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    plugin_cfg = (cfg.get("plugins") or {}).get("byrdocs") or {}
    out_dir = ROOT / "tests" / ".test-tmp" / "byrdocs-probe"
    out_dir.mkdir(parents=True, exist_ok=True)

    client = ByrdocsClient(
        username=plugin_cfg.get("username", ""),
        password=plugin_cfg.get("password", ""),
        token=plugin_cfg.get("token", ""),
        timeout=60,
        logger=None,
    )
    item = dict(SMALL)
    item["url"] = f"https://byrdocs.org/files/{item['id']}.{guess_ext(item)}"
    dest = out_dir / f"{item['id']}.{guess_ext(item)}"
    print(f"[1] 下载 {item['data']['title']} …")
    info = await client.download(item["url"], str(dest), file_hash=item["id"])
    print(f"    → {info['size']} 字节（{format_size(info['size'])}）"
          f" content-type={info['content_type']}")
    head = dest.read_bytes()[:4]
    print(f"    文件头：{head!r}")
    if not info["size"]:
        print("    ✗ 空文件")
        return 1
    await client.close()

    print(f"[2] 通过 NapCat 发私聊文件给 {target} …")
    napcat = NapCatClient()
    await napcat.start()
    try:
        name = filename_of(item)
        print(f"    action=upload_private_file name={name!r} file={str(dest)}")
        try:
            result = await napcat.api._call("upload_private_file", {
                "user_id": target, "file": str(dest), "name": name})
            print(f"    ✓ upload_private_file 返回：{result}")
        except Exception as e:  # noqa: BLE001
            print(f"    ✗ upload_private_file 失败：{type(e).__name__}: {e}")
            print("    退回 file 消息段再试 …")
            from napcat import MessageChain
            await napcat.api.send_private_msg(
                user_id=target, message=MessageChain().file(str(dest), name=name))
            print("    ✓ file 消息段已发送")
    finally:
        await napcat.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
