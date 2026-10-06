"""运行时匹配词的 CRUD 命令（用户 2026-10-06 要求：**运行时 CRUD 的 handle 模块**）。

四条命令：

    增加匹配词 <类型> <词>      添加匹配词 / 新增匹配词
    删除匹配词 <id> [id ...]    移除匹配词
    更新匹配词 <id> <类型> <词>  修改匹配词
    查看匹配词                  匹配词列表 / 查看抽奖匹配词

几点设计约定（都有测试钉住）：

- **优先级低于「监听」matcher 且 `block=True`**：CRUD 命令必须**先于**抽奖触发生效。
  否则一旦有人加了个模糊词「匹配词」，发「查看匹配词」就会既列表又抽奖（禁言自己）。
- **权限**：群内 = 群主/管理员 + 超管；私聊 = **仅超管**（`can_manage`）。
- **所有回复都用 htmlkit 渲染成图**（用户要求）；渲染不可用时回退纯文本
  （渲染问题绝不能表现成「命令没反应」）。
- 参数里的**词可以含空格**（只按第一个空白切分类型与词），中文梗常有空格。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from nonebot import get_driver, on_command
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from . import render, store, words
from .render import Row as RenderRow

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from typing import NoReturn

logger = logging.getLogger("nonebot_plugin_lottery")

#: CRUD 命令的优先级。必须 **< 监听 matcher 的优先级**（见模块 docstring）。
CRUD_PRIORITY: Final[int] = 3

#: 群内允许管理匹配词的角色（OneBot V11 的 `sender.role`）。
_ADMIN_ROLES: Final[frozenset[str]] = frozenset({"owner", "admin"})

USAGE_ADD: Final[str] = "增加匹配词 <类型> <词>"
USAGE_DEL: Final[str] = "删除匹配词 <id> [id ...]"
USAGE_UPDATE: Final[str] = "更新匹配词 <id> <类型> <词>"
USAGE_LIST: Final[str] = "查看匹配词"

#: 命令词（单一定义处：注册响应器与插件元数据都引它，避免两处各写一份）
CMD_ADD: Final[str] = "增加匹配词"
CMD_DEL: Final[str] = "删除匹配词"
CMD_UPDATE: Final[str] = "更新匹配词"
CMD_LIST: Final[str] = "查看匹配词"
CMD_NAMES: Final[tuple[str, ...]] = (CMD_ADD, CMD_DEL, CMD_UPDATE, CMD_LIST)

_WARN_ONE_HIT: Final[str] = "命中即随机禁言（1~43200 分钟），别加日常高频词"


# ── 纯函数：权限与参数解析（都好单测，不碰 IO）──────────────────────


def can_manage(
    *,
    is_group: bool,
    role: str | None,
    user_id: str,
    superusers: Collection[str],
) -> bool:
    """能不能增删改匹配词。

    - **超管**（NoneBot 的 `SUPERUSERS`）：任何场景都行，**包括私聊**
    - **群内**：群主 / 管理员
    - **私聊且非超管**：不行（用户明确要求「私聊仅超管」）
    """
    if str(user_id) in {str(u) for u in superusers}:
        return True
    if not is_group:
        return False
    return str(role or "").casefold() in _ADMIN_ROLES


def current_superusers() -> set[str]:
    """NoneBot 全局超管（`SUPERUSERS`）。取不到就当空集（宁可少放行）。"""
    try:
        return {str(u) for u in (get_driver().config.superusers or set())}
    except Exception:
        logger.exception("读取 SUPERUSERS 失败，按无超管处理")
        return set()


def parse_type_word(raw: str) -> tuple[str, str, str | None]:
    """解析 `<类型> <词>`，返回 `(类型key, 词, 错误)`。

    **只按第一个空白切分** —— 词里允许含空格（如「自刎 归天」）。
    """
    text = (raw or "").strip()
    if not text:
        return "", "", "缺少参数"
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return "", "", f"只给了「{parts[0]}」，还需要类型和匹配词"
    match_type = words.normalize_type(parts[0])
    if match_type is None:
        return "", "", f"不认识的类型「{parts[0]}」"
    word = parts[1].strip()
    if not word:
        return "", "", "匹配词不能为空"
    return match_type, word, None


def parse_update_args(raw: str) -> tuple[int, str, str, str | None]:
    """解析 `<id> <类型> <词>`，返回 `(id, 类型key, 词, 错误)`。"""
    text = (raw or "").strip()
    if not text:
        return 0, "", "", "缺少参数"
    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        return 0, "", "", f"参数不足，应为「{USAGE_UPDATE}」"
    try:
        word_id = int(parts[0])
    except ValueError:
        return 0, "", "", f"「{parts[0]}」不是合法的 id（id 见「{USAGE_LIST}」）"
    match_type, word, err = parse_type_word(f"{parts[1]} {parts[2]}")
    if err is not None:
        return 0, "", "", err
    return word_id, match_type, word, None


def parse_ids(raw: str) -> tuple[list[int], str | None]:
    """解析一个或多个 id（空格 / 逗号 / 顿号分隔），返回 `(ids, 错误)`。"""
    text = (raw or "").strip().replace(",", " ").replace("，", " ").replace("、", " ")
    if not text:
        return [], "缺少 id"
    tokens = text.split()
    bad = next((t for t in tokens if not t.isdigit()), None)
    if bad is not None:
        return [], f"「{bad}」不是合法的 id（id 见「{USAGE_LIST}」）"
    return list(dict.fromkeys(int(t) for t in tokens)), None


# ── 发送（一律优先出图，失败回退文本）──────────────────────────────


async def _reply(
    matcher: type[Matcher],
    *,
    title: str,
    lines: Sequence[str] = (),
    rows: Sequence[RenderRow] = (),
    tone: str = "info",
    footer: str | None = None,
) -> NoReturn:
    """回复一张图；渲染不可用时回退纯文本。

    **两种情况都会结束该 matcher**（`finish` 抛 `FinishedException`）。
    """
    png = await render.render_reply(
        title=title, lines=lines, rows=rows, tone=tone, footer=footer
    )
    if png is not None:
        await matcher.finish(MessageSegment.image(png))

    plain = [title, *lines]
    plain += [f"{r.label} {r.badge} {r.text}" for r in rows]
    if footer:
        plain.append(footer)
    await matcher.finish("\n".join(plain))


def _denied_hint(event: MessageEvent) -> str:
    """没有权限时的提示。"""
    if isinstance(event, GroupMessageEvent):
        return "本群只有群主/管理员，或机器人超管可以改抽奖匹配词"
    return "私聊只有机器人超管可以改抽奖匹配词"


def _guard(event: MessageEvent, user_id: str) -> str | None:
    """权限闸门：无权限时返回提示文案，有权限返回 None。"""
    role = getattr(getattr(event, "sender", None), "role", None)
    if can_manage(
        is_group=isinstance(event, GroupMessageEvent),
        role=role,
        user_id=user_id,
        superusers=current_superusers(),
    ):
        return None
    return _denied_hint(event)


def _usage_lines(command: str, extra: str) -> list[str]:
    return [f"用法：{command}", extra]


# ── 四条命令 ────────────────────────────────────────────────────────

add_cmd = on_command(
    CMD_ADD,
    aliases={"添加匹配词", "新增匹配词"},
    priority=CRUD_PRIORITY,
    block=True,
)
del_cmd = on_command(
    CMD_DEL, aliases={"移除匹配词"}, priority=CRUD_PRIORITY, block=True
)
upd_cmd = on_command(
    CMD_UPDATE,
    aliases={"修改匹配词"},
    priority=CRUD_PRIORITY,
    block=True,
)
list_cmd = on_command(
    CMD_LIST,
    aliases={"匹配词列表", "查看抽奖匹配词"},
    priority=CRUD_PRIORITY,
    block=True,
)


@add_cmd.handle()
async def _handle_add(event: MessageEvent, args: Message = CommandArg()) -> None:
    """增加一条匹配词。"""
    if (deny := _guard(event, event.get_user_id())) is not None:
        await _reply(add_cmd, title="没有权限", lines=[deny], tone="error")

    match_type, word, err = parse_type_word(args.extract_plain_text())
    if err is not None:
        await _reply(
            add_cmd,
            title="用法不对",
            lines=_usage_lines(USAGE_ADD, err),
            tone="error",
            footer=f"类型可选：{words.types_help()}",
        )

    if (bad := words.validate_word(match_type, word)) is not None:
        await _reply(add_cmd, title="这条加不了", lines=[bad], tone="error")

    if store.count_words() >= words.MAX_WORDS:
        await _reply(
            add_cmd,
            title="词表满了",
            lines=[f"最多 {words.MAX_WORDS} 条，请先删掉不用的"],
            tone="error",
        )

    new_id = store.add_word(match_type, word, created_by=event.get_user_id())
    if new_id is None:
        exist = store.find_word(match_type, word)
        hint = f"#{exist['id']} 已经就是这条" if exist is not None else "已经存在"
        await _reply(
            add_cmd,
            title="这条已经存在",
            lines=[hint],
            tone="warn",
            footer=f"要改类型或内容用：{USAGE_UPDATE}",
        )

    await _reply(
        add_cmd,
        title="已增加匹配词",
        rows=[RenderRow(f"#{new_id}", words.type_label(match_type), word)],
        tone="success",
        footer=_WARN_ONE_HIT,
    )


@del_cmd.handle()
async def _handle_del(event: MessageEvent, args: Message = CommandArg()) -> None:
    """按 id 删除匹配词（可一次多个）。"""
    if (deny := _guard(event, event.get_user_id())) is not None:
        await _reply(del_cmd, title="没有权限", lines=[deny], tone="error")

    ids, err = parse_ids(args.extract_plain_text())
    if err is not None:
        await _reply(
            del_cmd,
            title="用法不对",
            lines=_usage_lines(USAGE_DEL, err),
            tone="error",
            footer=f"id 见「{USAGE_LIST}」",
        )

    # ⚠️ 必须**先取快照再删**：删完再查 `get_word(i)` 全是 None，
    #    那样「已删除」的回执会一条都列不出来（这个 bug 第一版就写错了）。
    snapshot = {i: store.get_word(i) for i in ids}
    found = [i for i in ids if snapshot[i] is not None]
    missing = [i for i in ids if snapshot[i] is None]
    rows = [
        RenderRow(
            f"#{i}",
            words.type_label(str(snapshot[i]["match_type"])),  # type: ignore[index]
            str(snapshot[i]["word"]),  # type: ignore[index]
        )
        for i in found
    ]

    removed = store.remove_words(ids)
    if removed == 0:
        await _reply(
            del_cmd,
            title="没有删掉任何东西",
            lines=[f"这些 id 都不存在：{' '.join(f'#{i}' for i in missing)}"],
            tone="warn",
            footer=f"id 见「{USAGE_LIST}」",
        )

    skipped = " ".join(f"#{i}" for i in missing)
    footer = f"以下 id 不存在，已跳过：{skipped}" if missing else None
    await _reply(
        del_cmd,
        title=f"已删除 {removed} 条",
        rows=rows,
        tone="success",
        footer=footer,
    )


@upd_cmd.handle()
async def _handle_update(event: MessageEvent, args: Message = CommandArg()) -> None:
    """按 id 更新类型与词。"""
    if (deny := _guard(event, event.get_user_id())) is not None:
        await _reply(upd_cmd, title="没有权限", lines=[deny], tone="error")

    word_id, match_type, word, err = parse_update_args(args.extract_plain_text())
    if err is not None:
        await _reply(
            upd_cmd,
            title="用法不对",
            lines=_usage_lines(USAGE_UPDATE, err),
            tone="error",
            footer=f"类型可选：{words.types_help()}",
        )

    if (bad := words.validate_word(match_type, word)) is not None:
        await _reply(upd_cmd, title="这条改不了", lines=[bad], tone="error")

    before = store.get_word(word_id)
    result = store.update_word(word_id, match_type, word)
    if result == "not_found":
        await _reply(
            upd_cmd,
            title=f"没有 #{word_id}",
            lines=[f"id 见「{USAGE_LIST}」"],
            tone="error",
        )
    if result == "duplicate":
        clash = store.find_word(match_type, word)
        hint = f"#{clash['id']} 已经是这条了" if clash is not None else "与已有记录重复"
        await _reply(
            upd_cmd,
            title="改成这样会重复",
            lines=[hint],
            tone="warn",
            footer=f"要删掉旧的：{USAGE_DEL}",
        )

    old_label = (
        f"{words.type_label(str(before['match_type']))} {before['word']}"
        if before is not None
        else "（原记录已丢失）"
    )
    await _reply(
        upd_cmd,
        title=f"已更新 #{word_id}",
        lines=[f"原：{old_label}"],
        rows=[RenderRow(f"#{word_id}", words.type_label(match_type), word)],
        tone="success",
    )


@list_cmd.handle()
async def _handle_list(event: MessageEvent) -> None:
    """查看全部匹配词（按匹配顺序 = id 升序）。"""
    if (deny := _guard(event, event.get_user_id())) is not None:
        await _reply(list_cmd, title="没有权限", lines=[deny], tone="error")

    rows_all = store.list_words()
    if not rows_all:
        await _reply(
            list_cmd,
            title="还没有任何匹配词",
            lines=["现在发任何消息都不会触发抽奖。"],
            tone="warn",
            footer=f"先加一条：{USAGE_ADD}",
        )

    rows = [
        RenderRow(f"#{r['id']}", words.type_label(str(r["match_type"])), str(r["word"]))
        for r in rows_all
    ]
    await _reply(
        list_cmd,
        title=f"抽奖匹配词（{len(rows)} 条）",
        rows=rows,
        footer=(
            f"匹配顺序 = 从上到下（先命中先生效）\n"
            f"增加：{USAGE_ADD}　删除：{USAGE_DEL}　更新：{USAGE_UPDATE}"
        ),
    )


__all__ = [
    "CMD_ADD",
    "CMD_DEL",
    "CMD_LIST",
    "CMD_NAMES",
    "CMD_UPDATE",
    "CRUD_PRIORITY",
    "USAGE_ADD",
    "USAGE_DEL",
    "USAGE_LIST",
    "USAGE_UPDATE",
    "add_cmd",
    "can_manage",
    "current_superusers",
    "del_cmd",
    "list_cmd",
    "parse_ids",
    "parse_type_word",
    "parse_update_args",
    "upd_cmd",
]
