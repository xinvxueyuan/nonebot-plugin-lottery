# nonebot-plugin-lottery

抽奖（**禁言小助手**）—— 参与抽奖的群员会被随机禁言一段时间，我看看谁这么贱。

> 抽取自 [zhenxun_bot](https://github.com/HibiKier/zhenxun_bot) 的
> `zhenxun/plugins/lottery`（原作者 **阿珏酱**，v1.1），把真寻内部件
> （`zhenxun.configs` / `zhenxun.services.log` / `zhenxun.utils.message`）
> 换成原生 NoneBot2 等价物，行为保持一致。

## 用法

群里发送：

```
抽奖
```

回：

```
恭喜 阿百川(10001) 参与"抽奖"！
🎉 获得了 480分钟禁言大礼包 🎉
```

也就是**禁言自己**（点名的就是发命令的人）。要求 bot 在该群是管理员，
否则会回「抽奖禁言失败，可能是机器人没有禁言权限」。

## 配置

环境变量（`.env` / `.env.dev`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `LOTTERY_MIN_MUTE_TIME` | `1` | 抽奖禁言最短时间（分钟） |
| `LOTTERY_MAX_MUTE_TIME` | `480` | 抽奖禁言最长时间（分钟）；报多少就**真禁**多久 |
| `LOTTERY_UNMUTE_AFTER_MINUTES` | `10` | 禁言超过这个分钟数时，到点由机器人**调 API 解禁** |
| `LOTTERY_PERSIST_PENDING` | `true` | 解禁任务的**容灾持久化**（落 sqlite + 启动重建） |

`MIN > MAX` 写反了会自动对调并打 warning，不会让插件起不来。

## 「超 N 分钟自动解禁」是怎么做的

要求是「**原样但超 10 分钟的定时自动解禁（隐性）**」，语义是
**到时间再调用 API 解禁**，而**不是**把禁言时长砍成 10 分钟：

1. **禁言时长 = 抽到的原始值**（抽到 480 就真禁 480 分钟）——`duration=rolled*60`，不封顶。
2. 抽到的值 > 阈值（10）时，用 **nonebot-plugin-apscheduler** 挂一个一次性任务，
   在 **+10 分钟**时调 `set_group_ban(duration=0)` 把人主动放出来
   （幂等；同一用户二次抽奖覆盖旧任务）。

抽到的值 ≤ 阈值时不挂任务，等平台自然到期即可。

**「隐性」= 回复里报的数字与真实禁言时长一致**，但群里不会被告知
「其实 10 分钟后会被机器人提前放出来」。

## 容灾持久化（为什么还要一张 sqlite 表）

apscheduler 的任务默认只在**进程内存**里（`MemoryJobStore`）：bot 一重启，
等着解禁的任务就全没了 —— 被抽到 480 分钟的人会被真关 8 小时，而日志里
什么都看不出来。生产 qbot 3 天里重启过 12 次，不是小概率。

所以真相落在 sqlite（`lottery_pending_unmute` 表），apscheduler 只负责调度：

```
抽奖命中        → 先落库（unmute_at 绝对时刻 + expire_at 自然到期时刻），再挂任务
进程启动        → 读表重建：未到期的按原时刻重挂；已错过但未自然到期的**立刻**解禁；
                  连 expire_at 都过了的（平台早解开）直接清记录，不调无用 API
解禁成功        → 删记录
解禁失败/离线    → 记录留下 + attempts+1，60 秒后重试；超过 20 次才放弃并删记录
同一人二次抽奖   → 覆盖旧记录（唯一键 group_id+user_id），attempts 归零
```

### 为什么不用 apscheduler 自带的持久化 jobstore

`SQLAlchemyJobStore` 靠 pickle 保存**函数引用串**，恢复时 `__import__(模块名)`。
宿主 qbot 用 `nonebot.load_from_toml()` 加载 `vendor/` 下的插件，模块名是
从路径推导的**合成名**，带连字符：

```
vendor.nonebot-plugin-lottery.src.plugins.nonebot_plugin_lottery
```

连字符不是合法标识符 → `__import__` 必然失败。实测（2026-10-02）：

```
ref_to_obj("vendor.nonebot-plugin-lottery.src.plugins.nonebot_plugin_lottery:_unmute")
-> LookupError: Error resolving reference ...: could not import module
```

也就是说**即使 jobstore 写成功了，重启后也一定恢复不回来** —— 那种「持久化」
只会给人一种已经安全了的错觉。故采用「自建表 + 启动重建」，零新依赖、行为可测。

## 依赖

- `nonebot2 >= 2.5`
- `nonebot-adapter-onebot >= 2.4.6`
- `nonebot-plugin-apscheduler >= 0.5`（调度）
- `nonebot-plugin-localstore >= 0.7.4`（容灾库放在插件数据目录）

## 安装（宿主以 git 子模块引入）

```bash
git submodule add -b main https://github.com/xinvxueyuan/nonebot-plugin-lottery.git \
  vendor/nonebot-plugin-lottery
git -C vendor/nonebot-plugin-lottery checkout <钉定的 SHA>
```

宿主 `pyproject.toml`：

```toml
[tool.nonebot]
plugin_dirs = ["vendor/nonebot-plugin-lottery/src/plugins"]
```

## 开发

```bash
env -u UV_PYTHON -u UV_PROJECT_ENVIRONMENT -u SSL_CERT_FILE -u VIRTUAL_ENV -u PYTHONPATH uv sync --all-groups
env -u UV_PYTHON -u UV_PROJECT_ENVIRONMENT -u SSL_CERT_FILE -u VIRTUAL_ENV -u PYTHONPATH uv run pytest tests/ -q
env -u UV_PYTHON -u UV_PROJECT_ENVIRONMENT -u SSL_CERT_FILE -u VIRTUAL_ENV -u PYTHONPATH uv run ruff check src/ tests/
```

## License

MIT（沿用上游项目的许可）。
