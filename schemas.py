"""byrdocs 命令参数 Schema。

约定与框架一致（见 docs/plugin-dev.md 4.3.1）：用法行**不手写**，由 Schema 生成；
帮助里的占位符由框架的 `build_usage()` / `PLACEHOLDER_LEGEND` 统一渲染。
"""
from __future__ import annotations

import re
from typing import Optional

from napcat.command import CommandSchema, IntField, StrField, VariadicField


class SearchSchema(CommandSchema):
    """`/byrdocs search <关键词 ...> [--type=book] [--page=1]`"""

    # ⚠️ keyword 必须是**可变参数**：中文搜索词天然带空格（"高等数学 期末"），
    # 若按普通位置参数，第二个词会被当成 `type`（再下一个当 `page`）→ 报
    # 「page: 必须为整数」这种让人摸不着头脑的错。收成列表再拼回去，
    # 于是 `/byrdocs search 数据结构 期末` 就是一次正常的搜索；
    # 类型与页码走键值写法（`--type=test` / `--page=2`），它们本来就该显式给。
    keyword = VariadicField(description="搜索关键词（书名 / 课程 / 作者 / 13 位 ISBN；"
                                        "可写多个词；留空 = 返回全部）")
    type = StrField(default="all", max_length=12,
                    description="资源类型：all / book / doc / test（也可写 --type=test）")
    page = IntField(default=1, min=1, max=100,
                    description="页码（一般用 /byrdocs next 翻页）")


class NextSchema(CommandSchema):
    """`/byrdocs next [页数]`"""

    count = IntField(default=1, min=1, max=20, description="往后翻几页，默认 1")


class GetSchema(CommandSchema):
    """`/byrdocs get <编号|md5>`"""

    query = StrField(required=True, min_length=1, max_length=64,
                     description="搜索结果的编号，或 32 位文件 MD5")


class TargetSchema(CommandSchema):
    """`/byrdocs add <group 群号|user QQ号>`"""

    # ⚠️ 必须用 VariadicField：用户很自然会写 `/byrdocs add group 123456`（两个位置
    # 参数），普通 StrField 只吃第一个（"group"）→ 解析失败 → 报「格式不对」。
    # 可变参数把剩余位置参数**原样收成列表**，再由 `parse_target_ctx` 拼起来解析，
    # 这样 `add group 123` / `add 123` / `add user 123` 三种写法都能work。
    spec = VariadicField(description="绑定目标：`group 群号` 或 `user QQ号`（也可只给号）")


class TargetsSchema(CommandSchema):
    """`/byrdocs targets [group 群号|user QQ号]`"""

    spec = VariadicField(description="留空 = 列出全部；给了目标 = 只看这一条")


_TARGET_RE = re.compile(r"^(group|user|qq|群|群聊|用户|私聊)\s*[:：]?\s*(\d{4,12})$")


def join_spec(spec) -> str:
    """可变参数（list）或普通字符串 → 用空格拼成一串，交给 `parse_target`。"""
    if isinstance(spec, (list, tuple)):
        return " ".join(str(x) for x in spec).strip()
    return str(spec or "").strip()


def parse_target(spec) -> Optional[tuple]:
    """把用户输入解析成 `("group"|"user", id)`。

    接受：`group 123456` / `群 123456` / `user 123456` / `@123456` / `[@123456]`
    / `<@123456>` / 纯数字（**语义交给调用方按上下文猜**，见 `parse_target_ctx`）。
    """
    text = join_spec(spec)
    if not text:
        return None
    text = text.replace("，", ",").strip()
    match = _TARGET_RE.match(text)
    if match:
        kind = "group" if match.group(1) in ("group", "群", "群聊") else "user"
        return kind, int(match.group(2))
    # `[@123]` / `<@123>` / `@123`：QQ 的 @ 段被拍平后的形态
    at = re.fullmatch(r"[\[<（(]?\s*@\s*!?(\d{4,12})\s*[\])）>]?", text)
    if at:
        return "user", int(at.group(1))
    if text.isdigit() and 4 <= len(text) <= 12:
        return None, int(text)          # 只给数字：类型未知，交给上下文
    return None


def parse_target_ctx(spec: str, *, is_group: bool) -> Optional[tuple]:
    """带上下文的解析：纯数字时，群里按「用户」、私聊里按「群」理解。

    理由：命令的默认读者就是发消息的人 —— 群里发 `/byrdocs add 10000001` 想的是
    「把这个人加进白名单」，私聊里发 `/byrdocs add 1000000001` 想的是「把这个群
    加进来」。想要另一种就显式写 `group` / `user` 前缀。
    """
    parsed = parse_target(spec)
    if parsed is None:
        return None
    kind, value = parsed
    if kind:
        return kind, value
    return ("user", value) if is_group else ("group", value)
