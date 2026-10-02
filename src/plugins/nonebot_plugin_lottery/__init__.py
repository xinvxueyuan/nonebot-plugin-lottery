"""抽奖（禁言小助手）插件主模块。

抽取自 zhenxun_bot 的 ``zhenxun/plugins/lottery``（作者「阿珏酱」，v1.1），
把真寻内部件（``Config`` / ``PluginExtraData`` / ``MessageUtils`` /
``zhenxun.logger``）换成原生 NoneBot2 等价物，行为保持一致：

    群员发送 ``抽奖`` → 在 ``[MIN, MAX]`` 里随机取一个分钟数 → **禁言自己**
    → 回复「恭喜 xxx 参与"抽奖"！🎉 获得了 N分钟禁言大礼包 🎉」

用户 2026-10-02 追加要求：**「原样但超 10 分钟的定时自动解禁（隐性）」**。
落地方式有两层，缺一不可（原因见下）：

    1. 实际禁言时长 = ``min(抽到的数, LOTTERY_MUTE_CAP_MINUTES)``
       —— 让**平台侧**自己到期解开，不依赖本进程活着。
    2. 抽到的数 > 上限时，**额外**挂一个定时任务在 +上限 分钟时解禁
       （``set_group_ban(duration=0)``，幂等）。

⚠️ 为什么不能只靠第 2 层的定时任务：apscheduler 的任务是**进程内内存态**，
qbot 重启（这台机器 3 天里重启过 12 次）就丢了 —— 那被抽到 480 分钟的人
会被真关 8 小时。把封顶做进 ``duration`` 之后，无论 bot 死没死，用户最多
被关 10 分钟；定时任务退化成「更精确的兜底」。

⚠️ 「隐性」的含义：**回复里报的仍是抽到的原始数字**（保留原插件的玩笑效果），
不告诉群里「其实只关了 10 分钟」。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from random import randint
from typing import TYPE_CHECKING, Final

from nonebot import get_bot, logger, on_command, require
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

from .config import plugin_config

if TYPE_CHECKING:
    from collections.abc import Callable

__plugin_meta__ = PluginMetadata(
    name="抽奖",
    description="参与抽奖的群员会被随机禁言一段时间, 我看看谁这么贱",
    usage="""指令：
  - 抽奖    (直接参与抽奖并被随机禁言)
""".strip(),
    type="application",
    homepage="https://github.com/xinvxueyuan/nonebot-plugin-lottery",
    supported_adapters={"nonebot.adapters.onebot.v11"},
    extra={
        # 原作者（抽取来源）与本仓库维护者分开记，便于回溯
        "author": "阿珏酱",
        "maintainer": "xinvxueyuan",
        "upstream": "zhenxun_bot zhenxun/plugins/lottery",
        "version": "1.1",
        "menu_type": "群内小游戏",
        "commands": [{"command": "抽奖"}],
    },
)

lottery_cmd = on_command("抽奖", priority=5, block=True)

_FAILED_MSG: Final[str] = "抽奖禁言失败，可能是机器人没有禁言权限"
_JOB_ID_FMT: Final[str] = "lottery_unmute_{group_id}_{user_id}"


def pick_mute_minutes(
    minimum: int,
    maximum: int,
    cap: int,
    *,
    rng: Callable[[int, int], int] | None = None,
) -> tuple[int, int]:
    """抽时间。

    Returns:
        ``(报出来的分钟数, 实际禁言的分钟数)``。
        前者是抽到的原始值（回复里报它，保留原插件的玩笑效果）；
        后者封顶到 ``cap``，保证群里最多被关 ``cap`` 分钟。

    抽纯函数是为了能直接单测 —— 随机 + 封顶这两件事一旦写错，
    表现是「有人被关 8 小时」，只在生产才会发现。
    """
    pick = rng or randint
    rolled = pick(minimum, maximum)
    return rolled, min(rolled, cap)


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
    """定时解禁任务体：把用户从禁言里放出来。

    ⚠️ 任务在 **+上限分钟** 后才跑，那时手里的 ``Bot`` 对象可能已经因重连失效，
    所以这里用 ``get_bot(bot_id)`` 取**当前**实例。取不到（bot 离线/重启）就
    静默放弃 —— 平台侧的 ``duration`` 已经把上限封死了，用户不会被关更久。
    """
    try:
        bot = get_bot(bot_id)
    except Exception:
        logger.warning(
            "抽奖解禁任务：bot {} 不可用，跳过解禁（平台侧已按封顶时长自动解禁）",
            bot_id,
        )
        return
    try:
        await bot.set_group_ban(group_id=group_id, user_id=user_id, duration=0)
    except Exception:
        logger.exception("抽奖定时解禁失败 group={} user={}", group_id, user_id)
        return
    logger.info("抽奖定时解禁完成 group={} user={}", group_id, user_id)


def _schedule_unmute(bot: Bot, group_id: int, user_id: int, minutes: int) -> None:
    """挂一个一次性解禁任务（失败只 warning，不影响已生效的封顶禁言）。"""
    job_id = _JOB_ID_FMT.format(group_id=group_id, user_id=user_id)
    try:
        scheduler.add_job(
            _unmute,
            trigger="date",
            run_date=datetime.now() + timedelta(minutes=minutes),
            args=[str(bot.self_id), group_id, user_id],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=60,
            coalesce=True,
        )
    except Exception:
        logger.exception(
            "抽奖定时解禁任务挂载失败 group={} user={}（禁言仍按封顶时长生效）",
            group_id,
            user_id,
        )
        return
    logger.info(
        "抽奖：user={} 在群 {} 抽中超时，已于 {} 分钟后挂自动解禁",
        user_id,
        group_id,
        minutes,
    )


@lottery_cmd.handle()
async def _handle_lottery(bot: Bot, event: GroupMessageEvent) -> None:
    """抽奖主流程：随机时长 → 禁言自己 → 回复。"""
    rolled, actual = pick_mute_minutes(
        plugin_config.lottery_min_mute_time,
        plugin_config.lottery_max_mute_time,
        plugin_config.lottery_mute_cap_minutes,
    )

    try:
        user_name = await _member_nickname(bot, event.group_id, event.user_id)
        await bot.set_group_ban(
            group_id=event.group_id,
            user_id=event.user_id,
            duration=actual * 60,
        )
    except Exception:
        # 常见原因：bot 不是管理员 / 目标是群主 / 权限不足。
        logger.exception("抽奖禁言失败 group={} user={}", event.group_id, event.user_id)
        await lottery_cmd.finish(_FAILED_MSG)

    if rolled > actual:
        _schedule_unmute(bot, event.group_id, event.user_id, actual)

    # ⚠️ 报的是 `rolled`（抽到的原始值）而不是 `actual` —— 这是「隐性」的要求：
    #    群里看到的就是原插件的玩笑效果，封顶不对外说。
    await lottery_cmd.finish(
        f'恭喜 {user_name}({event.user_id}) 参与"抽奖"！\n'
        f"🎉 获得了 {rolled}分钟禁言大礼包 🎉"
    )


__all__: list[str] = [
    "__plugin_meta__",
    "pick_mute_minutes",
    "plugin_config",
]
