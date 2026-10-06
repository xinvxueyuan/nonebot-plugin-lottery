"""匹配词的类型表与匹配实现（**自实现**的运行时解析层）。

## 为什么自实现，而不是继续用 NoneBot 的内置规则

内置规则（`on_keyword` / `on_startswith` / ...）在**导入时**就把词写进 rule 里了，
改词必须重启进程。本插件要「运行时动态增删改词」，所以改成：

    一个 on_message 兜住所有群消息 → 在 handler 里查**运行时词表**
    → 命中才处理并阻断传播

但匹配**语义**仍然对齐 NoneBot 的内置规则，这样两种用法的心智模型一致：

| 类型 | key | 语义 | 对齐的 NoneBot 规则 |
|---|---|---|---|
| 模糊 | `contains` | 纯文本**包含** | `on_keyword`（`KeywordsRule`） |
| 开头 | `startswith` | 纯文本以它**开头** | `on_startswith`（`StartswithRule`） |
| 结尾 | `endswith` | 纯文本以它**结尾** | `on_endswith`（`EndswithRule`） |
| 精准 | `fullmatch` | 纯文本**完全相同** | `on_fullmatch`（`FullmatchRule`） |
| 正则 | `regex` | 对纯文本做 `re.search` | `on_regex`（`RegexRule`） |

⚠️ 一处**有意的偏离**（写在这里，免得以后被当成 bug 去「修」）：

NoneBot 的 `RegexRule` 是对 **`str(event.get_message())`**（含 `[CQ:image,...]` 段）做
`re.search`；本插件对 **`get_plaintext()`**（剥掉图片/at 段后的纯文本）做。理由：
① 另外 4 种类型都基于纯文本，正则单独用原始串会让规则行为不可预期；
② CQ 串里满是 `[`/`]`，写正则极易误命中图片/表情段。
统一用纯文本更可预期 —— 但**如果你就是要匹配 CQ 串**，这里没有提供。

## 匹配顺序

按**词表 id 升序**取第一条命中的（= 先加的先生效）。顺序可预期、也能从
「查看匹配词」的列表里直接读出来；想改顺序就删掉重加。**不按类型优先级**排
（那会让「为什么这条先命中」变得难解释）。
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any, Final

logger = logging.getLogger("nonebot_plugin_lottery")

#: 规范类型 key → 中文显示名（列表/回复里用它）。
MATCH_TYPES: Final[dict[str, str]] = {
    "contains": "模糊",
    "startswith": "开头",
    "endswith": "结尾",
    "fullmatch": "精准",
    "regex": "正则",
}

#: 展示与文档用的固定顺序（不参与匹配优先级）。
MATCH_ORDER: Final[tuple[str, ...]] = (
    "contains",
    "startswith",
    "endswith",
    "fullmatch",
    "regex",
)

#: 用户输入的类型写法 → 规范 key。大小写不敏感（`normalize_type` 里 casefold）。
_TYPE_ALIASES: Final[dict[str, str]] = {
    "contains": "contains",
    "contain": "contains",
    "模糊": "contains",
    "包含": "contains",
    "含": "contains",
    "startswith": "startswith",
    "start": "startswith",
    "开头": "startswith",
    "前缀": "startswith",
    "前": "startswith",
    "endswith": "endswith",
    "end": "endswith",
    "结尾": "endswith",
    "后缀": "endswith",
    "后": "endswith",
    "fullmatch": "fullmatch",
    "full": "fullmatch",
    "精准": "fullmatch",
    "全等": "fullmatch",
    "完全": "fullmatch",
    "等于": "fullmatch",
    "regex": "regex",
    "regexp": "regex",
    "re": "regex",
    "正则": "regex",
}

#: 单个匹配词的长度上限 —— 防手滑把一整段正文粘进来（渲染与匹配都受不了）。
MAX_WORD_LENGTH: Final[int] = 100

#: 词表条数上限。每次群消息都要过一遍词表（O(n)），也防止无意义地无限堆。
MAX_WORDS: Final[int] = 200


@dataclass(frozen=True)
class Hit:
    """一次命中的结果（带 id，回复里可以引用它）。"""

    word_id: int
    match_type: str
    word: str


def normalize_type(raw: str) -> str | None:
    """把用户写的类型（中/英、大小写混杂）规范成 key；不认识返回 None。"""
    return _TYPE_ALIASES.get((raw or "").strip().casefold())


def type_label(match_type: str) -> str:
    """类型的中文显示名（未知类型原样返回，便于排错）。"""
    return MATCH_TYPES.get(match_type, match_type)


def types_help() -> str:
    """「类型」参数的可选值说明（用在错误提示里）。"""
    return "/".join(MATCH_TYPES[k] for k in MATCH_ORDER)


def validate_word(match_type: str, word: str) -> str | None:
    """校验待写入的匹配词。返回错误文案；`None` = 合法。

    ⚠️ **正则必须在这里就编译一次**：无效正则在匹配时才炸的话，
    每来一条群消息都会抛异常、刷满日志（而且那条规则等于永远静默失效）。
    写入时就拒绝，用户当场就能改。
    """
    if match_type not in MATCH_TYPES:
        return f"未知的匹配类型 {match_type!r}"
    if not word:
        return "匹配词不能为空"
    if len(word) > MAX_WORD_LENGTH:
        return f"匹配词太长了（{len(word)} > {MAX_WORD_LENGTH} 字符）"
    if match_type == "regex":
        try:
            re.compile(word)
        except re.error as e:
            return f"正则无效：{e}"
    return None


def match_word(match_type: str, word: str, text: str) -> bool:
    """单条匹配。空文本一律不命中（对齐 NoneBot 各规则的 `if not text: return False`）。

    未知类型/坏正则**返回 False 并告警**，不抛 —— 一条坏规则不该让整个监听挂掉。
    """
    if not text:
        return False
    if match_type == "contains":
        return word in text
    if match_type == "startswith":
        return text.startswith(word)
    if match_type == "endswith":
        return text.endswith(word)
    if match_type == "fullmatch":
        return text == word
    if match_type == "regex":
        try:
            return re.search(word, text) is not None
        except re.error:
            logger.warning("匹配词正则无法编译，本条已跳过：%r", word)
            return False
    logger.warning("未知的匹配类型，本条已跳过：%r", match_type)
    return False


def find_match(entries: Any, text: str) -> Hit | None:
    """在词表里找**第一条**命中的（按传入顺序 = id 升序）。

    `entries` 是「能用 `[]` 取 `id`/`match_type`/`word` 的序列」——
    生产传 `sqlite3.Row`，测试传 dict 即可。
    """
    for row in entries:
        match_type = str(row["match_type"])
        word = str(row["word"])
        if match_word(match_type, word, text):
            return Hit(word_id=int(row["id"]), match_type=match_type, word=word)
    return None


__all__ = [
    "MATCH_ORDER",
    "MATCH_TYPES",
    "MAX_WORDS",
    "MAX_WORD_LENGTH",
    "Hit",
    "find_match",
    "match_word",
    "normalize_type",
    "type_label",
    "types_help",
    "validate_word",
]
