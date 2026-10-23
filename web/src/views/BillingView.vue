<script setup lang="ts">
/**
 * 积分与凭证（`/billing`，U27）。
 *
 * 三块：**我的积分 / 自定义 Key / 学校配置**（第三块仅学校平台账户可见）。
 *
 * ★★ 四条刻意的设计，每条都是为了挡住一类真实故障
 *
 *   1. **余额只读 `billing/account/`，绝不自己 `sum(流水)`**
 *      流水接口是分页的，前端加总只会得到「当前这一页之和」，
 *      而它看起来是个精确数字 —— 这是最危险的一种错误。
 *      余额与个人中心、与对账脚本共用同一个后端函数，三处必然一致。
 *
 *   2. **流水表把「变动后余额」列出来**
 *      有了它，老师可以逐笔核对「扣完这笔还剩多少」，
 *      不必去加流水。这一列就是「流水能与账单逐笔对上」的界面形态。
 *
 *   3. **扣减不标红**
 *      红色在本项目里表示「失败/异常」（对账不平、Key 无效、调用报错）。
 *      日常扣费不是异常，标红会让整张表看起来像一片故障。
 *      增减靠 **正负号 + 等宽数字**区分，颜色只留给真正的问题。
 *
 *   4. **Key 明文只在提交那一刻存在**
 *      输入框提交后立即清空，列表只显示后端返回的 `****末四位`。
 *      前端不留明文、不回填、不做「显示密码」的眼睛按钮 ——
 *      一旦能显示，它就会出现在投屏上。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import * as B from '@/api/billing'
import { ApiError } from '@/api/http'
import type { CreditSnapshot } from '@/api/profile'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

/** 学校平台账户才能看到第三块（与后端 `IsSchoolAdmin` 一致）。 */
const canManage = computed(() => auth.can('org.settings'))

const tab = ref<'credits' | 'keys' | 'org'>('credits')

function errText(e: unknown): string {
  if (e instanceof ApiError) return e.message
  return e instanceof Error ? e.message : '操作失败'
}

function fmtNum(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === '') return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return String(v)
  return Number.isInteger(n) ? String(n) : n.toFixed(2)
}

function fmtDate(v: string | null | undefined): string {
  if (!v) return '—'
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return v
  return d.toLocaleString('zh-CN', { hour12: false })
}

/** 带符号的变动额。★ 不减成"绝对值的彩色数字"：符号本身就是信息。 */
function fmtDelta(v: string): string {
  const n = Number(v)
  if (Number.isNaN(n)) return v
  const s = Number.isInteger(n) ? String(Math.abs(n)) : Math.abs(n).toFixed(4)
  return n >= 0 ? `+${s}` : `−${s}`
}

// ══════════════════════════════════════════════════════════════
//  我的积分
// ══════════════════════════════════════════════════════════════

const snapshot = ref<CreditSnapshot | null>(null)
const loadingSnap = ref(false)

const rows = ref<B.LedgerRow[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 20
const loadingRows = ref(false)

const filters = reactive<{ direction: B.LedgerDirection | ''; period: string }>({
  direction: '',
  period: '',
})

/**
 * 是否真的「余额偏低」。
 *
 * ★ 必须叠加 `has_grant`：本月还没发积分时余额当然是 0，
 *   那不是「低」，是「还没给」。两者要老师做的事完全不同 ——
 *   一个是等着，一个是去找管理员。
 */
const isReallyLow = computed(
  () => Boolean(snapshot.value?.has_grant) && Boolean(snapshot.value?.is_low),
)

async function loadSnapshot(): Promise<void> {
  loadingSnap.value = true
  try {
    snapshot.value = await B.getAccount()
  } catch (e) {
    ElMessage.error(errText(e))
  } finally {
    loadingSnap.value = false
  }
}

async function loadLedger(toPage?: number): Promise<void> {
  if (toPage) page.value = toPage
  loadingRows.value = true
  try {
    const res = await B.getLedger({
      direction: filters.direction,
      period: filters.period,
      page: page.value,
      page_size: pageSize,
    })
    rows.value = res.items
    total.value = res.total
  } catch (e) {
    ElMessage.error(errText(e))
  } finally {
    loadingRows.value = false
  }
}

function resetFilters(): void {
  filters.direction = ''
  filters.period = ''
  void loadLedger(1)
}

// ══════════════════════════════════════════════════════════════
//  自定义 Key
// ══════════════════════════════════════════════════════════════

const keys = ref<B.ApiKeyRow[]>([])
const kekSource = ref<'env' | 'derived'>('derived')
const loadingKeys = ref(false)
const testingId = ref<number | null>(null)

const keyDialog = ref(false)
const savingKey = ref(false)
const keyForm = reactive({
  scope: 'PERSONAL' as B.CredentialScope,
  provider: 'openai_compat',
  api_key: '',
  base_url: '',
})

async function loadKeys(): Promise<void> {
  loadingKeys.value = true
  try {
    const res = await B.listKeys()
    keys.value = res.items
    kekSource.value = res.kek_source
  } catch (e) {
    ElMessage.error(errText(e))
  } finally {
    loadingKeys.value = false
  }
}

function openKeyDialog(): void {
  keyForm.scope = 'PERSONAL'
  keyForm.provider = 'openai_compat'
  keyForm.api_key = ''
  keyForm.base_url = ''
  keyDialog.value = true
}

async function saveKey(): Promise<void> {
  if (!keyForm.api_key.trim()) {
    ElMessage.warning('请填写 API Key')
    return
  }
  savingKey.value = true
  try {
    await B.createKey({
      scope: keyForm.scope,
      provider: keyForm.provider,
      api_key: keyForm.api_key.trim(),
      base_url: keyForm.base_url.trim(),
    })
    // ★ 立刻清空明文：它已经落库加密，前端没有理由再留一份
    keyForm.api_key = ''
    keyDialog.value = false
    ElMessage.success('已保存')
    await loadKeys()
  } catch (e) {
    ElMessage.error(errText(e))
  } finally {
    savingKey.value = false
  }
}

async function toggleKey(row: B.ApiKeyRow): Promise<void> {
  const next: B.CredentialStatus = row.status === 'ACTIVE' ? 'DISABLED' : 'ACTIVE'
  try {
    await B.setKeyStatus(row.id, next)
    ElMessage.success(next === 'ACTIVE' ? '已启用' : '已停用')
    await loadKeys()
  } catch (e) {
    ElMessage.error(errText(e))
  }
}

async function removeKey(row: B.ApiKeyRow): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '删除后这把 Key 立即不可用，用它的调用会退回学校配的 Key 或平台默认。继续吗？',
      '删除 Key',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await B.deleteKey(row.id)
    ElMessage.success('已删除')
    await loadKeys()
  } catch (e) {
    ElMessage.error(errText(e))
  }
}

async function runKeyTest(row: B.ApiKeyRow): Promise<void> {
  testingId.value = row.id
  try {
    const res = await B.testKey(row.id)
    if (res.ok) ElMessage.success(`连通正常（${res.message}）`)
    else ElMessage.error(res.message || '连通测试失败')
    await loadKeys()
  } catch (e) {
    ElMessage.error(errText(e))
  } finally {
    testingId.value = null
  }
}

// ══════════════════════════════════════════════════════════════
//  学校配置（仅学校平台账户）
// ══════════════════════════════════════════════════════════════

const SETTING_ORDER: B.SettingField[] = [
  'credit_yuan_per_point',
  'credit_monthly_quota_teacher',
  'credit_monthly_quota_org_admin',
  'credit_low_balance_threshold',
]

const settings = ref<B.CreditSettings | null>(null)
const settingsForm = reactive<Record<string, string>>({})
const savingSettings = ref(false)

const orgAccounts = ref<B.OrgAccountRow[]>([])
const loadingAccounts = ref(false)

const granting = ref(false)
const grantResult = ref<B.GrantRunResult | null>(null)

const reconcile = ref<B.ReconcileResult | null>(null)
const loadingRecon = ref(false)

const adjustDialog = ref(false)
const adjusting = ref(false)
const adjustForm = reactive({
  user_id: null as number | null,
  target: '',
  delta: '',
  remark: '',
})

async function loadSettings(): Promise<void> {
  try {
    const res = await B.getSettings()
    settings.value = res.settings
    for (const f of SETTING_ORDER) settingsForm[f] = res.settings[f].value
  } catch (e) {
    ElMessage.error(errText(e))
  }
}

async function saveSettings(): Promise<void> {
  if (!settings.value) return
  // ★ 只提交**改动的**项：后端 `validate` 要求至少一项，
  //   而全量提交会让审计日志里出现一堆「值没变的修改」。
  const payload: Partial<Record<B.SettingField, string>> = {}
  for (const f of SETTING_ORDER) {
    if (settingsForm[f] !== settings.value[f].value) payload[f] = settingsForm[f]
  }
  if (!Object.keys(payload).length) {
    ElMessage.info('没有改动')
    return
  }
  savingSettings.value = true
  try {
    const res = await B.putSettings(payload)
    settings.value = res.settings
    for (const f of SETTING_ORDER) settingsForm[f] = res.settings[f].value
    ElMessage.success('积分规则已保存')
    // ★ 规则变了，我的余额展示（单价、阈值）也要跟着变
    await Promise.all([loadSnapshot(), loadLedger(1)])
  } catch (e) {
    ElMessage.error(errText(e))
  } finally {
    savingSettings.value = false
  }
}

async function loadAccounts(): Promise<void> {
  loadingAccounts.value = true
  try {
    const res = await B.getOrgAccounts()
    orgAccounts.value = res.items
  } catch (e) {
    ElMessage.error(errText(e))
  } finally {
    loadingAccounts.value = false
  }
}

async function doGrant(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '按当前规则给本机构所有账户发放本月积分。重复点击不会重复发放。',
      '发放本月积分',
      { type: 'info', confirmButtonText: '发放', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  granting.value = true
  try {
    grantResult.value = await B.runGrant({})
    ElMessage.success('发放完成')
    await loadAccounts()
  } catch (e) {
    ElMessage.error(errText(e))
  } finally {
    granting.value = false
  }
}

async function loadReconcile(): Promise<void> {
  loadingRecon.value = true
  try {
    reconcile.value = await B.getReconcile()
  } catch (e) {
    ElMessage.error(errText(e))
  } finally {
    loadingRecon.value = false
  }
}

function openAdjust(row: B.OrgAccountRow): void {
  adjustForm.user_id = row.user_id
  adjustForm.target = row.real_name || row.username || `#${row.user_id}`
  adjustForm.delta = ''
  adjustForm.remark = ''
  adjustDialog.value = true
}

async function submitAdjust(): Promise<void> {
  const delta = adjustForm.delta.trim()
  if (!delta || Number(delta) === 0) {
    ElMessage.warning('调账数额不能为 0')
    return
  }
  if (!adjustForm.remark.trim()) {
    ElMessage.warning('请填写调账原因（审计要用）')
    return
  }
  adjusting.value = true
  try {
    await B.adjustCredits({
      user_id: adjustForm.user_id,
      delta,
      remark: adjustForm.remark.trim(),
    })
    adjustDialog.value = false
    ElMessage.success('已调账')
    await Promise.all([loadAccounts(), loadLedger(1), loadSnapshot()])
  } catch (e) {
    ElMessage.error(errText(e))
  } finally {
    adjusting.value = false
  }
}

// ══════════════════════════════════════════════════════════════
//  初始化
// ══════════════════════════════════════════════════════════════

function scopeLabel(s: B.CredentialScope): string {
  return s === 'ORG' ? '机构共用' : '仅自己'
}

function directionTagType(
  d: B.LedgerDirection,
): 'success' | 'info' | 'warning' | 'danger' | 'primary' {
  if (d === 'GRANT') return 'success'
  if (d === 'ADJUST') return 'warning'
  if (d === 'REFUND') return 'info'
  return 'info'
}

onMounted(async () => {
  await Promise.all([loadSnapshot(), loadLedger(1), loadKeys()])
  if (canManage.value) {
    await Promise.all([loadSettings(), loadAccounts(), loadReconcile()])
  }
})
</script>

<template>
  <div class="bl">
    <header class="zf-page-head">
      <div>
        <h1 class="zf-page-title">积分与凭证</h1>
        <p class="zf-page-desc">
          余额、逐笔流水与自定义 API Key。积分由学校平台账户按月发放，
          <b>不结转、不可自助充值</b>。
        </p>
      </div>
      <div class="bl__acts">
        <el-button
          :loading="loadingSnap || loadingRows"
          @click="loadSnapshot(); loadLedger()"
        >
          刷新
        </el-button>
      </div>
    </header>

    <el-tabs v-model="tab">
      <!-- ════════════════ 我的积分 ════════════════ -->
      <el-tab-pane label="我的积分" name="credits">
        <div v-if="snapshot" class="bl__stats">
          <div class="bl__stat">
            <div class="bl__stat-label">可用积分</div>
            <div class="bl__stat-value" :class="{ 'bl__stat-value--low': isReallyLow }">
              {{ fmtNum(snapshot.balance) }}
            </div>
            <div class="bl__stat-sub">
              单价 {{ snapshot.price_yuan_per_point }} 元 / 分
            </div>
          </div>
          <div class="bl__stat">
            <div class="bl__stat-label">本月已用</div>
            <div class="bl__stat-value">{{ fmtNum(snapshot.period_used) }}</div>
            <div class="bl__stat-sub">
              账期 {{ snapshot.period }} · 已发放
              {{ fmtNum(snapshot.period_granted) }}
            </div>
          </div>
          <div class="bl__stat">
            <div class="bl__stat-label">在途冻结</div>
            <div class="bl__stat-value">{{ fmtNum(snapshot.frozen) }}</div>
            <div class="bl__stat-sub">AI 调用进行中的预扣，结算后自动结清</div>
          </div>
          <div class="bl__stat">
            <div class="bl__stat-label">低余额阈值</div>
            <div class="bl__stat-value">{{ fmtNum(snapshot.low_threshold) }}</div>
            <div class="bl__stat-sub">低于此值时提醒额度将尽</div>
          </div>
        </div>

        <el-alert
          v-if="snapshot && !snapshot.has_grant"
          type="info"
          title="本月积分尚未发放"
          show-icon
          :closable="false"
          style="margin: 12px 0"
        >
          账期 {{ snapshot.period }} 还没有发放记录。发放后即可使用。
        </el-alert>
        <el-alert
          v-else-if="isReallyLow"
          type="warning"
          title="积分余额偏低"
          show-icon
          :closable="false"
          style="margin: 12px 0"
        >
          余额已低于 {{ fmtNum(snapshot?.low_threshold) }} 分，请联系学校平台账户。
        </el-alert>

        <section class="zf-card">
          <div class="zf-card__head">
            <h2 class="zf-card__title">流水明细</h2>
            <span class="zf-dim">
              「变动后余额」可逐笔核对，无需自己累加
            </span>
          </div>
          <div class="zf-card__body">
            <div class="bl__filters">
              <el-select
                v-model="filters.direction"
                placeholder="全部方向"
                clearable
                style="width: 150px"
                @change="loadLedger(1)"
              >
                <el-option
                  v-for="(label, key) in B.DIRECTION_LABEL"
                  :key="key"
                  :label="label"
                  :value="key"
                />
              </el-select>
              <el-date-picker
                v-model="filters.period"
                type="month"
                placeholder="全部账期"
                value-format="YYYY-MM"
                clearable
                style="width: 160px"
                @change="loadLedger(1)"
              />
              <el-button size="small" @click="resetFilters">重置</el-button>
            </div>

            <el-table
              v-loading="loadingRows"
              :data="rows"
              size="small"
              empty-text="还没有流水。AI 调用或积分发放后会出现记录"
              style="width: 100%"
            >
              <el-table-column label="时间" width="170">
                <template #default="{ row }">{{ fmtDate(row.created_at) }}</template>
              </el-table-column>
              <el-table-column label="方向" width="90">
                <template #default="{ row }">
                  <el-tag size="small" :type="directionTagType(row.direction)">
                    {{ B.DIRECTION_LABEL[row.direction as B.LedgerDirection] }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="场景" width="150">
                <template #default="{ row }">
                  <!-- ★ 未知场景显示原码而不是「其他」：流水是核对用的，
                       出现没见过的场景说明「有新任务接进计费了」 -->
                  <span :title="row.scene">{{ B.sceneLabel(row.scene) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="变动" width="110" align="right">
                <template #default="{ row }">
                  <span class="bl__num">{{ fmtDelta(row.delta) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="变动后余额" width="120" align="right">
                <template #default="{ row }">
                  <span class="bl__num">{{ fmtNum(row.balance_after) }}</span>
                </template>
              </el-table-column>
              <el-table-column prop="period" label="账期" width="90" />
              <el-table-column label="备注" min-width="200">
                <template #default="{ row }">
                  <span class="bl__remark">{{ row.remark || '—' }}</span>
                </template>
              </el-table-column>
            </el-table>

            <el-pagination
              v-if="total > pageSize"
              layout="prev, pager, next, total"
              :total="total"
              :page-size="pageSize"
              :current-page="page"
              style="margin-top: 12px"
              @current-change="loadLedger"
            />
          </div>
        </section>
      </el-tab-pane>

      <!-- ════════════════ 自定义 Key ════════════════ -->
      <el-tab-pane label="自定义 Key" name="keys">
        <el-alert
          v-if="kekSource === 'derived'"
          type="warning"
          title="服务端未配置 ZF_SECRET_KEK，Key 是用 SECRET_KEY 派生的密钥加密的"
          show-icon
          :closable="false"
          style="margin-bottom: 12px"
        >
          开发环境可接受；<b>生产必须显式配置并备份</b>。
          否则一旦更换 SECRET_KEY，所有人已保存的 Key 会一起解不开，
          而且只有到下次调用时才会暴露。
        </el-alert>

        <section class="zf-card">
          <div class="zf-card__head">
            <h2 class="zf-card__title">我的 API Key</h2>
            <el-button type="primary" size="small" @click="openKeyDialog">
              新增 Key
            </el-button>
          </div>
          <div class="zf-card__body">
            <p class="bl__hint">
              用自己的 Key 调用 AI <b>不扣积分</b>。优先级：个人 Key → 机构 Key → 平台默认。
              明文只在提交那一刻存在，保存后只回末四位。
            </p>

            <el-table
              v-loading="loadingKeys"
              :data="keys"
              size="small"
              empty-text="还没有自定义 Key —— 不配也能用，只是走学校/平台的额度"
              style="width: 100%"
            >
              <el-table-column label="作用域" width="110">
                <template #default="{ row }">
                  <el-tag size="small" effect="plain">
                    {{ scopeLabel(row.scope) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="provider" label="厂商" width="140" />
              <el-table-column label="Key" width="130">
                <template #default="{ row }">
                  <span class="bl__num">{{ row.key_masked }}</span>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="90">
                <template #default="{ row }">
                  <el-tag
                    size="small"
                    :type="row.status === 'ACTIVE' ? 'success' : 'info'"
                  >
                    {{ row.status === 'ACTIVE' ? '启用' : '停用' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="上次测试" min-width="200">
                <template #default="{ row }">
                  <template v-if="row.last_tested_at">
                    <el-tag
                      size="small"
                      :type="row.last_test_ok ? 'success' : 'danger'"
                      effect="plain"
                    >
                      {{ row.last_test_ok ? '通过' : '失败' }}
                    </el-tag>
                    <span class="bl__dimline">
                      {{ fmtDate(row.last_tested_at) }} · {{ row.last_test_message }}
                    </span>
                  </template>
                  <span v-else class="bl__dimline">未测试</span>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="220">
                <template #default="{ row }">
                  <el-button
                    size="small"
                    :loading="testingId === row.id"
                    @click="runKeyTest(row)"
                  >
                    测试
                  </el-button>
                  <el-button size="small" @click="toggleKey(row)">
                    {{ row.status === 'ACTIVE' ? '停用' : '启用' }}
                  </el-button>
                  <el-button size="small" type="danger" plain @click="removeKey(row)">
                    删除
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </section>
      </el-tab-pane>

      <!-- ════════════════ 学校配置 ════════════════ -->
      <el-tab-pane v-if="canManage" label="学校配置" name="org">
        <section class="zf-card" style="margin-bottom: 12px">
          <div class="zf-card__head">
            <h2 class="zf-card__title">积分规则</h2>
            <el-button type="primary" size="small" :loading="savingSettings" @click="saveSettings">
              保存
            </el-button>
          </div>
          <div class="zf-card__body">
            <el-form label-width="140px" class="bl__form">
              <el-form-item
                v-for="f in SETTING_ORDER"
                :key="f"
                :label="B.SETTING_META[f].label"
              >
                <div class="bl__setting">
                  <el-input v-model="settingsForm[f]" style="width: 200px">
                    <template v-if="settings?.[f]" #append>
                      <el-tag
                        size="small"
                        :type="settings[f].source === 'org' ? 'success' : 'info'"
                        effect="plain"
                      >
                        {{ settings[f].source === 'org' ? '学校自配' : '平台默认' }}
                      </el-tag>
                    </template>
                  </el-input>
                  <span class="bl__hint">{{ B.SETTING_META[f].hint }}</span>
                </div>
              </el-form-item>
            </el-form>
          </div>
        </section>

        <section class="zf-card" style="margin-bottom: 12px">
          <div class="zf-card__head">
            <h2 class="zf-card__title">发放与对账</h2>
            <div class="bl__acts">
              <el-button size="small" :loading="granting" @click="doGrant">
                发放本月积分
              </el-button>
              <el-button size="small" :loading="loadingRecon" @click="loadReconcile">
                重新对账
              </el-button>
            </div>
          </div>
          <div class="zf-card__body">
            <el-alert
              v-if="grantResult"
              type="success"
              :closable="false"
              show-icon
              style="margin-bottom: 12px"
            >
              账期 {{ grantResult.period }}：发放 {{ grantResult.granted_users }} 人，
              合计 {{ grantResult.total_credits }} 分；
              跳过 {{ grantResult.skipped_users }} 人（本月已发过）。
            </el-alert>

            <template v-if="reconcile">
              <el-alert
                v-if="reconcile.ok"
                type="success"
                title="对账通过：账本自洽、与 AI 成本差 0"
                show-icon
                :closable="false"
              >
                检查 {{ reconcile.accounts_checked }} 个账户、
                {{ reconcile.ai_check.holds_checked }} 笔已结算调用，
                差额 {{ reconcile.ai_check.diff }} 分。
              </el-alert>
              <template v-else>
                <el-alert
                  type="error"
                  title="对账未通过"
                  show-icon
                  :closable="false"
                  style="margin-bottom: 8px"
                >
                  四类问题分开看：账本不平=程序缺陷，欠费=要补额度，
                  在途超时=等结算或人工关单，成本差=折算口径有问题。
                </el-alert>
                <el-descriptions :column="2" border size="small">
                  <el-descriptions-item label="账本不平">
                    {{ reconcile.balance_mismatch.length }} 个账户
                  </el-descriptions-item>
                  <el-descriptions-item label="欠费单">
                    {{ reconcile.shortfalls.length }} 笔
                  </el-descriptions-item>
                  <el-descriptions-item label="在途超时">
                    {{ reconcile.stale_holds.length }} 笔
                  </el-descriptions-item>
                  <el-descriptions-item label="成本折算差">
                    {{ reconcile.ai_check.diff }} 分（{{ reconcile.ai_check.variance.length }} 笔）
                  </el-descriptions-item>
                </el-descriptions>
              </template>
            </template>
          </div>
        </section>

        <section class="zf-card">
          <div class="zf-card__head">
            <h2 class="zf-card__title">全校账户</h2>
            <el-button size="small" :loading="loadingAccounts" @click="loadAccounts">
              刷新
            </el-button>
          </div>
          <div class="zf-card__body">
            <el-table
              v-loading="loadingAccounts"
              :data="orgAccounts"
              size="small"
              empty-text="本机构还没有积分账户"
              style="width: 100%"
            >
              <el-table-column label="教师" min-width="160">
                <template #default="{ row }">
                  {{ row.real_name || row.username || '—' }}
                  <span class="bl__dimline">{{ row.username }}</span>
                </template>
              </el-table-column>
              <el-table-column label="余额" width="120" align="right">
                <template #default="{ row }">
                  <span class="bl__num">{{ fmtNum(row.balance) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="冻结" width="110" align="right">
                <template #default="{ row }">
                  <span class="bl__num">{{ fmtNum(row.frozen) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="90">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.status === 'ACTIVE' ? 'success' : 'danger'">
                    {{ row.status === 'ACTIVE' ? '正常' : '冻结' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="100">
                <template #default="{ row }">
                  <el-button size="small" @click="openAdjust(row)">调账</el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </section>
      </el-tab-pane>
    </el-tabs>

    <!-- ── 新增 Key ── -->
    <el-dialog v-model="keyDialog" title="新增 API Key" width="480px">
      <el-form label-width="96px">
        <el-form-item label="作用域">
          <el-radio-group v-model="keyForm.scope">
            <el-radio value="PERSONAL">仅自己</el-radio>
            <el-radio v-if="canManage" value="ORG">机构共用</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="厂商">
          <el-input v-model="keyForm.provider" placeholder="openai_compat" />
        </el-form-item>
        <el-form-item label="API Key">
          <el-input
            v-model="keyForm.api_key"
            type="password"
            show-password
            autocomplete="off"
            placeholder="sk-..."
          />
          <div class="bl__hint">
            保存即加密，之后只能看到末四位。同一作用域+厂商视为覆盖。
          </div>
        </el-form-item>
        <el-form-item label="Base URL">
          <el-input v-model="keyForm.base_url" placeholder="留空用厂商默认地址" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="keyDialog = false">取消</el-button>
        <el-button type="primary" :loading="savingKey" @click="saveKey">保存</el-button>
      </template>
    </el-dialog>

    <!-- ── 调账 ── -->
    <el-dialog v-model="adjustDialog" title="人工调账" width="440px">
      <el-form label-width="80px">
        <el-form-item label="对象">
          <el-input :model-value="adjustForm.target" disabled />
        </el-form-item>
        <el-form-item label="数额">
          <el-input v-model="adjustForm.delta" placeholder="正数增加，负数扣减" />
        </el-form-item>
        <el-form-item label="原因">
          <el-input
            v-model="adjustForm.remark"
            type="textarea"
            :rows="2"
            maxlength="200"
            placeholder="审计要用，写清为什么加"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="adjustDialog = false">取消</el-button>
        <el-button type="primary" :loading="adjusting" @click="submitAdjust">
          确认调账
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.bl {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.bl__acts {
  display: flex;
  gap: 8px;
}

.bl__stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
}
.bl__stat {
  border: 1px solid var(--zf-border);
  border-radius: var(--zf-radius-lg);
  padding: 14px 16px;
  background: var(--zf-bg-surface);
}
.bl__stat-label {
  font-size: 12px;
  color: var(--zf-text-tertiary);
}
.bl__stat-value {
  margin-top: 6px;
  font-size: 26px;
  font-weight: 600;
  line-height: 1.2;
  color: var(--zf-text-primary);
  font-variant-numeric: tabular-nums;
}
/* ★ 低余额用警示色不用红色：红色在本项目里表示「失败/异常」 */
.bl__stat-value--low {
  color: var(--zf-warning);
}
.bl__stat-sub {
  margin-top: 4px;
  font-size: 12px;
  color: var(--zf-text-tertiary);
}

.bl__filters {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.bl__num {
  font-variant-numeric: tabular-nums;
  font-family: var(--zf-font-mono);
}
.bl__remark {
  color: var(--zf-text-secondary);
}
.bl__hint {
  font-size: 12px;
  line-height: 1.6;
  color: var(--zf-text-tertiary);
  margin: 0;
}
.bl__dimline {
  margin-left: 6px;
  font-size: 12px;
  color: var(--zf-text-tertiary);
}
.bl__form {
  max-width: 720px;
}
.bl__setting {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
</style>
