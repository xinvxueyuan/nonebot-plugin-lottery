"""容灾持久化测试：**重启之后解禁任务不能丢**。

这是用户 2026-10-02 明确要求的「需做容灾持久化」。核心断言：

  ① 抽奖命中时，待解禁记录**落库**（不是只挂内存任务）；
  ② 重启（进程内存全丢、只留磁盘）后，能从库里**重建**任务；
  ③ 停机期间错过的解禁 → 重启后**立刻**执行（而不是被丢掉）；
  ④ 禁言已自然到期的记录 → 重启时**清理掉**，不去调无用的解禁 API；
  ⑤ 解禁失败 → 记录**仍在**（累加 attempts）并在 60 秒后重试；
  ⑥ 解禁成功 → 记录被删（不留垃圾）。

⚠️ 测试里所有「时间」都用相对当前时间的偏移构造，别写死绝对时间戳。
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import time

import pytest

from nonebot_plugin_lottery import store

# ── store：建表 / 覆盖 / 删除 / 计数 ──────────────────────────────


def test_init_creates_table(tmp_store):
    conn = sqlite3.connect(tmp_store)
    names = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "lottery_pending_unmute" in names
    conn.close()


def test_save_and_read_back(tmp_store):
    now = time.time()
    store.save_pending(
        bot_id="3128682634",
        group_id=1094538078,
        user_id=10001,
        unmute_at=now + 600,
        expire_at=now + 480 * 60,
    )
    rows = store.all_pending()
    assert len(rows) == 1
    row = rows[0]
    assert (row["bot_id"], row["group_id"], row["user_id"]) == (
        "3128682634",
        1094538078,
        10001,
    )
    assert row["attempts"] == 0


def test_same_user_twice_overwrites_not_appends(tmp_store):
    """同一人二次抽奖 = 覆盖旧记录（新禁言覆盖旧禁言），不是叠两条。"""
    now = time.time()
    for offset in (600, 900):
        store.save_pending(
            bot_id="3128682634",
            group_id=1094538078,
            user_id=10001,
            unmute_at=now + offset,
            expire_at=now + 480 * 60,
        )
    assert store.count() == 1
    assert store.all_pending()[0]["unmute_at"] == pytest.approx(now + 900)


def test_remove_and_count(tmp_store):
    now = time.time()
    store.save_pending(
        bot_id="1", group_id=2, user_id=3, unmute_at=now + 60, expire_at=now + 3600
    )
    assert store.count() == 1
    assert store.remove_pending(2, 3) is True
    assert store.count() == 0
    assert store.remove_pending(2, 3) is False  # 再删返回 False，不报错


def test_bump_attempts_increments(tmp_store):
    now = time.time()
    store.save_pending(
        bot_id="1", group_id=2, user_id=3, unmute_at=now + 60, expire_at=now + 3600
    )
    assert store.bump_attempts(2, 3) == 1
    assert store.bump_attempts(2, 3) == 2
    assert store.bump_attempts(999, 999) == 0  # 记录不存在时返回 0，不抛


def test_bump_attempts_resets_on_new_lottery(tmp_store):
    """二次抽奖要把 attempts 归零 —— 否则老的重试计数会让新任务立刻被放弃。"""
    now = time.time()
    store.save_pending(
        bot_id="1", group_id=2, user_id=3, unmute_at=now + 60, expire_at=now + 3600
    )
    store.bump_attempts(2, 3)
    store.bump_attempts(2, 3)
    store.save_pending(
        bot_id="1", group_id=2, user_id=3, unmute_at=now + 600, expire_at=now + 7200
    )
    assert store.all_pending()[0]["attempts"] == 0


def test_uninitialized_store_raises_rather_than_silently_noop(monkeypatch):
    """没 init 就调用要抛错 —— 静默 noop 会让「容灾」变成假象。"""
    monkeypatch.setattr(store, "_db_path", None)
    with pytest.raises(RuntimeError, match=r"store\.init"):
        store.all_pending()


# ── 生产库路径：必须用 localstore 的**显式命名** API ──────────────


def test_default_db_path_uses_explicit_plugin_name(monkeypatch):
    """`default_db_path()` 不能依赖 localstore 的「调用栈猜插件」。

    `get_plugin_data_file()` 内部是 `_try_get_caller_plugin()`，猜不中就抛
    `RuntimeError("Cannot detect caller plugin")` —— 而插件里这个异常会被
    `_ensure_store` 吞掉只打日志，于是**容灾静默失效**、群里表现完全正常。
    所以必须走 `get_data_file(<显式名>, ...)`。
    """
    import nonebot_plugin_localstore as localstore

    called: list[tuple[str, str]] = []

    def fake_get_data_file(plugin_name, filename):
        called.append((plugin_name, filename))
        return Path("/tmp") / plugin_name / filename

    monkeypatch.setattr(localstore, "get_data_file", fake_get_data_file)

    def forbidden():  # pragma: no cover - 走到这里就说明用错了 API
        raise AssertionError("不该调用靠栈推断的 get_plugin_data_file()")

    monkeypatch.setattr(localstore, "get_plugin_data_file", forbidden, raising=False)

    path = store.default_db_path()

    assert called == [("nonebot_plugin_lottery", "lottery.sqlite3")]
    assert path.name == "lottery.sqlite3"
