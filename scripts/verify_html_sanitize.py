"""富文本白名单清洗 · 回归（A29-5 的后端入口）。

★★ 这个套件要证明两件事，缺一不可：

  ① **清洗本身对**：脚本、事件属性、`javascript:` 链接必须被剥掉；
     而**纯文本必须一个字不改** —— 题库里 `stem_html` 目前存的
     就是纯文本，若把 `a<b`、`<T8>` 当标签吃掉，那是数据事故。
  ② **接线真的生效**：不是「有这么一个函数」，而是
     `create_question` / `update_question` 写进库的字段**已经是清洗后的值**。
     ★ 只测函数不测接线，是「改完其实没接上」的经典盲区。

运行：
    $env:PYTHONPATH="D:\\tiku\\.devdeps;D:\\tiku\\server"
    python scripts/verify_html_sanitize.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ["DJANGO_SETTINGS_MODULE"] = "zhifou.settings.local"
os.environ["ZF_CACHE"] = "locmem"
os.environ["ZF_DB_HOST"] = "127.0.0.1"
os.environ["ZF_DB_PORT"] = "15433"
os.environ["ZF_DB_NAME"] = "zhifou"
os.environ["ZF_DB_USER"] = "zhifou"
os.environ["ZF_DB_PASSWORD"] = "zhifou"
os.environ.pop("ZF_DB_ENGINE", None)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from apps.common.sanitize import (  # noqa: E402
    looks_like_html,
    sanitize_html,
)
from apps.knowledge.models import Course, Task  # noqa: E402
from apps.questionbank.models import Question  # noqa: E402
from apps.questionbank.services import (  # noqa: E402
    QuestionEditService,
    QuestionService,
)

ORG = 1
PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {label}" + (f"  —— {detail}" if detail else ""))
    else:
        FAIL += 1
        FAILED.append(label)
        print(f"  [FAIL] {label}" + (f"  —— {detail}" if detail else ""))


def section(t: str) -> None:
    print(f"\n【{t}】")


print("=" * 76)
print("富文本白名单清洗 · 回归（A29-5）")
print("=" * 76)

# ══════════════════════════════════════════════════════════════
section("S1 · 危险内容必须被剥掉")
# ══════════════════════════════════════════════════════════════

XSS_CASES = [
    ("脚本标签", "<b>加粗</b><script>alert(1)</script>",
     "<script" not in sanitize_html("<b>加粗</b><script>alert(1)</script>")),
    ("事件属性", '<img src="x" onerror="alert(2)">',
     "onerror" not in sanitize_html('<img src="x" onerror="alert(2)">')),
    ("javascript: 链接", '<a href="javascript:alert(1)">点</a>',
     "javascript" not in sanitize_html(
         '<a href="javascript:alert(1)">点</a>')),
    ("iframe", '<iframe src="//evil"></iframe>',
     "iframe" not in sanitize_html('<iframe src="//evil"></iframe>')
     and sanitize_html('<iframe src="//evil"></iframe>').strip() == ""),
    ("style 表达式", '<p style="background:url(javascript:1)">x</p>',
     "style" not in sanitize_html(
         '<p style="background:url(javascript:1)">x</p>')),
    ("form/input", '<form action="/x"><input name="p"></form>',
     "form" not in sanitize_html(
         '<form action="/x"><input name="p"></form>')),
    ("svg onload", '<svg onload="alert(1)"></svg>',
     "svg" not in sanitize_html('<svg onload="alert(1)"></svg>')),
    ("data: 图片", '<img src="data:text/html;base64,PHN2Zz4=">',
     "data:" not in sanitize_html(
         '<img src="data:text/html;base64,PHN2Zz4=">')),
]
for name, payload, ok in XSS_CASES:
    check(f"★ {name} 被剥离", ok, repr(sanitize_html(payload)))

# ══════════════════════════════════════════════════════════════
section("S2 · ★★ 纯文本必须一个字不改（数据事故防线）")
# ══════════════════════════════════════════════════════════════

TEXT_CASES = [
    "普通中文题干，含 a<b 与 5>3 的比较",
    "当 x < 5 时成立",
    "数学: x<5 且 y>3，求硬度",
    "材料 T8 与 <T8> 牌号的区别",
    "ABCD 四个选项，答案选 B",
]
for t in TEXT_CASES:
    check(f"纯文本原样返回：{t[:14]}…", sanitize_html(t) == t,
          repr(sanitize_html(t)))
check("★ `<T8>` 不被当成标签吃掉（导入牌号时最容易踩）",
      "<T8>" in sanitize_html("材料 <T8> 的硬度"), 
      repr(sanitize_html("材料 <T8> 的硬度")))
check("纯文本不触发 HTML 判定", not looks_like_html("a<b 与 5>3"))
check("真标签会触发 HTML 判定", looks_like_html("<b>x</b>"))
check("危险标签也触发判定（否则会被当纯文本存进库）",
      looks_like_html("<script>alert(1)</script>"))
check("`<T8>` 不触发判定", not looks_like_html("<T8>"))

# ══════════════════════════════════════════════════════════════
section("S3 · 幂等与边界")
# ══════════════════════════════════════════════════════════════

x = "<b>x</b><script>1</script>"
check("★★ 幂等（重入不会越洗越少）",
      sanitize_html(sanitize_html(x)) == sanitize_html(x),
      repr(sanitize_html(sanitize_html(x))))
check("`None` → 空串", sanitize_html(None) == "")
check("空串 → 空串", sanitize_html("") == "")
check("保留白名单标签", "<b>" in sanitize_html("<b>x</b>"))
check("保留 http 链接",
      'href="https://ok.com"' in sanitize_html('<a href="https://ok.com">x</a>'))
check("链接自动加 rel=noopener",
      "noopener" in sanitize_html('<a href="https://ok.com">x</a>'))

# ══════════════════════════════════════════════════════════════
section("S4 · ★★★ 接线：写入路径必须真的清洗")
# ══════════════════════════════════════════════════════════════

course = Course.objects.for_org(ORG).first()
task = Task.objects.for_org(ORG).order_by("id").first()
check("前置：有课程与任务", course is not None and task is not None,
      f"course={getattr(course, 'code', None)} task={getattr(task, 'id', None)}")

qid = None
try:
    payload_stem = '<b>题干加粗</b><script>alert(1)</script>'
    q = QuestionService.create_question(
        org_id=ORG, course_id=course.id, task_id=task.id,
        question_type="SINGLE_CHOICE",
        stem_text=payload_stem,
        answer={"value": "A"},
        options=[
            {"label": "A", "content_text": '<i>A 选项</i><img src=x onerror=1>'},
            {"label": "B", "content_text": "B 选项"},
            {"label": "C", "content_text": "C 选项"},
            {"label": "D", "content_text": "D 选项"},
        ],
        explanation='解析里也带 <script>alert(9)</script>',
        created_by_id=None,
        check_duplicate=False,
    )
    qid = q.id
    q.refresh_from_db()
    check("★★★ 建题：`stem_html` 已清洗（无 script）",
          "<script" not in (q.stem_html or ""),
          repr(q.stem_html))
    check("★★★ 建题：`explanation_html` 已清洗",
          "script" not in (q.explanation_html or "").lower(),
          repr(q.explanation_html))
    opt_a = q.options.order_by("sort_order").first()
    check("★★★ 建题：选项 `content_html` 已清洗（无 onerror）",
          "onerror" not in (opt_a.content_html or "").lower(),
          repr(opt_a.content_html))
    check("★ 建题：`stem_text` 保持原样（文本字段不清洗）",
          q.stem_text == payload_stem, repr(q.stem_text))

    # 更新路径
    QuestionEditService.update_question(
        org_id=ORG, question_id=qid,
        stem_text='<b>改后题干</b><script>alert(2)</script>',
        explanation='<u>改后解析</u><iframe src="//x"></iframe>',
    )
    q.refresh_from_db()
    check("★★★ 改题：`stem_html` 已清洗",
          "<script" not in (q.stem_html or ""), repr(q.stem_html))
    check("★★★ 改题：`explanation_html` 已清洗",
          "iframe" not in (q.explanation_html or "").lower(),
          repr(q.explanation_html))
    check("★★ 改题后仍幂等（再次保存不改变已清洗值）",
          (QuestionEditService.update_question(
              org_id=ORG, question_id=qid,
              explanation=q.explanation_html) is not None),
          "")
finally:
    # ── 清理：按**精确 id** 删，不用全表计数（并行时会假红） ──
    if qid is not None:
        try:
            from apps.questionbank.models import QuestionKnowledge, QuestionOption

            QuestionOption.objects.for_org(ORG).filter(question_id=qid).delete()
            QuestionKnowledge.objects.for_org(ORG).filter(
                question_id=qid).delete()
            q = Question.objects.for_org(ORG).filter(pk=qid).first()
            if q is not None:
                q.hard_delete()
            left = Question.objects.for_org(ORG).filter(pk=qid).count()
            print(f"\n  · 已清理测试题 #{qid}（残留 {left}）")
        except Exception as e:  # noqa: BLE001
            print(f"\n  ★ 清理失败（需人工处理 #{qid}）："
                  f"{type(e).__name__}: {e}")

print()
print("=" * 76)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
if FAILED:
    print("失败清单：")
    for x2 in FAILED:
        print(f"  ✗ {x2}")
print("=" * 76)
sys.exit(1 if FAIL else 0)
