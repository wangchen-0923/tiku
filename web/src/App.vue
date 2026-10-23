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

/* ══════════════════════════════════════════════════════════════
   一级导航只放「核心模块」，一个模块一项
   ══════════════════════════════════════════════════════════════

   ★★ 收起的是**步骤**，不是**模块**：
       难度打标 / 导入题目    → 题库页（页内工具栏 + 难度未分层告警里）
       知识点补标 / 知识管理  → 知识体系页（页头 + 零题知识点卡）
       教师账号 / 积分与凭证  → 设置页（`/settings`，卡片式入口）

   ★ 为什么不做成下拉菜单：下拉把入口**藏起来**了，用户得先知道
     「它在那儿」才点得开；而页内入口是**在该动手的地方出现**——
     库里还有 1200 道题没标难度时，告警旁边就有「去快速打标」。
     入口跟着问题走，比跟着分类走有用。

   ★ 判定标准（用来决定以后新加的东西放哪儿）：
     一级 = 老师**每天**会进，且进去就是干活的主界面。
     其余一律进它所属的页，哪怕它自己是个完整页面。
   ══════════════════════════════════════════════════════════════ */
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

        <!-- ★★ 题库：一级只留「模块」，不留「步骤」。
             导入题目、难度打标都在**题库页里**：
               - 页头工具栏（随时可用）
               - 难度未分层告警卡（有问题时才出现，旁边就是「去快速打标」）
             ★ 不收进下拉菜单：菜单把入口藏起来，用户得先知道它在那儿；
               而页内入口出现在「你正看着这个问题」的地方。 -->
        <RouterLink class="zf-navlink" :to="{ name: 'QuestionList' }">
          题库
        </RouterLink>

        <!-- ★★ 知识体系：知识点补标与结构管理都在**知识体系页里**。
             补标挂在「零题知识点」卡上（看到 21 个点一道题都没有 →
             下一步就是补），管理在页头（学校平台账户才显示）。 -->
        <RouterLink class="zf-navlink" :to="{ name: 'Knowledge' }">
          知识体系
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

        <!-- ★★ 设置：教师账号、积分与凭证都在 `/settings` 页里。
             它们是「一学期动几次」的配置，不是每天的教学动线 ——
             常驻一级会让顶栏里出现三项一年点两次的链接。 -->
        <RouterLink class="zf-navlink" :to="{ name: 'Settings' }">
          设置
        </RouterLink>

        <!-- ★ 公共题审核（U14 题库双轨）：**仅平台管理员**，保留一级。
             理由是它不属于任何机构页面（机构管理员根本进不去），
             平台管理员登录后只有这一件正事。 -->
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
          <!-- ★ 姓名现在是**个人中心入口**（U26）：改密码、看积分都在那里。
               原来它只是一段纯文本，用户找不到「我在哪儿改密码」。 -->
          <RouterLink
            class="zf-navlink zf-topbar__me"
            :to="{ name: 'Profile' }"
            :title="'个人中心'"
          >
            {{ auth.user?.real_name || auth.user?.username }}
            <span v-if="showRole" class="zf-dim">· {{ auth.roleLabel }}</span>
          </RouterLink>
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
