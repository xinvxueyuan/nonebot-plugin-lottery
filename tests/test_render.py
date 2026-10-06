"""CRUD 回复的渲染测试（`render` 模块）。

⚠️ 这里**不装 htmlkit**：`build_html` 是纯函数、`render_reply` 走可 monkeypatch 的
`_load_htmlkit()`。所以「转义做没做」「超长列表有没有截断」「失败会不会回退」
这几件事全都能在 CI 里断言 —— 靠肉眼看渲染出来的图是验证不了的。
"""

from __future__ import annotations

import logging

import pytest

from nonebot_plugin_lottery import render


def _row(i: int, word: str = "词", badge: str = "模糊") -> render.Row:
    return render.Row(f"#{i}", badge, word)


# ── 纯函数 build_html ──────────────────────────────────────────────


def test_build_html_escapes_words():
    """**安全要点**：匹配词是用户随手写的，不转义会破坏 HTML（甚至注入标记）。"""
    html = render.build_html(
        title="t",
        rows=[render.Row("#1", "模糊", '<script>alert(1)</script> & "x"')],
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;" in html
    assert "&quot;" in html


def test_build_html_escapes_title_lines_and_footer():
    html = render.build_html(
        title="<b>标题</b>", lines=["<i>行</i>"], footer="<u>脚注</u>"
    )
    assert "<b>" not in html and "&lt;b&gt;" in html
    assert "<i>" not in html and "&lt;i&gt;" in html
    assert "<u>" not in html and "&lt;u&gt;" in html


def test_build_html_escapes_all_row_fields():
    html = render.build_html(title="t", rows=[render.Row("<a>", "<b>", "<c>")])
    assert "&lt;a&gt;" in html and "&lt;b&gt;" in html and "&lt;c&gt;" in html


def test_build_html_caps_rows_and_reports_remaining():
    """超长列表要截断并写明还剩多少 —— 否则一条命令能生成几十米长的图。"""
    rows = [_row(i) for i in range(1, render.MAX_ROWS + 6)]
    html = render.build_html(title="t", rows=rows)
    assert html.count('class="row"') == render.MAX_ROWS
    assert "另有 5 条未显示" in html


def test_build_html_shows_placeholder_when_empty():
    html = render.build_html(title="空表")
    assert "（空）" in html


def test_build_html_uses_tone_color():
    assert render.TONE_COLORS["success"] in render.build_html(title="t", tone="success")
    assert render.TONE_COLORS["error"] in render.build_html(title="t", tone="error")


def test_build_html_unknown_tone_falls_back_to_info():
    """不认识的颜色不该让渲染挂掉（也不该静默变成透明）。"""
    html = render.build_html(title="t", tone="不存在")
    assert render.TONE_COLORS["info"] in html


# ── CSS 的两个「litehtml 不是 Chromium」约束 ───────────────────────


def test_css_does_not_use_flex_gap():
    """回归：htmlkit 用的是 litehtml，flex 的 `gap` **不生效**（实测 0px）。

    间距必须用 margin —— 写成 gap 的话头像/文字会粘在一起，而本地看起来「没问题」。
    """
    assert "gap:" not in render._CSS


def test_css_declares_cjk_font_and_fixed_width():
    """字体与宽度都必须显式写死（默认字体不含中文 → 方块；max_width 不是缩放器）。"""
    assert "sans-serif" in render._CSS
    assert f"width: {render.CARD_WIDTH}px" in render._CSS
    assert render.MAX_WIDTH >= render.CARD_WIDTH


def test_max_width_is_not_smaller_than_card():
    """`max_width` 是上限：小于卡片宽会把内容**压扁**（实测文字溢出卡片）。"""
    assert render.MAX_WIDTH > render.CARD_WIDTH


# ── render_reply：成功 / 失败回退 ─────────────────────────────────


async def test_render_reply_returns_bytes_on_success(monkeypatch):
    seen: dict = {}

    async def fake_html_to_pic(html, **kwargs):
        seen.update(html=html, kwargs=kwargs)
        return b"\x89PNG\r\n\x1a\nfake"

    monkeypatch.setattr(render, "_load_htmlkit", lambda: fake_html_to_pic)

    png = await render.render_reply(title="标题", rows=[_row(1)])

    assert png == b"\x89PNG\r\n\x1a\nfake"
    assert "标题" in seen["html"]
    assert seen["kwargs"]["max_width"] == render.MAX_WIDTH


async def test_render_reply_returns_none_when_htmlkit_missing(monkeypatch, caplog):
    """Htmlkit 不可用（没装/加载失败）→ 返回 None 并告警，**绝不抛**。

    调用方据此回退纯文本；抛出去的话一个渲染问题就会表现成「命令没反应」。
    """

    def boom():
        raise ImportError("no htmlkit")

    monkeypatch.setattr(render, "_load_htmlkit", boom)

    with caplog.at_level(logging.WARNING, logger="nonebot_plugin_lottery"):
        assert await render.render_reply(title="t", rows=[_row(1)]) is None
    assert "渲染失败" in caplog.text


async def test_render_reply_returns_none_when_renderer_raises(monkeypatch, caplog):
    """渲染器自己抛异常（字体缺失/尺寸异常等）同样只回退。"""

    async def boom(html, **kwargs):
        raise RuntimeError("litehtml 崩了")

    monkeypatch.setattr(render, "_load_htmlkit", lambda: boom)

    with caplog.at_level(logging.WARNING, logger="nonebot_plugin_lottery"):
        assert await render.render_reply(title="t", rows=[_row(1)]) is None
    assert "RuntimeError" in caplog.text


@pytest.mark.parametrize("tone", sorted(render.TONE_COLORS))
async def test_render_reply_accepts_every_tone(monkeypatch, tone):
    async def fake(html, **kwargs):
        return b"png"

    monkeypatch.setattr(render, "_load_htmlkit", lambda: fake)
    assert await render.render_reply(title="t", tone=tone) == b"png"
