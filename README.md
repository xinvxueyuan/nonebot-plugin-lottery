# nonebot-plugin-lottery

抽奖（**禁言小助手**）—— 群里命中「匹配词」的人会被随机禁言一段时间，我看看谁这么贱。

> 抽取自 [zhenxun_bot](https://github.com/HibiKier/zhenxun_bot) 的
> `zhenxun/plugins/lottery`（原作者 **阿珏酱**），把真寻内部件
> （`zhenxun.configs` / `zhenxun.services.log` / `zhenxun.utils.message`）
> 换成原生 NoneBot2 等价物。
>
> **v2.0**：触发词改为**运行时匹配词表**（存 sqlite）+ CRUD 命令 + htmlkit 卡片。

## 功能

- 群消息命中**运行时匹配词表**里的任一条 → 在 `[MIN, MAX]` 分钟之间随机抽一个数，**禁言发消息的人自己**：

  ```
  恭喜 阿百川(10001) 参与"自刎归天"！
  🎉 获得了 480分钟禁言大礼包 🎉
  ```

- 匹配词**不在代码里、也不在配置里**：存在 sqlite（`lottery_words` 表），群管理/超管可在**运行时**增删改，改词不用重启进程。
- 匹配对象是 `event.get_plaintext()`（**剥掉图片/at 段后的纯文本**），所以 `[图片]自刎归天` 也会命中。多条都命中时按 **id 升序**取第一条（= 先加先生效）。
- 抽到的时长**超过阈值**（默认 10 分钟）时，挂一次性任务**到点调 API 解禁**（幂等；同一用户二次抽奖覆盖旧任务）；没超过就让平台自然到期。**禁言时长就是抽到的值，不做封顶**。
- 待解禁任务**容灾持久化**在 sqlite，bot 重启/被杀后启动时重建（见下文）—— 被抽到 480 分钟的人不会被真关 8 小时。
- 要求 bot 在该群是管理员，否则回「<命中的词>禁言失败，可能是机器人没有禁言权限」。
- CRUD 的**所有**回复默认渲染成 [nonebot-plugin-htmlkit](https://pypi.org/project/nonebot-plugin-htmlkit/) 卡片（用户要求），渲染不可用时自动回退纯文本 —— 渲染问题绝不会表现成「命令没反应」。

## 命令

| 命令 | 别名 | 说明 |
|---|---|---|
| `增加匹配词 <类型> <词>` | `添加匹配词`、`新增匹配词` | 加一条匹配词，命中即随机禁言 |
| `删除匹配词 <id> [id ...]` | `移除匹配词` | 按 id 删（可一次多个；空格 / 逗号 / 顿号分隔） |
| `更新匹配词 <id> <类型> <词>` | `修改匹配词` | 改第 `<id>` 条的类型与内容 |
| `查看匹配词` | `匹配词列表`、`查看抽奖匹配词` | 列出全部（带 id，按 id 升序 = 匹配顺序） |

```
查看匹配词                      列出全部（带 id）
增加匹配词 模糊 自刎归天        消息里**包含**它就触发
增加匹配词 开头 抽奖            消息以它**开头**才触发
更新匹配词 3 精准 抽奖          改第 3 条的类型/内容
删除匹配词 3 5                  按 id 删（可一次多个）
```

> ⚠️ 触发抽奖的**不是命令**，而是运行时匹配词 —— 词表里任意一条命中群消息即触发抽奖。
> 参数里的**词可以含空格**（只按第一个空白切分「类型」与「词」），中文梗常有空格。

### 匹配类型（对齐 NoneBot 内置规则的语义）

| 类型 | 写法 | 语义 | 对齐的 NoneBot 规则 |
|---|---|---|---|
| 模糊 | `模糊` / `包含` | 纯文本**包含** | `on_keyword` |
| 开头 | `开头` / `前缀` | 纯文本以它**开头** | `on_startswith` |
| 结尾 | `结尾` / `后缀` | 纯文本以它**结尾** | `on_endswith` |
| 精准 | `精准` / `全等` | 纯文本**完全相同** | `on_fullmatch` |
| 正则 | `正则` | 对纯文本做 `re.search` | `on_regex` |

匹配对象是 `event.get_plaintext()`（**剥掉图片/at 段后的纯文本**），所以
`[图片]自刎归天` 也会命中。多条都命中时按 **id 升序**取第一条（= 先加先生效）。

> ⚠️ 与 `on_regex` 的**一处有意差异**：NoneBot 的 `RegexRule` 是对
> `str(event.get_message())`（含 `[CQ:image,...]` 段）做 search，本插件统一用纯文本。
> 理由：另外四种都基于纯文本，正则单独用原始串会让规则不可预期（CQ 串里全是
> `[`/`]`，极易误命中）。要匹配 CQ 串请改用别的办法。

### CRUD 权限

| 场景 | 谁可以 |
|---|---|
| 群内 | 群主 / 管理员 / 超管 |
| 私聊 | **仅超管**（NoneBot `SUPERUSERS`） |

### 回复都是图片

CRUD 的**所有**回复都由 [nonebot-plugin-htmlkit](https://pypi.org/project/nonebot-plugin-htmlkit/)
渲染成卡片（用户要求）。它是**可选依赖**，装不上/渲染失败时自动回退纯文本 ——
渲染问题绝不会表现成「命令没反应」。

## 配置

环境变量（`.env` / `.env.dev`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `LOTTERY_MIN_MUTE_TIME` | `1` | 抽奖禁言最短时间（**分钟**）= 平台最短 **60 秒** |
| `LOTTERY_MAX_MUTE_TIME` | `43200` | 抽奖禁言最长时间（**分钟**）= 平台最长 **2,592,000 秒**（30 天）；报多少就**真禁**多久 |
| `LOTTERY_UNMUTE_AFTER_MINUTES` | `10` | 禁言超过这个分钟数时，到点由机器人**调 API 解禁** |
| `LOTTERY_PERSIST_PENDING` | `true` | 解禁任务的**容灾持久化**（落 sqlite + 启动重建） |
| `LOTTERY_MATCH_PRIORITY` | `5` | 监听所有群消息的 matcher 优先级；**必须 > 3**（CRUD 命令的优先级） |

> 触发词**不在配置里**：它们只在数据库（`lottery_words` 表），用 CRUD 命令管理。

`MIN > MAX` 写反了会自动对调并打 warning，不会让插件起不来。

### 单位说明（为什么配置是分钟，而不是秒）

QQ 群禁言的平台边界是**按秒**的：最短 60 秒、最长 2,592,000 秒（30 天）。
本插件配置沿用上游真寻插件的**分钟**单位（也是报给群里看的单位），
因此区间写作 `1 ~ 43200` 分钟 —— 与 `60 ~ 2,592,000` 秒**整除等价**。

换算关系在 `config.py` 里具名成 `QQ_BAN_MIN_SECONDS` / `QQ_BAN_MAX_SECONDS`
（及对应的 `*_MINUTES`），**不要**在代码里散裸数字：看到 `43200` 没人会
立刻想到「30 天」，看到 `QQ_BAN_MAX_SECONDS` 会。超出边界的值会被 pydantic 拦下。

## 依赖与安装

- 运行依赖（以 `pyproject.toml` 的 `dependencies` 为准）：`nonebot2 >= 2.5.0`、
  `nonebot-adapter-onebot >= 2.4.6`、`nonebot-plugin-apscheduler >= 0.5.0`（调度）、
  `nonebot-plugin-localstore >= 0.7.4`（解禁记录的容灾库放在它的插件数据目录）
- 可选依赖（extra `htmlkit`）：`nonebot-plugin-htmlkit == 0.1.0rc5`（CRUD 回复渲染成图；缺了回退纯文本）
- Python：`>= 3.10`
- 安装（宿主以 git 子模块引入）：

```bash
git submodule add -b main https://github.com/xinvxueyuan/nonebot-plugin-lottery.git \
  vendor/nonebot-plugin-lottery
git -C vendor/nonebot-plugin-lottery checkout <钉定的 SHA>
```

- 宿主 `[tool.nonebot] plugin_dirs` 加一行 `vendor/nonebot-plugin-lottery/src/plugins`
  （本仓自带的 `plugin_dirs = ["src/plugins"]` 是插件独立开发时用的）

## 测试与门禁

```bash
uv run python -m pytest tests/ -q
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run python scripts/mutation_check.py   # 变异检验：故意写坏行为，看测试是否变红
```

> 本机 git-bash 里跑要先剔除宿主环境变量（否则 uv 会挑错解释器/venv）：
> 每条命令前缀 `env -u UV_PYTHON -u UV_PROJECT_ENVIRONMENT -u SSL_CERT_FILE -u VIRTUAL_ENV -u PYTHONPATH`；
> 首次还要 `env -u ... uv sync --all-groups`。`scripts/mutation_check.py` 自己也是这么调子进程的。

## 触发词为什么是「运行时」的（以及两个坑）

内置规则（`on_command` / `on_keyword`）在**导入时**就把词固化进 rule，改词要重启进程。
用户要求「加个 hook 监控所有群消息、自行解析，方便动态调整」，于是：

```
on_message(rule=is_type(GroupMessageEvent), block=False)   ← 兜住所有群消息
   └─ handler 里查 DB 词表（words.find_match）→ 没命中就 return（零副作用）
                                            → 命中才 stop_propagation() + 禁言 + 回消息
```

两个坑（都有测试钉住，改代码前先读）：

1. **`block=False` 是安全底线**：它兜的是所有群消息，`block=True` 会把它们全吞掉、
   别的插件再也收不到。阻断只允许在**命中**时用 `matcher.stop_propagation()`
   （引擎在 handler 返回后才读 `matcher.block` 并抛 `StopPropagation`）。
2. **CRUD 命令的优先级必须更小**（`handle.CRUD_PRIORITY = 3` < 监听器的 5）：
   CRUD 命令是 `block=True`，靠更小的优先级抢先。否则词表里一旦有模糊词「匹配词」，
   发一条「查看匹配词」会**既列列表又抽奖**（把自己禁言）。

匹配顺序 = 词表 id 升序，可在「查看匹配词」里直接读出来；想调整顺序就删掉重加。

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

所以真相落在 sqlite（`lottery_pending_unmute` 表），apscheduler 只负责调度
（另有 `lottery_words` 表存运行时匹配词，见上文）：

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

## 许可

MIT（见 `pyproject.toml` 的 `license` 字段；仓库暂未附 `LICENSE` 文件）。

> 说明：`pyproject.toml` 的 `license` 字段标为 MIT（沿用上游抽取来源 zhenxun_bot 的
> `lottery` 插件的许可声明），但仓库根目录没有 `LICENSE` 文件。
