"""脱敏规则：禁用字段清单、PII 模式、科目白名单。

★ 三条规则的分工（见 06 文档 3.2）：

    1. `PROHIBITED_FIELDS`      —— **精确键名**命中：整个值替换为稳定 token
    2. `PROHIBITED_KEY_FRAGMENTS` —— **词根**命中：覆盖 `teacher_phone`、
       `guardian_mobile` 这类变体（只列精确名会漏掉它们）
    3. `ALLOWED_KEY_FRAGMENTS`  —— 白名单：`filename`、`chapter_name`
       这类含 name 词根但**不是身份字段**的键要放行

★★ 为什么必须有白名单：`PROHIBITED_KEY_FRAGMENTS` 含 `name` 词根，
  而 `paper.template_name`、`knowledge_node.node_name` 都是正常的
  业务字段。没有白名单会把它们连同值一起脱敏，
  导致 AI 收到一堆 `P...` 而无法工作。
"""

from __future__ import annotations

import re

#: 精确禁用键名（小写比较）
PROHIBITED_FIELDS: tuple[str, ...] = (
    "student_name", "student_no", "real_name", "phone", "id_card",
    "school_name", "class_name", "email", "address", "guardian_name",
)

#: 词根禁用：任何键名**包含**这些片段即命中（覆盖变体写法）
PROHIBITED_KEY_FRAGMENTS: tuple[str, ...] = (
    "name", "phone", "mobile", "idcard", "id_card", "email", "address",
    "guardian", "parent", "studentno", "student_no", "school", "class",
)

#: 白名单：含禁用词根但**属于学科/系统字段**，必须放行。
#:
#: ★ 这张表是「宁可漏脱敏还是宁可误脱敏」的取舍结果：
#:   漏脱敏是合规事故，误脱敏会让功能不可用。
#:   白名单是**显式枚举**的，新增要评审——不能写成通配。
ALLOWED_KEY_FRAGMENTS: tuple[str, ...] = (
    "filename", "template_name", "node_name", "chapter_name", "entry_name",
    "figure_name", "model_name", "field_name", "type_name", "task_name",
    "course_name", "major_name", "paper_name", "style_name", "prompt_name",
    "title",           # 试卷标题属学科内容，单独列出
    "classroom_id",    # id 不是身份信息（不含姓名/学号明文）
    "class_id",
)

#: token 前缀（按字段种类区分，便于人工排查）
TOKEN_PREFIX: dict[str, str] = {
    "person": "S",     # 人名
    "org": "O",        # 学校/机构
    "phone": "T",      # 电话
    "idno": "I",       # 身份证
    "generic": "P",    # 其它（自由文本命中）
}


def key_kind(field: str) -> str:
    """键名 → token 前缀类别。"""
    f = (field or "").lower()
    if f in ("student_name", "real_name", "guardian_name") or "name" in f:
        return "person"
    if "school" in f or "org" in f:
        return "org"
    if "phone" in f or "mobile" in f:
        return "phone"
    if "id_card" in f or "idcard" in f:
        return "idno"
    return "generic"


#: 脱敏后生成的**安全键名后缀**。
#:
#: ★★ 关键：`is_prohibited_key()` 必须**幂等**——
#:   它自己产出的安全键名（`person_ref` / `phone_ref`）不能再被判为禁用。
#:   否则会出现「我生成的安全键被自己的出站校验拦住」这种自相矛盾：
#:   实测 `phone_ref` 里含 `phone` 词根，被误判为泄漏。
SAFE_KEY_SUFFIX = "_ref"
SAFE_KEY_RE = re.compile(r"^(person|org|phone|idno|generic)_ref(_\d+)?$")


def is_prohibited_key(key: str) -> bool:
    """键名是否命中禁用规则。

    ★ 判定顺序：
        ① 是脱敏自己生成的 `*_ref` → **放行**（幂等）
        ② 精确禁用名
        ③ 白名单（含禁用词根但属学科/系统字段）
        ④ 词根命中

      顺序不能调：白名单必须在词根之前，否则 `template_name`
      会先被 `name` 词根命中。
    """
    k = (key or "").lower().strip()
    if not k:
        return False
    # ① 幂等：脱敏后的安全键名放行
    if SAFE_KEY_RE.match(k):
        return False
    # ② 精确禁用名
    if k in PROHIBITED_FIELDS:
        return True
    # ③ 白名单
    if any(a in k for a in ALLOWED_KEY_FRAGMENTS):
        return False
    # ④ 词根
    return any(frag in k for frag in PROHIBITED_KEY_FRAGMENTS)
