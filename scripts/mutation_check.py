"""变异检验：故意把「禁言时长 / 定时解禁 / 容灾持久化」写坏，看测试是否变红。

只跑 pytest 的退出码 —— 绿色说明测试**没**抓到变异，即那个断言是空壳。

当前语义（用户 2026-10-02 纠正 + 追加）：
  - 禁言按**抽到的时长**下；到阈值分钟时**调 API 解禁**（不是把时长砍成 10 分钟）
  - 必须用 nonebot-plugin-apscheduler 实现，且**做容灾持久化**（重启不丢任务）

其中 M9 / M11 / M12 / M13 / M14 / M15 是「持久化到底有没有真的生效」的探针：
只有容灾做对了，它们才会被抓到。
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent  # scripts/ 的上一级 = 仓库根

TARGETS = {
    "__init__": ROOT / "src/plugins/nonebot_plugin_lottery/__init__.py",
    "store": ROOT / "src/plugins/nonebot_plugin_lottery/store.py",
    "config": ROOT / "src/plugins/nonebot_plugin_lottery/config.py",
    # v2 的运行时匹配层
    "handle": ROOT / "src/plugins/nonebot_plugin_lottery/handle.py",
    "render": ROOT / "src/plugins/nonebot_plugin_lottery/render.py",
    "words": ROOT / "src/plugins/nonebot_plugin_lottery/words.py",
}

ENV_PREFIX = (
    "env -u UV_PYTHON -u UV_PROJECT_ENVIRONMENT -u SSL_CERT_FILE "
    "-u VIRTUAL_ENV -u PYTHONPATH"
)

# (编号, 说明, 目标, 原文, 变异后)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "M1",
        "把禁言时长砍成阈值（用户明确否决过的「用平台参数封顶」写法）",
        "__init__",
        "            duration=rolled * 60,",
        "            duration=min(rolled, unmute_after) * 60,",
    ),
    (
        "M2",
        "解禁任务的延迟用了抽到的值（480 分钟后才解禁 = 等于没解禁）",
        "__init__",
        "            unmute_after,\n            ban_minutes=rolled,",
        "            rolled,\n            ban_minutes=rolled,",
    ),
    (
        "M3",
        "定时解禁只写日志、不真的挂 apscheduler 任务（空壳实现）",
        "__init__",
        "        scheduler.add_job(",
        "        _dead = lambda *a, **k: None\n        _dead(",
    ),
    (
        "M4",
        "超阈判据 `>` 写成 `>=`（抽到正好 10 分钟也去挂任务）",
        "__init__",
        "    if rolled > unmute_after:",
        "    if rolled >= unmute_after:",
    ),
    (
        "M5",
        "定时解禁用了禁言时长而不是 0（等于没解禁）",
        "__init__",
        "        await bot.set_group_ban(group_id=group_id, user_id=user_id, duration=0)",
        "        await bot.set_group_ban(\n"
        "            group_id=group_id, user_id=user_id, duration=600\n"
        "        )",
    ),
    (
        "M6",
        "把 Bot/GroupMessageEvent 挪进 TYPE_CHECKING（依赖注入会静默失效）",
        "__init__",
        "from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent",
        "if TYPE_CHECKING:\n"
        "    from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent",
    ),
    (
        "M7",
        "`>` 判据反过来（没超阈值的才挂任务）",
        "__init__",
        "    if rolled > unmute_after:",
        "    if rolled < unmute_after:",
    ),
    (
        "M8",
        "纯函数里把抽到的值截断（禁言时长不再等于抽到的值）",
        "__init__",
        "    return pick(minimum, maximum)",
        "    return min(pick(minimum, maximum), 10)",
    ),
    (
        "M9",
        "抽奖命中时**不落库**（只挂内存任务）→ 重启就丢，容灾是假象",
        "__init__",
        "    store.save_pending(",
        "    _skip_persist = lambda **kw: None\n    _skip_persist(",
    ),
    (
        "M10",
        "落库了但**不挂任务**（记录躺着，本进程内不会解禁）",
        "__init__",
        "    _add_unmute_job(str(bot.self_id), group_id, user_id, when=unmute_at)",
        "    pass",
    ),
    (
        "M11",
        "解禁成功后**不删记录**（表无限增长，且下次启动会重复解禁）",
        "__init__",
        '    store.remove_pending(group_id, user_id)\n    logger.info("抽奖定时解禁完成',
        '    logger.info("抽奖定时解禁完成',
    ),
    (
        "M12",
        "取不到 bot 就**直接放弃**（不重排重试、不累计 attempts）→ 记录永远没人再碰",
        "__init__",
        '        logger.warning("抽奖解禁：bot {} 暂时不可用，60 秒后重试", bot_id)\n'
        "        _retry_later(bot_id, group_id, user_id)\n"
        "        return",
        '        logger.warning("抽奖解禁：bot {} 暂时不可用，60 秒后重试", bot_id)\n'
        "        return",
    ),
    (
        "M13",
        "启动恢复时把**已自然到期**的记录也去调 API（无用调用 + 噪音）",
        "__init__",
        "        if expire_at <= now:",
        "        if False:",
    ),
    (
        "M14",
        "启动恢复时把**已错过**的解禁丢掉（不补跑）→ 停机期间该解禁的人被漏掉",
        "__init__",
        "            when = _now()  # 停机期间错过了，立刻补上\n"
        "            overdue += 1",
        "            store.remove_pending(group_id, user_id)\n            continue",
    ),
    (
        "M15",
        "**启动钩子不注册**（重建逻辑永远不跑）→ 容灾整体失效",
        "__init__",
        "if plugin_config.lottery_persist_pending:\n    get_driver().on_startup(_on_startup)",
        "if False:\n    get_driver().on_startup(_on_startup)",
    ),
    (
        "M16",
        "恢复时按「现在+阈值」重排，而不是用记录里的**绝对时刻**（越重启越晚）",
        "__init__",
        "        when = datetime.fromtimestamp(unmute_at, tz=_scheduler_tz())",
        "        when = _now() + timedelta(minutes=10)",
    ),
    (
        "M17",
        "store 的 `(group_id,user_id)` 唯一约束失效 → 同一人叠出多条记录",
        "store",
        "    UNIQUE (group_id, user_id)",
        "    UNIQUE (group_id, user_id, unmute_at)",
    ),
    (
        "M18",
        "恢复时丢掉时区（naive）→ 服务器时区与调度器时区不一致时整体偏移数小时",
        "__init__",
        "when = datetime.fromtimestamp(unmute_at, tz=_scheduler_tz())",
        "when = datetime.fromtimestamp(unmute_at)",
    ),
    (
        "M19",
        "放弃读取调度器时区 → 又回到「靠服务器本地时区巧合正确」",
        "__init__",
        '    return getattr(scheduler, "timezone", None)',
        "    return None",
    ),
    (
        "M20",
        "默认上限退回 480（不再是平台最长 2,592,000 秒）",
        "config",
        "default=QQ_BAN_MAX_MINUTES,",
        "default=480,",
    ),
    (
        "M21",
        "平台边界常量写成「秒」以外的错值 → 分钟换算不再等于 2,592,000 秒",
        "config",
        "QQ_BAN_MAX_SECONDS: Final[int] = 2_592_000",
        "QQ_BAN_MAX_SECONDS: Final[int] = 43_200",
    ),
    # ── v2 运行时匹配层（2026-10-06）────────────────────────────────
    # 每条都对应一个「改坏了会静默出大事」的点，注释里写了后果。
    (
        "M22",
        "监听器改成 block=True —— 它是 catch-all，会把**所有**群消息吞掉、别的插件再也收不到",
        "__init__",
        "priority=plugin_config.lottery_match_priority,\n    block=False,\n)",
        "priority=plugin_config.lottery_match_priority,\n    block=True,\n)",
    ),
    (
        "M23",
        "命中后不 stop_propagation —— 引擎同优先级并发跑完所有 matcher，会和别的插件重复处理同一条消息",
        "__init__",
        "    matcher.stop_propagation()\n",
        "    pass  # MUTATED: 不阻断\n",
    ),
    (
        "M24",
        "用 CQ 原文匹配（而不是 get_plaintext）—— 图片/at 段会被当成正文，正则极易误命中",
        "__init__",
        "hit = words.find_match(store.list_words(), event.get_plaintext())",
        "hit = words.find_match(store.list_words(), str(event.get_message()))",
    ),
    (
        "M25",
        "CRUD 命令优先级 ≥ 监听器 —— 词表含模糊词「匹配词」时，发「查看匹配词」会既列表又抽奖",
        "handle",
        "CRUD_PRIORITY: Final[int] = 3",
        "CRUD_PRIORITY: Final[int] = 5",
    ),
    (
        "M26",
        "删除回执「先删再查」—— 查出来全是 None，「已删除 N 条」下面一条都列不出来",
        "handle",
        "    snapshot = {i: store.get_word(i) for i in ids}\n",
        "    store.remove_words(ids)\n    snapshot = {i: store.get_word(i) for i in ids}\n",
    ),
    (
        "M27",
        "渲染不转义用户输入 —— 匹配词里的 `<`/`&` 会破坏 HTML",
        "render",
        "f'<span class=\"word\">{html_mod.escape(r.text)}</span>'",
        "f'<span class=\"word\">{r.text}</span>'",
    ),
    (
        "M28",
        "渲染失败时抛异常（不回退）—— 命令会变成「毫无反应」",
        "render",
        'logger.warning("抽奖 CRUD 渲染失败，回退纯文本: %s: %s", type(e).__name__, e)\n'
        "        return None\n",
        "        raise\n",
    ),
    (
        "M29",
        "权限闸门放行所有人 —— 普通群成员也能删词",
        "handle",
        "    if str(user_id) in {str(u) for u in superusers}:\n        return True",
        "    return True  # MUTATED: 放行所有人",
    ),
    (
        "M30",
        "写入时不校验正则 —— 坏正则在每条消息上都抛一次，规则永远静默失效",
        "handle",
        '    if (bad := words.validate_word(match_type, word)) is not None:\n'
        '        await _reply(add_cmd, title="这条加不了", lines=[bad], tone="error")',
        "    pass  # MUTATED: 不校验",
    ),
    (
        "M31",
        "词表条数上限不生效 —— 每条群消息都要过一遍词表，无上限会被堆到很慢",
        "handle",
        "    if store.count_words() >= words.MAX_WORDS:",
        "    if False:  # MUTATED: 不看上限",
    ),
    (
        "M32",
        "空文本也参与匹配 —— 空消息（/纯图片消息）会命中 `.*` 这类规则",
        "words",
        '    if not text:\n        return False\n    if match_type == "contains":',
        '    if match_type == "contains":',
    ),
    (
        "M33",
        "多条命中时取**最后**一条 —— 匹配顺序不再等于「先加先生效」，用户看列表也推不出来",
        "words",
        "        if match_word(match_type, word, text):\n"
        '            return Hit(word_id=int(row["id"]), match_type=match_type, word=word)\n'
        "    return None",
        "        if match_word(match_type, word, text):\n"
        '            last = Hit(word_id=int(row["id"]), match_type=match_type, word=word)\n'
        '    return locals().get("last")',
    ),
]


def run_pytest() -> int:
    proc = subprocess.run(
        f"{ENV_PREFIX} uv run python -m pytest tests/ -q -p no:cacheprovider",
        shell=True,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode


def main() -> int:
    originals = {key: path.read_text(encoding="utf-8") for key, path in TARGETS.items()}

    baseline = run_pytest()
    print(f"基线（未变异）: exit={baseline}  {'✅ 绿' if baseline == 0 else '❌ 红'}")
    if baseline != 0:
        print("基线不是绿的，变异检验无意义 —— 先修好")
        return 2

    rows: list[tuple[str, str, str]] = []
    missed = 0
    for mid, desc, target, old, new in MUTATIONS:
        source = originals[target]
        if old not in source:
            rows.append((mid, desc, "⚠️ 锚点未命中（脚本要修）"))
            missed += 1
            continue
        try:
            TARGETS[target].write_text(source.replace(old, new, 1), encoding="utf-8")
            code = run_pytest()
        finally:
            TARGETS[target].write_text(source, encoding="utf-8")
        caught = code != 0
        rows.append((
            mid,
            desc,
            "✅ 变红（测试抓到了）" if caught else "❌ 仍绿（测试是空壳）",
        ))
        if not caught:
            missed += 1

    restored = run_pytest()
    print(f"还原后: exit={restored}  {'✅ 绿' if restored == 0 else '❌ 红'}")

    print("\n" + "=" * 78)
    for mid, desc, verdict in rows:
        print(f"{mid}  {verdict}\n     {desc}")
    print("=" * 78)
    print(
        f"结果：{len(MUTATIONS) - missed}/{len(MUTATIONS)} 个变异被抓到"
        + ("" if missed == 0 else f"，{missed} 个漏网 ⚠️")
    )

    if restored != 0:
        print("⚠️ 还原后测试不绿 —— 源码可能没恢复干净")
        return 3
    return 0 if missed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
