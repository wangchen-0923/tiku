"""提示词模板中心回归（U21）。

★★ 验的是「模板真的被用上了」，而不是「表里有 11 行数据」：

  ① 11 个场景全覆盖，每场景恰好 1 条 `ACTIVE`、`version=1`
  ② 用真实 payload 渲染，**变量缺失必须为 0**（模板笔误的照妖镜）
  ③ 机构模板优先于平台默认；机构只有 DRAFT 时回退平台
  ④ 编排器实际取到模板，`ai_call_log.prompt_version` 落库
  ⑤ 取不到模板时回退，且 `prompt_version` 记为 NULL（不是 0、不是 1）
  ⑥ seed 幂等：重跑不双发；改了内容不加 `--bump` 必须报冲突

★ 两种跑法::

    python scripts/verify_prompt_templates.py                 # 真 PG（默认 15433）
    python scripts/verify_prompt_templates.py --engine sqlite # 无库环境的逻辑自检

  ★ 全程包在一个事务里，**结束时整体回滚**——
    PG 模式下不会把测试用的机构/模板/调用日志留在开发库里。
"""

from __future__ import annotations

import argparse
import io
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
            tempfile.gettempdir(), f"zf_prompt_selftest_{os.getpid()}.sqlite3"
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


#: 每个场景的**典型 payload 键**。
#:
#: ★ 这些键是业务代码调用时必须提供的契约。模板里引用了这里没有的键，
#:   线上就会渲染出 `{{xxx}}` 字面量——模型会把它当普通文本，
#:   症状是"输出质量莫名其妙变差"，从结果根本反推不出来。
#:   所以这张表同时也是**接口契约文档**，改模板时要对着它改。
SAMPLE_PAYLOAD: dict[str, dict] = {
    "ocr_question": {"subject": "机械基础", "payload": {"text": "1. 图示阶梯轴..."}},
    "extract_figures": {"subject": "机械制图", "payload": {"page": 1}},
    "verify_figure_binding": {
        "questions": [{"no": "1", "stem": "如图所示的阶梯轴"}],
        "figures": [{"figure_id": "p1-f1", "caption": "图1"}],
    },
    "tag_question": {
        "subject": "机械基础",
        "kb_entries": [{"node_id": 12, "node_name": "屈服强度"}],
        "payload": {"stem": "..."},
    },
    "polish_stem": {
        "subject": "机械基础",
        "payload": {"stem": "咱把这个件..."},
        "kb_entries": [{"id": 3, "content": "屈服强度：..."}],
    },
    "calibrate_difficulty": {
        "subject": "机械基础",
        "payload": {"stem": "..."},
        "kb_entries": [{"id": 4, "content": "易混：屈服强度 vs 屈服极限"}],
    },
    "design_distractors": {
        "subject": "机械基础",
        "stem": "屈服强度的符号是？",
        "answer": "ReL",
        "kb_entries": [{"id": 5, "content": "学生常把 Rm 当成 ReL"}],
    },
    "gen_explanation": {
        "subject": "机械基础",
        "payload": {"stem": "..."},
        "answer": "ReL",
        "kb_entries": [{"id": 1, "content": "..."}],
    },
    "generate_question": {
        "subject": "机械基础",
        "difficulty_band": "MEDIUM",
        "question_type": "SINGLE",
        "count": 1,
        "kb_entries": [{"id": 1, "content": "..."}],
        "existing_questions": [],
    },
    "kb_cite": {
        "answer": "屈服强度符号为 ReL。",
        "kb_entries": [{"id": 1, "content": "..."}],
    },
    "parse_kb_document": {"subject": "机械基础", "payload": {"text": "第一章..."}},
}


def run() -> int:
    from django.core.management import call_command
    from django.db import transaction

    from apps.ai import prompts as P
    from apps.ai.prompt_baseline import BASELINES
    from apps.ai.prompts import PROMPT_SCENES, scene_key
    from apps.ai.routing import ALL_TASKS, CITATION_EXEMPT_TASKS

    with transaction.atomic():
        try:
            code = _run_checks(
                P, BASELINES, PROMPT_SCENES, scene_key, ALL_TASKS,
                CITATION_EXEMPT_TASKS, call_command,
            )
        finally:
            # ★ 整体回滚：测试造的机构、模板、调用日志一律不留
            transaction.set_rollback(True)
        return code


def _run_checks(
    P, BASELINES, PROMPT_SCENES, scene_key, ALL_TASKS,
    CITATION_EXEMPT_TASKS, call_command,
) -> int:
    from apps.ai.models import AIPromptTemplate, PromptTemplateStatus

    # ── 先确保有基线（事务内，随后一并回滚）──
    buf = io.StringIO()
    call_command("seed_prompt_templates", stdout=buf, verbosity=0)
    P.invalidate()

    # ══════════════════════════════════════════════════════
    print("== S1 场景覆盖与基线完整性 ==")

    expected = {scene_key(s) for s in PROMPT_SCENES}
    actives = AIPromptTemplate.objects.filter(
        org__isnull=True, status=PromptTemplateStatus.ACTIVE
    )
    have = set(actives.values_list("key", flat=True))

    check("11 个场景全覆盖", expected == have,
          f"缺 {sorted(expected - have)} 多 {sorted(have - expected)}")
    check("场景数恰为 11", len(PROMPT_SCENES) == 11, str(len(PROMPT_SCENES)))
    check(
        "场景清单 == ALL_TASKS - embed_text",
        set(PROMPT_SCENES) == (set(ALL_TASKS) - {"embed_text"}),
        f"差集 {sorted(set(PROMPT_SCENES) ^ (set(ALL_TASKS) - {'embed_text'}))}",
    )

    per_key = {}
    for row in actives:
        per_key.setdefault(row.key, []).append(row)
    check("每场景恰好 1 条 ACTIVE",
          all(len(v) == 1 for v in per_key.values()) and len(per_key) == 11,
          str({k: len(v) for k, v in per_key.items() if len(v) != 1}))
    check("全部 version=1", all(r.version == 1 for r in actives),
          str([(r.key, r.version) for r in actives if r.version != 1]))
    check("key 与 task 自洽",
          all(r.key == scene_key(r.task) for r in actives),
          str([(r.key, r.task) for r in actives if r.key != scene_key(r.task)]))

    # ══════════════════════════════════════════════════════
    print("\n== S2 模板内容质量 ==")

    rows = {r.task: r for r in actives}
    check("每条 system_prompt 足够具体（>100 字）",
          all(len(r.system_prompt) > 100 for r in actives),
          str([(r.task, len(r.system_prompt)) for r in actives
               if len(r.system_prompt) <= 100]))
    check("每条 user_prompt 都含变量占位符",
          all("{{" in r.user_prompt for r in actives),
          str([r.task for r in actives if "{{" not in r.user_prompt]))
    check("每条 output_schema 都有 properties",
          all(isinstance(r.output_schema, dict) and r.output_schema.get("properties")
              for r in actives),
          str([r.task for r in actives
               if not (isinstance(r.output_schema, dict)
                       and r.output_schema.get("properties"))]))
    check("每条 glossary 非空",
          all(r.glossary for r in actives),
          str([r.task for r in actives if not r.glossary]))
    check("glossary 的每条都是带说明的（>=10 字，不是单个同义词）",
          all(len(str(v)) >= 10 for r in actives for v in r.glossary.values()),
          str([(r.task, k) for r in actives
               for k, v in r.glossary.items() if len(str(v)) < 10]))

    # ★ 引用 kb_entries 的模板，kb_injection.top_k 必须 > 0：
    #   否则「模板里要求引用」但「检索阶段没注入任何条目」，
    #   模型只能凭空编引用——这是最危险的一种不一致。
    bad_kb = []
    for r in actives:
        if "{{kb_entries}}" in r.user_prompt:
            top_k = (r.kb_injection or {}).get("top_k", 0)
            if not top_k:
                bad_kb.append(r.task)
    check("引用 kb_entries 的模板都配了 top_k>0", not bad_kb, str(bad_kb))

    # ★ 反过来：不引用的不许乱配（否则白花检索开销与 token）
    over_kb = [
        r.task for r in actives
        if "{{kb_entries}}" not in r.user_prompt
        and (r.kb_injection or {}).get("top_k", 0)
    ]
    check("不引用知识库的模板 top_k 为 0", not over_kb, str(over_kb))

    check("system_prompt 不含内部字段名（payload.data / .images）",
          all("payload.data" not in r.system_prompt
              and "payload.images" not in r.system_prompt for r in actives),
          str([r.task for r in actives
               if "payload.data" in r.system_prompt
               or "payload.images" in r.system_prompt]))

    # ══════════════════════════════════════════════════════
    print("\n== R 渲染：变量缺失必须为 0 ==")

    all_missing: dict[str, tuple] = {}
    for task in PROMPT_SCENES:
        row = rows[task]
        ctx = P.build_context(
            task=task,
            payload_data=SAMPLE_PAYLOAD[task],
            output_schema=row.output_schema,
            glossary=row.glossary,
            few_shot=row.few_shot,
            extra={"org_name": "测试学校", "grade_band": row.grade_band,
                   "subject": row.subject},
        )
        rendered = P.render_template(row, ctx)
        if rendered.missing:
            all_missing[task] = rendered.missing
    check("11 个场景用典型 payload 渲染，缺变量为 0", not all_missing,
          str(all_missing))

    # 变量真的被替换了（不是原样留着）
    row = rows["polish_stem"]
    ctx = P.build_context(
        task="polish_stem", payload_data={"stem": "待润色文本"},
        output_schema=row.output_schema, glossary=row.glossary,
    )
    out, miss = P.render(row.user_prompt, ctx)
    check("{{payload}} 被替换成 JSON", "待润色文本" in out and "{{payload}}" not in out,
          out[:80])
    check("替换后无残留占位符", "{{" not in out or miss, f"miss={miss}")

    # 缺变量：不抛错 + 原样保留 + 记录
    out2, miss2 = P.render(
        "甲 {{has_this}} 乙 {{missing_this}}", {"has_this": "X"}
    )
    check("缺变量时保留占位符原样", "{{missing_this}}" in out2, out2)
    check("缺变量被记录", miss2 == ("missing_this",), str(miss2))
    check("已提供的变量正常替换", "X" in out2, out2)

    # ★ 中文变量名：正则不认，但**必须**被抓进 missing。
    #   否则运营在后台写 `{{题干}}` 会静默失效，只表现为"输出变差"。
    out_cn, miss_cn = P.render("题干：{{题干}}", {})
    check("中文占位符未被替换（语法不支持）", out_cn == "题干：{{题干}}", out_cn)
    check("中文占位符被抓进 missing（不静默）", miss_cn == ("题干",), str(miss_cn))

    # strict 模式必须抛
    try:
        P.render("{{no_such_var}}", {}, strict=True)
        check("strict 模式缺变量抛错", False, "未抛")
    except P.TemplateRenderError:
        check("strict 模式缺变量抛错", True)

    # 嵌套取值
    out3, miss3 = P.render("{{a.b.c}}", {"a": {"b": {"c": "深"}}})
    check("支持点号嵌套取值", out3 == "深" and not miss3, f"{out3} {miss3}")

    # 容器走 JSON 而非 Python repr
    out4, _ = P.render("{{d}}", {"d": {"k": "v", "n": None}})
    check("容器序列化为 JSON（双引号，非 repr）",
          '"k": "v"' in out4.replace(",", ", ") or '"k":"v"' in out4, out4)
    check("None 渲染为空串", P.render("[{{x}}]", {"x": None})[0] == "[]")

    # ══════════════════════════════════════════════════════
    print("\n== L 加载与优先级 ==")

    check("平台模板可加载",
          P.load_template(scene_key("polish_stem")) is not None)
    check("不存在的 key 返回 None",
          P.load_template("jxzc.secondary_vocational.不存在") is None)

    try:
        P.require_template("jxzc.secondary_vocational.不存在")
        check("require_template 抛 TemplateNotFound", False, "未抛")
    except P.TemplateNotFound:
        check("require_template 抛 TemplateNotFound", True)

    # 造一个机构，验证「机构优先」与「DRAFT 回退」
    from apps.org.models import Org, OrgType

    org = Org.objects.create(
        name="ZFTEST 提示词自检机构", short_name="ZFTESTP", type=OrgType.SCHOOL,
    )

    org_row = AIPromptTemplate.objects.create(
        org=org, key=scene_key("polish_stem"), task="polish_stem",
        system_prompt="机构版系统提示词" + "。说明" * 40,
        user_prompt="机构版 {{payload}}",
        output_schema={"type": "object", "properties": {"a": {"type": "string"}}},
        glossary={"甲": "机构的术语说明文本内容"},
        version=1, status=PromptTemplateStatus.ACTIVE,
    )
    P.invalidate(org_id=org.id)
    got = P.load_template(scene_key("polish_stem"), org_id=org.id)
    check("机构模板优先于平台默认", got is not None and got.pk == org_row.pk,
          f"got={getattr(got, 'pk', None)} want={org_row.pk}")

    # 机构只有 DRAFT → 回退平台
    AIPromptTemplate.objects.filter(pk=org_row.pk).update(
        status=PromptTemplateStatus.DRAFT
    )
    P.invalidate(org_id=org.id)
    fallback = P.load_template(scene_key("polish_stem"), org_id=org.id)
    check("机构只有 DRAFT 时回退平台默认",
          fallback is not None and fallback.org_id is None,
          f"org_id={getattr(fallback, 'org_id', None)}")

    # 缓存：改库后不 invalidate 仍读旧值
    AIPromptTemplate.objects.filter(pk=org_row.pk).update(
        status=PromptTemplateStatus.ACTIVE
    )
    cached = P.load_template(scene_key("polish_stem"), org_id=org.id)
    check("缓存期内仍读旧值（未失效前不回源）",
          cached is not None and cached.org_id is None,
          f"org_id={getattr(cached, 'org_id', None)}")
    P.invalidate(org_id=org.id)
    fresh = P.load_template(scene_key("polish_stem"), org_id=org.id)
    check("invalidate 后立即生效",
          fresh is not None and fresh.pk == org_row.pk,
          f"got={getattr(fresh, 'pk', None)}")

    # ══════════════════════════════════════════════════════
    print("\n== E 编排器端到端 ==")

    # ★ 必须先把 L 段造的机构模板删掉：否则编排器读到的是那条
    #   「机构版系统提示词」，而不是基线模板——断言会假失败。
    AIPromptTemplate.objects.filter(pk=org_row.pk).delete()
    P.invalidate(org_id=org.id)

    from apps.ai.base import AIRequest
    from apps.ai.masking import sanitize
    from apps.ai.orchestrator import AIOrchestrator, CallContext

    payload, mapping, fields = sanitize(
        {"stem": "自检题干：一段待润色的文字"}, persist_mapping=False,
    )
    req = AIRequest(
        task="polish_stem", prompt_template_key=scene_key("polish_stem"),
        payload=payload, org_id=org.id, teacher_id=None,
        idempotency_key="polish_stem:9001:1",
    )
    ctx = CallContext(org_id=org.id, mapping=mapping, field_names=fields)
    sys_p, usr_p = AIOrchestrator._build_prompts(req, ctx)

    check("编排器取到模板（system 含模板特征）",
          "语言层面" in sys_p or "润色" in sys_p, sys_p[:60])
    check("编排器返回的 user 提示词含实际输入",
          "自检题干" in usr_p, usr_p[:80])
    check("ctx.prompt_version 被记为 1", ctx.prompt_version == 1,
          str(ctx.prompt_version))

    # 无模板 → 回退 + version 为 None
    req_miss = AIRequest(
        task="polish_stem", prompt_template_key="jxzc.secondary_vocational.不存在",
        payload=payload, org_id=org.id, teacher_id=None,
        idempotency_key="polish_stem:9002:1",
    )
    ctx2 = CallContext(org_id=org.id, mapping=mapping, field_names=fields)
    sys_p2, _ = AIOrchestrator._build_prompts(req_miss, ctx2)
    check("无模板时回退最小提示词", "只输出 JSON" in sys_p2, sys_p2[:60])
    check("回退时 prompt_version 为 NULL（不是 0）",
          ctx2.prompt_version is None, str(ctx2.prompt_version))

    # 显式指定 system_prompt 优先
    ctx3 = CallContext(
        org_id=org.id, mapping=mapping, field_names=fields,
        system_prompt="调用方显式指定的提示词",
    )
    sys_p3, _ = AIOrchestrator._build_prompts(
        AIRequest(
            task="polish_stem", prompt_template_key=scene_key("polish_stem"),
            payload=payload, org_id=org.id,
        ),
        ctx3,
    )
    check("调用方显式指定时优先", sys_p3 == "调用方显式指定的提示词", sys_p3[:40])

    # ── 真跑一次，验证 prompt_version 落库 ──
    from apps.ai.models import AICallLog

    r = AIOrchestrator.invoke(
        AIRequest(
            task="polish_stem", prompt_template_key=scene_key("polish_stem"),
            payload=payload, org_id=org.id, teacher_id=None,
            idempotency_key="polish_stem:9003:1",
        ),
        ctx=CallContext(org_id=org.id, mapping=mapping, field_names=fields),
    )
    check("端到端调用成功", r.ok, f"status={r.status} err={r.error_message}")
    log = AICallLog.objects.filter(pk=r.call_log_id).first() if r.call_log_id else None
    check("ai_call_log.prompt_version 落库为 1",
          log is not None and log.prompt_version == 1,
          f"got={getattr(log, 'prompt_version', None)}")
    check("ai_call_log 记下了模板 key",
          log is not None and log.prompt_template_key == scene_key("polish_stem"),
          f"got={getattr(log, 'prompt_template_key', None)}")

    # ══════════════════════════════════════════════════════
    print("\n== G seed 幂等与版本管理 ==")

    before = AIPromptTemplate.objects.count()
    buf = io.StringIO()
    call_command("seed_prompt_templates", stdout=buf, verbosity=0)
    after = AIPromptTemplate.objects.count()
    check("重跑 seed 不新建任何行", after == before, f"{before} → {after}")
    check("重跑输出含「跳过 11」", "跳过 11" in buf.getvalue(), buf.getvalue()[-60:])

    # 改内容但不加 --bump → 必须报冲突且不改库
    row = AIPromptTemplate.objects.get(
        org__isnull=True, key=scene_key("calibrate_difficulty"),
        status=PromptTemplateStatus.ACTIVE,
    )
    original = row.system_prompt
    AIPromptTemplate.objects.filter(pk=row.pk).update(
        system_prompt=original + "（人为改动）"
    )
    P.invalidate()
    from django.core.management.base import CommandError

    try:
        call_command(
            "seed_prompt_templates", stdout=io.StringIO(), verbosity=0,
        )
        check("内容不一致且未加 --bump 时报冲突", False, "未抛 CommandError")
    except CommandError:
        check("内容不一致且未加 --bump 时报冲突", True)

    still = AIPromptTemplate.objects.get(pk=row.pk)
    check("报冲突时没擅自改库", still.system_prompt == original + "（人为改动）",
          still.system_prompt[-20:])

    # 加 --bump → 归档旧的、新建 v2
    call_command(
        "seed_prompt_templates", "--bump",
        stdout=io.StringIO(), verbosity=0,
    )
    P.invalidate()
    versions = list(
        AIPromptTemplate.objects
        .filter(org__isnull=True, key=scene_key("calibrate_difficulty"))
        .order_by("version")
        .values_list("version", "status")
    )
    check("--bump 后有两个版本", len(versions) == 2, str(versions))
    check("v1 被归档", versions and versions[0] == (1, "ARCHIVED"), str(versions))
    check("v2 启用", len(versions) > 1 and versions[1] == (2, "ACTIVE"), str(versions))
    active_cnt = AIPromptTemplate.objects.filter(
        org__isnull=True, key=scene_key("calibrate_difficulty"),
        status=PromptTemplateStatus.ACTIVE,
    ).count()
    check("同 key 仍只有 1 条 ACTIVE", active_cnt == 1, str(active_cnt))

    # 基线缺场景必须报错
    import apps.ai.prompt_baseline as pb
    import apps.ai.management.commands.seed_prompt_templates as seed_mod

    saved = pb.BASELINES
    seed_mod.BASELINES = saved[:-1]
    try:
        call_command("seed_prompt_templates", stdout=io.StringIO(), verbosity=0)
        check("基线缺场景时报错", False, "未抛 CommandError")
    except CommandError:
        check("基线缺场景时报错", True)
    finally:
        seed_mod.BASELINES = saved

    print(f"\n{'=' * 46}\nPASS {PASS} / FAIL {FAIL}")
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print("  -", f)
    return 0 if FAIL == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["pg", "sqlite"], default="pg")
    args = ap.parse_args()

    tmp = _bootstrap(args.engine)
    if args.engine == "sqlite":
        from django.core.management import call_command

        print("== 迁移到临时 sqlite 库 ==")
        call_command("migrate", verbosity=0, interactive=False)
        print("  迁移完成")

    try:
        return run()
    finally:
        if tmp and os.path.exists(tmp):
            from django.db import connections

            connections.close_all()
            try:
                os.remove(tmp)
            except PermissionError:  # pragma: no cover
                print(f"  （临时库未能删除，可手动清理：{tmp}）")


if __name__ == "__main__":
    sys.exit(main())
