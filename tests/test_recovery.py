"""容灾恢复测试：模拟**进程重启**后任务是否真的被重建。

与 `test_persistence.py`（只测 store 的增删查）分开：这里测的是
「重启这条路径」端到端 —— 落库 → 内存清空 → 启动钩子把任务捞回来。

模拟重启的办法：
  - 调度器用一个 **记录型替身**（内存那部分天然就是「丢了再重建」）；
  - store 指向同一个临时文件（磁盘部分保留）；
  - 直接调 `restore_pending_from_store()`（就是 `_on_startup` 的核心步骤）。
"""

from __future__ import annotations

from datetime import timezone
import time
from types import SimpleNamespace

import pytest

from nonebot_plugin_lottery import plugin_config, restore_pending_from_store, store


@pytest.fixture
def jobs(monkeypatch):
    """记录型调度器替身：模拟「重启后内存任务全丢，只剩磁盘真相」。"""
    from nonebot_plugin_lottery import _add_unmute_job  # noqa: F401  确保模块已载

    recorded: list[dict] = []

    def fake_add(bot_id, group_id, user_id, *, when):
        recorded.append({
            "bot_id": bot_id,
            "group_id": group_id,
            "user_id": user_id,
            "when": when,
        })

    monkeypatch.setattr("nonebot_plugin_lottery._add_unmute_job", fake_add)
    return recorded


@pytest.fixture
def utc_scheduler(monkeypatch):
    """把调度器时区换成 UTC —— 故意与「服务器本地时区」不一致。

    用来证明重建出的时间是**跟随调度器时区**的绝对值，而不是依赖
    「服务器本地时区恰好等于 apscheduler 配置的时区」这个巧合。
    """
    import nonebot_plugin_lottery as plugin

    fake = SimpleNamespace(timezone=timezone.utc)
    monkeypatch.setattr(plugin, "scheduler", fake)
    return fake


async def test_restored_datetimes_are_timezone_aware(jobs, tmp_store, utc_scheduler):
    """重建出的时间必须**带调度器时区**，且绝对时刻与记录一致。

    变异检验对象：把恢复写成 ``datetime.fromtimestamp(unmute_at)``（naive）。
    naive 时间会被 apscheduler 按它自己的时区本地化 —— 生产机时区恰好是
    Asia/Shanghai 时看不出问题，一旦服务器改成 UTC 就会整体偏移 8 小时，
    属于「只能事后复盘才发现」的故障。带时区就没有这个歧义。
    """
    now = time.time()
    store.save_pending(
        bot_id="3128682634",
        group_id=1094538078,
        user_id=10001,
        unmute_at=now + 37 * 60,
        expire_at=now + 480 * 60,
    )
    store.save_pending(
        bot_id="3128682634",
        group_id=1094538078,
        user_id=10002,
        unmute_at=now - 120,  # 已错过 → 立刻执行
        expire_at=now + 480 * 60,
    )

    await restore_pending_from_store()

    by_user = {j["user_id"]: j["when"] for j in jobs}
    for uid in (10001, 10002):
        when = by_user[uid]
        assert when.tzinfo is not None, f"user={uid} 的时间丢了时区 → 会被本地化挪走"
        assert when.tzinfo == timezone.utc, f"user={uid} 用的不是调度器时区"
    assert abs(by_user[10001].timestamp() - (now + 37 * 60)) < 5, (
        "绝对时刻必须与记录一致"
    )
    assert by_user[10002].timestamp() <= time.time() + 2


async def test_restore_rebuilds_future_job_at_original_time(jobs, tmp_store):
    """未到期的记录：按**原时刻**重建（不是「现在+阈值」，否则会越重启越晚）。

    ⚠️ 特意用 **37 分钟**这种不规则偏移，而不是阈值（10 分钟）：
    用 10 分钟的话，「按原时刻」与「按 now+阈值」算出来**恰好相同**，
    断言就成了空壳 —— 实测把恢复改成 `now + timedelta(minutes=10)` 时
    测试仍全绿（变异 M16 漏网），故改为不规则值。
    """
    unmute_at = time.time() + 37 * 60
    store.save_pending(
        bot_id="3128682634",
        group_id=1094538078,
        user_id=10001,
        unmute_at=unmute_at,
        expire_at=time.time() + 480 * 60,
    )

    await restore_pending_from_store()

    assert len(jobs) == 1
    job = jobs[0]
    assert (job["bot_id"], job["group_id"], job["user_id"]) == (
        "3128682634",
        1094538078,
        10001,
    )
    # 允许少量误差（构造与执行之间会走几毫秒）
    assert abs(job["when"].timestamp() - unmute_at) < 5


async def test_restore_runs_overdue_job_immediately(jobs, tmp_store):
    """**停机期间错过**的解禁：重启后立刻执行，而不是丢掉。

    这条是容灾的核心价值：bot 停 3 分钟，用户不该多被关那 3 分钟。
    """
    now = time.time()
    store.save_pending(
        bot_id="3128682634",
        group_id=1094538078,
        user_id=10001,
        unmute_at=now - 120,  # 2 分钟前就该解禁（那时进程不在）
        expire_at=now + 480 * 60,  # 但禁言还没自然到期
    )

    before = time.time()
    await restore_pending_from_store()

    assert len(jobs) == 1
    # 必须是「现在」执行，不能是把已经过去的时刻原样交给 apscheduler。
    # ⚠️ 用 epoch（timestamp）比较，不用 datetime 直接比较：
    #    run_date 是**带时区**的（跟随调度器时区），datetime.now() 是 naive，
    #    直接比会 TypeError，而且时区不同的机器上结论也不一样。
    assert jobs[0]["when"].timestamp() >= before
    assert jobs[0]["when"].timestamp() <= time.time() + 1
    assert store.count() == 1, "记录不该在重建阶段被删（解禁成功后才删）"


async def test_restore_cleans_up_naturally_expired_records(jobs, tmp_store):
    """禁言都已自然到期：**不再调 API**，只把记录清掉。

    bot 停机几小时再重启时，那些禁言平台早解开了，再去调解禁 API 是无用调用
    （还会在群里/日志里制造噪音）。
    """
    past = time.time() - 3600
    store.save_pending(
        bot_id="3128682634",
        group_id=1094538078,
        user_id=10001,
        unmute_at=past,
        expire_at=past - 60,  # 连自然到期都过了
    )

    await restore_pending_from_store()

    assert jobs == [], "已自然到期的记录不该再挂任务"
    assert store.count() == 0, "应清理掉"


async def test_restore_handles_mixed_batch(jobs, tmp_store):
    """混合批次：三种状态各归各位，一次启动全部处理正确。"""
    now = time.time()
    store.save_pending(  # ① 未到期 → 重建
        bot_id="B", group_id=1, user_id=11, unmute_at=now + 300, expire_at=now + 3600
    )
    store.save_pending(  # ② 已错过但未自然到期 → 立刻
        bot_id="B", group_id=1, user_id=22, unmute_at=now - 30, expire_at=now + 3600
    )
    store.save_pending(  # ③ 已自然到期 → 清理
        bot_id="B", group_id=1, user_id=33, unmute_at=now - 7200, expire_at=now - 3600
    )

    await restore_pending_from_store()

    scheduled = sorted(j["user_id"] for j in jobs)
    assert scheduled == [11, 22], "只该给 ① ② 挂任务"
    assert store.count() == 2, "③ 应被清理，① ② 保留到解禁成功"
    # ② 是立刻执行，① 在未来（同样用 epoch 比较，避免时区问题）
    by_user = {j["user_id"]: j["when"].timestamp() for j in jobs}
    assert by_user[22] <= time.time() + 1
    assert by_user[11] > time.time()


async def test_restore_with_empty_table_schedules_nothing(jobs, tmp_store):
    """空表（绝大多数重启都是这种情况）：什么都不做，不报错。"""
    await restore_pending_from_store()
    assert jobs == []


async def test_restore_reuses_persisted_bot_id(jobs, tmp_store):
    """重建时用的是记录里的 bot_id，不是当前 bot 的 —— 停机期间换了实例也能对准。"""
    now = time.time()
    store.save_pending(
        bot_id="9999999999",
        group_id=1,
        user_id=2,
        unmute_at=now + 300,
        expire_at=now + 3600,
    )
    await restore_pending_from_store()
    assert jobs[0]["bot_id"] == "9999999999"


def test_persistence_switch_defaults_to_on():
    """容灾持久化默认**开启**（用户要求的「需做容灾持久化」）。"""
    assert plugin_config.lottery_persist_pending is True


def test_startup_hook_registered_only_when_enabled():
    """启动钩子确实挂上了 `driver.on_startup`（否则重建逻辑永远不会跑）。

    这是「写了函数没人调」的典型陷阱：`restore_pending_from_store()` 有单测、
    函数也在，但从没被启动流程调用过 —— 容灾就是假的。

    ⚠️ NoneBot 把 startup 钩子存在 `driver._lifespan._startup_funcs`，
    **不是** `driver._startup_funcs`（先查清楚再断言，别凭印象写属性名）。
    """
    from nonebot import get_driver

    if not plugin_config.lottery_persist_pending:
        pytest.skip("当前配置关闭了持久化，钩子本就不该注册")

    from nonebot_plugin_lottery import _on_startup

    startup_funcs = get_driver()._lifespan._startup_funcs  # type: ignore[attr-defined]
    assert any(f is _on_startup for f in startup_funcs), (
        "启动钩子没注册 → 重启后不会重建解禁任务（容灾失效）"
    )
