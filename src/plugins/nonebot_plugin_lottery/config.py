"""插件配置。

## 禁言时长的单位与边界

QQ 群禁言的**平台边界**是以**秒**计的：最短 ``60`` 秒、最长 ``2_592_000`` 秒（30 天）。
本插件的配置沿用上游真寻插件的习惯，以**分钟**为单位，因此边界写成
``1 ~ 43_200`` 分钟（= ``60 ~ 2_592_000`` 秒，整除，无舍入）。
这两个换算关系用 ``QQ_BAN_*`` 常量具名表达，别把裸数字散在代码里 ——
看到 ``43200`` 不会有人立刻想到「30 天」，看到 ``QQ_BAN_MAX_SECONDS`` 会。

（2026-10-02 用户要求：禁言时长区间按平台真实边界取 ``60 ~ 2_592_000`` 秒。）
"""

from __future__ import annotations

import logging
from typing import Final

from nonebot import get_plugin_config
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger("nonebot_plugin_lottery")

#: QQ 群禁言的平台边界（**秒**）：最短 60 秒 / 最长 2,592,000 秒（30 天）。
QQ_BAN_MIN_SECONDS: Final[int] = 60
QQ_BAN_MAX_SECONDS: Final[int] = 2_592_000

#: 同一组边界的**分钟**表示（本插件配置的单位）：1 / 43,200。
QQ_BAN_MIN_MINUTES: Final[int] = QQ_BAN_MIN_SECONDS // 60
QQ_BAN_MAX_MINUTES: Final[int] = QQ_BAN_MAX_SECONDS // 60


class Config(BaseModel):
    """抽奖（禁言小助手）插件配置。

    环境变量 ``LOTTERY_MIN_MUTE_TIME``：抽奖禁言最短时间（分钟），
    默认 ``1``（= 60 秒，QQ 允许的最短禁言）。

    环境变量 ``LOTTERY_MAX_MUTE_TIME``：抽奖禁言最长时间（分钟），
    默认 ``43200``（= 2,592,000 秒 = 30 天，QQ 允许的最长禁言）。
    抽奖时在 ``[MIN, MAX]`` 之间等概率取一个数，**禁言时长就是它**。

    环境变量 ``LOTTERY_UNMUTE_AFTER_MINUTES``：超过这个分钟数时，机器人会在
    到点后**主动调用 API 解禁**（默认 10 分钟）。

    ⚠️ 注意这**不是**「禁言封顶」：禁言本身按抽到的时长下（480 就是真禁 480），
    只是到 ``UNMUTE_AFTER`` 分钟时再调一次 ``set_group_ban(duration=0)`` 把人放出来。
    用户 2026-10-02 明确纠正：
    「**不是调用平台 API 禁言 10 分钟，是到时间再调用 API 解禁**」。
    """

    lottery_min_mute_time: int = Field(
        default=QQ_BAN_MIN_MINUTES,
        ge=QQ_BAN_MIN_MINUTES,
        le=QQ_BAN_MAX_MINUTES,
        description="抽奖禁言最短时间（分钟），默认 1 = 平台最短 60 秒",
    )
    lottery_max_mute_time: int = Field(
        default=QQ_BAN_MAX_MINUTES,
        ge=QQ_BAN_MIN_MINUTES,
        le=QQ_BAN_MAX_MINUTES,
        description=(
            "抽奖禁言最长时间（分钟），默认 43200 = 平台最长 2592000 秒；"
            "报多少就真禁多久"
        ),
    )
    lottery_unmute_after_minutes: int = Field(
        default=10,
        ge=1,
        le=QQ_BAN_MAX_MINUTES,
        description="禁言超过这个分钟数时，到点由机器人调 API 主动解禁（非禁言封顶）",
    )
    lottery_persist_pending: bool = Field(
        default=True,
        description="是否启用解禁任务的容灾持久化（落 sqlite + 启动时重建任务）",
    )

    # ── 监听所有群消息的 matcher 优先级 ────────────────────────────────
    #
    # 触发词**不再来自配置**（2026-10-06 用户拍板：词表只存 DB、运行时增删改），
    # 所以这里只留一个「监听响应器」的优先级旋钮。
    #
    # ⚠️ 必须 **大于** `handle.CRUD_PRIORITY`（3）：CRUD 命令是 `block=True`，靠更小的
    # 优先级抢先。若把本值设成 ≤3，CRUD 会与监听同级 → 一条「查看匹配词」可能
    # 既列表又抽奖（把自己禁言）。该不变式在 `__init__.py` 里显式告警。
    lottery_match_priority: int = Field(
        default=5,
        ge=1,
        description=(
            "监听所有群消息的 matcher 优先级（越小越先），默认 5；"
            "必须大于 CRUD 命令的 3"
        ),
    )

    @model_validator(mode="after")
    def _fix_inverted_range(self) -> Config:
        """``MIN > MAX`` 时自动对调，而不是让插件起不来。

        手改 ``.env`` 时写反了（如 MIN=480 / MAX=1）会让 ``random.randint``
        直接抛 ``ValueError`` —— 表现是「发抽奖毫无反应 + 日志一堆报错」。
        配置错误只该让人看到一条 warning，不该让功能整个不可用。
        """
        if self.lottery_min_mute_time > self.lottery_max_mute_time:
            logger.warning(
                "LOTTERY_MIN_MUTE_TIME(%s) > LOTTERY_MAX_MUTE_TIME(%s)，"
                "已自动对调；请检查 .env 是否写反",
                self.lottery_min_mute_time,
                self.lottery_max_mute_time,
            )
            self.lottery_min_mute_time, self.lottery_max_mute_time = (
                self.lottery_max_mute_time,
                self.lottery_min_mute_time,
            )
        return self


plugin_config: Config = get_plugin_config(Config)

__all__ = [
    "QQ_BAN_MAX_MINUTES",
    "QQ_BAN_MAX_SECONDS",
    "QQ_BAN_MIN_MINUTES",
    "QQ_BAN_MIN_SECONDS",
    "Config",
    "plugin_config",
]
