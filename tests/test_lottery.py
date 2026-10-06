"""抽奖（禁言小助手）测试。

重点覆盖用户 2026-10-02 追加的那条要求：**「超阈值的定时自动解禁」**。

用户后来明确纠正过语义（很重要，别写反）：
    ✅ 禁言按**抽到的时长**下（480 就真禁 480），到 10 分钟时**调 API 解禁**
    ❌ 不是「把禁言时长砍成 10 分钟」（那是把平台参数当成上限用，已被否决）

所以核心断言是：
  ① `set_group_ban(duration=)` == 抽到的分钟数 * 60（**不封顶**）；
  ② 抽到的值 > 阈值时，挂一个「+阈值 分钟」的解禁任务（任务体调 duration=0）；
  ③ 抽到的值 ≤ 阈值时不挂任务（等平台自然到期）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
import pytest

if TYPE_CHECKING:
    from nonebot.matcher import Matcher

from nonebot_plugin_lottery import pick_mute_minutes, plugin_config
from nonebot_plugin_lottery.config import (
    QQ_BAN_MAX_MINUTES,
    QQ_BAN_MAX_SECONDS,
    QQ_BAN_MIN_SECONDS,
    Config,
)

# ── 纯函数：抽时长（不封顶）────────────────────────────────────────


def test_rolled_value_inside_range():
    for _ in range(200):
        assert 1 <= pick_mute_minutes(1, 480) <= 480


def test_rolled_value_is_returned_as_is_without_capping():
    """**核心**：抽到多少就是多少 —— 禁言时长不做任何封顶。

    这条钉住用户纠正过的语义。若有人「好心」把 min(rolled, cap) 加回来，这里会红。
    """
    for _ in range(200):
        rolled = pick_mute_minutes(1, 480)
        assert rolled >= 1  # 返回值本身就是完整时长，没有被截断
    assert pick_mute_minutes(1, 480, rng=lambda _a, _b: 480) == 480
    assert pick_mute_minutes(1, 480, rng=lambda _a, _b: 479) == 479


def test_values_within_range_are_untouched():
    for _ in range(200):
        rolled = pick_mute_minutes(1, 10)
        assert 1 <= rolled <= 10


def test_deterministic_rng_is_used():
    """注入 rng 后结果可预测 —— 便于断言边界（而不是靠随机碰运气）。"""
    assert pick_mute_minutes(1, 480, rng=lambda _a, _b: 480) == 480
    assert pick_mute_minutes(1, 480, rng=lambda _a, _b: 1) == 1
    # 正好等于阈值：不算超时，不该挂定时任务（边界，差一就错）
    assert pick_mute_minutes(1, 480, rng=lambda _a, _b: 10) == 10


def test_min_equal_max():
    assert pick_mute_minutes(5, 5) == 5


def test_rng_is_called_with_the_configured_bounds():
    seen: list[tuple[int, int]] = []

    def spy(a: int, b: int) -> int:
        seen.append((a, b))
        return a

    pick_mute_minutes(3, 77, rng=spy)
    assert seen == [(3, 77)]


# ── 配置：默认值 / 写反了要自动对调 ────────────────────────────────


def test_default_range_is_exactly_the_qq_platform_limits():
    """默认区间必须**正好**是 QQ 平台的真实边界：60 ~ 2,592,000 秒。

    2026-10-02 用户要求「禁言时间阈值改为 60～2,592,000s」。
    配置单位是**分钟**（沿用上游、也是报给群里看的单位），所以这里
    同时断言两侧：分钟值与秒值的换算必须整除且相等，
    光断言 43200 而不断言「= 2,592,000 秒」的话，
    有人把常量改错（比如写成 43_200 秒）也测不出来。
    """
    cfg = Config()
    assert cfg.lottery_min_mute_time == 1
    assert cfg.lottery_max_mute_time == 43200
    assert cfg.lottery_unmute_after_minutes == 10

    # 换算成秒必须正好等于用户给的边界
    assert cfg.lottery_min_mute_time * 60 == 60
    assert cfg.lottery_max_mute_time * 60 == 2_592_000

    # 常量本身也要对（防止有人只改常量或只改默认值）
    assert QQ_BAN_MIN_SECONDS == 60
    assert QQ_BAN_MAX_SECONDS == 2_592_000
    assert QQ_BAN_MAX_MINUTES * 60 == QQ_BAN_MAX_SECONDS


def test_mute_bounds_reject_out_of_platform_range():
    """超出平台边界的值必须被 pydantic 拦下，不能带到 API 调用上。"""
    for bad in (0, -1, QQ_BAN_MAX_MINUTES + 1):
        with pytest.raises(ValueError):
            Config(lottery_max_mute_time=bad)


def test_inverted_range_is_swapped_not_raised():
    """MIN > MAX 时自动对调，而不是让插件起不来/发牌时炸 randint。"""
    cfg = Config(lottery_min_mute_time=480, lottery_max_mute_time=1)
    assert cfg.lottery_min_mute_time == 1
    assert cfg.lottery_max_mute_time == 480


def test_unmute_threshold_must_be_positive():
    with pytest.raises(ValueError):
        Config(lottery_unmute_after_minutes=0)


def test_old_cap_name_is_gone():
    """旧的 `lottery_mute_cap_minutes`（禁言封顶）不该再存在 —— 语义已否决。"""
    assert not hasattr(Config(), "lottery_mute_cap_minutes")


# ── 插件确实加载了（import 期没有静默降级）─────────────────────────


def test_plugin_metadata_names_the_upstream_author():
    from nonebot_plugin_lottery import __plugin_meta__

    extra = __plugin_meta__.extra or {}
    assert extra.get("author") == "阿珏酱"  # 原作者，抽取来源可追溯
    assert extra.get("upstream", "").endswith("plugins/lottery")
    assert __plugin_meta__.name == "抽奖"


def _registered_commands(matcher) -> set[str]:
    """从 matcher 的 rule 里取出注册的命令字串。

    命令在 `matcher.rule.checkers` 里的 `Dependent.call`（`Command`）上，
    `cmds` 是**元组嵌套**（`(("抽奖",),)`，内层是 aliases）——
    直接 `in matcher.rule.commands` 会 AttributeError（这个我第一版就写错了）。
    """
    out: set[str] = set()
    for checker in matcher.rule.checkers or ():
        cmds = getattr(getattr(checker, "call", None), "cmds", ()) or ()
        for group in cmds:
            if isinstance(group, str):
                out.add(group)
            else:
                out.update(str(x) for x in group)
    return out


def _registered_keywords(matcher) -> set[str]:
    """从 matcher 的 rule 里取出注册的关键词（`KeywordsRule.keywords`）。"""
    out: set[str] = set()
    for checker in matcher.rule.checkers or ():
        out.update(getattr(getattr(checker, "call", None), "keywords", ()) or ())
    return out


# ── 触发词来自配置（不再是硬编码）────────────────────────────────────


def test_command_words_come_from_config():
    """`抽奖` 这类触发词**不再是硬编码** —— 全部来自 `lottery_commands`。"""
    from nonebot_plugin_lottery import lottery_cmd

    assert lottery_cmd is not None
    assert _registered_commands(lottery_cmd) == set(plugin_config.lottery_commands)
    assert lottery_cmd.block is True


def test_keyword_words_come_from_config():
    """网络梗走**关键词**响应器，词表同样来自配置。"""
    from nonebot_plugin_lottery import lottery_keyword

    assert lottery_keyword is not None
    assert _registered_keywords(lottery_keyword) == set(plugin_config.lottery_keywords)
    assert lottery_keyword.block is True


def test_keyword_priority_is_lower_than_command():
    """**核心不变式**：关键词必须在命令**之后**（数字更大）。

    引擎在同一优先级里是 `tg.start_soon` **并发跑完所有 matcher** 的，`block` 只能跳过
    **更低**优先级。两者同优先级时，一条同时命中两者的消息会被两个 matcher 各处理一次
    —— 禁言两次 + 回两条。这条断言就是防那个（改优先级的人会在这里被拦下）。
    """
    import nonebot_plugin_lottery as plugin
    from nonebot_plugin_lottery import lottery_cmd, lottery_keyword

    assert lottery_cmd is not None
    assert lottery_keyword is not None
    assert plugin._COMMAND_PRIORITY == 5, "命令优先级沿用原值，别顺手改"
    assert lottery_keyword.priority > lottery_cmd.priority


def test_metadata_command_words_come_from_config():
    """菜单/帮助里列的命令词与关键词也来自配置（用户要求文案跟着变）。"""
    from nonebot_plugin_lottery import __plugin_meta__

    extra = __plugin_meta__.extra or {}
    listed = [c["command"] for c in extra.get("commands", [])]
    assert listed == list(plugin_config.lottery_commands)
    assert extra.get("keywords") == list(plugin_config.lottery_keywords)
    usage = __plugin_meta__.usage or ""
    for w in plugin_config.lottery_commands + plugin_config.lottery_keywords:
        assert w in usage, f"用法说明里没有配置的词 {w!r}"


def test_words_really_come_from_env_config():
    """**端到端**（另起进程 + 真跑 handler）：env 换词表，文案用**命中的那个词**。

    ⚠️ 必须**另起进程**：`nonebot.init()` 得在 env 设好**之后**跑，而本插件在
    **模块级**就注册响应器 —— 同进程里改 env 再 import 已经晚了，测出来是假的。

    这里同时钉住三件事（都是变异检验抓出来的必需项）：
      ① 注册的命令词/关键词跟着 `LOTTERY_COMMANDS` / `LOTTERY_KEYWORDS` 变
         （写死 `"抽奖"` 时，默认配置下别的用例**照样全绿**，只有这条会红）；
      ② 回复文案用的是**实际命中的那个词** —— 用「配置里第一个词」代替时，
         默认配置（只有一个词）看不出区别，所以这里配了
         **别名** `禁言抽奖`；
      ③ 失败文案用**主命令词**（= 配置第一个），保证默认配置下与旧版逐字一致。
    """
    import json
    import os
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        import asyncio
        import json

        import nonebot

        nonebot.init(driver="~none", log_level="ERROR")
        from nonebot.adapters.onebot.v11 import Adapter

        nonebot.get_driver().register_adapter(Adapter)
        import nonebot_plugin_lottery as plugin
        from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message

        # 固定随机值且**不超过**解禁阈值 ⇒ 不挂任务、不碰数据库
        plugin.randint = lambda _a, _b: 3

        replies = []

        async def fake_finish(message=None, **kwargs):
            replies.append(str(message))
            raise RuntimeError("__finish__")     # 模拟 finish 结束控制流

        # 类属性赋值：handler 里 `matcher.finish(...)` 走的就是它
        plugin.lottery_cmd.finish = fake_finish
        plugin.lottery_keyword.finish = fake_finish

        def build_event(text):
            return GroupMessageEvent.model_construct(
                post_type="message",
                message_type="group",
                sub_type="normal",
                group_id=1,
                user_id=2,
                message_id=1,
                message=Message(text),
                raw_message=text,
            )

        class Bot:
            self_id = "1"

            def __init__(self, ban_ok=True):
                self.ban_ok = ban_ok

            async def get_group_member_info(self, *, group_id, user_id):
                return {"card": "阿百川", "nickname": "大鬼"}

            async def set_group_ban(self, *, group_id, user_id, duration):
                if not self.ban_ok:
                    raise RuntimeError("no permission")

        async def call(handler, *args):
            try:
                await handler(*args)
            except RuntimeError:
                pass

        def commands(matcher):
            out = set()
            for checker in (matcher.rule.checkers or ()):
                cmds = getattr(getattr(checker, "call", None), "cmds", ()) or ()
                for group in cmds:
                    out.update([group] if isinstance(group, str) else group)
            return out

        def keywords(matcher):
            out = set()
            for checker in (matcher.rule.checkers or ()):
                rule = getattr(checker, "call", None)
                out.update(getattr(rule, "keywords", ()) or ())
            return out

        async def main():
            event = build_event("抽奖")
            # ① 命中**别名**（不是配置里第一个词）
            await call(plugin._handle_lottery_command, Bot(), event, ("禁言抽奖",))
            # ② 命中主命令词
            await call(plugin._handle_lottery_command, Bot(), event, ("抽奖",))
            # ③ 命中关键词
            await call(plugin._handle_lottery_keyword, Bot(), event, "俺也一样")
            # ④ 失败路径（bot 没有禁言权限）
            no_perm = Bot(ban_ok=False)
            await call(plugin._handle_lottery_command, no_perm, event, ("抽奖",))

        asyncio.run(main())

        meta = plugin.__plugin_meta__
        payload = {
            "commands": sorted(commands(plugin.lottery_cmd)),
            "keywords": sorted(keywords(plugin.lottery_keyword)),
            "meta_commands": [
                c["command"] for c in (meta.extra or {}).get("commands", [])
            ],
            "usage": meta.usage,
            "replies": replies,
        }
        print("RESULT:" + json.dumps(payload, ensure_ascii=False))
        """
    )
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["LOTTERY_COMMANDS"] = '["禁言抽奖","抽奖"]'
    env["LOTTERY_KEYWORDS"] = '["俺也一样","自刎归天"]'

    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        env=env,
        check=True,
    )
    result = next(ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:"))
    data = json.loads(result.removeprefix("RESULT:"))

    # ① 词表跟着配置变
    assert data["commands"] == ["抽奖", "禁言抽奖"], "命令词没跟着配置变"
    assert data["keywords"] == ["俺也一样", "自刎归天"], "关键词没跟着配置变"
    assert data["meta_commands"] == ["禁言抽奖", "抽奖"], "菜单顺序应等于配置顺序"
    assert "禁言抽奖" in data["usage"]
    assert "俺也一样" in data["usage"]

    replies = data["replies"]
    assert len(replies) == 4, f"应有 4 条回复，实际 {replies}"
    # ② 文案用**命中的词**：命中别名时写别名，不能写主命令词
    assert '参与"禁言抽奖"！' in replies[0], replies[0]
    assert '参与"抽奖"！' in replies[1], replies[1]
    assert '参与"俺也一样"！' in replies[2], replies[2]
    # ③ 失败文案用主命令词（= 配置第一个），默认配置下与旧版逐字一致
    assert replies[3] == "禁言抽奖禁言失败，可能是机器人没有禁言权限", replies[3]


# ── 匹配语义：拿**真事件**过**真规则**（不测字符串，测 NoneBot 的真实行为）──


def _text_event(plaintext: str) -> GroupMessageEvent:
    """带 `message` 的真群聊事件（命令/关键词规则都读它）。"""
    return GroupMessageEvent.model_construct(
        post_type="message",
        message_type="group",
        sub_type="normal",
        group_id=1094538078,
        user_id=10001,
        message_id=1,
        message=Message(plaintext),
        raw_message=plaintext,
    )


async def _rule_matches(matcher, plaintext: str) -> bool:
    """按引擎的方式跑一遍该 matcher 的 rule。

    ⚠️ 必须先 `TrieRule.get_value` —— `nonebot/message.py::handle_event` 在跑 rule
    之前就会做这一步把命令解析结果写进 state（`command` 规则依赖它），
    漏了的话命令规则**恒不匹配**，测试会给出「配置没生效」的假结论。
    """
    from nonebot.rule import TrieRule

    event = _text_event(plaintext)
    state: dict = {}
    TrieRule.get_value(None, event, state)
    return bool(await matcher.rule(None, event, state))


@pytest.mark.parametrize(
    ("plaintext", "expected"),
    [
        ("抽奖", True),
        ("/抽奖", True),          # COMMAND_START 含 "/"
        (" 抽奖", True),          # 首段 lstrip 后匹配
        ("我们抽奖吧", False),      # 命令只在**开头**匹配
        ("我要抽奖", False),
        ("[CQ:image,file=x.jpg]抽奖", False),  # 首段不是文本段 → 命令规则不匹配
        ("无关消息", False),
    ],
)
async def test_command_rule_matching(plaintext, expected):
    """命令规则的**真实语义**：只看消息第一个段，且必须以 COMMAND_START + 词开头。"""
    from nonebot_plugin_lottery import lottery_cmd

    assert lottery_cmd is not None
    assert await _rule_matches(lottery_cmd, plaintext) is expected


@pytest.mark.parametrize(
    ("plaintext", "expected"),
    [
        ("自刎归天", True),
        ("今天自刎归天真好看", True),          # 模糊包含（网络梗的用法）
        ("[CQ:image,file=x.jpg]自刎归天", True),  # 图片段被剥离 → 仍命中
        ("[CQ:at,qq=1]自刎归天", True),        # at 段同理
        ("我部悍将刘三刀在此", True),
        ("无关消息", False),
    ],
)
async def test_keyword_rule_matching(plaintext, expected):
    """关键词规则的**真实语义**：`get_plaintext()`（剥图片/at 段）做「包含」判断。"""
    from nonebot_plugin_lottery import lottery_keyword

    assert lottery_keyword is not None
    assert await _rule_matches(lottery_keyword, plaintext) is expected


async def test_keyword_rule_does_not_swallow_plain_messages():
    """回归：普通聊天消息不该被关键词规则命中（网络梗是罕见串才适合放进去）。"""
    from nonebot_plugin_lottery import lottery_keyword

    assert lottery_keyword is not None
    for text in ("今天天气不错", "有人吗", "666", "作业写完了"):
        assert await _rule_matches(lottery_keyword, text) is False


def test_default_keywords_are_rare_strings():
    """默认网络梗必须是**罕见串**：命中的代价是有人被随机禁言（最长 30 天）。

    放「天意」「大哥」「俺也一样」这种日常高频词进来，群里会天天有人被禁言。
    """
    assert all(len(w) >= 4 for w in plugin_config.lottery_keywords), (
        "默认关键词太短，容易在日常聊天里误伤"
    )


# ── handler：禁言时长 / 解禁任务 ──────────────────────────────────


def _group_event() -> GroupMessageEvent:
    """真类事件替身：判群聊走 `isinstance`，鸭子类型替身会被判成私聊。"""
    return GroupMessageEvent.model_construct(group_id=1094538078, user_id=10001)


class _FakeBot:
    """只实现被测路径用到的两个 API。

    ⚠️ 记录调用顺序而不只是最终状态：`finish` 会抛异常结束流程，
    顺序错了（先解禁后禁言、或先回复后禁言）在这种替身下才看得出来。
    """

    self_id = "3128682634"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.ban_error: Exception | None = None
        self.member: dict | None = {"card": "阿百川", "nickname": "大鬼"}

    async def get_group_member_info(self, *, group_id: int, user_id: int) -> dict:
        self.calls.append((
            "get_group_member_info",
            {"group_id": group_id, "user_id": user_id},
        ))
        if self.member is None:
            raise RuntimeError("no member")
        return self.member

    async def set_group_ban(
        self, *, group_id: int, user_id: int, duration: int
    ) -> None:
        self.calls.append((
            "set_group_ban",
            {"group_id": group_id, "user_id": user_id, "duration": duration},
        ))
        if self.ban_error is not None:
            raise self.ban_error


@pytest.fixture
def captured_reply(monkeypatch):
    """只拦 `finish()`（拿回复文案），**不拦** `_schedule_unmute`。

    落库/挂任务相关用例必须用它 —— 用 `captured` 会把 `_schedule_unmute`
    整个替换掉，于是「有没有落库」永远测不到（测试自欺）。
    """
    from nonebot.exception import FinishedException

    from nonebot_plugin_lottery import lottery_cmd

    state = SimpleNamespace(messages=[])

    async def fake_finish(message=None, **kwargs):
        state.messages.append(str(message) if message is not None else "")
        raise FinishedException

    monkeypatch.setattr(lottery_cmd, "finish", fake_finish)
    return state


@pytest.fixture
def captured(monkeypatch):
    """拦 `lottery_cmd.finish()`，把回复文案与挂载的定时任务都记下来。

    `finish()` 会抛 `FinishedException`（NoneBot 控制流），所以替身要抛出去，
    否则 handler 会继续往下走 —— 那样测出来的顺序是假的。
    """
    from nonebot.exception import FinishedException

    from nonebot_plugin_lottery import lottery_cmd

    state = SimpleNamespace(messages=[], jobs=[])

    async def fake_finish(message=None, **kwargs):
        state.messages.append(str(message) if message is not None else "")
        raise FinishedException

    monkeypatch.setattr(lottery_cmd, "finish", fake_finish)

    def fake_schedule(bot, group_id, user_id, minutes, *, ban_minutes):
        # ban_minutes 是本次禁言的完整时长（用于算「自然到期」），一并记下来
        state.jobs.append((group_id, user_id, minutes, ban_minutes))

    monkeypatch.setattr("nonebot_plugin_lottery._schedule_unmute", fake_schedule)
    return state


async def _run(
    bot: _FakeBot,
    event: GroupMessageEvent,
    word: str = "抽奖",
    matcher: type[Matcher] | None = None,
) -> None:
    """直接调 handler 的公共实现（跳过 matcher 派发）。

    `matcher` 默认用命令响应器 —— 各 `captured*` fixture 就是 patch 它的 `finish`。
    """
    import nonebot_plugin_lottery as plugin

    with pytest.raises(Exception):  # noqa: B017 - finish() 抛 FinishedException
        await plugin._run_lottery(bot, event, word, matcher or plugin.lottery_cmd)


def _ban_duration(bot: _FakeBot) -> int:
    return next(c for c in bot.calls if c[0] == "set_group_ban")[1]["duration"]


async def test_ban_duration_is_the_rolled_value_not_capped(captured, monkeypatch):
    """**核心用例**：抽到 480 分钟 → `duration=480*60`（真禁 480 分钟）。

    同时应挂一个 10 分钟后的解禁任务。

    这条钉住用户纠正过的语义：
    「不是调用平台 API 禁言 10 分钟，是到时间再调用 API 解禁」。
    若有人把 duration 改成 `min(rolled, 10) * 60`，这里会红。
    """
    import nonebot_plugin_lottery as plugin

    monkeypatch.setattr(plugin, "randint", lambda _a, _b: 480)

    bot = _FakeBot()
    await _run(bot, _group_event())

    assert _ban_duration(bot) == 480 * 60, "禁言时长被改成了抽到的原始值以外的东西"
    assert "480分钟禁言大礼包" in captured.messages[-1], "回复里应报抽到的原始值"
    assert captured.jobs == [(1094538078, 10001, 10, 480)], (
        "超阈值应挂 10 分钟后的解禁任务"
    )


async def test_ban_duration_equals_rolled_for_every_value(captured, monkeypatch):
    """遍历各种抽到的值：duration 恒等于 rolled*60（不许出现任何封顶/截断）。"""
    import nonebot_plugin_lottery as plugin

    for rolled in (1, 9, 10, 11, 60, 479, 480):
        monkeypatch.setattr(plugin, "randint", lambda _a, _b, r=rolled: r)
        bot = _FakeBot()
        await _run(bot, _group_event())
        assert _ban_duration(bot) == rolled * 60, (
            f"抽到 {rolled} 分钟时禁言时长变成 {_ban_duration(bot)}s"
        )


async def test_unmute_job_delay_is_the_threshold_not_the_rolled_value(
    captured, monkeypatch
):
    """解禁任务的**延迟**必须是阈值（10），不是抽到的值（480）。

    写反的话：解禁会在 480 分钟后触发 = 等于没解禁，而且看不出错。
    """
    import nonebot_plugin_lottery as plugin

    monkeypatch.setattr(plugin, "randint", lambda _a, _b: 479)

    bot = _FakeBot()
    await _run(bot, _group_event())

    assert captured.jobs == [(1094538078, 10001, 10, 479)]


async def test_no_unmute_job_when_within_threshold(captured, monkeypatch):
    """抽到 3 分钟（没超阈值）：不挂任务，等平台自然到期。"""
    import nonebot_plugin_lottery as plugin

    monkeypatch.setattr(plugin, "randint", lambda _a, _b: 3)

    bot = _FakeBot()
    await _run(bot, _group_event())

    assert _ban_duration(bot) == 180
    assert captured.jobs == []
    assert "3分钟禁言大礼包" in captured.messages[-1]


async def test_rolled_exactly_at_threshold_schedules_no_job(captured, monkeypatch):
    """抽到 10 = 阈值本身：不算超时，不该挂任务（`>` 写成 `>=` 就会多挂）。"""
    import nonebot_plugin_lottery as plugin

    monkeypatch.setattr(plugin, "randint", lambda _a, _b: 10)

    bot = _FakeBot()
    await _run(bot, _group_event())

    assert _ban_duration(bot) == 600
    assert captured.jobs == []


async def test_rolled_one_above_threshold_schedules_job(captured, monkeypatch):
    """抽到 11 = 阈值 +1：要挂任务（与上一条配对，钉死边界的方向）。"""
    import nonebot_plugin_lottery as plugin

    monkeypatch.setattr(plugin, "randint", lambda _a, _b: 11)

    bot = _FakeBot()
    await _run(bot, _group_event())

    assert _ban_duration(bot) == 660
    assert captured.jobs == [(1094538078, 10001, 10, 11)]


async def test_ban_failure_reports_permission_hint_and_no_job(captured, monkeypatch):
    """禁言失败（bot 不是管理员）：回权限提示，且**不挂**解禁任务。

    原插件文案保持一致：「抽奖禁言失败，可能是机器人没有禁言权限」。
    """
    import nonebot_plugin_lottery as plugin

    monkeypatch.setattr(plugin, "randint", lambda _a, _b: 480)

    bot = _FakeBot()
    bot.ban_error = RuntimeError("机器人不是管理员")
    await _run(bot, _group_event())

    assert captured.messages[-1] == "抽奖禁言失败，可能是机器人没有禁言权限"
    assert captured.jobs == []


async def test_nickname_failure_falls_back_to_qq_and_still_bans(captured, monkeypatch):
    """取昵称失败不能拖垮抽奖：退回 QQ 号，禁言照常。"""
    import nonebot_plugin_lottery as plugin

    monkeypatch.setattr(plugin, "randint", lambda _a, _b: 5)

    bot = _FakeBot()
    bot.member = None  # 让 get_group_member_info 抛
    await _run(bot, _group_event())

    assert [c for c in bot.calls if c[0] == "set_group_ban"], "取不到昵称就不禁言了"
    assert "10001" in captured.messages[-1]


async def test_reply_comes_after_the_ban(captured, monkeypatch):
    """先禁言、后回复（与真寻原插件一致）——顺序反了群里会先看到文案再发现被关。"""
    import nonebot_plugin_lottery as plugin

    monkeypatch.setattr(plugin, "randint", lambda _a, _b: 7)

    bot = _FakeBot()
    await _run(bot, _group_event())

    assert [c[0] for c in bot.calls] == ["get_group_member_info", "set_group_ban"]


# ── 定时解禁任务体 ────────────────────────────────────────────────


async def test_unmute_job_calls_set_group_ban_zero(monkeypatch, tmp_store):
    """任务体必须用 `duration=0` 解禁（OneBot 的解禁约定）。"""
    import nonebot_plugin_lottery as plugin

    bot = _FakeBot()
    monkeypatch.setattr(plugin, "get_bot", lambda _bid: bot, raising=False)

    await plugin._unmute("3128682634", 1094538078, 10001)

    assert bot.calls == [
        ("set_group_ban", {"group_id": 1094538078, "user_id": 10001, "duration": 0})
    ]


async def test_unmute_job_tolerates_bot_unavailable(monkeypatch, tmp_store):
    """Bot 离线/重启后取不到实例：静默放弃，**不能抛**（会污染 apscheduler 日志）。"""
    import nonebot_plugin_lottery as plugin

    def boom(_bid):
        raise RuntimeError("bot not found")

    monkeypatch.setattr(plugin, "get_bot", boom, raising=False)

    await plugin._unmute("3128682634", 1094538078, 10001)  # 不抛即通过


async def test_unmute_job_tolerates_api_failure(monkeypatch, tmp_store):
    """解禁 API 失败也不能抛（有人的禁言可能已被其他插件解开）。"""
    import nonebot_plugin_lottery as plugin

    bot = _FakeBot()
    bot.ban_error = RuntimeError("api 挂了")
    monkeypatch.setattr(plugin, "get_bot", lambda _bid: bot, raising=False)

    await plugin._unmute("3128682634", 1094538078, 10001)  # 不抛即通过


# ── 接住「写了函数没人调」的接线断言 ──────────────────────────────


def test_add_unmute_job_actually_registers_an_apscheduler_job(monkeypatch):
    """`_add_unmute_job` 要真的把任务交给 apscheduler（而不是只 log 一句）。

    ⚠️ handler 相关的用例大多把这个函数或 `_schedule_unmute` 替换掉了（为了稳定），
    所以必须有这条钉住「它自己真的会挂任务」—— 否则整套可能是个空壳。
    """
    import nonebot_plugin_lottery as plugin

    added: list[dict] = []

    class _FakeScheduler:
        def add_job(self, func, **kwargs):
            added.append({"func": func, **kwargs})

    monkeypatch.setattr(plugin, "scheduler", _FakeScheduler())

    when = datetime.now() + timedelta(minutes=10)
    plugin._add_unmute_job("3128682634", 1094538078, 10001, when=when)

    assert len(added) == 1
    job = added[0]
    assert job["func"] is plugin._unmute
    assert job["trigger"] == "date"
    assert job["run_date"] == when, (
        "run_date 必须用传进来的绝对时刻（重启重建才对得上）"
    )
    assert job["args"] == ["3128682634", 1094538078, 10001]
    assert job["id"] == "lottery_unmute_1094538078_10001"
    assert job["replace_existing"] is True
    assert job["misfire_grace_time"] == plugin._MISFIRE_GRACE_SECONDS


def test_schedule_unmute_writes_record_then_schedules(tmp_store, monkeypatch):
    """`_schedule_unmute` = **先落库、再挂任务**，且 unmute_at/expire_at 都算对。

    顺序很重要：先挂任务后落库的话，两步之间进程崩掉就只剩一个内存任务，
    而它随进程一起没了 —— 记录也没留下，等于彻底丢失。
    """
    import nonebot_plugin_lottery as plugin
    from nonebot_plugin_lottery import store

    order: list[str] = []

    real_save = store.save_pending

    def spy_save(**kwargs):
        order.append("save")
        return real_save(**kwargs)

    monkeypatch.setattr(store, "save_pending", spy_save)
    monkeypatch.setattr(
        plugin, "_add_unmute_job", lambda *a, **k: order.append("schedule")
    )

    before = time.time()
    plugin._schedule_unmute(_FakeBot(), 1094538078, 10001, 10, ban_minutes=480)

    assert order == ["save", "schedule"], f"顺序错了：{order}"
    row = store.all_pending()[0]
    assert 590 <= row["unmute_at"] - before <= 610
    assert 480 * 60 - 10 <= row["expire_at"] - before <= 480 * 60 + 10


def test_add_unmute_job_swallows_scheduler_failure(monkeypatch):
    """挂任务失败只 warning —— 记录已在库里，下次启动会重建。"""
    import nonebot_plugin_lottery as plugin

    class _BrokenScheduler:
        def add_job(self, *args, **kwargs):
            raise RuntimeError("scheduler 挂了")

    monkeypatch.setattr(plugin, "scheduler", _BrokenScheduler())

    plugin._add_unmute_job(
        "3128682634", 1094538078, 10001, when=datetime.now()
    )  # 不抛即通过


def test_config_is_actually_used_by_the_handler():
    """Handler 读的是 `plugin_config`，不是写死的常量（含**触发词**）。"""
    import inspect

    import nonebot_plugin_lottery as plugin

    src = inspect.getsource(plugin._run_lottery)
    assert "plugin_config.lottery_min_mute_time" in src
    assert "plugin_config.lottery_max_mute_time" in src
    assert "plugin_config.lottery_unmute_after_minutes" in src
    assert plugin_config.lottery_unmute_after_minutes == 10
    # 触发词也不许再出现硬编码字面量（回归：别把 "抽奖" 写回响应器）
    assert '"抽奖"' not in src


# ── NoneBot 依赖注入：注解必须**运行时可解析** ────────────────────


def test_handler_annotations_resolve_to_real_classes_at_runtime():
    """`Bot` / `GroupMessageEvent` 必须是真 import，不能挪进 TYPE_CHECKING 块。

    NoneBot 靠 handler 的**签名注解**决定注入什么。把这两个类型放进
    `if TYPE_CHECKING:` 里（ruff 的 TC002 会这么建议）时，`get_typed_signature`
    解析出来就不是类对象 → bot/event 注入失败。

    ⚠️ 这类错误**只在生产暴露**（本地直接调 handler 的单元测试全绿），
    所以必须钉一条断言住它。**两个 handler 都要查**（命令 + 关键词）。
    """
    from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
    from nonebot.dependencies import get_typed_signature

    import nonebot_plugin_lottery as plugin

    for handler in (plugin._handle_lottery_command, plugin._handle_lottery_keyword):
        anns = {
            name: prm.annotation
            for name, prm in get_typed_signature(handler).parameters.items()
        }
        assert anns["bot"] is Bot, f"{handler.__name__} 的 bot 注解不是真类"
        assert anns["event"] is GroupMessageEvent, (
            f"{handler.__name__} 的 event 注解不是真类"
        )


def test_handlers_read_the_matched_word_from_nonebot_params():
    """两个 handler 都从 NoneBot 的注入参数取**实际命中的词**（而不是猜/写死）。

    `Command()` → 命中的命令词元组；`Keyword()` → 命中的关键词。
    猜（比如拿配置里第一个词当文案）在配了别名/多关键词时就会显示错词。
    """
    from nonebot.dependencies import get_typed_signature

    import nonebot_plugin_lottery as plugin

    cmd_anns = {
        n: p.annotation
        for n, p in get_typed_signature(
            plugin._handle_lottery_command
        ).parameters.items()
    }
    kw_anns = {
        n: p.annotation
        for n, p in get_typed_signature(
            plugin._handle_lottery_keyword
        ).parameters.items()
    }
    assert cmd_anns["cmd"].__name__ == "tuple"
    assert kw_anns["word"] is str
    # 真实默认值必须是 NoneBot 的**依赖对象**（防被人换成普通默认值/写死取词）。
    # ⚠️ `nonebot.params.Command` / `.Keyword` 是**函数**（不是类）：调用后得到
    # `DependsInner`，真正的取值函数挂在 `.dependency` 上（`_command` / `_keyword`）。
    import inspect

    from nonebot.internal.params import DependsInner

    d_cmd = inspect.signature(plugin._handle_lottery_command).parameters["cmd"].default
    d_kw = inspect.signature(plugin._handle_lottery_keyword).parameters["word"].default
    assert isinstance(d_cmd, DependsInner)
    assert isinstance(d_kw, DependsInner)
    assert d_cmd.dependency.__name__ == "_command"
    assert d_kw.dependency.__name__ == "_keyword"


async def test_reply_text_uses_the_matched_word(captured, monkeypatch):
    """**用户要求**：回复文案里的词跟着配置/实际命中走。

    命中网络梗时文案要写那个梗，而不是恒写「抽奖」。
    """
    import nonebot_plugin_lottery as plugin

    monkeypatch.setattr(plugin, "randint", lambda _a, _b: 3)

    await _run(_FakeBot(), _group_event(), word="自刎归天")

    assert '参与"自刎归天"！' in captured.messages[-1]


async def test_failure_text_uses_the_primary_command_word(captured, monkeypatch):
    """失败文案用**主命令词**（这句在说功能失败，不是在选择触发词）。

    默认配置下必须与旧版**逐字一致**，别因为这次改造把老文案改掉。
    """
    import nonebot_plugin_lottery as plugin

    monkeypatch.setattr(plugin, "randint", lambda _a, _b: 480)

    bot = _FakeBot()
    bot.ban_error = RuntimeError("机器人不是管理员")
    await _run(bot, _group_event(), word="自刎归天")

    assert captured.messages[-1] == "抽奖禁言失败，可能是机器人没有禁言权限"


# ── 容灾：抽奖命中时必须**落库**（不只是挂内存任务）─────────────────


async def test_lottery_persists_pending_unmute(captured_reply, monkeypatch, tmp_path):
    """抽到超阈值时：写一条待解禁记录，且 `unmute_at`/`expire_at` 都要对。

    只挂内存任务不落库 = 重启即丢，容灾要求不满足（用户明确要求持久化）。
    """
    from nonebot_plugin_lottery import store

    store.init(tmp_path / "lottery.sqlite3")
    import nonebot_plugin_lottery as plugin

    monkeypatch.setattr(plugin, "randint", lambda _a, _b: 480)

    before = time.time()
    bot = _FakeBot()
    await _run(bot, _group_event())

    rows = store.all_pending()
    assert len(rows) == 1, "抽奖命中后没有落库 → 重启就丢"
    row = rows[0]
    assert (row["bot_id"], row["group_id"], row["user_id"]) == (
        "3128682634",
        1094538078,
        10001,
    )
    # unmute_at ≈ 现在 + 10 分钟；expire_at ≈ 现在 + 480 分钟
    assert 590 <= row["unmute_at"] - before <= 610
    assert 480 * 60 - 10 <= row["expire_at"] - before <= 480 * 60 + 10


async def test_lottery_does_not_persist_when_within_threshold(
    captured, monkeypatch, tmp_path
):
    """没超阈值：不落库、不挂任务（平台自然到期即可，别留垃圾记录）。"""
    from nonebot_plugin_lottery import store

    store.init(tmp_path / "lottery.sqlite3")
    import nonebot_plugin_lottery as plugin

    monkeypatch.setattr(plugin, "randint", lambda _a, _b: 3)

    await _run(_FakeBot(), _group_event())

    assert store.count() == 0


async def test_lottery_does_not_persist_when_ban_failed(
    captured, monkeypatch, tmp_path
):
    """禁言失败（不是管理员）：**不能**留待解禁记录。

    否则启动时会去解一个根本没下过的禁言。
    """
    from nonebot_plugin_lottery import store

    store.init(tmp_path / "lottery.sqlite3")
    import nonebot_plugin_lottery as plugin

    monkeypatch.setattr(plugin, "randint", lambda _a, _b: 480)

    bot = _FakeBot()
    bot.ban_error = RuntimeError("不是管理员")
    await _run(bot, _group_event())

    assert store.count() == 0


# ── 容灾：解禁成功/失败时记录的去留 ───────────────────────────────


async def test_unmute_success_deletes_record(monkeypatch, tmp_path):
    from nonebot_plugin_lottery import store

    store.init(tmp_path / "lottery.sqlite3")
    now = time.time()
    store.save_pending(
        bot_id="3128682634",
        group_id=1094538078,
        user_id=10001,
        unmute_at=now,
        expire_at=now + 3600,
    )

    import nonebot_plugin_lottery as plugin

    bot = _FakeBot()
    monkeypatch.setattr(plugin, "get_bot", lambda _bid: bot, raising=False)

    await plugin._unmute("3128682634", 1094538078, 10001)

    assert store.count() == 0, "解禁成功应清掉记录，否则表会无限增长"


async def test_unmute_failure_keeps_record_and_reschedules(monkeypatch, tmp_path):
    """**容灾核心**：解禁失败时记录必须留着，并重排一次重试。

    上一版「失败就 return」会留下一条永远没人再碰的记录：表里看着有任务，
    实际再也不会解禁 —— 比丢任务更隐蔽。
    """
    from nonebot_plugin_lottery import store

    store.init(tmp_path / "lottery.sqlite3")
    now = time.time()
    store.save_pending(
        bot_id="3128682634",
        group_id=1094538078,
        user_id=10001,
        unmute_at=now,
        expire_at=now + 3600,
    )

    import nonebot_plugin_lottery as plugin

    rescheduled: list[dict] = []

    def fake_add(bot_id, group_id, user_id, *, when):
        rescheduled.append({"when": when})

    monkeypatch.setattr(plugin, "_add_unmute_job", fake_add)

    bot = _FakeBot()
    bot.ban_error = RuntimeError("API 抖动")
    monkeypatch.setattr(plugin, "get_bot", lambda _bid: bot, raising=False)

    await plugin._unmute("3128682634", 1094538078, 10001)

    assert store.count() == 1, "失败时记录不能删（否则再也不会重试）"
    assert store.all_pending()[0]["attempts"] == 1
    assert len(rescheduled) == 1, "失败后应重排一次重试"
    delta = rescheduled[0]["when"].timestamp() - time.time()
    assert 50 <= delta <= 70, f"重试间隔应约 60 秒，实际 {delta}s"


async def test_unmute_bot_unavailable_keeps_record_and_reschedules(
    monkeypatch, tmp_path
):
    """Bot 取不到（离线中）同样是「这一刻放不出去」，不能删记录。"""
    from nonebot_plugin_lottery import store

    store.init(tmp_path / "lottery.sqlite3")
    now = time.time()
    store.save_pending(
        bot_id="3128682634",
        group_id=1094538078,
        user_id=10001,
        unmute_at=now,
        expire_at=now + 3600,
    )

    import nonebot_plugin_lottery as plugin

    def boom(_bid):
        raise RuntimeError("bot offline")

    monkeypatch.setattr(plugin, "get_bot", boom, raising=False)
    monkeypatch.setattr(plugin, "_add_unmute_job", lambda *a, **k: None)

    await plugin._unmute("3128682634", 1094538078, 10001)  # 不抛即通过

    assert store.count() == 1


async def test_unmute_gives_up_after_max_attempts(monkeypatch, tmp_path):
    """重试到上限就放弃并删记录（否则 bot 长期离线会无限重试刷日志）。"""
    from nonebot_plugin_lottery import store

    store.init(tmp_path / "lottery.sqlite3")
    now = time.time()
    store.save_pending(
        bot_id="3128682634",
        group_id=1094538078,
        user_id=10001,
        unmute_at=now,
        expire_at=now + 3600,
    )

    import nonebot_plugin_lottery as plugin

    for _ in range(plugin._MAX_UNMUTE_ATTEMPTS):
        store.bump_attempts(1094538078, 10001)

    rescheduled: list[dict] = []
    monkeypatch.setattr(
        plugin, "_add_unmute_job", lambda *a, **k: rescheduled.append(k)
    )

    def boom(_bid):
        raise RuntimeError("bot offline")

    monkeypatch.setattr(plugin, "get_bot", boom, raising=False)

    await plugin._unmute("3128682634", 1094538078, 10001)

    assert store.count() == 0, "超过重试上限应清掉记录"
    assert rescheduled == [], "放弃时不该再排重试"
