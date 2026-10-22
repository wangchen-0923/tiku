<script setup lang="ts">
/**
 * 应用外壳。
 *
 * ★ 顶栏在**未登录时也显示**（只有品牌名）——登录页不该是「另一个
 *   应用」，否则用户会以为跳到了别处。
 */
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const showNav = computed(() => auth.isAuthenticated)
/** ★ 姓名与角色重复时不再重复显示（`real_name` 可能就叫「平台管理员」） */
const showRole = computed(
  () =>
    Boolean(auth.roleLabel) &&
    auth.roleLabel !== (auth.user?.real_name || auth.user?.username),
)
/**
 * 不用教师端外壳的页面。
 *
 * ★ 两个来源，合并成一个判据：
 *     1. 路由 meta 上的 `bare`（学生答题等整页独占的页面）
 *     2. 登录页与 404 页（它们自己排布，但**仍保留品牌顶栏**）
 *
 *   ★ 第 2 类与第 1 类的区别是「要不要顶栏」：
 *     登录页有顶栏（让人知道是同一个应用），
 *     学生答题页**没有**顶栏（他不是这个应用的用户，且手机屏幕宝贵）。
 */
const bare = computed(
  () =>
    Boolean(route.meta.bare) ||
    route.name === 'Login' ||
    route.name === 'NotFound',
)
/** ★ 是否显示品牌顶栏：学生答题页不显示 */
const showTopbar = computed(() => !route.meta.bare)

function handleLogout(): void {
  auth.logout()
  void router.push({ name: 'Login' })
}
</script>

<template>
  <div class="zf-shell">
    <!-- ★ `showTopbar` 为假时不渲染顶栏（学生答题页）。
         用 `v-if` 而不是 CSS 隐藏：隐藏了仍占布局与无障碍树。 -->
    <header v-if="showTopbar" class="zf-topbar">
      <div class="zf-topbar__brand">
        知否
        <small>中职机械制造技术</small>
      </div>

      <nav v-if="showNav" class="zf-topbar__nav">
        <RouterLink class="zf-navlink" :to="{ name: 'Home' }">概览</RouterLink>
        <RouterLink class="zf-navlink" :to="{ name: 'QuestionList' }">
          题库
        </RouterLink>
        <!-- ★ 难度快速打标（`P1-100b`）。
             放顶层导航而不是藏在题库页里：打标是一件**独立的、
             一次要花两三小时**的工作，老师需要能直接进来、
             也能中途离开再回来（`after_id` 支持续标）。 -->
        <RouterLink class="zf-navlink" :to="{ name: 'QuestionMarkDifficulty' }">
          难度打标
        </RouterLink>
        <!-- ★ 知识点补标：与难度打标并列的独立页面。
             它解的是「21 个知识点一道题都没有」——
             按那些点出的学情与个性化练习永远是空的。 -->
        <RouterLink class="zf-navlink" :to="{ name: 'QuestionMarkKnowledge' }">
          知识点补标
        </RouterLink>
        <!-- ★ 导入题目：上传 Word/PDF/Excel → 逐题确认 → 入题库。
             与「难度打标」「知识点补标」是一组：它们都在补数据。 -->
        <RouterLink class="zf-navlink" :to="{ name: 'ImportPaper' }">
          导入题目
        </RouterLink>
        <RouterLink class="zf-navlink" :to="{ name: 'Knowledge' }">
          知识体系
        </RouterLink>
        <!-- ★ 知识体系**管理**（计划 U10）：加章/任务/知识点、移动、删除。
             只给学校平台账户（`knowledge.node.manage` 是 L2 能力）——
             课程与知识树是全校共用资产，教师不该改它的结构。 -->
        <RouterLink
          v-if="auth.can('knowledge.node.manage')"
          class="zf-navlink"
          :to="{ name: 'KnowledgeManage' }"
        >
          知识管理
        </RouterLink>
        <!-- ★ 教师账号：学校平台账户的主界面（2026-10-21 规则改版）。
             学校不建班，只授权教师账号 —— 这是它唯一的日常动作。
             教师看不到这一项：他不需要给自己开账号。 -->
        <RouterLink
          v-if="auth.can('user.create')"
          class="zf-navlink"
          :to="{ name: 'TeacherAccounts' }"
        >
          教师账号
        </RouterLink>
        <RouterLink class="zf-navlink" :to="{ name: 'Classroom' }">
          班级
        </RouterLink>
        <RouterLink class="zf-navlink" :to="{ name: 'PaperList' }">
          试卷
        </RouterLink>
        <RouterLink class="zf-navlink" :to="{ name: 'AssignmentList' }">
          作业
        </RouterLink>
        <RouterLink class="zf-navlink" :to="{ name: 'SheetList' }">
          答题卡
        </RouterLink>
        <!-- ★ 公共题审核（U14 题库双轨）：**仅平台管理员**。
             队列里带「来源机构」，机构管理员看到就等于知道同行在交什么，
             所以能力是 `platform.public.review`（L3），不是 `question.public.*`。 -->
        <RouterLink
          v-if="auth.can('platform.public.review')"
          class="zf-navlink"
          :to="{ name: 'PublicReview' }"
        >
          公共题审核
        </RouterLink>
      </nav>
      <div v-else class="zf-topbar__nav" />

      <div class="zf-topbar__right">
        <template v-if="auth.isAuthenticated">
          <!-- 姓名与角色可能重复（real_name 可能就叫「平台管理员」），
               重复时只显示一次。
               注意：Vue 模板的注释语法是 HTML 注释，不是 Django 的
               花括号井号语法。用后者会把注释**原样渲染到页面上**。 -->
          <span class="zf-muted" style="font-size: var(--zf-text-sm)">
            {{ auth.user?.real_name || auth.user?.username }}
            <span v-if="showRole" class="zf-dim">· {{ auth.roleLabel }}</span>
          </span>
          <el-button size="small" @click="handleLogout">退出</el-button>
        </template>
        <RouterLink
          v-else-if="!bare"
          class="zf-navlink"
          :to="{ name: 'Login' }"
        >
          登录
        </RouterLink>
      </div>
    </header>

    <!-- ★ 学生答题页用 `zf-main--bare`：去掉内容区的最大宽度限制，
         让它能撑满手机屏（`zf-main` 是给桌面教师端用的）。 -->
    <main class="zf-main" :class="{ 'zf-main--bare': bare }">
      <RouterView />
    </main>
  </div>
</template>
