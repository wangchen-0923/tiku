"""本地开发凭据读取（★ 口令不入库）。

★★ 为什么要有这个文件

  验收脚本里原本写死了学校平台账户的用户名与口令。在**私有**仓库里
  这没什么，但这个仓库是**公开**的：

  - `admin` 的默认口令；
  - `sysadmin` 的初始口令。

  一旦公开，任何部署只要没改过默认密码，看到仓库的人就能直接登进去。
  而且**推上去就进了 git 历史**，事后删文件也删不掉，只能重写历史。

  所以口令从源码里搬出来，只留在两处：

    1. 环境变量（CI / 临时覆盖用）
    2. `scripts/.devcreds.local`（本机文件，**已在 .gitignore 里**）

  ★ 默认值一个都不给。取不到就**明确报错并说清怎么办**——
    「悄悄用一个猜的口令去登录、然后报 401」是最难查的失败方式。

★ 用法

    from _devcreds import admin_login, sysadmin_login

    st, j = call(f"{API}/auth/login/", method="POST", body=admin_login())

★ 首次配置

    cp scripts/.devcreds.example scripts/.devcreds.local
    # 然后按你的开发库实际口令改这个文件
"""

from __future__ import annotations

import os
from pathlib import Path

#: 本机凭据文件。★ 与 .py 同目录，随脚本一起被 .gitignore 忽略。
LOCAL_FILE = Path(__file__).with_name(".devcreds.local")

#: 缺失时的提示。★ 必须能照着做完，不能只说「缺配置」。
_HOWTO = """\
未找到本地开发凭据，验收脚本无法登录。

  请在仓库根目录执行（Windows PowerShell / cmd 均可）：

      copy scripts\\.devcreds.example scripts\\.devcreds.local

  然后编辑 scripts/.devcreds.local，填上你这套开发库的真实口令：

      ZF_DEV_ADMIN_PASSWORD=你的学校平台账户口令
      ZF_DEV_SYSADMIN_PASSWORD=你的系统管理员口令      # 只跑学生端脚本可不填

  也可以用环境变量临时覆盖（环境变量优先级更高）：

      set ZF_DEV_ADMIN_PASSWORD=...

  ★ 这个文件不会入库（见 .gitignore），不要改成别的文件名。
"""


def _read_local() -> dict[str, str]:
    """读 `KEY=VALUE` 形式的本地凭据文件。容忍注释、空行、CRLF。"""
    if not LOCAL_FILE.exists():
        return {}

    out: dict[str, str] = {}
    # ★ 用 utf-8-sig：Windows 记事本另存为 UTF-8 时可能带 BOM，
    #   带 BOM 时第一个键名会变成 "\ufeffZF_DEV_..."，查不到，
    #   表现是「明明填了却说没填」。
    for raw in LOCAL_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def get(name: str, default: str = "") -> str:
    """取一项配置：环境变量 > 本地文件 > 默认值。"""
    value = os.environ.get(name)
    if value:
        return value
    return _read_local().get(name, default)


def require(name: str) -> str:
    """取一项**必须有**的配置，缺了就带着配置指引报错。"""
    value = get(name)
    if value:
        return value
    raise RuntimeError(f"缺少 {name}\n\n{_HOWTO}")


def admin_login() -> dict[str, str]:
    """学校平台账户（`ORG_ADMIN`）的登录体。

    ★ 返回**新 dict**，调用方随便改不会污染别人。
    """
    return {
        "username": get("ZF_DEV_ADMIN_USERNAME", "admin"),
        "password": require("ZF_DEV_ADMIN_PASSWORD"),
    }


def sysadmin_login() -> dict[str, str]:
    """系统管理员（`PLATFORM_ADMIN`）的登录体。"""
    return {
        "username": get("ZF_DEV_SYSADMIN_USERNAME", "sysadmin"),
        "password": require("ZF_DEV_SYSADMIN_PASSWORD"),
    }
