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
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

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

    # ── 触发词：分「命令词」与「关键词」两轴 ────────────────────────────
    #
    # 两者走 NoneBot 的**不同**响应规则，语义不一样（2026-10-06 读引擎源码确认）：
    #
    #   command 规则（命令词）→ 只看**消息第一个段**（且必须是文本段），要求它
    #     以 `COMMAND_START + 词` 开头；`我们抽奖吧` 不匹配、`[图片]抽奖` 也不匹配。
    #   keyword 规则（关键词）→ 用 `event.get_plaintext()`（**剥离图片/at 段后的
    #     纯文本**）做「**包含**」判断；`今天自刎归天真好看` 匹配、
    #     `[图片]自刎归天` 也匹配。
    #
    # 所以「网络梗」必须走关键词：它是「消息里模糊带有」，而命令词是「以它开头」。
    lottery_commands: list[str] = Field(
        default=["抽奖"],
        description=(
            "触发抽奖的**命令词**列表，第一个为主命令、其余为别名"
            '（如 ["抽奖","禁言抽奖"]）；以它开头（遵循 COMMAND_START）才触发'
        ),
    )
    lottery_keywords: list[str] = Field(
        default=["自刎归天", "我部悍将刘三刀", "上将潘凤"],
        description=(
            "触发抽奖的**关键词**列表：消息纯文本**包含**任一即触发（网络梗用）；"
            "空列表 = 关闭关键词触发"
        ),
    )

    @field_validator("lottery_commands", "lottery_keywords", mode="before")
    @classmethod
    def _normalize_word_list(cls, value: object, info: ValidationInfo) -> list[str]:
        """去首尾空白、丢空项、去重（**保序** —— 顺序决定默认值与文案取词）。

        ⚠️ 这两个字段在 ``.env`` 里必须写成 **JSON 数组**：

            LOTTERY_COMMANDS=["抽奖","禁言抽奖"]
            LOTTERY_KEYWORDS=["自刎归天"]

        NoneBot 对自定义键统一走 ``json.loads``，所以写成裸值
        （``LOTTERY_KEYWORDS=自刎归天``）
        会在**解析 env 阶段**就抛 ``JSONDecodeError`` 让进程起不来 —— 那发生在
        本校验器之前，这里拦不到，只能靠文档说清。清空要写 ``[]``，不能写裸空。
        """
        if value is None:
            return []
        if isinstance(value, str):  # 单个词也容忍（虽然 env 里到不了这一步）
            value = [value]
        if not isinstance(value, (list, tuple, set)):
            return []
        out: list[str] = []
        for item in value:
            word = str(item).strip()
            if word and word not in out:
                out.append(word)
            elif not word:
                logger.warning(
                    "LOTTERY_%s 里有空白项，已忽略",
                    (info.field_name or "?").removeprefix("lottery_").upper(),
                )
        return out

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
