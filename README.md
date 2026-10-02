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
| `LOTTERY_MAX_MUTE_TIME` | `480` | 抽奖禁言最长时间（分钟）；**回复里报的就是抽到的这个数** |
| `LOTTERY_MUTE_CAP_MINUTES` | `10` | **实际**禁言上限（分钟） |

`MIN > MAX` 写反了会自动对调并打 warning，不会让插件起不来。

## 「超时自动解禁」是怎么做的（两层，缺一不可）

用户要求的是「**原样但超 10 分钟的定时自动解禁（隐性）**」，落地成两层：

1. **实际禁言时长 = `min(抽到的数, LOTTERY_MUTE_CAP_MINUTES)`**
   —— 让平台侧到点自己解开，**不依赖本进程活着**。
2. 抽到超时值时，**额外**挂一个 apscheduler 一次性任务在
   `+上限` 分钟时 `set_group_ban(duration=0)` 精确解禁（幂等）。

为什么不能只靠第 2 层：apscheduler 的任务是**进程内内存态**，bot 一重启就丢了。
生产机（qbot）3 天里重启过 12 次，只靠定时任务的话，被抽到 480 分钟的人
会被**真关 8 小时**。把封顶做进 `duration` 后，无论 bot 死没死，
用户最多被关 10 分钟；定时任务退化成「更精确的兜底」。

**「隐性」= 回复里报的仍是抽到的原始数字**（保留原插件的玩笑效果），
不告诉群里「其实只关了 10 分钟」。

## 依赖

- `nonebot2 >= 2.5`
- `nonebot-adapter-onebot >= 2.4.6`
- `nonebot-plugin-apscheduler >= 0.5`（第 2 层定时解禁用）

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
