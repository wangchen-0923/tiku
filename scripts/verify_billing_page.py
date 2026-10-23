"""积分与凭证页回归（U27）。

★★ 验的是**页面的接口契约**，而不是「服务层函数能跑」——
   U22/U24 的 `verify_credit_ledger.py` 已经验过服务层，
   但页面从不直接调服务层：它拿到的是 HTTP 响应。两者之间那一层
   （序列化、权限类、分页器、路由顺序）才是页面真正依赖的东西。

本脚本验五组：

  A **前端接线**：路由/导航/api 模块里的路径与后端 URLconf 一一对应，
    且没有任何一处会把 Key 明文写回界面。
  B **余额与流水能逐笔对上**（本单元的出口判据）：
    分页拉全 → `balance_after - delta == 下一条的 balance_after` →
    最新一条的 `balance_after == 余额接口的 balance`。
    ★ 这条链只要断一处，就说明有流水没被写、或分页漏行。
  C **Key 全生命周期**：新建 → 掩码 → 连通测试 → 停用 → 启用 → 删除；
    明文与密文都不得出现在任何响应里；别人的 Key 碰不到。
  D **权限口径**：学生 403、教师不能改规则、管理员全通。
  E 清理（★ 全部自建数据，绝不复用库里既有实体）。

★ 两种跑法：

    python scripts/verify_billing_page.py                  # 真 PG（默认 15433）
    python scripts/verify_billing_page.py --engine sqlite   # 无库环境的逻辑自检
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from decimal import Decimal

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

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
        print(f"  FAIL  {label}" + (f"   ← {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n== {title} ==")


def payload(r) -> dict:
    """取响应里的 `data` 段。

    ★★ 这里必须**按渲染后的 JSON 取**，不能用 `r.data`：
      信封 `{code, message, data}` 是 `ZfJSONRenderer` 在渲染阶段套上的，
      DRF 测试客户端的 `r.data` 是**渲染前**的结构。于是两种接口表现不同：

        普通接口  视图自己返回 `ok(payload)` → `r.data` 已是信封
        分页接口  `get_paginated_response()` 直接返回 `{items,total,...}`
                  → `r.data` 没有 `data` 键，取出来是 **None**

      第一次跑就栽在这里：`body["items"]` 报 `'NoneType' is not subscriptable`。
      统一走 `r.json()` 后两者一致，也就顺带证明了「页面看到的确实是信封」。
    """
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        return {}
    if isinstance(body, dict) and "data" in body:
        inner = body["data"]
        return inner if isinstance(inner, dict) else {"_value": inner}
    return body if isinstance(body, dict) else {}


def _bootstrap(engine: str) -> str | None:
    sys.path.insert(0, r"D:\tiku\server")
    sys.path.insert(0, r"D:\tiku\.devdeps")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
    os.environ["ZF_CACHE"] = "locmem"
    tmp_path = None

    if engine == "sqlite":
        os.environ["ZF_DB_ENGINE"] = "sqlite"
        tmp_path = os.path.join(
            tempfile.gettempdir(), f"zf_billing_page_{os.getpid()}.sqlite3"
        )
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    else:
        os.environ.pop("ZF_DB_ENGINE", None)
        os.environ["ZF_DB_HOST"] = "127.0.0.1"
        os.environ["ZF_DB_PORT"] = os.environ.get("ZF_DB_PORT", "15433")
        os.environ["ZF_DB_NAME"] = os.environ.get("ZF_DB_NAME", "zhifou")
        os.environ["ZF_DB_USER"] = os.environ.get("ZF_DB_USER", "zhifou")
        os.environ["ZF_DB_PASSWORD"] = os.environ.get("ZF_DB_PASSWORD", "zhifou")

    import django

    django.setup()

    if tmp_path:
        from django.db import connections

        connections.close_all()
        connections.databases["default"]["NAME"] = tmp_path
        assert connections["default"].settings_dict["NAME"] == tmp_path

    return tmp_path


# ══════════════════════════════════════════════════════════════
#  A 前端接线（纯静态，不需要库）
# ══════════════════════════════════════════════════════════════

FRONT_DIR = r"D:\tiku\web\src"


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def check_wiring() -> None:
    section("A 前端接线（静态）")

    api = _read(os.path.join(FRONT_DIR, "api", "billing.ts"))
    view = _read(os.path.join(FRONT_DIR, "views", "BillingView.vue"))
    router = _read(os.path.join(FRONT_DIR, "router", "index.ts"))
    app = _read(os.path.join(FRONT_DIR, "App.vue"))

    # A1 页面与路由已注册
    check("路由注册 name: 'Billing'", "name: 'Billing'" in router)
    check("路由指向 BillingView.vue", "views/BillingView.vue" in router)
    check("路由路径 /billing", "path: '/billing'" in router)
    check("导航含「积分与凭证」入口", "name: 'Billing'" in app)

    # A2 前端调用的每个路径都真实存在于后端 URLconf
    #
    # ★ 必须**递归**展开：`/api/v1/` 与 `billing/` 是两层 include，
    #   只看第一层会拿到一堆 include 节点而不是叶子路径 ——
    #   那样「缺失 11 个路径」是脚本的假警报，不是页面写错了。
    from django.urls import URLPattern, URLResolver, get_resolver

    backend_paths: set[str] = set()

    def walk(patterns, prefix: str = "/api/v1/") -> None:
        for p in patterns:
            if isinstance(p, URLResolver):
                walk(p.url_patterns, prefix + str(p.pattern))
            elif isinstance(p, URLPattern):
                backend_paths.add(prefix + str(p.pattern))

    # 只展开 `api/v1/` 这一支，避免把 admin/ 等无关路由也收进来
    root = get_resolver()
    for p in root.url_patterns:
        if isinstance(p, URLResolver) and str(p.pattern).rstrip("/") == "api/v1":
            walk(p.url_patterns)
        elif isinstance(p, URLPattern) and str(p.pattern).startswith("api/v1"):
            backend_paths.add("/" + str(p.pattern))

    def _norm(p: str) -> str:
        return re.sub(r"\$\{[^}]+\}", "<int:pk>", p)

    def _norm_backend(p: str) -> str:
        return re.sub(r"<[^>]+>", "<int:pk>", p)

    # ★ 只在**代码行**里找路径：注释里写着「接口一览（都挂在
    #   `/api/v1/billing/` 下）」，那条不是调用，扫进来会变成永久红。
    #   静态检查的误伤比漏检更糟 —— 一旦有人习惯性忽略它，它就废了。
    api_code = "\n".join(
        line for line in api.splitlines()
        if not line.strip().startswith(("*", "//", "/*"))
    )
    called = set(re.findall(r"'(/api/v1/billing/[^'`]*)'", api_code))
    called |= set(re.findall(r"`(/api/v1/billing/[^`]*)`", api_code))

    norm_backend = {_norm_backend(b) for b in backend_paths}
    missing = [c for c in sorted(called) if _norm(c) not in norm_backend]
    check(
        f"前端调用的 {len(called)} 个路径都存在于后端",
        not missing,
        "缺失: " + ", ".join(missing) if missing else "",
    )

    # A3 页面上没有任何「回显明文 Key」的写法
    check(
        "页面不渲染 key 明文（只渲染 key_masked）",
        "key_masked" in view and "row.api_key" not in view,
    )
    check(
        "提交后清空明文输入框",
        "keyForm.api_key = ''" in view,
    )
    check(
        "页面不显示 key_cipher / key_fingerprint",
        "key_cipher" not in view and "key_fingerprint" not in view,
    )

    # A4 余额不得由前端加总（U27 的第一条纪律）
    #
    # ★ 只禁**写代码**的那种加总：`.reduce(` / `rows.value.reduce`。
    #   不能简单搜 "sum(" —— 注释里写着「绝不自己 sum 流水」，
    #   那会把自己写的纪律说明判成违规（第一次就踩了）。
    check(
        "★ 页面不用 reduce 从流水算余额",
        ".reduce(" not in view and "rows.value.reduce" not in view,
    )
    check(
        "★ 余额来自 getAccount()",
        "B.getAccount()" in view,
    )

    # A5 学校配置块受能力控制，不是无条件渲染
    check(
        "学校配置块受 org.settings 能力控制",
        "auth.can('org.settings')" in view,
    )
    check(
        "el-tab-pane 上带 v-if（不是只隐藏内容）",
        bool(re.search(r"<el-tab-pane\s+v-if=\"canManage\"", view)),
    )

    # A6 分页参数名与后端一致（StandardPagination: page / page_size）
    check("分页传 page_size（后端参数名）", "page_size: query.page_size" in api)


# ══════════════════════════════════════════════════════════════
#  B–D 接口契约（需要库）
# ══════════════════════════════════════════════════════════════


def run() -> None:
    from rest_framework.test import APIClient

    from apps.accounts.models import User
    from apps.billing import services as billing
    from apps.billing.models import ApiCredential, CreditLedger
    from apps.org.models import Org, OrgType

    org = Org.objects.create(
        name="ZFTEST 积分页自检", short_name="ZFTESTB", type=OrgType.SCHOOL,
    )
    teacher = User.objects.create_user(
        username="zfbill_teacher", password="bill-pwd-123456",
        org=org, role="TEACHER", real_name="李四",
    )
    teacher2 = User.objects.create_user(
        username="zfbill_other", password="bill-pwd-123456",
        org=org, role="TEACHER", real_name="王五",
    )
    admin = User.objects.create_user(
        username="zfbill_admin", password="bill-pwd-123456",
        org=org, role="ORG_ADMIN", real_name="校管理员",
    )
    student = User.objects.create(
        username="zfbill_student", org=org, role="STUDENT",
        real_name="学生甲", is_active=True,
    )
    student.set_password("bill-pwd-123456")
    student.save()

    def client_of(user) -> APIClient:
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    t = client_of(teacher)
    a = client_of(admin)
    s = client_of(student)

    try:
        # ── 造流水：发放 / 预扣 / 结算 / 回滚 / 调账 五种方向各来一笔 ──
        billing.adjust(
            org_id=org.pk, user_id=teacher.pk, delta=Decimal("1000"),
            remark="自检初始额度", operator_id=admin.pk,
            idempotency_key=f"zfbill:seed:{teacher.pk}",
        )
        h1 = billing.hold(
            org_id=org.pk, user_id=teacher.pk, idempotency_key=f"zfbill:h1:{teacher.pk}",
            scene="gen_explanation", est_cost_yuan=Decimal("0.02"),
        )
        billing.settle(
            org_id=org.pk, hold_id=h1.id, actual_cost_yuan=Decimal("0.013"),
        )
        h2 = billing.hold(
            org_id=org.pk, user_id=teacher.pk, idempotency_key=f"zfbill:h2:{teacher.pk}",
            scene="tag_question", est_cost_yuan=Decimal("0.05"),
        )
        billing.refund(org_id=org.pk, hold_id=h2.id, reason="自检回滚")

        # ══════════════════════════════════════════════════════
        #  B 余额与流水逐笔对上（出口判据）
        # ══════════════════════════════════════════════════════
        section("B 余额与流水能逐笔对上")

        r = t.get("/api/v1/billing/account/")
        check("余额接口 200", r.status_code == 200, str(r.status_code))
        snap = payload(r)
        for field in (
            "balance", "frozen", "low_threshold", "is_low", "price_yuan_per_point",
            "period", "period_used", "period_granted", "has_grant",
        ):
            check(f"余额快照含 {field}", field in snap, str(snap)[:120])

        # 分页拉全（页大小刻意给小，验分页不漏行）
        rows: list[dict] = []
        page = 1
        while True:
            r = t.get("/api/v1/billing/ledger/", {"page": page, "page_size": 2})
            check(f"流水第 {page} 页 200", r.status_code == 200, str(r.status_code))
            body = payload(r)
            items = body["items"]
            rows.extend(items)
            if not body.get("next") or not items:
                break
            page += 1
            if page > 50:      # 兜底，避免死循环
                break

        check("分页拉到的行数 == total", len(rows) == body["total"],
              f"{len(rows)} vs {body['total']}")
        check("至少 5 条流水（发放/预扣/结算/预扣/回滚）", len(rows) >= 5,
              str(len(rows)))

        # ★ 链式核对：相邻两条之间，balance_after - delta == 下一条的 balance_after
        chain_broken = []
        for i in range(len(rows) - 1):
            expect = Decimal(rows[i]["balance_after"]) - Decimal(rows[i]["delta"])
            actual = Decimal(rows[i + 1]["balance_after"])
            if expect != actual:
                chain_broken.append((rows[i]["id"], str(expect), str(actual)))
        check(
            "★ 逐笔链核对：balance_after - delta == 下一条 balance_after",
            not chain_broken,
            str(chain_broken[:3]),
        )

        # 最早一条的「变动前余额」应为 0（账户从 0 起）
        earliest = rows[-1]
        check(
            "最早一条的变动前余额为 0",
            Decimal(earliest["balance_after"]) - Decimal(earliest["delta"]) == 0,
            f"{earliest['balance_after']} - {earliest['delta']}",
        )

        # 最新一条的 balance_after 必须等于余额接口
        check(
            "★ 最新流水的 balance_after == 余额接口的 balance",
            Decimal(rows[0]["balance_after"]) == Decimal(snap["balance"]),
            f"{rows[0]['balance_after']} vs {snap['balance']}",
        )

        # 全量 delta 之和 == 余额（分页没漏行才可能成立）
        total_delta = sum((Decimal(x["delta"]) for x in rows), Decimal("0"))
        check(
            "★ 分页拉全后 sum(delta) == balance",
            total_delta == Decimal(snap["balance"]),
            f"{total_delta} vs {snap['balance']}",
        )

        # 方向筛选
        r = t.get("/api/v1/billing/ledger/", {"direction": "SETTLE"})
        body = payload(r)
        check(
            "按方向筛选只回该方向",
            all(x["direction"] == "SETTLE" for x in body["items"]) and body["items"],
            str([x["direction"] for x in body["items"]]),
        )

        # 账期筛选
        r = t.get("/api/v1/billing/ledger/", {"period": snap["period"]})
        body = payload(r)
        check(
            "按账期筛选只回该账期",
            all(x["period"] == snap["period"] for x in body["items"]),
        )

        # 只能看自己的流水
        r = t.get("/api/v1/billing/ledger/", {"user_id": teacher2.pk})
        body = payload(r)
        own_ids = {x["username"] for x in body["items"]}
        check(
            "★ 传 user_id 也只看自己的（不读查询参数）",
            own_ids <= {teacher.username},
            str(own_ids),
        )

        # ══════════════════════════════════════════════════════
        #  C Key 全生命周期
        # ══════════════════════════════════════════════════════
        section("C 自定义 Key 全生命周期")

        SECRET = "sk-zfbill-SECRET-should-never-come-back-1234"

        r = t.post(
            "/api/v1/billing/keys/",
            {"scope": "PERSONAL", "provider": "openai_compat",
             "api_key": SECRET, "base_url": ""},
            format="json",
        )
        check("新建 Key 201", r.status_code == 201, f"{r.status_code} {r.data}")
        created = payload(r)
        key_id = created["id"]
        check("新建只回末四位", created.get("key_last4") == "1234",
              str(created.get("key_last4")))
        check("新建回的是掩码", created.get("key_masked") == "****1234",
              str(created.get("key_masked")))
        raw = r.content.decode("utf-8", "replace")
        check("★ 响应体不含 Key 明文", SECRET not in raw and "SECRET-should" not in raw)
        check("响应体不含密文字段", "key_cipher" not in raw and "key_fingerprint" not in raw)

        r = t.get("/api/v1/billing/keys/")
        body = payload(r)
        check("Key 列表 200", r.status_code == 200)
        check("列表含我刚建的那把", any(x["id"] == key_id for x in body["items"]))
        check("列表回 kek_source", body.get("kek_source") in ("env", "derived"),
              str(body.get("kek_source")))
        raw = r.content.decode("utf-8", "replace")
        check("★ 列表响应体不含明文", SECRET not in raw)

        # 密文确实是密文（库里存的不是明文）
        cred = ApiCredential.objects.for_org(org.pk).get(pk=key_id)
        stored = bytes(cred.key_cipher)
        check("★ 库里存的是密文（不含明文子串）",
              SECRET.encode() not in stored and b"sk-zfbill" not in stored)

        # 连通测试（桩掉 provider，不打外网）
        from apps.ai import providers as providers_mod

        class _StubProvider:
            default_model = "stub-model"

            def __init__(self):
                self.seen_key = None

            def with_credentials(self, *, api_key: str, base_url: str = ""):
                clone = _StubProvider()
                clone.seen_key = api_key
                return clone

            def invoke(self, **kwargs):
                return {"ok": True}

        orig_get = providers_mod.get_provider
        orig_err = providers_mod.ProviderError
        providers_mod.get_provider = lambda name: _StubProvider()
        try:
            r = t.post(f"/api/v1/billing/keys/{key_id}/test/")
            body = payload(r)
            check("连通测试 200", r.status_code == 200, str(r.status_code))
            check("测试结果 ok=true", body.get("ok") is True, str(body))
            check("测试回 tested_at", bool(body.get("tested_at")), str(body))

            r = t.get("/api/v1/billing/keys/")
            body = payload(r)
            row = next(x for x in body["items"] if x["id"] == key_id)
            check("测试结果已写回列表（last_test_ok）", row["last_test_ok"] is True,
                  str(row))
            check("列表带上次测试消息", bool(row["last_test_message"]),
                  str(row.get("last_test_message")))
        finally:
            providers_mod.get_provider = orig_get
            providers_mod.ProviderError = orig_err

        # 停用 / 启用
        r = t.patch(f"/api/v1/billing/keys/{key_id}/", {"status": "DISABLED"},
                    format="json")
        check("停用 200", r.status_code == 200, str(r.status_code))
        body = payload(r)
        check("停用后 status=DISABLED", body.get("status") == "DISABLED", str(body))

        r = t.patch(f"/api/v1/billing/keys/{key_id}/", {"status": "ACTIVE"},
                    format="json")
        body = payload(r)
        check("启用后 status=ACTIVE", body.get("status") == "ACTIVE", str(body))

        # 换成新 Key 是覆盖而不是报「已存在」
        r = t.post(
            "/api/v1/billing/keys/",
            {"scope": "PERSONAL", "provider": "openai_compat",
             "api_key": "sk-zfbill-second-key-9999"},
            format="json",
        )
        check("同作用域+同厂商是覆盖（不报重复）", r.status_code in (200, 201),
              str(r.status_code))
        body = payload(r)
        check("覆盖后末四位变化", body.get("key_last4") == "9999",
              str(body.get("key_last4")))
        check("覆盖后总数仍是 1",
              ApiCredential.objects.for_org(org.pk).count() == 1,
              str(ApiCredential.objects.for_org(org.pk).count()))

        # 别人的 Key 碰不到
        other = client_of(teacher2)
        r = other.patch(f"/api/v1/billing/keys/{key_id}/", {"status": "DISABLED"},
                        format="json")
        check("★ 别人的 Key 改不动（403）", r.status_code == 403, str(r.status_code))
        r = other.delete(f"/api/v1/billing/keys/{key_id}/")
        check("★ 别人的 Key 删不掉（403/404）", r.status_code in (403, 404),
              str(r.status_code))
        r = other.post(f"/api/v1/billing/keys/{key_id}/test/")
        check("★ 别人的 Key 测不了（403/404）", r.status_code in (403, 404),
              str(r.status_code))

        # 学生完全不可用
        r = s.get("/api/v1/billing/keys/")
        check("★ 学生看 Key 列表被拒（403）", r.status_code == 403, str(r.status_code))
        r = s.post(
            "/api/v1/billing/keys/",
            {"scope": "PERSONAL", "api_key": "sk-student-should-fail-0000"},
            format="json",
        )
        check("★ 学生建 Key 被拒（403）", r.status_code == 403, str(r.status_code))

        # 删除
        r = t.delete(f"/api/v1/billing/keys/{key_id}/")
        check("删除 200", r.status_code == 200, str(r.status_code))
        check("删除后库里没了",
              not ApiCredential.objects.for_org(org.pk).filter(pk=key_id).exists())

        # ══════════════════════════════════════════════════════
        #  D 权限口径
        # ══════════════════════════════════════════════════════
        section("D 权限口径")

        r = t.get("/api/v1/billing/settings/")
        check("教师可读积分规则（200）", r.status_code == 200, str(r.status_code))
        body = payload(r)
        check("规则含 4 项", len(body.get("settings", {})) >= 4,
              str(list(body.get("settings", {}).keys())))
        check("每项带 source（org/platform）",
              all("source" in v for v in body["settings"].values()))

        r = t.put("/api/v1/billing/settings/",
                  {"credit_low_balance_threshold": "50"}, format="json")
        check("★ 教师改规则被拒（403）", r.status_code == 403, str(r.status_code))

        r = a.put("/api/v1/billing/settings/",
                  {"credit_low_balance_threshold": "66"}, format="json")
        check("管理员改规则 200", r.status_code == 200, str(r.status_code))
        body = payload(r)
        # ★ 比 Decimal 而不是比字符串：规则值走 Decimal 序列化，
        #   写 66 读回 `66.0000`。字符串比较会把「同一个数」判成不等。
        check("改后值生效",
              Decimal(body["settings"]["credit_low_balance_threshold"]["value"])
              == Decimal("66"),
              str(body["settings"]["credit_low_balance_threshold"]))
        check("改后 source=org",
              body["settings"]["credit_low_balance_threshold"]["source"] == "org",
              str(body["settings"]["credit_low_balance_threshold"]))

        # 阈值变了，余额快照要跟着变（页面靠它显示「额度将尽」）
        r = t.get("/api/v1/billing/account/")
        snap2 = payload(r)
        check("余额快照的阈值随规则变化",
              Decimal(snap2["low_threshold"]) == Decimal("66"),
              str(snap2["low_threshold"]))

        r = t.get("/api/v1/billing/reconcile/")
        check("★ 教师看对账被拒（403）", r.status_code == 403, str(r.status_code))
        r = t.get("/api/v1/billing/org/accounts/")
        check("★ 教师看全校账户被拒（403）", r.status_code == 403, str(r.status_code))
        r = t.post("/api/v1/billing/grants/run/", {}, format="json")
        check("★ 教师手动发放被拒（403）", r.status_code == 403, str(r.status_code))

        r = a.get("/api/v1/billing/reconcile/")
        check("管理员对账 200", r.status_code == 200, str(r.status_code))
        body = payload(r)
        check("对账 ok=true（自建数据全程账本自洽）", body.get("ok") is True,
              str({k: body.get(k) for k in
                   ("balance_mismatch", "shortfalls", "stale_holds")})[:200])
        check("对账四类字段齐备",
              all(k in body for k in ("balance_mismatch", "ai_check",
                                      "stale_holds", "shortfalls")))

        r = a.get("/api/v1/billing/org/accounts/")
        body = payload(r)
        check("管理员看全校账户 200", r.status_code == 200)
        check("账户列表含本机构教师",
              any(x["user_id"] == teacher.pk for x in body["items"]),
              str([x.get("user_id") for x in body["items"]]))

        # 发放幂等：同一账期发两次，第二次全跳过
        r = a.post("/api/v1/billing/grants/run/", {}, format="json")
        check("手动发放 200", r.status_code == 200, str(r.status_code))
        r = a.post("/api/v1/billing/grants/run/", {}, format="json")
        body = payload(r)
        check("★ 重复发放幂等（第二次 granted_users=0）",
              body.get("granted_users") == 0, str(body))

        # 调账
        r = a.post("/api/v1/billing/adjust/",
                   {"user_id": teacher.pk, "delta": "12.5", "remark": "自检调账"},
                   format="json")
        check("调账 200", r.status_code == 200, str(r.status_code))
        r = a.post("/api/v1/billing/adjust/",
                   {"user_id": teacher.pk, "delta": "0", "remark": "x"},
                   format="json")
        check("★ 调账数额为 0 被拒（400）", r.status_code == 400, str(r.status_code))

        # 系统管理员（无机构）在本模块一律 403 —— 与全站口径一致
        from apps.accounts.models import User as U

        root = U.objects.create(
            username="zfbill_root", role="PLATFORM_ADMIN", org=None,
            is_active=True, real_name="系统管理员",
        )
        root.set_password("bill-pwd-123456")
        root.save()
        r = client_of(root).get("/api/v1/billing/account/")
        check("★ 系统管理员访问余额被拒（403）", r.status_code == 403,
              str(r.status_code))

    finally:
        _cleanup(org.pk, [teacher.pk, teacher2.pk, admin.pk, student.pk])


def _cleanup(org_id: int, user_ids: list[int]) -> None:
    """清理自建数据。

    ★ 顺序：先计费三表 + Key → 再用户 → 最后机构。
      `CreditAccount.user` 是 `PROTECT`，反过来会报错。
    ★ 机构用 `QuerySet.delete()`：`Org` 是软删模型，`org.delete()` 只打
      `deleted_at`，测试机构会一行行堆在库里。
    ★ 平台管理员不在 `user_ids` 里 —— 单独按用户名清。
    """
    from apps.accounts.models import User
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

    User._base_manager.filter(pk__in=user_ids).delete()
    User._base_manager.filter(username="zfbill_root").delete()
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

        call_command("migrate", run_syncdb=True, verbosity=0, interactive=False)

    print("=" * 46)
    print(f"积分与凭证页回归（U27）  引擎={args.engine}")
    print("=" * 46)

    try:
        check_wiring()
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
        for x in FAILED:
            print(f"  - {x}")
    print("=" * 46)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
