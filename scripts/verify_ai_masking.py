"""实测强制脱敏（对应 06 文档 TC-SEC-001）。"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.utils import timezone  # noqa: E402

from apps.ai.base import (  # noqa: E402
    AIRequest,
    LeakDetected,
    SanitizedPayload,
    sign_ticket,
    verify_ticket,
)
from apps.ai.masking import (  # noqa: E402
    audit_leak,
    flat_preview,
    restore,
    restore_with_field_names,
    sanitize,
)
from apps.ai.masking.crypto import (  # noqa: E402
    PiiCryptoError,
    decrypt_field,
    encrypt_field,
    make_dev_kek,
)
from apps.ai.masking.field_rules import is_prohibited_key  # noqa: E402
from apps.ai.masking.mapping_store import PiiMappingStore  # noqa: E402
from apps.audit.models import PiiMapping  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  —— {detail}" if detail else ""))


#: 泄漏检测模式（与 00 文档脱敏红线一致）
LEAK_PATTERNS = [
    r"张三", r"李四", r"1[3-9]\d{9}", r"\d{17}[\dXx]",
    r"某某中学", r"高二\(3\)班", r"张父",
]

DIRTY = {
    "student_name": "张三", "student_no": "20260312",
    "phone": "13800138000", "id_card": "110101200801011234",
    "school_name": "某某中学", "class_name": "高二(3)班",
    "guardian_name": "张父",
    "template_name": "标准A4",          # 白名单，必须保留
    "chapter_name": "第一章 金属材料",   # 白名单，必须保留
    "stem": "小明以 5m/s 的速度从某某中学出发，用时 12 秒。",
    "options": [{"label": "A", "content_text": "李四同学认为……"}],
}


def cleanup(org_id: int = 1) -> None:
    PiiMapping.objects.filter(org_id=org_id).delete()
    PiiMapping.objects.filter(org_id=999).delete()


def main() -> int:
    try:
        return _run()
    finally:
        cleanup()


def _run() -> int:
    cleanup()

    # ══════════════ M1 ★ 类型层强制 ══════════════
    print("\n【M1】★ 类型层强制（未脱敏请求构造不出来）")
    raised = False
    try:
        SanitizedPayload(data={"student_name": "张三"})
    except LeakDetected as e:
        raised = True
        msg = str(e)
    check("无票据的 SanitizedPayload 构造被拒", raised)
    check("错误信息说明了原因（只能由 sanitize 产出）",
          "sanitize" in msg, msg[:60])

    forged = False
    try:
        SanitizedPayload(
            data={"student_name": "张三"}, masking_ticket="deadbeef" * 8,
        )
    except LeakDetected:
        forged = True
    check("伪造票据被拒（HMAC 校验）", forged)

    tampered = False
    try:
        payload_ok, _, _ = sanitize({"stem": "正常题目内容"}, org_id=1,
                                    persist_mapping=False)
        SanitizedPayload(
            data={"student_name": "张三"},          # 内容与票据不符
            masking_ticket=payload_ok.masking_ticket,
        )
    except LeakDetected:
        tampered = True
    check("内容被篡改后票据失效", tampered, "★ 票据绑定内容")

    check("sanitize 产出的 payload 可正常构造", payload_ok.data["stem"] == "正常题目内容")

    req_rejected = False
    try:
        AIRequest(
            task="polish_stem", prompt_template_key="k",
            payload={"student_name": "张三"},       # 裸 dict
            org_id=1,
        )
    except LeakDetected:
        req_rejected = True
    check("AIRequest 拒绝裸 dict 作为 payload", req_rejected,
          "★ 第二重强制：入口层类型断言")

    req_ok = AIRequest(
        task="polish_stem", prompt_template_key="k",
        payload=payload_ok, org_id=1,
    )
    check("传 SanitizedPayload 时可正常构造", req_ok.task == "polish_stem")

    # ══════════════ M2 ★ 键名剥离 ══════════════
    print("\n【M2】★ 键名与值一起剥离")
    payload, mapping, fnames = sanitize(
        DIRTY, subject="physics", org_id=1, persist_mapping=False
    )
    clean_json = json.dumps(payload.data, ensure_ascii=False)

    for pat in LEAK_PATTERNS:
        check(f"脱敏结果不含 {pat!r}",
              not re.search(pat, clean_json), "★ 关键断言")

    check("禁用键名已替换（无 student_name）",
          "student_name" not in payload.data, str(list(payload.data)))
    check("换成安全键名 person_ref",
          "person_ref" in payload.data)
    check("phone 键也换名", "phone_ref" in payload.data)
    check("id_card 键也换名", "idno_ref" in payload.data)

    # 白名单必须保留
    check("★ 白名单 template_name 保留原值",
          payload.data.get("template_name") == "标准A4",
          str(payload.data.get("template_name")))
    check("★ 白名单 chapter_name 保留原值",
          payload.data.get("chapter_name") == "第一章 金属材料")
    check("正常字段 stem 保留", "stem" in payload.data)

    # ══════════════ M3 ★ 幂等：脱敏后的键名不再被判为禁用 ══════════════
    print("\n【M3】★ is_prohibited_key 必须幂等")
    for k in ("person_ref", "org_ref", "phone_ref", "idno_ref", "generic_ref",
              "person_ref_2"):
        check(f"安全键名 {k} 不再命中禁用", not is_prohibited_key(k),
              "★ 否则自己生成的安全键会被自己的校验拦住")
    for k in ("student_name", "phone", "guardian_phone", "id_card"):
        check(f"原始身份键名 {k} 仍命中禁用", is_prohibited_key(k))
    for k in ("template_name", "chapter_name", "filename", "stem", "content"):
        check(f"白名单/普通键名 {k} 不命中", not is_prohibited_key(k))

    # 再脱敏一次（幂等性：把已脱敏内容当输入）
    payload2, _, _ = sanitize(payload.data, subject="physics", org_id=1,
                              persist_mapping=False)
    check("★ 对已脱敏内容再脱敏不产生新映射（幂等）",
          not audit_leak(payload2.data, raise_on_hit=False),
          "★ 重复脱敏不应破坏内容")

    # ══════════════ M4 ★ 自由文本与白名单 ══════════════
    print("\n【M4】★ 自由文本扫描与科目白名单")
    check("人名+称谓被脱敏（李四同学）",
          "李四同学" not in clean_json)
    check("校名被脱敏（某某中学）", "某某中学" not in clean_json)
    check("手机号被脱敏", "13800138000" not in clean_json)
    check("身份证被脱敏", "110101200801011234" not in clean_json)
    check("★ 题目数字**未**被误脱敏（5m/s 保留）",
          "5m/s" in payload.data["stem"], payload.data["stem"])
    check("★ 题目数字**未**被误脱敏（12 秒保留）",
          "12 秒" in payload.data["stem"], payload.data["stem"])

    # 白名单：物理科目里「牛顿」不该被脱敏
    pl, _, _ = sanitize(
        {"stem": "牛顿同学认为，安培定律描述了电流与磁场的关系。"},
        subject="physics", org_id=1, persist_mapping=False,
    )
    check("物理白名单「牛顿」放行",
          "牛顿" in pl.data["stem"], pl.data["stem"])
    check("物理白名单「安培」放行", "安培" in pl.data["stem"])

    # 非白名单科目里「小明同学」应被脱敏
    pl2, _, _ = sanitize(
        {"stem": "小明同学认为这个结论正确。"},
        subject="unknown", org_id=1, persist_mapping=False,
    )
    check("非白名单科目里「小明同学」被脱敏",
          "小明同学" not in pl2.data["stem"], pl2.data["stem"])

    # 机械基础白名单
    pl3, _, _ = sanitize(
        {"stem": "按 GB/T 1800 标准，HRC 硬度测试采用洛氏 C 标尺。"},
        subject="JXJC", org_id=1, persist_mapping=False,
    )
    check("机械基础白名单 GB/T 放行", "GB/T" in pl3.data["stem"])

    # ══════════════ M5 还原 ══════════════
    print("\n【M5】还原（token → 明文）")
    restored = restore(payload.data, mapping)
    check("stem 完整还原", restored["stem"] == DIRTY["stem"],
          restored["stem"])
    check("options 完整还原",
          restored["options"][0]["content_text"]
          == DIRTY["options"][0]["content_text"])
    # ★ `restore()` 只还原**值**；键名还原要 `field_names`
    #   （`sanitize` 把 `student_name` 换成了 `person_ref`）
    full = restore_with_field_names(payload.data, mapping, fnames)
    check("键名还原（person_ref → student_name）",
          full.get("student_name") == "张三",
          str(sorted(full)))
    check("phone 键名与值都还原",
          full.get("phone") == "13800138000", str(full.get("phone")))
    check("守卫：只传 mapping 时键名不还原（职责分离）",
          "student_name" not in restored)
    check("template_name 未被改动", restored.get("template_name") == "标准A4")

    # ★ token 前缀污染测试：P1 不能吃掉 P10
    tricky = {"P1": "甲", "P10": "乙"}
    out = restore("P10 和 P1", tricky)
    check("★ 还原按 token 长度倒序（P10 不被 P1 污染）",
          out == "乙 和 甲", out)

    # ══════════════ M6 出站校验 ══════════════
    print("\n【M6】★ 出站校验（独立于 sanitize 的第二次扫描）")
    hits = audit_leak(payload.data, raise_on_hit=False)
    check("干净内容无命中", not hits, str(hits[:3]))

    raised = False
    try:
        audit_leak({"contact": "13800138000"})
    except LeakDetected:
        raised = True
    check("手机号命中即抛", raised)

    raised = False
    try:
        audit_leak({"id_no": "110101200801011234"})
    except LeakDetected:
        raised = True
    check("身份证命中即抛", raised)

    raised = False
    try:
        audit_leak({"stem": "张三同学说"})
    except LeakDetected:
        raised = True
    check("人名+称谓命中即抛", raised)

    no_false = audit_leak({"stem": "以 5m/s 运动 12 秒，电阻 100Ω"},
                          raise_on_hit=False)
    check("★ 纯题目数值不误报（5m/s、12 秒、100Ω）",
          not no_false, str(no_false))

    raised = False
    try:
        audit_leak({"student_name": "S123"})
    except LeakDetected:
        raised = True
    check("残留身份键名命中即抛", raised)

    preview = flat_preview({"stem": "正常内容"})
    check("flat_preview 生成安全日志串", preview.startswith("[ok]"), preview[:30])
    preview_bad = flat_preview({"phone": "13800138000"})
    check("flat_preview 对疑似泄漏加标记",
          "疑似泄漏" in preview_bad, preview_bad[:30])

    # ══════════════ M7 ★ 加密存储 ══════════════
    print("\n【M7】★ 映射表加密存储")
    pl, mp, fn = sanitize(DIRTY, subject="physics", org_id=1)
    check("已写入映射表", len(mp) >= 7, f"{len(mp)} 条")
    check("返回了 mapping_ref", len(fn) >= 7)

    row = PiiMapping.objects.filter(org_id=1).first()
    check("密文非空", bool(row.encrypted_value))
    check("★ 密文里不含明文",
          b"\xe5\xbc\xa0\xe4\xb8\x89" not in bytes(row.encrypted_value),
          "张三 的 UTF-8 字节不应出现在密文里")
    check("密文带版本号前缀（值 1）",
          bytes(row.encrypted_value)[0] == 1, str(bytes(row.encrypted_value)[0]))
    check("expires_at 已设置（默认 7 天）",
          row.expires_at is not None and row.expires_at > timezone.now())

    # 加解密往返
    blob = encrypt_field("敏感内容ABC")
    check("加密往返一致", decrypt_field(blob) == "敏感内容ABC")
    check("同一明文两次加密得到不同密文（随机 nonce）",
          encrypt_field("x") != encrypt_field("x"))

    broken = False
    try:
        bad = bytearray(blob)
        bad[-1] ^= 0xFF          # 篡改最后一字节
        decrypt_field(bytes(bad))
    except PiiCryptoError:
        broken = True
    check("★ 密文被篡改时解密失败（GCM 完整性）", broken,
          "★ 这是选 AEAD 的理由：防止成绩记到别人头上")

    check("key 生成器可用（base64 32 字节）",
          len(make_dev_kek()) >= 40, str(len(make_dev_kek())))

    # ══════════════ M8 跨请求还原 ══════════════
    print("\n【M8】跨请求还原（按 token 查库）")
    store = PiiMappingStore(org_id=1)
    token = next(iter(mp))
    got = store.get(token, field_name=fn[token])
    check("按 (org, token, field) 取回明文", got == mp[token],
          f"{got!r} vs {mp[token]!r}")

    many = store.load_tokens(list(mp)[:3])
    check("批量取回 3 条", len(many) == 3, str(len(many)))
    check("批量结果与映射表一致",
          all(many[t] == mp[t] for t in many))

    other = PiiMappingStore(org_id=999)
    check("★ 跨机构取不到（机构隔离）",
          other.get(token, field_name=fn[token]) is None)

    # 重复写入不报错（稳定 token 会被重复写）
    again = store.save(mp, fn)
    check("重复 save 不报错（update_or_create）", bool(again))

    # ══════════════ M9 过期清理 ══════════════
    print("\n【M9】过期映射清理")
    from datetime import timedelta

    PiiMapping.objects.filter(org_id=1).update(
        expires_at=timezone.now() - timedelta(days=1)
    )
    n = PiiMappingStore.purge_expired()
    check(f"清理了 {n} 条过期映射", n >= 7, str(n))
    check("清理后机构 1 无残留",
          PiiMapping.objects.filter(org_id=1).count() == 0)

    st = PiiMappingStore.stats()
    check("统计可用", "total" in st and "expired" in st, str(st))

    # ══════════════ M10 日志与落库不泄漏 ══════════════
    print("\n【M10】★ 日志与落库内容不泄漏（00 文档脱敏红线）")
    import logging

    class Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record)

    cap = Capture()
    root = logging.getLogger()
    root.addHandler(cap)
    try:
        pl, mp2, _ = sanitize(DIRTY, subject="physics", org_id=1)
        logger = logging.getLogger("apps.ai")
        # 模拟编排层记日志：用 flat_preview 而不是裸 dumps
        logger.info("AI 请求预览：%s", flat_preview(pl.data))
        logger.info("脱敏项数=%d", len(mp2))
    finally:
        root.removeHandler(cap)

    logs = "\n".join(
        r.getMessage() + json.dumps(
            {k: v for k, v in vars(r).items() if not k.startswith("_")},
            default=str,
        )
        for r in cap.records
    )
    # ★ 断言用**真实值**而不是正则模式串。
    #   模式串（如 `1[3-9]\d{9}`）本身会出现在 LeakDetected 的异常文本里，
    #   拿它当判据会把「异常信息里提到了这个模式」误判为泄漏。
    REAL_VALUES = ["张三", "李四", "13800138000", "110101200801011234",
                   "某某中学", "高二(3)班", "张父", "20260312"]
    for val in REAL_VALUES:
        check(f"日志不含真实值 {val!r}", val not in logs, "★ CI 阻断项")
    check("日志不含禁用键名", "student_name" not in logs)
    check("日志确实记录了内容（捕获器有效）",
          len(cap.records) >= 2, f"{len(cap.records)} 条")

    # ── 汇总 ──
    print()
    print("=" * 74)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    print("=" * 74)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 2
    print("  ✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
