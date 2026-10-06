"""CRUD 回复的图片渲染（nonebot-plugin-htmlkit）。

用户 2026-10-06 要求：**CRUD 的所有回复都用 htmlkit 渲染成图**（列表、成功、失败提示）。

## 三条必须遵守的约定

1. **失败一律回退文本，绝不抛**：渲染是锦上添花，渲染不通不能表现成「命令没反应」。
   `render_reply()` 返回 `None` 就由调用方发纯文本（见 `handle.py`）。
2. **所有用户输入必须 `html.escape`**：匹配词是用户随手写的，里面有 `<`/`&`/引号时
   不转义会**破坏 HTML**（轻则版面乱掉，重则注入标记）。这是本模块唯一的安全要点。
3. **别用浏览器语义写 CSS**：htmlkit 用的是自带原生渲染器（litehtml），
   **不是 Chromium**。
   实测：`dpi` 对输出像素无影响、`max_width` 是**上限而非缩放器**（内容更宽时会把内容
   压扁）、flex 的 `gap` **不生效**。所以宽度/字号/间距全部**显式写死**，间距用
   `margin` 而不是 `gap`。
"""

from __future__ import annotations

from dataclasses import dataclass
import html as html_mod
import logging
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger("nonebot_plugin_lottery")

#: 卡片宽度；`MAX_WIDTH` 是 htmlkit 的上限，必须 ≥ 卡片宽 + padding（否则被压扁）
CARD_WIDTH: Final[int] = 600
MAX_WIDTH: Final[int] = 680

#: 一张图最多画多少行（超出只提示条数 —— 否则一条命令能生成几十米长的图）
MAX_ROWS: Final[int] = 60

#: 语气 → 主色（标题左边框/图标色）
TONE_COLORS: Final[dict[str, str]] = {
    "info": "#2563eb",
    "success": "#16a34a",
    "error": "#dc2626",
    "warn": "#d97706",
}

_CSS: Final[str] = f"""
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 16px; background: #f1f5f9;
    /* ⚠️ 必须显式声明 CJK 字体：宿主的默认 sans-serif 若不含中文字形，
       渲染出来全是方块（Windows 上还会打一条无害的 Fontconfig 警告）。 */
    font-family: "WenQuanYi Zen Hei", "Noto Sans CJK SC", "Microsoft YaHei",
                 "PingFang SC", sans-serif;
  }}
  .card {{
    width: {CARD_WIDTH}px; background: #ffffff; border-radius: 12px;
    padding: 22px 24px; border: 1px solid #e2e8f0;
  }}
  .title {{
    font-size: 26px; font-weight: bold; color: #0f172a;
    padding-left: 12px; border-left: 6px solid #2563eb; margin-bottom: 16px;
  }}
  .line {{ font-size: 20px; color: #334155; line-height: 30px; margin-bottom: 6px; }}
  .row {{ margin-bottom: 8px; }}
  .id {{
    display: inline-block; width: 66px; font-size: 19px; color: #64748b;
    font-family: monospace;
  }}
  .badge {{
    display: inline-block; width: 72px; font-size: 18px; color: #1d4ed8;
    background: #eff6ff; border-radius: 6px; text-align: center;
    padding: 2px 0; margin-right: 14px;
  }}
  .word {{ font-size: 21px; color: #0f172a; }}
  .footer {{
    margin-top: 16px; padding-top: 12px; border-top: 1px dashed #cbd5e1;
    font-size: 17px; color: #64748b; line-height: 26px;
  }}
"""

_TEMPLATE: Final[str] = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{css}</style></head>
<body><div class="card">
  <div class="title" style="border-left-color:{color}">{title}</div>
  {body}
  {footer}
</div></body></html>"""


@dataclass(frozen=True)
class Row:
    """列表一行：左侧编号 / 中间类型徽标 / 右侧匹配词。"""

    label: str
    badge: str
    text: str


def build_html(
    *,
    title: str,
    lines: Sequence[str] = (),
    rows: Sequence[Row] = (),
    tone: str = "info",
    footer: str | None = None,
) -> str:
    """拼 HTML（**纯函数**，可单测；转义与行数上限都在这里做）。

    抽成纯函数是为了能在不装 htmlkit 的环境里断言「转义做了没」「超长列表是否被截断」——
    这两点靠肉眼看图是验证不了的。
    """
    color = TONE_COLORS.get(tone, TONE_COLORS["info"])
    parts: list[str] = []
    parts.extend(f'<div class="line">{html_mod.escape(ln)}</div>' for ln in lines)

    shown = list(rows)[:MAX_ROWS]
    parts.extend(
        '<div class="row">'
        f'<span class="id">{html_mod.escape(r.label)}</span>'
        f'<span class="badge">{html_mod.escape(r.badge)}</span>'
        f'<span class="word">{html_mod.escape(r.text)}</span>'
        "</div>"
        for r in shown
    )
    hidden = len(rows) - len(shown)
    if hidden > 0:
        parts.append(f'<div class="line">…另有 {hidden} 条未显示</div>')
    if not rows and not lines:
        parts.append('<div class="line">（空）</div>')

    footer_html = (
        f'<div class="footer">{html_mod.escape(footer)}</div>' if footer else ""
    )
    return _TEMPLATE.format(
        css=_CSS,
        color=color,
        title=html_mod.escape(title),
        body="\n  ".join(parts),
        footer=footer_html,
    )


def _load_htmlkit() -> Any:
    """惰性取 `html_to_pic`（**单独抽出来**，测试可 monkeypatch，不必真装 htmlkit）。

    `nonebot-plugin-htmlkit` 是**可选依赖**（extra `htmlkit`），开发环境/CI 不装它，
    所以这行 import 在静态检查里解析不到 —— 这是预期的，不是漏依赖。
    """
    from nonebot_plugin_htmlkit import html_to_pic  # pyright: ignore[reportMissingImports]

    return html_to_pic


async def render_reply(
    *,
    title: str,
    lines: Sequence[str] = (),
    rows: Sequence[Row] = (),
    tone: str = "info",
    footer: str | None = None,
) -> bytes | None:
    """渲染成 PNG。**任何失败都只 warning 并返回 None**（调用方回退纯文本）。"""
    try:
        html_to_pic = _load_htmlkit()
        return await html_to_pic(
            build_html(title=title, lines=lines, rows=rows, tone=tone, footer=footer),
            max_width=MAX_WIDTH,
        )
    except Exception as e:
        logger.warning("抽奖 CRUD 渲染失败，回退纯文本: %s: %s", type(e).__name__, e)
        return None


__all__ = [
    "CARD_WIDTH",
    "MAX_ROWS",
    "MAX_WIDTH",
    "TONE_COLORS",
    "Row",
    "build_html",
    "render_reply",
]
