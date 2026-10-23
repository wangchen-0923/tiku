<script setup lang="ts">
/**
 * 设置。
 *
 * ★★ 这一页存在的理由，不是「把几个链接装进一个盒子」，而是
 *    **给一类东西找个固定的家**：一学期只动几次的配置。
 *
 *   原来「教师账号」「积分与凭证」各占一个顶栏一级位置。顶栏是
 *    `overflow-x: auto` 的横条（见 app.css 的溢出策略），项一多就要
 *    横向滚动，而滚动条默认隐藏 —— 用户**看不出右边还有内容**。
 *    把它们收进设置，顶栏从 11 项降到 8 项，滚动条不再触发。
 *
 * ★ 判据（和顶栏那条一致）：
 *     一级 = 老师每天会进、进去就干活的主界面
 *     设置 = 一学期动几次的配置
 *   「教师账号」一年只在开学初动、教师本人根本不需要；
 *   「积分与凭证」是话费用完才来。两者都不是日常动线。
 *
 * ★ 为什么是页面而不是下拉菜单：
 *   下拉菜单里必须写一行字解释「这一项是干嘛的」，否则光看
 *   「教师账号」四个字，不知道它能不能建班、能不能改密码。
 *   页面里可以有说明、有适用角色、有「进入」按钮 —— 用户不必先
 *   知道它存在，也能看懂它是什么。
 *
 * ★ 不做的事：这里**不放**任何表单。所有编辑都在各自的原页面里，
 *   设置页只做**分发**。两处都能改，就会出现两套校验、两种报错
 *   文案（个人中心的手机号规则和教师账号页的必然分叉）。
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import type { Capability } from '@/constants/permissions'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()

interface SettingEntry {
  /** 路由名 */
  name: string
  title: string
  desc: string
  /** 需要的能力；不写 = 所有登录用户都能看到 */
  requires?: Capability
  /**
   * 还要求账号**属于某个机构**。
   *
   * ★★ 系统管理员（`PLATFORM_ADMIN`）等级最高，`user.create` 这类
   *   `min: LEVEL.SCHOOL` 的能力它**也满足**——但它的 `org_id` 是空的，
   *   后端任何带 `_org_id(request)` 的业务接口对它一律 403。
   *   只按 `can()` 判断，平台管理员就会看到一个点进去全是 403 的
   *   「教师账号」——比不显示更糟：他会以为自己权限坏了。
   *
   * ★ 这是权限模型的既有口径缺口（`min` 表达「至少」，但机构治理动作
   *   对平台方本来就无意义），本页先按「有没有机构」兜住；
   *   要从根上修得给 `RULES` 里那批能力补 `only: ['ORG_ADMIN']`，
   *   那会牵动所有机构页面，单独排期。
   */
  requiresOrg?: boolean
  /** 适合谁（显示成标签，帮用户判断「这是不是我要找的」） */
  audience?: string
}

interface SettingGroup {
  key: string
  title: string
  entries: SettingEntry[]
}

const GROUPS: SettingGroup[] = [
  {
    key: 'members',
    title: '成员',
    entries: [
      {
        name: 'TeacherAccounts',
        title: '教师账号',
        desc: '授权本校教师登录、重置密码、停用账号。学校不统一建班，班由教师自建，所以这里只管人。',
        // ★ 只有学校平台账户能授权教师：教师不需要给自己开账号。
        //   `requiresOrg` 再拦一层：平台管理员虽然满足 `user.create`
        //   的等级要求，但没有机构，建不了本校教师。
        requires: 'user.create',
        requiresOrg: true,
        audience: '学校平台账户',
      },
    ],
  },
  {
    key: 'billing',
    title: '计费',
    entries: [
      {
        name: 'Billing',
        title: '积分与凭证',
        desc: '查看余额与逐笔流水、配置自己的 API Key。学校平台账户在同一页里改积分规则、发放本月额度、对账、调账。',
        // ★ 刻意**不设** requires：设成 `org.settings` 会让教师
        //   连自己的余额都看不到。学校配置那一块在页内按能力控制。
        audience: '全部教师',
      },
    ],
  },
  {
    key: 'personal',
    title: '个人',
    entries: [
      {
        name: 'Profile',
        title: '个人中心',
        desc: '改姓名与手机号、修改密码（改完旧会话立即失效，手机丢了就靠这一步）。',
        audience: '全部角色',
      },
    ],
  },
]

/** 过滤掉当前身份看不到的条目；整组为空则整组不渲染。 */
const visibleGroups = computed(() =>
  GROUPS.map((g) => ({
    ...g,
    entries: g.entries.filter(
      (e) =>
        (!e.requires || auth.can(e.requires)) &&
        (!e.requiresOrg || Boolean(auth.user?.org_id)),
    ),
  })).filter((g) => g.entries.length > 0),
)

function open(name: string): void {
  void router.push({ name })
}
</script>

<template>
  <div class="zf-stack">
    <div class="zf-row" style="justify-content: space-between">
      <div>
        <h1 class="page-title">设置</h1>
        <p class="zf-muted page-sub">
          一学期只动几次的东西：谁可以登录、话费还剩多少、自己的密码
        </p>
      </div>
    </div>

    <section
      v-for="group in visibleGroups"
      :key="group.key"
      class="zf-card"
    >
      <div class="zf-card__head">
        <h2 class="zf-card__title">{{ group.title }}</h2>
      </div>
      <div class="zf-card__body set-body">
        <!-- ★ 用 `<button>` 而不是 `<div @click>`：键盘能 Tab 到、
             Enter 能进，读屏软件会念「按钮」。整行可点也要能键盘点。 -->
        <button
          v-for="entry in group.entries"
          :key="entry.name"
          type="button"
          class="set-row"
          @click="open(entry.name)"
        >
          <span class="set-row__main">
            <span class="set-row__t">
              {{ entry.title }}
              <el-tag
                v-if="entry.audience"
                size="small"
                type="info"
                class="set-row__tag"
              >
                {{ entry.audience }}
              </el-tag>
            </span>
            <span class="set-row__d">{{ entry.desc }}</span>
          </span>
          <span class="set-row__go" aria-hidden="true">进入 ›</span>
        </button>
      </div>
    </section>

    <p class="zf-dim set-foot">
      这里不提供任何表单：教师账号在「教师账号」页改、密码在「个人中心」改、
      积分规则在「积分与凭证」页改。两处都能改，就会出现两套校验。
    </p>
  </div>
</template>

<style scoped>
.set-body {
  display: flex;
  flex-direction: column;
  gap: 0;
  padding-top: 0;
  padding-bottom: 0;
}

/* ★ 行与行之间用 `border-top`（第一条不画），不用 gap + 卡片：
   设置项是**同一个列表**里的条目，拆成一张张卡会让页面看起来
   像三个互不相关的功能，而它们其实是同一件事（配置）。 */
.set-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--zf-space-4);
  width: 100%;
  padding: var(--zf-space-4) 0;
  border: none;
  border-top: 1px solid var(--zf-border);
  background: transparent;
  text-align: left;
  cursor: pointer;
  font: inherit;
  color: inherit;
}

.set-row:first-child {
  border-top: none;
}

.set-row:hover {
  background: var(--zf-bg-hover);
}

/* ★ 键盘焦点必须看得见：整行是个隐形按钮，没有焦点环
   键盘用户会以为 Tab 卡住了。 */
.set-row:focus-visible {
  outline: 2px solid var(--zf-brand);
  outline-offset: -2px;
  border-radius: var(--zf-radius-sm);
}

.set-row__main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.set-row__t {
  display: flex;
  align-items: center;
  gap: var(--zf-space-2);
  font-size: var(--zf-text-md);
  font-weight: var(--zf-weight-medium);
  color: var(--zf-text-primary);
}

.set-row__tag {
  font-weight: var(--zf-weight-normal);
}

.set-row__d {
  font-size: var(--zf-text-sm);
  color: var(--zf-text-secondary);
  line-height: 1.6;
}

.set-row__go {
  flex: none;
  font-size: var(--zf-text-sm);
  color: var(--zf-brand);
}

.set-foot {
  font-size: var(--zf-text-xs);
  margin: 0;
}
</style>
