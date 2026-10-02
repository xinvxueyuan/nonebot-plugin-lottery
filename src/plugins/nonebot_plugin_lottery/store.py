"""待解禁任务的持久化（容灾的关键）。

## 为什么需要这张表

apscheduler 的任务默认只在**进程内存**里（`MemoryJobStore`）。bot 一重启
（本生产机 3 天重启过 12 次），等着解禁的任务就全没了 —— 被抽到 480 分钟的
群友会被真关 8 小时，而日志里什么都看不出来。

## 为什么不用 apscheduler 自带的持久化 jobstore

`SQLAlchemyJobStore` 靠 pickle 保存**函数的引用串**，恢复时 `__import__(模块名)`。
宿主 qbot 用 `nonebot.load_from_toml()` 加载 vendor 下的插件，模块名是从路径
推导出来的**合成名**，带连字符：

    vendor.nonebot-plugin-lottery.src.plugins.nonebot_plugin_lottery

连字符不是合法标识符 → `__import__` 必然失败。实测（2026-10-02）：

    ref_to_obj("vendor.nonebot-plugin-lottery.src.plugins.nonebot_plugin_lottery:_unmute")
    -> LookupError: Error resolving reference ...: could not import module

**即使 jobstore 写成功了，重启后也一定恢复不回来**。所以：

    调度  = apscheduler（nonebot-plugin-apscheduler）
    真相  = 本表（stdlib sqlite3，零新依赖）
    启动  = 从本表读出未完成记录 → 重建 apscheduler 任务

这样即使任务丢了、bot 被 kill、机器重启，**禁言一定会被解开**。

## 表结构

    lottery_pending_unmute(id, bot_id, group_id, user_id,
                           unmute_at, expire_at, created_at, attempts)

- `unmute_at`：应该在什么时候解禁（unix 秒，绝对时刻）
- `expire_at`：这次禁言**自然到期**的时刻（= 禁言开始 + 抽到的时长）。
  ⚠️ 用来判断「还需不需要解禁」：如果 bot 停机太久、`expire_at` 都过了，
  平台早就自动解开了，这时**不该**再去调解禁 API（无意义且会打扰）。
- `(group_id, user_id)` 唯一：同一个人二次抽奖 = 覆盖旧记录（新禁言覆盖旧禁言）。
"""

from __future__ import annotations

import contextlib
from pathlib import Path
import sqlite3
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # 注解在 `from __future__ import annotations` 下惰性求值，运行时不需要它。
    # 用 Generator 而非 Iterator：@contextmanager 标 Iterator 已判 deprecated。
    from collections.abc import Generator

_TABLE = "lottery_pending_unmute"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id     TEXT    NOT NULL DEFAULT '',
    group_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    unmute_at  REAL    NOT NULL,
    expire_at  REAL    NOT NULL,
    created_at REAL    NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (group_id, user_id)
);
"""

_db_path: Path | None = None


def default_db_path() -> Path:
    """生产路径：localstore 的插件数据目录（只在 NoneBot 初始化后调用）。

    ⚠️ 用 ``get_data_file(<插件名>, ...)`` 这个**显式命名**的 API，而**不是**
    ``get_plugin_data_file()``：后者靠**调用栈**猜是哪个插件调用它
    （``_try_get_caller_plugin`` → 找不到就
    ``RuntimeError("Cannot detect caller plugin")``）。
    在启动钩子里栈更绕，一旦猜不中就抛异常 —— 而我们的 ``_ensure_store`` 会把
    异常吞掉只打个日志，结果就是「容灾静默失效」，最难查的那种。
    显式传名没有这个不确定性。
    """
    import nonebot_plugin_localstore as localstore

    return localstore.get_data_file("nonebot_plugin_lottery", "lottery.sqlite3")


def init(path: Path) -> None:
    """指定数据库文件并建表。启动钩子与测试都走这里。"""
    global _db_path
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _db_path = path
    with _conn() as conn:
        conn.executescript(_SCHEMA)


def is_initialized() -> bool:
    return _db_path is not None


@contextlib.contextmanager
def _conn() -> Generator[sqlite3.Connection]:
    if _db_path is None:
        msg = "store.init() 还没调用"
        raise RuntimeError(msg)
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_pending(
    *,
    bot_id: str,
    group_id: int,
    user_id: int,
    unmute_at: float,
    expire_at: float,
) -> None:
    """写入/覆盖一条待解禁记录（同一个人二次抽奖覆盖旧记录）。"""
    with _conn() as conn:
        conn.execute(
            f"INSERT INTO {_TABLE}"
            " (bot_id, group_id, user_id, unmute_at, expire_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (group_id, user_id) DO UPDATE SET"
            "   bot_id = excluded.bot_id,"
            "   unmute_at = excluded.unmute_at,"
            "   expire_at = excluded.expire_at,"
            "   created_at = excluded.created_at,"
            "   attempts = 0",
            (bot_id, group_id, user_id, unmute_at, expire_at, time.time()),
        )


def remove_pending(group_id: int, user_id: int) -> bool:
    """删掉一条待解禁记录，True=确实删掉了。"""
    with _conn() as conn:
        cur = conn.execute(
            f"DELETE FROM {_TABLE} WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        return cur.rowcount > 0


def bump_attempts(group_id: int, user_id: int) -> int:
    """解禁失败时累加重试次数，返回累加后的值。"""
    with _conn() as conn:
        conn.execute(
            f"UPDATE {_TABLE} SET attempts = attempts + 1"
            " WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        row = conn.execute(
            f"SELECT attempts FROM {_TABLE} WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        ).fetchone()
        return int(row["attempts"]) if row else 0


def all_pending() -> list[sqlite3.Row]:
    """所有待解禁记录（启动时重建任务用）。"""
    with _conn() as conn:
        return conn.execute(
            f"SELECT id, bot_id, group_id, user_id, unmute_at, expire_at,"
            f" created_at, attempts FROM {_TABLE} ORDER BY unmute_at"
        ).fetchall()


def count() -> int:
    """记录数（测试与运维核对用）。"""
    with _conn() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {_TABLE}").fetchone()
        return int(row["n"]) if row else 0


__all__ = [
    "all_pending",
    "bump_attempts",
    "count",
    "default_db_path",
    "init",
    "is_initialized",
    "remove_pending",
    "save_pending",
]
