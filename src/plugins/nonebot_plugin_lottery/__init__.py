"""抽奖（禁言小助手）插件主模块。

抽取自 zhenxun_bot 的 ``zhenxun/plugins/lottery``（作者「阿珏酱」，v1.1），
把真寻内部件（``Config`` / ``PluginExtraData`` / ``MessageUtils`` /
``zhenxun.logger``）换成原生 NoneBot2 等价物，行为保持一致：

    群消息命中**运行时匹配词表**里的任一条 → 在 ``[MIN, MAX]`` 里随机取一个分钟数
    → **禁言自己** → 回复「恭喜 xxx 参与"<命中的词>"！🎉 获得了 N分钟禁言大礼包 🎉」

⚠️ 匹配词**不在配置里、也不在代码里**（2026-10-06 用户拍板）：它们存 sqlite
（`store` 的 `lottery_words` 表），由 `handle` 模块的命令在**运行时**增删改。
所以这里只有一个 catch-all 的 `on_message` 监听 + 自实现的解析（`words`），
而**不是** `on_command` / `on_keyword` —— 后两者的词在导入时就固化进 rule 了。

用户 2026-10-02 追加要求：**「原样但超 10 分钟的定时自动解禁（隐性）」**，
且必须**用 nonebot-plugin-apscheduler 实现并做容灾持久化**。

⚠️ 落地方式是「**到时间再调用 API 解禁**」，**不是**「把禁言时长砍成 10 分钟」
（用户后来明确纠正过这一点）：

    1. 禁言时长 = **抽到的原始值**（例如 480 分钟就真禁 480）—— 不做任何封顶。
    2. 抽到的值 > 阈值时，挂一个 apscheduler 一次性任务，在 +阈值 分钟时
       调 ``set_group_ban(duration=0)`` **主动解禁**（幂等）。

## 容灾（重启不丢解禁任务）

apscheduler 的任务默认只在进程内存里，bot 一重启就没了 —— 被抽到 480 分钟的人
会被真关 8 小时。所以真相落在 sqlite（``store.py`` 的 ``lottery_pending_unmute`` 表），
apscheduler 只当调度器：

    抽奖命中 → 写一条待解禁记录（绝对时刻 unmute_at / expire_at）+ 挂任务
    启动时   → 读表：未到期的重建任务；已过期但还没自然到期的立刻解禁；
               连 expire_at 都过了的（平台早解开）直接清掉，不打扰 API
    解禁成功 → 删记录；失败（bot 离线等）→ 累加 attempts 并在 60 秒后重试

为什么不用 apscheduler 自带的持久化 jobstore（``SQLAlchemyJobStore``）：
它靠 pickle 存函数引用串、恢复时 ``__import__(模块名)``，而宿主用
``load_from_toml()`` 加载 vendor 插件时模块名是带连字符的**合成名**
（``vendor.nonebot-plugin-lottery.src.plugins.nonebot_plugin_lottery``），
不是合法标识符 → 实测恢复必然 ``LookupError: could not import module``。
详见 ``store.py`` 顶部注释。

⚠️ 「隐性」的含义：**回复里报的数字与真实禁言时长一致**（都是抽到的值），
但群里不会被告知「其实 10 分钟后就会被机器人提前放出来」。
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from random import randint
import time
from typing import TYPE_CHECKING, Final

from nonebot import get_bot, get_driver, logger, on_message, require
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.matcher import Matcher
from nonebot.plugin import PluginMetadata
from nonebot.rule import is_type

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

from . import handle, store, words
from .config import plugin_config

if TYPE_CHECKING:
    from collections.abc import Callable


def _usage_text() -> str:
    """用法说明。词表在 DB 里、运行时可变，所以这里只列**管理命令**。"""
    return "\n".join(
        [
            "抽奖（禁言小助手）：命中「匹配词」的人会被随机禁言",
            "（1~43200 分钟；超过阈值的由机器人到点自动解禁）",
            "",
            "匹配词管理（群主 / 管理员 / 超管；私聊仅超管）：",
            f"  {handle.USAGE_LIST}",
            f"  {handle.USAGE_ADD}",
            f"  {handle.USAGE_UPDATE}",
            f"  {handle.USAGE_DEL}",
            "",
            f"类型：{words.types_help()}",
        ]
    )


__plugin_meta__ = PluginMetadata(
    name="抽奖",
    description="参与抽奖的群员会被随机禁言一段时间, 我看看谁这么贱",
    usage=_usage_text(),
    type="application",
    homepage="https://github.com/xinvxueyuan/nonebot-plugin-lottery",
    supported_adapters={"nonebot.adapters.onebot.v11"},
    extra={
        # 原作者（抽取来源）与本仓库维护者分开记，便于回溯
        "author": "阿珏酱",
        "maintainer": "xinvxueyuan",
        "upstream": "zhenxun_bot zhenxun/plugins/lottery",
        "version": "2.0",
        "menu_type": "群内小游戏",
        "commands": [{"command": c} for c in handle.CMD_NAMES],
    },
)

# ── 监听所有群消息（运行时匹配，2026-10-06 用户拍板）───────────────
#
# 为什么不用 `on_command` / `on_keyword`：那两种规则在**导入时**就把词固化进 rule，
# 改词必须重启进程。用户要求「加个 hook 监控所有群消息、自行解析，方便动态调整」，
# 所以改成一个 catch-all matcher + 自实现解析（`words`）+ DB 词表（`store`）。
#
# ⚠️ 三条关键点（每条都有测试钉住）：
#   ① `block=False` **必须**：这是兜底 matcher，`block=True` 会把**所有**群消息吞掉、
#      别的插件再也收不到。只有在**命中**时才用 `matcher.stop_propagation()` 阻断。
#   ② `rule=is_type(GroupMessageEvent)`：只看群消息（用户要求「监控所有群消息」）；
#      私聊消息连规则都不进，零开销。CRUD 命令另注册，私聊也能用。
#   ③ 优先级必须 **大于** `handle.CRUD_PRIORITY`：CRUD 命令靠更小的优先级抢先并阻断。
#      否则一条「查看匹配词」在词表里有模糊词「匹配词」时会**同时**列表 + 抽奖。
lottery_watch = on_message(
    rule=is_type(GroupMessageEvent),
    priority=plugin_config.lottery_match_priority,
    block=False,
)

if plugin_config.lottery_match_priority <= handle.CRUD_PRIORITY:
    logger.warning(
        "LOTTERY_MATCH_PRIORITY({}) <= CRUD 命令优先级({})："
        "CRUD 命令可能被抽奖抢先（发「查看匹配词」会同时把自己禁言）。请调大。",
        plugin_config.lottery_match_priority,
        handle.CRUD_PRIORITY,
    )

_JOB_ID_FMT: Final[str] = "lottery_unmute_{group_id}_{user_id}"

# 解禁失败后的重试间隔与上限。上限存在的意义：bot 长期离线时不要无限重试刷日志；
# 超过之后禁言通常也已自然到期（抽到的时长至少 10 分钟起）。
_RETRY_DELAY_SECONDS: Final[int] = 60
_MAX_UNMUTE_ATTEMPTS: Final[int] = 20
# apscheduler 错过触发时间的容忍窗口（超过就不补跑，交给容灾表下次启动处理）
_MISFIRE_GRACE_SECONDS: Final[int] = 300


def _scheduler_tz() -> tzinfo | None:
    """取 apscheduler 配置的时区（nonebot-plugin-apscheduler 默认 Asia/Shanghai）。

    ⚠️ 为什么需要它：apscheduler 把**naive** 的 ``run_date`` 按它自己配置的时区
    本地化。生产机时区恰好在 2026-10-02 就是 Asia/Shanghai（+0800），所以
    「服务器本地时间」与「调度器时区」一致、看不出问题；但服务器一旦改 UTC
    或有人调整 ``APSCHEDULER_CONFIG``，naive 时间就会被静默挪动 8 小时
    —— 那是「解禁要么早 8 小时、要么晚 8 小时」这种只能靠事后复盘发现的故障。
    统一用带时区的 datetime 就不依赖这个巧合了。
    """
    return getattr(scheduler, "timezone", None)


def _now() -> datetime:
    """当前时间（带上调度器时区；调度器没配时区时退回本地时间）。"""
    tz = _scheduler_tz()
    return datetime.now(tz) if tz is not None else datetime.now()


def pick_mute_minutes(
    minimum: int,
    maximum: int,
    *,
    rng: Callable[[int, int], int] | None = None,
) -> int:
    """抽禁言时长（分钟）。

    **禁言就按这个值下** —— 没有封顶。用户 2026-10-02 明确要求：
    「不是调用平台 API 禁言 10 分钟，是到时间再调用 API 解禁」。

    抽成纯函数是为了能注入 ``rng`` 做确定性断言（抽奖逻辑只在生产才跑到，
    写错了就是「有人被关 8 小时」这种只能事后补救的事故）。
    """
    pick = rng or randint
    return pick(minimum, maximum)


async def _member_nickname(bot: Bot, group_id: int, user_id: int) -> str:
    """取群名片/昵称，失败就退回 QQ 号（**不抛**）。

    原插件语义保持一致：取不到昵称是「少个称呼」，不该让整个抽奖失败。
    """
    try:
        info = await bot.get_group_member_info(group_id=group_id, user_id=user_id)
    except Exception:
        logger.exception(
            "抽奖取成员昵称失败 group={} user={}，退回 QQ 号", group_id, user_id
        )
        return str(user_id)
    return str(info.get("card") or info.get("nickname") or user_id)


async def _unmute(bot_id: str, group_id: int, user_id: int) -> None:
    """定时解禁任务体：到点了主动调用 API 把人放出来。

    ⚠️ 这个函数就是「自动解禁」的**唯一实现**：禁言是按抽到的时长下的
    （例如 480 分钟），不靠平台自然到期，而是由这里提前 ``set_group_ban(duration=0)``
    解开。

    任务在 +N 分钟后才跑，那时手里的 ``Bot`` 对象可能已经因重连失效，
    所以用 ``get_bot(bot_id)`` 取**当前**实例。

    **失败时不再「放弃」**（这是容灾的一部分）：bot 离线 / API 抖动都只说明
    「这一刻放不出去」，记录仍在表里，所以累加 attempts 并在 60 秒后重试；
    重试到上限才放弃并删记录（那时禁言已经自然过了一大半，且持续重试会刷日志）。
    """
    _ensure_store()
    try:
        bot = get_bot(bot_id)
    except Exception:
        logger.warning("抽奖解禁：bot {} 暂时不可用，60 秒后重试", bot_id)
        _retry_later(bot_id, group_id, user_id)
        return

    try:
        await bot.set_group_ban(group_id=group_id, user_id=user_id, duration=0)
    except Exception:
        logger.exception(
            "抽奖解禁失败 group={} user={}，60 秒后重试", group_id, user_id
        )
        _retry_later(bot_id, group_id, user_id)
        return

    store.remove_pending(group_id, user_id)
    logger.info("抽奖定时解禁完成 group={} user={}", group_id, user_id)


def _retry_later(bot_id: str, group_id: int, user_id: int) -> None:
    """解禁失败后重排一次重试；超过上限则清掉记录（避免无限重试刷日志）。"""
    attempts = store.bump_attempts(group_id, user_id)
    if attempts > _MAX_UNMUTE_ATTEMPTS:
        store.remove_pending(group_id, user_id)
        logger.error(
            "抽奖解禁连续失败 {} 次，放弃 group={} user={}"
            "（禁言将按抽到的时长自然到期）",
            attempts,
            group_id,
            user_id,
        )
        return
    _add_unmute_job(
        bot_id,
        group_id,
        user_id,
        when=_now() + timedelta(seconds=_RETRY_DELAY_SECONDS),
    )


def _ensure_store() -> None:
    """确保容灾库已就绪（懒初始化，幂等）。

    正常情况下由启动钩子 ``_on_startup`` 初始化；这里再兜一层是因为
    「启动钩子没跑但 handler 被调了」不该让抽奖功能整个挂掉
    （代价只是一次多余的 init，而 init 本身是幂等的）。

    测试通过事先 ``store.init(tmp_path)`` 抢占，不会被这里覆盖
    （``is_initialized()`` 为真就跳过）。
    """
    if store.is_initialized():
        return
    try:
        store.init(store.default_db_path())
    except Exception:
        logger.exception("抽奖容灾库初始化失败（本次抽奖将无法持久化解禁任务）")


def _add_unmute_job(
    bot_id: str, group_id: int, user_id: int, *, when: datetime
) -> None:
    """把一条解禁任务交给 apscheduler（**不含**持久化，持久化由 store 负责）。

    失败只 warning：禁言本身已经生效，而待解禁记录仍在表里，
    下次启动/重试仍会把它捞回来。
    """
    job_id = _JOB_ID_FMT.format(group_id=group_id, user_id=user_id)
    try:
        scheduler.add_job(
            _unmute,
            trigger="date",
            run_date=when,
            args=[bot_id, group_id, user_id],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=_MISFIRE_GRACE_SECONDS,
            coalesce=True,
        )
    except Exception:
        logger.exception(
            "抽奖解禁任务挂载失败 group={} user={}（记录已落库，重启时会重建）",
            group_id,
            user_id,
        )


def _schedule_unmute(
    bot: Bot, group_id: int, user_id: int, minutes: int, *, ban_minutes: int
) -> None:
    """落库 + 挂任务。**先落库再挂任务** —— 反过来的话，两步之间崩了就没有真相。

    Args:
        bot: 当前 bot 实例（只取其 ``self_id``，不持有它）。
        group_id: 群号。
        user_id: 被禁言的群员。
        minutes: 多久之后解禁（阈值）。
        ban_minutes: 本次禁言的完整时长（抽到的值），用来算「自然到期时刻」。
    """
    _ensure_store()
    now = _now()
    unmute_at = now + timedelta(minutes=minutes)
    expire_at = now + timedelta(minutes=ban_minutes)

    store.save_pending(
        bot_id=str(bot.self_id),
        group_id=group_id,
        user_id=user_id,
        unmute_at=unmute_at.timestamp(),
        expire_at=expire_at.timestamp(),
    )
    _add_unmute_job(str(bot.self_id), group_id, user_id, when=unmute_at)
    logger.info(
        "抽奖：user={} 在群 {} 被禁言 {} 分钟，已落库并挂 {} 分钟后的自动解禁",
        user_id,
        group_id,
        ban_minutes,
        minutes,
    )


async def restore_pending_from_store() -> None:
    """启动时从容灾表重建解禁任务（**这是「重启不丢解禁」的关键**）。

    三种情况分开处理：

    | 记录状态 | 处理 |
    |---|---|
    | `unmute_at` 还没到 | 按原时刻重建任务 |
    | `unmute_at` 已过、`expire_at` 未到 | **立刻**解禁（停机期间本该解禁的） |
    | `expire_at` 也已过 | 平台早按抽到的时长自然解开了 → 直接删记录，不调 API |

    最后一种很重要：bot 停机好几小时后再重启，那些禁言早就到期了，
    再去调解禁 API 是纯粹的无用调用。
    """
    _ensure_store()
    rows = store.all_pending()
    if not rows:
        return

    now = time.time()
    rebuilt = overdue = cleaned = 0
    for row in rows:
        group_id, user_id = int(row["group_id"]), int(row["user_id"])
        bot_id = str(row["bot_id"])
        unmute_at, expire_at = float(row["unmute_at"]), float(row["expire_at"])

        if expire_at <= now:
            store.remove_pending(group_id, user_id)
            cleaned += 1
            continue

        when = datetime.fromtimestamp(unmute_at, tz=_scheduler_tz())
        if unmute_at <= now:
            when = _now()  # 停机期间错过了，立刻补上
            overdue += 1
        else:
            rebuilt += 1
        _add_unmute_job(bot_id, group_id, user_id, when=when)

    logger.info(
        "抽奖容灾：恢复 {} 条待解禁（其中 {} 条已过期立即执行），"
        "清理 {} 条自然到期的记录",
        rebuilt + overdue,
        overdue,
        cleaned,
    )


async def _on_startup() -> None:
    """插件启动钩子：定库路径 + 报词表状态 + 从持久化记录重建解禁任务。

    ⚠️ 干跑（只 `import bot` 不 `run()`）**不会**触发这里，所以生产干跑
    不会碰到数据库，也不会有任务被挂起。
    """
    _ensure_store()
    n_words = store.count_words()
    if n_words == 0:
        logger.warning(
            "抽奖匹配词表为空 —— 现在**任何消息都不会触发抽奖**；"
            "用「{}」添加（群主/管理员/超管）",
            handle.USAGE_ADD,
        )
    else:
        logger.info(
            "抽奖已加载 {} 条匹配词（运行时可变，见「{}」）", n_words, handle.USAGE_LIST
        )
    await restore_pending_from_store()


if plugin_config.lottery_persist_pending:
    get_driver().on_startup(_on_startup)


def _failed_msg(word: str) -> str:
    """禁言失败文案。用**实际命中的那个词**（词表现在是运行时数据，没有「主词」概念）。"""
    return f"{word}禁言失败，可能是机器人没有禁言权限"


async def _run_lottery(
    bot: Bot,
    event: GroupMessageEvent,
    word: str,
    matcher: Matcher | type[Matcher],
) -> None:
    """抽奖主流程：随机时长 → 按该时长禁言自己 → 回复（超阈值则挂定时解禁）。

    `word` = **实际命中的那条匹配词**（来自 DB），只用于回复文案。

    `matcher` 既可能是**实例**（监听 matcher 走 DI 注入，因为要先调
    `stop_propagation()`）、也可能是**类**（单测里 patch 类属性上的 `finish`）；
    `finish` 是 classmethod，两种都可用。
    """
    rolled = pick_mute_minutes(
        plugin_config.lottery_min_mute_time,
        plugin_config.lottery_max_mute_time,
    )
    unmute_after = plugin_config.lottery_unmute_after_minutes

    try:
        user_name = await _member_nickname(bot, event.group_id, event.user_id)
        # 禁言时长**就是**抽到的值（不做封顶）：用户要的是「到时间再调 API 解禁」，
        # 而不是「只禁 10 分钟」。
        await bot.set_group_ban(
            group_id=event.group_id,
            user_id=event.user_id,
            duration=rolled * 60,
        )
    except Exception:
        # 常见原因：bot 不是管理员 / 目标是群主 / 权限不足。
        logger.exception("抽奖禁言失败 group={} user={}", event.group_id, event.user_id)
        await matcher.finish(_failed_msg(word))

    # 只在「抽到的时长超过阈值」时才需要提前解禁；没超的话等平台自然到期即可。
    if rolled > unmute_after:
        _schedule_unmute(
            bot,
            event.group_id,
            event.user_id,
            unmute_after,
            ban_minutes=rolled,
        )

    await matcher.finish(
        f'恭喜 {user_name}({event.user_id}) 参与"{word}"！\n'
        f"🎉 获得了 {rolled}分钟禁言大礼包 🎉"
    )


@lottery_watch.handle()
async def _handle_lottery_watch(
    bot: Bot,
    event: GroupMessageEvent,
    matcher: Matcher,
) -> None:
    """监听所有群消息：命中运行时词表才处理，否则**原样放行**。

    ⚠️ 命中后必须 `matcher.stop_propagation()`：引擎在同一优先级内是
    `tg.start_soon` **并发跑完所有 matcher** 的，`block` 只用来跳过**更低**优先级；
    而这里 `block=False`，不显式阻断的话「抽奖」和别的插件会同时处理这条消息。
    """
    _ensure_store()
    hit = words.find_match(store.list_words(), event.get_plaintext())
    if hit is None:
        # 不是我们的消息：不阻断、不回消息、不写库 —— 对其它插件完全透明
        return
    matcher.stop_propagation()
    await _run_lottery(bot, event, hit.word, matcher)


__all__: list[str] = [
    "__plugin_meta__",
    "lottery_watch",
    "pick_mute_minutes",
    "plugin_config",
]
