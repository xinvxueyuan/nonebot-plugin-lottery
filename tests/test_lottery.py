"""抽奖（禁言小助手）测试。

重点覆盖用户 2026-10-02 追加的那条要求：
**「原样但超 10 分钟的定时自动解禁（隐性）」** —— 它有三个可独立测的断言：
  ① 回复里报的是**抽到的原始数字**（隐性：不透露封顶）；
  ② 实际 `set_group_ban(duration=)` 封顶到 10 分钟；
  ③ 抽中超时值时，**才**挂定时解禁任务。

② 与 ③ 是**两层**保障，必须都测到：只靠定时任务的话，qbot 重启（这台机器 3 天
重启过 12 次）任务就丢了，被抽到 480 分钟的人会被真关 8 小时。
"""

from __future__ import annotations

from types import SimpleNamespace

from nonebot.adapters.onebot.v11 import GroupMessageEvent
import pytest

from nonebot_plugin_lottery import pick_mute_minutes, plugin_config
from nonebot_plugin_lottery.config import Config

# ── 纯函数：随机 + 封顶 ────────────────────────────────────────────


def test_rolled_value_inside_range():
    for _ in range(200):
        rolled, _actual = pick_mute_minutes(1, 480, 10)
        assert 1 <= rolled <= 480


def test_actual_is_capped_but_rolled_is_not():
    """**核心**：报出来的不封顶、实际禁言封顶。这就是「隐性」。"""
    for _ in range(200):
        rolled, actual = pick_mute_minutes(1, 480, 10)
        assert actual == min(rolled, 10)
        assert actual <= 10


def test_values_within_cap_are_untouched():
    """抽到的数没超上限时，实际时长与抽到的一致（不改原有手感）。"""
    for _ in range(200):
        rolled, actual = pick_mute_minutes(1, 10, 10)
        assert actual == rolled


def test_cap_larger_than_max_has_no_effect():
    """上限比 MAX 还大 = 封顶不生效（配置成 999 分钟时的预期行为）。"""
    for _ in range(100):
        rolled, actual = pick_mute_minutes(1, 60, 999)
        assert actual == rolled


def test_deterministic_rng_is_used():
    """注入 rng 后结果可预测 —— 便于断言边界（而不是靠随机碰运气）。"""
    rolled, actual = pick_mute_minutes(1, 480, 10, rng=lambda _a, _b: 480)
    assert (rolled, actual) == (480, 10)

    rolled, actual = pick_mute_minutes(1, 480, 10, rng=lambda _a, _b: 1)
    assert (rolled, actual) == (1, 1)

    # 正好等于上限：不算超时，不该挂定时任务（边界，差一就错）
    rolled, actual = pick_mute_minutes(1, 480, 10, rng=lambda _a, _b: 10)
    assert (rolled, actual) == (10, 10)


def test_min_equal_max():
    rolled, actual = pick_mute_minutes(5, 5, 10)
    assert (rolled, actual) == (5, 5)


def test_rng_is_called_with_the_configured_bounds():
    seen: list[tuple[int, int]] = []

    def spy(a: int, b: int) -> int:
        seen.append((a, b))
        return a

    pick_mute_minutes(3, 77, 10, rng=spy)
    assert seen == [(3, 77)]


# ── 配置：默认值 / 写反了要自动对调 ────────────────────────────────


def test_default_config_matches_upstream_plugin():
    """默认值必须与真寻原插件一致（MIN=1 / MAX=480）。"""
    cfg = Config()
    assert cfg.lottery_min_mute_time == 1
    assert cfg.lottery_max_mute_time == 480
    assert cfg.lottery_mute_cap_minutes == 10


def test_inverted_range_is_swapped_not_raised():
    """MIN > MAX 时自动对调，而不是让插件起不来/发牌时炸 randint。"""
    cfg = Config(lottery_min_mute_time=480, lottery_max_mute_time=1)
    assert cfg.lottery_min_mute_time == 1
    assert cfg.lottery_max_mute_time == 480


def test_cap_must_be_positive():
    with pytest.raises(ValueError):
        Config(lottery_mute_cap_minutes=0)


# ── 插件确实加载了（import 期没有静默降级）─────────────────────────


def test_plugin_metadata_names_the_upstream_author():
    from nonebot_plugin_lottery import __plugin_meta__

    extra = __plugin_meta__.extra or {}
    assert extra.get("author") == "阿珏酱"  # 原作者，抽取来源可追溯
    assert extra.get("upstream", "").endswith("plugins/lottery")
    assert __plugin_meta__.name == "抽奖"


def _registered_commands(matcher) -> set[str]:
    """从 matcher 的 rule 里取出注册的命令字串。

    NoneBot2 把命令放在 `matcher.rule.checkers` 里的 `Dependent.call`（`Command`）上，
    `cmds` 是**元组嵌套**（`(("抽奖",),)`，内层是 aliases）—— 直接 `in rule.commands`
    会 AttributeError（这个我第一版就写错了）。
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


def test_command_is_registered():
    """`抽奖` 命令真的注册上了（裸命令，qbot 的 COMMAND_START 含空串）。"""
    from nonebot_plugin_lottery import lottery_cmd

    assert "抽奖" in _registered_commands(lottery_cmd)
    assert lottery_cmd.block is True


# ── handler：三态行为（报原始值 / 封顶 / 挂任务）────────────────────


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

    from nonebot_plugin_lottery import _schedule_unmute

    def fake_schedule(bot, group_id, user_id, minutes):
        state.jobs.append((group_id, user_id, minutes))

    monkeypatch.setattr("nonebot_plugin_lottery._schedule_unmute", fake_schedule)
    del _schedule_unmute
    return state


async def _run(bot: _FakeBot, event: GroupMessageEvent) -> None:
    """直接调 handler 函数体（跳过 matcher 派发）。"""
    import nonebot_plugin_lottery as plugin

    handler = plugin._handle_lottery
    with pytest.raises(Exception):  # noqa: B017 - finish() 抛 FinishedException
        await handler(bot, event)


async def test_handler_bans_with_capped_duration_and_reports_rolled(
    captured, monkeypatch
):
    """**核心用例**：报 480 分钟，实际只禁言 10 分钟，并且挂了定时解禁。

    把封顶只做在定时任务上（不改 duration）时，这条会红 —— 那正是
    「bot 一重启就变成真关 8 小时」的写法。
    """
    monkeypatch.setattr(
        "nonebot_plugin_lottery.randint", lambda _a, _b: 480, raising=False
    )
    import nonebot_plugin_lottery as plugin

    monkeypatch.setattr(plugin, "randint", lambda _a, _b: 480)

    bot = _FakeBot()
    await _run(bot, _group_event())

    ban = next((c for c in bot.calls if c[0] == "set_group_ban"), None)
    assert ban is not None, "没有调用 set_group_ban"
    assert ban[1]["duration"] == 600, "实际禁言没封顶到 10 分钟"

    assert "480分钟禁言大礼包" in captured.messages[-1], "回复里报的应是抽到的原始值"
    assert captured.jobs == [(1094538078, 10001, 10)], "超时值没有挂定时解禁"


async def test_handler_skips_unmute_job_when_within_cap(captured, monkeypatch):
    """抽到 3 分钟：不挂定时任务（平台自己到期就解了）。"""
    import nonebot_plugin_lottery as plugin

    monkeypatch.setattr(plugin, "randint", lambda _a, _b: 3)

    bot = _FakeBot()
    await _run(bot, _group_event())

    assert next(c for c in bot.calls if c[0] == "set_group_ban")[1]["duration"] == 180
    assert captured.jobs == []
    assert "3分钟禁言大礼包" in captured.messages[-1]


async def test_handler_never_exceeds_the_cap_even_when_rolled_max(
    captured, monkeypatch
):
    """遍历抽到的各种值，实际 duration 永不超过封顶（防差一错误）。"""
    import nonebot_plugin_lottery as plugin

    for rolled in (1, 9, 10, 11, 60, 479, 480):
        monkeypatch.setattr(plugin, "randint", lambda _a, _b, r=rolled: r)
        bot = _FakeBot()
        await _run(bot, _group_event())
        duration = next(c for c in bot.calls if c[0] == "set_group_ban")[1]["duration"]
        assert duration <= 600, f"抽到 {rolled} 分钟时实际禁言 {duration}s 超了封顶"


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


async def test_unmute_job_calls_set_group_ban_zero(monkeypatch):
    """任务体必须用 `duration=0` 解禁（OneBot 的解禁约定）。"""
    import nonebot_plugin_lottery as plugin

    bot = _FakeBot()
    monkeypatch.setattr(plugin, "get_bot", lambda _bid: bot, raising=False)

    await plugin._unmute("3128682634", 1094538078, 10001)

    assert bot.calls == [
        ("set_group_ban", {"group_id": 1094538078, "user_id": 10001, "duration": 0})
    ]


async def test_unmute_job_tolerates_bot_unavailable(monkeypatch):
    """Bot 离线/重启后取不到实例：静默放弃，**不能抛**（会污染 apscheduler 日志）。"""
    import nonebot_plugin_lottery as plugin

    def boom(_bid):
        raise RuntimeError("bot not found")

    monkeypatch.setattr(plugin, "get_bot", boom, raising=False)

    await plugin._unmute("3128682634", 1094538078, 10001)  # 不抛即通过


async def test_unmute_job_tolerates_api_failure(monkeypatch):
    """解禁 API 失败也不能抛（有人的禁言可能已被其他插件解开）。"""
    import nonebot_plugin_lottery as plugin

    bot = _FakeBot()
    bot.ban_error = RuntimeError("api 挂了")
    monkeypatch.setattr(plugin, "get_bot", lambda _bid: bot, raising=False)

    await plugin._unmute("3128682634", 1094538078, 10001)  # 不抛即通过


# ── 接住「写了函数没人调」的接线断言 ──────────────────────────────


def test_schedule_unmute_actually_registers_an_apscheduler_job(monkeypatch):
    """`_schedule_unmute` 要真的把任务交给 apscheduler（而不是只 log 一句）。

    上面那些 handler 用例都把这个函数替换掉了（为了稳定），
    所以必须有这条来钉住「它自己真的会挂任务」——否则整体可能是个空壳。
    """
    import nonebot_plugin_lottery as plugin

    added: list[dict] = []

    class _FakeScheduler:
        def add_job(self, func, **kwargs):
            added.append({"func": func, **kwargs})

    monkeypatch.setattr(plugin, "scheduler", _FakeScheduler())

    bot = _FakeBot()
    plugin._schedule_unmute(bot, 1094538078, 10001, 10)

    assert len(added) == 1
    job = added[0]
    assert job["func"] is plugin._unmute
    assert job["trigger"] == "date"
    assert job["args"] == ["3128682634", 1094538078, 10001]
    assert job["id"] == "lottery_unmute_1094538078_10001"
    assert job["replace_existing"] is True
    # run_date 应在 ~10 分钟后（同一用户二次抽奖要覆盖旧任务，不能叠加）
    from datetime import datetime

    delta = (job["run_date"] - datetime.now()).total_seconds()
    assert 570 <= delta <= 600, f"解禁时间不对：{delta}s"


def test_schedule_unmute_swallows_scheduler_failure(monkeypatch):
    """挂任务失败只 warning，不影响已生效的封顶禁言。"""
    import nonebot_plugin_lottery as plugin

    class _BrokenScheduler:
        def add_job(self, *args, **kwargs):
            raise RuntimeError("scheduler 挂了")

    monkeypatch.setattr(plugin, "scheduler", _BrokenScheduler())

    plugin._schedule_unmute(_FakeBot(), 1094538078, 10001, 10)  # 不抛即通过


def test_config_is_actually_used_by_the_handler():
    """Handler 读的是 `plugin_config`，不是写死的常量。"""
    import inspect

    import nonebot_plugin_lottery as plugin

    src = inspect.getsource(plugin._handle_lottery)
    assert "plugin_config.lottery_min_mute_time" in src
    assert "plugin_config.lottery_max_mute_time" in src
    assert "plugin_config.lottery_mute_cap_minutes" in src
    assert plugin_config.lottery_mute_cap_minutes == 10


# ── NoneBot 依赖注入：注解必须**运行时可解析** ────────────────────


def test_handler_annotations_resolve_to_real_classes_at_runtime():
    """`Bot` / `GroupMessageEvent` 必须是真 import，不能挪进 TYPE_CHECKING 块。

    NoneBot 靠 handler 的**签名注解**决定注入什么。把这两个类型放进
    `if TYPE_CHECKING:` 里（ruff 的 TC002 会这么建议）时，`get_typed_signature`
    解析出来就不是类对象 → bot/event 注入失败。

    ⚠️ 这类错误**只在生产暴露**（本地直接调 handler 的单元测试全绿），
    所以必须钉一条断言住它。
    """
    from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
    from nonebot.dependencies import get_typed_signature

    import nonebot_plugin_lottery as plugin

    anns = {
        name: prm.annotation
        for name, prm in get_typed_signature(plugin._handle_lottery).parameters.items()
    }
    assert anns["bot"] is Bot
    assert anns["event"] is GroupMessageEvent


# ── 边界：抽到的数**正好等于**上限 ────────────────────────────────


async def test_rolled_exactly_at_cap_schedules_no_job(captured, monkeypatch):
    """抽到 10 = 上限本身：不算超时，不该挂任务（`>` 写成 `>=` 就会多挂）。

    多挂一个任务本身无害，但它说明判据写错了 —— 而同一个判据将来一旦被
    复用（比如改成「超时就不禁言」），差一错误就会变成功能错误。
    """
    import nonebot_plugin_lottery as plugin

    monkeypatch.setattr(plugin, "randint", lambda _a, _b: 10)

    bot = _FakeBot()
    await _run(bot, _group_event())

    assert next(c for c in bot.calls if c[0] == "set_group_ban")[1]["duration"] == 600
    assert captured.jobs == []
    assert "10分钟禁言大礼包" in captured.messages[-1]
