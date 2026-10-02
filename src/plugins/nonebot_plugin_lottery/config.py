"""插件配置。"""

from __future__ import annotations

import logging

from nonebot import get_plugin_config
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger("nonebot_plugin_lottery")


class Config(BaseModel):
    """抽奖（禁言小助手）插件配置。

    环境变量 ``LOTTERY_MIN_MUTE_TIME``：抽奖禁言最短时间（分钟），默认 1。

    环境变量 ``LOTTERY_MAX_MUTE_TIME``：抽奖禁言最长时间（分钟），默认 480。
    抽奖时在 ``[MIN, MAX]`` 之间等概率取一个数，**禁言时长就是它**。

    环境变量 ``LOTTERY_UNMUTE_AFTER_MINUTES``：超过这个分钟数时，机器人会在
    到点后**主动调用 API 解禁**（默认 10 分钟）。

    ⚠️ 注意这**不是**「禁言封顶」：禁言本身按抽到的时长下（480 就是真禁 480），
    只是到 ``UNMUTE_AFTER`` 分钟时再调一次 ``set_group_ban(duration=0)`` 把人放出来。
    用户 2026-10-02 明确纠正：
    「**不是调用平台 API 禁言 10 分钟，是到时间再调用 API 解禁**」。
    """

    lottery_min_mute_time: int = Field(
        default=1,
        ge=1,
        le=43200,
        description="抽奖禁言最短时间（分钟）",
    )
    lottery_max_mute_time: int = Field(
        default=480,
        ge=1,
        le=43200,
        description="抽奖禁言最长时间（分钟）；报出来多少就真禁多久",
    )
    lottery_unmute_after_minutes: int = Field(
        default=10,
        ge=1,
        le=43200,
        description="禁言超过这个分钟数时，到点由机器人调 API 主动解禁（非禁言封顶）",
    )
    lottery_persist_pending: bool = Field(
        default=True,
        description="是否启用解禁任务的容灾持久化（落 sqlite + 启动时重建任务）",
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

__all__ = ["Config", "plugin_config"]
