/**
 * 三级账户 · 前端能力表。
 *
 * ★★ 三级账户：系统管理员（L30）> 学校平台账户（L20）> 教师账户（L10）。
 *
 *   与后端 `apps/common/permissions.py` 的 `ROLE_RANK` **必须保持一致** ——
 *   改等级只改这两处。前端的判断只是「别让用户点进去才看到 403」，
 *   **真正的权限在后端**：前端藏起来的按钮，直接调接口一样会被拒。
 *   所以这里的任何一条写错，后果只是体验问题，不是安全漏洞。
 *
 * ★★ 为什么不用「role === 'ORG_ADMIN'」这种直接比较：
 *
 *   三级是**递增**关系（系统管理员天然能做学校账户的事），
 *   写等号就得把每一级都列一遍，加一级必然漏。
 *   用等级比较，加角色只改 `LEVEL`。
 */

export type Role = 'PLATFORM_ADMIN' | 'ORG_ADMIN' | 'TEACHER' | 'STUDENT'

/** 角色等级。★ 与后端 `LEVEL_*` 常量一一对应。 */
export const LEVEL = {
  STUDENT: 0,
  TEACHER: 10,
  SCHOOL: 20,
  SYSTEM: 30,
} as const

/**
 * 角色名 → 等级。★ **必须**与后端 `apps/common/permissions.py:ROLE_RANK` 逐字一致。
 *
 * ★★ 这张表不是"多写一份"，而是**唯一的转换点**：
 *
 *   上面的 `LEVEL` 键是**等级名**（`SCHOOL` / `SYSTEM`），
 *   而业务代码里传进来的永远是**角色名**（`ORG_ADMIN` / `PLATFORM_ADMIN`）。
 *   两者不是同一套字面量：`LEVEL['ORG_ADMIN']` **不存在**。
 *
 *   2026-10-22 的事故就是 `levelOf()` 直接拿角色名去查 `LEVEL` ——
 *   查不到走 `?? 0`，于是：
 *
 *     levelOf('TEACHER')        = 10  ← 键名碰巧相同，正常
 *     levelOf('STUDENT')        =  0  ← 碰巧正确
 *     levelOf('ORG_ADMIN')      =  0  ← **应为 20**
 *     levelOf('PLATFORM_ADMIN') =  0  ← **应为 30**
 *
 *   表现为「教师能正常用，学校平台账户和系统管理员一登录就 403」，
 *   而且**没有任何报错** —— 看起来像"账号没权限"，不像代码 bug。
 *
 *   ★ 教训：凡是「查表 + `?? 默认值`」的写法，键名对不上时是**静默降级**，
 *     永远不会抛错。默认值选 0 就意味着"最没权限"，一旦不匹配全网瘫痪；
 *     选高了则是越权。所以这类表必须与后端**同名同值**，并由测试盯着。
 */
const ROLE_RANK: Record<string, number> = {
  STUDENT: LEVEL.STUDENT,
  TEACHER: LEVEL.TEACHER,
  ORG_ADMIN: LEVEL.SCHOOL,
  PLATFORM_ADMIN: LEVEL.SYSTEM,
}

/** 三级显示名。★ 唯一来源，别在组件里再写一份映射表。 */
export const ROLE_LABEL: Record<Role, string> = {
  PLATFORM_ADMIN: '系统管理员',
  ORG_ADMIN: '学校平台账户',
  TEACHER: '教师账户',
  STUDENT: '学生',
}

/**
 * 取角色等级。未知角色按 0（最低）。
 *
 * ★ 先按**角色名**查 `ROLE_RANK`（正常路径），再兜底按**等级名**查
 *   `LEVEL`（历史调用方可能传 `'SCHOOL'` / `'SYSTEM'` 这类字面量）。
 */
export function levelOf(role: string | null | undefined): number {
  const key = String(role ?? '')
  const byRole = ROLE_RANK[key]
  if (byRole !== undefined) return byRole
  return (LEVEL as Record<string, number>)[key] ?? 0
}

/**
 * 能力定义。
 *
 * `min`   最低等级
 * `only`  仅限这些角色（用于「平台方不能做教学动作」这类**上界**约束）
 *
 * ★ `only` 是必要的：权限不是「越高越全能」。
 *   系统管理员的等级最高，但他**不能**发布作业、不能批改 ——
 *   如果只用 `min`，这些按钮就会出现在平台方的界面上。
 */
interface Rule {
  min?: number
  only?: Role[]
}

const RULES = {
  // ── 平台治理（仅 L3）──
  'platform.orgs.manage': { min: LEVEL.SYSTEM },
  'platform.quota': { min: LEVEL.SYSTEM },
  'platform.audit.global': { min: LEVEL.SYSTEM },
  'platform.public.review': { min: LEVEL.SYSTEM },

  // ── 学校治理（L2 起）──
  'org.settings': { min: LEVEL.SCHOOL },
  // ★★ 建班与名册是**教师**的动作（2026-10-21 规则改版）：
  //    学校不统一设班，班级由教师自建。这条从 LEVEL.SCHOOL 降到 TEACHER。
  //    「改归属」仍归学校 —— 那是治理动作，见下面 org.classroom.assign。
  'org.classroom.manage': { min: LEVEL.TEACHER },
  'org.roster.manage': { min: LEVEL.TEACHER },
  //: 变更班级归属（历史班指派、离职转接）。★ 只有学校账户能改。
  'org.classroom.assign': { min: LEVEL.SCHOOL },
  'user.create': { min: LEVEL.SCHOOL },
  'user.reset-password': { min: LEVEL.SCHOOL },
  'user.disable': { min: LEVEL.SCHOOL },
  'user.transfer': { min: LEVEL.SCHOOL },
  'knowledge.node.manage': { min: LEVEL.SCHOOL },
  // ★★ 知识库条目的增删改（2026-10-23 口径改版）：**教师**，不是学校账户。
  //    需求方定的是「条目归属老师个人，由老师自行建立」——
  //    老师录错一个字却要等学校账户来改，这个功能等于没做。
  //    学校账户仍可管全机构（后端 `assert_entry_writable` 按归属判），
  //    所以这里是 `min` 而不是 `only: ['TEACHER']`。
  //    ★ 原先是 `knowledge.entry.publish`（min SCHOOL）且全项目零引用 ——
  //      一个为条目写入准备、却因为编辑器没做而成了死配置的能力名。
  'knowledge.entry.manage': { min: LEVEL.TEACHER },
  'question.public.review': { min: LEVEL.SCHOOL },
  'analytics.org': { min: LEVEL.SCHOOL },

  // ── 教学（教师专属：L2/L3 只能看，不能做）──
  'paper.compose': { only: ['TEACHER'] },
  'paper.export': { only: ['TEACHER'] },
  'question.create': { only: ['TEACHER'] },
  /**
   * AI 生题。
   *
   * ★★ 这里与 `question.create` 的规则**故意不同**（`min` 而不是
   *   `only: ['TEACHER']`），和后端 `GenerateQuestionView` 的
   *   `IsTeacherAccount`（L1 及以上）保持一致。
   *
   *   不一致的代价是：后端允许、前端不给入口 —— 界面上"这个功能
   *   不存在"，而接口文档里有。要么两边都放开，要么两边都收紧；
   *   **只改一边会让排障的人怀疑自己看的是哪份代码**。
   *
   * ★ 如果产品上确定「生题只能教师做」（它消耗机构积分、往题库写题），
   *   那要**同时**改后端视图的权限类，而不是只在这里加 `only`。
   */
  'ai.generate': { min: LEVEL.TEACHER },
  'question.public.submit': { min: LEVEL.TEACHER },
  'assignment.publish': { only: ['TEACHER'] },
  'grading.execute': { only: ['TEACHER'] },
  'grading.confirm': { only: ['TEACHER'] },
  'analytics.teaching': { min: LEVEL.TEACHER },

  // ── 通用 ──
  'self.profile': { min: LEVEL.TEACHER },
  'self.apikey': { min: LEVEL.TEACHER },
} satisfies Record<string, Rule>

export type Capability = keyof typeof RULES

/** 该角色是否具备某能力。 */
export function can(role: string | null | undefined, cap: Capability): boolean {
  const rule: Rule = RULES[cap]
  if (rule.only) return rule.only.includes(String(role) as Role)
  return levelOf(role) >= (rule.min ?? 0)
}

/** 该角色的全部能力（调试用，也可给菜单渲染用）。 */
export function capabilitiesOf(role: string | null | undefined): Capability[] {
  return (Object.keys(RULES) as Capability[]).filter((c) => can(role, c))
}

/**
 * 路由所需的最低等级。
 *
 * ★ 给 `router/index.ts` 的 `meta.minLevel` 用：
 *   守卫里比较 `levelOf(user.role) < to.meta.minLevel` 就跳 403。
 */
export const ROUTE_MIN_LEVEL = {
  /** 所有登录用户 */
  any: LEVEL.TEACHER,
  /** 学校治理页（账号、班级、知识点、全校学情） */
  school: LEVEL.SCHOOL,
  /** 平台治理页（学校列表、全局审计） */
  system: LEVEL.SYSTEM,
} as const
