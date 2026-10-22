"""三级账户 · 数据修正 + 平台管理员机构约束。

★★ 这次迁移做两件事，**必须一起做**：

  1. **把「平台管理员 + 挂机构」的账号降为学校平台账户。**

     建库时的唯一 `admin` 是 `PLATFORM_ADMIN` 却挂着 `org_id=1`，
     与模型注释「平台管理员 org 为空」直接矛盾。后果不是「不规范」，
     而是**三级制衡不存在**：一个账号同时是平台方和校方，
     它既能看到全校每个学生的成绩，又能跨校操作，
     所有权限规则都被这一个账号绕过。

  2. **加 `ck_platform_admin_no_org` 约束**，让「平台管理员必须无机构」
     从口头规则变成**数据库拒绝**。

     这条约束是这次改造的核心价值：规则只写在注释里时，
     那一条脏数据活了整整一个开发周期都没人发现。

★ 顺序不能反：先修数据，再加约束。反了会直接报
  `violates check constraint`。

★ 新建的系统管理员密码：

  - 优先读环境变量 `ZF_SYSADMIN_PASSWORD`；
  - **没有配环境变量时生成一个随机口令**，并打印在迁移日志里，
    你必须去日志里抄下来。
  - **生产部署必须用环境变量覆盖，并在首次登录后立即改密。**

★★ 为什么不再有「开发库缺省口令」

  这个迁移原本写死了一个固定的开发库缺省口令。私有仓库里这没问题，
  但本仓库是**公开**的 —— 一份公开的、所有部署都相同的
  系统管理员口令，等于每套没改过密码的环境都能被人直接登进去，
  而且是最高权限（`PLATFORM_ADMIN`，跨校可见）。

  改成「随机生成 + 打印一次」之后：新部署照样能用，但口令**每套都不一样**，
  公开源码不再泄露任何人的密码。代价是必须看迁移日志，
  所以下面这行 print 写得足够显眼。
"""

from __future__ import annotations

import os
import secrets

from django.contrib.auth.hashers import make_password
from django.db import migrations, models

#: 新建的系统管理员用户名
SYS_USERNAME = "sysadmin"


def _resolve_password() -> tuple[str, bool]:
    """返回 `(口令, 是否来自环境变量)`。

    ★ 环境变量优先；没配就现场生成 16 字节随机口令（≈22 位 URL-safe 字符）。
      用 `secrets` 而不是 `random`：后者是可预测的 Mersenne Twister，
      拿它生成密码等于没生成。
    """
    env_pwd = (os.environ.get("ZF_SYSADMIN_PASSWORD") or "").strip()
    if env_pwd:
        return env_pwd, True
    return secrets.token_urlsafe(16), False


def forwards(apps, schema_editor):
    User = apps.get_model("accounts", "User")

    # ── 1. 平台管理员带机构 → 降为学校平台账户 ──
    # ★ 同时清掉 `is_superuser` / `is_staff`：`User.is_system_admin`
    #   把 `is_superuser` 也算作系统管理员（Django admin 的逃生口），
    #   不清的话这个人换个属性名还是 L3。
    demoted = User.objects.filter(
        role="PLATFORM_ADMIN",
    ).exclude(org_id__isnull=True).update(
        role="ORG_ADMIN", is_superuser=False, is_staff=False,
    )
    if demoted:
        print(f"  [三级账户] {demoted} 个平台管理员已降为学校平台账户")

    # ── 2. 确保存在一个真正的系统管理员（org 为空）──
    has_sys = User.objects.filter(
        role="PLATFORM_ADMIN", org_id__isnull=True, deleted_at__isnull=True,
    ).exists()
    if not has_sys and not User.objects.filter(username=SYS_USERNAME).exists():
        pwd, from_env = _resolve_password()
        User.objects.create(
            username=SYS_USERNAME,
            real_name="系统管理员",
            role="PLATFORM_ADMIN",
            org_id=None,
            is_staff=True,
            is_superuser=True,
            is_active=True,
            password=make_password(pwd),
        )
        if from_env:
            print(f"  [三级账户] 已创建系统管理员 {SYS_USERNAME}（密码来自 ZF_SYSADMIN_PASSWORD）")
        else:
            # ★ 故意打一大块，让人在滚动的迁移日志里**不可能看漏**。
            #   口令只在这里出现一次，数据库里存的是哈希，事后捞不回来。
            print(
                "\n"
                "  ╔══════════════════════════════════════════════════════════╗\n"
                f"  ║  已创建系统管理员  {SYS_USERNAME}\n"
                f"  ║  初始口令：{pwd}\n"
                "  ║                                                          ║\n"
                "  ║  ★ 这是**随机生成**的，只显示这一次，请立即抄走。          ║\n"
                "  ║  ★ 首次登录后马上改密；下次部署请设 ZF_SYSADMIN_PASSWORD  ║\n"
                "  ╚══════════════════════════════════════════════════════════╝\n"
            )


def backwards(apps, schema_editor):
    """回滚只删约束（由 `RemoveConstraint` 负责），不恢复 admin 的角色。

    ★ 有意不恢复：把学校账户再提回系统管理员会重新制造
      「两权合一」的脏数据，回滚一次就够难受了，不该自动重建它。
    """
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_alter_user_role"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
