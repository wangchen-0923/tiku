/**
 * 题库 API。
 *
 * ★★ 响应契约（**必须按后端实际返回写，不能凭 DRF 默认约定猜**）：
 *
 *   DRF 分页默认给 `{count, next, previous, results}`，但本项目的
 *   `ZfJSONRenderer` 把它**改写**成了：
 *
 *       { code, message, data: { items, total, next, previous } }
 *
 *   即 `results → items`、`count → total`。
 *   我第一版按 DRF 默认写了 `data.results` / `data.count`，
 *   首页统计直接报 `KeyError`——**这类错误类型系统抓不到**，
 *   只有真的发一次请求才会暴露。
 *
 * ★ 列表是**摘要**，不含题干全文（后端刻意如此：几千条 × 每条几百字
 *   会让响应到几 MB）。字段是 `stem_preview`（80 字），
 *   详情走 `/questions/{id}/`。
 */

import { http } from './http'

/** 本项目的分页信封（注意是 `items` / `total`，不是 `results` / `count`）。 */
export interface Paged<T> {
  items: T[]
  total: number
  next: string | null
  previous: string | null
}

/** 列表项（对齐后端 `QuestionListSerializer`）。 */
export interface QuestionBrief {
  id: number
  uuid: string
  type: string
  subject: string
  grade_band: string
  /** ★ 80 字摘要，不是全文 */
  stem_preview: string
  status: string
  source_type: string
  /** ★ 字符串（后端是 Decimal，序列化成 "3.00"） */
  difficulty_effective: string
  difficulty_band: string
  difficulty_source: string
  has_figure: boolean
  figure_count: number
  answer_count: number
  correct_rate: number | null
  task_id: number | null
  task_name: string
  created_at: string
  /** ★ 双轨（v2.0）：所属题库 */
  bank_id: number | null
  bank_kind: '' | 'PRIVATE' | 'PUBLIC'
  /** ★ 下载来的副本才有：来源公共题 id */
  source_question_id: number | null
  /** ★ 来源题已更新（漂移角标的数据源，只提示不自动同步） */
  source_drift: boolean
}

// ══════════════════════════════════════════════════
//  详情（对齐后端 `QuestionDetailSerializer`）
// ══════════════════════════════════════════════════

export interface QuestionOption {
  id: number
  /** 卷面标签 A/B/C/D… */
  label: string
  content_html: string
  content_text: string
  sort_order: number
  is_correct: boolean
  image_asset_id: number | null
}

export interface QuestionKnowledgeLink {
  knowledge_node_id: number
  node_name: string
  node_code: string
  weight: string
  is_primary: boolean
  source: string
}

export interface QuestionFigure {
  id: number
  asset_id: number
  attach_point: string
  seq_in_question: number
  caption: string
  figure_category: string
  description: string
  /** ★ `PENDING` 的配图会**阻止**题目用于组卷（三个硬保证之一） */
  review_status: string
  binding_confidence: string | null
  binding_method: string
  quality_score: string | null
  quality_flags: unknown
}

/**
 * 题目详情。
 *
 * ★★ 难度有**四个字段**，不是一（设计文档 2.4）：
 *
 *     difficulty_manual    教师手填（有值时**优先级最高**）
 *     difficulty_ai        AI 估算
 *     difficulty_stat      正确率统计反推
 *     difficulty_effective 实际生效值（按上面优先级算出）
 *     difficulty_source    effective 来自哪个来源
 *
 *   ★ 所以「改难度」要改 `difficulty_manual`，而不是 `effective`
 *     —— 后者是派生值，改了也会被重算覆盖。
 */
export interface QuestionDetail {
  id: number
  uuid: string
  type: string
  subject: string
  grade_band: string
  course_id: number | null
  task_id: number | null
  task_name: string
  chapter_raw: string
  stem_html: string
  stem_text: string
  stem_hash: string
  options: QuestionOption[]
  /** ★ 形态随题型变化（选择题 `{"value":"C"}`；填空 `{"blanks":[...]}`） */
  answer: Record<string, unknown>
  answer_text: string
  answer_count: number
  explanation_html: string
  explanation_text: string
  difficulty_manual: string | null
  difficulty_ai: string | null
  difficulty_stat: string | null
  difficulty_effective: string
  difficulty_source: string
  difficulty_band: string
  has_figure: boolean
  figure_count: number
  figures: QuestionFigure[]
  knowledge_links: QuestionKnowledgeLink[]
  correct_rate: number | null
  source_type: string
  source_ref: string
  status: string
  ai_evidence: unknown
  reviewed_at: string | null
  created_at: string
  updated_at: string
}

export interface ListQuestionParams {
  page?: number
  pageSize?: number
  keyword?: string
  type?: string
  status?: string
  difficultyBand?: string
  sourceType?: string
  hasFigure?: boolean
  /** ★ 只看「配图待确认」的题（挡住组卷，首页待办卡的入口） */
  figurePending?: boolean
  onlyUsable?: boolean
  taskId?: number
  nodeId?: number
  /**
   * 双轨（v2.0）：取哪个库的题。
   *
   * ★ 不传 = `private`（后端默认）。**不要**指望传空字符串表示「全部」——
   *   后端只认 `private` / `public`，`all` 会 400（见视图 docstring：
   *   两个列表的收窄口径不同，拼成一个 QuerySet 会破坏分页）。
   */
  bank?: 'private' | 'public'
}

export async function listQuestions(
  params: ListQuestionParams = {},
): Promise<Paged<QuestionBrief>> {
  return http.get<Paged<QuestionBrief>>('/api/v1/questions/', {
    page: params.page,
    page_size: params.pageSize,
    // ★★ 后端读的参数名是 **`keyword`**。
    //    前端曾传 `search`，被静默忽略，**搜索框一直返回全部题**
    //    （实测：`?keyword=X` → total=1，`?search=X` → total=1944）。
    //    这类错**不报错**，只有真发请求比对总数才发现。
    keyword: params.keyword,
    type: params.type,
    status: params.status,
    difficulty_band: params.difficultyBand,
    source_type: params.sourceType,
    has_figure: params.hasFigure === undefined
      ? undefined
      : String(params.hasFigure),
    only_usable: params.onlyUsable ? 'true' : undefined,
    figure_pending: params.figurePending ? 'true' : undefined,
    task_id: params.taskId,
    node_id: params.nodeId,
    bank: params.bank,
  })
}

/**
 * 只取总数。
 *
 * ★ `page_size=1` 让响应体最小 —— 我们要的只是 `total`，
 *   没必要把 20 条数据也传回来。
 */
export async function countQuestions(
  params: Omit<ListQuestionParams, 'page' | 'pageSize'> = {},
): Promise<number> {
  const data = await listQuestions({ ...params, pageSize: 1 })
  return data.total
}

/** 题目详情（含题干全文、选项、答案、解析、知识点、配图）。 */
export async function getQuestion(id: number): Promise<QuestionDetail> {
  return http.get<QuestionDetail>(`/api/v1/questions/${id}/`)
}

// ══════════════════════════════════════════════════
//  写入
// ══════════════════════════════════════════════════

/** 选项输入（对齐 `OptionInputSerializer`）。 */
export interface OptionInput {
  label: string
  content_text: string
  content_html?: string
  image_asset_id?: number | null
}

/**
 * 新建题目（**默认落 `DRAFT`**）。
 *
 * ★ 必填只有三项：`course_id` / `question_type` / `stem_text`。
 *   其余按题型给：选择题给 `options` + `answer`，填空题给
 *   `answer.blanks`。
 *
 * ★ `check_duplicate` 默认 **true**：后端会做三层去重，
 *   命中时返回业务分支（不是 4xx）——调用方要处理
 *   「疑似重复，是否继续」。
 */
export interface CreateQuestionBody {
  course_id: number
  question_type: string
  stem_text: string
  answer?: Record<string, unknown>
  options?: OptionInput[]
  node_ids?: number[]
  task_id?: number | null
  explanation?: string
  difficulty?: number | null
  source_type?: string
  source_ref?: string
  check_duplicate?: boolean
}

/** 新建。返回详情（`created` 201）。 */
export async function createQuestion(
  body: CreateQuestionBody,
): Promise<QuestionDetail> {
  return http.post<QuestionDetail>('/api/v1/questions/', body)
}

/**
 * 修改题目（部分字段）。
 *
 * ★ 后端只接受这 7 个字段：`stem_text` / `answer` / `options` /
 *   `explanation` / `difficulty` / `task_id` / `node_ids`。
 *   改**题型**或**课程**不在其中 —— 那是「另一道题」，
 *   应该新建而不是改（否则已有作答记录会失去意义）。
 *
 * ★★ `difficulty` 改的是 `difficulty_manual`（教师手填）。
 *   它优先级最高，所以这是**人工补难度标注的唯一通路** ——
 *   也是解决「题库难度全是 3.00」的办法。
 */
export interface UpdateQuestionBody {
  stem_text?: string
  answer?: Record<string, unknown>
  options?: OptionInput[]
  explanation?: string
  /** ★ 写入 `difficulty_manual`，1–5（允许 0.5 步长） */
  difficulty?: number | null
  task_id?: number | null
  node_ids?: number[]
}

export async function updateQuestion(
  id: number,
  body: UpdateQuestionBody,
): Promise<QuestionDetail> {
  return http.patch<QuestionDetail>(`/api/v1/questions/${id}/`, body)
}

/**
 * 删除题目。
 *
 * ★ 后端接受一个可选的 `reason`（≤500 字）用于审计。
 *   `http.del` 支持带 body，所以这里传得进去。
 */
export async function deleteQuestion(
  id: number,
  reason = '',
): Promise<{ id: number }> {
  return http.del<{ id: number }>(`/api/v1/questions/${id}/`, undefined, {
    reason,
  })
}

/**
 * 审核入参。
 *
 * ★★ 是 `action: "APPROVE" | "REJECT"`，**不是** `approve: true/false`。
 *   我第一版按布尔写，后端直接 400：「`action` 该字段是必填项」。
 */
export interface ReviewBody {
  action: 'APPROVE' | 'REJECT'
  reason?: string
}

/**
 * 撤回为草稿（**编辑已审核题目的唯一合法路径**）。
 *
 * ★★ 为什么需要它：
 *
 *   `PATCH /questions/{id}/` 对 `APPROVED` 的题返回 **409**，
 *   错误信息是「请先调用 revise 撤回为草稿」。
 *   而**这个端点是后来才补的** —— 原来服务方法 `revise_question()`
 *   存在、`urls.py` 里却没有路径，于是照着提示做**没有任何接口可调**，
 *   已审核的题就改不了了。
 *
 *   ★ 而「打标」（关联知识点）恰恰常在审核之后才想起来补。
 *
 * ★ 撤回为什么是必要的（不是过度设计）：已发布的试卷用**快照**，
 *   改了题库不会改到历史卷。若允许直接改已审核的题，
 *   老师会以为「卷子上的题也变了」而不去重发 ——
 *   撤回会同时把引用它的**未发布**试卷标记为需重新定稿，让影响面可见。
 */
export async function reviseQuestion(
  id: number,
  reason = '',
): Promise<QuestionDetail> {
  return http.post<QuestionDetail>(`/api/v1/questions/${id}/revise/`, {
    reason,
  })
}

/** 审核（通过 / 驳回）。 */
export async function approveQuestion(
  id: number,
  body: ReviewBody,
): Promise<QuestionDetail> {
  return http.post<QuestionDetail>(`/api/v1/questions/${id}/approve/`, body)
}

/** 相似题（三层去重的结果）。 */
export async function listSimilarQuestions(
  id: number,
): Promise<QuestionBrief[]> {
  const d = await http.get<{ items?: QuestionBrief[] } | QuestionBrief[]>(
    `/api/v1/questions/${id}/similar/`,
  )
  if (Array.isArray(d)) return d
  return (d as { items?: QuestionBrief[] }).items ?? []
}

/**
 * AI 生成解析（`gen_explanation`）。
 *
 * ★ 默认 `apply=false`：**只生成不回写**，前端把它填进编辑框让老师过一眼。
 *   解析是要给学生看的内容，模型直接改进库风险太高，
 *   而且会覆盖掉老师可能已经改过的版本。
 *
 * ★ 三种「生成成功但不该落库」的情况后端会带 `blocked_reason` 回来：
 *   `EMPTY_EXPLANATION` / `INSUFFICIENT_EVIDENCE` / `LOW_CONFIDENCE`。
 *   界面必须把它们当**提示**展示，而不是当成功。
 */
export interface ExplanationResult {
  question_id: number
  status: string
  applied: boolean
  explanation?: string
  key_points?: string[]
  citations?: number[]
  confidence?: number
  grounded?: boolean
  blocked_reason?: string
  error?: string
  provider?: string
  model?: string
  call_log_id?: number | null
  cost_yuan?: number
  latency_ms?: number
  tokens?: { input: number; output: number }
}

export async function generateExplanation(
  id: number,
  opts: { apply?: boolean; force?: boolean } = {},
): Promise<ExplanationResult> {
  return http.post<ExplanationResult>(
    `/api/v1/questions/${id}/generate-explanation/`,
    { apply: opts.apply === true, force: opts.force === true },
  )
}

// ══════════════════════════════════════════════════
//  题库双轨（v2.0）：提交 / 审核 / 下载
// ══════════════════════════════════════════════════

/** 提交到公共库的结果（`POST /questions/{id}/submit-to-public/`）。 */
export interface SubmitToPublicResult {
  public_question_id: number
  status: string
  submitted_at: string
  /** 来源题带图时被丢弃的张数（公共库不支持跨机构图片） */
  dropped_figures: number
  node_codes: string[]
}

/**
 * 把本校私有题提交到公共库（**复制**，本校那道题不受影响）。
 *
 * ★★ `dropFigures` 是「显式确认丢掉配图」的开关，**默认必须传 false**。
 *   带配图的题提交会掉图（图片按机构授权，不能跨机构共享），
 *   后端会返回 409 并说明原因；老师点过确认之后才传 true。
 *   ★ 不要为了「让他能提交」而默认传 true —— 那是静默丢图。
 */
export async function submitToPublic(
  id: number,
  opts: { note?: string; dropFigures?: boolean } = {},
): Promise<SubmitToPublicResult> {
  return http.post<SubmitToPublicResult>(
    `/api/v1/questions/${id}/submit-to-public/`,
    { note: opts.note ?? '', drop_figures: opts.dropFigures === true },
  )
}

/** 审核队列项（对齐后端 `PublicReviewQueueView`）。 */
export interface PublicReviewItem {
  id: number
  stem_preview: string
  type: string
  status: string
  course_name: string
  task_name: string
  submitted_from_org_id: number | null
  submitted_from_org_name: string
  submitted_from_question_id: number | null
  submitted_at: string | null
  submit_note: string
  node_codes: string[]
  dropped_figures: number
  /** 已等待小时数：> 72 应在界面上标红（R7 堆积可见） */
  waiting_hours: number | null
}

export interface PublicReviewQueue {
  stats: {
    pending: number
    approved: number
    rejected: number
    archived: number
    total: number
  }
  total: number
  limit: number
  offset: number
  items: PublicReviewItem[]
}

/** 公共题审核队列（**仅平台管理员**可调，机构管理员会 403）。 */
export async function listPublicReviewQueue(params: {
  status?: string
  keyword?: string
  limit?: number
  offset?: number
} = {}): Promise<PublicReviewQueue> {
  return http.get<PublicReviewQueue>(
    '/api/v1/questions/public/review-queue/',
    {
      status: params.status,
      keyword: params.keyword,
      limit: params.limit,
      offset: params.offset,
    },
  )
}

/** 公共题详情（**含待审**，仅平台管理员可用）。
 *
 *  ★ 不能复用 `getQuestion(id)`：那个走 `for_org()`，只放行已审核的公共题，
 *    审核员点进待审题会 404（等于盲审）。
 */
export async function getPublicQuestion(id: number): Promise<QuestionDetail> {
  return http.get<QuestionDetail>(`/api/v1/questions/public/${id}/`)
}

/** 审核单道公共题。★ 驳回必须带 reason，否则后端 400 `REASON_REQUIRED`。 */
export async function reviewPublicQuestion(
  id: number,
  body: ReviewBody,
): Promise<{ id: number; status: string }> {
  return http.post<{ id: number; status: string }>(
    `/api/v1/questions/public/${id}/review/`,
    body,
  )
}

/** 批量审核（一次最多 50 道）。返回 `done` / `skipped` 两份清单。 */
export async function batchReviewPublic(
  questionIds: number[],
  body: ReviewBody,
): Promise<{ done: number[]; skipped: { id: number; why: string }[]; action: string }> {
  return http.post('/api/v1/questions/public/batch-review/', {
    question_ids: questionIds,
    action: body.action,
    reason: body.reason ?? '',
  })
}

/** 下架公共题。★ 已下载出去的副本不受影响（复制语义）。 */
export async function archivePublicQuestion(
  id: number,
  reason = '',
): Promise<{ id: number; status: string }> {
  return http.post<{ id: number; status: string }>(
    `/api/v1/questions/public/${id}/archive/`,
    { reason },
  )
}

export interface DownloadResult {
  question_id: number
  source_question_id: number
  source_version: string
  status: string
  course_id: number
  task_id: number | null
  mapped_nodes: { code: string; node_id: number }[]
  /** 在本机构树上找不到同 code 的知识点（没带过来，需要手工补） */
  unmapped_node_codes: string[]
  /** 来源题当初提交时丢掉的配图数 */
  lost_figures: number
}

/**
 * 把公共题下载成本机构私有草稿。
 *
 * ★★ `courseId` 必填，而且必须是**本机构的课程**：
 *   公共题的 `course` 指向来源机构的课程，照抄会挂到别人的课程上
 *   （详见后端 `download_public` 的 docstring）。
 */
export async function downloadPublicQuestion(
  id: number,
  body: { courseId: number; taskId?: number | null; bankId?: number | null },
): Promise<DownloadResult> {
  return http.post<DownloadResult>(
    `/api/v1/questions/public/${id}/download/`,
    {
      course_id: body.courseId,
      task_id: body.taskId ?? null,
      bank_id: body.bankId ?? null,
    },
  )
}

/** 副本的来源与漂移状态。 */
export interface SourceStatus {
  is_copy: boolean
  source_question_id: number | null
  source_version: string
  source_status: string
  source_updated_at: string | null
  /** 来源题已更新（角标数据源）。**只提示，不自动同步。** */
  drifted: boolean
  source_available: boolean
}

export async function getSourceStatus(id: number): Promise<SourceStatus> {
  return http.get<SourceStatus>(`/api/v1/questions/${id}/source-status/`)
}

/**
 * 题库统计。
 * ★★ 这里的字段名是**照后端 `QuestionStatsView` 实测**写的。
 *   我第一版按印象写成 `total` / `by_difficulty_band`，
 *   而后端给的是 `usable_total` / `by_band` ——
 *   类型对不上时 TS 不会报错（因为 `[k: string]: unknown` 兜住了），
 *   界面只会安静地显示 `undefined`。
 */
export interface QuestionStats {
  /** 可组卷题数（不是题库总数 —— DRAFT 与未确认配图的题不算） */
  usable_total: number
  by_band?: Record<string, number>
  by_type?: Record<string, number>
  /**
   * 难度分层健康度 —— 「决策 7」的机械判据。
   *
   * ★★ 为什么需要它：题库全是默认难度 3.00、`difficulty_manual`
   *   全空时，个性化练习按掌握度推导出的 [1.0, 2.0] 区间取题
   *   **一道也拿不到**，而报错只给 `PRACTICE.BANK_INSUFFICIENT` ——
   *   **从报错看不出根因是数据而不是算法**。
   *
   *   `needs_annotation=true` 即「已处于挡住个性化练习的状态」。
   */
  difficulty?: {
    total: number
    /** 有 `difficulty_manual` 的题数（真人工标注） */
    manually_rated: number
    /** ★ 未人工标注的题数 —— 界面直说「还有 N 道是默认值」 */
    unrated: number
    /** 未标注占比（0–1） */
    unrated_ratio: number
    by_band: Record<string, number>
    /** ★ 档位种类数；`<= 1` 就是没分层 */
    band_count: number
    /** 出现最多的 `difficulty_effective` 值 */
    effective_mode: string
    /**
     * ★★ 是否**仍**处于「挡住个性化练习」的状态。
     *
     *   判据是**未标注占比 > 0.9**，不是「有没有标过」。
     *   只标 1 道题会让档位数变成 2、`manually_rated` 不再为 0 ——
     *   按「有没有标过」判，告警会立刻消失，
     *   而剩下 1900+ 道仍是默认值，练习照样取不到题。
     */
    needs_annotation: boolean
  }
  [k: string]: unknown
}

export async function getQuestionStats(): Promise<QuestionStats> {
  return http.get<QuestionStats>('/api/v1/questions/stats/')
}

// ══════════════════════════════════════════════════
//  展示辅助
// ══════════════════════════════════════════════════

/** 8 种题型（设计文档 2.3）。 */
export const QUESTION_TYPE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'SINGLE_CHOICE', label: '单项选择' },
  { value: 'MULTIPLE_CHOICE', label: '多项选择' },
  { value: 'TRUE_FALSE', label: '判断题' },
  { value: 'FILL_BLANK', label: '填空题' },
  { value: 'SHORT_ANSWER', label: '简答题' },
  { value: 'CALCULATION', label: '计算题' },
  { value: 'DRAWING', label: '作图题' },
  { value: 'ESSAY', label: '论述题' },
]

/**
 * 难度是否只能是整数。
 *
 * ★★ 实测：`difficulty_manual` 是 **`IntegerField`**
 *   （`MinValueValidator(1)` / `MaxValueValidator(5)`），
 *   **不是** decimal。传 1.5 会被静默取整成 2。
 *
 *   ★ 这与文档里「难度 1–5，允许 0.5 步长」**不一致** ——
 *     `difficulty_effective` / `difficulty_ai` / `difficulty_stat`
 *     都是 `DecimalField(decimal_places=2)`，只有人工标注那个是整数。
 *     已记入开发进度记录（待裁决）。
 *
 *   所以界面上人工难度**只能给整数 1–5**，不给 0.5 步长 ——
 *   给了也会被静默取整，那比不让填更糟。
 */
export const MANUAL_DIFFICULTY_INTEGER_ONLY = true

/** 把难度说明书里的档位映射到整数（供选择器用）。 */
export const DIFFICULTY_CHOICES: Array<{ value: number; label: string }> = [
  { value: 1, label: '1 · 很易' },
  { value: 2, label: '2 · 较易' },
  { value: 3, label: '3 · 中等' },
  { value: 4, label: '4 · 较难' },
  { value: 5, label: '5 · 很难' },
]

/** 难度档位（后端 `difficulty_band`）→ 中文。 */
export const DIFFICULTY_BAND_LABEL: Record<string, string> = {
  EASY: '易',
  MEDIUM: '中',
  HARD: '难',
}

export const QUESTION_STATUS_LABEL: Record<string, string> = {
  DRAFT: '草稿',
  PENDING_REVIEW: '待审',
  APPROVED: '已通过',
  REJECTED: '已驳回',
}

export const QUESTION_STATUS_TONE: Record<
  string,
  'info' | 'success' | 'warning' | 'danger'
> = {
  DRAFT: 'info',
  PENDING_REVIEW: 'warning',
  APPROVED: 'success',
  REJECTED: 'danger',
}

/**
 * `difficulty_source` 的中文名。
 *
 * ★★ 取值照**后端 `DifficultySource` 枚举**列，不凭印象造。
 *   我第一版写了 `AI` 与 `STAT`，但枚举里**没有 `AI`**，
 *   而是 `MANUAL_STAT_BLEND`（标定与统计混合）。
 *   写错的后果是界面显示原始英文枚举值。
 *
 * ★ `MANUAL` = 教师**标定**（首次填），`MANUAL_OVERRIDE` = 教师
 *   **覆盖**已有的统计值。两者都表示「人工填过」。
 *
 * ★★ 但**不能**把 `DEFAULT` 也显示成「人工标注」——
 *   那正是导入数据曾经的自相矛盾：1944 题标着 `MANUAL`，
 *   而实际 `difficulty_manual` 非空的只有 4 道
 *   （导入脚本没设该字段，取了模型默认值 `MANUAL`）。
 *   已修导入脚本 + 数据。
 */
export const DIFFICULTY_SOURCE_LABEL: Record<string, string> = {
  MANUAL: '人工标注',
  MANUAL_OVERRIDE: '人工覆盖',
  MANUAL_STAT_BLEND: '人工+统计',
  STAT: '统计推断',
  DEFAULT: '默认值（未标注）',
}

/** 该来源是否表示「人工填过难度」。 */
export function isManuallyRated(source: string): boolean {
  return source === 'MANUAL' || source === 'MANUAL_OVERRIDE'
}

/** 判断题标签固定 A=正确 / B=错误（环境决定）。 */
export const TRUE_FALSE_OPTIONS: OptionInput[] = [
  { label: 'A', content_text: '正确' },
  { label: 'B', content_text: '错误' },
]

/** 该题型是否需要选项。 */
export function needsOptions(type: string): boolean {
  return (
    type === 'SINGLE_CHOICE' ||
    type === 'MULTIPLE_CHOICE' ||
    type === 'TRUE_FALSE'
  )
}

/**
 * 各题型的 `answer` 结构（**实测契约，不是猜的**）。
 *
 * ★★ 这里有三个容易写错的地方，都是实测确认过的：
 *
 *     ★ 单选与多选**共用 `value` 键**（不是 `value` / `values` 两个键）：
 *         `{"value": "A"}`         单选
 *         `{"value": ["A", "C"]}`  多选
 *
 *     ★ 判断题的 `value` 是**布尔值**（不是 "A"/"B" 字符串）：
 *         `{"value": true}`  → 正确
 *         `{"value": false}` → 错误
 *       传 `"B"` 会被后端拒：「判断题答案是布尔值 true/false」。
 *
 *     ★ 填空题是 `blanks` 数组，每项要有**非空**的 `accepted`：
 *         `{"blanks": [{"index": 1, "accepted": ["定位"]}]}`
 *
 *   我第一版按 `values` 键 + A/B 字符串写，三类全错。
 */
export const TRUE_FALSE_LABEL: Record<'true' | 'false', string> = {
  true: '正确',
  false: '错误',
}

/** 判断题的 `answer.value`（布尔）→ 界面用的 `'A' | 'B'`。 */
export function tfValueToLabel(
  answer: Record<string, unknown> | null | undefined,
): '' | 'A' | 'B' {
  const v = (answer ?? {}).value
  if (v === true) return 'A'
  if (v === false) return 'B'
  return ''
}

/** 界面的 `'A' | 'B'` → 判断题的布尔值（★ A=正确=true）。 */
export function tfLabelToValue(label: '' | 'A' | 'B'): boolean | null {
  if (label === 'A') return true
  if (label === 'B') return false
  return null
}

/** 选择题的答案标签（单选 `{value:"A"}` / 多选 `{value:["A","C"]}`）。 */
export function choiceLabels(
  answer: Record<string, unknown> | null | undefined,
): string[] {
  const v = (answer ?? {}).value
  if (typeof v === 'string') return v ? [v.toUpperCase()] : []
  if (Array.isArray(v)) return v.map((x) => String(x).toUpperCase())
  return []
}

/** 选择题答案（单选用一个字母，多选用数组）。 */
export function choiceAnswer(
  labels: string[],
  multi: boolean,
): Record<string, unknown> {
  const sorted = [...labels].sort()
  return { value: multi ? sorted : (sorted[0] ?? '') }
}

/** 判断题是否可自动批改（后端 `AUTO_GRADABLE_TYPES`）。 */
export function isAutoGradable(type: string): boolean {
  return (
    type === 'SINGLE_CHOICE' ||
    type === 'MULTIPLE_CHOICE' ||
    type === 'TRUE_FALSE' ||
    type === 'FILL_BLANK'
  )
}

/**
 * 数出填空题的空数。
 *
 * ★ 题库里的空位标记是 **8 个半角下划线**（实测确认）。
 *   数不出时返回 1 —— 宁可给一个输入框，也不要给 0 个
 *   （那会让学生无法作答）。
 */
export function countBlanks(stem: string): number {
  const s = stem || ''
  return Math.max(
    1,
    (s.match(/_{3,}/g) ?? []).length + (s.match(/＿{3,}/g) ?? []).length,
  )
}
