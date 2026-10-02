"""变异检验：故意把「封顶/隐性/定时解禁」写坏，看测试是否变红。

只跑 pytest 的退出码 —— 绿色说明测试**没**抓到变异，即那个断言是空壳。
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent  # scripts/ 的上一级 = 仓库根
SRC = ROOT / "src/plugins/nonebot_plugin_lottery/__init__.py"
BAK = ROOT / "src/plugins/nonebot_plugin_lottery/__init__.py.bak-mutation"

ENV_PREFIX = (
    "env -u UV_PYTHON -u UV_PROJECT_ENVIRONMENT -u SSL_CERT_FILE "
    "-u VIRTUAL_ENV -u PYTHONPATH"
)

# (编号, 说明, 原文, 变异后)
MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "M1",
        "去掉实际禁言的封顶（只靠定时任务）→ 这正是「bot 一重启就真关 8 小时」的写法",
        "            duration=actual * 60,",
        "            duration=rolled * 60,",
    ),
    (
        "M2",
        "回复里报封顶后的值（破坏「隐性」，群里会发现只关了 10 分钟）",
        '        f"🎉 获得了 {rolled}分钟禁言大礼包 🎉"',
        '        f"🎉 获得了 {actual}分钟禁言大礼包 🎉"',
    ),
    (
        "M3",
        "定时解禁只写日志、不真的挂 apscheduler 任务（空壳实现）",
        "        scheduler.add_job(",
        "        _noop = lambda: None\n        _dead = lambda: None\n        _dead(",
    ),
    (
        "M4",
        "超时判据 `>` 写成 `>=`（抽到正好 10 分钟也去挂任务）",
        "    if rolled > actual:",
        "    if rolled >= actual:",
    ),
    (
        "M5",
        "定时解禁用了封顶时长而不是 0（等于没解禁）",
        "        await bot.set_group_ban(group_id=group_id, user_id=user_id, duration=0)",
        "        await bot.set_group_ban(\n"
        "            group_id=group_id, user_id=user_id, duration=600\n"
        "        )",
    ),
    (
        "M6",
        "把 Bot/GroupMessageEvent 挪进 TYPE_CHECKING（依赖注入会静默失效）",
        "from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent",
        "if TYPE_CHECKING:\n    from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent",
    ),
    (
        "M7",
        "`min(rolled, cap)` 写成 `max(rolled, cap)`（封顶变成下限）",
        "    return rolled, min(rolled, cap)",
        "    return rolled, max(rolled, cap)",
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
    original = SRC.read_text(encoding="utf-8")
    BAK.write_text(original, encoding="utf-8")

    baseline = run_pytest()
    print(f"基线（未变异）: exit={baseline}  {'✅ 绿' if baseline == 0 else '❌ 红'}")
    if baseline != 0:
        print("基线就不是绿的，变异检验无意义 —— 先修好")
        return 2

    rows: list[tuple[str, str, str]] = []
    failures = 0
    for mid, desc, old, new in MUTATIONS:
        if old not in original:
            rows.append((mid, desc, "⚠️ 锚点未命中（脚本要修）"))
            failures += 1
            continue
        try:
            SRC.write_text(original.replace(old, new, 1), encoding="utf-8")
            code = run_pytest()
        finally:
            SRC.write_text(original, encoding="utf-8")
        caught = code != 0
        rows.append((
            mid,
            desc,
            "✅ 变红（测试抓到了）" if caught else "❌ 仍绿（测试是空壳）",
        ))
        if not caught:
            failures += 1

    restored = run_pytest()
    print(f"还原后: exit={restored}  {'✅ 绿' if restored == 0 else '❌ 红'}")

    print("\n" + "=" * 78)
    for mid, desc, verdict in rows:
        print(f"{mid}  {verdict}\n     {desc}")
    print("=" * 78)
    print(
        f"结果：{len(MUTATIONS) - failures}/{len(MUTATIONS)} 个变异被抓到"
        + ("" if failures == 0 else f"，{failures} 个漏网 ⚠️")
    )
    if restored != 0:
        print("⚠️ 还原后测试不绿 —— 源码可能没恢复干净，检查 .bak-mutation")
        return 3
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
