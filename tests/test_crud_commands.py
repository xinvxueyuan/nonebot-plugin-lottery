"""CRUD 命令测试（`handle` 模块）：权限矩阵、参数解析、四个命令的端到端行为。

约定：
- 渲染被替换成「假 PNG」，所以断言里能直接查 `message.type == "image"`
  （用户要求 **CRUD 的所有回复都渲染成图**）；
- 「渲染不可用」的路径单独测（回退纯文本，保证命令永远有响应）；
- 事件用**真类**（`GroupMessageEvent` / `PrivateMessageEvent`），sender 用真 `Sender`。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11.event import Sender
from nonebot.exception import FinishedException
import pytest

from nonebot_plugin_lottery import handle, store, words

FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


# ── 事件替身（真类）────────────────────────────────────────────────


def _group_event(role: str = "admin", user_id: int = 10001) -> GroupMessageEvent:
    return GroupMessageEvent.model_construct(
        post_type="message",
        message_type="group",
        sub_type="normal",
        group_id=1094538078,
        user_id=user_id,
        message_id=1,
        message=Message(""),
        raw_message="",
        sender=Sender(user_id=user_id, role=role),
    )


def _private_event(user_id: int = 10001) -> PrivateMessageEvent:
    return PrivateMessageEvent.model_construct(
        post_type="message",
        message_type="private",
        sub_type="friend",
        user_id=user_id,
        message_id=1,
        message=Message(""),
        raw_message="",
    )


@dataclass
class _Capture:
    """记录 CRUD 回复。

    - `messages` = `finish()` 收到的东西（出图时是 image 段，回退时是 str）
    - `renders`  = 每次 `render_reply(**kwargs)` 的参数（**卡片内容在这里**）

    ⚠️ 出图路径下**不能**用 `str(message)` 断言文案 —— image 段字符串化后是
    `[CQ:image,file=base64://...]`，看不到任何卡片文字。所以文案断言走 `card`。
    """

    messages: list = field(default_factory=list)
    renders: list = field(default_factory=list)
    real_render: object = None

    @property
    def last(self):
        assert self.messages, "没有任何回复 —— 命令可能静默结束了"
        return self.messages[-1]

    @property
    def last_render(self) -> dict:
        assert self.renders, "没有调用渲染 —— 回复可能没走图"
        return self.renders[-1]

    @property
    def card(self) -> str:
        """最近一次卡片的全部可见文字（标题/行/行内三段/页脚）拼成一串。"""
        kw = self.last_render
        parts = [str(kw.get("title", "")), *[str(x) for x in kw.get("lines", ())]]
        parts += [f"{r.label} {r.badge} {r.text}" for r in kw.get("rows", ())]
        if kw.get("footer"):
            parts.append(str(kw["footer"]))
        return "\n".join(parts)


@pytest.fixture
def crud(monkeypatch) -> _Capture:
    """Patch 四个 CRUD matcher 的 `finish`；渲染默认返回假 PNG。"""
    cap = _Capture()

    async def fake_finish(message=None, **kwargs):
        cap.messages.append(message)
        raise FinishedException

    for matcher in (handle.add_cmd, handle.del_cmd, handle.upd_cmd, handle.list_cmd):
        monkeypatch.setattr(matcher, "finish", fake_finish)

    async def fake_render(**kwargs):
        cap.renders.append(kwargs)
        return FAKE_PNG

    cap.real_render = handle.render.render_reply  # 需要测真实回退路径的用例用它
    monkeypatch.setattr(handle.render, "render_reply", fake_render)
    return cap


def _takes_args(handler) -> bool:
    """Handler 是否声明了 `args` 形参（决定 `_call` 传不传第二个位置参数）。"""
    import inspect

    return len(inspect.signature(handler).parameters) > 1


async def _call(handler, event, text: str = "") -> None:
    """直接调 handler（跳过引擎派发）。`finish` 会抛 `FinishedException` 结束流程。"""
    call_args = [event, Message(text)] if _takes_args(handler) else [event]
    with pytest.raises(FinishedException):
        await handler(*call_args)


def _seed(*specs: tuple[str, str]) -> list[int]:
    ids = []
    for match_type, word in specs:
        new_id = store.add_word(match_type, word)
        assert new_id is not None
        ids.append(new_id)
    return ids


# ── 权限（纯函数矩阵）─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("is_group", "role", "user_id", "superusers", "expected"),
    [
        # 群内：群主 / 管理员 → 可以
        (True, "owner", "1", set(), True),
        (True, "admin", "1", set(), True),
        # 群内普通成员 → 不行
        (True, "member", "1", set(), False),
        (True, None, "1", set(), False),
        # 超管：群内、私聊都可以
        (True, "member", "9", {"9"}, True),
        (False, None, "9", {"9"}, True),
        # 私聊非超管 → 不行（用户明确要求「私聊仅超管」）
        (False, None, "1", set(), False),
        # 类型混用也要对（配置里可能写 int）
        (True, "member", "9", {9}, True),
        (True, "ADMIN", "1", set(), True),  # 大小写不敏感
    ],
)
def test_can_manage_matrix(is_group, role, user_id, superusers, expected):
    assert (
        handle.can_manage(
            is_group=is_group, role=role, user_id=user_id, superusers=superusers
        )
        is expected
    )


def test_crud_priority_is_below_watch_priority():
    """**核心不变式**：CRUD 命令必须比监听器的优先级小（先跑 + `block=True`）。

    否则词表里有模糊词「匹配词」时，一条「查看匹配词」会**既列表又抽奖**
    （把自己禁言）。这条断言就是防后人把两个优先级写反/写平。
    """
    import nonebot_plugin_lottery as plugin

    assert plugin.lottery_watch.priority > handle.CRUD_PRIORITY
    for matcher in (handle.add_cmd, handle.del_cmd, handle.upd_cmd, handle.list_cmd):
        assert matcher.priority == handle.CRUD_PRIORITY
        assert matcher.block is True


def test_all_crud_commands_are_registered():
    """四条命令（含别名）都注册上了，且命令词与元数据一致。"""
    expected = {
        handle.add_cmd: {"增加匹配词", "添加匹配词", "新增匹配词"},
        handle.del_cmd: {"删除匹配词", "移除匹配词"},
        handle.upd_cmd: {"更新匹配词", "修改匹配词"},
        handle.list_cmd: {"查看匹配词", "匹配词列表", "查看抽奖匹配词"},
    }
    for matcher, want in expected.items():
        got = set()
        for checker in matcher.rule.checkers or ():
            cmds = getattr(getattr(checker, "call", None), "cmds", ()) or ()
            for group in cmds:
                got.update([group] if isinstance(group, str) else group)
        assert got == want, f"{matcher} 注册的命令词不对：{got}"

    import nonebot_plugin_lottery as plugin

    meta_cmds = [c["command"] for c in (plugin.__plugin_meta__.extra or {})["commands"]]
    assert meta_cmds == list(handle.CMD_NAMES)


def test_handler_annotations_resolve_at_runtime():
    """Handler 注解必须运行时可解析。

    NoneBot 靠注解注入；放进 `TYPE_CHECKING` 只在生产炸。
    """
    from nonebot.adapters.onebot.v11 import Message, MessageEvent
    from nonebot.dependencies import get_typed_signature

    for matcher, handler in (
        (handle.add_cmd, handle._handle_add),
        (handle.del_cmd, handle._handle_del),
        (handle.upd_cmd, handle._handle_update),
        (handle.list_cmd, handle._handle_list),
    ):
        anns = {
            name: prm.annotation
            for name, prm in get_typed_signature(handler).parameters.items()
        }
        assert anns["event"] is MessageEvent, f"{matcher} 的 event 注解不是真类"
        if "args" in anns:  # 查看命令没有可解析参数，不声明 args
            assert anns["args"] is Message, f"{matcher} 的 args 注解不是真类"


# ── 参数解析（纯函数）─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected_type", "expected_word"),
    [
        ("模糊 自刎归天", "contains", "自刎归天"),
        ("开头 抽奖", "startswith", "抽奖"),
        ("正则 ^抽\\d+$", "regex", "^抽\\d+$"),
        ("contains abc", "contains", "abc"),
        ("模糊 带 空格 的词", "contains", "带 空格 的词"),  # 只按第一个空白切
    ],
)
def test_parse_type_word_ok(raw, expected_type, expected_word):
    match_type, word, err = handle.parse_type_word(raw)
    assert err is None
    assert (match_type, word) == (expected_type, expected_word)


@pytest.mark.parametrize("raw", ["", "   ", "模糊", "不存在类型 词"])
def test_parse_type_word_errors(raw):
    _, _, err = handle.parse_type_word(raw)
    assert err is not None and err.strip()


@pytest.mark.parametrize(
    ("raw", "expected_id", "expected_type", "expected_word"),
    [
        ("3 模糊 自刎归天", 3, "contains", "自刎归天"),
        ("12 精准 词", 12, "fullmatch", "词"),
    ],
)
def test_parse_update_args_ok(raw, expected_id, expected_type, expected_word):
    word_id, match_type, word, err = handle.parse_update_args(raw)
    assert err is None
    assert (word_id, match_type, word) == (expected_id, expected_type, expected_word)


@pytest.mark.parametrize("raw", ["", "1", "abc 模糊 词", "1 模糊"])
def test_parse_update_args_errors(raw):
    *_, err = handle.parse_update_args(raw)
    assert err is not None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", [1]),
        ("1 2 3", [1, 2, 3]),
        ("1,2", [1, 2]),
        ("1，2、3", [1, 2, 3]),
        ("1 1 2", [1, 2]),  # 去重
    ],
)
def test_parse_ids_ok(raw, expected):
    ids, err = handle.parse_ids(raw)
    assert err is None and ids == expected


@pytest.mark.parametrize("raw", ["", "abc", "1 abc"])
def test_parse_ids_errors(raw):
    ids, err = handle.parse_ids(raw)
    assert err is not None and ids == []


# ── 增加 ─────────────────────────────────────────────────────────


async def test_add_creates_row_and_replies_image(crud):
    await _call(handle._handle_add, _group_event(), "模糊 自刎归天")

    rows = store.list_words()
    assert len(rows) == 1
    assert (rows[0]["match_type"], rows[0]["word"]) == ("contains", "自刎归天")
    assert "已增加匹配词" in crud.card
    assert crud.last.type == "image", "CRUD 回复必须渲染成图"


async def test_add_records_who_did_it(crud):
    await _call(handle._handle_add, _group_event(user_id=22222), "模糊 词")
    assert store.list_words()[0]["created_by"] == "22222"


async def test_add_duplicate_reports_existing_id(crud):
    _seed(("contains", "自刎归天"))
    await _call(handle._handle_add, _group_event(), "模糊 自刎归天")

    assert store.count_words() == 1, "重复添加不该插第二条"
    assert "已经存在" in crud.card and "#1" in crud.card


async def test_add_rejects_bad_regex_without_writing(crud, caplog):
    await _call(handle._handle_add, _group_event(), "正则 ([unclosed")

    assert store.count_words() == 0, "坏正则不该写进库"
    assert "正则无效" in crud.card


async def test_add_rejects_unknown_type(crud):
    await _call(handle._handle_add, _group_event(), "乱七八糟 词")
    assert store.count_words() == 0
    assert "用法不对" in crud.card
    assert words.types_help() in crud.card  # 提示可选类型


async def test_add_denied_for_normal_member(crud):
    await _call(handle._handle_add, _group_event(role="member"), "模糊 词")

    assert store.count_words() == 0, "没权限却写库了"
    assert "没有权限" in crud.card


async def test_add_denied_for_private_non_superuser(crud):
    await _call(handle._handle_add, _private_event(), "模糊 词")
    assert store.count_words() == 0
    assert "没有权限" in crud.card


async def test_add_allowed_for_superuser_in_private(crud, monkeypatch):
    monkeypatch.setattr(handle, "current_superusers", lambda: {"10001"})

    await _call(handle._handle_add, _private_event(), "模糊 词")

    assert store.count_words() == 1, "超管私聊应该能加词"


async def test_add_allowed_for_group_owner(crud):
    await _call(handle._handle_add, _group_event(role="owner"), "模糊 词")
    assert store.count_words() == 1


async def test_add_respects_max_words_guard(crud, monkeypatch):
    monkeypatch.setattr(words, "MAX_WORDS", 2)
    _seed(("contains", "甲"), ("contains", "乙"))

    await _call(handle._handle_add, _group_event(), "模糊 丙")

    assert store.count_words() == 2, "超过上限还写进去了"
    assert "词表满了" in crud.card


# ── 删除 ─────────────────────────────────────────────────────────


async def test_delete_removes_and_lists_what_was_removed(crud):
    """回执必须列出**被删的**内容。

    ⚠️ 回归：曾经的实现是「先删再查」——查出来全是 None，
    于是「已删除 N 条」下面一条都列不出来。
    """
    ids = _seed(("contains", "自刎归天"), ("startswith", "抽奖"))

    await _call(handle._handle_del, _group_event(), f"{ids[0]} {ids[1]}")

    assert store.count_words() == 0
    assert "已删除 2 条" in crud.card
    assert "自刎归天" in crud.card and "抽奖" in crud.card
    assert "模糊" in crud.card  # 类型徽标也带上


async def test_delete_reports_unknown_ids(crud):
    ids = _seed(("contains", "甲"))
    await _call(handle._handle_del, _group_event(), f"{ids[0]} 999")

    assert store.count_words() == 0
    assert "已删除 1 条" in crud.card
    assert "不存在" in crud.card and "#999" in crud.card


async def test_delete_all_unknown_ids(crud):
    await _call(handle._handle_del, _group_event(), "7 8")

    assert "没有删掉任何东西" in crud.card
    assert "#7" in crud.card and "#8" in crud.card


async def test_delete_bad_args(crud):
    _seed(("contains", "甲"))
    await _call(handle._handle_del, _group_event(), "abc")

    assert store.count_words() == 1, "参数错却删了东西"
    assert "用法不对" in crud.card


async def test_delete_denied_for_normal_member(crud):
    ids = _seed(("contains", "甲"))
    await _call(handle._handle_del, _group_event(role="member"), str(ids[0]))
    assert store.count_words() == 1


# ── 更新 ─────────────────────────────────────────────────────────


async def test_update_changes_type_and_word(crud):
    ids = _seed(("contains", "旧词"))
    await _call(handle._handle_update, _group_event(), f"{ids[0]} 精准 新词")

    row = store.get_word(ids[0])
    assert row is not None
    assert (row["match_type"], row["word"]) == ("fullmatch", "新词")
    assert "已更新" in crud.card and "旧词" in crud.card  # 回执带上原值


async def test_update_not_found(crud):
    await _call(handle._handle_update, _group_event(), "999 模糊 词")
    assert "没有 #999" in crud.card


async def test_update_duplicate_reports_clash(crud):
    ids = _seed(("contains", "甲"), ("contains", "乙"))
    await _call(handle._handle_update, _group_event(), f"{ids[1]} 模糊 甲")

    assert "会重复" in crud.card and f"#{ids[0]}" in crud.card
    row = store.get_word(ids[1])
    assert row is not None and row["word"] == "乙", "失败时不该改动原记录"


async def test_update_bad_regex_does_not_write(crud):
    ids = _seed(("contains", "词"))
    await _call(handle._handle_update, _group_event(), f"{ids[0]} 正则 ([bad")

    row = store.get_word(ids[0])
    assert row is not None and row["match_type"] == "contains"


# ── 查看 ─────────────────────────────────────────────────────────


async def test_list_renders_rows_with_ids(crud):
    _seed(("contains", "自刎归天"), ("regex", r"^抽\d+$"))
    await _call(handle._handle_list, _group_event())

    text = crud.card
    assert crud.last.type == "image"
    assert "2 条" in text
    assert "#1" in text and "#2" in text
    assert "自刎归天" in text and "正则" in text


async def test_list_empty_table_hints_how_to_add(crud):
    await _call(handle._handle_list, _group_event())

    assert "还没有任何匹配词" in crud.card
    assert handle.USAGE_ADD in crud.card


async def test_list_denied_for_normal_member(crud):
    _seed(("contains", "甲"))
    await _call(handle._handle_list, _group_event(role="member"))
    assert "没有权限" in crud.card


# ── 渲染不可用时的回退（命令永远要有响应）─────────────────────────


async def test_reply_falls_back_when_render_returns_none(crud, monkeypatch):
    """渲染返回 None（上层约定）→ 回退纯文本。"""

    async def no_render(**_kwargs):
        return None

    monkeypatch.setattr(handle.render, "render_reply", no_render)
    _seed(("contains", "自刎归天"))

    await _call(handle._handle_list, _group_event())

    assert isinstance(crud.last, str), "渲染失败时应回退成纯文本消息"
    assert "抽奖匹配词" in crud.last and "自刎归天" in crud.last


async def test_reply_falls_back_via_the_real_renderer(crud, monkeypatch):
    """**走真实的 `render_render` 失败分支**（不是用替身返回 None 糊过去）。

    ⚠️ 为什么必须这样测：只用替身返回 None 的话，`render_reply` 内部
    「把异常吞掉并返回 None」这段逻辑**从未被执行** —— 有人把它改成
    `raise`（渲染一挂命令就没反应）测试照样全绿（变异检验实证）。
    """
    monkeypatch.setattr(handle.render, "render_reply", crud.real_render)

    def boom():
        raise ImportError("nonebot_plugin_htmlkit 未安装")

    monkeypatch.setattr(handle.render, "_load_htmlkit", boom)
    _seed(("contains", "自刎归天"))

    await _call(handle._handle_list, _group_event())

    assert isinstance(crud.last, str), "真实渲染失败后必须回退纯文本，而不是抛异常"
    assert "抽奖匹配词" in crud.last


async def test_error_paths_also_render(crud):
    """用户要求「CRUD 的**所有**回复都渲染成图」—— 包括报错路径。"""
    await _call(handle._handle_add, _group_event(), "乱七八糟 词")
    assert crud.last.type == "image"
