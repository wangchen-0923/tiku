"""个人中心回归（U26）。

★★ 验的是出口判据，不是"接口能返回 200"：

  ① **改密码后旧令牌立刻失效**——这是本任务唯一有安全含义的判据
  ② **资产数与其来源页一致**：个人中心的积分必须等于积分页的积分
  ③ 资料修改只认白名单字段（`role` / `username` 传了也不生效）
  ④ 系统管理员（无机构）不因积分模块而白屏

★ 为什么必须走真实 JWT 链路

  DRF 的 `force_authenticate` 会**绕过**认证类，令牌根本不参与校验。
  用它测「旧令牌失效」永远是通过的——因为压根没检查令牌。
  所以这里一律 `client.credentials(HTTP_AUTHORIZATION="Bearer ...")`，
  让 `JWTAuthentication` 真的跑一遍。

★ 两种跑法

    python scripts/verify_profile_center.py                 # 真 PG（默认 15433）
    python scripts/verify_profile_center.py --engine sqlite  # 无库环境的逻辑自检
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass


def _bootstrap(engine: str) -> str | None:
    sys.path.insert(0, r"D:\tiku\server")
    sys.path.insert(0, r"D:\tiku\.devdeps")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
    os.environ["ZF_CACHE"] = "locmem"
    tmp_path = None

    if engine == "sqlite":
        os.environ["ZF_DB_ENGINE"] = "sqlite"
        tmp_path = os.path.join(
            tempfile.gettempdir(), f"zf_profile_selftest_{os.getpid()}.sqlite3"
        )
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        os.environ["ZF_SQLITE_PATH"] = tmp_path
    return tmp_path


PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        FAILED.append(label)
        print(f"  FAIL  {label}  {detail}")


def run() -> int:
    from django.core.management import call_command

    from apps.accounts.authentication import (
        ACCESS_TOKEN_LIFETIME,
        TOKEN_TYPE_ACCESS,
        decode_access_token,
        issue_tokens,
        password_fingerprint,
    )
    from apps.accounts.models import User
    from apps.org.models import Org, OrgType

    # ── 建独立测试机构（★ 绝不复用库里既有实体）──
    org = Org.objects.create(
        name="ZFTEST 个人中心自检", short_name="ZFTESTO",
        type=OrgType.SCHOOL,
    )
    teacher = User.objects.create_user(
        username="zfprof_teacher", password="prof-old-pwd-123",
        org=org, role="TEACHER", real_name="张三", phone="13800000001",
    )
    sysadmin = User.objects.create(
        username="zfprof_sys", real_name="系统管理员",
        role="PLATFORM_ADMIN", org=None, is_active=True,
    )
    sysadmin.set_password("prof-sys-pwd-123")
    sysadmin.save()

    from rest_framework.test import APIClient

    def authed(token: str) -> APIClient:
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return c

    try:
        # ══════════════════════════════════════════════════════
        # A 旧令牌失效（核心判据）
        # ══════════════════════════════════════════════════════
        print("\n== A 改密码后旧令牌失效 ==")

        tok = issue_tokens(teacher)
        old_access = tok["access_token"]

        # A1 令牌载荷带密码指纹
        payload = decode_access_token(old_access)
        check("令牌载荷含 pv（密码指纹）", "pv" in payload, str(payload))
        check("pv 等于当前密码指纹",
              payload.get("pv") == password_fingerprint(teacher))
        check("pv 长度 16", len(str(payload.get("pv"))) == 16,
              str(payload.get("pv")))

        # A2 改密前旧令牌可用
        r = authed(old_access).get("/api/v1/auth/me/")
        check("改密前旧令牌可用（200）", r.status_code == 200,
              f"{r.status_code} {getattr(r, 'data', '')}")

        # A3 自助改密
        r = authed(old_access).post(
            "/api/v1/auth/change-password/",
            {"old_password": "prof-old-pwd-123",
             "new_password": "prof-new-pwd-456"},
            format="json",
        )
        check("改密码成功（200）", r.status_code == 200,
              f"{r.status_code} {getattr(r, 'data', '')}")

        # A4 ★ 旧令牌必须失效
        r = authed(old_access).get("/api/v1/auth/me/")
        check("★ 改密后旧令牌失效（401）", r.status_code == 401,
              f"got {r.status_code}")

        # A5 旧令牌失效后，刷新令牌也不能换新的
        r = APIClient().post(
            "/api/v1/auth/refresh/",
            {"refresh_token": tok["refresh_token"]},
            format="json",
        )
        check("改密后旧刷新令牌也被拒（非 200）", r.status_code != 200,
              f"got {r.status_code}")

        # A6 新密码登录后令牌可用
        r = APIClient().post(
            "/api/v1/auth/login/",
            {"username": "zfprof_teacher", "password": "prof-new-pwd-456"},
            format="json",
        )
        check("新密码登录成功（200）", r.status_code == 200,
              f"{r.status_code} {getattr(r, 'data', '')}")
        new_token = ((r.data or {}).get("data") or {}).get("access_token")
        check("登录返回新令牌", bool(new_token))
        r = authed(new_token or "").get("/api/v1/auth/me/")
        check("新令牌可用（200）", r.status_code == 200,
              f"got {r.status_code}")

        # A7 管理员重置密码同样让旧令牌失效
        teacher.refresh_from_db()
        admin_tok = issue_tokens(teacher)["access_token"]
        sysadmin.set_password("prof-sys-pwd-999")
        sysadmin.save()
        # 用服务层重置（真实路径）
        from apps.accounts.services import UserService

        UserService.reset_password(
            org_id=org.id, user_id=teacher.id,
            new_password="prof-reset-pwd-789", operator=sysadmin,
            operator_id=sysadmin.id,
        )
        r = authed(admin_tok).get("/api/v1/auth/me/")
        check("★ 管理员重置密码后旧令牌也失效（401）",
              r.status_code == 401, f"got {r.status_code}")

        # ══════════════════════════════════════════════════════
        # B 资产同源
        # ══════════════════════════════════════════════════════
        print("\n== B 资产数与其来源页一致 ==")

        live = issue_tokens(User.objects.get(pk=teacher.pk))["access_token"]
        c = authed(live)

        r_prof = c.get("/api/v1/accounts/profile/")
        check("个人中心可访问（200）", r_prof.status_code == 200,
              f"{r_prof.status_code} {getattr(r_prof, 'data', '')}")
        prof = (r_prof.data or {}).get("data") or {}

        r_acct = c.get("/api/v1/billing/account/")
        check("积分页可访问（200）", r_acct.status_code == 200,
              f"{r_acct.status_code}")
        acct = (r_acct.data or {}).get("data") or {}

        assets = prof.get("assets") or {}
        credits = assets.get("credits") or {}

        check("个人中心资产可用", assets.get("available") is True,
              str(assets.get("reason")))
        check("★ 余额与积分页一致",
              credits.get("balance") == acct.get("balance") is not None,
              f"profile={credits.get('balance')} acct={acct.get('balance')}")
        check("★ 本月消耗与积分页一致",
              credits.get("period_used") == acct.get("period_used"),
              f"profile={credits.get('period_used')} "
              f"acct={acct.get('period_used')}")
        check("低余额阈值与积分页一致",
              credits.get("low_threshold") == acct.get("low_threshold"))
        check("单价与积分页一致",
              credits.get("price_yuan_per_point")
              == acct.get("price_yuan_per_point"))

        # Key 数量与实际一致
        from apps.billing import credentials as creds

        creds.upsert(
            org_id=org.id, user_id=teacher.id, scope="PERSONAL",
            provider="openai_compat", api_key="sk-" + "profselftest0001",
            base_url="",
        )
        r_prof2 = c.get("/api/v1/accounts/profile/")
        a2 = ((r_prof2.data or {}).get("data") or {}).get("assets") or {}
        check("Key 数量与实际一致（1）", a2.get("credential_count") == 1,
              str(a2.get("credential_count")))

        # ══════════════════════════════════════════════════════
        # C 资料白名单
        # ══════════════════════════════════════════════════════
        print("\n== C 只认白名单字段 ==")

        r = c.patch(
            "/api/v1/auth/me/",
            {"real_name": "张三改", "phone": "13900000002"},
            format="json",
        )
        check("改资料成功（200）", r.status_code == 200,
              f"{r.status_code} {getattr(r, 'data', '')}")
        teacher.refresh_from_db()
        check("姓名已改", teacher.real_name == "张三改", teacher.real_name)
        check("手机号已改", teacher.phone == "13900000002", teacher.phone)

        # C2 传 role 不生效
        r = c.patch(
            "/api/v1/auth/me/",
            {"real_name": "张三改", "role": "ORG_ADMIN", "username": "hacked"},
            format="json",
        )
        teacher.refresh_from_db()
        check("★ 传 role 不生效（仍是 TEACHER）",
              teacher.role == "TEACHER", teacher.role)
        check("★ 传 username 不生效（登录名未变）",
              teacher.username == "zfprof_teacher", teacher.username)

        # C3 手机号可清空
        r = c.patch("/api/v1/auth/me/", {"phone": ""}, format="json")
        teacher.refresh_from_db()
        check("手机号可清空", teacher.phone == "", repr(teacher.phone))

        # C4 超长姓名被拒
        r = c.patch(
            "/api/v1/auth/me/", {"real_name": "长" * 200}, format="json")
        check("超长姓名被拒（400）", r.status_code == 400,
              f"got {r.status_code}")
        teacher.refresh_from_db()
        check("被拒后姓名未变", teacher.real_name == "张三改", teacher.real_name)

        # ══════════════════════════════════════════════════════
        # D 结构完整性 / 系统管理员
        # ══════════════════════════════════════════════════════
        print("\n== D 结构完整性 ==")

        check("含 profile 段", isinstance(prof.get("profile"), dict))
        check("含 security 段", isinstance(prof.get("security"), dict))
        check("含 assets 段", isinstance(prof.get("assets"), dict))
        check("profile 不含 password 字段",
              "password" not in (prof.get("profile") or {}),
              str(list((prof.get("profile") or {}).keys())))
        sec = prof.get("security") or {}
        check("security 给出上次登录 IP 字段", "last_login_ip" in sec)

        # D2 系统管理员（无机构）
        sys_tok = issue_tokens(User.objects.get(pk=sysadmin.pk))["access_token"]
        r = authed(sys_tok).get("/api/v1/accounts/profile/")
        check("系统管理员可访问个人中心（200）", r.status_code == 200,
              f"{r.status_code} {getattr(r, 'data', '')}")
        s_assets = (((r.data or {}).get("data") or {}).get("assets") or {})
        check("系统管理员资产标记为不可用",
              s_assets.get("available") is False, str(s_assets))
        check("系统管理员资产给出原因", bool(s_assets.get("reason")),
              str(s_assets.get("reason")))

        # D3 未认证访问被拒
        r = APIClient().get("/api/v1/accounts/profile/")
        check("未登录访问个人中心被拒（401）", r.status_code == 401,
              f"got {r.status_code}")

    finally:
        _cleanup(org.pk, [teacher.pk, sysadmin.pk])

    return 0


def _cleanup(org_id: int, user_ids: list[int]) -> None:
    """清理自建数据。

    ★ 顺序：先计费三表 → 再 Key → 再日志 → 再用户 → 最后机构。
      `CreditAccount.user` 是 `PROTECT`，反过来会报错。
    ★ 机构用 `QuerySet.delete()`：`Org` 是软删模型，
      `org.delete()` 只打 `deleted_at`，测试机构会一行行堆在库里。
    """
    from apps.accounts.models import User
    from apps.ai.models import AICallLog
    from apps.org.models import Org

    try:
        from apps.billing.models import (
            ApiCredential, CreditAccount, CreditHold, CreditLedger,
        )
        CreditLedger._base_manager.filter(org_id=org_id).delete()
        CreditHold._base_manager.filter(org_id=org_id).delete()
        ApiCredential._base_manager.filter(org_id=org_id).delete()
        CreditAccount._base_manager.filter(org_id=org_id).delete()
    except Exception as exc:  # noqa: BLE001
        print(f"  （计费表清理跳过：{exc}）")

    AICallLog._base_manager.filter(org_id=org_id).delete()
    User._base_manager.filter(pk__in=user_ids).delete()
    Org._base_manager.filter(pk=org_id).delete()
    print("\n== 清理 ==")
    print(f"  测试机构 {org_id} 已删除")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["pg", "sqlite"], default="pg")
    args = ap.parse_args()

    tmp = _bootstrap(args.engine)

    import django

    django.setup()

    if args.engine == "sqlite":
        from django.core.management import call_command

        call_command("migrate", run_syncdb=True, verbosity=0,
                     interactive=False)

    print("=" * 46)
    print(f"个人中心回归（U26）  引擎={args.engine}")
    print("=" * 46)

    try:
        run()
    finally:
        try:
            from django.db import connection

            connection.close()
        except Exception:  # noqa: BLE001
            pass
        if tmp and os.path.exists(tmp):
            import time

            time.sleep(0.3)
            try:
                os.remove(tmp)
            except OSError:
                pass

    print()
    print("=" * 46)
    print(f"PASS {PASS} / FAIL {FAIL}")
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print(f"  - {f}")
    print("=" * 46)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
