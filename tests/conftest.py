"""pytest 全局初始化。

**为什么必须有这个文件**：本包的 `__init__.py` 是 NoneBot 插件入口，模块级就调用
`on_command()` / `require()` / `get_plugin_config()`。测试要
`from nonebot_plugin_lottery import ...` —— 这会**先执行 `__init__.py`**，
没有初始化过的 driver 会直接抛 `ValueError: NoneBot has not been initialized.`
（`require()` 与 apscheduler 的 `get_driver()` 都会踩）。

`conftest.py` 会被 pytest 最先导入，所以在这里 `nonebot.init()` 正好赶在
任何测试模块 import 插件之前。
"""

import nonebot
from nonebot.adapters.onebot.v11 import Adapter
import pytest

# 用 **none 驱动**：单元测试不需要任何服务器/客户端连接，
# 这样就不必为跑测试装 fastapi+uvicorn 这一大坨。
nonebot.init(driver="~none", log_level="WARNING")
nonebot.get_driver().register_adapter(Adapter)


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    """**每个测试**都把容灾库指到临时目录（autouse，忘不掉）。

    这是安全护栏：`store.default_db_path()` 指向 localstore 的**真实**插件数据目录。
    哪个测试漏了隔离、又踩到懒初始化，就会往本机（乃至生产）的数据目录里写表 ——
    这类污染在本地根本发现不了。

    用 monkeypatch 设 `_db_path` 还有一个好处：测试结束后自动还原，
    测试之间不会互相串库。
    """
    from nonebot_plugin_lottery import store

    path = tmp_path / "lottery.sqlite3"
    monkeypatch.setattr(store, "_db_path", path, raising=False)
    store.init(path)
    return path


@pytest.fixture
def tmp_store(_isolate_store):
    """需要库路径时用它（与 autouse 的隔离是同一个文件）。"""
    return _isolate_store
