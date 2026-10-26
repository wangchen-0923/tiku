/**
 * AI 生题 API。
 *
 * ★★ 这个模块的存在理由：`POST /ai/questions/generate/` **有两种形态、
 *   三种结果**，而它们在 HTTP 层看起来很像，特别容易接错。
 *
 *   形态（由后端 `ZF_TASK_MODE` 决定，前端**无法预判**）：
 *
 *     inline（默认） → `queued: false` + 真结果（`created` 有内容）
 *     queued        → `queued: true`  + `created` **恒空**、`created_count` **恒 0**
 *
 *   ★ 所以**绝不能**只看 `created_count === 0` 就判「生成失败」——
 *     queued 下它永远是 0，而任务可能正在跑。判据只能是 `queued`。
 *
 *   结果：
 *
 *     ① HTTP 202 + `code: "OK"` → 受理成功，看 `data`
 *     ② HTTP 200 + `code` 非 OK（`AI.KB_INSUFFICIENT` /
 *        `AI.GENERATE_DISABLED` …）→ **业务分支**，`http.ts` 抛
 *        `BusinessBranch`。这不是错误，是「该去补知识库」的引导。
 *     ③ HTTP 402 → 余额不足。`ApiError`，且 `data` 里带还差多少积分。
 *
 *   ★ 把 ② 当 ③ 处理（弹红框「生题失败」）会让老师去重试，
 *     而重试一百次也过不了闸门 —— 真正该做的是去知识库加两条内容。
 *
 * ★★ 硬保证（三条，前端不要试图绕过）：
 *
 *   1. 落库恒 `DRAFT` + `AI_GENERATED` —— 请求体里**没有** `status` 字段，
 *      后端也不接受。AI 生成的题必须老师审核才能进试卷。
 *   2. 必须在知识库有依据时才会生成 —— 闸门不通过时**拒绝生成是正确行为**。
 *   3. 查重开着：AI 极容易重复题库已有的题，命中会作为**失败项**返回并
 *      带 `duplicate_id`，不是整批失败。
 */

import { http } from './http'

// ══════════════════════════════════════════════════
//  可用性（打开抽屉时先查这个）
// ══════════════════════════════════════════════════

/** 生题可用性的三档（后端 `MODE_*` 常量）。 */
export type GenerateMode = 'OPEN' | 'LIMITED' | 'DISABLED'

/**
 * `GET /ai/generate/availability/` 的返回。
 *
 * ★ `mode` 决定界面怎么表现，而**不是**决定「能不能点」：
 *   - `OPEN`     开放
 *   - `LIMITED`  仅 `allowed_node_ids` 里的知识点可生题（白名单）
 *   - `DISABLED` 整体不可用，`reason` 要**原样**显示给老师
 *
 * ★★ `reason` 是后端写好的中文句子，界面上不要自己再编一句
 *   「AI 生题暂不可用」—— 那句话丢掉了「为什么」，老师无从下手。
 */
export interface GenerateAvailability {
  mode: GenerateMode
  reason: string
  /** 知识库覆盖率 0–1 */
  kb_coverage: number
  /** 本课程可组卷题数（题库太小会判 LIMITED：生成后无法查重/校准） */
  bank_size: number
  thresholds: {
    kb_coverage: number
    kb_coverage_disabled: number
    bank_size: number
  }
  total_nodes: number
  covered_nodes: number
  /** LIMITED 模式的白名单（后端截断到 200 个） */
  allowed_node_ids: number[]
  allowed_node_count: number
  /** 指定 `node_id` 时才有：该知识点是否允许生题 */
  node_allowed: boolean | null
  /** AI **优化**已有题目（解析/打标/干扰项）始终可用，与生题无关 */
  optimize_available: boolean
}

export async function getGenerateAvailability(params: {
  courseId?: number
  nodeId?: number
} = {}): Promise<GenerateAvailability> {
  return http.get<GenerateAvailability>('/api/v1/ai/generate/availability/', {
    course_id: params.courseId,
    node_id: params.nodeId,
  })
}

// ══════════════════════════════════════════════════
//  生题
// ══════════════════════════════════════════════════

export interface GenerateBody {
  /** ★ 复数、非空。一次可针对多个知识点（如「动量守恒 + 碰撞」） */
  knowledgeNodeIds: number[]
  questionType: string
  /** 1–10（后端上限；超过会被 400 拒） */
  count?: number
  /** 1.0–5.0；不传则由模型判断 */
  difficulty?: number | null
  /** 题干要求（≤500 字） */
  stemHint?: string
  /** 挂到哪个章节（`question.task_id`）。仅影响组卷筛选 */
  chapterId?: number | null
  /** 单次调用成本上限（元）。0 = 不限 */
  maxCostYuan?: number | null
  kbInjection?: {
    top_k?: number
    min_weight?: number
    min_entries?: number
  }
}

/** 一条生成成功的题目（够用来展示 + 跳到详情，不是完整题目）。 */
export interface GeneratedQuestion {
  question_id: number
  /** 题干前 80 字 */
  stem: string
  difficulty: number | null
  /** 模型自评置信度（0–1，可能为 null） */
  confidence: number | null
  has_explanation: boolean
}

/** 一条失败项。 */
export interface GenerateFailure {
  /** 第几道（从 1 开始）。模型少给时，差额项的 `index` 会越过实际条数 */
  index: number
  error: string
  /**
   * ★ 查重命中时才有：与题库里哪道题重复。
   *
   * 界面上要把它变成可点开的链接 —— 「重复了」而不说和谁重复，
   * 老师只能自己去题库里翻。
   */
  duplicate_id?: number
}

/**
 * `POST /ai/questions/generate/` 的返回（已剥壳，即 `data`）。
 *
 * ★★ `status` 与 `queued` 是**两个维度**，不要合并判断：
 *
 *     queued=true          → status 恒 "PENDING"，进度看 `/tasks/{run_id}/`
 *     queued=false, status="OK"     → 有题落库
 *     queued=false, status="FAILED" → 一道都没落库（看 `failed`）
 */
export interface GenerateResult {
  accepted: boolean
  queued: boolean
  /** 任务记录 id（`/api/v1/tasks/{run_id}/`）。★ 恒定，两种形态都有 */
  run_id: number | null
  /** Celery task id（仅 queued 有；空字符串表示没拿到） */
  task_id: string
  status: string
  /** inline 完成时间；queued 下为 null */
  queued_at: string | null
  created: GeneratedQuestion[]
  created_count: number
  failed: GenerateFailure[]
  failed_count: number
  /** 要了几道。`created_count + failed_count` 应当等于它（模型少给时也补平） */
  requested: number
  call_log_id: number | null
  provider: string
  model: string
  cost_yuan: number
  latency_ms: number
  /**
   * 投递到队列失败、已回退同步执行时的原因（空字符串 = 没回退）。
   * ★ 这个字段有值时界面要**说一句**：老师点的是"异步"，
   *   实际是同步跑完的，耗时会明显更长。
   */
  dispatch_fallback: string
  // ── 闸门与可用性证据（「凭什么允许生成」）──
  knowledge_node_ids: number[]
  knowledge_node_codes: string[]
  question_type: string
  count: number
  difficulty: number | null
  kb_entry_ids: number[]
  kb_hit_count: number
  kb_min_entries: number
  availability_mode: GenerateMode
  prompt_template_key: string
  /** 恒 `DRAFT` —— 显示成「草稿」而不是「已完成」 */
  will_create_status: string
  will_create_source_type: string
}

export async function generateQuestions(
  body: GenerateBody,
): Promise<GenerateResult> {
  return http.post<GenerateResult>('/api/v1/ai/questions/generate/', {
    // ★ 字段名是后端契约（snake_case），别顺手改成 camelCase
    knowledge_node_ids: body.knowledgeNodeIds,
    question_type: body.questionType,
    count: body.count ?? 1,
    difficulty: body.difficulty ?? null,
    stem_hint: body.stemHint ?? '',
    chapter_id: body.chapterId ?? null,
    max_cost_yuan: body.maxCostYuan ?? null,
    ...(body.kbInjection ? { kb_injection: body.kbInjection } : {}),
  })
}

/** 闸门拒绝时 `BusinessBranch.data` 的形状（用于展示「差多少」）。 */
export interface GateRejectedData {
  ok: false
  code: string
  message: string
  /** `kb_entry` 表示「去补知识库」 */
  suggestion: string
  kb_hit_count?: number
  kb_min_entries?: number
  kb_entry_ids?: number[]
  knowledge_node_id?: number
  coverage_ratio?: number
  /** 不可用场景（`AI.GENERATE_DISABLED`）会带完整可用性 */
  mode?: GenerateMode
  reason?: string
  total_nodes?: number
  covered_nodes?: number
}
