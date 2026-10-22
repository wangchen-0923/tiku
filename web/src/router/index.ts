/**
 * 路由。
 *
 * ★★ 守卫的判据是「**是否确定未登录**」，不是「有没有用户」：
 *
 *     if (!auth.resolved)  await auth.bootstrap()   // 先问清楚
 *     if (auth.isAnonymous) → 跳登录
 *
 *   第一版我差点写成 `if (!auth.user) → 跳登录`，那样刷新页面时
 *   `user` 还是 `null`（`/auth/me/` 尚未返回），有效令牌也会被
 *   踢回登录页 —— 这是这类应用最典型的糟糕体验。
 */

import { createRouter, createWebHistory } from 'vue-router'

import { LEVEL, levelOf } from '@/constants/permissions'
import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'Home',
      component: () => import('@/views/HomeView.vue'),
      meta: { title: '概览' },
    },
    {
      path: '/login',
      name: 'Login',
      component: () => import('@/views/LoginView.vue'),
      meta: { title: '登录', anonymousOnly: true },
    },
    {
      path: '/questions',
      name: 'QuestionList',
      component: () => import('@/views/QuestionListView.vue'),
      meta: { title: '题库' },
    },
    {
      // ★ 难度快速打标（`P1-100b`）。
      //
      //   独立页面，**不嵌在题库里**（需求方确认的形态）：
      //   打标是「一屏一道题、按 1–5 即刻前进」的键盘流，
      //   嵌进题库列表会被筛选/分页/表格干扰。
      //
      //   ★ 路径必须在 `/questions` **之后**：
      //     Vue Router 按顺序匹配，`/questions/mark-difficulty`
      //     比 `/questions` 更具体，放后面也能正确命中
      //     （`/questions` 不带 `:id`，不会吞掉子路径）。
      path: '/questions/mark-difficulty',
      name: 'QuestionMarkDifficulty',
      component: () => import('@/views/QuestionMarkView.vue'),
      meta: { title: '难度打标' },
    },
    {
      // ★★ 知识点批量补标（2026-10-16 需求方口径）。
      //
      //   与难度打标页**形态不同**：难度是「1944 道题每道都要标」，
      //   所以一屏一题；知识点补标的落点只有 44 个，痛点是
      //   「21 个知识点一道题都没有」——所以是「先选知识点，再批量勾题」。
      //
      //   ★ 同样独立成页，不塞进知识体系页：这里是**写数据**的地方，
      //     与「看指标」是两件事，混在一起容易误点。
      path: '/questions/mark-knowledge',
      name: 'QuestionMarkKnowledge',
      component: () => import('@/views/KnowledgeMarkView.vue'),
      meta: { title: '知识点补标' },
    },
    {
      // ★ 试卷导入（2026-10-17）：上传 → 切题 → 逐题确认 → 入库。
      //   只进题库、不生成试卷；未确认的题不入库（需求方口径）。
      path: '/import',
      name: 'ImportPaper',
      component: () => import('@/views/ImportView.vue'),
      meta: { title: '导入题目' },
    },
    {
      path: '/knowledge',
      name: 'Knowledge',
      component: () => import('@/views/KnowledgeView.vue'),
      meta: { title: '知识体系' },
    },
    {
      // ★★ 知识体系**管理**（计划 U10，2026-10-21）。
      //
      //   与 `/knowledge` 分开的理由不是「页面太长」，而是**动作性质不同**：
      //   浏览页是天天打开的运营台，管理页一学期动几次。
      //   把「删除知识点」放在天天看的那一页上，迟早误点。
      //
      //   `minLevel: SCHOOL` 与后端一致：课程/知识树是全校共用资产，
      //   教师在它下面自由增删，等于让 44 个知识点的编号由某位老师
      //   随手改掉（后端写接口也是 `IsSchoolAdmin`）。
      path: '/knowledge/manage',
      name: 'KnowledgeManage',
      component: () => import('@/views/KnowledgeManageView.vue'),
      meta: { title: '知识体系管理', minLevel: LEVEL.SCHOOL },
    },
    {
      // ★★ 教师账号（2026-10-21 规则改版后新增）。
      //
      //   学校不再统一建班，职责收窄成「授权教师账号」——
      //   这一页就是那个动作的入口。没有它，学校账户在新规则下
      //   一个动作都做不了。
      //
      //   `minLevel: SCHOOL` 只让学校平台账户及以上进入；
      //   教师看不见这个菜单（他不需要给自己开账号）。
      path: '/settings/teachers',
      name: 'TeacherAccounts',
      component: () => import('@/views/TeacherAccountsView.vue'),
      meta: { title: '教师账号', minLevel: LEVEL.SCHOOL },
    },
    {
      // ★★ 公共题审核工作台（U14，2026-10-21 题库双轨）。
      //
      //   `minLevel: SYSTEM` 与后端 `IsSystemAdmin` 一致：
      //   机构管理员**不能**进来 —— 审核是平台职责，
      //   而且队列里带来源机构名，机构管理员看到就等于
      //   「知道同行在提交什么」。
      path: '/platform/public-review',
      name: 'PublicReview',
      component: () => import('@/views/PublicReviewView.vue'),
      meta: { title: '公共题审核', minLevel: LEVEL.SYSTEM },
    },
    {
      // ★ 班级与名册：发布作业的前提。
      //   后端 `/org/classrooms/` 早已就绪，但此前**没有这一页**，
      //   导致「建班 → 导入学生 → 发布作业 → …」在第一步就断了。
      path: '/classrooms',
      name: 'Classroom',
      component: () => import('@/views/ClassroomView.vue'),
      meta: { title: '班级管理' },
    },
    {
      path: '/sheets',
      name: 'SheetList',
      component: () => import('@/views/SheetListView.vue'),
      meta: { title: '答题卡' },
    },
    {
      path: '/sheets/new',
      name: 'SheetDesignerNew',
      component: () => import('@/views/SheetDesignerView.vue'),
      meta: { title: '新建答题卡' },
    },
    {
      // ★ 静态路径必须在 `/sheets/:id/edit` 之前
      path: '/sheets/scan',
      name: 'SheetScan',
      component: () => import('@/views/SheetScanView.vue'),
      meta: { title: '扫描识别' },
    },
    {
      path: '/sheets/:id/edit',
      name: 'SheetDesigner',
      component: () => import('@/views/SheetDesignerView.vue'),
      meta: { title: '编辑答题卡' },
    },
    {
      path: '/papers',
      name: 'PaperList',
      component: () => import('@/views/PaperListView.vue'),
      meta: { title: '试卷' },
    },
    {
      path: '/papers/:id',
      name: 'PaperCompose',
      component: () => import('@/views/PaperComposeView.vue'),
      meta: { title: '组卷' },
    },
    {
      // ★★ 试卷导出独立成页（`P1-62b`）：
      //   左参数 / 右实时预览；原来的抽屉已删除，不允许两套导出界面并存。
      //   路由放在 `/papers/:id` **之后**（更具体的路径不怕顺序，
      //   但保持「列表 → 详情 → 子页」的阅读顺序）。
      path: '/papers/:id/export',
      name: 'PaperExport',
      component: () => import('@/views/PaperExportView.vue'),
      meta: { title: '试卷导出' },
    },
    {
      path: '/assignments',
      name: 'AssignmentList',
      component: () => import('@/views/AssignmentListView.vue'),
      meta: { title: '作业' },
    },
    {
      path: '/assignments/:id',
      name: 'AssignmentDetail',
      component: () => import('@/views/AssignmentDetailView.vue'),
      meta: { title: '作业详情' },
    },
    {
      path: '/assignments/:id/grading',
      name: 'GradingWorkbench',
      component: () => import('@/views/GradingWorkbenchView.vue'),
      meta: { title: '批改' },
    },
    {
      // ★★ 主观题阅卷（21 文档任务一）。入口只在「批改工作台」顶部。
      //
      //   与「批改工作台」并列而不是替代：
      //     工作台  按**学生**看（谁交了、谁批完）—— 班主任视角
      //     阅卷页  按**题目**看（第 11 题批了几份）—— 改卷视角
      //   老师的真实动线是后者：一次只批一道题，45 份连着看。
      path: '/assignments/:id/review',
      name: 'AssignmentReview',
      component: () => import('@/views/AssignmentReviewView.vue'),
      meta: { title: '主观题阅卷' },
    },
    {
      path: '/assignments/:id/analytics',
      name: 'AssignmentAnalytics',
      component: () => import('@/views/AssignmentAnalyticsView.vue'),
      meta: { title: '学情' },
    },
    // ★★ 学生端扫码入口：`/s/{publish_token}`
    //
    //   `bare: true` → **不用教师端外壳**（没有顶栏、没有导航）。
    //   学生不是登录用户，给他看「概览/题库/试卷」导航是错的，
    //   而且手机上那 56px 顶栏很浪费屏幕。
    //
    //   `public: true` → **不走登录守卫**。学生的身份由 URL 里的
    //   `publish_token` + 绑定后签发的 `session_token` 承载，
    //   把他踢到教师登录页是荒谬的。
    {
      path: '/s/:token',
      name: 'StudentAnswer',
      component: () => import('@/views/StudentAnswerView.vue'),
      meta: { title: '答题', bare: true, public: true },
    },
    {
      path: '/forbidden',
      name: 'Forbidden',
      component: () => import('@/views/ForbiddenView.vue'),
      // ★ `public: true`：这个页面本身就是「权限不足」的终点，不能再走等级校验。
      //   不加的话，等级不足的用户被送到这里后，守卫会再判一次
      //   `level < minLevel` 又送回 Forbidden —— 同路由重定向，
      //   导航被 abort，用户看到的是一张白屏。
      meta: { title: '无权访问', public: true },
    },
    {
      path: '/:pathMatch(.*)*',
      name: 'NotFound',
      component: () => import('@/views/NotFoundView.vue'),
      meta: { title: '页面不存在' },
    },
  ],
})

/**
 * 一轮导航里已经发生的重定向次数。
 *
 * ★ 存在理由：守卫互相 return 对方 = 无限导航 = **标签页永远转圈**，
 *   而且不抛错、不报警（2026-10-22 的 Login ↔ Forbidden 事故）。
 *   有了这个计数，最坏情况只是「显示了一张不该出现的页面」，
 *   而不是整个应用打不开。
 */
let redirectHops = 0

router.beforeEach(async (to) => {
  // ★★★ 重定向硬上限。守卫里任何两个页面互相 return 对方，都会形成
  //   「导航永不落地 → 主线程被无限任务占满 → 标签页一直转圈」的死循环。
  //   这种故障**不抛错、不报错、控制台一片干净**，排查成本极高
  //   （2026-10-22 就栽在 Login ↔ Forbidden 上）。
  //
  //   所以这里留一道机械兜底：一轮导航里重定向超过 4 跳就直接放行，
  //   宁可显示一张不该出现的页面，也不要把整个应用拖死。
  //   `afterEach` 里把它归零 —— 死循环时导航永不完成，计数器只增不减，
  //   刚好能触发上限；正常导航每跳都会归零，不会有误伤。
  if (redirectHops > 4) {
    return true
  }

  // ★★ 公共页（学生答题、登录）**先放行**，不要先跑 `bootstrap()`：
  //   学生的浏览器里不该有任何教师令牌相关的请求。
  if (to.meta.public) {
    return true
  }

  const auth = useAuthStore()

  // 确定登录状态（有令牌才真的请求，且只跑一次）
  if (!auth.resolved) {
    await auth.bootstrap()
  }

  /** 记一次重定向（越限后由上面的兜底放行） */
  if (to.meta.anonymousOnly && auth.isAuthenticated) {
    redirectHops += 1
    return { name: 'Home' }
  }
  if (!to.meta.anonymousOnly && auth.isAnonymous) {
    // ★ 记住原本要去的地址，登录后跳回去
    redirectHops += 1
    return { name: 'Login', query: { redirect: to.fullPath } }
  }

  // ★★★ 上面两行已经处理完「登录 / 未登录」的分流，这里**必须**先放行
  //   `anonymousOnly` 的页面，再做等级校验。
  //
  //   漏掉这一句会形成**登录页 ↔ Forbidden 无限互踢**：
  //
  //     未登录访问 `/`  → isAnonymous → 送去 Login
  //     Login 有 anonymousOnly → 前两个 if 都不进 → 落到下面的等级校验
  //     未登录时 levelOf(undefined) = 0 < 10 → 送去 Forbidden
  //     Forbidden 没有 anonymousOnly → isAnonymous → 又送回 Login
  //     回到第 2 步 → 死循环
  //
  //   后果不是「跳错页」，而是**整个应用打不开**：每次重定向都要加载一次
  //   路由组件，主线程被无限任务占满，`DOMContentLoaded` 永远不触发，
  //   浏览器标签页一直转圈。
  //
  //   ★ 为什么长期没被发现：开发时浏览器里一直有有效令牌，
  //     `isAnonymous` 恒为假，这条路径从来没被走过。一旦令牌过期
  //     或被清掉（换浏览器、清缓存、`localStorage` 失效），立刻必现。
  if (to.meta.anonymousOnly) {
    return true
  }

  // ★★ 三级账户的等级校验。
  //
  //    `meta.minLevel` 不写时默认 `LEVEL.TEACHER`（10）——
  //    教师端的每一个页面都至少要求教师等级，学生身份行
  //    （`role=STUDENT`，等级 0）不该看到任何一页。
  //
  //    ★ 这只是**体验层**：真正的拦截在后端（403）。
  //      前端拦得住的只是「不该出现的菜单」，拦不住直接调接口。
  const need = (to.meta.minLevel as number | undefined) ?? LEVEL.TEACHER
  if (levelOf(auth.user?.role) < need) {
    // ★ `to.name === 'Forbidden'` 时不能再跳 Forbidden（同路由重定向会被
    //   vue-router abort，用户看到白屏）。Forbidden 已标 `public`，
    //   正常走不到这里，这行是兜底。
    if (to.name === 'Forbidden') return true
    redirectHops += 1
    return { name: 'Forbidden' }
  }
  return true
})

router.afterEach((to) => {
  // ★ 一轮导航落地，重定向计数归零（见 `beforeEach` 顶部的硬上限）
  redirectHops = 0
  const t = (to.meta.title as string) || ''
  document.title = t ? `${t} · 知否` : '知否 · 中职机械制造技术题库与精准作业'
})

export default router
