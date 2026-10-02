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
    抽奖时在 ``[MIN, MAX]`` 之间等概率取一个数，**回复里报的就是这个数**。

    环境变量 ``LOTTERY_MUTE_CAP_MINUTES``：实际禁言上限（分钟），默认 10。
    超过它的抽取结果**实际只禁言这么久**，并额外挂一个定时解禁任务兜底
    （用户 2026-10-02 要求：「原样但超 10 分钟的定时自动解禁（隐性）」）。
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
        description="抽奖禁言最长时间（分钟）；回复里报的就是抽到的数",
    )
    lottery_mute_cap_minutes: int = Field(
        default=10,
        ge=1,
        le=43200,
        description="实际禁言上限（分钟）；超过的抽取结果实际只关这么久 + 定时解禁兜底",
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
