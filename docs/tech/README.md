# 「知否」技术文件集 · 索引与共享约定

> **本文件是全套技术文件的唯一起点。** 任何开发任务开工前，先读本文件；本文件定义的命名、目录、字段、编号规则对全部子文件具有强制约束力。
>
> 上游依据：[知否-开发设计文档-v2.1.md](../知否-开发设计文档-v2.1.md)（以下简称「设计文档」）。本文与设计文档冲突时，**以设计文档为准，并以 issue 形式回写本文**。

| 项目 | 内容 |
| --- | --- |
| 文件集版本 | v1.0（与设计文档 v2.1 对齐） |
| 适用阶段 | 阶段零（技术验证）→ 阶段三（规模化） |
| 目标读者 | 后端、前端、算法、测试、运维、产品 |

---

## 一、文件清单与阅读顺序

| 编号 | 文件 | 内容 | 主要读者 | 何时必读 |
| --- | --- | --- | --- | --- |
| — | **README.md（本文件）** | 索引 + 共享约定（命名/目录/字段/编号/错误码） | 全员 | **开工前必读** |
| 00 | [00-架构与工程约定.md](00-架构与工程约定.md) | 系统分层、模块边界、异步任务、错误码总表、日志与追踪、环境与配置、CI 门禁 | 全员 | 开工前必读 |
| 01 | [01-项目结构与编码规范.md](01-项目结构与编码规范.md) | 完整目录树、分层职责、命名规范、Python/Vue 编码规范、Git 工作流、Code Review 清单 | 全员 | 写第一行代码前 |
| 02 | [02-数据模型与DDL.md](02-数据模型与DDL.md) | 全部表 DDL、索引、约束、迁移策略、种子数据 | 后端 | 第一个模块开发前 |
| 03 | [03-开发计划与任务分解.md](03-开发计划与任务分解.md) | 阶段划分、迭代计划、任务拆解与工时、里程碑与交付物 | 全员/PM | 开工前必读 |
| 04 | [04-部署与运维手册.md](04-部署与运维手册.md) | 环境拓扑、部署步骤、备份恢复、监控告警、故障处置、扩容路径 | 运维/后端 | 搭环境前 |
| 05 | [05-API契约规范.md](05-API契约规范.md) | 通用约定、全部接口定义、请求/响应示例、错误码、OpenAPI 生成方式 | 前后端 | 接口联调前 |
| 06 | [06-AI编排层实现规格.md](06-AI编排层实现规格.md) | Provider 抽象、强制脱敏、路由、幂等限流、知识库检索、防幻觉校验、评估集 | 后端/算法 | AI 模块开发前 |
| 07 | [07-导入与配图识别规格.md](07-导入与配图识别规格.md) | 解析切题、配图三路径、归属规则、逐图确认闸门、关口二校验、质量标记 | 后端/算法 | 导入模块开发前 |
| 08 | [08-答题卡与OMR规格.md](08-答题卡与OMR规格.md) | 答题卡排版与生成、学号区、OMR 算法链、阈值标定、手机端采集、仲裁 | 后端/前端 | 答题卡模块开发前 |
| 09 | [09-组卷与批改规格.md](09-组卷与批改规格.md) | 候选筛选、Fisher-Yates、约束求解、等值校验、题库健康度、填空批改、重算链 | 后端 | 组卷/批改开发前 |
| 10 | [10-学情与个性化练习规格.md](10-学情与个性化练习规格.md) | 掌握度算法、增量/全量重算、热力图、练习生成（结构约束）、PDF 批量导出、效果追踪 | 后端/算法 | 学情模块开发前 |
| 11 | [11-前端实现规范与设计令牌.md](11-前端实现规范与设计令牌.md) | 完整设计令牌、Element Plus 主题、组件规范、路由与页面实现、AI 味走查 | 前端 | 前端开工前 |
| 12 | [12-测试方案与验收标准.md](12-测试方案与验收标准.md) | 测试分层、测试数据集、42 条验收标准对应测试、CI 集成、缺陷分级 | 测试/全员 | 写测试前 |
| 13 | [13-配置项与提示词库.md](13-配置项与提示词库.md) | 全部配置项、机构级覆盖、提示词模板库（多学科）、术语表 | 后端/教研 | 配置化开发前 |
| **14** | **[14-缺陷登记册与统一指标口径.md](14-缺陷登记册与统一指标口径.md)** | **★ 未决问题唯一登记处、统一指标口径表（验收唯一依据）、待裁决事项** | **全员** | **开工前必读；与指标交叉引用时只用这张表** |
| **15** | **[15-职教适配与存量数据迁移规格.md](15-职教适配与存量数据迁移规格.md)** | **★ 职业教育适配（专业/课程/任务）、存量数据迁移规格（1,881 题 + 42 知识点 + 111 条知识库初稿）** | **后端/教研** | **导入开发前必读；数据依据来自真实 Excel** |
| **16** | **[16-开发前准备清单与开工判定.md](16-开发前准备清单与开工判定.md)** | **开发前准备清单、环境基线实测（PDF 链路 / 依赖 / 字体）、PoC 发现的缺陷、三批开工顺序** | **全员** | **开工前必读** |
| **17** | **[17-答题卡编辑器设计.md](17-答题卡编辑器设计.md)** | **★ 教师端答题卡编辑器：页面规格、参数清单、排版引擎算法（分栏/分页/校验）、题型签名、接口、任务分解** | **前端/后端/产品** | **答题卡模块开发前必读** |
| **19** | **[19-需求确认与增量计划.md](19-需求确认与增量计划.md)** | **★ 第二轮需求确认书**：6 条需求的逐条对账、12 项决策记录、6 个须先修的缺陷、15 个新任务 |
| **20** | **[20-难度快速打标页设计.md](20-难度快速打标页设计.md)** | **难度快速打标页**（`P1-100b`）：独立页面、一题一屏、数字键一键提交并跳下一题；★ 口径 = 纯人工打标（不是启发式） |

**推荐阅读路径**：

- 新人入职：README → 00 → 01 → 03 → 02 → 自己模块的规格文件
- 前端：README → 00 → 01 → 05 → 11 → 08（扫描端）
- 后端：README → 00 → 01 → 02 → 自己模块的规格文件 → 12
- 运维：README → 00 → 04 → 03

---

## 二、项目标识约定（**强制**）

| 项 | 约定值 | 说明 |
| --- | --- | --- |
| 中文产品名 | 知否 | |
| 英文/代码名 | `zhifou` | 全小写，用于仓库名、包名、Docker 服务名 |
| 前端代码前缀 | `zf` | CSS 变量 `--zf-*`、组件 `ZfXxx`、localStorage key `zf_*` |
| Python 包根 | `zhifou/` | 项目配置包 |
| 业务模块包 | `apps/<module>/` | 见第一节目录树 |
| 数据库名 | `zhifou` | 测试库 `zhifou_test` |
| Redis key 前缀 | `zf:` | 例 `zf:session:{token}` |
| 对象存储 key 前缀 | `zf/{org_id}/{kind}/...` | 见 02 文档的 `asset` 表 |
| 环境变量前缀 | `ZF_` | 例 `ZF_SECRET_KEY` |
| API 基础路径 | `/api/v1/` | |
| Async 任务队列名 | `p0_critical` / `p1_generate` / `p2_recompute` | **不得新增队列**，需要新分类时改本文 |

---

## 三、目录结构约定（**强制**）

除 `deploy/`、`docs/`、`web/` 外，后端代码一律位于 `server/`。仓库采用 **monorepo**：

```
tiku/
├── docs/                        # 设计文档与本技术文件集
│   ├── 知否-开发设计文档-v2.1.md
│   ├── 知否-开发设计文档-v1.0-归档.md
│   └── tech/                    # 本文件集
├── server/                      # 后端（Django）
├── web/                         # 前端（Vue 3，教师端 + 学生端 + 手机扫描端）
├── deploy/                      # 部署（Dockerfile / compose / nginx / 初始化脚本）
└── scripts/                     # 运维与开发辅助脚本
```

**`server/` 内部结构**（详见 01 文档）：

```
server/
├── manage.py
├── pyproject.toml
├── zhifou/                      # 项目配置包
│   ├── settings/                # base.py / local.py / staging.py / production.py
│   ├── urls.py  celery.py  asgi.py  wsgi.py
└── apps/
    ├── common/                  # 通用工具（无业务依赖）
    ├── accounts/  org/          # 用户、机构、班级、名册
    ├── knowledge/               # 知识点树、章节、知识库
    ├── questionbank/            # 题目、选项、配图、关系
    ├── ingest/                  # 导入流水线
    ├── ai/                      # AI 编排层
    ├── paper/                   # 组卷
    ├── sheet/                   # 答题卡样式与模板
    ├── assignment/              # 发布任务
    ├── response/                # 作答（线上 + 扫描）
    ├── omr/                     # OMR 引擎（识别算法）
    ├── grading/                 # 批改
    ├── analytics/               # 学情
    ├── practice/                # 个性化练习
    ├── media/                   # 资源与对象存储
    └── audit/                   # 审计
```

---

## 四、命名规范（**强制**）

### 4.1 数据库

| 对象 | 规则 | 示例 |
| --- | --- | --- |
| 表名 | 小写 + 下划线，**单数**，模块语义前缀 | `question`、`question_image`、`paper_item`、`scan_card` |
| 主键 | 统一 `id bigserial primary key` | |
| 外键字段 | `<被引用表单数>_id` | `question_id`、`paper_item_id` |
| 时间字段 | `created_at` / `updated_at` / `deleted_at`（timestamptz） | |
| 布尔字段 | `is_*` / `has_*` / `allow_*` | `is_primary`、`has_figure`、`allow_late` |
| 枚举字段 | 存字符串，值用 **大写下划线** | `status` = `APPROVED` |
| 计数/统计 | `*_count` / `*_rate` / `*_ratio` / `*_score` | `wrong_count`、`correct_rate` |
| JSONB 字段 | `*_json` / `*_config` / `*_meta` / `*_flags` / `*_evidence` | `layout_json`、`ai_meta` |
| 索引名 | `idx_<表>_<字段缩写>`；唯一 `uq_<表>_<字段>` | `idx_question_org_subject_status` |
| 外键约束名 | `fk_<表>_<字段>` | `fk_paper_item_question` |

### 4.2 Python

| 对象 | 规则 | 示例 |
| --- | --- | --- |
| 模块/包 | 小写 + 下划线 | `figure_binding.py` |
| 类 | 大驼峰 | `FigureBindingService` |
| 函数/变量 | 小写 + 下划线 | `detect_figure_regions` |
| 常量 | 全大写 | `FILL_THRESHOLD_FILLED` |
| 私有 | 单下划线前缀 | `_normalize_text` |
| 任务类型枚举 | 大写下划线（字符串值） | `TaskType.OCR_QUESTION = "ocr_question"` |
| 服务层类 | 以 `Service` 结尾 | `GradingService` |
| 选择器（读） | 以 `Selector` 结尾 | `QuestionSelector` |
| Celery 任务 | `动词_名词` + 显式 `queue=` | `extract_figures_for_batch` |

### 4.3 前端

| 对象 | 规则 | 示例 |
| --- | --- | --- |
| 组件文件 | 大驼峰 | `FigureConfirmPanel.vue` |
| 视图/页面 | 大驼峰 | `IngestReviewView.vue` |
| 组合式函数 | `use` 前缀 | `useQualityCheck.ts` |
| Pinia store | `use<Domain>Store` | `useQuestionBankStore` |
| API 模块 | 按后端模块同名 | `api/questionbank.ts` |
| CSS 变量 | `--zf-<类别>-<语义>` | `--zf-brand`、`--zf-text-secondary` |
| 路由 name | 大驼峰，与页面组件同名 | `name: 'IngestReview'` |

---

## 五、状态枚举总表（**跨模块唯一口径**）

以下枚举值在数据库、API、前端共用。**新增枚举值必须回写本表**。

### 5.1 题目 `question.status`

| 值 | 含义 | 可否组卷 |
| --- | --- | --- |
| `DRAFT` | 草稿（AI 生成题的默认状态） | ❌ |
| `PENDING_REVIEW` | 待审核 | ❌ |
| `APPROVED` | 已审核可用 | ✅ |
| `REJECTED` | 已拒绝 | ❌ |
| `ARCHIVED` | 归档 | ❌ |

### 5.2 题目来源 `question.source_type`

`ORIGINAL_HUMAN`（人工原创） / `AI_GENERATED`（AI 生成） / `AI_REWRITTEN`（AI 改写） / `EXTRACTED`（文件识别导入）

### 5.3 配图确认 `question_image.review_status`（v2.1）

| 值 | 含义 | 是否满足门禁 |
| --- | --- | --- |
| `PENDING` | 待确认 | ❌ 阻断 |
| `RECROPPED` | 已重裁（瞬时态，随即转 CONFIRMED） | ❌ 阻断 |
| `REPLACED` | 已换图（瞬时态） | ❌ 阻断 |
| `CONFIRMED` | 已确认 | ✅ |
| `DELETED` | 判定为非配图 | ✅ |

`confirm_method`：`CLICK` / `RECROP` / `REPLACE` / `MANUAL_CAPTURE` / `BATCH`

### 5.4 作答来源 `response.source`

`ONLINE` / `SCAN_PHONE` / `SCAN_UPLOAD` / `MANUAL`

**合并优先级（高→低）**：`MANUAL` > `SCAN_PHONE` = `SCAN_UPLOAD` > `ONLINE`

### 5.5 评分方式 `score_result.method`

`AUTO_EXACT` / `AUTO_NORMALIZED` / `AUTO_FUZZY` / `AUTO_SYNONYM` / `AI_EQUIV` / `MANUAL` / `MANUAL_AGGREGATE`（快速模式分摊）

### 5.6 评分状态 `score_result.state`

`PENDING` / `AUTO_GRADED` / `NEEDS_REVIEW` / `MANUAL_ENTERED` / `TEACHER_CONFIRMED` / `TEACHER_MODIFIED` / `VOID`

### 5.7 扫描卡 `scan_card.status`

`PENDING` / `AUTO_OK` / `NEEDS_REVIEW` / `REVIEWED` / `VOID` / `FAILED`

### 5.8 扫描批次 `scan_batch.status`

`UPLOADING` / `PROCESSING` / `WAITING_REVIEW` / `COMPLETED` / `FAILED`

`capture_channel`：`PHONE_H5` / `UPLOAD`

### 5.9 发布任务 `assignment.status`

`DRAFT` / `PUBLISHED` / `CLOSED` / `ARCHIVED`

`channel`：`PAPER_OMR` / `ONLINE` / `BOTH`

### 5.10 学生任务状态 `assignment_student.status`

`NOT_STARTED` / `IN_PROGRESS` / `SUBMITTED` / `SCANNED` / `ABSENT` / `PARTIAL`

### 5.11 组卷方式 `paper.compose_mode`

`MANUAL` / `RULE` / `DIAGNOSIS`

`purpose`：`QUIZ` / `EXAM` / `HOMEWORK` / `PRACTICE` / `MAKEUP`

### 5.12 提示词模板 `ai_prompt_template.status`

`DRAFT` / `ACTIVE` / `ARCHIVED`

### 5.13 知识条目 `knowledge_entry`

`entry_type`：`CONCEPT` / `THEOREM` / `METHOD` / `EXAMPLE` / `COMMON_ERROR` / `SYLLABUS` / `EXPERIMENT` / `REFERENCE`

`source_type`：`TEXTBOOK` / `TEACHER_INPUT` / `UPLOADED_DOC` / `EXTERNAL` / `PLATFORM`

`status`：`DRAFT` / `ACTIVE` / `ARCHIVED`

### 5.14 个性化练习 `practice_set.status`

`GENERATED` / `TEACHER_APPROVED` / `PUBLISHED` / `COMPLETED` / `CANCELLED`

`strategy`：`WEAK_FIRST` / `BALANCED` / `CHALLENGE`

### 5.15 试卷与发布任务补充枚举（v1.0 补齐）

`paper.status`：`DRAFT` / `FINALIZED` / `ARCHIVED`

`paper.purpose`：`QUIZ` / `EXAM` / `HOMEWORK` / `PRACTICE` / `MAKEUP`

`paper.compose_mode`：`MANUAL` / `RULE` / `DIAGNOSIS`

`paper.variant_label`：`A` / `B` / `MAKEUP`

`assignment.channel`：`PAPER_OMR` / `ONLINE` / `BOTH`

`assignment.answer_visibility`：`AFTER_SUBMIT` / `AFTER_GRADE` / `NEVER`

`assignment.score_visibility`：`TOTAL` / `DETAIL` / `NONE`

`question_relation.relation`：`VARIANT` / `SAME_SOURCE` / `SIMILAR` / `PREREQUISITE_QUESTION`

`sheet_style.page_size`：`A4` / `A3` / `B4` / `16K`
`sheet_style.sides`：`SINGLE` / `DOUBLE`
`sheet_style.layout_mode`：`1_PER_ROW` / `2_PER_ROW`
`sheet_style.student_no_layout`：`SINGLE_ROW` / `MULTI_ROW`
`sheet_style.subjective_zone`：`NONE` / `BY_SCORE` / `CUSTOM`

`scan_card.student_no_flags`（数组元素）：`MULTI_MARK_POS{n}` / `CHECKSUM_FAILED` / `NOT_IN_ROSTER` / `DUPLICATE` / `SHORT_NO`

`question_image.quality_flags`（数组元素）：`LOW_RESOLUTION` / `LOW_CONTRAST` / `BLURRY` / `TILTED` / `HAS_BINDING_LINE` / `SUSPECT_CROP` / `MULTI_FIGURE_MERGED` / `TEXT_OCR_FAILED`

`paper_item.layout_flags`（数组元素，**归属 paper_item，非 question_image**，见 14 文档 D-03）：`SPLIT_BY_PAGE` / `TOO_WIDE` / `TOO_SMALL` / `OVERLAP` / `DUP_IN_PAPER`

`asset.kind`：`QUESTION_FIGURE` / `SCAN_RAW` / `SCAN_RECTIFIED` / `PAPER_PDF` / `SHEET_PDF` / `UPLOAD_SOURCE` / `REPORT_PDF` / `KNOWLEDGE_ATTACH` / `EXPORT_ZIP` / `EXPORT_PART`

### 5.16 职业教育适配枚举（v1.1 新增）

**`grade_band` 学段（含职教）**：
`PRIMARY`（小学）/ `JUNIOR`（初中）/ `SENIOR`（普高）/ **`SECONDARY_VOCATIONAL`**（中等职业学校，职高/中职）/ **`HIGHER_VOCATIONAL`**（高等职业院校）

**`major.level`**：`SECONDARY`（中职）/ `HIGHER`（高职）

**`course.style_config.question_style`**：`TASK_DRIVEN`（任务驱动，职教默认）/ `KNOWLEDGE_POINT`（知识点导向，K12 常用）

**`task.is_practical`**：布尔（是否实训任务）

### 5.17 `user.role`（v1.2 增补 `STUDENT`）

| 值 | 等级 | 含义 |
| --- | --- | --- |
| `PLATFORM_ADMIN` | 30 | 平台管理员（跨机构） |
| `ORG_ADMIN` | 20 | 机构管理员 |
| `TEACHER` | 10 | 教师 |
| **`STUDENT`** | **0** | **学生身份行**：每个名册学号一行，发布任务时自动建，一期**无登录凭据** |

★ 等级表在 `apps/common/permissions.py::ROLE_RANK`（**加角色只改这一处**）。
★ `STUDENT` 等级必须是 0 且不加：作答走 `/s/{token}/` + `session_token`，不走 JWT；
  等级一旦 > 0，学生就会自动获得教师端接口权限。
★ 学生能否登录看的是**有没有可用密码**（`User.can_login`），不是这个角色值。

### 5.18 题库双轨（v2.0 新增，2026-10-21）

**`question_bank.kind`** —— 题目双轨的唯一判据：

| 值 | 含义 | `org_id` 指向 |
| --- | --- | --- |
| `PRIVATE` | 机构私有库 | 本机构 |
| `PUBLIC` | 平台公共库 | **平台机构行**（`org.type=PLATFORM`） |

**`question_bank.status`**：`ACTIVE`（启用）/ `ARCHIVED`（已归档）

**`org.type` 增补 `PLATFORM`**（平台本体）：

| 值 | 含义 |
| --- | --- |
| `SCHOOL` | 学校 |
| `TRAINING` | 培训机构 |
| **`PLATFORM`** | **平台本体**：公共题库/公共知识库等系统级资产的 `org_id` 锚点 |

★ **平台行不是「一种机构」**，它不参与机构列表与容量统计（查询须 `exclude(type=PLATFORM)`）。
★ **不要把平台管理员账号的 `org_id` 改到平台行**——`for_org()` 只按 `org_id` 过滤，
  改完他立刻看不到任何学校的业务数据。平台权限走 `role=PLATFORM_ADMIN` 判定。

**可见性口径（`QuestionQuerySet.for_org()`，唯一收口点）**：

```
for_org(org_id) = Q(org_id=org_id) | Q(bank.kind=PUBLIC AND status=APPROVED)
```

| 场景 | 看得见公共题 | 说明 |
| --- | --- | --- |
| 题库列表 `/questions/?bank=public` | ✅ 仅 `APPROVED` | 不收窄创建人 |
| 题库列表 `/questions/`（默认 private） | ❌ | 本校私有题，按「本人 + 学校公共」收窄 |
| **组卷取数 `usable_for_paper()`** | ❌ | 决策 3：引用公共题须先**下载**成私有副本 |
| 写操作（改/删/审核/打标） | ❌ | 公共题只能由 `PLATFORM_ADMIN` 改 |

★ 公共题的 `course`/`task` 指向**来源机构**的知识树，因此下载时必须由下载方指定
  本校接收课程，知识点按 `code` 重新映射（映射不上的会在返回里列出）。

### 5.19 公式（TeX）约定（v2.1 新增，2026-10-22）

**存储**：公式以 **TeX 原文**写在文本字段里，定界符 `$…$`（行内）/ `$$…$$`（独立）。
**不存** KaTeX 生成的 HTML。三条理由：

1. 后端清洗白名单里没有 `class` 也没有 `<math>`，存 HTML 会被洗成没有 class 的
   `<span>`，公式彻底散架；要放行就得给白名单开 `class`，那是 XSS 的口子。
2. 老师改不了 —— 存 HTML 想改 `\frac` 就得重排整个公式。
3. 导出源要的是 TeX（Node SSR 与 Word 文本降级都需要原文）。

**扫描规则（三处必须同构，改一处必须改三处）**：

| 位置 | 文件 |
| --- | --- |
| 浏览器 | `web/src/utils/math.ts` → `scanMath` |
| 后端排版 / 降级 | `server/apps/common/mathtext.py` → `find_math_spans` |
| PDF 渲染 | `render-service/src/math.js` → `scanMath` |

支持的定界符：`$$…$$`、`\[…\]`（独立，可跨行）；`$…$`、`\(…\)`（行内，不跨行）。
`\$` 是转义美元符，不参与配对。**未闭合一律退回字面文本**（老师打了一半就保存，
不该整页报错）。

**行内 `$…$` 的误判防护**：含反斜杠命令 → 是公式；含中文且无反斜杠 → 不是
（`价格 $5 到 $10 之间` 不能变成斜体 `5 到 `）。★ 残余误判：`$5$` 会被当公式。

**`$a<b$` 必须被保护**：HTML5 解析器会把 `<b` 当标签开头，`$` 被吞进标签名，
结果是 `求 $a<b$ 的解` → `求 $a 的解`，**不报错、不告警**。
所以 `sanitize_html` 的顺序是：挖出公式 → 换 ASCII 占位符 → nh3 清洗 → 放回去。

**统一渲染出口（前端）**：`web/src/components/MathText.vue` → `utils/math.ts:renderRich`。
业务组件**禁止**裸用 `v-html`（`verify_math_formula.py` 有静态断言）。

| 渲染点 | 组件 |
| --- | --- |
| 题目详情 / 答题页 / 阅卷页 / 审核 / 标注 | `MathText`（`mode` 自动判断 HTML 或纯文本） |
| 试卷预览 / 导出预览 | `MathText mode="text"`（排版行已无标签） |
| 答题卡 | 无题干文字，只有题号与气泡，无需渲染 |
| PDF / Word | Django 侧 `math_render` 分档，见下 |

**排版宽度**：`paper/layout.py` 的 `text_width_mm` / `wrap_text_lines` 按公式**渲染宽度**
（`mathtext.tex_width_em`）估算，不按 TeX 源码字符数。`\frac{1}{2}` 是 12 个 ASCII
字符（6 个字宽），渲染出来只有 0.75 个字宽 —— 不修正会提前折行、并让选项列数误降。
**公式在折行时是不可分割的原子**（切开会让两个半边都找不到配对）。

**`paper_render_data.layout.math_render` 两档**：

| 档 | 谁用 | 行里内容 | 宽度 |
| --- | --- | --- | --- |
| `tex`（默认） | 前端预览、Node 渲染服务 | 保留 `$…$` | 按渲染宽度估算 |
| `plain` | reportlab、python-docx | 公式已降级成 `1/2`、`x²` | 按降级文本量 |

★ 两档折行结果**可以不同**，这是有意的（引擎不同，能画的字宽就不同）。
`math_render` **不在 `layout_config` 里** —— 它由调用方（预览接口 vs 导出接口）决定，
不能让客户端传一个参数就改变分页。

**渲染引擎与回退**：

```
RENDER_SERVICE_URL 配置了？ ──是──▶ POST /render（X-Render-Token）──▶ PDF
        │否 / 失败 / 超时                │
        └──────────────► reportlab（plain 档）+ issue: PAPER.FORMULA_DEGRADED
```

★ 回退**不报错**，只加一条**非阻塞** issue；失败后熔断 120s，避免每次导出白等 35 秒。
★ 有公式却没告警 = bug；没公式却告警 = 骚扰。两者都验（`verify_render_service.py` R4）。

| 配置项 | 含义 |
| --- | --- |
| `ZF_RENDER_URL` | 渲染服务地址，空 = 不启用（默认） |
| `ZF_RENDER_TOKEN` | 内网共享密钥（**不是**用户 JWT） |
| `ZF_RENDER_TIMEOUT` | 客户端超时，默认 35s（比服务端 30s 多 5s） |

**已降级文本的形态**（`strip_tex`）：`\frac{1}{2}` → `1/2`、`\frac{x+1}{2}` → `(x+1)/2`、
`\sqrt{3}` → `√3`、`x^{2}` → `x²`、`30^{\circ}` → `30°`、`\times` → `×`、
`\begin{cases}` → 用「；」分行。**降级后不允许残留反斜杠**。

---

### 5.20 本地开发凭据（v2.1 新增，2026-10-23）

**本仓库是公开的**，所以「开发库账号口令」不能写在源码里。

**唯一入口**：`scripts/_devcreds.py`。验收脚本一律调用它，不写死用户名口令：

```python
from _devcreds import admin_login

st, j = call(f"{API}/auth/login/", method="POST", body=admin_login())
```

**取值优先级**：环境变量 → `scripts/.devcreds.local` → 报错。

| 变量 | 含义 |
| --- | --- |
| `ZF_DEV_ADMIN_USERNAME` / `ZF_DEV_ADMIN_PASSWORD` | 学校平台账户（`ORG_ADMIN`） |
| `ZF_DEV_SYSADMIN_USERNAME` / `ZF_DEV_SYSADMIN_PASSWORD` | 系统管理员（`PLATFORM_ADMIN`） |

**首次配置**：

```
copy scripts\.devcreds.example scripts\.devcreds.local
# 然后填真实口令
```

`.devcreds.local` 已在 `.gitignore` 里。**没有默认值** —— 取不到就报错，
并打印上面这段配置说明。这是有意为之：静默用一个猜的口令去登录，
只会得到 401，是最难查的失败方式。

**系统管理员的初始口令**：迁移 `accounts/0005_three_tier` 不再有固定缺省值。
优先读 `ZF_SYSADMIN_PASSWORD`；没有就 `secrets.token_urlsafe(16)` 现场生成，
并在迁移日志里用一个方框打印一次。**只显示一次**，数据库里存的是哈希，事后捞不回来。

---

## 五之二、编码规则（**v1.1 放宽，替换原「只允许字母数字与点」的限制**）

**适用范围**：`knowledge_node.code`、`chapter.code`、`task.code`、`course.code`、`major.code`

| 规则 | 值 |
| --- | --- |
| 长度 | 1–80 |
| 允许字符 | 字母、数字、`-`、`_`、`.`、中文 |
| **禁止** | 空白字符、`/`、`\`、以 `.` 或 `-` 开头 |
| 唯一性范围 | `(COALESCE(org_id, 0), code)` |
| 推荐格式 | **分层编码**：`<课程码>-C<章号>-T<任务号>-K<知识点序号>` |

**实例（职教机械基础）**：

| 实体 | code | name |
| --- | --- | --- |
| 课程 | `MJJC` | 机械基础 |
| 章 | `MJJC-C1` | 第1章 金属材料与热处理 |
| 任务 | `MJJC-C1-T1` | 任务一：分析轴的力学性能 |
| 知识点 | `MJJC-C1-T1-K1` | 金属材料性能分类 |
| 知识点 | `MJJC-C1-T6-K1` | 整体热处理 |

> **编码与名称的关系**：编码是**内部稳定标识**（改名不变、可用于跨机构对齐）；名称是**展示用**（与纸质教材一致）。两者都存，不允许只存其一。
>
> **`seq_in_task` 的用途**：保留教材原始序号（如「1.」「2.」），用于教师对照纸质书；`name` 存去掉序号的纯名称（`整体热处理` 而非 `1. 整体热处理`）。

---

## 六、错误码总表（**跨端唯一口径**）

**格式**：`<模块>.<语义>`，全大写下划线。HTTP 状态码与错误码分离——同一错误码可映射不同状态码时要显式声明。

| 错误码 | HTTP | 含义 | 前端处理 |
| --- | --- | --- | --- |
| `AUTH.INVALID_CREDENTIALS` | 401 | 账号或密码错误 | 提示并停留 |
| `AUTH.TOKEN_EXPIRED` | 401 | token 过期 | 静默刷新，失败则跳登录 |
| `AUTH.FORBIDDEN` | 403 | 无权限 | 提示无权限 |
| `ORG.MISMATCH` | 403 | 跨机构访问 | 记录告警（可能是攻击） |
| `COMMON.NOT_FOUND` | 404 | 资源不存在 | |
| `COMMON.VALIDATION_ERROR` | 400 | 参数校验失败 | 表单字段级报错 |
| `COMMON.RATE_LIMITED` | 429 | 触发限流 | 提示稍后重试 |
| `COMMON.IDEMPOTENT_CONFLICT` | 409 | 幂等键冲突（已有进行中请求） | 提示"正在处理" |
| `QUESTION.DUPLICATE` | 409 | 题目重复 | 提示并给"仍要新建" |
| `QUESTION.NOT_APPROVED` | 409 | 题目未审核，不可组卷 | 提示去审核 |
| `QUESTION.NO_KNOWLEDGE_TAG` | 400 | 缺少知识点标签 | 提示补标签 |
| `QUESTION_BANK.INVALID` | 400 | 题库不存在/不属于本机构/类型不允许 | 展示可用题库 |
| `QUESTION_BANK.INVALID_STATE` | 409 | 当前状态不允许该操作（提交/审核/下载/下架） | 展示当前状态与下一步（如「先撤回为草稿」） |
| `QUESTION_BANK.REASON_REQUIRED` | 400 | 驳回/下架必须填理由 | 输入框校验 |
| `QUESTION_BANK.SOURCE_UNAVAILABLE` | 409 | 来源公共题已下架/未通过，不可下载 | 提示「该题已从公共库下架」 |
| `INGEST.PARSE_FAILED` | 422 | 文件解析失败 | 提示换文件或手动录入 |
| `INGEST.SEGMENT_UNCERTAIN` | 200 | 切题结果需人工校对 | 跳转校对页（**非错误**） |
| `FIGURE.PENDING_CONFIRM` | 409 | 存在未确认配图 | 跳转配图确认页 |
| `RENDER.UNAUTHORIZED` | 401 | 渲染服务令牌无效（**仅内网，Django 不转发给用户**） | Django 侧直接回退 reportlab |
| `RENDER.INVALID_LAYOUT` | 400 | 排版 JSON 不合法（同上） | 回退 + 记日志 |
| `RENDER.TIMEOUT` | 504 | 渲染超时（同上） | 回退 + 熔断 120s |
| `RENDER.FAILED` | 500 | 渲染失败（同上） | 回退 |
| `FIGURE.BATCH_CONFIRM_REJECTED` | 400 | 批量确认条件不满足 | 展示不满足的原因 |
| `FIGURE.MISSING_REFERENCED` | 409 | 题干提到图但无配图 | 阻断定稿，跳转处理 |
| `FIGURE.SPLIT_BY_PAGE` | 409 | 配图将被分页切断 | 跳转调整 |
| `FIGURE.DUPLICATE_ASSET` | 409 | 手动截图命中去重（pHash 相似） | 弹层展示已有图 + 「复用这张图」/「仍要新建」 |
| `SHEET.BIND_MISMATCH` | 409 | 答题卡与试卷结构不匹配 | 展示具体差异 |
| `SHEET.STUDENT_NO_OVERFLOW` | 400 | 学号区超出页面 | 展示排版建议 |
| `SHEET.TEMPLATE_VERSION_IMMUTABLE` | 409 | 模板版本只增不改，不能原地改版 | 提示「另存为新版本」 |
| `ASSIGNMENT.WINDOW_CLOSED` | 409 | 不在作答时间窗内 | 学生端提示「本次作业已于 X 关闭」+ 教师联系方式 |
| `ASSIGNMENT.ALREADY_SUBMITTED` | 409 | 已提交且不允许补交 | 显示提交时间与结果入口，禁用作答 |
| `ASSIGNMENT.PAPER_NOT_FINALIZED` | 409 | 试卷未定稿，不能发布 | 提示并跳转试卷校验（T38） |
| `SCAN.ANCHOR_NOT_FOUND` | 422 | 定位点识别失败 | 提示重拍 |
| `SCAN.STUDENT_NO_UNMATCHED` | 200 | 学号不在名册 | 跳转指定学生（**非错误**） |
| `SCAN.STUDENT_NO_AMBIGUOUS` | 409 | 学号重复/多涂 | 进人工队列 |
| `GRADING.SCORE_EXCEED_MAX` | 400 | 分数超满分 | 字段级报错 |
| `GRADING.NO_RESPONSE` | 409 | 无作答记录，无法批改 | 提示先录作答 |
| `GRADING.ASSIGNMENT_NOT_GRADED` | 409 | 成绩未确认，不能生成练习 | 提示先确认成绩 |
| `PAPER.CANDIDATE_INSUFFICIENT` | 409 | 候选题目不足 | 展示缺口清单 |
| `PAPER.EQUIVALENCE_FAILED` | 409 | AB 卷等值不达标 | 展示差异题与建议 |
| `AI.PROVIDER_UNAVAILABLE` | 503 | 模型服务不可用 | 提示手动完成 |
| `AI.SCHEMA_INVALID` | 502 | 输出不符合 Schema | 内部重试，超限提示 |
| `AI.BUDGET_EXCEEDED` | 402 | 超出机构预算 | 提示联系管理员 |
| `AI.KB_INSUFFICIENT` | 200 | 知识库依据不足，拒绝生题 | 提示去补充知识库（**非错误**） |
| `AI.CITATION_INVALID` | 502 | 引用了不存在的依据 | 内部重试，超限标记 |
| `EXPORT.TASK_RUNNING` | 409 | 同类导出任务进行中 | 提示等待 |
| `EXPORT.LAYOUT_ISSUES` | 409 | 导出前排版检查未通过 | 展示问题清单 |

> **约定**：标注「**非错误**」的码为业务分支，HTTP 返回 200，由前端按普通响应处理。

---

## 七、异步任务返回契约（**跨端唯一口径**）

所有异步接口统一返回 `202 Accepted`：

```json
{
  "data": {
    "task_id": "b7f3e2a1-...",
    "task_type": "ingest.parse",
    "status": "PENDING",
    "queue": "p0_critical",
    "progress": 0,
    "created_at": "2026-10-01T12:00:00+08:00"
  }
}
```

任务状态查询 `GET /api/v1/tasks/{task_id}/`：

```json
{
  "data": {
    "task_id": "b7f3e2a1-...",
    "task_type": "ingest.parse",
    "status": "PROGRESS",
    "progress": 45,
    "message": "正在识别第 2/3 页的配图",
    "result": null,
    "error": null,
    "started_at": "...",
    "finished_at": null
  }
}
```

`status` ∈ `PENDING` / `PROGRESS` / `SUCCESS` / `FAILURE` / `REVOKED`。

**约定**：

- `progress` 为 0–100 整数；无进度概念的任务恒为 0，`status=SUCCESS` 时置 100。
- `result` 结构由各任务类型定义，见 05 文档对应接口。
- 前端轮询间隔：前 10 秒每 1 秒，之后每 3 秒，超过 5 分钟降为每 10 秒。

---

## 八、时间与数值约定（**强制**）

| 项 | 约定 |
| --- | --- |
| 时区 | 全系统 `Asia/Shanghai`；数据库存 `timestamptz`（UTC 存储，展示转换） |
| 时间格式 | API 传输 ISO 8601 带时区；**禁止传时间戳整数** |
| 日期 | `YYYY-MM-DD` |
| 百分比 | 存 **0–1 小数**（`numeric(5,4)`），**不存 0–100**；前端展示时 ×100 |
| 得分率 | 0–1 小数 |
| 难度 | 1–5，允许 0.5 步长（`numeric(4,2)` 存 `difficulty_stat`/`difficulty_effective`） |
| 掌握度 | 0–1 小数（`numeric(4,3)`） |
| 置信度 | 0–1 小数（`numeric(4,3)`） |
| 坐标 | **相对比例 0–1**（答题卡 `layout_json`），**禁止像素** |
| 金额 | 存 `numeric(10,4)`，单位元 |
| token 数 | 整数 |

---

## 九、ID 与对外标识约定（**强制**）

| 场景 | 规则 |
| --- | --- |
| 数据库主键 | `bigserial`，不出现在公开分享链接中 |
| 对外唯一标识 | 业务表带 `uuid` 字段；API 路径参数与响应均使用 **uuid 或自增 id**——**统一用自增 id**（简化），但**分享/扫码类 token 必须用不可枚举的随机串** |
| 学生作答 token | `assignment.publish_token`：32 字节 URL-safe 随机串，不含任何 ID 信息 |
| 扫码进入 | `/s/{publish_token}`，**不带 student_id**（学生身份在页面内绑定） |
| 签名 URL | 对象存储访问一律用签名 URL，有效期 ≤ 5 分钟（扫描原图 ≤ 5 分钟） |
| 幂等键 | 格式 `<task>:<business_id>:<prompt_version>`（**确定性、可复现，不用 UUIDv4**），由服务端在发起调用前计算 |

---

## 十、跨文件一致性规则（**变更管理**）

1. **单一事实来源**：数据库字段以 02 文档为唯一来源；接口以 05 文档为唯一来源；阈值/配置以 13 文档为唯一来源。其他文档引用而不重定义。
2. **改字段的流程**：改 02 文档 → 生成迁移 → 更新 05 文档的响应示例 → 更新 01 文档的类型定义 → 通知前端。
3. **改接口的流程**：改 05 文档 → 若破坏兼容则升 `/api/v2/` → 更新前端 `api/` 封装。
4. **改阈值的流程**：改 13 文档 → 同步数据库默认值 → 机构级已覆盖的值不受影响 → 记录变更日志。
5. **新增状态枚举**：必须回写本文第五节，否则 CI 的枚举一致性测试会失败。
6. **禁止事项**：在代码里写魔法数字（阈值必须来自配置）、在组件里硬编码颜色字号（必须来自设计令牌）、在业务层直接调模型厂商 SDK（必须走 `apps/ai`）。

---

## 十一、与设计文档的条款映射

技术文件不重复设计文档的**理由与取舍**，只落地**实现规格**。需要理解"为什么这么做"时，查设计文档对应章节：

| 设计文档章节 | 对应技术文件 |
| --- | --- |
| 4 总体架构、4.4 队列分级 | 00 |
| 5 视觉设计系统、5.6 AI 味清单 | 11 |
| 6 AI 编排层、6.5 防幻觉 | 06 |
| 7.1–7.3 知识体系与知识库、7.5–7.6 切题与去重 | 07 |
| 7.4 配图识别专项（含 7.4.8 逐图确认） | 07 |
| 7.7 AI 能力与生题硬规则 | 06 |
| 7.8 难度体系 | 09 |
| 8 组卷（Fisher-Yates、等值、健康度） | 09 |
| 9.2 答题卡与学号区、9.3 OMR、9.4 仲裁 | 08 |
| 9.5–9.7 在线作答、双通道合并 | 05、08 |
| 10 批改（客观/填空/逐题录分） | 09 |
| 11 学情与个性化练习（含 PDF 导出） | 10 |
| 12 数据模型 | 02 |
| 13 接口 | 05 |
| 14 页面 | 11 |
| 15 非功能 | 00、04 |
| 16 测试策略 | 12 |
| 17 部署 | 04 |
| 18 成本 | 04（附录） |
| 19 排期与验收标准 | 03、12 |
| 20 风险、21 待确认 | 03（风险转任务） |
| 22 附录 A–I | 13（配置与提示词）、11（令牌）、08（答题卡规范） |

---

## 十二、文件集维护规则

| 项 | 规则 |
| --- | --- |
| 变更方式 | 小改直接提交并记录在文末「变更记录」；结构性变更需提 PR 由技术负责人评审 |
| 版本对齐 | 文件集版本号跟随设计文档版本（当前 v1.0 ↔ 设计文档 v2.1） |
| 废弃标记 | 废弃章节加 `> ⚠ 已废弃（替代：见 xx）`，**不删除**，保留追溯 |
| 图表 | 统一用 Mermaid（流程图/状态图/时序图/ER 图），禁止贴图片（不可 diff） |
| 代码示例 | 必须可运行或明确标注为伪代码；伪代码用 `text` 语言标记而非 `python` |
