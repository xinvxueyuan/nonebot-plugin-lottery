"""运行时匹配词表的持久化测试（`store` 的 CRUD 数据层）。

⚠️ 这里的断言**不含任何用户文案** —— store 只返回状态码/行，文案是 `handle` 的事。
"""

from __future__ import annotations

import sqlite3

import pytest

from nonebot_plugin_lottery import store


def test_add_returns_new_id_and_shows_in_list(tmp_store):
    word_id = store.add_word("contains", "自刎归天", created_by="10001")
    assert word_id is not None and word_id > 0

    rows = store.list_words()
    assert len(rows) == 1
    assert (rows[0]["id"], rows[0]["match_type"], rows[0]["word"]) == (
        word_id,
        "contains",
        "自刎归天",
    )
    assert rows[0]["created_by"] == "10001"
    assert store.count_words() == 1


def test_add_duplicate_returns_none(tmp_store):
    """同 `(类型, 词)` 不重复插 —— 返回 None 让上层给友好提示。"""
    assert store.add_word("contains", "x") is not None
    assert store.add_word("contains", "x") is None
    assert store.count_words() == 1


def test_same_word_different_type_is_allowed(tmp_store):
    """同一个词配不同类型是**不同**规则（如「开头:抽奖」与「精准:抽奖」）。"""
    assert store.add_word("startswith", "抽奖") is not None
    assert store.add_word("fullmatch", "抽奖") is not None
    assert store.count_words() == 2


def test_list_words_is_ordered_by_id(tmp_store):
    ids = [store.add_word("contains", w) for w in ("甲", "乙", "丙")]
    assert [r["id"] for r in store.list_words()] == ids


def test_get_word_and_find_word(tmp_store):
    word_id = store.add_word("contains", "自刎归天", created_by="7")
    got = store.get_word(word_id)
    assert got is not None and got["word"] == "自刎归天"
    assert store.get_word(99999) is None

    found = store.find_word("contains", "自刎归天")
    assert found is not None and found["id"] == word_id
    assert store.find_word("fullmatch", "自刎归天") is None


def test_update_word_ok(tmp_store):
    word_id = store.add_word("contains", "旧词")
    assert store.update_word(word_id, "fullmatch", "新词") == "ok"
    row = store.get_word(word_id)
    assert row is not None
    assert (row["match_type"], row["word"]) == ("fullmatch", "新词")


def test_update_word_to_its_own_value_is_ok(tmp_store):
    """改成和原值一样（id 相同）算成功，不能误判成 duplicate。"""
    word_id = store.add_word("contains", "同样的词")
    assert store.update_word(word_id, "contains", "同样的词") == "ok"


def test_update_word_not_found(tmp_store):
    assert store.update_word(4242, "contains", "x") == "not_found"


def test_update_word_duplicate(tmp_store):
    a = store.add_word("contains", "甲")
    b = store.add_word("contains", "乙")
    assert store.update_word(b, "contains", "甲") == "duplicate"
    row = store.get_word(b)
    assert row is not None and row["word"] == "乙", "重复失败时不该改动原记录"
    assert store.get_word(a) is not None


def test_remove_words_batch_and_unknown_ids(tmp_store):
    ids = [store.add_word("contains", w) for w in ("甲", "乙", "丙")]
    removed = store.remove_words([ids[0], 99999, ids[2]])
    assert removed == 2, "不存在的 id 不计入删除数"
    assert [r["id"] for r in store.list_words()] == [ids[1]]


def test_remove_words_dedupes_ids(tmp_store):
    word_id = store.add_word("contains", "甲")
    assert store.remove_words([word_id, word_id]) == 1


def test_remove_words_empty_list_is_noop(tmp_store):
    store.add_word("contains", "甲")
    assert store.remove_words([]) == 0
    assert store.count_words() == 1


def test_init_is_idempotent_and_keeps_words(tmp_store):
    """`init()` 每次启动都会跑 `executescript` —— 必须**只建表不动数据**。"""
    word_id = store.add_word("contains", "保留我")
    store.init(tmp_store)  # 再跑一次
    assert store.count_words() == 1
    assert store.get_word(word_id) is not None


def test_words_table_and_pending_table_coexist(tmp_store):
    """新增词表不能影响既有的待解禁表（生产库里已有数据）。"""
    store.save_pending(
        bot_id="1", group_id=2, user_id=3, unmute_at=100.0, expire_at=200.0
    )
    store.add_word("contains", "新词")

    assert store.count() == 1
    assert store.count_words() == 1
    row = store.all_pending()[0]
    assert (row["group_id"], row["user_id"]) == (2, 3)


def test_duplicate_rejected_at_db_level_too(tmp_store):
    """UNIQUE 是最后一道防线：绕过 `add_word` 直接插也要被拦。"""
    store.add_word("contains", "x")
    conn = sqlite3.connect(tmp_store)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO lottery_words (match_type, word, created_at, created_by)"
                " VALUES (?, ?, ?, ?)",
                ("contains", "x", 1.0, ""),
            )
    finally:
        conn.close()
