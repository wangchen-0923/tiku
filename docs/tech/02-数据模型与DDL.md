# 02 · 数据模型与 DDL

> 上游依据：[开发设计文档 v2.1](../知否-开发设计文档-v2.1.md) 第 12 章。
> **本文是数据库字段的唯一事实来源。** 其他技术文件引用本文，不重定义字段。
> 命名、枚举、时间数值约定见 [README](README.md)。

---

## 一、通用约定

### 1.1 建表模板

```sql
CREATE TABLE <table_name> (
    id              bigserial PRIMARY KEY,
    org_id          bigint NOT NULL REFERENCES org(id),
    -- 业务字段 --
    status          varchar(20) NOT NULL DEFAULT 'PENDING',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz
);
CREATE INDEX idx_<table>_<fields> ON <table> (<fields>) WHERE deleted_at IS NULL;
COMMENT ON TABLE <table_name> IS '<中文说明>';
COMMENT ON COLUMN <table_name>.<field> IS '<说明>';
```

**铁律**：

| 规则 | 说明 |
| --- | --- |
| 主键 | 一律 `bigserial`，不用 UUID 做主键 |
| 机构隔离 | 含 `org_id` 的表必须建 `(org_id, ...)` 前缀索引 |
| 软删除 | 业务表用 `deleted_at`，**不物理删除**；唯一约束需带 `WHERE deleted_at IS NULL` |
| 时间 | 一律 `timestamptz`，默认 `now()` |
| 枚举 | 存 `varchar`，值见 README 第五节；**不用 PG enum 类型**（改值需迁移，成本高） |
| JSONB | 必须有应用层 Schema 校验；关键查询字段不从 JSONB 里取 |
| updated_at | 用触发器或 Django `auto_now`（团队统一用 Django 层） |
| 数值 | 得分率/掌握度/置信度 `numeric(4,3)`；难度 `numeric(4,2)`；金额 `numeric(10,4)` |

### 1.2 触发器

```sql
-- updated_at 自动维护（所有含 updated_at 的表统一挂）
CREATE OR REPLACE FUNCTION trg_set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
```

### 1.3 扩展

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;    -- 中文/英文模糊检索
CREATE EXTENSION IF NOT EXISTS vector;     -- pgvector：相似题、知识库语义检索
```

---

## 二、模块 ER 总览

```mermaid
erDiagram
    ORG ||--o{ CLASSROOM : has
    CLASSROOM ||--o{ CLASSROOM_STUDENT : has
    ORG ||--o{ KNOWLEDGE_NODE : has
    KNOWLEDGE_NODE ||--o{ CHAPTER : "章节挂节点"
    CHAPTER ||--o{ KNOWLEDGE_ENTRY : has
    KNOWLEDGE_ENTRY }o--o{ KNOWLEDGE_NODE : "多对多"
    ORG ||--o{ QUESTION : has
    QUESTION ||--o{ QUESTION_OPTION : has
    QUESTION ||--o{ QUESTION_IMAGE : has
    QUESTION }o--o{ KNOWLEDGE_NODE : "question_knowledge"
    QUESTION ||--o{ QUESTION_RELATION : "变式/相似"
    PAPER ||--o{ PAPER_ITEM : has
    PAPER_ITEM }o--|| QUESTION : refs
    SHEET_STYLE ||--o{ SHEET_TEMPLATE : generates
    PAPER ||--o{ PAPER_SHEET_BINDING : binds
    SHEET_TEMPLATE ||--o{ PAPER_SHEET_BINDING : bound
    ASSIGNMENT }o--|| PAPER : uses
    ASSIGNMENT }o--|| CLASSROOM : to
    ASSIGNMENT }o--|| SHEET_TEMPLATE : "答题卡"
    ASSIGNMENT ||--o{ ASSIGNMENT_STUDENT : has
    ASSIGNMENT_STUDENT ||--o{ RESPONSE : has
    PAPER_ITEM ||--o{ RESPONSE : answers
    RESPONSE ||--o| SCORE_RESULT : graded
    SCAN_BATCH ||--o{ SCAN_CARD : has
    SCAN_CARD ||--o{ RESPONSE : produces
    STUDENT ||--o{ KNOWLEDGE_MASTERY : owns
    KNOWLEDGE_NODE ||--o{ KNOWLEDGE_MASTERY : of
    PRACTICE_SET }o--|| PAPER : "专属试卷"
    PRACTICE_SET ||--o{ PRACTICE_SET_ITEM : has
```

---

## 三、组织与用户

### 3.1 `org` — 机构

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| name | varchar(200) | NOT NULL | 全称 |
| short_name | varchar(50) | | 简称（卷面页眉） |
| logo_url | varchar(500) | | |
| type | varchar(20) | NOT NULL DEFAULT 'SCHOOL' | SCHOOL / TRAINING |
| **ai_config** | jsonb | NOT NULL DEFAULT '{}' | AI 覆盖：模型偏好、预算、生题可用性阈值、脱敏白名单 |
| **grading_config** | jsonb | NOT NULL DEFAULT '{}' | 批改覆盖：填空阈值、OMR 阈值、默认学号位数 |
| sheet_default_style_id | bigint | REFERENCES sheet_style(id) | 默认答题卡样式 |
| quota_config | jsonb | NOT NULL DEFAULT '{}' | 容量与配额 |
| status | varchar(20) | NOT NULL DEFAULT 'ACTIVE' | ACTIVE / SUSPENDED |
| created_at / updated_at / deleted_at | timestamptz | | |

```sql
CREATE TABLE org (
    id                      bigserial PRIMARY KEY,
    name                    varchar(200) NOT NULL,
    short_name              varchar(50),
    logo_url                varchar(500),
    type                    varchar(20) NOT NULL DEFAULT 'SCHOOL',
    ai_config               jsonb NOT NULL DEFAULT '{}'::jsonb,
    grading_config          jsonb NOT NULL DEFAULT '{}'::jsonb,
    sheet_default_style_id  bigint,
    quota_config            jsonb NOT NULL DEFAULT '{}'::jsonb,
    status                  varchar(20) NOT NULL DEFAULT 'ACTIVE',
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    deleted_at              timestamptz
);
```

`ai_config` 结构（Schema 校验）：

```json
{
  "model_preference": {"ocr_question": "qwen3-vl-plus", "gen_explanation": "deepseek-chat"},
  "daily_budget_yuan": 50,
  "monthly_budget_yuan": 800,
  "enabled_tasks": ["ocr_question", "polish_stem", "gen_explanation", "tag_question"],
  "gen_available": {"kb_coverage": 0.60, "bank_size": 500},
  "pii_whitelist": ["牛顿", "法拉第", "欧姆"]
}
```

`grading_config` 结构：

```json
{
  "fill_blank": {"fuzzy_accept": 0.92, "fuzzy_review": 0.75, "fuzzy_max_len": 12, "enable_ai": true, "ai_min_confidence": 0.85},
  "omr": {
    "UPLOAD":   {"fill_threshold_filled": 0.45, "fill_threshold_empty": 0.15},
    "PHONE_H5": {"fill_threshold_filled": 0.50, "fill_threshold_empty": 0.20},
    "anchor_max_deviation": 0.01,
    "card_confidence_threshold": 0.95,
    "student_no_confidence_threshold": 0.98,
    "threshold_version": "v1"
  },
  "student_no_default_digits": 8,
  "subjective_default_view": "BY_QUESTION",
  "subjective_allow_aggregate_mode": true
}
```

### 3.2 `user` — 用户

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | REFERENCES org(id) | 学生账号可空 |
| username | varchar(150) | NOT NULL UNIQUE | |
| password | varchar(128) | NOT NULL | Django PBKDF2 |
| real_name | varchar(100) | NOT NULL | |
| phone | varchar(255) | | **应用层加密**（AES-GCM + 检索用哈希列） |
| phone_hash | varchar(64) | | 用于按手机号检索（HMAC-SHA256） |
| role | varchar(20) | NOT NULL | PLATFORM_ADMIN / ORG_ADMIN / TEACHER / **STUDENT** |
| status | varchar(20) | NOT NULL DEFAULT 'ACTIVE' | ACTIVE / DISABLED |
| last_login_at | timestamptz | | |
| created_at / updated_at / deleted_at | | | |

> **★ `STUDENT` = 学生身份行（2026-10-16 落地，`accounts.StudentIdentityService`）**
>
> 每个名册学生（学号）一行，**发布任务时自动建**，用户名规则
> `stu-{org_id}-{学号}`，`password` 是 Django 的**不可用密码**
> （`set_unusable_password()` → `!` 开头）。
>
> | 概念 | 载体 |
> | --- | --- |
> | 这个人是谁（作答/成绩/学情挂谁） | 这一行（`role=STUDENT`） |
> | 他能不能自己登录看数据 | 这一行**有没有可用密码**（`User.can_login`） |
> | 他是几号、叫什么 | `classroom_student` / `assignment_student` 的学号姓名快照 |
>
> - 因此**七张业务表（response / score_result / student_total_score /
>   knowledge_mastery / diagnosis_snapshot / practice_set / scan_card）
>   一个字都没改** —— 它们照旧按 `student_id` 取数；
> - 学生**不绑账号也能用「学号 + 姓名」线上作答**（决策 8 = A）；
> - 日后「绑定账号」= 给这一行设密码/绑手机 → `can_login` 自动翻转，
>   历史数据天然在它名下，**不需要迁移历史**；
> - ★ 身份行**不进**「用户管理」列表（`UserSelector.list_users`
>   默认只返回 `PERMISSIONS.STAFF_ROLES`）。
>
> ⚠ 判据迁移：凡是用 `ClassroomStudent.student IS NULL` 表示
> 「未绑账号」的地方，都要改成 `student.can_login`（或
> `has_usable_password_hash()`）—— 身份行建好后那个字段永远非空。

```sql
CREATE UNIQUE INDEX uq_user_username ON user (username) WHERE deleted_at IS NULL;
CREATE INDEX idx_user_org_role ON user (org_id, role) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX uq_user_phone_hash ON user (phone_hash) WHERE phone_hash IS NOT NULL AND deleted_at IS NULL;
```

### 3.3 `classroom` — 班级

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| name | varchar(100) | NOT NULL | 机械基础·高一(1)班 |
| grade | varchar(20) | NOT NULL | 一年级 |
| **grade_band** | varchar(20) | NOT NULL | PRIMARY / JUNIOR / SENIOR / **SECONDARY_VOCATIONAL**（中等职业学校，职高/中职）/ HIGHER_VOCATIONAL |
| **major_id** | bigint | REFERENCES major(id) | **★ 该班所属专业（机械/财会/计算机/电商）；同一班级通常只学一个专业的课** |
| head_teacher_id | bigint | REFERENCES user(id) | |
| academic_year | varchar(20) | NOT NULL | 2026-2027 |
| status | varchar(20) | NOT NULL DEFAULT 'ACTIVE' | |
| created_at / updated_at / deleted_at | | | |

```sql
CREATE UNIQUE INDEX uq_classroom_org_name_year
    ON classroom (org_id, name, academic_year) WHERE deleted_at IS NULL;
CREATE INDEX idx_classroom_org_grade ON classroom (org_id, grade_band) WHERE deleted_at IS NULL;
CREATE INDEX idx_classroom_major ON classroom (major_id) WHERE deleted_at IS NULL;
```

### 3.4 `major` — 专业（★ 职业教育适配新增）

**职业学校的顶层教学组织单位。** 一个学校有多个专业（机械、财会、计算机、电商），每个专业下有多门课程，不同专业的题库与知识点树必须隔离（跨专业不能串题）。

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| name | varchar(100) | NOT NULL | 机械制造技术 / 会计事务 / 计算机应用 / 电子商务 |
| code | varchar(80) | NOT NULL | 专业代码（可用教育部专业代码，如 `660101`） |
| short_name | varchar(50) | | 机械 / 财会 / 计算机 / 电商 |
| level | varchar(20) | | SECONDARY（中职）/ HIGHER（高职） |
| sort_order | int | NOT NULL DEFAULT 0 | |
| status | varchar(20) | NOT NULL DEFAULT 'ACTIVE' | |
| created_at / updated_at / deleted_at | | | |

```sql
CREATE UNIQUE INDEX uq_major_org_code ON major (org_id, code) WHERE deleted_at IS NULL;
CREATE INDEX idx_major_org ON major (org_id, sort_order) WHERE deleted_at IS NULL;
```

> **普通中学（K12）如何使用**：不需要建 `major`，`classroom.major_id` 留空即可。系统以 `major_id` 是否为空来判断是否启用专业维度——**同一套代码支持两种形态，不做两套**。

### 3.5 `classroom_student` — 班级学生名册

**这是免登录认人与答题卡学号匹配的基础表。**

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | 冗余，便于隔离查询 |
| classroom_id | bigint | NOT NULL | |
| student_id | bigint | REFERENCES user(id) | 可空（无账号学生也在名册） |
| **student_no** | varchar(50) | NOT NULL | **学号/考号，与答题卡学号区对应** |
| name | varchar(100) | NOT NULL | 名册姓名 |
| display_name | varchar(100) | | 展示名（处理重名，如「张三(大)」） |
| seat_no | varchar(20) | | 座位号 |
| join_at | timestamptz | NOT NULL DEFAULT now() | |
| leave_at | timestamptz | | 转班不删记录 |
| status | varchar(20) | NOT NULL DEFAULT 'ACTIVE' | ACTIVE / LEFT |
| created_at / updated_at / deleted_at | | | |

```sql
-- ★ 学号在同一班内唯一（答题卡靠学号认人，重复会导致成绩记错人）
CREATE UNIQUE INDEX uq_classroom_student_no
    ON classroom_student (classroom_id, student_no) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX uq_classroom_student_active
    ON classroom_student (classroom_id, student_id)
    WHERE status = 'ACTIVE' AND deleted_at IS NULL AND student_id IS NOT NULL;
CREATE INDEX idx_classroom_student_name ON classroom_student (classroom_id, name);
```

> **名册导入必须校验**：① 班内学号唯一 ② 同学号位数一致（不一致要告警，因为答题卡无法统一）。

---

## 四、知识体系（课程 / 章 / 任务 / 知识点 / 知识库）

> **★ 结构变更说明（v1.1，职业教育适配）**
>
> 原设计为「学科 → 知识点」两层，按普通高中（K12）建模。实际数据（职高机械基础）的层级是**四层**：
>
> ```
> 专业 major（机械制造技术）
>  └─ 课程 course（机械基础）            ← 原 subject 字段升级为独立实体
>      └─ 章 chapter（第1章 金属材料与热处理）  ← chapter 表；「篇」作为属性，不独立成层
>           └─ 任务 task（任务一：分析轴的力学性能）  ← ★ 新增实体
>                └─ 知识点 knowledge_node（1. 金属材料性能分类）
>                     └─ 核心内容（使用性能：物理、化学、力学性能。）  ← knowledge_entry
> ```
>
> **为什么 `task` 必须独立成实体**：题库的「章节号」实际是 `章.任务`（如 `1.1` = 第1章任务一），**任务是组卷的最小粒度**，也是知识点树的第三层。若把任务塞进 `chapter` 的 `parent_id` 层级里，则无法表达「题目标到任务、知识点挂到任务」这个关系。
>
> **`subject` 字段的去留**：所有业务表的 `subject` 字段（varchar）**保留但语义改为「课程编码」**，指向 `course.code`。这样避免大规模改列名，同时新增 `course_id` 外键用于强约束。**新代码应使用 `course_id`，`subject` 仅为兼容与检索冗余。**

### 4.1 `course` — 课程（★ 职业教育适配新增）

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | | null = 平台模板 |
| **major_id** | bigint | REFERENCES major(id) | **所属专业；K12 场景留空** |
| name | varchar(200) | NOT NULL | 机械基础 / 机械识图 / 零件测量 |
| **code** | varchar(80) | NOT NULL | 课程编码，如 `MJJC`（机械基础） |
| grade_band | varchar(20) | NOT NULL | **SECONDARY_VOCATIONAL** 等 |
| **textbook** | jsonb | NOT NULL DEFAULT '{}' | `{name, publisher, edition, isbn}` 教材信息 |
| total_hours | int | | 总课时 |
| **style_config** | jsonb | NOT NULL DEFAULT '{}' | **课程命题风格**（见下方说明） |
| sort_order | int | NOT NULL DEFAULT 0 | |
| is_template | boolean | NOT NULL DEFAULT false | |
| status | varchar(20) | NOT NULL DEFAULT 'ACTIVE' | |
| created_at / updated_at / deleted_at | | | |

```sql
CREATE UNIQUE INDEX uq_course_org_code
    ON course (COALESCE(org_id, 0), code) WHERE deleted_at IS NULL;
CREATE INDEX idx_course_major ON course (major_id, sort_order) WHERE deleted_at IS NULL;
```

**`style_config` 说明**（职业教育特有，直接影响 AI 生题与批改）：

```json
{
  "context_required": true,
  "context_examples": ["宝武集团", "比亚迪", "减速器维护", "数控机床"],
  "context_rule": "命题必须嵌入真实岗位/企业情境，不得使用「某物体」「小明」这类去情境化表述",
  "terminology_strict": true,
  "allow_drawing_question": false,
  "question_style": "TASK_DRIVEN"
}
```

> **`context_required` 是本项目的一条硬规则**：机械基础的题库里**每一题都有真实工业情境**（宝武集团机床床身、比亚迪电池外壳冲压、减速器维护、销的标记、齿轮拆卸）。这是职教「任务驱动」命题法。**AI 生题与改写必须保留情境，不得去情境化**——否则产出与存量题库风格不一致，教师一眼就能看出是机器生成的。

### 4.2 `knowledge_node` — 知识点节点

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | | **null = 平台模板** |
| **course_id** | bigint | REFERENCES course(id) | **★ 所属课程（推荐使用）** |
| subject | varchar(20) | NOT NULL | 课程编码（兼容字段，与 `course.code` 一致） |
| grade_band | varchar(20) | NOT NULL | |
| **task_id** | bigint | REFERENCES task(id) | **★ 所属任务（第三层）** |
| parent_id | bigint | REFERENCES knowledge_node(id) | null = 根（一般即任务层） |
| name | varchar(200) | NOT NULL | |
| **code** | varchar(80) | | **唯一编码，分层生成：`MJJC-C1-T1-K1`** |
| **path** | varchar(500) | NOT NULL | 物化路径 `机械基础/第1章 金属材料与热处理/任务一 分析轴的力学性能/1. 金属材料性能分类` |
| level | smallint | NOT NULL DEFAULT 1 | |
| sort_order | int | NOT NULL DEFAULT 0 | |
| exam_weight | numeric(3,2) | NOT NULL DEFAULT 1.0 | 考纲权重（学情组卷加权用） |
| **seq_in_task** | smallint | | **教材原始序号（如「1.」「2.」），用于与纸质书对照** |
| is_template | boolean | NOT NULL DEFAULT false | |
| template_source_id | bigint | | 来自哪个模板节点 |
| status | varchar(20) | NOT NULL DEFAULT 'ACTIVE' | |
| created_at / updated_at / deleted_at | | | |

```sql
CREATE UNIQUE INDEX uq_knowledge_node_org_code
    ON knowledge_node (COALESCE(org_id, 0), code) WHERE deleted_at IS NULL AND code IS NOT NULL;
CREATE INDEX idx_knowledge_node_tree
    ON knowledge_node (org_id, course_id, parent_id, sort_order) WHERE deleted_at IS NULL;
CREATE INDEX idx_knowledge_node_task ON knowledge_node (task_id, sort_order) WHERE deleted_at IS NULL;
CREATE INDEX idx_knowledge_node_path ON knowledge_node (path varchar_pattern_ops);
```

> **★ 编码规则放宽（v1.1）**：原设计的编码校验正则只允许「字母数字与点」，会**拒绝**实际使用的编码形式（如 `MJJC-C1-T1-K1`）。现放宽为：
>
> | 规则 | 值 |
> | --- | --- |
> | 长度 | 1–80 |
> | 允许字符 | 字母、数字、`-`、`_`、`.`、中文 |
> | 禁止 | 空白字符、`/`、`\`、以 `.` 或 `-` 开头 |
> | 唯一性范围 | `(COALESCE(org_id,0), code)` |
>
> **推荐编码方案（分层生成，可读且稳定）**：`<课程码>-C<章号>-T<任务号>-K<知识点序号>`，例：`MJJC-C1-T1-K1`。**改名不影响编码**，这是编码相对名称的唯一优势。

### 4.3 `task` — 任务（★ 职业教育适配新增，第三层）

**任务是职教教材的基本教学单元，也是组卷的最小粒度与题目标签的最细粒度（存量题库只标到这一级）。**

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | | null = 平台模板 |
| **course_id** | bigint | NOT NULL REFERENCES course(id) | |
| **chapter_id** | bigint | NOT NULL REFERENCES chapter(id) | **所属章** |
| **seq** | smallint | NOT NULL | **任务序号（对应题库的 `X.Y` 中的 Y）** |
| name | varchar(200) | NOT NULL | 任务一：分析轴的力学性能 |
| code | varchar(80) | NOT NULL | `MJJC-C1-T1` |
| path | varchar(500) | NOT NULL | |
| objectives | jsonb | NOT NULL DEFAULT '[]' | 教学目标（"了解/理解/掌握/能…"） |
| is_practical | boolean | NOT NULL DEFAULT false | **是否实训任务**（职教特有） |
| sort_order | int | NOT NULL DEFAULT 0 | |
| status | varchar(20) | NOT NULL DEFAULT 'ACTIVE' | |
| created_at / updated_at / deleted_at | | | |

```sql
CREATE UNIQUE INDEX uq_task_course_seq
    ON task (course_id, seq) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX uq_task_org_code
    ON task (COALESCE(org_id, 0), code) WHERE deleted_at IS NULL;
CREATE INDEX idx_task_chapter ON task (chapter_id, seq);
```

> **★ 题库章节号 ⇔ 任务 的映射（关键约定）**
>
> 存量题库的「所属章节」列是 `X.Y` 形式（如 `1.1`），含义是 **`X` = 章号，`Y` = 任务号**，**不是**「章.节」。因此：
>
> | 题库章节号 | 含义 | 映射 |
> | --- | --- | --- |
> | `1.1` | 第1章 任务一 | `chapter.seq=1` 且 `task.seq=1` |
> | `5.4` | 第5章 任务四 | `chapter.seq=5` 且 `task.seq=4` |
>
> **导入脚本必须做这条映射校验**：若 `(chapter.seq, task.seq)` 组合不存在，则该题**不得静默入库**，必须进异常清单人工处理。（实际数据中出现过 `4.3` 无对应任务的情况，见 [15-职教适配与存量数据迁移规格.md](15-职教适配与存量数据迁移规格.md)。）

### 4.4 `knowledge_edge` — 知识点关系

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| from_node_id | bigint | NOT NULL REFERENCES knowledge_node(id) | |
| to_node_id | bigint | NOT NULL REFERENCES knowledge_node(id) | |
| relation | varchar(20) | NOT NULL | PREREQUISITE / RELATED / CONTAINS |
| weight | numeric(3,2) | NOT NULL DEFAULT 1.0 | |
| created_at / updated_at | | | |

```sql
CREATE UNIQUE INDEX uq_knowledge_edge ON knowledge_edge (from_node_id, to_node_id, relation);
CREATE INDEX idx_knowledge_edge_to ON knowledge_edge (to_node_id, relation);
-- CHECK: from_node_id <> to_node_id
```

**约束**：`PREREQUISITE` 关系必须无环。用应用层校验 + 每日定时检测任务（发现环则告警，不自动修复）。

### 4.5 `chapter` — 章（含「篇」属性）

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | | null = 平台模板 |
| **course_id** | bigint | NOT NULL REFERENCES course(id) | **★ 所属课程** |
| subject | varchar(20) | NOT NULL | 课程编码（兼容字段） |
| grade_band | varchar(20) | NOT NULL | |
| **seq** | smallint | NOT NULL | **★ 章号（对应题库 `X.Y` 的 X）** |
| name | varchar(200) | NOT NULL | 第1章 金属材料与热处理 |
| **part_name** | varchar(100) | | **★ 「篇」作为属性，不独立成层**：第一篇：机械工程材料 |
| **part_seq** | smallint | | 篇号（1/2/3），用于分组显示 |
| code | varchar(80) | | `MJJC-C1` |
| path | varchar(500) | NOT NULL | |
| sort_order | int | NOT NULL DEFAULT 0 | |
| textbook | varchar(100) | | 教材名 |
| volume | varchar(50) | | |
| is_template | boolean | NOT NULL DEFAULT false | |
| status | varchar(20) | NOT NULL DEFAULT 'ACTIVE' | |
| created_at / updated_at / deleted_at | | | |

```sql
CREATE UNIQUE INDEX uq_chapter_course_seq
    ON chapter (course_id, seq) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX uq_chapter_org_code
    ON chapter (COALESCE(org_id, 0), code) WHERE deleted_at IS NULL AND code IS NOT NULL;
CREATE INDEX idx_chapter_tree ON chapter (org_id, course_id, part_seq, sort_order);
```

> **「篇」的处理方式（已确认）**：`篇` **不独立成层**，而是作为 `chapter` 的两个属性列（`part_seq` 篇号 + `part_name` 篇名）存在。理由：篇只起分组显示作用（共 3 篇），建成独立实体会让树深一层、增加 join 与维护成本，而实际业务上没有"对篇整体操作"的需求。
>
> 树形展示时按 `part_seq` 分组渲染即可，**知识点树仍是四层：课程 → 章 → 任务 → 知识点**。
>
> **`parent_id` 字段废弃**：原设计用 `parent_id` 表达「章 → 节」两级，但实际数据里「节」的角色由 `task` 承担。`parent_id` 保留但不再使用，新数据一律留空。

### 4.7 `task_knowledge` — 任务与知识点映射（★ v1.1 新增，多对多）

**为什么必须有这张表**：知识点按名称去重后，**一个知识点可能被多个任务关联**（实测 106 行中 64 处是同名跨任务复用）。而 `knowledge_node.task_id` 是单值外键，表达不了多任务关系。

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| **task_id** | bigint | NOT NULL REFERENCES task(id) ON DELETE CASCADE | |
| **knowledge_node_id** | bigint | NOT NULL REFERENCES knowledge_node(id) | |
| **seq_in_task** | smallint | | **该知识点在此任务下的教材原始序号**（如「1.」「2.」），用于与纸质教材对照 |
| is_primary | boolean | NOT NULL DEFAULT false | 该任务是此知识点的主归属 |
| created_at | timestamptz | NOT NULL DEFAULT now() | |

```sql
CREATE UNIQUE INDEX uq_task_knowledge ON task_knowledge (task_id, knowledge_node_id);
CREATE INDEX idx_task_knowledge_node ON task_knowledge (knowledge_node_id);
CREATE UNIQUE INDEX uq_task_knowledge_primary
    ON task_knowledge (knowledge_node_id) WHERE is_primary = true;
```

> **使用约定**：
> - `knowledge_node.task_id` = **主任务**（首次出现位置），用于展示与默认查询
> - `task_knowledge` = **完整的多任务关系**，**组卷按知识点抽题与学情聚合时必须用它**，不能只看 `task_id`
> - 例：知识点「2. 基本特性」同时挂在第2章任务一（平面连杆机构）与第5章任务一（带传动）下——学情分析要把两个任务下该知识点的作答合并计算

### 4.8 `chapter_knowledge` — 章与知识点映射（多对多，跨任务合并时使用）

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| chapter_id | bigint | NOT NULL REFERENCES chapter(id) | |
| knowledge_node_id | bigint | NOT NULL REFERENCES knowledge_node(id) | |
| is_primary | boolean | NOT NULL DEFAULT false | 该章节主覆盖的知识点 |
| created_at | timestamptz | | |

```sql
CREATE UNIQUE INDEX uq_chapter_knowledge ON chapter_knowledge (chapter_id, knowledge_node_id);
CREATE INDEX idx_chapter_knowledge_node ON chapter_knowledge (knowledge_node_id);
```

### 4.9 `knowledge_entry` — 知识库条目（AI 生题的依据来源）

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| subject | varchar(20) | NOT NULL | |
| grade_band | varchar(20) | NOT NULL | |
| chapter_id | bigint | REFERENCES chapter(id) | **可空**（允许只挂知识点） |
| **entry_type** | varchar(20) | NOT NULL | CONCEPT / THEOREM / METHOD / EXAMPLE / COMMON_ERROR / SYLLABUS / EXPERIMENT / REFERENCE |
| title | varchar(300) | NOT NULL | |
| content_html | text | NOT NULL | 富文本（已净化） |
| content_text | text | NOT NULL | 纯文本（检索/向量化/注入 AI） |
| source_type | varchar(20) | NOT NULL DEFAULT 'TEACHER_INPUT' | TEXTBOOK / TEACHER_INPUT / UPLOADED_DOC / EXTERNAL / PLATFORM |
| source_ref | jsonb | NOT NULL DEFAULT '{}' | {textbook, volume, chapter, page, url, note} |
| attachment_keys | jsonb | NOT NULL DEFAULT '[]' | asset id 列表 |
| **embed_vector** | vector(1024) | | 语义检索 |
| usage_count | int | NOT NULL DEFAULT 0 | 被 AI 引用次数 |
| accept_rate | numeric(4,3) | | 引用该条目的生成结果采纳率 |
| status | varchar(20) | NOT NULL DEFAULT 'ACTIVE' | DRAFT / ACTIVE / ARCHIVED |
| verified_by | bigint | REFERENCES user(id) | **审核过的条目检索权重更高** |
| verified_at | timestamptz | | |
| created_by | bigint | REFERENCES user(id) | |
| created_at / updated_at / deleted_at | | | |

```sql
CREATE INDEX idx_kb_org_scope ON knowledge_entry (org_id, subject, grade_band, entry_type, status)
    WHERE deleted_at IS NULL;
CREATE INDEX idx_kb_chapter ON knowledge_entry (chapter_id, entry_type) WHERE deleted_at IS NULL;
CREATE INDEX idx_kb_content_trgm ON knowledge_entry USING gin (content_text gin_trgm_ops);
CREATE INDEX idx_kb_embed ON knowledge_entry USING ivfflat (embed_vector vector_cosine_ops) WITH (lists = 100);
```

### 4.10 `knowledge_entry_node` — 条目与知识点映射

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| entry_id | bigint | NOT NULL REFERENCES knowledge_entry(id) ON DELETE CASCADE | |
| knowledge_node_id | bigint | NOT NULL REFERENCES knowledge_node(id) | |
| weight | numeric(3,2) | NOT NULL DEFAULT 1.0 | 相关性权重（检索排序用） |
| source | varchar(20) | NOT NULL DEFAULT 'TEACHER' | AI / TEACHER |
| created_at | | | |

```sql
CREATE UNIQUE INDEX uq_kb_entry_node ON knowledge_entry_node (entry_id, knowledge_node_id);
CREATE INDEX idx_kb_entry_node_node ON knowledge_entry_node (knowledge_node_id, weight DESC);
```

---

## 五、题库

### 5.1 `question` — 题目

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| uuid | uuid | NOT NULL DEFAULT gen_random_uuid() UNIQUE | 对外标识 |
| org_id | bigint | NOT NULL | |
| subject | varchar(20) | NOT NULL | |
| grade_band | varchar(20) | NOT NULL | |
| **type** | varchar(30) | NOT NULL | SINGLE_CHOICE / MULTI_CHOICE / TRUE_FALSE / FILL_BLANK / SHORT_ANSWER / CALCULATION / PROOF / ESSAY |
| stem_html | text | NOT NULL | 富文本题干（已净化） |
| stem_text | text | NOT NULL | 纯文本题干 |
| stem_hash | varchar(64) | NOT NULL | 归一化后 MD5（精确去重） |
| simhash | bigint | | 近似去重 |
| **has_figure** | boolean | NOT NULL DEFAULT false | 是否有配图 |
| **figure_count** | smallint | NOT NULL DEFAULT 0 | 配图数量 |
| answer | jsonb | NOT NULL DEFAULT '{}' | 标准答案（结构随题型） |
| answer_text | text | | 答案纯文本 |
| explanation_html | text | | 解析 |
| explanation_text | text | | |
| difficulty_manual | smallint | | 1–5，教师标定 |
| difficulty_ai | smallint | | AI 建议值 |
| difficulty_stat | numeric(4,2) | | 统计难度 |
| **difficulty_effective** | numeric(4,2) | NOT NULL DEFAULT 3.00 | 组卷实际使用 |
| **difficulty_source** | varchar(20) | NOT NULL DEFAULT 'MANUAL' | MANUAL / STAT / MANUAL_STAT_BLEND / MANUAL_OVERRIDE |
| answer_count | int | NOT NULL DEFAULT 0 | 被作答次数（已批改） |
| correct_rate | numeric(5,4) | | 实测得分率 |
| **source_type** | varchar(30) | NOT NULL | ORIGINAL_HUMAN / AI_GENERATED / AI_REWRITTEN / EXTRACTED |
| source_ref | jsonb | NOT NULL DEFAULT '{}' | {ingest_batch_id, page, bbox} |
| **chapter_id** | bigint | REFERENCES chapter(id) | 所属章 |
| **task_id** | bigint | REFERENCES task(id) | **★ 所属任务（存量题库的标签粒度就是这一级，如 `1.1`）** |
| **course_id** | bigint | REFERENCES course(id) | **★ 所属课程（组卷候选池的隔离边界）** |
| ai_meta | jsonb | NOT NULL DEFAULT '{}' | {model, prompt_version, kb_entry_ids, generated_at} |
| **kb_citations** | jsonb | NOT NULL DEFAULT '[]' | AI 生成时引用的知识条目（引用溯源） |
| status | varchar(20) | NOT NULL DEFAULT 'DRAFT' | DRAFT / PENDING_REVIEW / APPROVED / ARCHIVED / REJECTED |
| review_by | bigint | REFERENCES user(id) | |
| review_at | timestamptz | | |
| copyright | varchar(50) | | 版权标注 |
| tags | jsonb | NOT NULL DEFAULT '[]' | 自定义标签 |
| embed_vector | vector(1024) | | 相似题检索 |
| created_by | bigint | REFERENCES user(id) | |
| created_at / updated_at / deleted_at | | | |

```sql
CREATE INDEX idx_question_org_scope ON question (org_id, subject, status, type) WHERE deleted_at IS NULL;
CREATE INDEX idx_question_difficulty ON question (org_id, subject, difficulty_effective) WHERE deleted_at IS NULL;
CREATE INDEX idx_question_chapter ON question (chapter_id, status) WHERE deleted_at IS NULL;
CREATE INDEX idx_question_task ON question (task_id, status) WHERE deleted_at IS NULL;
CREATE INDEX idx_question_course ON question (course_id, status, type) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX uq_question_stem_hash ON question (org_id, stem_hash) WHERE deleted_at IS NULL;
CREATE INDEX idx_question_stem_trgm ON question USING gin (stem_text gin_trgm_ops);
CREATE INDEX idx_question_embed ON question USING ivfflat (embed_vector vector_cosine_ops) WITH (lists = 200);
CREATE INDEX idx_question_figure ON question (org_id, has_figure) WHERE deleted_at IS NULL;
```

**答案 JSON 结构**（按题型）：

```json
// SINGLE_CHOICE / MULTI_CHOICE / TRUE_FALSE
{"value": "B"}
{"value": ["A", "C"]}
{"value": true}

// FILL_BLANK（一空多解）
{"blanks": [
  {"index": 1, "accepted": ["1/2", "0.5"], "synonyms": ["二分之一"]},
  {"index": 2, "accepted": ["增大"]}
]}

// CALCULATION（步骤分，用于主观题录入参考）
{"final": "25 m/s", "steps": [{"desc": "列牛顿第二定律", "score": 3}]}
```

### 5.2 `question_option` — 选项

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| question_id | bigint | NOT NULL REFERENCES question(id) ON DELETE CASCADE | |
| label | varchar(5) | NOT NULL | A/B/C/D… |
| content_html | text | NOT NULL | |
| content_text | text | NOT NULL | |
| sort_order | int | NOT NULL | |
| is_correct | boolean | NOT NULL DEFAULT false | |
| **image_asset_id** | bigint | REFERENCES asset(id) | 选项配图 |
| **error_reason** | text | | 该选项对应的典型错误原因 |
| error_stat | jsonb | NOT NULL DEFAULT '{}' | {count, rate} 被选统计 |
| created_at / updated_at | | | |

```sql
CREATE UNIQUE INDEX uq_question_option ON question_option (question_id, label);
CREATE INDEX idx_question_option_question ON question_option (question_id, sort_order);
```

> **★ 判断题（`TRUE_FALSE`）必须有 2 个选项行：`A` = 正确 / `B` = 错误**
>
> 2026-10-17 修的一个线上缺陷：库里 **618 / 619 道判断题没有任何选项行**
> （答案只存布尔值 `{"value": true}`，导入路径漏了这一步）。后果：
>
> 1. 学生端线上作答**没有可点的选项**（快照 `options` 是空数组）；
> 2. 即使点了也会失败：乱序基准标签是空元组 →
>    「卷面选项 A 超出第 N 题的卷面选项数 0」；
> 3. 试卷/答题卡上判断题的两个框拿不到基准标签。
>
> 现在三处都有兜底（`QuestionService.create_question` 自动补、
> `PaperService._build_snapshot` 补快照、`ordering_for_row` 补基准标签），
> 存量数据由 `scripts/fix_true_false_options.py` 修。
>
> ★ 卡面/H5 **印的是 `T`/`F`**（2026-10-16 裁决），但库里的标签始终是
> `A`/`B`；作答回传用的是**卷面位置字母**（`position_label`），
> 不是印刷标签 —— 混用会让 `label_index("T") = 19` 越界。
>
> **★ 判断题不参与选项乱序**（2026-10-17 需求方口径：「选项乱序只针对选择题」）：
> 两个框是「对 / 错」，**位置本身有意义** —— 打乱后界面会出现
> 「`T` 位写着『错误』」，而判分按位置还原会把学生点的 `T` **整题判反**。
> 因此 `_collect_paper_items` 只把**选择题**交给乱序，
> `ordering_for_row` 在读侧也会剔除非选择题的乱序条目；
> 存量脏数据由 `scripts/fix_option_order_choice_only.py` 清理。

### 5.3 `question_image` — ★ 题目配图（含 v2.1 逐图确认字段）

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| question_id | bigint | REFERENCES question(id) ON DELETE CASCADE | |
| paper_item_id | bigint | REFERENCES paper_item(id) | 材料题多题共用时用 |
| **attach_point** | varchar(20) | NOT NULL DEFAULT 'STEM' | STEM / OPTION_A~Z / MATERIAL |
| asset_id | bigint | NOT NULL REFERENCES asset(id) | 图片资源 |
| seq_in_question | smallint | NOT NULL DEFAULT 1 | |
| **src_path** | varchar(20) | NOT NULL | PDF_OBJECT / WORD_INLINE / VISION_EXTRACT / MANUAL |
| source_ref | jsonb | NOT NULL DEFAULT '{}' | {page, bbox, anchor_id} |
| caption | varchar(100) | | 题注（图1） |
| **figure_category** | varchar(30) | | GEOMETRY / FUNCTION_GRAPH / CIRCUIT / FORCE_DIAGRAM / APPARATUS / CHEM_STRUCTURE / MAP / CHART / PHOTO / TABLE / OTHER |
| description | text | | AI 生成的图片描述 |
| **text_inside** | jsonb | NOT NULL DEFAULT '[]' | 图内文字（R1、6V、坐标轴标注） |
| quality_score | numeric(4,3) | | |
| **quality_flags** | jsonb | NOT NULL DEFAULT '[]' | LOW_RESOLUTION / LOW_CONTRAST / BLURRY / TILTED / HAS_BINDING_LINE / **SUSPECT_CROP** / MULTI_FIGURE_MERGED / TEXT_OCR_FAILED |
| binding_confidence | numeric(4,3) | | 图文归属置信度 |
| **binding_method** | varchar(20) | | SEQUENCE / REFERENCE_TEXT / CAPTION / OPTION_POS / CROSS_PAGE / MANUAL |
| **review_status** | varchar(20) | NOT NULL DEFAULT 'PENDING' | **PENDING / RECROPPED / REPLACED / CONFIRMED / DELETED** |
| **confirm_method** | varchar(20) | | **CLICK / RECROP / REPLACE / MANUAL_CAPTURE / BATCH** |
| **confirm_evidence** | jsonb | NOT NULL DEFAULT '{}' | **{criteria, viewed_seconds, count, suspicious_speed}** |
| **confirmed_by** | bigint | REFERENCES user(id) | |
| **confirmed_at** | timestamptz | | |
| **crop_history** | jsonb | NOT NULL DEFAULT '[]' | **[{bbox, at, by, method}]** |
| **origin_image_id** | bigint | REFERENCES question_image(id) | **换图/借用来源** |
| **ingest_batch_id** | bigint | REFERENCES ingest_batch(id) | **所属导入批次（完成门禁需要）** |
| created_at / updated_at | | | |

> ⚠ **`layout_flags` 与 `display_scale` 不在此表**（v2.1-补修正）。它们属于「这张图在这份卷子里排成什么样」，**因卷而异**；同一张配图可能被多份试卷复用（7.4.8.4「借用共享同一 asset」），挂在本表会互相覆盖。正确位置：`paper_item.layout_flags` 与 `paper_item.figure_display_scale`。判据：「这张图是什么样」→ 本表；「这张图在这份卷里排成什么样」→ `paper_item`。详见 [14-缺陷登记册与统一指标口径.md](14-缺陷登记册与统一指标口径.md) D-03。

```sql
CREATE INDEX idx_qimage_question ON question_image (question_id, seq_in_question);
CREATE INDEX idx_qimage_review ON question_image (review_status) WHERE review_status = 'PENDING';
CREATE INDEX idx_qimage_batch ON question_image (ingest_batch_id, review_status);
CREATE INDEX idx_qimage_paper_item ON question_image (paper_item_id, review_status);
-- ★ 试卷定稿门禁：按 question_id 反查配图（不能用 paper_item_id，见 14 文档 D-02）
CREATE INDEX idx_qimage_question_pending ON question_image (question_id)
    WHERE review_status = 'PENDING';
-- ★ 排版标记查询走 paper_item
CREATE INDEX idx_paper_item_layout_flags ON paper_item USING gin (layout_flags);
```

> **`review_status` 语义变更（v2.1）**：原 `AUTO/NEEDS_REVIEW` 一律归为 `PENDING`，因为现在**每张图都必须人工确认**，不存在"自动通过"。

### 5.4 `question_knowledge` — 题目-知识点关联

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| question_id | bigint | NOT NULL REFERENCES question(id) ON DELETE CASCADE | |
| knowledge_node_id | bigint | NOT NULL REFERENCES knowledge_node(id) | |
| **weight** | numeric(3,2) | NOT NULL DEFAULT 1.0 | 主知识点 1.0，次要 0.3–0.5 |
| is_primary | boolean | NOT NULL DEFAULT false | |
| source | varchar(20) | NOT NULL DEFAULT 'TEACHER' | AI / TEACHER / IMPORT |
| created_at | | | |

```sql
CREATE UNIQUE INDEX uq_question_knowledge ON question_knowledge (question_id, knowledge_node_id);
CREATE INDEX idx_qk_node ON question_knowledge (knowledge_node_id, question_id);  -- 组卷按知识点抽题
CREATE UNIQUE INDEX uq_question_primary_node ON question_knowledge (question_id)
    WHERE is_primary = true;
```

### 5.5 `question_relation` — 题目关系

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| from_question_id | bigint | NOT NULL REFERENCES question(id) | |
| to_question_id | bigint | NOT NULL REFERENCES question(id) | |
| relation | varchar(20) | NOT NULL | VARIANT / SAME_SOURCE / SIMILAR / PREREQUISITE_QUESTION |
| similarity | numeric(4,3) | | |
| created_by | varchar(20) | NOT NULL DEFAULT 'AI' | AI / TEACHER |
| created_at | | | |

```sql
CREATE UNIQUE INDEX uq_question_relation ON question_relation (from_question_id, to_question_id, relation);
CREATE INDEX idx_qrel_from ON question_relation (from_question_id, relation);
CREATE INDEX idx_qrel_to ON question_relation (to_question_id, relation);
```

---

## 六、资源（asset）

### 6.1 `asset` — 统一资源表

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| **kind** | varchar(30) | NOT NULL | QUESTION_FIGURE / SCAN_RAW / SCAN_RECTIFIED / PAPER_PDF / SHEET_PDF / UPLOAD_SOURCE / REPORT_PDF / KNOWLEDGE_ATTACH / EXPORT_ZIP / EXPORT_PART |
| oss_key | varchar(500) | NOT NULL UNIQUE | 对象存储 key |
| mime | varchar(100) | NOT NULL | |
| byte_size | bigint | NOT NULL | |
| width / height | int | | 图片尺寸 |
| **dpi** | smallint | | 印刷用图需要 |
| **thumb_keys** | jsonb | NOT NULL DEFAULT '{}' | {sm, md, lg} 多档缩略图 |
| **phash** | bigint | | 图片感知哈希（去重） |
| **ref_count** | int | NOT NULL DEFAULT 0 | **引用计数（决定何时可清理）** |
| **retain_until** | timestamptz | | 生命周期策略 |
| created_at | timestamptz | NOT NULL DEFAULT now() | |
| deleted_at | timestamptz | | |

```sql
CREATE INDEX idx_asset_cleanup ON asset (kind, retain_until) WHERE deleted_at IS NULL;
CREATE INDEX idx_asset_phash ON asset (phash) WHERE phash IS NOT NULL;
CREATE INDEX idx_asset_org_kind ON asset (org_id, kind);
```

**铁律**：删除 `asset` 前必须 `ref_count = 0`。图片可能被多道题、多份试卷快照、多个练习引用。

---

## 七、组卷

### 7.1 `paper` — 试卷

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| uuid | uuid | NOT NULL UNIQUE | |
| org_id | bigint | NOT NULL | |
| subject | varchar(20) | NOT NULL | |
| grade_band | varchar(20) | NOT NULL | |
| title | varchar(300) | NOT NULL | |
| **purpose** | varchar(20) | NOT NULL | QUIZ / EXAM / HOMEWORK / PRACTICE / MAKEUP |
| total_score | numeric(6,2) | NOT NULL DEFAULT 0 | |
| item_count | int | NOT NULL DEFAULT 0 | |
| difficulty_expect | numeric(4,3) | | 期望得分率 |
| **compose_mode** | varchar(20) | NOT NULL DEFAULT 'MANUAL' | MANUAL / RULE / DIAGNOSIS |
| compose_config | jsonb | NOT NULL DEFAULT '{}' | 抽题规则快照 |
| **random_seed** | bigint | | 可复现 |
| **compose_rationale** | jsonb | NOT NULL DEFAULT '{}' | 组卷说明（学情组卷「针对哪些薄弱点」） |
| knowledge_coverage | jsonb | NOT NULL DEFAULT '{}' | |
| layout_config | jsonb | NOT NULL DEFAULT '{}' | 排版参数 |
| is_personalized | boolean | NOT NULL DEFAULT false | |
| owner_student_id | bigint | REFERENCES user(id) | is_personalized 时 |
| **source_paper_id** | bigint | REFERENCES paper(id) | **个性化练习的结构来源** |
| paper_group_id | bigint | | AB/补考卷分组 |
| variant_label | varchar(10) | | A / B / MAKEUP |
| status | varchar(20) | NOT NULL DEFAULT 'DRAFT' | DRAFT / FINALIZED / ARCHIVED |
| created_by / created_at / updated_at / deleted_at | | | |

```sql
CREATE INDEX idx_paper_org_status ON paper (org_id, subject, status) WHERE deleted_at IS NULL;
CREATE INDEX idx_paper_group ON paper (paper_group_id) WHERE paper_group_id IS NOT NULL;
CREATE INDEX idx_paper_owner_student ON paper (owner_student_id) WHERE is_personalized = true;
```

**`compose_config` 结构**：

```json
{
  "sections": [
    {"type": "SINGLE_CHOICE", "count": 10, "score_each": 4, "difficulty_range": [2, 3], "title": "一、选择题"},
    {"type": "TRUE_FALSE", "count": 5, "score_each": 2, "difficulty_range": [1, 3], "title": "二、判断题"},
    {"type": "FILL_BLANK", "count": 4, "score_each": 4, "difficulty_range": [2, 4], "title": "三、填空题"},
    {"type": "CALCULATION", "count": 4, "score_each": 10, "difficulty_range": [3, 5], "title": "四、解答题"}
  ],
  "knowledge_nodes": [1024, 1025, 1030],
  "weak_quota": 0.65,
  "exclude_recent_days": 90
}
```

### 7.2 `paper_item` — 卷面题

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| paper_id | bigint | NOT NULL REFERENCES paper(id) ON DELETE CASCADE | |
| question_id | bigint | NOT NULL REFERENCES question(id) | |
| section_title | varchar(200) | | 大题标题 |
| **seq** | int | NOT NULL | 卷面题号 |
| score | numeric(5,2) | NOT NULL | 配分 |
| **snapshot_json** | jsonb | NOT NULL | **题干/选项/答案/解析/配图的快照** |
| **layout_flags** | jsonb | NOT NULL DEFAULT '[]' | **★ 本卷面的排版问题标记（SPLIT_BY_PAGE / TOO_WIDE / TOO_SMALL / OVERLAP / DUP_IN_PAPER）——归属 paper_item 而非 question_image，因为同一张图在不同试卷中排版结果不同** |
| **figure_display_scale** | jsonb | NOT NULL DEFAULT '{}' | **★ 本卷面各配图的显示比例 `{question_image_id: 0.8}`（关口二调整）** |
| option_order | jsonb | NOT NULL DEFAULT '[]' | 该卷面选项顺序 |
| group_key | varchar(50) | | 材料题分组标识 |
| created_at / updated_at | | | |

```sql
CREATE UNIQUE INDEX uq_paper_item_seq ON paper_item (paper_id, seq);
CREATE INDEX idx_paper_item_question ON paper_item (question_id);
```

**快照铁律**：`snapshot_json` 必须包含题干、选项、答案、解析、**配图 asset_id 列表**。题库改题不影响已发布试卷。

### 7.3 `paper_equivalence` — AB 卷等值校验记录

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| paper_group_id | bigint | NOT NULL | |
| paper_a_id / paper_b_id | bigint | NOT NULL | |
| expect_rate_a / expect_rate_b | numeric(4,3) | NOT NULL | 期望得分率 |
| diff | numeric(4,3) | NOT NULL | 差值 |
| knowledge_jaccard | numeric(4,3) | | 知识点相似度 |
| passed | boolean | NOT NULL | |
| detail | jsonb | NOT NULL DEFAULT '{}' | 逐题难度对比 |
| created_at | | | |

```sql
CREATE INDEX idx_paper_equiv_group ON paper_equivalence (paper_group_id);
```

---

## 八、答题卡

### 8.1 `sheet_style` — 答题卡样式

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | | **null = 系统预设** |
| name | varchar(100) | NOT NULL | 「标准A4·8位学号」 |
| is_preset | boolean | NOT NULL DEFAULT false | |
| page_size | varchar(10) | NOT NULL DEFAULT 'A4' | A4 / A3 / B4 / 16K |
| sides | varchar(10) | NOT NULL DEFAULT 'SINGLE' | SINGLE / DOUBLE |
| layout_mode | varchar(20) | NOT NULL DEFAULT '1_PER_ROW' | 1_PER_ROW / 2_PER_ROW |
| columns | smallint | NOT NULL DEFAULT 1 | 1 / 2 |
| **student_no_digits** | smallint | NOT NULL DEFAULT 8 | **1–12；0 = 无学号区** |
| **student_no_layout** | varchar(20) | NOT NULL DEFAULT 'SINGLE_ROW' | **SINGLE_ROW / MULTI_ROW** |
| **student_no_per_row** | smallint | NOT NULL DEFAULT 6 | 多行时每行位数 |
| **student_no_group_size** | smallint | NOT NULL DEFAULT 4 | 每几位加分隔线 |
| **student_no_label_side** | varchar(10) | NOT NULL DEFAULT 'LEFT' | LEFT / NONE |
| student_no_checksum | boolean | NOT NULL DEFAULT false | |
| subjective_zone | varchar(20) | NOT NULL DEFAULT 'BY_SCORE' | NONE / BY_SCORE / CUSTOM |
| subjective_zone_config | jsonb | NOT NULL DEFAULT '{}' | 每题留框高度等 |
| sealing_line | jsonb | NOT NULL DEFAULT '{}' | 密封线配置 |
| header_config | jsonb | NOT NULL DEFAULT '{}' | 页眉、标题、注意事项文案 |
| margin_config | jsonb | NOT NULL DEFAULT '{}' | 页边距 |
| **practice_serial_zone** | jsonb | NOT NULL DEFAULT '{}' | **个性化练习卷号区（阶段二）** |
| created_by / created_at / updated_at / deleted_at | | | |

**约束**：`student_no_digits` 必须 `BETWEEN 0 AND 12`；`student_no_layout` 与 `student_no_per_row` 组合必须能通过排版校验（见 08 文档）。

### 8.2 `sheet_template` — 答题卡模板实例

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| **style_id** | bigint | NOT NULL REFERENCES sheet_style(id) | |
| name | varchar(200) | NOT NULL | 模板名（模板库检索用） |
| **version** | int | NOT NULL | **只增不改** |
| page_count | smallint | NOT NULL DEFAULT 1 | |
| **objective_item_count** | smallint | NOT NULL | **客观题数（绑定校验用）** |
| **question_types** | jsonb | NOT NULL DEFAULT '[]' | 支持的题型结构（绑定校验用） |
| **layout_json** | jsonb | NOT NULL | **锚点 + 学号区 + 题涂点的相对坐标** |
| threshold_config | jsonb | NOT NULL DEFAULT '{}' | 该版本的判定阈值快照 |
| asset_id | bigint | REFERENCES asset(id) | 生成的 PDF |
| status | varchar(20) | NOT NULL DEFAULT 'ACTIVE' | ACTIVE / DEPRECATED |
| created_by / created_at / deleted_at | | | |

```sql
CREATE UNIQUE INDEX uq_sheet_template ON sheet_template (org_id, name, version);
CREATE INDEX idx_sheet_template_style ON sheet_template (style_id, version DESC);
```

**`layout_json` 结构**（坐标一律相对比例 0–1）：

```json
{
  "version": 3,
  "page_size": "A4",
  "dpi_reference": 300,
  "anchors": [
    {"id": "TL", "x": 0.0635, "y": 0.0423},
    {"id": "TR", "x": 0.9365, "y": 0.0423},
    {"id": "BL", "x": 0.0635, "y": 0.9577},
    {"id": "BR", "x": 0.9365, "y": 0.9577}
  ],
  "student_no": {
    "enabled": true,
    "digits": 8,
    "layout": "SINGLE_ROW",
    "per_row": 8,
    "has_checksum": false,
    "label_width": 0.02,
    "origin": {"x": 0.08, "y": 0.10},
    "col_pitch": 0.045,
    "row_pitch": 0.011,
    "radius": 0.0085,
    "positions": [
      {"pos": 0, "row": 0, "col": 0,
       "bubbles": [{"d": 0, "cx": 0.0821, "cy": 0.1050, "r": 0.0085}]}
    ]
  },
  "objective": {
    "layout_mode": "1_PER_ROW",
    "items": [
      {"seq": 1, "bubbles": [{"label": "A", "cx": 0.180, "cy": 0.220, "r": 0.0075}]}
    ]
  },
  "subjective_zones": [
    {"seq": 21, "x": 0.08, "y": 0.70, "w": 0.84, "h": 0.12,
     "score_box": {"x": 0.90, "y": 0.70, "w": 0.05, "h": 0.02}}
  ]
}
```

> **同时存 `layout/per_row/digits` 与展开后的 `positions`**：前者供前端校验与显示，后者供识别端直接读取，避免识别端重复实现排布算法导致不一致。

### 8.3 `paper_sheet_binding` — 试卷与答题卡绑定

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| paper_id | bigint | NOT NULL REFERENCES paper(id) ON DELETE CASCADE | |
| sheet_template_id | bigint | NOT NULL REFERENCES sheet_template(id) | |
| is_primary | boolean | NOT NULL DEFAULT true | 打印默认用这张 |
| bound_by | bigint | REFERENCES user(id) | |
| bound_at | timestamptz | NOT NULL DEFAULT now() | |

```sql
CREATE UNIQUE INDEX uq_paper_sheet_binding ON paper_sheet_binding (paper_id, sheet_template_id);
CREATE UNIQUE INDEX uq_paper_sheet_primary ON paper_sheet_binding (paper_id) WHERE is_primary = true;
```

**绑定校验（应用层，见 08 文档 `binding.py`）**：

```
试卷客观题题量 == 模板 objective_item_count
试卷客观题题型结构 ⊆ 模板 question_types
试卷学号位数需求 <= 模板学号位数
```

不匹配返回 `SHEET.BIND_MISMATCH`，并给出具体差异。

---

## 九、发布任务与作答

### 9.1 `assignment` — 发布任务

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| uuid | uuid | NOT NULL UNIQUE | |
| org_id | bigint | NOT NULL | |
| paper_id | bigint | NOT NULL REFERENCES paper(id) | |
| classroom_id | bigint | NOT NULL REFERENCES classroom(id) | |
| title | varchar(300) | NOT NULL | |
| **channel** | varchar(20) | NOT NULL DEFAULT 'PAPER_OMR' | PAPER_OMR / ONLINE / BOTH |
| **sheet_template_id** | bigint | REFERENCES sheet_template(id) | PAPER_OMR/BOTH 时必填 |
| start_at / end_at | timestamptz | NOT NULL | |
| shuffle_questions / shuffle_options | boolean | NOT NULL DEFAULT false | |
| answer_visibility | varchar(20) | NOT NULL DEFAULT 'AFTER_GRADE' | AFTER_SUBMIT / AFTER_GRADE / NEVER |
| score_visibility | varchar(20) | NOT NULL DEFAULT 'NONE' | TOTAL / DETAIL / NONE |
| allow_late | boolean | NOT NULL DEFAULT false | |
| pass_ratio | numeric(4,3) | NOT NULL DEFAULT 0.600 | 及格线 |
| **auto_practice** | boolean | NOT NULL DEFAULT true | 批改后是否自动生成个性化练习 |
| **auto_grade_on_submit** | boolean | NOT NULL DEFAULT false | ★ **交卷自动批改**（2026-10-17 新增）：学生一交卷就自动批改**他这一份**（客观题 + 填空题）。默认 false —— 自动批改会立刻写成绩并触发学情/练习级联，应由老师显式勾选。可在作业详情页改（`PATCH /assignments/{id}/`） |
| publish_token | varchar(64) | NOT NULL UNIQUE | 学生扫码入口 |
| status | varchar(20) | NOT NULL DEFAULT 'DRAFT' | DRAFT / PUBLISHED / CLOSED / ARCHIVED |
| created_by / created_at / updated_at / published_at / deleted_at | | | |

```sql
CREATE INDEX idx_assignment_org_status ON assignment (org_id, status, end_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX idx_assignment_classroom ON assignment (classroom_id, status);
CREATE UNIQUE INDEX uq_assignment_token ON assignment (publish_token);
-- CHECK: channel IN ('PAPER_OMR','BOTH') 时 sheet_template_id IS NOT NULL
```

### 9.2 `assignment_student` — 任务-学生（**含乱序映射，本项目最高风险数据**）

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| assignment_id | bigint | NOT NULL REFERENCES assignment(id) ON DELETE CASCADE | |
| student_id | bigint | NOT NULL REFERENCES user(id) | |
| **question_order** | jsonb | NOT NULL DEFAULT '[]' | **该生题目顺序 `[paper_item_id, ...]`** |
| **option_order** | jsonb | NOT NULL DEFAULT '{}' | **该生每题选项顺序 `{"12": ["C","A","D","B"]}`** |
| status | varchar(20) | NOT NULL DEFAULT 'NOT_STARTED' | NOT_STARTED / IN_PROGRESS / SUBMITTED / SCANNED / ABSENT / PARTIAL |
| entered_at | timestamptz | | 扫码进入时间（教师看板用） |
| submitted_at | timestamptz | | |
| device_fingerprint | varchar(100) | | 冒名检测用 |
| session_token | varchar(64) | | 作答会话（Redis 主存，此处留痕） |
| created_at / updated_at | | | |

```sql
CREATE UNIQUE INDEX uq_assignment_student ON assignment_student (assignment_id, student_id);
CREATE INDEX idx_assignment_student_status ON assignment_student (assignment_id, status);
```

> **⚠ 这两列是全项目最容易出错、后果最严重的数据。**
> ① `question_order` 必须在**发布时确定并持久化**，禁止每次读取重新随机（否则扫描结果无法复现）。
> ② 一人一卷时「卡上第 N 题」→ `question_order[N-1]` → 实际 `paper_item`，错一位则全班串号。
> ③ 强制测试用例见 12 文档 `TC-RESPONSE-*`。

### 9.3 `response` — 作答记录

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| assignment_id | bigint | NOT NULL REFERENCES assignment(id) | |
| student_id | bigint | NOT NULL REFERENCES user(id) | |
| paper_item_id | bigint | NOT NULL REFERENCES paper_item(id) | |
| **source** | varchar(20) | NOT NULL | ONLINE / SCAN_PHONE / SCAN_UPLOAD / MANUAL |
| answer_raw | text | | 原始作答 |
| answer_normalized | text | | 归一化后（比对用） |
| answer_json | jsonb | NOT NULL DEFAULT '{}' | 结构化（多选/多空） |
| scan_confidence | numeric(4,3) | | 扫描识别置信度 |
| scan_card_id | bigint | REFERENCES scan_card(id) | |
| image_asset_id | bigint | REFERENCES asset(id) | 该题原图裁切 |
| **superseded_by** | bigint | REFERENCES response(id) | **被覆盖记录（不物理删除）** |
| answered_at | timestamptz | | |
| created_at / updated_at | | | |

```sql
CREATE INDEX idx_response_assignment_student ON response (assignment_id, student_id);
CREATE INDEX idx_response_paper_item ON response (paper_item_id);
CREATE INDEX idx_response_scan_card ON response (scan_card_id) WHERE scan_card_id IS NOT NULL;
-- 同一 (assignment, student, paper_item) 允许多条历史，但只有 superseded_by IS NULL 的是生效记录
CREATE UNIQUE INDEX uq_response_active ON response (assignment_id, student_id, paper_item_id)
    WHERE superseded_by IS NULL;
```

### 9.4 `student_total_score` — 成绩汇总（物化表）

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| assignment_id / student_id | bigint | NOT NULL | |
| total_score | numeric(6,2) | NOT NULL DEFAULT 0 | |
| objective_score / subjective_score | numeric(6,2) | NOT NULL DEFAULT 0 | |
| rank_in_class | int | | **只存不默认展示** |
| correct_count / wrong_count / blank_count | int | NOT NULL DEFAULT 0 | |
| graded_at | timestamptz | | |
| created_at / updated_at | | | |

```sql
CREATE UNIQUE INDEX uq_student_total ON student_total_score (assignment_id, student_id);
CREATE INDEX idx_student_total_classroom ON student_total_score (org_id, assignment_id, total_score DESC);
```

> 物化表：`score_result` 变化时重算。**不要每次查询实时聚合**，否则成绩单查询会变成 N 次聚合。

---

## 十、扫描与识别

### 10.1 `scan_batch` — 扫描批次

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| assignment_id | bigint | NOT NULL REFERENCES assignment(id) | |
| sheet_template_id | bigint | NOT NULL REFERENCES sheet_template(id) | |
| **capture_channel** | varchar(20) | NOT NULL | **PHONE_H5 / UPLOAD** |
| operator_id | bigint | NOT NULL REFERENCES user(id) | |
| total_cards / recognized_cards / failed_cards / review_cards | int | NOT NULL DEFAULT 0 | |
| status | varchar(20) | NOT NULL DEFAULT 'UPLOADING' | UPLOADING / PROCESSING / WAITING_REVIEW / COMPLETED / FAILED |
| **threshold_version** | varchar(20) | NOT NULL | 判定阈值版本 |
| **quality_report** | jsonb | NOT NULL DEFAULT '{}' | 失败原因分布等 |
| created_at / completed_at | | | |

```sql
CREATE INDEX idx_scan_batch_org ON scan_batch (org_id, status, created_at DESC);
CREATE INDEX idx_scan_batch_assignment ON scan_batch (assignment_id);
```

### 10.2 `scan_card` — 单张答题卡

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| scan_batch_id | bigint | NOT NULL REFERENCES scan_batch(id) ON DELETE CASCADE | |
| assignment_id | bigint | REFERENCES assignment(id) | **所属发布任务（冗余，便于按任务查卡与统计）** |
| sheet_template_id | bigint | NOT NULL | |
| raw_asset_id | bigint | NOT NULL REFERENCES asset(id) | 原图 |
| rectified_asset_id | bigint | REFERENCES asset(id) | 校正后图 |
| student_id | bigint | REFERENCES user(id) | 指定的学生 |
| **recognized_student_no** | varchar(50) | | **识别出的学号** |
| **student_no_confidence** | numeric(4,3) | | **学号识别置信度（阈值更严 0.98）** |
| **student_no_flags** | jsonb | NOT NULL DEFAULT '[]' | MULTI_MARK_POS{n} / CHECKSUM_FAILED / NOT_IN_ROSTER / DUPLICATE |
| **capture_channel** | varchar(20) | NOT NULL | PHONE_H5 / UPLOAD |
| status | varchar(20) | NOT NULL DEFAULT 'PENDING' | PENDING / AUTO_OK / NEEDS_REVIEW / REVIEWED / VOID / FAILED |
| overall_confidence | numeric(4,3) | | |
| **anchor_quality** | numeric(4,3) | | 锚点质量 |
| **anchor_deviation** | numeric(5,4) | | **校正后锚点偏差（关键防线指标）** |
| uncertain_items | jsonb | NOT NULL DEFAULT '[]' | 不确定题号列表 |
| fail_reason | varchar(200) | | |
| reviewed_by / reviewed_at | | | |
| created_at / updated_at | | | |

```sql
CREATE INDEX idx_scan_card_batch_status ON scan_card (scan_batch_id, status);
CREATE INDEX idx_scan_card_assignment ON scan_card (assignment_id, status);
CREATE INDEX idx_scan_card_student_no ON scan_card (recognized_student_no)
    WHERE recognized_student_no IS NOT NULL;
CREATE INDEX idx_scan_card_assignment_student ON scan_card (assignment_id, student_id);
```

**学号冲突检测查询**：

```sql
-- 同一批次内同一学号出现两次
SELECT recognized_student_no, count(*)
FROM scan_card
WHERE scan_batch_id = $1 AND recognized_student_no IS NOT NULL AND status <> 'VOID'
GROUP BY recognized_student_no HAVING count(*) > 1;
```

---

## 十一、评分

### 11.1 `score_result` — 评分结果

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| assignment_id / student_id / paper_item_id | bigint | NOT NULL | |
| response_id | bigint | REFERENCES response(id) | |
| score | numeric(5,2) | NOT NULL DEFAULT 0 | |
| max_score | numeric(5,2) | NOT NULL | |
| is_correct | boolean | | null = 部分对 |
| **method** | varchar(20) | NOT NULL | AUTO_EXACT / AUTO_NORMALIZED / AUTO_FUZZY / AUTO_SYNONYM / AI_EQUIV / MANUAL / MANUAL_AGGREGATE |
| confidence | numeric(4,3) | | 自动评分置信度 |
| **state** | varchar(30) | NOT NULL DEFAULT 'PENDING' | PENDING / AUTO_GRADED / NEEDS_REVIEW / MANUAL_ENTERED / TEACHER_CONFIRMED / TEACHER_MODIFIED / VOID |
| ai_call_log_id | bigint | REFERENCES ai_call_log(id) | 仅填空题 AI 等价判定 |
| **score_points** | jsonb | NOT NULL DEFAULT '[]' | **本期保留不使用（为未来主观题分步评分留余地）** |
| deduction_reasons | jsonb | NOT NULL DEFAULT '[]' | 扣分原因标签 |
| graded_by / graded_at | bigint / timestamptz | | |
| confirmed_by / confirmed_at | bigint / timestamptz | | |
| created_at / updated_at | | | |

```sql
CREATE UNIQUE INDEX uq_score_result ON score_result (assignment_id, student_id, paper_item_id);
CREATE INDEX idx_score_result_state ON score_result (org_id, state) WHERE state IN ('NEEDS_REVIEW');
CREATE INDEX idx_score_result_assignment ON score_result (assignment_id);
-- CHECK (score >= 0 AND score <= max_score)
```

---

## 十二、学情

### 12.1 `knowledge_mastery` — 掌握度（当前值）

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| student_id | bigint | NOT NULL REFERENCES user(id) | |
| knowledge_node_id | bigint | NOT NULL REFERENCES knowledge_node(id) | |
| **mastery** | numeric(4,3) | NOT NULL DEFAULT 0 | 0–1 |
| **confidence** | numeric(4,3) | NOT NULL DEFAULT 0 | 0–1 |
| **effective_samples** | numeric(6,2) | NOT NULL DEFAULT 0 | 衰减后等效样本量 |
| answered_count | int | NOT NULL DEFAULT 0 | |
| correct_rate | numeric(5,4) | | |
| last_answered_at | timestamptz | | |
| calculated_at | timestamptz | NOT NULL DEFAULT now() | |
| **calc_version** | varchar(20) | NOT NULL DEFAULT 'v1' | 算法版本（换算法后可对比） |

```sql
CREATE UNIQUE INDEX uq_knowledge_mastery ON knowledge_mastery (student_id, knowledge_node_id);
CREATE INDEX idx_mastery_org_node ON knowledge_mastery (org_id, knowledge_node_id);
CREATE INDEX idx_mastery_weak ON knowledge_mastery (org_id, knowledge_node_id, mastery)
    WHERE confidence >= 0.5;
```

### 12.2 `diagnosis_snapshot` — 诊断快照

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| student_id | bigint | NOT NULL | |
| assignment_id | bigint | REFERENCES assignment(id) | 可空＝定时快照 |
| snapshot_at | timestamptz | NOT NULL DEFAULT now() | |
| **mastery_map** | jsonb | NOT NULL | `{node_id: {mastery, confidence}}` |
| weak_nodes | jsonb | NOT NULL DEFAULT '[]' | 当时的薄弱点 |
| total_score / class_avg | numeric(6,2) | | |
| **trigger** | varchar(30) | NOT NULL | ASSIGNMENT_GRADED / SCORE_CORRECTION / SCHEDULED |
| created_at | | | |

```sql
CREATE INDEX idx_diagnosis_student_time ON diagnosis_snapshot (student_id, snapshot_at DESC);
CREATE INDEX idx_diagnosis_assignment ON diagnosis_snapshot (assignment_id);
```

### 12.3 `question_error_stat` — 题目错误统计

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| question_id | bigint | NOT NULL REFERENCES question(id) | |
| **scope_type** | varchar(20) | NOT NULL | CLASSROOM / GRADE / ORG / ASSIGNMENT |
| scope_id | bigint | NOT NULL | |
| assignment_id | bigint | REFERENCES assignment(id) | |
| answered_count / wrong_count / blank_count | int | NOT NULL DEFAULT 0 | |
| correct_rate | numeric(5,4) | | |
| **option_distribution** | jsonb | NOT NULL DEFAULT '{}' | **选项分布（关键分析数据）** |
| blank_rate | numeric(5,4) | | |
| avg_score | numeric(5,2) | | |
| calculated_at | timestamptz | NOT NULL DEFAULT now() | |

```sql
CREATE INDEX idx_qes_assignment ON question_error_stat (assignment_id, question_id);
CREATE INDEX idx_qes_scope ON question_error_stat (org_id, scope_type, scope_id, correct_rate);
CREATE UNIQUE INDEX uq_qes ON question_error_stat (question_id, scope_type, scope_id, COALESCE(assignment_id, 0));
```

### 12.4 `error_tag_stat` — 错因标签统计

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| scope_type / scope_id | varchar(20) / bigint | NOT NULL | |
| knowledge_node_id | bigint | REFERENCES knowledge_node(id) | |
| **error_tag** | varchar(50) | NOT NULL | 概念混淆 / 计算错误 / 审题失误 / 单位缺失 / 公式记错 / 空白未答 |
| count | int | NOT NULL DEFAULT 0 | |
| ratio | numeric(5,4) | | |
| calculated_at | | | |

```sql
CREATE UNIQUE INDEX uq_error_tag ON error_tag_stat (COALESCE(org_id,0), scope_type, scope_id, COALESCE(knowledge_node_id,0), error_tag);
```

---

## 十三、个性化练习

### 13.1 `practice_set` — 个性化练习集

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| student_id | bigint | NOT NULL REFERENCES user(id) | |
| **source_assignment_id** | bigint | NOT NULL REFERENCES assignment(id) | 来自哪次任务的诊断（溯源） |
| **source_paper_id** | bigint | NOT NULL REFERENCES paper(id) | **结构复制自哪份试卷** |
| paper_id | bigint | NOT NULL REFERENCES paper(id) | 生成的专属试卷 |
| assignment_id | bigint | REFERENCES assignment(id) | 发布后的任务 |
| **target_nodes** | jsonb | NOT NULL DEFAULT '[]' | 目标知识点 + 生成时掌握度快照 |
| strategy | varchar(20) | NOT NULL DEFAULT 'WEAK_FIRST' | WEAK_FIRST / BALANCED / CHALLENGE |
| **gen_config** | jsonb | NOT NULL DEFAULT '{}' | 生成参数快照（含 `"constraint": "SAME_STRUCTURE_AS_SOURCE"`） |
| effect_delta | jsonb | NOT NULL DEFAULT '{}' | 效果追踪结果 |
| effect_measured_at | timestamptz | | |
| **is_stale** | boolean | NOT NULL DEFAULT false | 成绩修正后标记需重评估 |
| **serial_no** | varchar(20) | NOT NULL | **练习编号（印在 PDF 页脚）** |
| **export_zip_asset_id** | bigint | REFERENCES asset(id) | 批量导出包（24h 后清理） |
| **export_generated_at** | timestamptz | | |
| **export_config** | jsonb | NOT NULL DEFAULT '{}' | 导出参数 |
| **layout_check_result** | jsonb | NOT NULL DEFAULT '{}' | 导出前排版检查结果 |
| status | varchar(20) | NOT NULL DEFAULT 'GENERATED' | GENERATED / TEACHER_APPROVED / PUBLISHED / COMPLETED / CANCELLED |
| created_at / updated_at / deleted_at | | | |

```sql
CREATE UNIQUE INDEX uq_practice_serial_no ON practice_set (org_id, serial_no);
CREATE INDEX idx_practice_student ON practice_set (student_id, status);
CREATE INDEX idx_practice_source ON practice_set (source_assignment_id, student_id);
CREATE INDEX idx_practice_stale ON practice_set (org_id, is_stale) WHERE is_stale = true;
```

### 13.2 `practice_set_item` — 练习题目

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| practice_set_id | bigint | NOT NULL REFERENCES practice_set(id) ON DELETE CASCADE | |
| paper_item_id | bigint | NOT NULL REFERENCES paper_item(id) | |
| target_node_id | bigint | NOT NULL REFERENCES knowledge_node(id) | 该题针对哪个目标知识点 |
| **reason** | varchar(100) | NOT NULL | **选题理由（可解释性）** |
| **difficulty_band** | varchar(20) | NOT NULL | CHALLENGE / CONSOLIDATE / CONFIDENCE |

```sql
CREATE INDEX idx_psi_set ON practice_set_item (practice_set_id);
CREATE UNIQUE INDEX uq_psi ON practice_set_item (practice_set_id, paper_item_id);
```

---

## 十四、AI 编排

### 14.1 `ai_prompt_template` — 提示词模板

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | | **null = 平台默认** |
| **key** | varchar(120) | NOT NULL | `physics.senior.gen_explanation` |
| subject / grade_band / task | varchar | NOT NULL | |
| system_prompt | text | NOT NULL | |
| user_prompt | text | NOT NULL | 含 `{{变量}}` 占位符 |
| output_schema | jsonb | NOT NULL DEFAULT '{}' | |
| glossary | jsonb | NOT NULL DEFAULT '{}' | 术语表 |
| few_shot | jsonb | NOT NULL DEFAULT '[]' | |
| **kb_injection** | jsonb | NOT NULL DEFAULT '{}' | 注入哪些类型知识条目、top_k、最低权重 |
| version | int | NOT NULL | |
| status | varchar(20) | NOT NULL DEFAULT 'DRAFT' | DRAFT / ACTIVE / ARCHIVED |
| metrics | jsonb | NOT NULL DEFAULT '{}' | 采纳率、平均耗时、引用无效率 |
| created_by / created_at / updated_at | | | |

```sql
CREATE UNIQUE INDEX uq_prompt_template ON ai_prompt_template (COALESCE(org_id,0), key, version);
CREATE UNIQUE INDEX uq_prompt_template_active ON ai_prompt_template (COALESCE(org_id,0), key)
    WHERE status = 'ACTIVE';
```

### 14.2 `ai_call_log` — AI 调用日志（**数据资产源头**）

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id / user_id | bigint | | |
| task | varchar(40) | NOT NULL | |
| provider / model | varchar(50) | NOT NULL | |
| prompt_template_key | varchar(120) | | |
| prompt_version | int | | |
| **idempotency_key** | varchar(200) | | 配合成功状态做幂等 |
| **request_payload** | jsonb | NOT NULL | **已脱敏后的实际请求** |
| request_images | jsonb | NOT NULL DEFAULT '[]' | |
| **response_raw** | jsonb | | 厂商原始返回（永不丢弃） |
| response_parsed | jsonb | | 校验后的结构化结果 |
| schema_valid | boolean | | |
| **kb_entry_ids** | jsonb | NOT NULL DEFAULT '[]' | 本次注入的知识条目 |
| **kb_hit_count** | int | NOT NULL DEFAULT 0 | 检索命中数（0 表示拒绝了生成） |
| **citation_valid** | boolean | | 引用校验是否通过 |
| input_tokens / output_tokens | int | | |
| cost_yuan | numeric(10,4) | | |
| latency_ms | int | | |
| **status** | varchar(20) | NOT NULL | SUCCESS / FAILED / TIMEOUT / SCHEMA_INVALID / RATE_LIMITED / **REJECTED_NO_KB**（无知识库依据主动拒绝） |
| **reject_reason** | varchar(30) | | **★ KB_EMPTY（空召回）/ KB_INSUFFICIENT（条目不足）/ MODEL_INSUFFICIENT（模型自述依据不足）**——三种拒绝必须可区分，否则防幻觉效果无法归因 |
| fallback_used | boolean | NOT NULL DEFAULT false | |
| error_message | text | | |
| created_at | timestamptz | NOT NULL DEFAULT now() | |

```sql
CREATE INDEX idx_ai_log_org_time ON ai_call_log (org_id, created_at DESC);
CREATE UNIQUE INDEX uq_ai_log_idem ON ai_call_log (idempotency_key)
    WHERE idempotency_key IS NOT NULL AND status = 'SUCCESS';
CREATE INDEX idx_ai_log_task_status ON ai_call_log (task, status, created_at DESC);
```

### 14.3 `ai_feedback` — 教师对 AI 输出的修正

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| ai_call_log_id | bigint | NOT NULL REFERENCES ai_call_log(id) | |
| task | varchar(40) | NOT NULL | |
| **business_type** | varchar(30) | NOT NULL | question / question_figure / score_result / knowledge_entry |
| business_id | bigint | NOT NULL | |
| ai_output | jsonb | NOT NULL | |
| final_output | jsonb | NOT NULL | |
| **diff** | jsonb | NOT NULL DEFAULT '[]' | [{path, from, to}] |
| **action** | varchar(20) | NOT NULL | ACCEPTED / MODIFIED / REJECTED |
| teacher_id | bigint | NOT NULL | |
| created_at | | | |

```sql
CREATE INDEX idx_ai_feedback_task_action ON ai_feedback (task, action, created_at DESC);
CREATE INDEX idx_ai_feedback_business ON ai_feedback (business_type, business_id);
```

### 14.4 `kb_retrieval_log` — 知识库检索日志

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| **ai_call_log_id** | bigint | REFERENCES ai_call_log(id) | **★ 关联本次调用（否则 `result_accepted` 只能靠时间窗口回填，同题多次生成时会串号）** |
| task | varchar(40) | NOT NULL | |
| query_text | text | | |
| hit_ids | jsonb | NOT NULL DEFAULT '[]' | 命中的条目与分数 |
| **hit_count** | int | NOT NULL DEFAULT 0 | 命中数（0 = 空召回） |
| used_ids | jsonb | NOT NULL DEFAULT '[]' | 实际注入的条目 |
| result_accepted | boolean | | 最终生成结果是否被采纳（由 `ai_call_log_id` 精确回填） |
| created_at | | | |

```sql
CREATE INDEX idx_kb_retrieval_task ON kb_retrieval_log (org_id, task, created_at DESC);
CREATE INDEX idx_kb_retrieval_call ON kb_retrieval_log (ai_call_log_id);
CREATE INDEX idx_kb_retrieval_empty ON kb_retrieval_log (org_id, task)
    WHERE hit_count = 0;      -- 空召回率看板
```

---

## 十五、导入与审计

### 15.1 `ingest_batch` — 导入批次

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id / created_by | bigint | NOT NULL | |
| **source_type** | varchar(20) | NOT NULL | IMAGE / PDF / PDF_SCANNED / DOCX / XLSX / MANUAL |
| file_keys | jsonb | NOT NULL DEFAULT '[]' | asset id 列表 |
| status | varchar(20) | NOT NULL DEFAULT 'UPLOADED' | UPLOADED / PARSING / SEGMENTED / REVIEWING / COMPLETED / FAILED |
| **parse_strategy** | varchar(20) | | TEXT_LAYER / VISION / HYBRID |
| detected_count / confirmed_count | int | NOT NULL DEFAULT 0 | |
| parse_log | jsonb | NOT NULL DEFAULT '{}' | 各阶段日志 |
| warnings | jsonb | NOT NULL DEFAULT '[]' | |
| created_at / completed_at | | | |

```sql
CREATE INDEX idx_ingest_batch_org ON ingest_batch (org_id, status, created_at DESC);
```

### 15.2 `ingest_item` — 批次中的单题（校对对象）

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| batch_id | bigint | NOT NULL REFERENCES ingest_batch(id) ON DELETE CASCADE | |
| seq | int | NOT NULL | |
| raw_segment | jsonb | NOT NULL | 切题原始结果 |
| source_ref | jsonb | NOT NULL DEFAULT '{}' | 页码/坐标 |
| confidence | numeric(4,3) | | |
| status | varchar(20) | NOT NULL DEFAULT 'PENDING' | PENDING / CONFIRMED / EDITED / DISCARDED |
| question_id | bigint | REFERENCES question(id) | 确认入库后的题目 |
| **discard_reason** | varchar(200) | | **丢弃原因（用于分析哪些题型识别差）** |
| reviewed_by / reviewed_at | | | |

```sql
CREATE INDEX idx_ingest_item_batch ON ingest_item (batch_id, seq);
CREATE INDEX idx_ingest_item_status ON ingest_item (batch_id, status);
```

### 15.3 `audit_log` — 操作审计

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id / user_id | bigint | | |
| **action** | varchar(80) | NOT NULL | `score_result.modify` / `question.delete` / `assignment.publish` / `question_figure.recrop` / `scan_card.rebind_student` |
| target_type / target_id | varchar(50) / bigint | NOT NULL | |
| **before / after** | jsonb | | **变更前后（成绩修改必须记录）** |
| reason | varchar(200) | | |
| ip / user_agent | varchar(64) / varchar(300) | | |
| request_id | varchar(64) | | |
| created_at | | | |

```sql
CREATE INDEX idx_audit_org_time ON audit_log (org_id, created_at DESC);
CREATE INDEX idx_audit_target ON audit_log (target_type, target_id, created_at DESC);
CREATE INDEX idx_audit_action ON audit_log (action, created_at DESC);
```

### 15.4 `data_export_log` — 数据导出日志（合规需要）

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id / user_id | bigint | NOT NULL | |
| **export_type** | varchar(50) | NOT NULL | SCORE_SHEET / STUDENT_REPORT / CLASS_REPORT / QUESTION_BANK / PRACTICE_PACK |
| scope | jsonb | NOT NULL DEFAULT '{}' | |
| row_count | int | | |
| file_asset_id | bigint | REFERENCES asset(id) | |
| created_at | | | |

### 15.5 `pii_mapping` — 脱敏映射表（独立、加密）

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | bigserial | PK | |
| org_id | bigint | NOT NULL | |
| **token** | varchar(64) | NOT NULL | `S{sha256[:10]}` |
| **field_name** | varchar(50) | NOT NULL | student_name / student_no / phone … |
| **encrypted_value** | bytea | NOT NULL | **字段级加密（AES-GCM）** |
| created_at | timestamptz | NOT NULL DEFAULT now() | |
| expires_at | timestamptz | | 定期清理 |

```sql
CREATE UNIQUE INDEX uq_pii_mapping ON pii_mapping (org_id, token, field_name);
CREATE INDEX idx_pii_mapping_expiry ON pii_mapping (expires_at) WHERE expires_at IS NOT NULL;
```

> **本表与业务表物理分离是刻意的**：便于独立加密、独立清理、独立授权。

---

## 十六、表清单速查

| # | 表 | 模块 | 含 org_id |
| --- | --- | --- | --- |
| 1 | org | 组织 | — |
| 2 | user | 账号 | ✅ |
| 3 | classroom | 组织 | ✅ |
| 4 | **major** | 组织 | ✅ |
| 5 | classroom_student | 组织 | ✅ |
| 6 | **course** | 知识 | ✅ |
| 7 | knowledge_node | 知识 | ✅ |
| 8 | **task** | 知识 | ✅ |
| 9 | knowledge_edge | 知识 | ✅ |
| 10 | chapter | 知识 | ✅ |
| 11 | chapter_knowledge | 知识 | ✅ |
| 12 | **task_knowledge** | 知识 | ✅ |
| 13 | knowledge_entry | 知识 | ✅ |
| 14 | knowledge_entry_node | 知识 | ✅ |
| 15 | question | 题库 | ✅ |
| 16 | question_option | 题库 | ✅ |
| 17 | question_image | 题库 | ✅ |
| 18 | question_knowledge | 题库 | ✅ |
| 19 | question_relation | 题库 | ✅ |
| 20 | asset | 媒体 | ✅ |
| 21 | paper | 组卷 | ✅ |
| 22 | paper_item | 组卷 | ✅ |
| 23 | paper_equivalence | 组卷 | ✅ |
| 24 | sheet_style | 答题卡 | ✅ |
| 25 | sheet_template | 答题卡 | ✅ |
| 26 | paper_sheet_binding | 答题卡 | ✅ |
| 27 | assignment | 发布 | ✅ |
| 28 | assignment_student | 发布 | ✅ |
| 29 | response | 作答 | ✅ |
| 30 | student_total_score | 作答 | ✅ |
| 31 | scan_batch | 扫描 | ✅ |
| 32 | scan_card | 扫描 | ✅ |
| 33 | score_result | 评分 | ✅ |
| 34 | knowledge_mastery | 学情 | ✅ |
| 35 | diagnosis_snapshot | 学情 | ✅ |
| 36 | question_error_stat | 学情 | ✅ |
| 37 | error_tag_stat | 学情 | ✅ |
| 38 | practice_set | 练习 | ✅ |
| 39 | practice_set_item | 练习 | ✅ |
| 40 | ai_prompt_template | AI | ✅ |
| 41 | ai_call_log | AI | ✅ |
| 42 | ai_feedback | AI | ✅ |
| 43 | kb_retrieval_log | AI | ✅ |
| 44 | ingest_batch | 导入 | ✅ |
| 45 | ingest_item | 导入 | ✅ |
| 46 | audit_log | 审计 | ✅ |
| 47 | data_export_log | 审计 | ✅ |
| 48 | pii_mapping | 安全 | ✅ |
| 49 | celery_task_meta | 系统 | — |

**共 49 张表**（其中 47 张含 `org_id`，全部必须使用 `OrgScopedManager`）。

> **v1.1 新增 4 张**：`major`（专业）、`course`（课程）、`task`（任务）、`task_knowledge`（任务-知识点多对多）——职业教育适配。原 45 张 → 49 张。

---

## 十七、迁移策略

| 规则 | 说明 |
| --- | --- |
| 迁移粒度 | 一个逻辑变更一个迁移文件；**迁移文件必须独立提交** |
| 命名 | Django 默认 `NNNN_<动作>_<对象>.py`，动作必须可读（`add_figure_confirm_fields`） |
| 大表变更 | 超 100 万行的表加字段用 `ADD COLUMN ... DEFAULT NULL` 再分批回填，**禁止带 `NOT NULL DEFAULT` 直接加**（会锁表） |
| 索引创建 | 生产环境用 `CREATE INDEX CONCURRENTLY`（Django 需 `AddIndexConcurrently`） |
| 数据迁移 | 与结构迁移分离；必须可回滚；必须分批（每批 1000 行） |
| 禁止 | 在迁移里调用业务服务、调用外部 API、做复杂计算 |
| 回滚 | 每个迁移必须能在 5 分钟内回滚（否则拆成多步） |
| 生产执行 | 发布窗口执行；先在 staging 演练 |
| 校验 | CI 执行 `makemigrations --check --dry-run`，模型与迁移不一致即失败 |

**pgvector 索引说明**：`ivfflat` 索引需要在**有数据后**创建效果才好（lists 约等于 `sqrt(行数)`）。初次创建时表为空，建议：

```sql
-- 初始迁移只建列，不建索引
-- 数据导入后由数据迁移或运维脚本建索引
CREATE INDEX CONCURRENTLY idx_question_embed
    ON question USING ivfflat (embed_vector vector_cosine_ops) WITH (lists = 200);
```

---

## 十八、种子数据（`scripts/seed.py`）

**必须一条命令生成完整可演示环境**：

```bash
python scripts/seed.py --org-size small   # 一期规模
python scripts/seed.py --org-size full    # 满规模（性能测试用）
```

| 数据 | small | full | 说明 |
| --- | --- | --- | --- |
| 机构 | 1 | 1 | 含默认 ai_config / grading_config |
| 班级 | 4 | 100 | 每班 50 人 |
| 学生 | 200 | 5000 | 学号位数统一为 8 位（可配） |
| 教师 | 8 | 40 | 含 1 位机构管理员 |
| 知识点树 | 物理/数学/英语 三科，各约 80 节点 | 同左 | 从平台模板深拷贝 |
| 章节 | 每科 3 章 12 节 | 同左 | 与知识点建立映射 |
| 知识库条目 | 50 | 500 | 覆盖 8 种类型，含 10 条 COMMON_ERROR |
| 题目 | 500（其中 30% 含配图） | 20000 | 含 AI 生成题 20 道（DRAFT 状态）与 DRAFT/PENDING_REVIEW 各若干（**用于验证组卷过滤**） |
| 提示词模板 | 3 学科 × 8 任务 | 同左 | 含 glossary 与 few_shot |
| 答题卡样式 | 3 个系统预设 + 1 个自定义 | 同左 | 分别 8 位 / 6 位 / 12 位多行 |
| 试卷 | 6 | 200 | 含 1 组 AB 卷 |
| 发布任务 | 3（1 已批改 / 1 进行中 / 1 未开始） | 100 | 含 ONLINE 与 PAPER_OMR 两种通道 |
| 扫描批次 | 3 | 20 | 含 **1 张锚点失败卡、1 张学号多位多涂卡、1 张学号不在名册卡** |
| 配图 | 含 1 个批次有 **PENDING 配图**（用于验证门禁） | 同左 | |
| 掌握度与快照 | 已批改任务的完整结果 | 同左 | 含 20 天历史快照（用于趋势图） |
| 个性化练习 | 1 个班 50 份（含 2 份题库缺口失败记录） | 100 班 | |

**种子数据要求**：

| 要求 | 说明 |
| --- | --- |
| 确定性 | 用固定随机种子（`--seed 42`），同样参数生成同样数据 |
| 幂等 | 重复执行不产生重复数据（先清理或 upsert） |
| 图片 | 用程序生成占位图（几何图/电路图/函数图各若干），不依赖外部素材 |
| 覆盖异常场景 | **必须包含**：DRAFT 题、PENDING 配图、锚点失败卡、学号异常卡、题库缺口、AB 卷不达标案例 |
| 可清理 | `python scripts/seed.py --flush` 清空演示数据 |

---

## 变更记录

| 版本 | 日期 | 变更人 | 说明 |
| --- | --- | --- | --- |
| v1.0 | 2026-10-01 | — | 首版，覆盖 45 张表，对齐设计文档 v2.1 第 12 章 |

## 待确认问题

| 编号 | 问题 | 影响 |
| --- | --- | --- |
| Q-02-1 | pgvector 维度是否固定 1024？（不同厂商 embedding 维度不同） | 若切换厂商，维度变化需重建列与索引；建议在 README 固定 1024 并使用支持该维度的模型 |
| Q-02-2 | `assignment_student.option_order` 用 `{"12": [...]}` 的 key 是 `paper_item_id` 还是 `seq`？ | 建议用 `paper_item_id`（seq 会因一人一卷而不同），本文按 `paper_item_id` 定义 |
| Q-02-3 | 扫描原图删除后，`scan_card.raw_asset_id` 是否需要置空？ | 建议保留 asset 记录（软删），保留可追溯性 |
| Q-02-4 | 是否需要答卷 PDF 归档（学生纸质作答的原件）？ | 影响存储成本；当前设计只存扫描件 |
