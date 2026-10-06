"""匹配类型与匹配实现（`words` 模块）的测试。

语义必须**对齐 NoneBot 内置规则**（本项目自实现的理由见 `words` 模块 docstring），
所以这里的用例按「哪种类型 ≈ 哪条内置规则」组织。
"""

from __future__ import annotations

import logging

import pytest

from nonebot_plugin_lottery import words

# ── 五种类型的匹配语义 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("match_type", "word", "text", "expected"),
    [
        # 模糊 = on_keyword（包含）
        ("contains", "自刎归天", "今天自刎归天真好看", True),
        ("contains", "自刎归天", "自刎归天", True),
        ("contains", "自刎归天", "自刎归西", False),
        # 开头 = on_startswith
        ("startswith", "抽奖", "抽奖", True),
        ("startswith", "抽奖", "抽奖吧各位", True),
        ("startswith", "抽奖", "我们抽奖吧", False),
        ("startswith", "抽奖", " 抽奖", False),  # 忠实于内置规则：不 lstrip
        # 结尾 = on_endswith
        ("endswith", "退群", "我要退群", True),
        ("endswith", "退群", "退群了", False),
        # 精准 = on_fullmatch（完全相同）
        ("fullmatch", "精准词", "精准词", True),
        ("fullmatch", "精准词", "精准词啊", False),
        ("fullmatch", "精准词", " 精准词", False),
        # 正则 = on_regex（search，不是 fullmatch）
        ("regex", r"^抽\d+次$", "抽3次", True),
        ("regex", r"^抽\d+次$", "我要抽3次", False),
        ("regex", r"\d+", "我有123个", True),
    ],
)
def test_match_word_semantics(match_type, word, text, expected):
    assert words.match_word(match_type, word, text) is expected


@pytest.mark.parametrize("match_type", words.MATCH_ORDER)
def test_empty_text_never_matches(match_type):
    """空文本一律不命中（对齐内置规则里的 `if not text: return False`）。

    ⚠️ 特别防「正则 `.*` 之类在空串上命中」—— 那会让**所有空消息**都触发抽奖。
    """
    for word in ("x", ".*", "", r"\s*"):
        assert words.match_word(match_type, word, "") is False


def test_contains_is_case_sensitive():
    """模糊匹配区分大小写（与 NoneBot 的 keyword 规则一致，它没有 ignorecase 参数）。"""
    assert words.match_word("contains", "ABC", "xxABCxx") is True
    assert words.match_word("contains", "abc", "xxABCxx") is False


def test_bad_regex_at_match_time_returns_false_and_warns(caplog):
    """坏正则在**匹配时**只告警并跳过 —— 一条坏规则不该让整个监听挂掉。"""
    with caplog.at_level(logging.WARNING, logger="nonebot_plugin_lottery"):
        assert words.match_word("regex", "([unclosed", "任意文本") is False
    assert "正则无法编译" in caplog.text


def test_unknown_match_type_returns_false_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="nonebot_plugin_lottery"):
        assert words.match_word("nope", "x", "x") is False
    assert "未知的匹配类型" in caplog.text


# ── 类型别名（用户输入中英文都能写）────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("模糊", "contains"),
        ("包含", "contains"),
        ("开头", "startswith"),
        ("前缀", "startswith"),
        ("结尾", "endswith"),
        ("精准", "fullmatch"),
        ("全等", "fullmatch"),
        ("正则", "regex"),
        ("contains", "contains"),
        ("CONTAINS", "contains"),
        ("  regex  ", "regex"),
        ("模糊的", None),
        ("", None),
    ],
)
def test_normalize_type(raw, expected):
    assert words.normalize_type(raw) == expected


def test_type_label_and_help():
    assert words.type_label("contains") == "模糊"
    assert words.type_label("未知类型") == "未知类型"  # 未知原样返回，便于排错
    assert words.types_help() == "模糊/开头/结尾/精准/正则"


# ── 写入前的校验（正则必须在这里就被编译）──────────────────────────


def test_validate_word_rejects_bad_regex():
    """无效正则必须在**写入时**就拒绝。

    否则它每收到一条群消息都抛一次、刷满日志，而那条规则等于永远静默失效。
    """
    err = words.validate_word("regex", "([unclosed")
    assert err is not None and "正则无效" in err


def test_validate_word_rejects_empty_and_too_long():
    assert words.validate_word("contains", "") is not None
    over = "字" * (words.MAX_WORD_LENGTH + 1)
    assert words.validate_word("contains", over) is not None
    assert words.validate_word("contains", "字" * words.MAX_WORD_LENGTH) is None


def test_validate_word_rejects_unknown_type():
    assert words.validate_word("nope", "x") is not None


def test_validate_word_accepts_good_values():
    for match_type in words.MATCH_ORDER:
        assert words.validate_word(match_type, "自刎归天") is None


# ── find_match：顺序、命中结构 ────────────────────────────────────


def _rows(*specs):
    return [
        {"id": i, "match_type": mt, "word": w}
        for i, (mt, w) in enumerate(specs, start=1)
    ]


def test_find_match_returns_first_by_id_order():
    rows = _rows(("contains", "自刎"), ("contains", "自刎归天"))
    hit = words.find_match(rows, "自刎归天")
    assert hit is not None
    assert hit.word_id == 1, "应取 id 小的那条（先加先生效）"
    assert hit.word == "自刎"


def test_find_match_returns_none_when_nothing_matches():
    assert words.find_match(_rows(("fullmatch", "精准词")), "别的") is None


def test_find_match_returns_none_on_empty_table():
    assert words.find_match([], "任意") is None


def test_find_match_hit_carries_id_type_and_word():
    hit = words.find_match(_rows(("regex", r"^抽\d+$")), "抽3")
    assert hit is not None
    assert (hit.word_id, hit.match_type, hit.word) == (1, "regex", r"^抽\d+$")


def test_find_match_tolerates_bad_rows_without_raising(caplog):
    """词表里混进坏数据（未知类型/坏正则）时，只跳过它、不抛。"""
    rows = [
        {"id": 1, "match_type": "nope", "word": "x"},
        {"id": 2, "match_type": "regex", "word": "([bad"},
        {"id": 3, "match_type": "contains", "word": "好词"},
    ]
    with caplog.at_level(logging.WARNING, logger="nonebot_plugin_lottery"):
        hit = words.find_match(rows, "这是好词")
    assert hit is not None and hit.word_id == 3


def test_find_match_accepts_sequences_not_only_rows():
    """只要能按 key 取值就行（生产传 sqlite3.Row，测试传 dict）。"""
    rows = [{"id": 7, "match_type": "contains", "word": "啊"}]
    hit = words.find_match(rows, "啊啊啊")
    assert hit is not None and hit.word_id == 7
