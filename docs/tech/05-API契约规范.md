# 05 · API 契约规范

> 本文件是「知否」全部 HTTP 接口的**唯一事实来源**（README 第十节第 1 条）。字段以 [02-数据模型与DDL.md](02-数据模型与DDL.md) 为唯一来源，阈值以 [13-配置项与提示词库.md](13-配置项与提示词库.md) 为唯一来源，本文引用而不重定义。
> 上游依据：[知否-开发设计文档-v2.1.md](../知否-开发设计文档-v2.1.md) 第 13 章、7.4.8 节、第 9 章、10.5 节、11.7.5 节。共享约定见 [README.md](README.md)；冲突时以 README 为准并回写。

| 项目 | 内容 | 项目 | 内容 |
| --- | --- | --- | --- |
| 文件版本 | v1.1（对齐设计文档 v2.1；2026-10-17 补记需求 3 已实现接口） | 接口版本 | `/api/v1/` |
| 适用端 | 教师端 Web、学生端 H5、手机扫描端 H5 | 目标读者 | 后端、前端、算法、测试 |

---

## 一、通用约定

### 1.1 基础路径与全局格式

| 项 | 约定 |
| --- | --- |
| 基础路径 | `/api/v1/`（README 第二节） |
| 学生端 | 主体在 `/api/v1/` 下；扫码入口 `/api/v1/s/{publish_token}/`，**不带 student_id**（学生在页面内绑定，README 第九节） |
| 媒体类型 | `application/json; charset=utf-8`；小文件 `multipart/form-data`；大文件预签名直传（1.8） |
| 时区 / 时间 | `Asia/Shanghai`；ISO 8601 带时区；**禁止时间戳整数**（README 第八节） |
| 比例类 | 百分比 / 得分率 / 掌握度 / 置信度一律 0–1 小数，前端展示时 ×100（README 第八节） |
| 坐标 | `layout_json`、`bbox` 一律相对比例 0–1，**禁止像素**（README 第八节、12.6） |
| 难度 | 1–5，允许 0.5 步长（`numeric(4,2)`） |
| ID | 路径参数与响应统一用**自增 id**；分享/扫码类用不可枚举随机串（`publish_token`、`assignment_token`、`session_token`） |
| 字符集 | 全站 UTF-8；回答中的公式用 LaTeX 字符串（`$...$`），不传图片 |

### 1.2 统一响应包装

```jsonc
// 成功（200 / 201 / 204）
{ "data": { "id": 10231, "title": "高二物理 月考 A 卷" } }
```

```jsonc
// 列表（data 内固定分页信封）
{ "data": { "count": 1240, "page": 1, "page_size": 20, "total_pages": 62,
            "results": [ { "id": 10231, "title": "高二物理 月考 A 卷" } ] } }
```

```jsonc
// 失败
{ "error": { "code": "SHEET.BIND_MISMATCH",
             "message": "该模板是为 20 道客观题设计的，当前试卷有 15 道客观题，无法直接绑定。",
             "detail": { "template_objective_item_count": 20, "paper_objective_item_count": 15,
                         "suggestions": ["新建模板", "选择其他模板", "修改试卷题型配额"] } } }
```

```jsonc
// 业务分支（README 第六节标注「非错误」的码）：HTTP 200，不用 error 包装
{ "data": { "ok": false, "code": "AI.KB_INSUFFICIENT",
            "message": "该知识点（动量守恒）的知识库内容不足（当前 1 条，至少需要 2 条），无法保证生成质量。",
            "suggestion": "kb_entry", "kb_hit_count": 1, "kb_min_entries": 2 } }
```

约定：

1. `error.detail` 为对象或 `null`；**机器可读信息不得只写在 `message`**。
2. `message` 面向教师，必须说明「怎么办」（14.3）；列表页错误还必须给 `detail` 中的定位信息（如 `id`）。
3. 业务分支码固定为 `INGEST.SEGMENT_UNCERTAIN` / `SCAN.STUDENT_NO_UNMATCHED` / `AI.KB_INSUFFICIENT` 三个，恒 `ok=false`、HTTP 200；`ok=true` 时省略 `code`。
4. HTTP 状态码与错误码分离；同一码映射不同状态码时本文逐接口声明。

### 1.3 认证

| 场景 | 机制 | 有效期 |
| --- | --- | --- |
| 教师端 / 管理端 | JWT：`Authorization: Bearer <access_token>` | Access **2h**，Refresh **7d** |
| 刷新 | `POST /api/v1/auth/refresh/` | 成功签发新 access；refresh 过期 → `AUTH.TOKEN_EXPIRED` |
| 学生作答 | 扫码 URL 携带一次性 `assignment_token`（= `assignment.publish_token`，32 字节 URL-safe 随机串） | 绑定后换取 `session_token`（存 `assignment_student.session_token`） |
| 手机扫描端 | 教师 JWT（长时扫描需静默刷新） | 同教师端 |

```json
{ "sub": "2048", "org_id": 17, "role": "TEACHER", "exp": 1790860800, "iat": 1790853600, "jti": "3e1c..." }
```

- `role` ∈ `ORG_ADMIN` / `TEACHER` / `ASSISTANT` / `STUDENT`（12.2 `user.role`）；前端只读载荷，不自行构造。
- 机构隔离：`org_id` **只取自 token**，不接受客户端传入；越权返回 `ORG.MISMATCH`（403）并记录告警。
- 学生 `session_token` 与 `assignment_id + student_id` 绑定，**不得**访问其他任务（16.4 权限用例）。

### 1.4 分页、排序与筛选

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `page` | int | 1 | 1 起，`page=0` → 400 |
| `page_size` | int | 20 | 上限 100，超出 → `COMMON.VALIDATION_ERROR` |
| `ordering` | string | 各接口声明 | 字段名，`-` 前缀降序（如 `-created_at`） |
| `q` | string | — | 模糊搜索（`question.stem_text` 走 `gin_trgm_ops`） |

筛选参数一律用数据库字段名（`status`、`type`、`subject`、`grade_band`、`review_status`…），多值用重复参数 `?status=APPROVED&status=PENDING_REVIEW`。大列表必须服务端筛选排序，**禁止前端全量拉取后本地筛选**。

### 1.5 幂等

| 项 | 约定 |
| --- | --- |
| 请求头 | `Idempotency-Key: <value>` |
| 取值 | `<task>:<business_id>:<prompt_version>`，其中 `<business_id>` 由前端生成 **UUIDv4**（同时满足 README 第九节两条表述，见 G-2）。例：`ai.generate_question:6f1c2a94-7d3e-4b1a-9c62-2f0a8e5b71cd:v3` |
| 服务端行为 | 首次执行并记录响应；重复请求返回**首次响应体**（200/202），**不重复执行、不重复计费** |
| 冲突 | 键相同且首请求仍在执行 → `COMMON.IDEMPOTENT_CONFLICT`（409），`detail.retry_after_ms` 给建议等待 |
| 有效期 | 24 小时 |
| 必须携带 | `POST /ai/**`、`POST /scan/cards/quick/`、`POST /papers/{id}/auto-compose/`、`POST /papers/{id}/compose-by-diagnosis/`、`POST /papers/{id}/sheet-binding/`、`POST /assignments/{id}/publish/`、`POST /grading/assignments/{id}/subjective-entry/**`、`POST /practice/generate/`、`POST /practice/export/`、`POST /question-figures/batch-confirm/`、`POST /question-figures/{id}/capture/` |

### 1.6 异步任务契约

返回结构、`status` 取值（`PENDING` / `PROGRESS` / `SUCCESS` / `FAILURE` / `REVOKED`）、`progress` 语义、前端轮询间隔**全部以 README 第七节为准**。本文只补 README 委托的两件事：

**(1) `task_type` 注册表**（新增任务类型必须先改本表；任务 `result` 结构见本文第三节）：

| task_type | 队列 | 触发接口 | result |
| --- | --- | --- | --- |
| `ingest.parse` | `p0_critical` | `POST /ingest/batches/` | §3.1 |
| `ingest.figure_extract` | `p0_critical` | `POST /ingest/batches/` 内部阶段 | §3.1 |
| `kb.bulk_import` | `p1_generate` | `POST /kb/entries/bulk-import/` | §2.4 |
| `ai.generate_question` | `p1_generate` | `POST /ai/questions/generate/` | §3.2 |
| `ai.gen_explanation` / `ai.polish_stem` / `ai.design_distractors` / `ai.calibrate_difficulty` / `ai.tag_question` / `ai.kb_cite` | `p1_generate` | 对应 `/ai/questions/{id}/*` | §3.2 |
| `sheet.generate_template` | `p1_generate` ※未在设计文档指定，见 G-1 | `POST /sheet/templates/` | §3.3 |
| `omr.recognize_batch` | `p0_critical` | `POST /scan/batches/{id}/process/` | §3.4 |
| `grading.auto_grade` | `p0_critical` | `POST /grading/assignments/{id}/auto-grade/` | §3.5 |
| `grading.recompute_after_score_change` | `p2_recompute` | `POST /grading/assignments/{id}/recompute/` | §3.5 |
| `analytics.full_recompute` | `p2_recompute` | 定时 / `POST /grading/assignments/{id}/recompute/` | §3.5 |
| `analytics.report_export` | `p2_recompute` | `POST /analytics/reports/export/` | §3.3 |
| `practice.generate` | `p1_generate` | `POST /practice/generate/` | §3.6 |
| `practice.export` | `p1_generate` | `POST /practice/export/` | §3.7 |
| `media.asset_cleanup` | `p2_recompute` | 定时 | — |

> **队列名不得新增**（README 第二节）。AI 任务值风格为小写下划线（`ocr_question`，6.1），流水线任务值风格为点分（`ingest.parse`，README 第七节示例）——README 第四节措辞需修正，见 G-13。

**(2) 任务失败与错误码映射**：`status=FAILURE` 时 `error.code` 必须取自本文第五节总表（如解析失败 → `INGEST.PARSE_FAILED`）；**禁止**在 `error` 里放裸字符串。

### 1.7 限流

维度：IP、机构、用户、接口。以下为**建议默认值**，实际数值以 13 文档为准并支持机构级覆盖（README 第十节第 4 条、设计 15.3）。

| 接口组 | 维度 | 建议默认 | 触发后 |
| --- | --- | --- | --- |
| `POST /auth/login/` | IP + username | 10 次/分钟/IP；同账号连败 5 次锁 10 分钟 | `COMMON.RATE_LIMITED`（429） |
| `/s/{token}/**` | IP + token | 60/分钟/IP；同 token 20/分钟 | 429 |
| `POST /scan/cards/quick/` | 教师 | 30/分钟（对应 5–8 秒/张拍摄节奏） | 429 |
| `POST /scan/upload-url/` | 教师 | 60/分钟 | 429 |
| `POST /ai/**` | 机构 + 用户 | AI 限流器（6.6）+ 机构 `daily_budget_yuan` 护栏 | 429 / `AI.BUDGET_EXCEEDED`（402） |
| 全局 | 用户 | 600/分钟 | 429 |

响应头 `X-RateLimit-Limit` / `-Remaining` / `-Reset`（Unix 秒）；429 必须给 `detail.retry_after_s`。

### 1.8 大文件上传与预签名直传

设计 13.1：**图片上传走预签名 URL 直传对象存储，不经应用服务器**（手机扫描每张 1–3 MB）。

| 项 | 约定 |
| --- | --- |
| 适用 | `SCAN_RAW`、`UPLOAD_SOURCE`、`QUESTION_FIGURE`、`KNOWLEDGE_ATTACH` 上传；导出包为下行 |
| 小文件 | ≤ 10 MB 的单文件表单场景（如导入一份 Word）可直接 `multipart/form-data` 由应用中转 |
| 大文件 / 批量 | `POST /scan/upload-url/` 取签名 → `PUT` 到对象存储 → 把 `oss_key` 提交给业务接口 |
| 有效期 | 签名 URL **≤ 5 分钟**（含扫描原图，README 第九节、15.3） |
| key 前缀 | `zf/{org_id}/{kind}/...`（README 第二节） |
| 安全 | 对象存储私有读；下载一律签名 URL；客户端**不得**缓存或转发签名 URL；每次访问写审计 |
| 校验 | 类型白名单 + 大小限制 + **魔数校验**（15.3） |

```jsonc
// POST /api/v1/scan/upload-url/ 请求
{ "batch_id": 9031, "kind": "SCAN_RAW", "filename": "card_0007.jpg",
  "mime": "image/jpeg", "byte_size": 1734210, "sha256": "9f2c...e1" }
```

```jsonc
// 200 OK
{ "data": { "upload_url": "https://oss.../zf/17/SCAN_RAW/2026/10/9f2c...e1.jpg?Signature=...",
            "method": "PUT", "headers": { "Content-Type": "image/jpeg" },
            "oss_key": "zf/17/SCAN_RAW/2026/10/9f2c...e1.jpg", "asset_id": 558201,
            "expires_at": "2026-10-01T12:05:00+08:00" } }
```

### 1.9 批量接口

设计 13.1：扫描上传、成绩录入、题库批量操作都提供批量接口。约定：

- 批量请求体用数组字段（`items` / `entries` / `scores`），单次上限 500 条，超出 → 400。
- 逐条返回结果，**不允许只回总数**；默认非事务（部分成功可用），需要全或无的接口单独声明。

```json
{ "data": { "succeeded": 42, "failed": 1,
            "results": [ { "index": 0, "student_id": 3312, "ok": true, "score_result_id": 881023 },
                         { "index": 41, "student_id": 3398, "ok": false, "code": "GRADING.SCORE_EXCEED_MAX",
                           "message": "22 题满分 10 分，录入 12 分" } ] } }
```

### 1.10 权限记号

| 记号 | 含义 |
| --- | --- |
| `PUBLIC` | 无需认证（仅登录与学生扫码入口），强制限流 |
| `STUDENT` | 学生作答会话（`assignment_token` 换取的 `session_token`） |
| `TEACHER` | 机构内 `TEACHER` / `ASSISTANT` / `ORG_ADMIN` 均可 |
| `TEACHER_WRITE` | 教师可写；`ASSISTANT` 仅可写批改 / 扫描 / 录分类接口 |
| `ORG_ADMIN` | 仅机构管理员（设置、预算、审计、提示词模板） |

上述全部记号均受机构隔离约束。学生只能访问本人 `assignment_student` 范围数据。

---

## 二、接口清单

> 本清单 = 设计文档 13.2 节全部接口 + 7.4.8.6 节新增接口。`异步` 列写 `—` 为同步，写 `202 <task_type>` 为异步。标 `†` 者为**同一能力的两个入口**（13.2 与 7.4.8.6 各定义一个），实现必须复用同一 Service 层，见 G-3。

### 2.1 认证与机构

| 方法 | 路径 | 权限 | 说明 | 异步 |
| --- | --- | --- | --- | --- |
| POST | `/auth/login/` | PUBLIC | 账号密码登录，返回 access/refresh | — |
| POST | `/auth/refresh/` | PUBLIC | 刷新 access | — |
| GET | `/org/current/` | TEACHER | 当前机构信息（`ai_config` / `grading_config` 非敏感子集） | — |
| GET/PATCH | `/org/settings/` | ORG_ADMIN | 机构设置（阈值、默认答题卡样式、预算） | — |
| GET | `/audit/logs/` | ORG_ADMIN | 审计日志查询 | — |

### 2.2 班级与学生

| 方法 | 路径 | 权限 | 说明 | 异步 |
| --- | --- | --- | --- | --- |
| GET/POST | `/classrooms/` | TEACHER | 班级列表 / 新建 | — |
| GET/PATCH | `/classrooms/{id}/` | TEACHER | 班级详情 / 修改 | — |
| GET/POST | `/classrooms/{id}/students/` | TEACHER_WRITE | 名册列表 / 新增 | — |
| POST | `/classrooms/{id}/students/import/` | TEACHER_WRITE | 名册导入（Excel） | — |
| POST | `/classrooms/{id}/students/validate/` | TEACHER_WRITE | **导入前校验学号唯一性与位数一致性**（13.2、9.2.4.1） | — |
| PATCH/DELETE | `/classrooms/{id}/students/{sid}/` | TEACHER_WRITE | 修改 / 移出（`status=LEFT`，不删记录） | — |

### 2.3 知识点与章节

| 方法 | 路径 | 权限 | 说明 | 异步 |
| --- | --- | --- | --- | --- |
| GET | `/knowledge/templates/` | TEACHER | 平台知识点模板 | — |
| POST | `/knowledge/templates/{id}/apply/` | TEACHER_WRITE | 应用模板到本机构（深拷贝） | — |
| GET/POST | `/knowledge/nodes/` | TEACHER | 知识点树 | — |
| PATCH/DELETE | `/knowledge/nodes/{id}/` | TEACHER_WRITE | 修改 / 软删除 | — |
| POST | `/knowledge/nodes/merge/` | TEACHER_WRITE | 节点合并（防同义词污染） | — |
| POST | `/knowledge/edges/` | TEACHER_WRITE | 建立前置/关联关系（`PREREQUISITE` 无环校验） | — |
| GET | `/knowledge/coverage/` | TEACHER | 题库对知识点的覆盖统计 | — |
| GET/POST | `/chapters/` | TEACHER | 章节列表 / 新建 | — |
| GET | `/chapters/{id}/knowledge/` | TEACHER | 章节关联的知识点 | — |
| POST | `/chapters/templates/apply/` | TEACHER_WRITE | 应用章节模板 | — |

### 2.4 知识库

| 方法 | 路径 | 权限 | 说明 | 异步 |
| --- | --- | --- | --- | --- |
| GET/POST | `/kb/entries/` | TEACHER | 条目列表（学科/章节/知识点/类型/状态筛选）/ 新建 | — |
| GET/PATCH/DELETE | `/kb/entries/{id}/` | TEACHER_WRITE | 详情 / 修改 / 软删除（写审计） | — |
| POST | `/kb/entries/{id}/verify/` | TEACHER_WRITE | 审核（写 `verified_by`，提升检索权重） | — |
| POST | `/kb/entries/bulk-import/` | TEACHER_WRITE | 从文档批量导入 | `202 kb.bulk_import` |
| GET | `/kb/import/{task_id}/` | TEACHER | 导入进度与**切分结果** | — |
| POST | `/kb/import/{task_id}/confirm/` | TEACHER_WRITE | 确认切分与归类（**不自动入库**，7.2.1） | — |
| POST | `/kb/search/` | TEACHER | 检索（调试与人工查阅） | — |
| GET | `/kb/health/` | TEACHER | 知识点覆盖率 / 条目利用率 / 采纳率 | — |
| GET | `/kb/entries/{id}/usage/` | TEACHER | 该条目被引用记录 | — |

`kb.bulk_import` 的 `result`：

```json
{ "slices": [ { "slice_index": 0, "heading_path": "第一章 动量守恒定律/第3节 动量守恒定律",
                "title": "动量守恒定律", "content_text": "如果一个系统不受外力……",
                "suggested_entry_type": "THEOREM", "suggested_chapter_id": 214,
                "suggested_node_ids": [8812, 8813], "confidence": 0.81 } ],
  "slice_count": 37, "kb_entry_ids": null }
```

### 2.5 题库

| 方法 | 路径 | 权限 | 说明 | 异步 |
| --- | --- | --- | --- | --- |
| GET/POST | `/questions/` | TEACHER | 列表 / **新建**（§4.1） | — |
| GET/PATCH/DELETE | `/questions/{id}/` | TEACHER_WRITE | 详情 / 修改 / 软删除 | — |
| POST | `/questions/{id}/review/` | TEACHER_WRITE | 审核（`APPROVED` / `REJECTED`） | — |
| GET | `/questions/{id}/stat/` | TEACHER | 作答统计（得分率、选项分布） | — |
| POST | `/questions/{id}/similar/` | TEACHER | 相似题 | — |
| GET | `/questions/{id}/figures/` | TEACHER | 该题配图列表 | — |
| POST | `/questions/{id}/figures/` | TEACHER_WRITE | 手动添加配图（提交 `asset_id`） | — |
| PATCH/DELETE | `/question-figures/{id}/` | TEACHER_WRITE | 修改（归属/题注/分类）/ 移除（`review_status=DELETED`，不物理删） | — |
| POST | `/question-figures/{id}/recrop/` | TEACHER_WRITE | 重新裁切（**重裁即确认**，§4.4） | — |
| POST | `/question-figures/{id}/capture/` | TEACHER_WRITE | 手动截图（补遗漏图，自动确认，§4.5）† | — |
| POST | `/question-figures/{id}/replace/` | TEACHER_WRITE | 换图（借用 / 图库 / 上传，自动确认）† | — |
| POST | `/question-figures/{id}/confirm/` | TEACHER_WRITE | 确认单张（`confirm_method=CLICK`） | — |
| POST | `/question-figures/{id}/unconfirm/` | TEACHER_WRITE | 撤销确认（回 `PENDING`） | — |
| POST | `/question-figures/{id}/display-scale/` | TEACHER_WRITE | 调整显示比例（关口二） | — |
| POST | `/question-figures/batch-confirm/` | TEACHER_WRITE | **受限批量确认**（§4.6）† | — |
| POST | `/questions/export/` | TEACHER | 导出 | — |
| POST | `/questions/import/` | TEACHER_WRITE | 从 Excel 批量导入 | — |

### 2.6 AI 能力（全部只在新建/编辑题目阶段，C5）

| 方法 | 路径 | 权限 | 说明 | 异步 |
| --- | --- | --- | --- | --- |
| POST | `/ai/questions/{id}/polish/` | TEACHER_WRITE | 题干润色（返回 diff，逐句采纳） | `202 ai.polish_stem` |
| POST | `/ai/questions/{id}/explanation/` | TEACHER_WRITE | 生成解析（含答案一致性自校验） | `202 ai.gen_explanation` |
| POST | `/ai/questions/{id}/distractors/` | TEACHER_WRITE | 干扰项设计（需 `COMMON_ERROR` 依据） | `202 ai.design_distractors` |
| POST | `/ai/questions/{id}/difficulty/` | TEACHER_WRITE | 难度校准（只写 `difficulty_ai`） | `202 ai.calibrate_difficulty` |
| POST | `/ai/questions/{id}/tag/` | TEACHER_WRITE | 打标签（限定现有知识点树） | `202 ai.tag_question` |
| POST | `/ai/questions/{id}/kb-cite/` | TEACHER_WRITE | 从知识库引证 | `202 ai.kb_cite` |
| POST | `/ai/questions/generate/` | TEACHER_WRITE | **AI 生题**（§4.2） | `202 ai.generate_question` |
| GET | `/ai/generate/availability/` | TEACHER | 生题能力可用性（按知识点，7.7.5 矩阵） | — |
| POST | `/ai/questions/{id}/apply/` | TEACHER_WRITE | 采纳 AI 结果（写库 + `ai_feedback`） | — |
| GET/PATCH | `/ai/templates/`、`/ai/templates/{id}/` | ORG_ADMIN / TEACHER | 提示词模板管理 | — |
| GET | `/ai/usage/` | ORG_ADMIN | 成本与用量报表 | — |
| GET | `/ai/feedback/stats/` | TEACHER | 采纳率看板 | — |
| GET | `/ai/citation-stats/` | TEACHER | 引用有效率看板（防幻觉度量） | — |

> AI 类接口**必须流式返回**（7.7.1）：`text/event-stream`，事件 `chunk` / `done` / `error`；`202` 的 `task_id` 供非流式客户端轮询与结果落库。

### 2.7 导入

| 方法 | 路径 | 权限 | 说明 | 异步 |
| --- | --- | --- | --- | --- |
| POST | `/ingest/batches/` | TEACHER_WRITE | 上传文件创建批次（§4.3） | `202 ingest.parse` |
| GET | `/ingest/batches/{id}/` | TEACHER | 进度（含 `pending_figure_count`） | — |
| POST | `/ingest/batches/{id}/finish/` | TEACHER_WRITE | 批次完成（**有 `PENDING` 配图时 409**，见 G-17） | — |
| GET | `/ingest/batches/{id}/items/` | TEACHER | 待校对题目 | — |
| PATCH | `/ingest/items/{id}/` | TEACHER_WRITE | 校对单题 | — |
| POST | `/ingest/items/{id}/confirm/` | TEACHER_WRITE | 确认入库 | — |
| POST | `/ingest/items/{id}/discard/` | TEACHER_WRITE | 丢弃（需原因） | — |
| POST | `/ingest/batches/{id}/confirm-all/` | TEACHER_WRITE | 批量确认高置信度**题目** | — |
| GET | `/ingest/batches/{id}/figures/` | TEACHER | 批次配图列表（含归属与质量，§4.3） | — |
| GET | `/ingest/batches/{id}/figure-review/` | TEACHER | **逐图确认队列**（按题目顺序，含进度，§4.11） | — |
| PATCH | `/ingest/figures/{id}/` | TEACHER_WRITE | 修改配图归属 / 裁切范围（§4.3）† | — |
| POST | `/ingest/figures/{id}/split/` | TEACHER_WRITE | 拆分多图（`MULTI_FIGURE_MERGED`） | — |
| POST | `/ingest/figures/{id}/redraw-request/` | TEACHER_WRITE | 标记需重绘（`needs_redraw`） | — |
| POST | `/ingest/batches/{id}/figures/confirm-all/` | TEACHER_WRITE | 批量确认配图（仅限受限条件）† | — |

### 2.8 组卷

| 方法 | 路径 | 权限 | 说明 | 异步 |
| --- | --- | --- | --- | --- |
| GET/POST | `/papers/` | TEACHER | 列表 / 新建 | — |
| GET/PATCH | `/papers/{id}/` | TEACHER_WRITE | 详情 / 修改 | — |
| POST | `/papers/{id}/auto-compose/` | TEACHER_WRITE | 规则抽题（§4.7，同步 < 5s） | — |
| POST | `/papers/{id}/compose-by-diagnosis/` | TEACHER_WRITE | 学情组卷（纯算法加权，§4.7） | — |
| POST | `/papers/{id}/re-roll/` | TEACHER_WRITE | 重抽未锁定题（同 `random_seed` 可复现） | — |
| GET | `/papers/{id}/candidates/` | TEACHER | 同类候选（换题用） | — |
| POST | `/papers/{id}/items/{item_id}/replace/` | TEACHER_WRITE | 换题 | — |
| POST | `/papers/{id}/lock/` | TEACHER_WRITE | 锁定 | — |
| POST | `/papers/{id}/generate-variant/` | TEACHER_WRITE | 生成 AB / 补考卷 | — |
| GET | `/papers/{id}/equivalence/` | TEACHER | 等值校验 | — |
| POST | `/papers/{id}/preview/` | TEACHER | 预览 | — |
| GET | `/papers/{id}/export/` | TEACHER | 导出 PDF / Word | — |
| PUT | `/papers/{id}/score-allocation/` | TEACHER_WRITE | 设/清「期望总分」（传 `null` 清空） | — |
| GET | `/papers/{id}/score-allocation/` | TEACHER | **配分建议**（按目标总分反推每空分值，只读） | — |
| POST | `/papers/{id}/score-allocation/apply/` | TEACHER_WRITE | 按建议写入配分（仅 DRAFT） | — |
| GET/POST | `/sheet/papers/{id}/binding/` | TEACHER / TEACHER_WRITE | 查询 / 绑定答题卡（含校验，§4.8） | — |
| DELETE | `/sheet/papers/{id}/binding/` | TEACHER_WRITE | **解绑**（`P1-70b`）：body 传 `binding_id` 解一条、`unbind_all=true` 解全部 | — |
| POST | `/sheet/papers/{id}/binding/set-primary/` | TEACHER_WRITE | 指定**打印默认**答题卡（`P1-70b`） | — |
| GET | `/sheet/papers/{id}/binding-report/` | TEACHER | 绑定体检（卷卡签名比对 + 候选） | — |
| GET | `/papers/bank-health/` | TEACHER | 题库健康度（8.7） | — |
| GET | `/papers/{id}/figure-verify/` | TEACHER | **关口二：试卷配图全面复检**（§4.11） | — |
| POST | `/papers/{id}/figure-verify/confirm-all/` | TEACHER_WRITE | 全部确认后定稿（`FINALIZED`） | — |

### 2.9 答题卡

| 方法 | 路径 | 权限 | 说明 | 异步 |
| --- | --- | --- | --- | --- |
| GET/POST | `/sheet/styles/` | TEACHER | 样式列表（系统预设 + 机构自定义）/ 新建（可另存为） | — |
| GET/PATCH/DELETE | `/sheet/styles/{id}/` | TEACHER_WRITE | 样式详情 / 修改 / 删除 | — |
| GET | `/sheet/templates/` | TEACHER | 模板库（可用于绑定） | — |
| POST | `/sheet/templates/` | TEACHER_WRITE | 由样式生成模板（§4.9） | `202 sheet.generate_template` |
| GET | `/sheet/templates/{id}/` | TEACHER | 模板详情（含 `layout_json` 与 PDF 签名链接，§4.9） | — |
| GET | `/sheet/templates/{id}/preview/` | TEACHER | 预览（PNG） | — |
| GET | `/sheet/templates/{id}/calibration/` | TEACHER | 生成校准页（测试识别链路） | — |

### 2.10 发布任务

| 方法 | 路径 | 权限 | 说明 | 异步 |
| --- | --- | --- | --- | --- |
| POST | `/assignments/` | TEACHER_WRITE | 创建（草稿） | — |
| PATCH | `/assignments/{id}/` | TEACHER_WRITE | 修改配置 | — |
| POST | `/assignments/{id}/publish/` | TEACHER_WRITE | **发布**（生成学生记录、**学生身份行**、乱序映射、二维码，§4.10） | — |
| GET | `/assignments/{id}/students/` | TEACHER | 任务名单（`student_no` / `student_name` 来自**身份快照**，`student_login_bound` 表示能否自己登录） | — |
| GET | `/assignments/{id}/students/{sid}/` | TEACHER | 单生详情（`sid` = **身份行 id**，含乱序映射全文） | — |
| POST | `/assignments/{id}/mark-absent/` | TEACHER_WRITE | 批量标记缺考（`student_ids` = 身份行 id 列表） | — |
| GET | `/assignments/{id}/dashboard/` | TEACHER | 教师看板 | — |
| GET | `/assignments/{id}/qrcode/` | TEACHER | 作答二维码 | — |
| POST | `/assignments/{id}/close/` | TEACHER_WRITE | 提前结束 | — |
| GET | `/s/{token}/` | PUBLIC→STUDENT | 学生入口（校验时间窗，失败 → `ASSIGNMENT.WINDOW_CLOSED`） | — |
| GET | `/s/{token}/roster/` | STUDENT | 班级名册（点选认人） | — |
| POST | `/s/{token}/bind/` | STUDENT | 绑定身份（重名需学号后 4 位） | — |
| GET | `/s/session/paper/` | STUDENT | 获取卷面（含逐生乱序） | — |
| POST | `/s/session/answer/` | STUDENT | 保存单题答案 | — |
| POST | `/s/session/submit/` | STUDENT | 提交（返回未答题号） | — |
| GET | `/s/session/result/` | STUDENT | 结果（按 `answer_visibility` / `score_visibility`） | — |
| GET | `/s/session/wrong/` | STUDENT | 错题回顾 | — |

### 2.11 扫描与识别

| 方法 | 路径 | 权限 | 说明 | 异步 |
| --- | --- | --- | --- | --- |
| POST | `/scan/batches/` | TEACHER_WRITE | 创建批次（含 `capture_channel`） | — |
| POST | `/scan/upload-url/` | TEACHER_WRITE | 预签名直传 URL（手机端每张单独调用，1.8） | — |
| POST | `/scan/cards/quick/` | TEACHER_WRITE | **手机端：上传一张并立即识别**（§4.12） | 同步，目标 ≤ 2s |
| POST | `/scan/batches/{id}/process/` | TEACHER_WRITE | 网页通道：批量入队识别 | `202 omr.recognize_batch` |
| GET | `/scan/batches/{id}/` | TEACHER | 批次进度 | — |
| GET | `/scan/batches/{id}/cards/` | TEACHER | 卡片列表（可筛 `status`） | — |
| GET/PATCH | `/scan/cards/{id}/` | TEACHER / TEACHER_WRITE | 卡片详情 / 修改识别结果（逐题 + 学号，§4.13） | — |
| POST | `/scan/cards/{id}/confirm/` | TEACHER_WRITE | 确认（写 `Response` + 触发重算） | — |
| POST | `/scan/cards/{id}/void/` | TEACHER_WRITE | 作废 | — |
| POST | `/scan/cards/{id}/hold/` | TEACHER_WRITE | 存疑待定 | — |
| POST | `/scan/cards/{id}/rebind-student/` | TEACHER_WRITE | 重新指定学生（§4.14） | — |
| GET | `/scan/batches/{id}/report/` | TEACHER | 批次质量报告 | — |

### 2.12 批改

| 方法 | 路径 | 权限 | 说明 | 异步 |
| --- | --- | --- | --- | --- |
| POST | `/grading/assignments/{id}/auto-grade/` | TEACHER_WRITE | 触发自动批改 | `202 grading.auto_grade` |
| GET | `/grading/assignments/{id}/summary/` | TEACHER | 批改进度与待确认数 | — |
| GET | `/grading/assignments/{id}/review-queue/` | TEACHER | 待确认队列（填空存疑 + 扫描低置信） | — |
| GET | `/grading/assignments/{id}/roster/` | TEACHER | ★ **批改工作台的学生名单**：交卷情况 + 每生批改进度 + 总分（2026-10-17 新增，§4.10b） | — |
| POST | `/grading/review/batch/` | TEACHER_WRITE | 批量确认 / 修改（仅对显式勾选项生效） | — |
| GET | `/grading/assignments/{id}/scores/` | TEACHER | 成绩列表 | — |
| PATCH | `/grading/scores/{id}/` | TEACHER_WRITE | 修改单个分数（写审计） | — |
| GET/POST | `/grading/assignments/{id}/subjective-entry/` | TEACHER / TEACHER_WRITE | **主观题逐题录入数据 / 按题批量提交**（§4.15） | — |
| GET | `/grading/assignments/{id}/subjective-progress/` | TEACHER | 各主观题录入进度（轻量轮询，见 G-16） | — |
| POST | `/grading/assignments/{id}/subjective-entry/paste/` | TEACHER_WRITE | 整列粘贴导入（§4.16） | — |
| POST | `/grading/assignments/{id}/subjective-entry/mark-absent/` | TEACHER_WRITE | 批量标记缺考 | — |
| POST | `/grading/assignments/{id}/subjective-entry/aggregate/` | TEACHER_WRITE | 快速模式（总分分摊，`MANUAL_AGGREGATE`，§4.17） | — |
| POST | `/grading/assignments/{id}/confirm/` | TEACHER_WRITE | 确认成绩（触发学情 + 练习生成） | — |
| POST | `/grading/assignments/{id}/recompute/` | TEACHER_WRITE | 手动触发重算 | `202 grading.recompute_after_score_change` |

### 2.13 学情分析

| 方法 | 路径 | 权限 | 说明 | 异步 |
| --- | --- | --- | --- | --- |
| GET | `/analytics/classrooms/{id}/heatmap/` | TEACHER | 班级知识点热力图（§4.18） | — |
| GET | `/analytics/assignments/{id}/measurement/` | TEACHER | ★ **试卷测量指标**：难度 / 区分度 / 信度（Cronbach's α）/ 内容效度（2026-10-17 新增，§4.20） | — |
| GET | `/analytics/classrooms/{id}/top-errors/` | TEACHER | 高频错题 + 选项分布 | — |
| GET | `/analytics/students/{id}/report/` | TEACHER | 学生学情报告（§4.19） | — |
| GET | `/analytics/students/{id}/mastery-trend/` | TEACHER | 掌握度趋势（读 `diagnosis_snapshot`） | — |
| GET | `/analytics/assignments/{id}/overview/` | TEACHER | 单次任务学情总览 | — |
| POST | `/analytics/reports/export/` | TEACHER | 报告导出（班级 / 学生 / 成绩单） | `202 analytics.report_export` |

### 2.14 个性化练习

| 方法 | 路径 | 权限 | 说明 | 异步 |
| --- | --- | --- | --- | --- |
| POST | `/practice/generate/` | TEACHER_WRITE | 生成（§4.20） | `202 practice.generate` |
| GET | `/practice/sets/` | TEACHER | 练习集列表 | — |
| GET | `/practice/sets/{id}/` | TEACHER | 详情（含 `reason` 选题理由） | — |
| PATCH | `/practice/sets/{id}/items/{item_id}/` | TEACHER_WRITE | 替换 / 移除某题 | — |
| POST | `/practice/sets/{id}/approve/` | TEACHER_WRITE | 教师确认（`TEACHER_APPROVED`） | — |
| POST | `/practice/sets/batch-publish/` | TEACHER_WRITE | 批量发布为任务 | — |
| GET | `/practice/effect/` | TEACHER | 效果追踪看板 | — |
| GET | `/practice/cold-start-status/` | TEACHER | 冷启动状态 | — |
| POST | `/practice/export/` | TEACHER_WRITE | **批量导出打印包**（§4.21） | `202 practice.export` |
| GET | `/practice/export/{task_id}/` | TEACHER | 导出进度（已生成 N/M 份，§4.21） | — |
| GET | `/practice/export/{task_id}/download/` | TEACHER | 下载 ZIP / 分片包 | — |
| GET | `/practice/export/preview/` | TEACHER | 打印预览（抽 2 份 + 答题卡拼合） | — |
| GET | `/practice/export/layout-check/` | TEACHER | 导出前排版检查 | — |

### 2.15 任务与系统

| 方法 | 路径 | 权限 | 说明 | 异步 |
| --- | --- | --- | --- | --- |
| GET | `/tasks/{task_id}/` | TEACHER | 任务状态查询（结构见 README 第七节） | — |
| GET | `/health/` | PUBLIC | 健康检查（不含敏感信息） | — |
| GET | `/metrics/` | ORG_ADMIN | 指标（Prometheus 文本格式） | — |

---

## 三、异步任务的 `result` 结构（README 第七节委托本文定义）

### 3.1 `ingest.parse` / `ingest.figure_extract`

```json
{ "batch_id": 9031, "file_name": "高二物理月考.docx", "page_count": 4, "parse_path": "WORD_INLINE",
  "item_count": 25, "figure_count": 6, "figure_pending_count": 6, "segment_uncertain_count": 2,
  "ocr_confidence_avg": 0.93, "ingest_item_ids": [77001, 77002],
  "warnings": ["第 2 页第 9 题切题边界不确定"] }
```

| 字段 | 说明 |
| --- | --- |
| `parse_path` | `PDF_TEXT` / `PDF_OBJECT` / `WORD_INLINE` / `VISION`（对应 7.4.2 三条路径） |
| `figure_pending_count` | `review_status=PENDING` 的配图数；**前端必须用它渲染「还有 N 张配图未确认」**（7.4.8.1） |
| `segment_uncertain_count` | `>0` 时前端按 `INGEST.SEGMENT_UNCERTAIN` 分支跳转校对页 |

### 3.2 `ai.*` 系列

公共字段（所有 AI 任务都返回）：`model`、`prompt_version`（写 `question.ai_meta`）、`kb_entry_ids`（**引用溯源**）、`kb_hit_count`（为 0 说明拒绝了生成）、`citations`、`citation_valid`、`citation_detail`（`{"kind": "INVALID_CITATION"｜"NO_CITATION"｜"TERMINOLOGY_WARNING", "invalid_ids": ["K9"]}`）、`cost_yuan`、`elapsed_ms`。

```jsonc
// ai.generate_question
{ "questions": [ { "stem_html": "<p>关于动量守恒，下列说法正确的是（  ）</p>",
                   "stem_text": "关于动量守恒，下列说法正确的是（  ）",
                   "options": [ { "label": "A", "content_html": "系统合外力为零时动量守恒", "is_correct": true },
                                { "label": "B", "content_html": "系统合外力做功为零时动量守恒", "is_correct": false,
                                  "error_reason": "把机械能守恒条件误当作动量守恒条件" } ],
                   "answer": { "value": ["A"] }, "answer_text": "A",
                   "explanation_html": "<p>依据 [K1]，动量守恒的条件是合外力为零。</p>",
                   "difficulty_ai": 3.5, "concluded_answer": "A", "kb_citations": ["K1", "K2"] } ],
  "created_question_ids": [10455], "status": "DRAFT" }
```

```jsonc
// ai.gen_explanation
{ "concluded_answer": "A", "explanation_html": "<p>依据 [K1]……</p>",
  "steps": [ { "seq": 1, "text": "判断系统所受合外力", "citation": "K1" } ],
  "common_errors": ["误认为合外力做功为零即动量守恒"],
  "consistency": { "ok": true, "reason": null, "warn": null } }
```

```jsonc
// ai.polish_stem
{ "revised_stem_html": "<p>下列关于加速度的说法中，正确的是（  ）</p>",
  "diff": [ { "op": "replace", "old": "关于加速度说法正确的是", "new": "下列关于加速度的说法中，正确的是" } ],
  "changed_numbers": false, "reason": "补全标点与量词，未改动数值与条件" }
```

```jsonc
// ai.tag_question
{ "knowledge_tags": [ { "node_id": 8812, "path": "力学/牛顿运动定律/牛顿第二定律", "weight": 1.0 },
                      { "node_id": 8830, "path": "力学/动力学/受力分析", "weight": 0.4 } ],
  "question_type": "SINGLE_CHOICE", "cognitive_level": "APPLY", "suggested_score": 4,
  "suggest_new_node": { "name": "非惯性系", "parent_id": 8815, "reason": "题目涉及惯性力，现有树无对应节点" } }
```

```jsonc
// ai.design_distractors
{ "options": [ { "label": "B", "content_html": "速度变化越快，加速度越小",
                 "error_reason": "混淆加速度与速度变化率的定义" } ],
  "degraded": false, "degrade_reason": null }
```

硬约束：`knowledge_tags[].node_id` **必须存在于本次注入的节点列表**，否则任务 `FAILURE`（7.7.2 硬校验）；`suggest_new_node` 只是**建议**，由教师决定是否新增；`changed_numbers=true` 时前端必须警示并禁止一键采纳；`degraded=true` 表示无 `COMMON_ERROR` 依据，降级为「不生成，仅提示」（7.7 第 3 项）。

### 3.3 `sheet.generate_template` / `analytics.report_export`

```json
{ "sheet_template_id": 3301, "version": 3, "page_count": 2, "objective_item_count": 20,
  "asset_id": 771002, "student_no": { "digits": 8, "layout": "SINGLE_ROW", "per_row": null },
  "layout_issues": [] }
```

`layout_issues` 非空即**学号区排版约束校验未通过**（9.2.4.1），此时任务 `FAILURE`、`error.code=SHEET.STUDENT_NO_OVERFLOW`，`detail.issues[]` + `detail.suggestions[]`（改多行 / 减位数 / 改横向纸张）。

```json
{ "report_type": "CLASSROOM", "asset_id": 660201,
  "download_url": "https://oss.../zf/17/REPORT_PDF/2026/10/report_4412.pdf?Signature=...",
  "expires_at": "2026-10-01T12:55:00+08:00", "include_rank": false }
```

> `include_rank` 默认 `false`（11.9：学生报告默认不含排名，由机构开关决定）。

### 3.4 `omr.recognize_batch`

```json
{ "batch_id": 9033, "capture_channel": "UPLOAD", "total_cards": 54, "recognized_cards": 54,
  "auto_ok_cards": 51, "review_cards": 3, "failed_cards": 0,
  "quality_report": { "fail_reason_distribution": { "ANCHOR_NOT_FOUND": 0, "ANCHOR_DEVIATION": 1,
                                                    "STUDENT_NO_AMBIGUOUS": 1, "LOW_CONFIDENCE": 1 },
                      "anchor_deviation_p95": 0.0031, "student_no_confidence_avg": 0.972 } }
```

### 3.5 `grading.auto_grade` / `grading.recompute_after_score_change` / `analytics.full_recompute`

```json
{ "assignment_id": 4410, "graded_items": 1125, "auto_graded": 1080, "needs_review": 12,
  "manual_pending": 33,
  "recomputed": { "students": 45, "affected_nodes": 17, "diagnosis_snapshots": 45, "stale_practice_sets": 2 },
  "elapsed_ms": 8420 }
```

`manual_pending` = 主观题待录入数（10.5）；`stale_practice_sets` = 成绩修正后**标记为需重评估**（`practice_set.is_stale`）而非自动重生成（10.6 第 6 条）。

### 3.6 `practice.generate`

```json
{ "requested": 45, "generated": 43,
  "skipped": [ { "student_id": 3391, "student_name": "王五", "reason": "PAPER.CANDIDATE_INSUFFICIENT",
                 "message": "知识点「自感现象」无可用题目",
                 "missing": [ { "knowledge_node_id": 8907, "node_name": "自感现象", "required": 2, "available": 0 } ] } ],
  "sets": [ { "practice_set_id": 5512, "student_id": 3312, "student_name": "张三", "serial_no": "P-2026-0012",
              "target_nodes": [ { "node_id": 8801, "node_name": "动量守恒", "mastery_at_gen": 0.38, "confidence": 0.85 } ],
              "difficulty_distribution": { "CONFIDENCE": 2, "CONSOLIDATE": 5, "CHALLENGE": 12 },
              "variant_count": 4, "structure_constraint": "SAME_STRUCTURE_AS_SOURCE" } ] }
```

约定：部分失败（`generated>0` 且 `skipped` 非空）任务仍为 `SUCCESS`，**前端必须展示缺口清单**（11.7.4 第 3 条，不允许静默失败）；全部失败（`generated=0`）任务 `FAILURE`，`error.code=PAPER.CANDIDATE_INSUFFICIENT`；题量与题型结构恒等于源试卷（C11）。

### 3.7 `practice.export`

```json
{ "practice_set_count": 45, "generated_files": 45, "failed_files": [], "generic_sheet_asset_id": 559900,
  "include_generic_sheet": true, "chunk_size": 15,
  "chunks": [ { "index": 1, "file_count": 15, "asset_id": 559911, "byte_size": 61234567 } ],
  "zip_asset_id": 559999,
  "layout_issues": [ { "practice_set_id": 5512, "student_name": "张三", "seq": 7, "flag": "SPLIT_BY_PAGE",
                       "message": "第 7 题配图会被分页切断" } ],
  "auto_fixed": false, "expires_at": "2026-10-02T12:40:00+08:00" }
```

---

## 四、重点接口完整契约

> 公共错误码（任何接口都可能返回，下文不再重复）：`AUTH.TOKEN_EXPIRED`、`AUTH.FORBIDDEN`、`ORG.MISMATCH`、`COMMON.NOT_FOUND`、`COMMON.VALIDATION_ERROR`、`COMMON.RATE_LIMITED`、`COMMON.IDEMPOTENT_CONFLICT`。

### 4.1 `POST /questions/` — 新建题目（含配图字段）

**权限** `TEACHER` ｜ **幂等** 建议 ｜ **异步** 否

```jsonc
// 请求
{ "subject": "physics", "grade_band": "SENIOR", "type": "SINGLE_CHOICE",
  "stem_html": "<p>如图所示的电路中，电源电动势为 6V，则电流为（  ）</p>",
  "stem_text": "如图所示的电路中，电源电动势为 6V，则电流为（  ）",
  "options": [ { "label": "A", "content_html": "0.5A", "content_text": "0.5A", "is_correct": false },
               { "label": "B", "content_html": "1A", "content_text": "1A", "is_correct": true },
               { "label": "C", "content_html": "2A", "content_text": "2A", "is_correct": false },
               { "label": "D", "content_html": "<img src=\"zf://asset/558202\"/>", "content_text": "[图]",
                 "is_correct": false, "image_id": 558202 } ],
  "answer": { "value": ["B"] }, "answer_text": "B",
  "explanation_html": "<p>由闭合电路欧姆定律得 I = E/R = 1A。</p>",
  "difficulty_manual": 3, "source_type": "EXTRACTED",
  "source_ref": { "file_name": "高二物理月考.docx", "page": 2, "ingest_batch_id": 9031 },
  "chapter_id": 214,
  "knowledge_tags": [ { "node_id": 8801, "weight": 1.0, "is_primary": true },
                      { "node_id": 8812, "weight": 0.3, "is_primary": false } ],
  "has_figure": true, "tags": ["月考", "电路"], "copyright": "校内使用",
  "figures": [ { "asset_id": 558201, "attach_point": "STEM", "seq_in_question": 1, "caption": "图1",
                 "figure_category": "CIRCUIT", "description": "一个直流电路图，含电源、两个电阻和一个电流表",
                 "src_path": "WORD_INLINE", "review_status": "PENDING", "display_scale": 0.8 } ] }
```

```jsonc
// 201 Created
{ "data": { "id": 10455, "uuid": "6f1c2a94-7d3e-4b1a-9c62-2f0a8e5b71cd", "status": "DRAFT",
            "source_type": "EXTRACTED", "has_figure": true, "figure_count": 1, "figure_pending_count": 1,
            "difficulty_manual": 3, "difficulty_effective": 3, "difficulty_source": "MANUAL",
            "created_at": "2026-10-01T12:20:00+08:00",
            "figures": [ { "id": 889001, "asset_id": 558201, "attach_point": "STEM", "review_status": "PENDING" } ] } }
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `type` | ✅ | `SINGLE_CHOICE` / `MULTI_CHOICE` / `TRUE_FALSE` / `FILL_BLANK` / `SHORT_ANSWER` / `CALCULATION` / `PROOF` / `ESSAY`（12.4） |
| `answer` | ✅ | JSONB；单选 `{"value":["B"]}`；多选 `{"value":["A","C"]}`；填空 `{"blanks":[{"index":1,"accept":["0.5","1/2"]}]}` |
| `options[].image_id` | — | 指向 `asset`；字段名与 README 第四节「外键 `<表>_id`」不一致，见 G-4 |
| `knowledge_tags` | ✅ 至少 1 条 | 缺省 → `QUESTION.NO_KNOWLEDGE_TAG`；底层表 `question_knowledge` 在 12.4 未定义字段，见 G-5 |
| `difficulty_manual` | ✅ | 1–5（0.5 步长）；冷启动唯一生效值（7.8） |
| `source_type` | ✅ | `ORIGINAL_HUMAN` / `AI_GENERATED` / `AI_REWRITTEN` / `EXTRACTED`；**`AI_GENERATED` 不接受客户端传入**（7.7.4 规则 2） |
| `figures[].review_status` | — | 客户端**不得**传 `CONFIRMED`，新建配图恒为 `PENDING`（服务端强制） |
| `figures[].attach_point` | ✅ | `STEM` / `OPTION_A`…`OPTION_Z` / `MATERIAL`（12.4） |
| `review_by` / `review_at` | 只读 | 由 `/questions/{id}/review/` 写入 |

**错误码**：`QUESTION.DUPLICATE`（409，`detail.similar_questions[]` + 支持 `force=true` 重放）、`QUESTION.NO_KNOWLEDGE_TAG`（400）。
**配图前置**：`asset_id` 必须先存在 → 先 `POST /scan/upload-url/`（`kind=QUESTION_FIGURE`）直传，再提交本接口。

### 4.2 `POST /ai/questions/generate/` — AI 生题（含 `KB_INSUFFICIENT` 分支）

**权限** `TEACHER` ｜ **幂等** **必须** ｜ **异步** 过门槛后 202

```jsonc
// 请求
{ "knowledge_node_ids": [8801], "chapter_id": 214, "question_type": "SINGLE_CHOICE", "difficulty": 3.5,
  "count": 1, "stem_hint": "动量守恒的条件与常见错误",
  "kb_injection": { "top_k": 8, "min_weight": 0.6, "min_entries": 2 }, "max_cost_yuan": 0.3 }
```

```jsonc
// 分支 A：知识库依据不足（HTTP 200，非错误）
{ "data": { "ok": false, "code": "AI.KB_INSUFFICIENT",
            "message": "该知识点（动量守恒）的知识库内容不足（当前 1 条，至少需要 2 条），无法保证生成质量。建议先补充知识库内容，或改用「改写现有题目」。",
            "suggestion": "kb_entry", "kb_hit_count": 1, "kb_min_entries": 2,
            "kb_entry_ids": [4412], "knowledge_node_id": 8801, "coverage_ratio": 0.62 } }
```

```jsonc
// 分支 B：通过 L1 检索门槛（HTTP 202）
{ "data": { "task_id": "c1a9f0d2-...", "task_type": "ai.generate_question", "status": "PENDING",
            "queue": "p1_generate", "progress": 0, "created_at": "2026-10-01T12:31:00+08:00",
            "kb_entry_ids": [4412, 4419], "kb_hit_count": 5 } }
```

| 约定 | 说明 |
| --- | --- |
| `kb_injection` | 覆盖 `ai_prompt_template.kb_injection`；阈值唯一来源是 13 文档，缺省用模板配置 |
| L1 门槛 | 命中（`weight ≥ kb_min_weight`）条数 < `kb_min_entries` 时**不调用生成模型**（16.4 ★用例） |
| 落库 | 恒 `status=DRAFT`、`source_type=AI_GENERATED`；`ai_meta.kb_entry_ids` + `question.kb_citations` 写引用溯源 |
| L3 校验 | `citations` 含注入列表外的编号 → 内部重试一次，再失败 `AI.CITATION_INVALID`；无任何引用 → `AI.CITATION_INVALID` + `detail.kind="NO_CITATION"` |
| 可用性 | `GET /ai/generate/availability/`（7.7.5：覆盖率 < 30% 禁用；30–60% 限制；> 60% 且题库 ≥ 500 开放） |

**错误码**：`AI.KB_INSUFFICIENT`（200 非错误）、`AI.PROVIDER_UNAVAILABLE`（503）、`AI.SCHEMA_INVALID`（502）、`AI.CITATION_INVALID`（502）、`AI.BUDGET_EXCEEDED`（402）。

### 4.3 导入：`POST /ingest/batches/`、`GET /ingest/batches/{id}/figures/`、`PATCH /ingest/figures/{id}/`

**(a) 创建批次** — 权限 `TEACHER` ｜ 异步 `202 ingest.parse`

```jsonc
// 请求（multipart/form-data，或大文件预签名后提交 oss_key）
{ "file_name": "高二物理月考.docx", "oss_key": "zf/17/UPLOAD_SOURCE/2026/10/8d1f...c2.docx",
  "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "byte_size": 2411520, "subject": "physics", "grade_band": "SENIOR",
  "source_ref": { "textbook": "人教版", "volume": "选择性必修一", "note": "2026 秋季月考" },
  "options": { "extract_figures": true, "auto_tag": true, "dedup_check": true } }
```

```jsonc
// 202 Accepted
{ "data": { "batch_id": 9031, "task_id": "b7f3e2a1-...", "task_type": "ingest.parse", "status": "PENDING",
            "queue": "p0_critical", "progress": 0, "created_at": "2026-10-01T12:00:00+08:00",
            "review_url": "/ingest/9031/review", "figure_review_url": "/ingest/9031/figure-review" } }
```

**(b) 批次配图列表** `GET /ingest/batches/{id}/figures/`

```json
{ "data": { "count": 6, "page": 1, "page_size": 20, "total_pages": 1, "pending_count": 3,
            "results": [ { "id": 889001, "ingest_batch_id": 9031, "question_id": null, "paper_item_id": null,
                           "attach_point": "STEM", "seq_in_question": 1, "asset_id": 558201,
                           "thumb_url": "https://oss.../thumb_400.jpg?Signature=...",
                           "url": "https://oss.../fig_1200.png?Signature=...",
                           "expires_at": "2026-10-01T12:36:00+08:00",
                           "src_path": "PDF_OBJECT", "source_ref": { "page": 2, "bbox": [0.152, 0.311, 0.412, 0.502], "anchor_id": null },
                           "caption": "图1", "figure_category": "CIRCUIT",
                           "description": "一个直流电路图，含电源、两个电阻和一个电流表",
                           "text_inside": ["R1", "R2", "6V"], "ocr_confidence": 0.62,
                           "quality_score": 0.72, "quality_flags": ["SUSPECT_CROP"],
                           "binding_confidence": 0.71, "binding_method": "SEQUENCE",
                           "review_status": "PENDING", "confirm_method": null, "confirm_evidence": null,
                           "confirmed_by": null, "confirmed_at": null, "crop_history": [],
                           "display_scale": 0.8, "layout_flags": [], "origin_image_id": null } ] } }
```

**(c) 修改配图归属 / 裁切** `PATCH /ingest/figures/{id}/`

```jsonc
// 请求
{ "question_id": 10455, "paper_item_id": null, "attach_point": "OPTION_D", "seq_in_question": 2,
  "caption": "图2", "figure_category": "CIRCUIT", "description": "选项 D 的电路图",
  "source_ref": { "page": 2, "bbox": [0.150, 0.310, 0.420, 0.505] }, "reason": "该图属于第 12 题题干，原归属错误" }
```

```jsonc
// 200 OK（结构同 (b) 单条，此处只列变更字段）
{ "data": { "id": 889001, "review_status": "CONFIRMED", "confirm_method": "RECROP", "asset_id": 558260,
            "crop_history": [ { "bbox": [0.150, 0.310, 0.420, 0.505], "at": "2026-10-01T12:40:00+08:00",
                                "by": 2048, "method": "RECROP" } ],
            "quality_flags": [], "layout_flags": [], "audit_log_id": 77120 } }
```

| 约定 | 说明 |
| --- | --- |
| `bbox` | 相对比例 `[x, y, w, h]`（0–1，**禁止像素**）；提交即写 `crop_history`、**重跑质检**、置 `CONFIRMED`（重裁即确认，Q4） |
| `attach_point` 变更 | 服务端重算该题 `has_figure` / `figure_count`；`reason` 必填并写审计（15.3） |
| `asset_id` 变化 | 重裁生成**新 asset，不覆盖原图**（7.4.7：印刷需 300dpi 原图） |
| 与 §4.4 的关系 | 二者共用同一 Service；`recrop` 是重裁的显式入口（G-3） |

**错误码**：`INGEST.PARSE_FAILED`（422）、`FIGURE.PENDING_CONFIRM`（409，`finish` 时）、`COMMON.VALIDATION_ERROR`（400，bbox 越界 / 宽高为 0）。

### 4.4 `POST /question-figures/{id}/recrop/` — 重裁（重裁即确认）

```jsonc
// 请求
{ "bbox": [0.150, 0.310, 0.420, 0.505], "source_page_asset_id": 558100, "rotate_deg": 0,
  "reason": "右侧疑似被截断" }
```

```jsonc
// 200 OK
{ "data": { "id": 889001, "asset_id": 558260, "url": "https://oss.../fig_1200.png?Signature=...",
            "width": 1200, "height": 828, "review_status": "CONFIRMED", "confirm_method": "RECROP",
            "confirmed_by": 2048, "confirmed_at": "2026-10-01T12:40:00+08:00",
            "crop_history": [ { "bbox": [0.152, 0.311, 0.412, 0.502], "at": "2026-10-01T11:58:00+08:00", "by": null, "method": "EXTRACT" },
                              { "bbox": [0.150, 0.310, 0.420, 0.505], "at": "2026-10-01T12:40:00+08:00", "by": 2048, "method": "RECROP" } ],
            "quality_score": 0.91, "quality_flags": [], "layout_flags": [], "asset_ref_count": 1 } }
```

约定：**不残留 `PENDING`**（同请求完成 `RECROPPED → CONFIRMED`，16.4 ★用例）；`crop_history` 追加以支持撤销（R22）；撤销走 `unconfirm`（另需提交原始 bbox 才能还原裁切）。**错误码**：`COMMON.VALIDATION_ERROR`（400，bbox 越界 / 面积过小）。

### 4.5 `POST /question-figures/{id}/capture/` — 手动截图（补遗漏图）

```jsonc
// 请求
{ "source_page_asset_id": 558100, "bbox": [0.412, 0.588, 0.330, 0.240], "question_id": 10455,
  "paper_item_id": null, "attach_point": "STEM", "seq_in_question": 2, "caption": "图2",
  "figure_category": "FORCE_DIAGRAM", "dedupe": { "on_phash_hit": "PROMPT" } }
```

```jsonc
// 201 Created
{ "data": { "id": 889020, "asset_id": 558301, "src_path": "MANUAL", "created_by": 2048,
            "created_at": "2026-10-01T12:44:00+08:00", "review_status": "CONFIRMED",
            "confirm_method": "MANUAL_CAPTURE", "quality_score": 0.86, "quality_flags": ["LOW_RESOLUTION"],
            "phash": 8812034711223301, "duplicate_candidates": [], "audit_log_id": 77121 } }
```

| 约定 | 说明 |
| --- | --- |
| 原图选择 | 用**原始上传页**（非校正图），保留更多细节（7.4.8.4 第 5 点） |
| 自动确认 | `src_path=MANUAL`、`review_status=CONFIRMED`、`confirm_method=MANUAL_CAPTURE`（16.4 ★用例） |
| 质检 | 走与自动抽取**相同**的质检与去重流程 |
| phash 命中 | `PROMPT` → `409 FIGURE.DUPLICATE_ASSET`；`REUSE` → 复用已有 `asset_id` 并新增一条 `question_image`（`ref_count` +1）；`FORCE` → 强制新建 |

### 4.6 `POST /question-figures/batch-confirm/` — 受限批量确认

**幂等** **必须**（防重复计数 `viewed_seconds`）

```jsonc
// 请求
{ "batch_id": 9031,
  "criteria": { "min_binding_confidence": 0.98, "src_paths": ["PDF_OBJECT", "WORD_INLINE"],
                "binding_methods": ["REFERENCE_TEXT"], "figure_categories": ["CIRCUIT"] },
  "viewed_seconds": 26.4, "viewed_confirmed": true, "preview_count": 12 }
```

```jsonc
// 200 OK
{ "data": { "confirmed_count": 12, "confirm_method": "BATCH", "suspicious_speed": false,
            "seconds_per_figure": 2.2,
            "confirm_evidence": { "criteria": { "min_binding_confidence": 0.98, "src_paths": ["PDF_OBJECT", "WORD_INLINE"] },
                                  "viewed_seconds": 26.4, "viewed_confirmed": true, "count": 12, "suspicious_speed": false },
            "batch_pending_count": 3, "audit_log_id": 77122 } }
```

**四个硬性拒绝条件**（`FIGURE.BATCH_CONFIRM_REJECTED`，400，`detail.reasons[]` 逐条返回原文）：

| 条件 | 拒绝文案 |
| --- | --- |
| `criteria` 缺失或含 `all: true` | 不支持全选批量确认，必须给出筛选条件 |
| `criteria.min_binding_confidence < 0.97` | 批量确认要求归属置信度 ≥ 0.97 |
| `criteria.src_paths` 含 `VISION_EXTRACT` / `MANUAL` | 批量确认仅限对象抽取路径的配图 |
| `viewed_confirmed != true` | 必须在前端确认已逐张查看 |

`viewed_seconds / count < 2.0` **不拒绝**，但置 `suspicious_speed=true` 并写入 `confirm_evidence`（R21、Q19）。前端必须先渲染一屏可见的缩略图网格，再由教师显式点击「我已逐张查看」。

### 4.7 组卷：`auto-compose` 与 `compose-by-diagnosis`

**权限** `TEACHER` ｜ **幂等** **必须**（同 seed 重放） ｜ **异步** 否（15.1：规则 < 5s、学情 < 8s）

```jsonc
// POST /papers/4412/auto-compose/
{ "random_seed": 20261001, "shuffle_questions": true, "shuffle_options": false,
  "template": { "total_score": 100,
    "sections": [ { "section_title": "一、单项选择题", "question_type": "SINGLE_CHOICE", "count": 10,
                    "score_each": 4, "difficulty_range": [1, 3], "knowledge_node_ids": [8801, 8812], "chapter_id": 214 },
                  { "section_title": "二、填空题", "question_type": "FILL_BLANK", "count": 5,
                    "score_each": 4, "difficulty_range": [2, 4], "knowledge_node_ids": [8801] } ] },
  "constraints": { "max_repeat_within_days": 90, "min_knowledge_coverage": 0.8, "difficulty_expect_range": [0.6, 0.75] } }
```

```jsonc
// POST /papers/4413/compose-by-diagnosis/（学情组卷：纯算法加权，无 AI）
{ "classroom_id": 307, "random_seed": 20261001, "weak_quota": 0.65,
  "template": { "total_score": 100, "sections": [ { "section_title": "一、单项选择题", "question_type": "SINGLE_CHOICE", "count": 10, "score_each": 4 } ] } }
```

```jsonc
// 200 OK（两种方式同结构）
{ "data": { "id": 4412, "compose_mode": "RULE", "random_seed": 20261001, "total_score": 100, "item_count": 15,
            "difficulty_expect": 0.68, "knowledge_coverage": { "covered_nodes": 6, "total_nodes": 7, "jaccard": 0.83 },
            "compose_rationale": [ { "node_id": 8801, "node_name": "动量守恒", "mastery": 0.43, "allocated_score": 22,
                                     "reason": "班级掌握度 43%，低于薄弱阈值 0.60" },
                                   { "node_id": 8812, "node_name": "机械能守恒", "mastery": 0.41, "allocated_score": 18,
                                     "reason": "班级掌握度 41%" } ],
            "items": [ { "id": 88011, "seq": 1, "question_id": 10455, "score": 4, "section_title": "一、单项选择题",
                         "question_status": "APPROVED", "has_figure": true, "difficulty_effective": 3 } ],
            "bank_health_warnings": [ { "node_id": 8907, "node_name": "自感现象", "verdict": "EMPTY",
                                        "message": "该知识点无可用题目，已降低覆盖率要求" } ] } }
```

| 约定 | 说明 |
| --- | --- |
| 候选池 | 只用 `Question.objects.usable_for_paper()`（`status=APPROVED`）；**含 `PENDING` 配图的题被硬过滤**（7.4.8.1 组卷门禁） |
| `compose_rationale` | 仅 `DIAGNOSIS` 模式必返回，前端必须展示（8.1 第 4 步） |
| 抽不满 | 必须返回 `bank_health_warnings`（或其他 `gaps`），**禁止静默降级**（R7、8.7） |
| 复现 | 同诊断数据 + 同 `random_seed` → 同样的卷子；`re-roll` 复用 seed 序列 |
| 权重 | 数据不足（`confidence < min_confidence_for_recommend`）的知识点取中性权重 1.0 |

**错误码**：`PAPER.CANDIDATE_INSUFFICIENT`（409，`detail.gaps[]` = `{knowledge_node_id, node_name, question_type, required, available}`）、`PAPER.EQUIVALENCE_FAILED`（409）、`QUESTION.NOT_APPROVED`（409，换题时）。

### 4.8 `POST /papers/{id}/sheet-binding/` — 绑定答题卡

```jsonc
// 请求
{ "sheet_template_id": 3301, "is_primary": true }
```

```jsonc
// 201 Created
{ "data": { "id": 6610, "paper_id": 4412, "sheet_template_id": 3301, "is_primary": true,
            "bound_by": 2048, "bound_at": "2026-10-01T13:02:00+08:00",
            "template": { "name": "标准A4·8位学号·20题", "version": 3, "objective_item_count": 20,
                          "question_types": { "SINGLE_CHOICE": 15, "TRUE_FALSE": 5 },
                          "student_no_digits": 8, "page_count": 2, "status": "ACTIVE" } } }
```

三条校验（8.5，必须实现）：`试卷客观题题量 == template.objective_item_count`；`试卷客观题题型结构 ⊆ template.question_types`；`试卷学号位数需求 <= template.student_no_digits`。

```jsonc
// 409 SHEET.BIND_MISMATCH
{ "error": { "code": "SHEET.BIND_MISMATCH",
             "message": "该模板是为 20 道客观题设计的，当前试卷有 15 道客观题，无法直接绑定。",
             "detail": { "checks": [ { "rule": "objective_item_count", "expected": 20, "actual": 15, "ok": false },
                                     { "rule": "question_types", "ok": true },
                                     { "rule": "student_no_digits", "expected": 8, "actual": 8, "ok": true } ],
                         "suggestions": ["新建模板", "选择其他模板", "修改试卷题型配额"] } } }
```

### 4.9 答题卡：`POST /sheet/templates/`、`GET /sheet/templates/{id}/`

```jsonc
// POST 请求（由样式生成模板）— 异步 202 sheet.generate_template
{ "style_id": 21, "name": "高二物理月考·标准A4", "from_paper_id": 4412, "objective_item_count": 20,
  "question_types": { "SINGLE_CHOICE": 15, "TRUE_FALSE": 5 }, "threshold_version": "v1" }
```

```jsonc
// 202 Accepted
{ "data": { "task_id": "d4e5f6a7-...", "task_type": "sheet.generate_template", "status": "PENDING",
            "queue": "p1_generate", "progress": 0, "created_at": "2026-10-01T13:10:00+08:00",
            "sheet_template_id": 3301 } }
```

```jsonc
// GET 200 OK（节选关键字段）
{ "data": { "id": 3301, "org_id": 17, "style_id": 21, "name": "高二物理月考·标准A4", "version": 3,
            "page_count": 2, "objective_item_count": 20, "question_types": { "SINGLE_CHOICE": 15, "TRUE_FALSE": 5 },
            "status": "ACTIVE", "asset_id": 771002,
            "pdf_url": "https://oss.../sheet_3301_v3.pdf?Signature=...",
            "preview_url": "/api/v1/sheet/templates/3301/preview/",
            "calibration_url": "/api/v1/sheet/templates/3301/calibration/",
            "expires_at": "2026-10-01T13:15:00+08:00",
            "threshold_config": { "threshold_version": "v1", "fill_threshold_filled": 0.45, "fill_threshold_empty": 0.15,
                                  "student_no_confidence_threshold": 0.98, "anchor_max_deviation": 0.01 },
            "layout_json": { "version": 3, "page_size": "A4", "dpi_reference": 300,
              "anchors": [ { "id": "TL", "x": 0.0635, "y": 0.0423 }, { "id": "TR", "x": 0.9365, "y": 0.0423 },
                           { "id": "BL", "x": 0.0635, "y": 0.9577 }, { "id": "BR", "x": 0.9365, "y": 0.9577 } ],
              "student_no": { "enabled": true, "digits": 8, "layout": "SINGLE_ROW", "per_row": null,
                              "group_size": 4, "has_checksum": false,
                              "positions": [ { "pos": 0, "bubbles": [ { "d": 0, "cx": 0.0820, "cy": 0.1050, "r": 0.0085 },
                                                                     { "d": 1, "cx": 0.0820, "cy": 0.1170, "r": 0.0085 } ] } ] },
              "objective": { "layout_mode": "1_PER_ROW",
                             "items": [ { "seq": 1, "bubbles": [ { "label": "A", "cx": 0.180, "cy": 0.220, "r": 0.0075 },
                                                                 { "label": "B", "cx": 0.215, "cy": 0.220, "r": 0.0075 } ] } ] },
              "subjective_zones": [ { "seq": 21, "x": 0.08, "y": 0.70, "w": 0.84, "h": 0.12,
                                      "score_box": { "x": 0.90, "y": 0.70, "w": 0.05, "h": 0.02 } } ] } } }
```

| 约定 | 说明 |
| --- | --- |
| `version` | **只增不改**（12.6）；原地改版 → `409 SHEET.TEMPLATE_VERSION_IMMUTABLE`，前端提示「另存为新版本」 |
| `positions` | **展开后的每位涂点坐标**，识别端**直接读取**，不自行推导排布算法（9.2.4.1） |
| 学号区 | `student_no_digits ∈ 0..12`（0 = 不印）；`MULTI_ROW` 时版面上**必须**标注「第 1-6 位 / 第 7-12 位」 |
| 阈值 | 识别以卡片所属模板的 `threshold_config` 快照为准 |
| 签名 | 一律 ≤ 5 分钟（1.8） |

**错误码**：`SHEET.STUDENT_NO_OVERFLOW`（400，`detail.issues[]` + `detail.suggestions[]`）、`SHEET.TEMPLATE_VERSION_IMMUTABLE`（409）。

### 4.10 `POST /assignments/{id}/publish/` — 发布任务

**幂等** **必须**（防重复生成学生记录与乱序映射）

```jsonc
// 请求
{ "confirm_checks": true, "regenerate_publish_token": false, "notify_students": false }
```

```jsonc
// 200 OK
{ "data": { "id": 4410, "status": "PUBLISHED", "paper_id": 4412, "paper_status": "FINALIZED",
            "classroom_id": 307, "channel": "BOTH", "sheet_template_id": 3301,
            "start_at": "2026-10-08T08:00:00+08:00", "end_at": "2026-10-09T08:00:00+08:00",
            "publish_token": "9Kx2mQ7vT1sR4pL8nB3cZ0dF6gH5jW2y",
            "publish_url": "https://zhifou.example.com/s/9Kx2mQ7vT1sR4pL8nB3cZ0dF6gH5jW2y",
            "qrcode_url": "/api/v1/assignments/4410/qrcode/", "student_count": 45,
            "mapping_summary": { "shuffle_questions": true, "shuffle_options": true, "students_with_mapping": 45,
                                 "seed_strategy": "PER_STUDENT_DETERMINISTIC" },
            "published_at": "2026-10-01T13:30:00+08:00" } }
```

**发布前门禁（全部必须实现）**：
| 门禁 | 规则 | 失败返回 |
| --- | --- | --- |
| 试卷定稿 | `paper.status == FINALIZED` | `409 ASSIGNMENT.PAPER_NOT_FINALIZED`（新增码，见 G-18） |
| 配图全确认 | 该卷所有配图 `review_status ∈ {CONFIRMED, DELETED}` | `409 FIGURE.PENDING_CONFIRM`（`detail.pending_count` + `detail.figure_review_url`） |
| 图文完整 | 「题干含图引用词 + `has_figure=true` + 配图数 0」三条件同时成立 | `409 FIGURE.MISSING_REFERENCED`（`detail.items[]` 给题号） |
| 答题卡 | `PAPER_OMR` / `BOTH` 时 `sheet_template_id` 必填且结构匹配 | `400 COMMON.VALIDATION_ERROR` / `409 SHEET.BIND_MISMATCH` |
| 名册非空 | 班级 `status=ACTIVE` 名册至少 1 人 | `400 COMMON.VALIDATION_ERROR` |

**乱序映射（9.6）**：`question_order` / `option_order` **在发布时一次性生成并持久化到 `assignment_student`**，之后学生端卷面、扫描映射、成绩统计**必须复用**，**禁止每次读取重新随机**（16.4 ★用例）。一人一卷时映射基准是该生自己的 `paper_item_id`。

> **★ 选项乱序只针对选择题**（2026-10-17 需求方口径）：
> `option_order` **只对 `SINGLE_CHOICE` / `MULTIPLE_CHOICE` 生成**。
> 判断题的两个框是「对 / 错」，位置本身有意义 —— 打乱后
> 界面上会出现「`T` 位写着『错误』」，判分按位置还原还会把学生点的
> `T` **整题判反**。`_collect_paper_items` 不再把非选择题交给乱序，
> `ordering_for_row` 在读侧也剔除非选择题的乱序条目
> （`OPTION_SHUFFLABLE_TYPES`）。

**★ 学生身份行（2026-10-16 落地，`P1-71b` 的下半段）**

发布时**为班里每个学号建一行 `accounts.User`**（`role=STUDENT`，用户名
`stu-{org_id}-{学号}`，`set_unusable_password()`），并把
`classroom_student.student` / `assignment_student.student` 指向它：

| 效果 | 说明 |
| --- | --- |
| **学生不绑账号也能线上作答** | 身份由「学号 + 姓名」+ `session_token` 确定，七张业务表照旧按 `student_id` 挂载 |
| 名单能显示学号姓名 | `assignment_student.student_no/student_name` 是**发布时冻结的快照**，不依赖账号 |
| 幂等 | 重复发布不重建、不重新乱序；**只补**缺失的 `student_id`（老任务因此自愈） |
| `identity` 报告 | `bound` = **有登录凭据**（能自己登录看成绩），`unbound` = 只能靠老师念分数；**不是阻塞项**（`needs_attention` 恒为 `false`） |
| 绑定账号（后续能力） | = 给这一行设密码/绑手机 → `User.can_login` 自动为真，历史数据天然在它名下 |

> ⚠ 判据迁移：`ClassroomStudent.student_id IS NULL` 已**不能**表示「未绑账号」
> （身份行建好后永远非空）。相关判断一律用
> `User.can_login` / `accounts.services.has_usable_password_hash()`。

### 4.10b `GET /grading/assignments/{id}/roster/` — 批改工作台的学生名单

> **★ 2026-10-17 新增**（需求方口径：「批改界面应该显示学生名单、显示交卷情况；
> 已交卷的可以在该学生后面点『批改』，批改后可以点『查看详情』；也可以点『批改全班』」）。

```jsonc
// 200 OK
{ "data": { "assignment_id": 4410, "item_count": 20, "auto_item_count": 20,
            "subjective_item_count": 0, "title": "期中测试",
            "counts": { "total": 45, "submitted": 43, "not_submitted": 2,
                        "graded": 43, "pending_grade": 0, "needs_review": 3 },
            "items": [ { "student_id": 3399, "row_id": 771, "student_no": "20260312",
                         "student_name": "张三", "status": "SUBMITTED",
                         "submitted": true, "entered_at": "...", "submitted_at": "...",
                         "item_count": 20, "auto_item_count": 20, "subjective_item_count": 0,
                         "scored": 20, "auto_graded": 17, "needs_review": 3, "confirmed": 0,
                         "graded": true, "auto_done": true, "total_score": "86.00" } ] } }
```

| 字段 | 说明 |
| --- | --- |
| `student_id` | **学生身份行 id**：`auto-grade` 的 `student_ids` 用它 |
| `row_id` | 名单行 id：`GET /assignments/{id}/students/{sid}/` 用它 |
| `submitted` | 是否已交卷（`mark_submitted` 或扫描入库） |
| `scored` / `auto_item_count` | 批改进度：已产生评分条数 / 机器可批题数 |
| `auto_done` | 机器能批的部分是否批完 |
| `needs_review` | 该生待人工确认的条数 |
| `total_score` | 成绩汇总（未算出来时为 `null`，**不要显示成 0**） |

★ **未交卷的学生不产生评分记录**：批量批改只对已交卷的学生执行
（给未交卷的人建 0 分记录会让成绩单出现大量假 0 分）。

★ 「批改全班」= 前端循环调用 `auto-grade`（每生一次），
  一次失败不影响其他人；也可以不传 `student_ids`，由后端批已交卷的全部。

```jsonc
// POST /grading/assignments/{id}/auto-grade/  （单生批改）
{ "student_ids": [3399] }
```

**「查看详情」的数据来源**：`GET /grading/scores/?assignment_id={id}&student_id={sid}`
（该接口的每条评分现在额外返回 `student_answer`（学生作答，已还原到原题字母）
与 `correct_answer`（标准答案文本，取自卷面快照），供逐题核对）。

### 4.11 关口队列：`GET /ingest/batches/{id}/figure-review/`、`GET /papers/{id}/figure-verify/`

```jsonc
// 关口一：逐图确认队列（节选）
{ "data": { "batch_id": 9031, "total": 12, "confirmed": 3, "pending": 9, "deleted": 0,
            "blocking": true, "current_index": 4,
            "keyboard_hints": { "Enter": "confirm", " ": "confirm", "X": "delete", "R": "recrop", "S": "capture",
                                "V": "replace", "ArrowLeft": "prev", "ArrowRight": "next_unconfirmed",
                                "Shift+Enter": "confirm_and_next_question" },
            "items": [ { "id": 889001, "question_seq": 3, "question_id": 10455, "attach_point": "STEM",
                         "seq_in_question": 1, "url": "https://oss.../fig_1200.png?Signature=...",
                         "thumb_url": "https://oss.../thumb_400.jpg?Signature=...",
                         "source_page_url": "https://oss.../page_0002.png?Signature=...",
                         "source_ref": { "page": 2, "bbox": [0.152, 0.311, 0.412, 0.502] },
                         "caption": "图1", "figure_category": "CIRCUIT", "text_inside": ["R1", "R2", "6V"],
                         "ocr_confidence": 0.62, "quality_flags": ["SUSPECT_CROP"],
                         "binding_method": "SEQUENCE", "binding_confidence": 0.71, "review_status": "PENDING",
                         "batchable": false, "batchable_reason": "binding_confidence 0.71 < 0.97 或 src_path 非对象抽取" } ] } }
```

> `batchable` 由服务端按 7.4.8.3 三条件计算下发，**前端不得自行判断**。

```jsonc
// 关口二：试卷配图校验（节选）
{ "data": { "paper_id": 4412, "paper_status": "DRAFT", "figure_total": 32, "confirmed": 29, "pending": 3,
            "checks": { "split_by_page": [ { "question_image_id": 889101, "seq": 9, "message": "该图被分页线切断，建议缩小或下移" } ],
                        "too_wide": [ { "question_image_id": 889102, "seq": 7, "ratio": 0.97, "message": "图宽 12cm，接近栏宽上限" } ],
                        "too_small": [], "overlap": [],
                        "duplicate_in_paper": [ { "question_image_id": 889103, "asset_id": 558201, "seqs": [7, 12] } ],
                        "missing": [ { "paper_item_id": 88051, "seq": 14, "message": "题干提到图片但无配图，阻断定稿" } ] },
            "blocking": true, "blocking_reasons": ["1 道题提到图片但缺少配图"],
            "preview_pages": [ { "page": 1, "url": "https://oss.../preview_p1.png?Signature=..." } ] } }
```

| 约定 | 说明 |
| --- | --- |
| `missing` | **阻断定稿**（`layout_check_block_on_missing=true`），同时映射 `FIGURE.MISSING_REFERENCED`（409，16.4 ★用例） |
| 防误报 | 三条件（含图引用词 + `has_figure=true` + 配图数 0）同时成立才报，纯文本匹配会产生大量误报（7.4.8.5 注） |
| 分页切断 / 超宽等 | 默认以 `warnings`（200）返回；仅当 `?block_on_split=true` 时 409 `FIGURE.SPLIT_BY_PAGE`（README 与 7.4.8.5 口径差异见 G-8） |
| 手动动作 | `recrop` / `capture` / `replace` **与关口一完全复用同一组接口**（7.4.8.5） |
| 定稿 | 全部确认后 `POST /papers/{id}/figure-verify/confirm-all/` → `paper.status=FINALIZED` |

### 4.12 `POST /scan/cards/quick/` — 手机端拍一张立即识别

**权限** `TEACHER_WRITE` ｜ **幂等** **必须** ｜ **异步** 否（同步，目标 ≤ 2s，15.1）

```jsonc
// 请求
{ "batch_id": 9033, "oss_key": "zf/17/SCAN_RAW/2026/10/card_0007.jpg", "capture_channel": "PHONE_H5",
  "captured_at": "2026-10-01T14:02:11+08:00",
  "client_quality": { "anchors_found": 4, "brightness": 0.41, "uniformity": 0.72, "tilt_angle": 4.2,
                      "blur_score": 61.0, "ok": true, "messages": [] },
  "device": { "model": "iPhone 15", "screen": "390x844" }, "sequence_no": 7 }
```

```jsonc
// 分支 A：识别成功（200）
{ "data": { "card_id": 99231, "scan_batch_id": 9033, "status": "AUTO_OK", "elapsed_ms": 1420,
            "capture_channel": "PHONE_H5", "anchor_quality": 0.96, "anchor_deviation": 0.0031,
            "student_no": { "recognized_student_no": "20260312", "student_no_confidence": 0.99,
                            "student_no_flags": [], "matched": true,
                            "match": { "student_id": 3312, "name": "张三", "display_name": "张三",
                                       "classroom_id": 307, "student_no": "20260312" } },
            "items": [ { "seq": 1, "paper_item_id": 88011, "answer": "B", "fill_ratio": 0.71,
                         "confidence": 0.99, "status": "FILLED", "uncertain": false } ],
            "uncertain_items": [], "answer_count": 20, "filled_ratio": 0.95,
            "feedback": { "level": "SUCCESS", "title": "识别成功", "message": "张三 18/20 已识别", "primary_action": "NEXT_CARD" },
            "suggest_retake": false } }
```

```jsonc
// 分支 B：学号未匹配（200，非错误）
{ "data": { "ok": false, "code": "SCAN.STUDENT_NO_UNMATCHED",
            "message": "识别出学号 20260399，未在班级名册中找到。请指定学生。",
            "card_id": 99232, "status": "NEEDS_REVIEW", "recognized_student_no": "20260399",
            "student_no_confidence": 0.93,
            "candidates": [ { "student_id": 3399, "display_name": "张三(大)", "student_no": "20260399", "seat_no": "12" } ],
            "feedback": { "level": "WARNING", "title": "未匹配到学生", "message": "请指定学生或重拍",
                          "primary_action": "REBIND_STUDENT" }, "suggest_retake": false } }
```

| 字段 | 说明 |
| --- | --- |
| `client_quality` | 拍前实时质量检测（9.3.3）。服务端**记录但不阻断**：`ok=false` 也接受上传，写入 `scan_card.fail_reason` / `quality_report` 并在仲裁时标出（「提示但不阻断」原则） |
| `student_no_confidence` | 阈值 **0.98**（比客观题 0.95 更严，13 文档 `student_no_confidence_threshold`） |
| `anchor_deviation` | > `anchor_max_deviation`（0.01）→ 直接进人工队列（9.3.2 ③） |
| `feedback.primary_action` | `NEXT_CARD` / `RETAKE` / `REBIND_STUDENT` / `OPEN_DETAIL`；前端**只渲染一个主动作**（5.7） |

**错误码**：`SCAN.ANCHOR_NOT_FOUND`（422，`detail.anchors_found`，提示重拍）、`SCAN.STUDENT_NO_AMBIGUOUS`（409）、`SCAN.STUDENT_NO_UNMATCHED`（200 非错误）。

### 4.13 `GET /scan/cards/{id}/`、`PATCH /scan/cards/{id}/`

```jsonc
// GET 200 OK（节选）
{ "data": { "id": 99231, "scan_batch_id": 9033, "sheet_template_id": 3301, "status": "NEEDS_REVIEW",
            "capture_channel": "PHONE_H5",
            "raw_url": "https://oss.../card_0007.jpg?Signature=...",
            "rectified_url": "https://oss.../card_0007_rect.png?Signature=...",
            "expires_at": "2026-10-01T14:07:11+08:00", "anchor_quality": 0.96, "anchor_deviation": 0.0031,
            "overall_confidence": 0.94,
            "student_no": { "recognized_student_no": "20260312", "student_no_confidence": 0.97,
                            "student_no_flags": ["CHECKSUM_MISMATCH"], "matched": true,
                            "match": { "student_id": 3312, "display_name": "张三", "student_no": "20260312" } },
            "items": [ { "seq": 7, "paper_item_id": 88017, "answer": "A", "fill_ratio": 0.31, "second_fill_ratio": 0.29,
                         "confidence": 0.62, "status": "UNCERTAIN", "uncertain": true,
                         "crop_url": "https://oss.../q7.png?Signature=..." },
                       { "seq": 12, "paper_item_id": 88022, "answer": "B", "fill_ratio": 0.51, "second_fill_ratio": 0.47,
                         "confidence": 0.55, "status": "MULTI_MARK", "uncertain": true,
                         "crop_url": "https://oss.../q12.png?Signature=..." } ],
            "uncertain_items": [7, 12],
            "subjective_zones": [ { "seq": 21, "paper_item_id": 88033, "max_score": 8,
                                    "score_box_url": "https://oss.../s21.png?Signature=...", "entered_score": 6,
                                    "state": "MANUAL_ENTERED" },
                                  { "seq": 22, "paper_item_id": 88034, "max_score": 10,
                                    "score_box_url": "https://oss.../s22.png?Signature=...", "entered_score": null,
                                    "state": "PENDING" } ],
            "fail_reason": null, "reviewed_by": null, "reviewed_at": null, "audit_log_ids": [] } }
```

```jsonc
// PATCH 请求
{ "student_no": "20260312",
  "items": [ { "seq": 7, "answer": "A", "manual_override": true }, { "seq": 12, "answer": "B", "manual_override": true } ],
  "subjective_scores": [ { "seq": 22, "score": 8 } ],
  "reason": "对照原图确认为 A、B", "expected_updated_at": "2026-10-01T14:03:00+08:00" }
```

```jsonc
// PATCH 200 OK
{ "data": { "id": 99231, "status": "REVIEWED",
            "items": [ { "seq": 7, "answer": "A", "status": "MANUAL_OVERRIDE", "uncertain": false } ],
            "subjective_scores": [ { "seq": 22, "score": 8, "state": "MANUAL_ENTERED", "method": "MANUAL" } ],
            "needs_reconfirm": true, "audit_log_id": 77130, "updated_at": "2026-10-01T14:05:00+08:00" } }
```

| 约定 | 说明 |
| --- | --- |
| `expected_updated_at` | 乐观锁；不一致 → `409 COMMON.IDEMPOTENT_CONFLICT`（防两位教师同时改一卡） |
| `manual_override` | 置 `Response.source=MANUAL`（合并优先级最高，README 5.4） |
| 不物理删除 | 被覆盖的 `Response` 写 `superseded_by`（9.6） |
| 确认 | 改完后仍需 `POST /scan/cards/{id}/confirm/` 才写 `Response` 与触发重算（`needs_reconfirm=true`） |

**错误码**：`GRADING.SCORE_EXCEED_MAX`（400，`detail.max_score`）、`COMMON.IDEMPOTENT_CONFLICT`（409）。

### 4.14 `POST /scan/cards/{id}/rebind-student/` — 重新指定学生

```jsonc
// 请求
{ "student_id": 3399, "reason": "学号涂错，学生本人确认", "keep_answers": true }
```

```jsonc
// 200 OK
{ "data": { "id": 99232, "student_id": 3399,
            "student": { "display_name": "张三(大)", "student_no": "20260399", "classroom_id": 307 },
            "status": "NEEDS_REVIEW",
            "remapped": { "question_order_applied": true, "remapped_items": 20, "uncertain_items": [7] },
            "requires_confirm": true, "audit_log_id": 77131 } }
```

| 约定 | 说明 |
| --- | --- |
| 映射重算 | 学号变更后，卡上「第 N 题」必须按**新学生**的 `assignment_student.question_order` 重新映射（9.6 ★，映射错则全班成绩错乱） |
| 不自动覆盖 | `requires_confirm=true`，教师需再确认；目标学生已有卡时 **409 `SCAN.STUDENT_NO_AMBIGUOUS`**，`detail.conflicts[]` 列出现有卡由教师选择（9.3.2 ⑥） |
| 审计 | 必须写审计（15.3：学号重绑） |

### 4.15 `GET/POST /grading/assignments/{id}/subjective-entry/`

```jsonc
// GET（参数 view=BY_QUESTION|BY_STUDENT、paper_item_id、student_id、include_images=true）200 OK（节选）
{ "data": { "assignment_id": 4410, "classroom": { "id": 307, "name": "高二(3)班" }, "view": "BY_QUESTION",
            "progress": { "subjective_item_count": 4, "student_count": 45, "entered": 8, "expected_total": 180,
                          "by_item": [ { "seq": 21, "paper_item_id": 88033, "max_score": 8, "entered": 45, "total": 45 },
                                       { "seq": 22, "paper_item_id": 88034, "max_score": 10, "entered": 8, "total": 45 } ] },
            "current_item": { "seq": 22, "paper_item_id": 88034, "max_score": 10,
                              "knowledge_tags": [ { "node_id": 8801, "node_name": "动量守恒", "weight": 1.0 } ],
                              "avg_score": 6.2, "avg_score_rate": 0.62 },
            "rows": [ { "student_id": 3312, "display_name": "张三", "seat_no": "1", "student_no": "20260312",
                        "is_absent": false, "blank_count": 0, "score": 6, "score_rate": 0.6,
                        "score_result_id": 881023, "state": "MANUAL_ENTERED", "method": "MANUAL",
                        "scan_image_url": "https://oss.../card_0001_p2.png?Signature=...",
                        "score_box_url": "https://oss.../s22.png?Signature=...", "note": null } ],
            "keyboard_hints": { "Tab": "next_column", "Enter": "next_row", "Del": "clear",
                                "Ctrl+D": "mark_absent", "Ctrl+V": "paste_column" } } }
```

```jsonc
// POST 请求（按题批量提交逐题分）
{ "view": "BY_QUESTION", "mode": "PER_ITEM", "paper_item_id": 88034,
  "entries": [ { "student_id": 3312, "score": 6 }, { "student_id": 3313, "score": 8 },
               { "student_id": 3314, "score": null, "is_absent": true } ] }
```

```jsonc
// POST 200 OK
{ "data": { "succeeded": 2, "failed": 1,
            "results": [ { "index": 0, "student_id": 3312, "ok": true, "score_result_id": 881023, "state": "MANUAL_ENTERED" },
                         { "index": 2, "student_id": 3314, "ok": true, "score_result_id": null, "state": "ABSENT",
                           "excluded_from_class_avg": true } ],
            "progress": { "entered": 10, "expected_total": 180, "by_item": [ { "seq": 22, "entered": 10, "total": 45 } ] },
            "recompute_task_id": null } }
```

| 约定 | 说明 |
| --- | --- |
| `mode` | `PER_ITEM`（默认）；快速模式走 §4.17 |
| `score` | 0 ≤ score ≤ `max_score`；超限 → `GRADING.SCORE_EXCEED_MAX` |
| `is_absent` | 缺考：不生成分数，**不计入班级平均分**（10.5 边界 1、A40） |
| `score=null` 且非缺考 | 视为部分作答的空白题 → 记 0 分并累加 `student_total_score.blank_count`（10.5 边界 2） |
| 逐题独立提交 | 每道题独立提交，**不要求一次录完**（10.5 第 7 条）；`/confirm/` 才触发全量重算 |

**错误码**：`GRADING.SCORE_EXCEED_MAX`（400）、`GRADING.NO_RESPONSE`（409，该生无作答记录却录主观分）。

### 4.16 `POST /grading/assignments/{id}/subjective-entry/paste/` — 整列粘贴

```jsonc
// 请求（dry_run=true 不写库）
{ "paper_item_id": 88034, "column_text": "6\n8\n\n10\n0", "mapping": "ROSTER_ORDER",
  "has_header": false, "dry_run": true }
```

```jsonc
// 200 OK
{ "data": { "dry_run": true, "parsed_count": 5, "mapping": "ROSTER_ORDER",
            "rows": [ { "line": 1, "raw": "6", "score": 6, "student_id": 3312, "display_name": "张三",
                        "matched_by": "ROSTER_ORDER", "warning": null },
                      { "line": 3, "raw": "", "score": null, "student_id": 3314, "display_name": "王五",
                        "matched_by": "ROSTER_ORDER", "warning": "空值按未作答处理" } ],
            "unmatched_lines": [],
            "conflicts": [ { "line": 4, "reason": "已有分数 10 分，本次 10 分，是否覆盖", "overwrite": true } ],
            "requires_confirm": true } }
```

| 约定 | 说明 |
| --- | --- |
| `mapping` | `ROSTER_ORDER`（默认）/ `SEAT_NO` / `STUDENT_NO` / `NAME` |
| `dry_run` | **必须先 dry run**，返回匹配结果供教师核对后再正式提交（防错位写分） |
| 数量不匹配 | 行数 ≠ 名册人数且 `mapping=ROSTER_ORDER` → 400，`detail.reason="ROSTER_ORDER_LENGTH_MISMATCH"` |
| 冲突 | 与已有分数不同则列 `conflicts[]`，需 `confirmed=true` 才覆盖 |

### 4.17 `POST /grading/assignments/{id}/subjective-entry/aggregate/` — 快速模式（总分分摊）

```jsonc
// 请求
{ "scope": "CLASSROOM", "subjective_item_ids": [88033, 88034, 88035, 88036], "strategy": "EVEN",
  "entries": [ { "student_id": 3312, "total_score": 22 }, { "student_id": 3313, "total_score": 30, "is_absent": false } ],
  "confirm": true }
```

```jsonc
// 200 OK
{ "data": { "method": "MANUAL_AGGREGATE", "strategy": "EVEN",
            "students": [ { "student_id": 3312, "total_score": 22, "subjective_max_total": 40,
                            "scores": [ { "seq": 21, "paper_item_id": 88033, "max_score": 8, "score": 4.4, "score_result_id": 881201 },
                                        { "seq": 22, "paper_item_id": 88034, "max_score": 10, "score": 5.5, "score_result_id": 881202 },
                                        { "seq": 23, "paper_item_id": 88035, "max_score": 12, "score": 6.6, "score_result_id": 881203 },
                                        { "seq": 24, "paper_item_id": 88036, "max_score": 10, "score": 5.5, "score_result_id": 881204 } ] } ],
            "precision_warning": "本次主观题成绩为总分分摊（method=MANUAL_AGGREGATE），知识点归因不精确。",
            "precision_warning_visible_in_report": true } }
```

约定：只录总分 → 按题量平均分摊并逐题写 `score_result`（`method=MANUAL_AGGREGATE`）；学情报告必须带精度提示（A40、附录 F）；`total_score > Σ 主观题满分` → `GRADING.SCORE_EXCEED_MAX`；`subjective_allow_aggregate_mode=false` → `403 AUTH.FORBIDDEN`。⚠ 「按题量平均分摊」与「按各题满分比例分摊」结果不同（8/10/12/10 分题），已登记 G-6，**实现前必须确认**。

### 4.18 `GET /analytics/classrooms/{id}/heatmap/`

参数：`subject`、`grade_band`、`node_id`（下钻）、`assignment_id`、`compare=GRADE|PREVIOUS`、`include_children=true`

```json
{ "data": { "classroom": { "id": 307, "name": "高二(3)班" }, "subject": "physics",
            "calculated_at": "2026-10-01T13:00:00+08:00",
            "compare": { "mode": "PREVIOUS", "label": "上次", "avg_mastery_delta": 0.03 },
            "summary": { "avg_mastery": 0.68, "weakest_node": { "node_id": 8801, "node_name": "动量守恒", "mastery": 0.43 } },
            "nodes": [ { "node_id": 8801, "node_name": "动量守恒", "path": "力学/动量/动量守恒", "level": 3,
                         "mastery": 0.43, "confidence": 0.85, "effective_samples": 12.4, "answered_count": 45,
                         "correct_rate": 0.41, "is_weak": true, "mastery_level": "MASTERY_2",
                         "color_token": "--zf-mastery-2", "previous_mastery": 0.40, "delta": 0.03,
                         "prerequisite_hint": { "node_id": 8790, "node_name": "动量定理", "mastery": 0.52 } },
                       { "node_id": 8907, "node_name": "自感现象", "mastery": null, "confidence": 0.0,
                         "answered_count": 0, "mastery_level": "NA", "color_token": "--zf-mastery-na", "display": "--" } ] } }
```

| 约定 | 说明 |
| --- | --- |
| `mastery_level` | `MASTERY_0`(0–0.30) / `MASTERY_1`(0.30–0.50) / `MASTERY_2`(0.50–0.70) / `MASTERY_3`(0.70–0.85) / `MASTERY_4`(0.85–1.0) / `NA`（11.2 展示口径） |
| `color_token` | 服务端只回令牌名，**颜色值由前端令牌决定**（README 第十节禁止硬编码颜色） |
| 数据不足 | `mastery=null` + `display="--"`，**不得**用 0% / 100% 填坑（11.4） |
| 聚合 | 父节点为子节点加权聚合；`question_knowledge.weight` 参与分摊（11.2 补充） |

### 4.19 `GET /analytics/students/{id}/report/`

参数：`from`、`to`、`assignment_id`、`include_rank`（默认 `false`）

```json
{ "data": { "student": { "id": 3312, "display_name": "张三", "student_no": "20260312" },
            "classroom": { "id": 307, "name": "高二(3)班" },
            "period": { "from": "2026-09-01", "to": "2026-10-12" },
            "summary": { "latest_total_score": 68, "latest_full_score": 100, "class_avg": 72,
                         "mastery_delta_vs_previous": 0.032, "rank_in_class": null, "include_rank": false },
            "weak_nodes": [ { "node_id": 8801, "node_name": "动量守恒", "mastery": 0.38, "confidence": 0.85,
                              "wrong_count": 3, "answered_count": 4, "priority": 1,
                              "prerequisite": { "node_id": 8790, "node_name": "动量定理", "mastery": 0.51, "is_also_weak": true } } ],
            "strong_nodes": [ { "node_id": 8750, "node_name": "匀变速直线运动", "mastery": 0.92,
                                "suggestion": "可尝试提高难度的变式题" } ],
            "wrong_questions": [ { "question_id": 10455, "paper_item_id": 88011, "assignment_id": 4410, "seq": 7,
                                   "node_names": ["动量守恒"], "answered_at": "2026-10-10T09:12:00+08:00",
                                   "student_answer": "C", "correct_answer": "B",
                                   "snapshot": { "stem_html": "<p>如图所示的电路中…</p>", "has_figure": true },
                                   "figure_urls": [ { "question_image_id": 889001,
                                                      "url": "https://oss.../fig_1200.png?Signature=...",
                                                      "thumb_url": "https://oss.../thumb_400.jpg?Signature=..." } ] } ],
            "mastery_trend": [ { "snapshot_at": "2026-09-20T00:00:00+08:00", "avg_mastery": 0.61, "trigger": "ASSIGNMENT_GRADED" } ],
            "practice_suggestion": { "target_node_ids": [8801, 8812], "strategy": "WEAK_FIRST", "available": true,
                                     "cold_start_blocked": false },
            "precision_warnings": [ { "code": "MANUAL_AGGREGATE", "message": "本次主观题成绩为总分分摊，知识点归因不精确。" } ] } }
```

| 约定 | 说明 |
| --- | --- |
| `rank_in_class` | 仅 `include_rank=true` 时返回数字，否则恒 `null`（11.9：学生报告默认不含排名） |
| `figure_urls` | **错题缩略图必须包含配图**且可点击放大（11.6 v2.0 补充） |
| `prerequisite` | 只追溯**一层**，且仅当前置也薄弱时返回（11.6） |
| `precision_warnings` | 总分分摊成绩必须带精度提示（A40） |

### 4.20 `POST /practice/generate/` — 生成个性化练习

**幂等** **必须** ｜ **异步** `202 practice.generate`

```jsonc
// 请求
{ "source_assignment_id": 4410, "classroom_id": 307, "student_ids": [3312, 3313, 3314],
  "strategy": "WEAK_FIRST", "include_students_without_weakness": true, "recent_exclude_days": 60,
  "structure_constraint": "SAME_STRUCTURE_AS_SOURCE" }
```

```jsonc
// 202 Accepted
{ "data": { "task_id": "e7f8a9b0-...", "task_type": "practice.generate", "status": "PENDING",
            "queue": "p1_generate", "progress": 0, "created_at": "2026-10-01T15:00:00+08:00",
            "classroom_id": 307, "student_count": 45 } }
```

| 约定 | 说明 |
| --- | --- |
| 前置条件 | 该任务成绩已确认（`/grading/assignments/{id}/confirm/`） |
| 结构 | 题量、题型、配分与源试卷完全一致；答题卡复用原试卷绑定的模板（C11） |
| 参数 | `weak_threshold` / `confidence_ratio` / `challenge_ratio` 等**全部来自 13 文档**，请求体仅允许覆盖 `recent_exclude_days` 与 `strategy` |
| 选题理由 | 每题写 `practice_set_item.reason`，前端必须展示（11.7.4 第 2 条） |
| ⚠ 缺口 | 「题型结构例外申请」（2.3 提到）**无接口与字段定义**，本文不定义，见 G-7 |

**错误码**：`GRADING.ASSIGNMENT_NOT_GRADED`（409）、`PAPER.CANDIDATE_INSUFFICIENT`（409，全部失败时；部分失败走 `result.skipped[]`，见 §3.6）。

### 4.21 `POST /practice/export/`、`GET /practice/export/{task_id}/`

```jsonc
// POST 请求
{ "source_assignment_id": 4410, "practice_set_ids": [5512, 5513, 5514], "include_generic_sheet": true,
  "chunk_size": 15, "precheck_layout": true, "auto_fix": false, "force_regenerate": false }
```

```jsonc
// 202 Accepted
{ "data": { "task_id": "f1a2b3c4-...", "task_type": "practice.export", "status": "PENDING",
            "queue": "p1_generate", "progress": 0, "created_at": "2026-10-01T15:20:00+08:00",
            "practice_set_count": 45, "cache_hit": false, "expires_at": "2026-10-02T15:20:00+08:00" } }
```

```jsonc
// GET /practice/export/{task_id}/ 200 OK（PROGRESS 态）
{ "data": { "task_id": "f1a2b3c4-...", "task_type": "practice.export", "status": "PROGRESS", "progress": 60,
            "message": "已生成 27/45 份", "generated": 27, "total": 45, "phase": "RENDER_PDF",
            "chunks": [ { "index": 1, "file_count": 15, "status": "READY", "asset_id": 559911,
                          "download_url": "https://oss.../chunk_1.zip?Signature=..." } ],
            "generic_sheet_asset_id": 559900, "zip_asset_id": null, "download_url": null, "cache_hit": false,
            "layout_issues": [ { "practice_set_id": 5512, "student_name": "张三", "seq": 7, "flag": "SPLIT_BY_PAGE",
                                 "message": "第 7 题配图会被分页切断" } ],
            "auto_fixed": false, "expires_at": "2026-10-02T15:20:00+08:00",
            "created_at": "2026-10-01T15:20:00+08:00", "finished_at": null } }
```

| 约定 | 说明 |
| --- | --- |
| 文件组织 | `00_通用答题卡.pdf` + `00_打印说明.txt` + `{序号}_{姓名}_{学号}.pdf`，序号按班级名册顺序（11.7.5：顺序错乱会让分发变成灾难） |
| 页脚 | 每份 PDF 页脚含该生 `practice_set.serial_no`（如 `P-2026-0012`）——防卷卡错配的第一道防线（R23） |
| 缓存 | 同一练习集重复导出直接复用（`cache_hit=true`）；练习被修改后缓存失效（A42） |
| 生命周期 | 导出包 24 小时后自动清理（`export_zip_retain_hours=24`，A42） |
| 分包下载 | `chunk_size=15` 时额外提供分片包（满规模必须，Q20） |
| 排版检查 | `precheck_layout=true` 先跑关口二排版检查；配图已在源试卷逐图确认，**不重复确认**（11.7.5） |
| 自动修正 | 默认 `auto_fix=false`（附录 F），**只提示不静默修正**；`true` 时返回 `auto_fixed=true` 并重检 |

**错误码**：`EXPORT.TASK_RUNNING`（409，`detail.running_task_id`）、`EXPORT.LAYOUT_ISSUES`（409，`detail.issues[]`）。

---

### 4.21 `GET /analytics/assignments/{id}/measurement/` — 试卷测量指标

> **★ 2026-10-17 新增**（需求方口径：「学情中应该根据学生的作答情况，
> 自动计算试卷难度、信度和效度」）。

```jsonc
// 200 OK
{ "data": { "assignment_id": 4410, "student_count": 43, "item_count": 20, "full_score": 100,
            "difficulty": { "value": 0.68, "band": "MEDIUM", "label": "中等",
                            "note": "全卷难度 = 全体考生得分之和 / 全体满分之和（得分率口径，越大越易）" },
            "reliability": { "value": 0.812, "band": "GOOD", "label": "好",
                             "item_count": 20, "student_count": 43, "reason": "",
                             "note": "Cronbach's α；全为二分题时与 KR-20 基本一致" },
            "validity": { "covered_node_count": 18, "total_node_count": 44, "coverage": 0.409,
                          "item_binding_rate": 1.0, "items_with_nodes": 20, "item_count": 20,
                          "uncovered_node_count": 26,
                          "note": "内容效度按「考点覆盖率」口径；效标效度需外部效标，本系统不提供" },
            "discrimination_summary": { "avg": 0.41, "count": 20, "poor_count": 1 },
            "items": [ { "paper_item_id": 8821, "seq": 1, "question_type": "SINGLE_CHOICE",
                         "max_score": 5, "difficulty": 0.86, "difficulty_band": "EASY",
                         "difficulty_label": "易", "discrimination": 0.32,
                         "discrimination_band": "GOOD", "discrimination_label": "良好",
                         "discrimination_high": 1.0, "discrimination_low": 0.68,
                         "discrimination_reason": "" } ] } }
```

| 指标 | 口径 | 判据 |
| --- | --- | --- |
| 难度 `P` | 该题**平均得分率**（0–1，**越大越易**）；未作答记 0 | ≥0.85 易 / 0.70–0.85 较易 / 0.50–0.70 中等 / 0.30–0.50 较难 / <0.30 难 |
| 区分度 `D` | 总分前 27% 与后 27% 的**得分率之差** | ≥0.40 很好 / 0.30–0.40 良好 / 0.20–0.30 尚可 / <0.20 差（负值提示题目或答案有问题） |
| 信度 `α` | Cronbach's α（全卷各题得分的内部一致性） | ≥0.90 优 / 0.80–0.90 好 / 0.70–0.80 可接受 / <0.70 偏低 |
| 内容效度 | 考点覆盖率 + 逐题挂点率 | 越高越好；未覆盖知识点数一并给出 |

★ **三条诚实边界（不得违反）**：

1. **不提供效标效度** —— 它需要外部效标（另一次权威考试成绩），系统里没有；
   编一个数字出来比不给更糟。报告里如实说明（`validity.note`）。
2. **样本不足时 `value = null` + `reason`**，不给假数字：
   信度至少需要 2 名学生、2 道计分题且总分有方差；
   区分度至少需要 4 名有成绩的学生。
3. **只统计有成绩的学生**：把缺考的人按 0 分算进来，
   会把难度与信度一起系统性拖低。

★ 公式与判据在 `apps/analytics/measurement.py`（纯函数，可穷举单测；
手算校验见 `scripts/verify_paper_measurement.py`）。

## 五、错误码总表

> 主干来自 README 第六节，**不新增冲突码**；新增码见 5.2。格式 `<模块>.<语义>`，全大写下划线。

### 5.1 全量错误码（含触发条件与前端处理）

| 错误码 | HTTP | 触发条件（设计依据） | 前端处理建议 |
| --- | --- | --- | --- |
| `AUTH.INVALID_CREDENTIALS` | 401 | 登录校验失败（连败 5 次锁定） | 表单级提示并停留；`detail.locked_until` 存在时显示解锁时间 |
| `AUTH.TOKEN_EXPIRED` | 401 | access / refresh 过期 | 先 `/auth/refresh/` 静默续期并重放原请求（**只重放一次**），失败跳登录并保留 `redirect` |
| `AUTH.FORBIDDEN` | 403 | 角色不足（如 ASSISTANT 改机构设置）、快速模式被机构关闭 | Toast 无权限 + 「联系管理员」，**不给**重试按钮 |
| `ORG.MISMATCH` | 403 | 资源 `org_id` ≠ token `org_id` | Toast「无权访问该数据」+ 上报监控（可能是攻击） |
| `COMMON.NOT_FOUND` | 404 | id 不存在或已软删除 | 页面级空态 +「返回列表」；列表内则移除该行并 Toast |
| `COMMON.VALIDATION_ERROR` | 400 | 字段类型/范围/必填不符 | **字段级报错**（`detail.errors` 为 `{field: [msg]}`） |
| `COMMON.RATE_LIMITED` | 429 | 超过 1.7 任一限额 | Toast + 按 `detail.retry_after_s` 禁用按钮倒计时 |
| `COMMON.IDEMPOTENT_CONFLICT` | 409 | 同幂等键首请求未完成；或乐观锁 `expected_updated_at` 不符 | 复用同一幂等键重试并提示「正在处理」；乐观锁场景提示「数据已被他人修改，请刷新」 |
| `QUESTION.DUPLICATE` | 409 | `stem_hash` 命中或 SimHash 相似度超阈（7.6） | 抽屉展示相似题对比 + **「仍要新建」**（`force=true` 重放） |
| `QUESTION.NOT_APPROVED` | 409 | 换题/锁定选了非 `APPROVED` 题（7.7.4 规则 3） | Toast + 「去审核」 |
| `QUESTION.NO_KNOWLEDGE_TAG` | 400 | `knowledge_tags` 为空 | 表单定位知识点字段并聚焦 |
| `INGEST.PARSE_FAILED` | 422 | 加密 PDF / 无文本层且视觉识别失败 / Word 结构异常（7.3） | 结果页给原因 + 「换文件」/「手动录入」 |
| `INGEST.SEGMENT_UNCERTAIN` | 200 | `segment_uncertain_count > 0`（**非错误**） | 直接跳 `T07`（`?filter=uncertain`），**不得**弹错误 |
| `FIGURE.PENDING_CONFIRM` | 409 | 批次 `finish`、试卷定稿、发布时仍有 `review_status=PENDING`（三闸门） | 弹层「还有 N 张配图未确认」+ 主按钮跳 `T37`/`T38`（`detail.figure_review_url`） |
| `FIGURE.BATCH_CONFIRM_REJECTED` | 400 | 4.6 四个拒绝条件之一 | 展示 `detail.reasons[]`；按钮置灰并显示不满足条件 |
| `FIGURE.MISSING_REFERENCED` | 409 | 含图引用词 + `has_figure=true` + 配图数 0（7.4.8.5） | 阻断定稿/发布；列题号并可跳关口二对应题 |
| `FIGURE.SPLIT_BY_PAGE` | 409 | 图片 bbox 与分页线相交（7.4.8.5） | 默认作 warnings 展示 + 快捷动作（下移/缩小/调留白）；仅阻断模式弹错误（G-8） |
| `SHEET.BIND_MISMATCH` | 409 | 题量 / 题型结构 / 学号位数三项校验失败（8.5） | 逐条展示 `detail.checks[]` + 三个建议动作按钮 |
| `SHEET.STUDENT_NO_OVERFLOW` | 400 | `validate_student_no_layout` 返回问题（9.2.4.1） | 展示 `detail.issues[]` + 建议（改多行每行 N 位 / 减位数 / 横向纸张），高亮 `student_no_digits` |
| `SCAN.ANCHOR_NOT_FOUND` | 422 | 锚点 < 4 或回投影偏差超限（9.3.2 ②③） | 手机端**立即**弹「识别失败 + 重新拍摄」单按钮；Web 端进人工队列并给重传入口 |
| `SCAN.STUDENT_NO_UNMATCHED` | 200 | 识别学号不在名册（**非错误**） | 不弹错误；进学号确认区并聚焦「改为其他学生」选择器（14.3） |
| `SCAN.STUDENT_NO_AMBIGUOUS` | 409 | 多位多涂 / 同学号冲突 / 目标学生已有卡 | 进人工队列；展示冲突列表由教师选择，**不自动覆盖** |
| `GRADING.SCORE_EXCEED_MAX` | 400 | 录入分 > 满分；总分分摊超上限 | **字段级报错**（输入框标红 + 阻止保存，10.5 第 4 条），并显示得分率预览 |
| `GRADING.NO_RESPONSE` | 409 | 该生缺考/无 `Response` 却提交主观分 | Toast + 「标记缺考」按钮 |
| `GRADING.ASSIGNMENT_NOT_GRADED` | 409 | 生成练习前未确认成绩（10.6） | Toast + 「去确认成绩」跳批改工作台 |
| `PAPER.CANDIDATE_INSUFFICIENT` | 409 | 组卷/练习生成可用题不足（8.7、11.7.2） | 展示 `detail.gaps[]` + 「导出缺口清单」/「去导入试卷」 |
| `PAPER.EQUIVALENCE_FAILED` | 409 | 期望得分率差 > 0.03 / Jaccard < 0.8 / 难度分布差超限（8.3） | 展示差异题与建议换题；允许「仍要采用」并留审计 |
| `AI.PROVIDER_UNAVAILABLE` | 503 | 全部候选 provider 失败或超时（6.3） | AI 按钮置灰 + 「暂不可用，可手动完成」，基础功能不受影响（15.2） |
| `AI.SCHEMA_INVALID` | 502 | 结构化输出校验失败（6.1） | 内部已重试；达 `max_retry` 后提示重试或手动编辑，保留原输入 |
| `AI.BUDGET_EXCEEDED` | 402 | 超机构日预算（6.6：先停生题） | Toast 联系管理员；按钮置灰并显示额度重置时间 |
| `AI.KB_INSUFFICIENT` | 200 | L1 命中 < `kb_min_entries`（**非错误**） | 不弹错误；展示 `message` + 「去补充知识库」（`suggestion="kb_entry"`）+ `coverage_ratio` |
| `AI.CITATION_INVALID` | 502 | 引用编号不在注入列表；或 `kind=NO_CITATION`（6.5 L3） | 内部重试一次；仍失败则标注「引用校验未通过」并禁止一键采纳，允许查看原始输出 |
| `EXPORT.TASK_RUNNING` | 409 | 同 `source_assignment_id` 已有导出任务 | Toast + 「查看进度」（`detail.running_task_id`） |
| `EXPORT.LAYOUT_ISSUES` | 409 | `layout_issues` 非空且 `auto_fix=false` | 展示问题清单；给「自动修正后重试」与「导出后手动调整」，**不静默导出** |

### 5.2 本次新增错误码

以下 5 码在 README 第六节中**不存在**，均因设计文档定义了明确失败分支却未分配错误码而新增，**不与任何现有码冲突**，需同步回写 README 第六节。

| 错误码 | HTTP | 含义 | 新增理由（设计依据） | 前端处理建议 |
| --- | --- | --- | --- | --- |
| `ASSIGNMENT.WINDOW_CLOSED` | 409 | 不在作答时间窗内 | 9.5 要求学生入口与作答会话校验时间窗；`AUTH.*` 语义不符（学生无账号） | 学生端全屏提示「本次作业已于 X 关闭」+ 教师联系方式；`detail.start_at` / `detail.end_at` |
| `ASSIGNMENT.ALREADY_SUBMITTED` | 409 | 已提交且不允许补交 | 9.5 明确要校验「是否已提交」，`allow_late=false` | 显示提交时间与结果入口（按 `score_visibility`），禁用作答按钮 |
| `ASSIGNMENT.PAPER_NOT_FINALIZED` | 409 | 试卷未定稿，不能发布 | 7.4.8.1 试卷定稿门禁 + 12.5 `paper.status` 含 `FINALIZED` | Toast + 「去试卷校验」跳转 `T38` |
| `FIGURE.DUPLICATE_ASSET` | 409 | 命中去重（pHash 相似） | 16.4 ★手动截图要求「phash 命中已有图时提示重复而非新建」 | 弹层展示已有图缩略图 + 「复用这张图」/「仍要新建」 |
| `SHEET.TEMPLATE_VERSION_IMMUTABLE` | 409 | 模板版本只增不改 | 12.6 明确 `version` 只增不改 | 提示「模板已生成，无法原地修改」+ 「另存为新版本」 |

---

## 六、OpenAPI 生成与契约测试

### 6.1 drf-spectacular 配置要点

```python
# server/zhifou/settings/base.py
REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.accounts.authentication.ZfJWTAuthentication",
        "apps.assignment.authentication.AssignmentTokenAuthentication",
        "apps.assignment.authentication.StudentSessionAuthentication",
    ],
    "EXCEPTION_HANDLER": "apps.common.exceptions.zf_exception_handler",  # 输出 1.2 的错误信封
}

SPECTACULAR_SETTINGS = {
    "TITLE": "知否题库管理与 AI 精准作业平台 API",
    "VERSION": "v1",
    "DESCRIPTION": "契约以 docs/tech/05-API契约规范.md 为准；schema 由该文档约束生成。",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v1",
    "COMPONENT_SPLIT_REQUEST": True,          # 请求/响应 schema 分离，便于前端生成类型
    "SORT_OPERATION_PARAMETERS": False,
    "COMPONENT_NO_READ_ONLY_REQUIRED": True,
    "POSTPROCESSING_HOOKS": ["apps.common.openapi.add_deprecation_headers"],  # 写入 Deprecation/Sunset（§7.2）
    # 关键：枚举名必须与 README 第五节「状态枚举总表」一一对应，
    # 否则同一取值集合在不同接口会生成匿名枚举，前端无法复用类型。
    "ENUM_NAME_OVERRIDES": {
        "QuestionStatusEnum": "apps.questionbank.choices.QUESTION_STATUS",
        "QuestionSourceTypeEnum": "apps.questionbank.choices.QUESTION_SOURCE_TYPE",
        "FigureReviewStatusEnum": "apps.questionbank.choices.FIGURE_REVIEW_STATUS",
        "FigureConfirmMethodEnum": "apps.questionbank.choices.FIGURE_CONFIRM_METHOD",
        "ResponseSourceEnum": "apps.response.choices.RESPONSE_SOURCE",
        "ScoreMethodEnum": "apps.grading.choices.SCORE_METHOD",
        "ScoreStateEnum": "apps.grading.choices.SCORE_STATE",
        "ScanCardStatusEnum": "apps.omr.choices.SCAN_CARD_STATUS",
        "ScanBatchStatusEnum": "apps.omr.choices.SCAN_BATCH_STATUS",
        "CaptureChannelEnum": "apps.omr.choices.CAPTURE_CHANNEL",
        "AssignmentStatusEnum": "apps.assignment.choices.ASSIGNMENT_STATUS",
        "AssignmentStudentStatusEnum": "apps.assignment.choices.ASSIGNMENT_STUDENT_STATUS",
        "ComposeModeEnum": "apps.paper.choices.COMPOSE_MODE",
        "PaperPurposeEnum": "apps.paper.choices.PAPER_PURPOSE",
        "PromptTemplateStatusEnum": "apps.ai.choices.PROMPT_TEMPLATE_STATUS",
        "KnowledgeEntryTypeEnum": "apps.knowledge.choices.ENTRY_TYPE",
        "KnowledgeEntryStatusEnum": "apps.knowledge.choices.ENTRY_STATUS",
        "PracticeSetStatusEnum": "apps.practice.choices.PRACTICE_SET_STATUS",
    },
}
```

补充要求：① 每个 View 用 `@extend_schema(responses={200: XxxSerializer, 409: ErrorEnvelope, ...})` **显式声明错误响应**，禁止统一写 500；② `202` 响应统一引用 `AsyncTaskAcceptedSerializer`（字段同 README 第七节），不得各写一遍；③ `task_type` 用 `enum` 显式取自 §1.6 注册表，保证前端生成联合类型；④ 签名 URL 标注 `readOnly: true` 且描述「有效期 ≤ 5 分钟」，避免前端持久化。

### 6.2 schema 导出与 CI 门禁

```bash
cd server
python manage.py spectacular --file ../docs/openapi/zhifou-v1.yaml --validate --fail-on-warn

# CI：schema 与代码不一致则阻断合并
python manage.py spectacular --file /tmp/schema.yaml --validate
diff -u ../docs/openapi/zhifou-v1.yaml /tmp/schema.yaml \
  || (echo "OpenAPI schema 已变更，请重新导出并更新 docs/openapi/zhifou-v1.yaml"; exit 1)
```

规则：**接口变更必须先改本文档 → 再改代码 → 再导出 schema**（README 第十节第 3 条）。CI 同时校验 §1.6 的 `task_type` 注册表与代码任务类型枚举一致。

### 6.3 前端 TS 类型生成

```bash
# web/package.json: "api:types": "openapi-typescript ../docs/openapi/zhifou-v1.yaml -o src/types/api.d.ts"
pnpm api:types
```

```typescript
// src/api/questionbank.ts
import type { components } from '@/types/api'
type QuestionCreate = components['schemas']['QuestionCreateRequest']
type Question = components['schemas']['Question']

export async function createQuestion(payload: QuestionCreate): Promise<Question> {
  const { data } = await http.post<{ data: Question }>('/questions/', payload)
  return data.data
}
```

约定：生成物 `src/types/api.d.ts` **提交入库**并标注 `// auto-generated, do not edit`；枚举统一「联合类型 + 兜底宽类型」（`type QuestionStatus = components['schemas']['QuestionStatusEnum'] | (string & {})`），保证后端新增枚举值时不炸；**禁止**手写与后端重复的接口类型。

### 6.4 契约测试

| 层 | 做法 |
| --- | --- |
| 响应结构校验 | pytest + `jsonschema`：取生成 schema 的响应组件对真实响应 `validate()`；每模块至少列表 / 详情 / 错误分支各 1 条 |
| 全量契约 | `schemathesis run docs/openapi/zhifou-v1.yaml --base-url http://localhost:8000/api/v1 --checks all`（CI 对 staging 执行） |
| 错误信封 | 断言所有 4xx/5xx 满足 `{"error": {"code", "message", "detail"}}` 且 `code` 在第五节总表内，**出现表外错误码即失败** |
| 枚举一致性 | 断言代码枚举值集合 == README 第五节总表（README 第十节第 5 条） |
| 幂等 | 同 `Idempotency-Key` 连续两次调用，第二次返回首次响应体，且 AI 任务不重复计费（16.4） |
| 分页边界 | `page_size=101`、`page=0` 必须 400 |

---

## 七、版本与兼容策略

### 7.1 何时升 `/api/v2/`

**破坏性变更（必须升 v2 并双版本并行）**：删除或重命名响应字段（如删除 `question.has_figure`）；修改字段语义或单位（如 `mastery` 改成 0–100，违反 README 第八节）；修改枚举取值集合（删除或改义）；修改错误码与 HTTP 状态码映射；修改分页信封结构；修改认证方式语义（如 `assignment_token` 改为可复用）。

**兼容性变更（留在 v1）**：新增可选请求字段（服务端必须有默认值）；新增响应字段（前端必须忽略未知字段）；**新增枚举值**（前端类型必须容忍，未知值展示原始文本 + 灰底，禁止白屏）；新增接口或 `task_type`（先改本文与 README）；放宽校验（如 `page_size` 上限提高）。

### 7.2 字段废弃流程（先加后删、双写期）

```text
第 1 步（v1.x）新增替代字段，旧字段保留并双写；文档标注 > ⚠ 已废弃（替代：new_field，计划移除：2027-03-31）
               响应头加 Deprecation: true / Sunset: Wed, 31 Mar 2027 00:00:00 GMT
第 2 步（双写期 ≥ 90 天）前端完成迁移；后端监控旧字段读取量
第 3 步（下一个小版本）读取量为 0 且满 90 天后移除旧字段，文档加删除记录；移除前 14 天通知机构管理员
```

硬性规则：**禁止同一 PR 里既加替代字段又删旧字段**；停止顺序是「先停止返回 → 再停止接受 → 最后删列」（删列走 02 文档迁移流程）；每个废弃字段必须在本文「变更记录」与 README 可追溯。

### 7.3 前端兼容处理

| 场景 | 做法 |
| --- | --- |
| 未知枚举值 | `mapStatusLabel(status)` 的 `default` 分支返回 `{ text: status, tone: 'neutral' }`，不抛异常 |
| 未知错误码 | 兜底 Toast `error.message` 并上报监控；**不显示**裸错误码 |
| 字段缺失 | 可选字段一律 `?.` + 默认值；禁止在 store 里用 `!` 断言后端字段必存在 |
| v1/v2 并存 | 仅在 `src/api/http.ts` 一处配置 `baseURL`（`import.meta.env.VITE_API_BASE`），业务代码不得拼版本号 |
| 契约漂移 | CI 跑 `pnpm api:types && git diff --exit-code src/types/api.d.ts`，schema 变了必须显式提交类型更新 |

---

## 八、待确认问题

> 阅读 README 与设计文档 v2.1 时发现的**缺口与矛盾**。本文按最小假设落地并在此登记，**未自行发明表、接口或字段**。

| 编号 | 类型 | 问题 | 本文临时口径 | 建议 |
| --- | --- | --- | --- | --- |
| **G-1** | 缺口 | `sheet.generate_template` **未指定队列**，而 README 第二节禁止新增队列名 | 暂定 `p1_generate` | 回写 README 第二节并确认 P1 并发 4 是否够用 |
| **G-2** | 矛盾 | README 第九节既说幂等键「前端生成 UUIDv4」又说「格式 `<task>:<business_id>:<prompt_version>`」 | `<business_id>` 用 UUIDv4 填充 | 在 README 补一个示例串 |
| **G-3** | 矛盾 | 批量确认与配图核对有两套入口：13.2 的 `/ingest/batches/{id}/figures/confirm-all/`、`/ingest/figures/{id}/`（PATCH 含裁切）vs 7.4.8.6 的 `/question-figures/batch-confirm/`、`/question-figures/{id}/recrop/`（以及 `capture` vs `POST /questions/{id}/figures/`） | 两套都保留并标 `†`，**强制复用同一 Service**；v2.1 的 `/question-figures/*` 为权威 | 确认后把 13.2 旧入口标 `> ⚠ 已废弃`（README 第十二节不删除） |
| **G-4** | 命名不一致 | `question_option.image_id`（FK asset）不符合 README 第四节「外键 `<表>_id`」 | 按 12.4 原文写 `image_id` | 02 文档改名为 `asset_id`，同步 01/05 |
| **G-5** | 缺口 | `question_knowledge` 被 7.7.2、11.2（`.weight`）、12.12 多处引用，但 **12.4 从未定义其字段** | 统一用 `knowledge_tags: [{node_id, weight, is_primary}]` | 02 文档补 DDL：`id / question_id / knowledge_node_id / weight / is_primary / source / unique(question_id, knowledge_node_id)` |
| **G-6** | 歧义 | 10.5 快速模式写「按题量平均分摊」，但各题满分不同（8/10/12/10），平均分摊会让 12 分题与 8 分题得分相同 | 按平均分摊实现（`strategy=EVEN`）并预留 `strategy` | 确认是否改为「按各题满分比例分摊」；若是需在 13 文档加配置项 |
| **G-7** | 缺口 | 2.3 的「题型结构例外申请」（为个别学生生成不同结构的非标准卷）**无接口、无页面、`gen_config` 无字段** | 本文不定义，`structure_constraint` 恒为 `SAME_STRUCTURE_AS_SOURCE` | 若一期支持，需补接口字段与页面入口 |
| **G-8** | 矛盾 | README 给 `FIGURE.SPLIT_BY_PAGE` 定 409（像阻断），7.4.8.5 却把分页切断定义为 ⚠ 提示，**只有「缺失」阻断定稿** | 默认 200 + `warnings`；`block_on_split=true` 时 409 | 明确二者关系并回写 README 第六节 |
| **G-9** | 缺口 | `scan_card`（12.7）**无 `assignment_id` 字段**，但 12.12 索引写 `scan_card (assignment_id, student_id)` | 从 `scan_batch.assignment_id` 取任务 | 补 `scan_card.assignment_id`，或修正 12.12 索引描述 |
| **G-10** | 缺口 | 一人一卷时 `assignment_student` 只有 `question_order` / `option_order`，**没有指向该生专属 `Paper` 的外键**（`paper.owner_student_id` 无法唯一定位） | 只回 `question_order`（`paper_item_id` 数组），靠 `paper_item_id` 反查 | 补 `assignment_student.paper_id`（一人一卷时必填） |
| **G-11** | 矛盾 | `score_result.method`：README 5.5 含 `MANUAL_AGGREGATE`，12.8 说明只列到 `MANUAL` | 以 README 5.5 为准 | 修正 12.8 |
| **G-12** | 矛盾 | `knowledge_entry.source_type`：7.2 列 4 值，12.3 多 `PLATFORM` | 以 12.3 为准（DDL 唯一来源） | 修正 7.2 并回写 README 第五节 |
| **G-13** | 措辞 | README 第四节称「任务类型枚举：大写下划线（字符串值）」，但示例 `TaskType.OCR_QUESTION = "ocr_question"` 与 `"ingest.parse"` 都是小写 | 枚举**名**大写下划线、枚举**值**小写（点分或下划线） | 修正 README 第四节措辞 |
| **G-14** | 口径冲突 | 同一指标多处不一致：配图召回率 7.4.6（≥99%/≥88%）vs A3/A4（≥98%/≥80%）vs 阶段零（同 A3/A4）；配图校对耗时 7.4.6（≤45s/≤120s）vs A6（≤90s）vs 7.4.8.2（≤40s）；OMR 静默通过率 9.3.4（80%/92%）vs A17/A18（88%/78%）vs 阶段零（88%/75%）；沉默错误率 7.4.6（≤0.5%/≤2%）vs A7（≤2%）vs Q4（零容忍） | 本文只引用，不定义验收数字 | **必须统一验收口径**（建议以 v2.1 的 7.4.6 + 7.4.8 为准并修订 19.1），否则验收必然扯皮 |
| **G-15** | 缺口 | README 第五节枚举总表**未收录**：`paper.status`、`assignment.answer_visibility` / `score_visibility`、`question_relation.relation`、`sheet_style` 全部枚举、`knowledge_entry.source_type`、`scan_card.student_no_flags` 取值 | 按 12 章原文使用 | 回写 README 第五节，否则「枚举一致性测试」无法通过 |
| **G-16** | 缺口 | `/subjective-progress/` 与 `/subjective-entry/` 返回的 `progress` 语义重叠，14.1 未说明前端从哪个接口取题号导航 | 两者都返回 `progress`，前者为轻量轮询接口 | 确认后给 `subjective-progress` 固定用途 |
| **G-17** | 缺口 | 13.2 未定义「批次完成」接口，但 7.4.8.1 / 7.4.8.6 需要它承载 `can_finalize_ingest_batch` 门禁 | 本文补 `POST /ingest/batches/{id}/finish/`，行为完全取自 7.4.8.6 | 确认后回写设计文档 13.2（**只补承载门禁的入口，不发明语义**） |
| **G-18** | 缺口 | 学生端时间窗/重复提交的错误码在 README 第六节无归属 | 新增 `ASSIGNMENT.WINDOW_CLOSED` / `ASSIGNMENT.ALREADY_SUBMITTED` | 回写 README 第六节 |

---

## 变更记录

| 版本 | 日期 | 变更人 | 说明 |
| --- | --- | --- | --- |
| v1.0 | 2026-10-01 | 架构组 | 首版：通用约定、15 个模块完整接口清单（含 7.4.8.6 的 v2.1 配图接口）、21 组重点接口契约、异步任务 `result` 结构与 `task_type` 注册表、错误码总表（新增 5 码）、OpenAPI 与契约测试方案、版本与兼容策略、18 项待确认问题（G-1–G-18） |
| **v1.1** | **2026-10-17** | — | **补记需求 3 已实现的接口**（文末 `§补充：知识库管理与 AI 接入`）：条目 `GET/PATCH/DELETE /knowledge/entries/{id}/` 与 `KNOWLEDGE.ENTRY_IN_USE`(409)；AI 厂商接入的环境变量口径。★ 只补**已实现且有回归**的部分 |
| **v1.2** | **2026-10-17** | — | 追加 **S-5 `POST /knowledge/import/extract/`**（文档文本抽取，`P1-18b` 接线）：权限 `IsOrgAdmin`、请求字段 `file`、响应 = `ExtractedDoc.as_dict()` + `needs_ocr` / `supported_extensions` / `max_bytes`、400/422 文案与「扫描件走 200」的口径；★ 同时把它从上一版「未实现名单」里移出 |
| **v1.3** | **2026-10-17** | — | 追加 **S-6 `POST /knowledge/import/parse/`**（AI 拆条 + 自动归类，**预览不落库**）与 **S-7 `POST /knowledge/import/commit/`**（老师确认后入库，`created/skipped/failed` 三桶 + 逐条审计）：`P1-18c`。★ 只写实测通过的内容（`scripts/verify_knowledge_import_parse.py` 107 项）；新增任务类型 `parse_kb_document`（默认路由仍 `echo`，不设 `ZF_AI_ROUTES` 就不联网）与审计动作 `knowledge_entry.import`，迁移 `ai/0004` + `audit/0005` |

---

## 补充：知识库管理与 AI 接入（2026-10-17 实现，需求 3）

> ★ 收录规则：**只写已经有回归脚本、且实测通过的接口**。
> 未实现的（导入向导的批量循环、练习打印包导出等）
> **不写进契约** —— 写进去会让前端照着一个不存在的接口开发。
>
> ★ 2026-10-17 更新：`POST /knowledge/import/extract/` 已实现并实测通过，
> 从下面这份「未实现」名单里移出，见 **S-5**。
>
> ★ 2026-10-17 再更新（v1.3）：**AI 拆条归类已实现并实测通过**
> （`P1-18c` → **S-6** parse 预览 / **S-7** commit 入库），
> 一并从「未实现」名单里移出。

### S-1 `GET /api/v1/knowledge/entries/{id}/` —— 条目详情

- 权限：**教师及以上**（`IsTeacherOrAbove`）。
- 响应：与列表项同构（含 `id/title/content/entry_type/status/node_ids/…`）。
- 不存在的 id → `404 COMMON.NOT_FOUND`；**跨机构** id → 同样 `404`（不泄漏「它存在但不属于你」）。

### S-2 `PATCH /api/v1/knowledge/entries/{id}/` —— 更新条目

- 权限：**机构管理员**（`IsOrgAdmin`）。
- 请求体：**显式白名单**（`EntryUpdateSerializer`），支持 `title` / `content` / `entry_type` / `node_ids` 等可编辑字段。
  ★ **派生字段一律 400**（不是静默忽略）：外键 id、计数、`status` 之类由服务层或流程决定的字段传进来会被显式拒绝 —— 静默忽略会让调用方以为写进去了。
- 依赖闸门（**需求方口径 Q7**）：**该条目关联的知识点下没有任何题目时，才允许改/删**。
  - 粒度：**按知识点整体判断**（不是逐条判断）；一个条目可关联多个知识点，**任一有题即拒**。
  - 改 `node_ids` 时，**新旧关联集合一起过闸**（否则留了「先改挂、再删」的绕道）。
  - 「有题」= `QuestionKnowledge` 关联且题目**未软删**；★ **草稿题也算**（保守：L1 门禁不要求先有 APPROVED 题，而「有草稿题」恰恰说明有人正在这点上出题；放行代价不可逆，拦住只是多点一下）。
- 失败：`409 KNOWLEDGE.ENTRY_IN_USE`，`data.blocking = [{node_id, code, name, question_count}…]`。
  `message` 是一句**可照做**的话，例如：
  「不能修改/删除该知识库条目：该条目关联的知识点下已有题目——JXJC-C01-T01-K01（金属材料性能分类，150 题）。……请先处理这些知识点下的题目（或先把条目改挂到没有题的知识点上）」
- 成功：写审计 `knowledge_entry.update`，`before`/`after` 均为条目快照（**含 `node_ids`**）。
- 回归：`scripts/verify_knowledge_entry_crud.py`（**98 项**；其中 R10 专测
  「改挂时**摘掉**有题知识点 → 409 且关联不变」这条曾漏判的绕道）。

### S-3 `DELETE /api/v1/knowledge/entries/{id}/` —— 删除条目

- 权限：**机构管理员**。**软删**（打 `deleted_at`），行仍在库里 —— 已印出的试卷与已生成的练习快照还要按条目 id 追溯。
- 写审计 `knowledge_entry.delete`（`before` = 快照，`after` = `null`）；**幂等**（重复 DELETE 不重复写审计）。
- 依赖闸门与失败同上（`409 KNOWLEDGE.ENTRY_IN_USE`）。
- ★ **归档 vs 删除**（两者语义不同，别混用）：

  | 操作 | 端点 | 行为 |
  | --- | --- | --- |
  | **归档** | `POST /knowledge/entries/{id}/archive/`（既有） | 只改 `status`，**行不软删**，可逆，不写审计，**不过** Q7 闸门 |
  | **删除** | `DELETE /knowledge/entries/{id}/`（本次新增） | 打 `deleted_at`，默认查询不可见，**必须写审计**，**过** Q7 闸门 |

### S-4 AI 厂商接入的环境变量（部署级，非接口）

> 完整口径见 [13-配置项与提示词库.md](13-配置项与提示词库.md) §10.0。

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `ZF_AI_API_KEY` | 无 | **必须显式设置**；缺失时 AI 调用抛 `ProviderUnavailable`（「配置问题、重试无用」） |
| `ZF_AI_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI 兼容接口地址 |
| `ZF_AI_MODEL` | `deepseek-chat` | 默认模型 |
| `ZF_AI_ROUTES` | 空 | 任务级路由覆盖（JSON）；★ 非法配置**忽略整份**并回落平台默认（`echo`） |

★ 现状提示：`DEFAULT_ROUTES` **全部是 `echo`（不联网）** —— **不设 `ZF_AI_ROUTES` 时不会真的调模型**。
回归：`scripts/verify_ai_provider_openai.py`（离线 **111 项**；`--live` 探针需凭据）。

### S-5 `POST /api/v1/knowledge/import/extract/` —— 文档文本抽取（上传 → 返回文本）

> `P1-18b` 的抽取层（`apps/knowledge/extract.py`）接上 HTTP 的**唯一**入口。
> ★ 本端点只做「**文件字节 → 文本**」：**不入库、不建条目、不调 AI**
> （拆条与自动归类是 `P1-18c`）。因此它天然**幂等**：同一个文件传两次响应逐字相同，库里一行不变。

- **权限**：**机构管理员**（`IsOrgAdmin`）。
  ★ 它会把**整份文档内容**读出来放进响应，是「把外部资料批量读进系统」的入口，
  与条目改动同一档（条目的更新/删除同样是机构管理员）。
  实测：教师（`TEACHER`）→ `403 AUTH.FORBIDDEN`；未登录 → `401 AUTH.TOKEN_EXPIRED`。
  ★ 抽取层本身与角色无关 —— 将来若要让教师直接用，只改视图那一行权限。
- **请求**：`multipart/form-data`，字段名 **`file`**，**一次一个文件**
  （多文件由导入向导在客户端逐个调用；「第 3 个失败时前 2 个算不算成功」
  这类部分失败语义**不在本端点定义**）。
  缺字段 / 字段名写错 / 用 JSON 传文件名 → `400`，`message` 直接点名 `file`：
  「缺少 file 字段：请用 multipart/form-data 上传一个文件（字段名 file），支持后缀 .docx / .pdf / .txt」。
- **响应 `data`**：`ExtractedDoc.as_dict()` 的**全部字段**（字段名与它逐一对应，**不另起一套**）：

  `filename` / `source_type`（`DOCX` / `PDF` / `TXT`）/ `text` / `char_count` /
  `non_whitespace_count` / `page_count`（pdf 才有，否则 `null`）/ `page_char_counts` /
  `paragraph_count`（docx / txt 才有）/ `encoding`（txt 才有）/ `byte_size` /
  `image_count` / `heading_count` / `is_empty` / `warnings` /
  `blocks[{level, text, kind, page}]`

  ＋ 三个**只为前端分支**的新字段：

  | 字段 | 类型 | 说明 |
  | --- | --- | --- |
  | `needs_ocr` | bool | 疑似扫描件（等价于 `warnings` 含 `SCAN_PDF_WARNING`） |
  | `supported_extensions` | string[] | 当前支持的后缀，实测 `["docx","pdf","txt"]` |
  | `max_bytes` | int | 本次生效的上限，默认 `20971520`（20MB） |

  ★ `blocks[].level` 是**标题层级**（`1..6`；`0` = 正文），目前只有 `.docx` 会给出；
  它是 `P1-18c`「按标题层级切条」的唯一依据 —— **别在客户端重新推断层级**。
  ★ 不变式经 HTTP 仍成立：`"\n".join(b.text for b in blocks) == text`（docx / pdf）。
- ★★ **扫描件 / 空文档不是错误**：HTTP **200** + `warnings` 提示。

  | 情形 | 实测结果（HTTP 端点回归里的实测值） |
  | --- | --- |
  | 扫描件 PDF（无文本层） | `200`，`needs_ocr=true`，`warnings` 含「疑似扫描件：未取到文本层，需要 OCR（一期不做配图/OCR 抽取，见 P3-09）」（前端按**「扫描件」三字**分支） |
  | 纯图片 docx | `200`，`is_empty=true`，`image_count≥1`，`warnings` 说「文档未取到文本，但含 N 张图片…需要 OCR」 |
  | 全空白 txt | `200`，`is_empty=true`，`warnings` 说「文本为空或仅含空白字符：没有可导入的知识内容」 |

  ★★ **`needs_ocr` 只等价于「扫描件 PDF 那句常量」**：图片版 docx 同样需要 OCR，
  但它的 `needs_ocr` 仍是 `false`（文案不同）。前端要提示 OCR 时**不能只看 `needs_ocr`**，
  得同时看 `warnings` / `is_empty` —— 这是刻意的分工（业务分支靠 `warnings`，`needs_ocr` 只是给最常见的扫描件一个布尔快捷方式）。

  ★ **不要把它改成 4xx**：本期不做的只是 OCR，老师仍可以拿到（可能为空的）文本自己贴；
  报错会让他以为上传失败而反复重试（与 `INGEST.SEGMENT_UNCERTAIN` /
  `PRACTICE.BANK_INSUFFICIENT`「非错误走 200」同一口径）。
- **失败码**（判定与文案的唯一真源是 `apps/knowledge/extract.py`，本端点不改写 `message`）：

  | HTTP | `code` | 触发条件 |
  | --- | --- | --- |
  | 400 | `COMMON.VALIDATION_ERROR` | 缺 `file` / 0 字节 / 后缀不在白名单 / 扩展名与内容（魔数）不符 / 超过 `max_bytes` |
  | 422 | `INGEST.PARSE_FAILED` | 内容侧读不出来：docx 不是合法 OOXML、PDF 结构损坏或已加密 |

  400 文案（**实测原文**）：

  - `.doc` →「不支持旧版 .doc：请在 Word 里「另存为」.docx 后重新上传」
  - `.md` →「不支持的文件类型 .md，仅支持 .docx, .pdf, .txt」
  - `.docx` 里装的是 PDF →「文件内容与后缀 .docx 不符：内容实际是 PDF，请使用正确的后缀重新上传」
  - 空文件 →「文件内容为空（0 字节），请确认上传的文件是否完整」
  - 超限 →「文件过大：1KB 超过上限 1KB，请拆分后分批上传」，
    ★ `data = {filename, byte_size, max_bytes}`（前端可直接显示两个尺寸；**`byte_size` 是实测字节数**）

  422 文案（**实测原文**）：

  - 加密 PDF →「PDF 已加密，无法抽取文本：请先解除密码保护再上传」
  - zip 损坏的 docx →「Word 文档解析失败（文件可能已损坏或不是真正的 .docx）：…」
  - PDF 结构损坏 →「PDF 打开失败（文件可能已损坏）：…」
- **上限**：默认 **20MB**，与 `media.storage.KIND_MAX_BYTES["KNOWLEDGE_ATTACH"]`
  **同源**（`extract.DEFAULT_MAX_BYTES`）；上限**含等号**（恰好 20MB 通过）。
  部署级可用 `settings.ZF_KNOWLEDGE_IMPORT_MAX_BYTES` **收紧**它
  —— ★ 它**不是请求参数**：上限若由客户端指定，就等于没有上限。
- **不落盘、不写库**（实测）：整整一个上传请求只发 **1 条 SQL**（JWT 鉴权查账号
  `SELECT "user".…`），**抽取调用本身 0 条 SQL**，没有任何 `INSERT/UPDATE/DELETE`；
  响应里没有 `asset_id` / `oss_key` 之类字段，上传的临时文件随请求回收。
- **方法**：只有 `POST`。已认证用户用别的方法请求该路径 → `405`
  （`Allow: POST, OPTIONS`，`OPTIONS` 是 DRF 自省）；
  ★ 未登录时**先判鉴权** → `401`（不是 `405`）。
- **回归**：`scripts/verify_knowledge_import_extract_api.py`（**103 项**，跑真库 PostgreSQL，
  清理后 8 个计数回基线）；
  抽取层的算法与边界另有 `scripts/verify_knowledge_extract.py`（**39 项**，离线、0 条 SQL）。

### S-6 `POST /api/v1/knowledge/import/parse/` —— AI 拆条 + 自动归类（**预览，不落库**）

> `P1-18c` 的 AI 拆条归类。★ 它是三步链的第二步：
> **extract（免费、抽文本）→ parse（花钱、不落库）→ commit（老师确认后才入库）**。
> 服务层：`apps/knowledge/import_service.py::parse_document`。

- **权限**：**机构管理员**（`IsOrgAdmin`），与 S-5 / S-2 / S-3 同档。
  实测：教师（`TEACHER`）→ `403 AUTH.FORBIDDEN`（message「无操作权限」）；
  未登录 → `401 AUTH.TOKEN_EXPIRED`。
- **请求**：`application/json`

  | 字段 | 必填 | 说明 |
  | --- | --- | --- |
  | `text` | ✅ | **已抽取的文本**（S-5 响应里的 `data.text`） |
  | `filename` | | 只用于 `source_ref` 与提示词回显（≤255） |
  | `node_hints` | | 知识点 id 数组（≤50）；服务端解析出编码与名称一起送给模型 |
  | `batch_ref` | | 批次标识（原样回显，便于与 commit 对账；S-7 要求必填） |

- ★★ **本端点收 `text` 而不是 `file`**（这是**成本口径**，不是实现方便）：
  「不能把整份文档的 AI 费用花在老师没看过文本之前」。
  收文件的话，选错文件 / 扫描件 PDF / 20MB 旧教材都会在老师看清内容**之前**
  触发几十次模型调用；收 `text` 则强制先走 S-5（免费、不调 AI）。
  ★ 附带好处：`parse` 是输入的**纯函数**（同一段文本 → 同一批候选），
  「零副作用」因此可以被离线、确定性地验证。
- ★ **不必传 `blocks`**：`text` 里 `.docx` 的标题行带 `#`/`##` 前缀
  （S-5 已说明 `blocks[].level` 是它的结构版），所以「按标题层级切条」
  只需要 `text`。无标题（pdf / txt）时按段落 + 长度（4000 字符/块）切。
- **响应 `data`**：

  | 字段 | 说明 |
  | --- | --- |
  | `candidates[]` | `{index, title, content, entry_type, source_ref, suggested_node_ids, confidence, warnings, section_no, heading_path}` |
  | `dropped[]` | 结构性坏候选（`{section_no, reason, title}`；`reason ∈ {EMPTY_CONTENT, NOT_AN_OBJECT, TOO_MANY_IN_BLOCK}`） |
  | `errors[]` | 某一块的 AI 失败（`{section_no, code, detail, call_log_id}`） |
  | `blocks[]` | 切块结果（`{section_no, heading_path, chars}`） |
  | `node_hints` | `{used, dropped, requested, over_cap}` |
  | `ai` | `{calls[], status_counts, cost_yuan_total, all_ok}`；每次调用含 `call_log_id` / `status` / `provider` / `model` / `input_tokens` / `output_tokens` / `cost_yuan` / `latency_ms` / `retries` |
  | `prompt_template_key` / `prompt_source` | 实测 `jxjc.secondary_vocational.parse_kb_document` / `BUILTIN`（见下） |
  | `persisted` | ★ **恒为 `false`**（契约：本端点零副作用） |

- ★ **提示词模板**：走既有机制（`matcher_key(subject, band, task)` +
  `PromptTemplateSelector.get_active`，机构覆盖平台、学段覆盖通用）。
  ★ 本项目 `ai_prompt_template` 表**没有种子迁移**（实测 0 行，模板由
  `POST /api/v1/ai/prompt-templates/` 运行期写入），所以查不到行时回落
  `import_service.DEFAULT_SYSTEM_PROMPT`（内置），但
  `prompt_template_key` **照常记录** —— `ai_call_log` 里留空会让这次调用
  无法复现「当时用的是哪版提示词」。
- ★ **归一化与过滤**（模型输出必然是脏的，所以逐条兜底，**不丢整块**）：

  | 情形 | 实测行为 |
  | --- | --- |
  | `entry_type` 写小写 `common_error` | 归一成 `COMMON_ERROR`（合法值不产生 warning） |
  | `entry_type` 写不存在的 `DEFINITION` | **回落 `CONCEPT`** 并写进该条 `warnings`（`"entry_type 'DEFINITION' 不在 8 种类型内，已回落 CONCEPT"`）——内容不丢，且不是静默改 |
  | `suggested_node_ids` 含不存在 / **跨机构** id | **过滤掉**，并把被滤的 id 列进该条 `warnings`（实测：模型给 `[4, 99999999, 192]`，机构 1 的响应里只剩 `[4]`） |
  | `node_hints` 含不存在 / 跨机构 id | 同样过滤，记在 `node_hints.dropped`（实测 `[191, 99999999]`） |
  | `confidence` 给 `1.5` | clamp 到 `1.0` |
  | `source_ref` 为空 | 回落成 `「文档名 · 标题路径」`（不留空——AI 拆条入库的条目没有别的溯源手段） |
  | `content` 为空 | 进 `dropped`（`EMPTY_CONTENT`），不占用候选位置 |

  ★★ **`suggested_node_ids` 的判据是「在本次 `node_hints` 里」**，不是「在本机构存在」：
    模型不可能知道我们没告诉它的 id，它给的任何 id 都只能是猜的。
    因此**不传 `node_hints` 时建议必然为空**（并给该条加一条「请老师补选知识点」的
    warning）——前端要拿到自动归类，就必须把相关章节的知识点 id 作为 `node_hints` 传进来。
    放行「恰好猜中真实 id」的关联，会把内容挂到毫不相关的知识点上，而这种错误入库后
    看起来完全合法、极难发现。

- ★★ **模型返回坏 JSON 不是 500**（实测）：编排层追加纠正提示重试**一次**，
  仍不合格 → `errors[0] = {code: "SCHEMA_INVALID", detail: "输出不是合法 JSON"}`
  且该次调用 `retries = 1`，HTTP 仍 **200**，`candidates` 为空，
  `ai.status_counts = {"SCHEMA_INVALID": 1}`。
  ★ 逐块独立：一块坏不影响其余块。
  ★ 口径与「扫描件走 200」一致：AI 没答好是**业务分支**，不是服务器坏了。
- ★ **零副作用**（实测，两种独立判据）：

  | 判据 | 实测 |
  | --- | --- |
  | `parse` 前后 `knowledge_entry` 行数 | `110 -> 110`（`knowledge_entry_node` 同样 `110 -> 110`） |
  | 整个 HTTP 请求的 SQL | 没有任何针对 `knowledge_entry` / `knowledge_entry_node` / `knowledge_node` 的 `INSERT/UPDATE/DELETE`；写语句**只有 1 条** `INSERT INTO "ai_call_log"` |
  | 脱敏映射 | `pii_mapping` 恒为 0（`persist_mapping=False`，映射只留内存） |

- **失败**：

  | HTTP | `code` | 触发条件（实测文案） |
  | --- | --- | --- |
  | 400 | `COMMON.VALIDATION_ERROR` | 缺 `text`：「缺少 text 字段：本端点收**已抽取的文本**，请先用 POST /api/v1/knowledge/import/extract/ 上传文档拿到 text，确认内容无误后再调用本端点」 |
  | 400 | `COMMON.VALIDATION_ERROR` | `text` 全空白：「text 不能为空：请先上传文档抽取文本（本端点不接受文件，只接受文本）」 |
  | 400 | `COMMON.VALIDATION_ERROR` | 切块超上限：「文本过长：按标题/段落切出了 43 块，超过单次上限 40 块（4000 字符/块）。请拆分文档后分批解析，或先精简内容再试」，`data = {blocks: 43, max_blocks: 40, max_section_chars: 4000, text_chars: 624}` |

  ★ **超限只提示不静默截断**：静默截断会让老师以为「整份都解析了」，
  而实际后半份根本没进模型——那是「有内容说成没内容」那一类错误。
- **上限的取值理由**：单块 4000 字符（≈4k 输入 token，按平台默认路由
  text-strong 约 0.014 元/块）；单次 ≤ **40 块**。
  ★ 40 而不是「不限」：① 用户级令牌桶是 **60 次/60 秒**（`limits.TOKEN_BUCKETS`），
  40 < 60 留出余量；② 40 × 0.014 ≈ **0.56 元/文档**，超过它就该走「老师先拆文档」。
- **方法**：只有 `POST`；已认证用户用别的方法请求 → `405`（`Allow: POST, OPTIONS`）。
- **回归**：`scripts/verify_knowledge_import_parse.py`（**107 项**，离线注入
  `EchoProvider(script=[...])`，**无凭据不联网**，跑真库 PostgreSQL，
  清理后计数回基线；`--live` 分支有凭据才真调）。

### S-7 `POST /api/v1/knowledge/import/commit/` —— 老师确认后入库（三桶）

> 服务层：`apps/knowledge/import_service.py::commit_candidates`。
> ★ 形态与 `QuestionEditService.set_primary_knowledge_batch` 一致
> （逐条独立 + 三桶 + 逐条审计）。

- **权限**：**机构管理员**（`IsOrgAdmin`）。实测：未登录 `401`、教师 `403`。
- **请求**：`application/json`

  | 字段 | 必填 | 说明 |
  | --- | --- | --- |
  | `candidates` | ✅ | `parse` 返回的候选数组（老师可在前端删/改/改挂知识点后提交） |
  | `batch_ref` | ✅ | 批次标识（≤200）；写进每条审计的 `after.batch_ref` |

- ★★ `candidates` **收得很松**（只要求「是对象数组」，≤200 条）：
  字段级校验在服务层**逐条**做，坏的那条进 `failed` 桶，**不是整请求 400**。
  （单条录入端点 `POST /entries/` 用「整请求 400」是对的，那里一次只有一条。）
- **响应 `data`**：`requested` / `created` / `skipped` / `failed`
  ＋ `created_items[]` / `skipped_items[]` / `failed_items[]`
  ＋ `created_entry_ids[]`（★ 精确 id 列表，调用方按它清理或跳转，不必按标题猜）。
- **三桶口径**（实测 `created=2 / skipped=1 / failed=3`，`requested=6`）：

  | 桶 | 含义 | 实测示例 |
  | --- | --- | --- |
  | `created` | 真入库（`status=ACTIVE`、`source_type=UPLOADED_DOC`、`created_by`=操作人） | 2 条 |
  | `skipped` | **批内完全重复**（正文 + 知识点集合都相同）只建第一条 | `{reason: "DUPLICATE_IN_BATCH", duplicate_of_index: 0}` |
  | `failed` | 被拒或出错，带 `code` + `detail` | 3 条（见下） |

  ★ `skipped` 的存在理由：模型确实会在不同块里重复输出同一条内容，
    而知识库的 L1 门禁是**按条数**判的，重复条目会把它虚高。
- **`failed` 原因码**（HTTP 整体仍 **200**，因为这是批量结果）：

  | `code` | 触发条件（实测） |
  | --- | --- |
  | `KNOWLEDGE.ENTRY_IN_USE` | 该候选要挂的知识点**下已有题目**（含草稿题）→ **Q7 闸门**，与条目改/删同一个码。实测 `detail`：「不能新增条目：该条目关联的知识点下已有题目——JXJC-C01-T01-K01（金属材料性能分类，150 题）。知识库是 AI 生题的依据，有点在练时不允许改动依据；请先处理这些知识点下的题目（或先把条目改挂到没有题的知识点上）。」 |
  | `COMMON.VALIDATION_ERROR` | `content` 空 / 没有 `suggested_node_ids` / `entry_type` 不在 8 种类型内 / 知识点 id **不存在或跨机构**（实测跨机构：「存在无效的知识点 ID（不存在或不属于本机构）：4」） |
  | `UNEXPECTED` | 意外异常（已写日志）——**也记账**，否则前端会显示「失败 0」而实际少了 1 条 |

  ★ **入库侧对 `entry_type` 严格**（不回落默认值）：
    `KnowledgeEntry.entry_type` 是 `CharField(choices=...)`，而
    `create_entry()` **不跑** `full_clean()` —— 一个非法值会被**静默写进库**
    （`ai_call_log.task` 上踩过同一个坑）。预览阶段可以回落并留 warning
    （那只是建议），入库阶段必须严（写下去之后 Q7 会锁住有题的知识点）。
  ★ **Q7 用在入库**是需求方口径：知识库是 AI 生题的**依据来源**，
    「往正在练的知识点上加依据」这件事不该无声发生；拦住一条候选的代价
    只是老师先把它挂到别处。
- **逐条审计**：每条真入库的候选写一条 `knowledge_entry.import`
  （`after` = 条目快照 + `batch_ref` + `candidate_index` + `ai_task`，
  `target_type = knowledge.KnowledgeEntry`）。
  ★ 动作码是**新增**的 `knowledge_entry.import`（迁移 `audit/0005`），
  刻意不复用 `knowledge_entry.update`：审计要能区分「老师手工录入/编辑」
  与「文档拆条入库」。
- **整请求失败**：400 `COMMON.VALIDATION_ERROR` —— 缺/空 `candidates`
  （实测「candidates 不能为空：没有要入库的候选条目」）、缺 `batch_ref`、超过 200 条。
- **回归**：同 S-6 的 `scripts/verify_knowledge_import_parse.py`（**107 项**）。

★ **新增迁移**（本需求）：`ai/0004_alter_aicalllog_task_alter_aifeedback_task_and_more`
（新增任务类型 `parse_kb_document` 的 choices）、`audit/0005_alter_auditlog_action`
（新增动作 `knowledge_entry.import`）。两库（真 PG 15433 + sqlite 自检库）
均已 `migrate` 且 `makemigrations --check --dry-run` = `No changes detected`。

---

## 补充：阅卷与质量闭环（2026-10-20 实现，`docs/tech/21` 六项任务）

统一约定不变：成功 `{code:"OK", message, data}`；失败走 `ZfError` →
`{code, message, data}`，`data` 里带结构化 `extra`。

### R-1 `GET /api/v1/grading/assignments/{id}/review/items/` —— 主观题逐题进度

阅卷页左栏数据源。**只含主观题**（客观题机器批完了，列出来只会让老师白点）。

```jsonc
{ "assignment_id": 41,
  "items": [{ "paper_item_id": 812, "seq": 11, "type": "SHORT_ANSWER",
              "type_label": "简答题", "section_title": "三、简答",
              "stem_text": "…", "max_score": "10.00",
              "part_hint": { "lines": 6 },
              "total_students": 3, "graded": 2, "pending": 1 }],
  "overall": { "graded": 2, "total": 9 },   // total = 主观题数 × 已交卷人数
  "pending": 7 }
```

★ `total_students` 只计 `SUBMITTED / SCANNED / GRADED`：未交卷的人进分母会让
「45 人只交 40 份」时进度永远到不了 100%。

### R-2 `GET /api/v1/grading/assignments/{id}/review/items/{paper_item_id}/students/`

某题的逐份学生清单（中栏 + 右栏）。

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `filter` | `all` | `pending` 只回未批（`graded=false`） |
| `page` / `page_size` | 1 / 50 | `page_size ≤ 200` |
| `ensure_crops` | `1` | 当前页缺切图时**现生成**（历史卡也能直接批） |

`data.items[]` 关键字段：

```jsonc
{ "student_id": 2116, "student_no": "2026QL00", "student_name": "学生0",
  "crop_asset_id": 9130, "rect": {"x_mm": 18.5, "y_mm": 96.2,
                                  "w_mm": 173.0, "h_mm": 42.4},
  "page_no": 2, "part_index": 1, "part_count": 2,
  "page_width_mm": 210.0, "page_height_mm": 297.0,
  "crops": [{ "asset_id": 9130, "part_index": 1, "page_no": 2, "rect": {...} }],
  "has_crop": true, "score": "6.0", "score_state": "MANUAL_ENTERED",
  "comment": "第 2 步漏写", "graded": true }
```

★ `page_width_mm / page_height_mm` 取自**该卡模板的 `layout_json`**（不是 A4 常量）：
前端「看整页 + 高亮 rect」要把 mm 换成百分比，假设 A4 会让高亮框画到页外。
★ `comment` 复用 `deduction_reasons[0].reason`，不新增字段。

### R-3 `GET …/review/items/{paper_item_id}/crop/?student_id=` —— 切图字节

返回 `image/jpeg`，顺序：该题切图 → 该生整页校正图 → 原图；
回落整页时响应头带 `X-Zf-Fallback: PAGE`。
★ 不走 `<img src>`：资产接口要 JWT，`<img>` 带不上 `Authorization`。

### R-4 `POST /api/v1/grading/assignments/{id}/review/score/` —— 保存并下一位

```jsonc
// 请求
{ "student_id": 2116, "paper_item_id": 812, "score": 6.5, "reason": "第 2 步漏写（≤500 字）" }
// 响应
{ "score_id": 5541, "state": "MANUAL_ENTERED", "score": "6.5",
  "updated_at": "2026-10-20T07:12:03Z",
  "item_progress": { "graded": 3, "total": 3 },
  "next_student": { "student_id": 2117, "student_no": "2026QL01", "student_name": "学生1" } }
```

- `next_student = null` 表示**这道题批完了**（前端据此跳汇总）。
- 越界：400 `GRADING.SCORE_EXCEED_MAX`。
- 重复给分视为**修改**（`TEACHER_MODIFIED`），不新增记录。
- 已作废（`VOID`）的评分**不能**靠录分复活：409 `COMMON.INVALID_STATE_TRANSITION`。

### R-5 `GET /api/v1/grading/assignments/{id}/review/summary/`

`{items, overall, progress(0–1), finished, students, title}` —— 批完后的汇总页。

### A-1 `GET /api/v1/analytics/assignments/{id}/items/` —— 逐题指标

| 参数 | 说明 |
| --- | --- |
| `refresh=1` | 现算并落库；不传则读 `item_stat` 快照（首次访问后端自动算一次） |

`data.items[]`：`seq / question_type / max_score / n / mean / p / p_label /
d / d_label / d_high / d_low / d_reason / point_biserial / option_counts /
zero_choice_options / sample_sufficient / warnings[]`。

`data.summary`：`item_count / graded_items / student_count / min_samples(30) /
sample_sufficient / warning`。
★ `sample_sufficient=false` 时**不做「好/差」判定**：3 个人算出来的 D 是噪声。

### A-2 `GET /api/v1/analytics/assignments/{id}/items/export/`

`text/csv; charset=utf-8` + **UTF-8 BOM**（Excel 双击不乱码），
文件名 `item-analysis-{id}.csv`。

### A-3 `GET /api/v1/analytics/questions/{id}/stats/` —— 题目历史校准

`{question_id, exam_count, total_n, p_avg, d_avg, last_assignment_id, note}`。
★ `exam_count=0` 时三个指标为 `null`（不是 0），`note` 写「尚无达到样本门槛的场次」。

### K-1 `GET/POST /api/v1/knowledge/aliases/`

- `GET ?node_id=` → `{items:[{id,node_id,alias,...}]}`（不传列全机构）。
- `POST {node_id, alias}` → 201；★ 该别名已指向**另一个**节点 → 400
  （正确动作是先合并，不是再加一条）。

### K-2 `GET /api/v1/knowledge/aliases/resolve/?text=&course_id=`

`{matched, node_id, via: ""|"alias"|"name", candidates:[{node_id,name,via}]}`。
★ 一个文本命中多个节点时 `matched=false` + 候选，**系统不猜**。

### K-3 合并与回滚

| 接口 | 说明 |
| --- | --- |
| `POST /api/v1/knowledge/nodes/{id}/merge/` | `{source_id, target_id, reason}`；路径 `{id}` 必须等于 `target_id`；返回 `{log_id, counts}` |
| `GET /api/v1/knowledge/merges/?node_id=` | 合并历史 |
| `POST /api/v1/knowledge/merges/{log_id}/rollback/` | 按日志反向恢复；已回滚过的拒绝 |

★ 合并只改引用、不物理删除：源节点 `status=DEPRECATED` 且 `deprecated_at` 非空。

### K-4 `GET /api/v1/knowledge/coverage/?course_id=` —— 标注覆盖率

`{total_questions, tagged_questions, coverage_ratio, primary_tagged_questions,
primary_ratio, conflict_questions, conflict_ratio, pending_ai_questions,
target:{coverage:0.90, primary:0.85}, meets_target, nodes[], chapters[],
untagged_node_count}`。

★ **只计 `review_state=CONFIRMED`**：AI 标的 `PENDING` 不算——否则 AI 一跑就「达标」。

### K-5 `GET /api/v1/knowledge/tag-review/` + `POST /api/v1/knowledge/tag-review/{link_id}/`

- 列表 `{total, count, items:[{link_id, question_id, stem_preview, node_id,
  node_name, is_primary, source, confidence, review_state}]}`，按 `confidence` **升序**
  （老师的时间该花在最可能错的标注上）。
- 确认/驳回 `{state: "CONFIRMED"|"REJECTED", reason}` → `{link_id, review_state}`。
  ★ 传 `link_id`（关联行 id），不是 `question_id`。

### P-1 `GET /api/v1/papers/{id}/coverage/` —— 发布前覆盖体检

> ★ 21 文档写的是 `/api/v1/paper/papers/{id}/coverage/`；实际前缀是
> `/api/v1/papers/`（`zhifou/urls.py` 的 `api_v1` 列表），以此为准。

```jsonc
{ "paper_id": 1601, "title": "…", "item_count": 12, "total_score": 39.0,
  "target_total_score": 39.0,
  "can_publish": false,                       // ← 唯一判据：只看 blocking
  "blocking": [{ "code": "B-NODE-ZERO", "message": "…", "detail": {...} }],
  "warnings": [{ "code": "W-DUP-NEAR", "message": "…" }],
  "knowledge": { "nodes": [...], "node_count": 1, "untagged_seqs": [7],
                 "untagged_count": 1, "required_node_ids": [12],
                 "missing_required": [12], "over_quota": [],
                 "out_of_syllabus": [], "note": "只统计已人工确认的标注…" },
  "difficulty": { "buckets": [{"band":"EASY","count":4,"ratio":0.333,
                               "target_ratio":0.30,"drift":0.033}],
                  "max_drift": 0.033, "tolerance": 0.15, "skewed": false },
  "structure": { "rows": [...], "expected": {...}, "mismatched": [] },
  "duplicates": [{ "kind": "EXACT", "seqs": [9, 12], "similarity": 1.0 }],
  "summary": "有 1 项阻断，不能发布" }
```

阻断码：`B-TOTAL`（总分不符）/ `B-EMPTY`（无题）/ `B-NODE-ZERO`（必需考点零覆盖）/
`B-DUP-EXACT`（题干哈希相同）。
告警码：`W-NODE-OVER` / `W-NODE-SHARE` / `W-OUT-OF-SYLLABUS` / `W-DUP-NEAR` / `W-STRUCTURE`。

### 新增错误码

| 码 | HTTP | 触发 |
| --- | --- | --- |
| `COMMON.INVALID_STATE_TRANSITION` | 409 | 状态机拒绝非法流转（如 `VOID → MANUAL_ENTERED`） |
| `PAPER.COVERAGE_BLOCKED` | 409 | 定稿/发布时体检有阻断项（`force=true` + `reason` 可越过，理由入审计） |

**回归**：`scripts/verify_grading_quality_loop.py`（90 项，sqlite 自检库，
不渲染 PDF / 不生成图片）；前端 `pnpm typecheck` / `pnpm build`。


