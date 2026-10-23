<script setup lang="ts">
/**
 * 个人中心（U26）。
 *
 * 三块：**资料 / 安全 / 我的资产**。
 *
 * ★★ 两个刻意的设计
 *
 *   1. **资料表单里没有「角色」和「登录名」**
 *      不是「灰掉」，是根本不渲染。灰掉的输入框会让人反复尝试，
 *      最后来问「为什么改不了」。后端序列化器同样是白名单，
 *      两边都不给绕过的可能。
 *
 *   2. **改密码成功后立即登出**
 *      后端会让所有旧令牌（访问 + 刷新）立即失效——这是正确行为，
 *      但意味着成功后**当前会话已经死了**。如果不主动跳登录页，
 *      老师会停在这个页面上继续点，然后遇到一连串 401，
 *      以为是「系统坏了」。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  changePassword,
  getProfile,
  updateMe,
  type ProfilePayload,
} from '@/api/profile'
import { ApiError } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()

const loading = ref(false)
const error = ref('')
const data = ref<ProfilePayload | null>(null)
const tab = ref<'profile' | 'security' | 'assets'>('profile')

// ── 资料表单 ──
const profileForm = reactive({ real_name: '', phone: '' })
const savingProfile = ref(false)

// ── 改密表单 ──
const pwdForm = reactive({
  old_password: '',
  new_password: '',
  confirm: '',
})
const changingPwd = ref(false)

const profile = computed(() => data.value?.profile)
const security = computed(() => data.value?.security)
const assets = computed(() => data.value?.assets)
const credits = computed(() => assets.value?.credits)

/**
 * 是否真的「余额偏低」。
 *
 * ★ 必须叠加「本月已发放过」这个条件。
 *   本月还没发积分时余额当然是 0——那不是「低」，是「还没给」。
 *   两者要老师做的事完全不同：一个是等着，一个是去找管理员。
 */
const isReallyLow = computed(
  () => Boolean(credits.value?.has_grant) && Boolean(credits.value?.is_low),
)

/** 姓名或手机号是否改过（没改就不让点保存，避免无意义的审计记录）。 */
const profileDirty = computed(() => {
  const p = profile.value
  if (!p) return false
  return (
    profileForm.real_name !== (p.real_name || '') ||
    profileForm.phone !== (p.phone || '')
  )
})

/** 新密码的即时反馈：长度来自后端 `auth.password_min_length`（默认 8）。 */
const pwdHint = computed(() => {
  const v = pwdForm.new_password
  if (!v) return ''
  if (v.length < 8) return '至少 8 位'
  if (v === pwdForm.old_password) return '新密码不能与原密码相同'
  if (pwdForm.confirm && v !== pwdForm.confirm) return '两次输入不一致'
  return ''
})

const canSubmitPwd = computed(
  () =>
    Boolean(pwdForm.old_password) &&
    Boolean(pwdForm.new_password) &&
    pwdForm.new_password === pwdForm.confirm &&
    !pwdHint.value,
)

function errText(e: unknown): string {
  if (e instanceof ApiError) return e.message
  return e instanceof Error ? e.message : '操作失败'
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const res = await getProfile()
    data.value = res
    profileForm.real_name = res.profile.real_name || ''
    profileForm.phone = res.profile.phone || ''
  } catch (e) {
    error.value = errText(e)
  } finally {
    loading.value = false
  }
}

async function saveProfile(): Promise<void> {
  if (!profileDirty.value) return
  savingProfile.value = true
  try {
    await updateMe({
      real_name: profileForm.real_name,
      phone: profileForm.phone,
    })
    ElMessage.success('资料已更新')
    // ★ 重新拉一次而不是本地回填：以后端返回为准，
    //   避免「前端显示改成功了、库里其实没变」。
    await load()
    await auth.bootstrap()
  } catch (e) {
    ElMessage.error(errText(e))
  } finally {
    savingProfile.value = false
  }
}

async function submitPassword(): Promise<void> {
  if (!canSubmitPwd.value) return
  try {
    await ElMessageBox.confirm(
      '修改后所有设备都需要用新密码重新登录，当前页面也会退出。继续吗？',
      '修改密码',
      { type: 'warning', confirmButtonText: '修改', cancelButtonText: '取消' },
    )
  } catch {
    return
  }

  changingPwd.value = true
  try {
    await changePassword({
      old_password: pwdForm.old_password,
      new_password: pwdForm.new_password,
    })
    // ★ 顺序很重要：先清本地令牌，再提示，再跳转。
    //   后端已让旧令牌失效，本地留着它只会让后续请求 401。
    auth.logout()
    ElMessage.success('密码已修改，请用新密码重新登录')
    await router.push({ name: 'Login' })
  } catch (e) {
    ElMessage.error(errText(e))
  } finally {
    changingPwd.value = false
  }
}

function fmtDate(v: string | null | undefined): string {
  if (!v) return '—'
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return v
  return d.toLocaleString('zh-CN', { hour12: false })
}

function fmtCredits(v: string | null | undefined): string {
  if (v === null || v === undefined || v === '') return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return String(v)
  // 整数就不显示小数点，0.5 这种保留
  return Number.isInteger(n) ? String(n) : n.toFixed(2)
}

onMounted(load)
</script>

<template>
  <div class="pf">
    <header class="zf-page-head">
      <div>
        <h1 class="zf-page-title">个人中心</h1>
        <p class="zf-page-desc">
          资料、密码与我的积分。班级与试卷属于你个人，
          <b>学校看不到</b>；积分由学校平台账户按月发放。
        </p>
      </div>
      <div class="pf__acts">
        <el-button :loading="loading" @click="load">刷新</el-button>
      </div>
    </header>

    <el-alert
      v-if="error"
      type="error"
      :title="error"
      show-icon
      :closable="false"
      style="margin-bottom: 12px"
    />

    <el-skeleton v-if="loading && !data" :rows="6" animated />

    <template v-else-if="data">
      <!-- ★ 顶部身份条：让人一眼确认「我登的是哪个账号」 -->
      <section class="zf-card pf__idcard">
        <div class="pf__avatar">
          {{ (profile?.real_name || profile?.username || '?').slice(0, 1) }}
        </div>
        <div class="pf__idmain">
          <div class="pf__name">
            {{ profile?.real_name || profile?.username }}
            <el-tag size="small" effect="plain">
              {{ profile?.role_label || profile?.role }}
            </el-tag>
          </div>
          <div class="pf__meta">
            <span>{{ profile?.username }}</span>
            <span v-if="profile?.org_name" class="pf__dot">·</span>
            <span v-if="profile?.org_name">{{ profile?.org_name }}</span>
          </div>
        </div>
      </section>

      <el-tabs v-model="tab" class="pf__tabs">
        <!-- ══════════════ 资料 ══════════════ -->
        <el-tab-pane label="资料" name="profile">
          <section class="zf-card">
            <div class="zf-card__head">
              <h2 class="zf-card__title">基本信息</h2>
              <span class="zf-dim">登录名与角色只能由学校平台账户修改</span>
            </div>
            <div class="zf-card__body">
              <el-form label-width="96px" class="pf__form">
                <el-form-item label="登录名">
                  <el-input :model-value="profile?.username" disabled />
                  <div class="pf__tip">
                    登录名是你在系统里的唯一标识，改了对不上历史记录。
                  </div>
                </el-form-item>

                <el-form-item label="角色">
                  <el-input
                    :model-value="profile?.role_label || profile?.role"
                    disabled
                  />
                </el-form-item>

                <el-form-item label="姓名">
                  <el-input
                    v-model="profileForm.real_name"
                    maxlength="100"
                    show-word-limit
                    placeholder="请填写真实姓名"
                  />
                </el-form-item>

                <el-form-item label="手机号">
                  <el-input
                    v-model="profileForm.phone"
                    maxlength="20"
                    placeholder="可留空"
                  />
                  <div class="pf__tip">
                    仅用于学校内部联系，不会出现在学生看到的页面上。
                  </div>
                </el-form-item>

                <el-form-item>
                  <el-button
                    type="primary"
                    :loading="savingProfile"
                    :disabled="!profileDirty"
                    @click="saveProfile"
                  >
                    保存
                  </el-button>
                  <el-button
                    v-if="profileDirty"
                    @click="
                      profileForm.real_name = profile?.real_name || '';
                      profileForm.phone = profile?.phone || ''
                    "
                  >
                    撤销
                  </el-button>
                </el-form-item>
              </el-form>
            </div>
          </section>
        </el-tab-pane>

        <!-- ══════════════ 安全 ══════════════ -->
        <el-tab-pane label="安全" name="security">
          <section class="zf-card" style="margin-bottom: 12px">
            <div class="zf-card__head">
              <h2 class="zf-card__title">最近登录</h2>
            </div>
            <div class="zf-card__body">
              <el-descriptions :column="2" border size="small">
                <el-descriptions-item label="时间">
                  {{ fmtDate(security?.last_login_at) }}
                </el-descriptions-item>
                <el-descriptions-item label="IP">
                  {{ security?.last_login_ip || '—' }}
                </el-descriptions-item>
              </el-descriptions>
              <div class="pf__tip" style="margin-top: 8px">
                如果不是你本人登录，请立即修改密码。
              </div>
            </div>
          </section>

          <section class="zf-card">
            <div class="zf-card__head">
              <h2 class="zf-card__title">修改密码</h2>
              <span class="zf-dim">修改后所有设备均需重新登录</span>
            </div>
            <div class="zf-card__body">
              <el-form label-width="96px" class="pf__form">
                <el-form-item label="原密码">
                  <el-input
                    v-model="pwdForm.old_password"
                    type="password"
                    show-password
                    autocomplete="current-password"
                  />
                </el-form-item>
                <el-form-item label="新密码">
                  <el-input
                    v-model="pwdForm.new_password"
                    type="password"
                    show-password
                    autocomplete="new-password"
                  />
                  <div v-if="pwdHint" class="pf__tip pf__tip--warn">
                    {{ pwdHint }}
                  </div>
                </el-form-item>
                <el-form-item label="确认新密码">
                  <el-input
                    v-model="pwdForm.confirm"
                    type="password"
                    show-password
                    autocomplete="new-password"
                  />
                </el-form-item>
                <el-form-item>
                  <el-button
                    type="primary"
                    :loading="changingPwd"
                    :disabled="!canSubmitPwd"
                    @click="submitPassword"
                  >
                    修改密码
                  </el-button>
                </el-form-item>
              </el-form>
            </div>
          </section>
        </el-tab-pane>

        <!-- ══════════════ 我的资产 ══════════════ -->
        <el-tab-pane label="我的资产" name="assets">
          <el-alert
            v-if="assets && !assets.available"
            type="info"
            :title="assets.reason || '当前账号不参与积分计费'"
            show-icon
            :closable="false"
            style="margin-bottom: 12px"
          />

          <template v-if="credits">
            <div class="pf__stats">
              <div class="pf__stat">
                <div class="pf__stat-label">可用积分</div>
                <div
                  class="pf__stat-value"
                  :class="{ 'pf__stat-value--low': isReallyLow }"
                >
                  {{ fmtCredits(credits.balance) }}
                </div>
                <div class="pf__stat-sub">
                  单价 {{ credits.price_yuan_per_point }} 元 / 分
                </div>
              </div>
              <div class="pf__stat">
                <div class="pf__stat-label">本月已用</div>
                <div class="pf__stat-value">
                  {{ fmtCredits(credits.period_used) }}
                </div>
                <!-- ★ 把「发放」和「已用」并排放：只看已用看不出
                     「是没发还是用完了」，两个数一起才成立。 -->
                <div class="pf__stat-sub">
                  账期 {{ credits.period }} · 已发放
                  {{ fmtCredits(credits.period_granted) }}
                </div>
              </div>
              <div class="pf__stat">
                <div class="pf__stat-label">我的 API Key</div>
                <div class="pf__stat-value">{{ assets?.credential_count ?? 0 }}</div>
                <div class="pf__stat-sub">自带 Key 调用不扣积分</div>
              </div>
            </div>

            <el-alert
              v-if="credits && !credits.has_grant"
              type="info"
              title="本月积分尚未发放"
              show-icon
              :closable="false"
              style="margin: 12px 0"
            >
              账期 {{ credits.period }} 还没有发放记录。
              积分由学校平台账户按月发放，发放后即可使用。
            </el-alert>

            <el-alert
              v-else-if="isReallyLow"
              type="warning"
              title="积分余额偏低"
              show-icon
              :closable="false"
              style="margin: 12px 0"
            >
              余额已低于 {{ fmtCredits(credits?.low_threshold) }} 分，
              联系学校平台账户发放。
            </el-alert>

            <section class="zf-card">
              <div class="zf-card__body">
                <div class="pf__tip">
                  积分由学校平台账户按月发放，<b>不结转、不可自助充值</b>。
                  本页只显示汇总，
                  <RouterLink class="pf__link" :to="{ name: 'Billing' }">
                    逐笔流水与自定义 Key 在「积分与凭证」页
                  </RouterLink>
                  。两侧数字来自同一个后端口径，不会不一致。
                </div>
              </div>
            </section>
          </template>
        </el-tab-pane>
      </el-tabs>
    </template>
  </div>
</template>

<style scoped>
.pf {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.pf__acts {
  display: flex;
  gap: 8px;
}

/* ── 身份条 ── */
.pf__idcard {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 16px 18px;
}
.pf__avatar {
  width: 48px;
  height: 48px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  font-weight: 600;
  color: var(--zf-text-inverse);
  background: var(--zf-brand);
  flex: 0 0 auto;
}
.pf__idmain {
  min-width: 0;
}
.pf__name {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 17px;
  font-weight: 600;
  color: var(--zf-text-primary);
}
.pf__meta {
  margin-top: 4px;
  font-size: 13px;
  color: var(--zf-text-tertiary);
}
.pf__dot {
  margin: 0 6px;
}

/* ── 表单 ── */
.pf__form {
  max-width: 520px;
}
.pf__tip {
  font-size: 12px;
  line-height: 1.6;
  color: var(--zf-text-tertiary);
  margin-top: 4px;
}
.pf__tip--warn {
  color: var(--zf-warning);
}
/* 指向「积分与凭证」的入口：只在这一句里出现，所以样式就地定义 */
.pf__link {
  color: var(--zf-brand);
  text-decoration: none;
}
.pf__link:hover {
  text-decoration: underline;
}

/* ── 资产卡 ── */
.pf__stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
}
.pf__stat {
  border: 1px solid var(--zf-border);
  border-radius: 10px;
  padding: 14px 16px;
  background: var(--zf-bg-surface);
}
.pf__stat-label {
  font-size: 12px;
  color: var(--zf-text-tertiary);
}
.pf__stat-value {
  margin-top: 6px;
  font-size: 26px;
  font-weight: 600;
  line-height: 1.2;
  color: var(--zf-text-primary);
  font-variant-numeric: tabular-nums;
}
/* ★ 余额偏低用警示色，不用红色：红色在本项目里表示「失败/亏损」 */
.pf__stat-value--low {
  color: var(--zf-warning);
}
.pf__stat-sub {
  margin-top: 4px;
  font-size: 12px;
  color: var(--zf-text-tertiary);
}
</style>
